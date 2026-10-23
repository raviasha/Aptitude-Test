"""Assemble reviewed quantitative visual corrections without changing latest/.

Only explicit reviewed records are changed. Original and revised banks can
coexist: revised bank names get a dated suffix, preserving assessment history.
The output is local/staged, not a published or freshly vision-sealed release.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.build_reviewed_presentation_packages import build_packages
from scripts.prepare_quantitative_chart_text_review import (
    records as chart_source_records, verify as verify_chart_sources, reviewed_entries,
)


def sha(content):
    return hashlib.sha256(content).hexdigest()


def validate_output_dir(output, baseline):
    output, protected = Path(output).resolve(), Path(baseline).resolve().parent
    if output == protected or output.is_relative_to(protected) or protected.is_relative_to(output):
        raise ValueError('Output must not overlap protected latest question banks.')


def validate_chart_review(entries):
    source = chart_source_records()
    # The review-time corpus digest binds all fields, including image hashes;
    # records() also checks those hashes against the actual embedded PNG bytes.
    verify_chart_sources(source)
    if entries != reviewed_entries(source):
        raise ValueError('Chart manifest does not match the reviewed selections.')


def read_package(path):
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        rows = {}
        for name in manifest['question_files']:
            data = archive.read(name).decode('utf-8')
            if name.endswith('.jsonl'):
                records = [json.loads(line) for line in data.splitlines() if line.strip()]
            else:
                document = json.loads(data)
                records = document.get('questions', []) if isinstance(document, dict) else document
            for row in records:
                if row['key'] in rows:
                    raise ValueError('Duplicate source key')
                rows[row['key']] = row
        return manifest, rows


def write_master(path, chapters, readme):
    hashes = {}
    with zipfile.ZipFile(path, 'x', compression=zipfile.ZIP_STORED) as archive:
        for chapter in chapters:
            data = chapter.read_bytes()
            member = f'chapters/{chapter.name}'
            archive.writestr(member, data)
            hashes[member] = sha(data)
        archive.writestr('README.txt', readme)
        archive.writestr('SHA256SUMS.json', json.dumps(hashes, indent=2))
    with zipfile.ZipFile(path) as archive:
        assert all(sha(archive.read(member)) == digest for member, digest in hashes.items())
    return {'file': path.name, 'sha256': sha(path.read_bytes()), 'chapters': len(hashes)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--geometry-review', type=Path, required=True)
    parser.add_argument('--chart-review', type=Path, required=True)
    parser.add_argument('--source-pdf', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    baseline = ROOT/'question-banks/latest/quantitative-aptitude'
    output = args.output.resolve()
    validate_output_dir(output, baseline)
    if output.exists():
        raise FileExistsError(output)
    originals = sorted(baseline.glob('*.zip'))
    assert len(originals) == 39
    before_hashes = {p.name: sha(p.read_bytes()) for p in originals}
    review = json.loads(args.geometry_review.read_text(encoding='utf-8'))
    chart_records = json.loads(args.chart_review.read_text(encoding='utf-8'))
    validate_chart_review(chart_records)
    assert len(chart_records) == 306
    by_chapter = {}
    for record in chart_records:
        chapter = str(int(record['key'].split('-')[0][2:]))
        by_chapter.setdefault(chapter, []).append(record)
    for source in originals:
        manifest, records = read_package(source)
        chapter = str(next(iter(records.values()))['chapter'])
        if chapter in by_chapter:
            assert chapter not in review['chapters']
            review['chapters'][chapter] = {'baseline_sha256': before_hashes[source.name],
                                          'records': by_chapter[chapter]}
        if chapter in review['chapters']:
            review['chapters'][chapter]['bank_name'] = manifest['bank_name'] + ' [Visual revision 2026-10-22]'
    assert set(review['chapters']) == {'24', '25', '36', '37', '38', '39'}
    assert sum(len(c['records']) for c in review['chapters'].values()) == 336
    output.mkdir(parents=True)
    combined = output/'review-manifest.json'
    combined.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding='utf-8')
    generated = build_packages(baseline, combined, output/'chapters', args.source_pdf)
    validation = []
    for generated_zip in generated:
        old_manifest, old_rows = read_package(baseline/generated_zip.name)
        new_manifest, new_rows = read_package(generated_zip)
        assert old_rows.keys() == new_rows.keys()
        assert {k: v for k, v in old_manifest.items() if k != 'bank_name'} == {
            k: v for k, v in new_manifest.items() if k != 'bank_name'}
        chapter = str(next(iter(old_rows.values()))['chapter'])
        reviewed = {r['key']: r for r in review['chapters'][chapter]['records']}
        for key, original in old_rows.items():
            revised = new_rows[key]
            if key not in reviewed:
                assert original == revised, key
                continue
            assert {k: v for k, v in original.items() if k not in {'question_text', 'display_media'}} == {
                k: v for k, v in revised.items() if k not in {'question_text', 'display_media'}}, key
            assert revised['question_text'] == reviewed[key]['question_text']
            assert revised['display_media']['question']['placement'] == 'context'
            assert revised['display_media']['question']['alt_text'] == reviewed[key].get('fallback_text', original['question_text'])
        validation.append({'file': generated_zip.name, 'sha256': sha(generated_zip.read_bytes()),
                           'chapter': chapter, 'questions': len(new_rows), 'changed': len(reviewed),
                           'bank_name': new_manifest['bank_name']})

    # Test real storage and the strict client wire shape against a disposable DB.
    # Never open or mutate the user's installed coordinator/student data.
    import app
    with tempfile.TemporaryDirectory(prefix='ksat-visual-import-') as tmp:
        root = Path(tmp)
        with patch.multiple(app, DATA_DIR=root, DB_PATH=root/'aptitude.db',
                            BACKUP_DIR=root/'backups', QUESTION_BANKS_DIR=root/'Question Banks'):
            app.ensure_schema()
            imported_count = 0
            for path in generated:
                name, rows, stimuli, version, metadata = app.parse_question_package_with_metadata(io.BytesIO(path.read_bytes()))
                saved = app.save_question_package(name, rows, stimuli, path.name, version, **metadata)
                with app.db() as connection:
                    selected = connection.execute('SELECT * FROM questions WHERE bank_id=?', (saved['bank_id'],)).fetchall()
                    public, assets = app.public_release_material(connection, selected)
                    expected = read_package(path)[1]
                    for q in public:
                        original = expected[q.source_key]
                        assert q.question_text.strip(), q.source_key
                        if 'question' in original.get('display_media', {}):
                            assert q.stimulus is not None and q.stimulus.type == 'image', q.source_key
                            assert q.stimulus.url in assets
                            assert q.display_media.question is None
                    imported_count += len(public)

    readme = (
        'Quantitative Aptitude — reviewed visual update, 2026-10-22\n\n'
        'LOCAL STAGED BUILD; not uploaded to GitHub or newly V2 vision-sealed.\n'
        '30 source diagrams added in chapters 24/25; duplicate chart/table prose\n'
        'removed from 306 questions in chapters 36–39. Stems, required directions,\n'
        'options, answers and solutions retained. Transcriptions remain as image\n'
        'alt text and in the updated client UI as an image-failure fallback.\n\n'
        'Existing installation: import quantitative-visual-updates-6-chapters.zip.\n'
        'New setup: quantitative-aptitude-master-39-chapters.zip contains all 39.\n'
        'Corrected banks have a [Visual revision 2026-10-22] suffix. Select these\n'
        'for NEW tests; do not select both old and revised copies of a chapter.\n'
        'Do not delete older banks, student data or results. Existing test releases\n'
        'are immutable and are not altered by these imports.\n\n'
        'Requires the accompanying Coordinator context-image release change.\n'
        'Legacy clients support that stimulus format; the improved explicit\n'
        'image-failure fallback is in the updated client UI source only.\n'
        'No new installer is built by this packaging script.\n\n'
        'Old metadata is baseline history only. See metadata/presentation-maintenance.json\n'
        'inside corrected chapters for exact before/after hashes and review notes.\n'
    )
    (output/'README.txt').write_text(readme, encoding='utf-8')
    updates = write_master(output/'quantitative-visual-updates-6-chapters.zip', generated, readme)
    replacements = {p.name: p for p in generated}
    master = write_master(output/'quantitative-aptitude-master-39-chapters.zip',
                          [replacements.get(p.name, p) for p in originals], readme)
    assert {p.name: sha(p.read_bytes()) for p in originals} == before_hashes
    report = {'status': 'local_staged_validated', 'source_pdf_sha256': review['source_pdf_sha256'],
              'baseline_archives_unchanged': True, 'answers_options_solutions_unchanged': True,
              'disposable_database_imports_and_public_wire_questions': imported_count,
              'changed_records': 336, 'chapters': validation, 'bundles': [updates, master]}
    (output/'validation.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
