"""Import sealed V2 chapter ZIPs into disposable current-checkout Coordinator data.

Repeat --package ZIP --chapter-dir ROOT_CHAPTER for each sealed bank. Optional
--master ZIP tests the same banks in a separate fresh database. This is an import
compatibility check, not source approval, browser rendering, or publication.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEXT_FIELDS = ('key', 'question_text', 'question_html', 'category', 'chapter',
               'difficulty', 'options', 'correct_answer', 'explanation', 'solution_steps')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def sha(content):
    return hashlib.sha256(content).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def media_items(media):
    if 'question' in media:
        yield 'question', media['question']
    for label, item in media.get('options', {}).items():
        yield 'options.' + label, item
    for index, item in enumerate(media.get('solution', [])):
        yield f'solution.{index}', item


def render_data(record):
    """Comparable field data, excluding ephemeral media paths, not visible text."""
    result = {key: record.get(key, '') for key in TEXT_FIELDS}
    result['display_media'] = {
        field: {'sha256': item['sha256'], 'alt_text': item['alt_text'],
                'placement': item.get('placement', 'replacement')}
        for field, item in media_items(record.get('display_media', {}))
    }
    return result


def load_sealed(package, chapter_dir):
    package, chapter_dir = Path(package).resolve(), Path(chapter_dir).resolve()
    content = package.read_bytes()
    digest = sha(content)
    result = read_json(chapter_dir / 'package-result.json')
    seal_path = Path(result['path']).with_suffix('.package.json')
    seal = read_json(seal_path)
    require(digest == result['sha256'] == seal.get('candidate_sha256'), f'Package seal mismatch: {package}')
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        require(archive.testzip() is None, f'Corrupt sealed ZIP: {package}')
        manifest = json.loads(archive.read('manifest.json'))
        require(manifest['format_version'] == 3, 'This smoke requires sealed format-3 chapter packages')
        questions = []
        for name in manifest['question_files']:
            raw = archive.read(name).decode('utf-8-sig')
            questions.extend(json.loads(line) for line in raw.splitlines() if line.strip()) if name.endswith('.jsonl') else questions.extend(json.loads(raw))
        audit = json.loads(archive.read(manifest['audit_summary_file']))
        lineage = json.loads(archive.read(manifest['lineage_file']))['records']
        require(audit['audit_sha256'] == result['audit_sha256'] == seal.get('audit_sha256'), 'Audit seal mismatch')
        require(len(questions) == result['question_count'] == audit['approved_count'], 'Sealed question count mismatch')
        require(result['rejected_count'] == audit['reviewed_rejection_count'], 'Sealed rejection count mismatch')
    keys = [q['key'] for q in questions]
    require(len(set(keys)) == len(keys), 'Duplicate sealed question keys')
    lineage_by_key = {r['key']: r for r in lineage}
    require(set(lineage_by_key) == set(keys), 'Sealed lineage coverage mismatch')
    references = {}
    for entry in read_json(chapter_dir / 'state/renders.json')['records']:
        artifacts = entry['artifacts']
        key = artifacts['source_key']
        if key not in lineage_by_key:
            continue  # A reviewed rejection may still have historical render state.
        evidence_path = Path(artifacts['import_evidence_path'])
        evidence_bytes = evidence_path.read_bytes()
        require(sha(evidence_bytes) == artifacts['import_evidence_sha256'], f'ROOT reference hash mismatch: {key}')
        evidence = json.loads(evidence_bytes)
        require(evidence['source_key'] == key and evidence['record']['key'] == key, 'ROOT reference key mismatch')
        require(sha(canonical(evidence['record'])) == evidence['imported_record_sha256'] == artifacts['imported_record_sha256'],
                f'ROOT reference record hash mismatch: {key}')
        require(artifacts['candidate_sha256'] == lineage_by_key[key]['candidate_sha256'], f'ROOT reference candidate mismatch: {key}')
        require(key not in references, f'Duplicate ROOT reference: {key}')
        references[key] = {'data': render_data(evidence['record']), 'path': str(evidence_path),
                           'evidence_sha256': sha(evidence_bytes), 'record_sha256': evidence['imported_record_sha256']}
    require(set(references) == set(keys), 'ROOT reference coverage mismatch')
    return {'path': str(package), 'sha256': digest, 'bytes': len(content), 'manifest': manifest,
            'audit_sha256': audit['audit_sha256'], 'questions': questions, 'references': references,
            'seal_path': str(seal_path), 'seal_sha256': sha(seal_path.read_bytes())}


def inspect_bank(app, spec):
    with app.db() as connection:
        bank = connection.execute('SELECT b.*, bk.title AS book_title FROM question_banks b LEFT JOIN books bk ON bk.book_id=b.book_id WHERE bank_name=?',
                                  (spec['manifest']['bank_name'],)).fetchone()
        require(bank is not None, 'Imported bank is missing')
        rows = connection.execute('SELECT * FROM questions WHERE bank_id=?', (bank['bank_id'],)).fetchall()
    require(len(rows) == len(spec['questions']), 'Imported question count mismatch')
    expected = {q['key']: q for q in spec['questions']}
    require({r['source_key'] for r in rows} == set(expected), 'Imported question keys mismatch')
    wanted_title = spec['manifest'].get('book_title')
    if wanted_title is not None:
        wanted_title = app.question_books.normalize_title(wanted_title)
    require(bank['book_title'] == wanted_title, 'Imported book metadata mismatch')
    require(bank['format_version'] == spec['manifest']['format_version'], 'Imported format version mismatch')
    asset_dir = app.question_assets_dir() / str(bank['bank_id'])
    records, assets, media_counts = [], set(), Counter()
    with zipfile.ZipFile(spec['path']) as archive:
        for row in sorted(rows, key=lambda r: r['source_key']):
            key = row['source_key']
            wanted = expected[key]
            stored = json.loads(row['display_media_json'] or '{}')
            raw_media = dict(media_items(wanted.get('display_media', {})))
            require(set(dict(media_items(stored))) == set(raw_media), f'Imported media fields mismatch: {key}')
            for field, item in media_items(stored):
                content = (asset_dir / item['asset_filename']).read_bytes()
                source = archive.read(raw_media[field]['asset'])
                require(content == source and sha(content) == item['sha256'] == raw_media[field]['sha256'], f'Imported media bytes mismatch: {key} {field}')
                width, height = app.question_media.png_dimensions(content)
                require((item['width'], item['height']) == (width, height), f'Imported media dimensions mismatch: {key}')
                assets.add(item['asset_filename'])
                media_counts[field.split('.')[0]] += 1
            actual = {
                'key': key, 'question_text': app.display_question_text(row['question_text'], key),
                'question_html': app.clean_display_text(row['question_html'] or ''),
                'category': row['category'], 'chapter': row['chapter'], 'difficulty': row['difficulty'],
                'options': app.question_options(row), 'correct_answer': row['correct_answer'],
                'explanation': app.clean_display_text(row['explanation'] or ''),
                'solution_steps': app.display_solution_steps(row['question_text'], row['solution_steps'], key),
                'display_media': stored,
            }
            actual_data, expected_data = render_data(actual), render_data(wanted)
            differences = [field for field in actual_data if actual_data[field] != expected_data[field]]
            require(not differences, f'Sealed/imported render-data mismatch: {key}: {differences}')
            require(json.loads(row['option_explanations'] or '{}') == wanted.get('option_explanations', {}), f'Option explanations mismatch: {key}')
            require(row['stimulus_id'] == (wanted.get('stimulus_id') or None), f'Stimulus reference mismatch: {key}')
            reference = spec['references'][key]
            root_differences = [field for field in actual_data if actual_data[field] != reference['data'][field]]
            unexpected_root_differences = [field for field in root_differences if field != 'category']
            require(not unexpected_root_differences,
                    f'ROOT render-data mismatch: {key}: {unexpected_root_differences}; only category may differ')
            records.append({'key': key, 'sealed_match': True, 'render_data_sha256': sha(canonical(actual_data)),
                            'root_differing_fields': root_differences, 'root_reference': reference['path'],
                            'root_reference_sha256': reference['evidence_sha256'],
                            'root_render_record_sha256': reference['record_sha256']})
    require({p.name for p in asset_dir.iterdir() if p.is_file()} == assets, 'Imported asset inventory mismatch')
    return {'bank_name': bank['bank_name'], 'book_title': bank['book_title'], 'question_count': len(rows),
            'package_path': spec['path'], 'package_sha256': spec['sha256'], 'package_bytes': spec['bytes'],
            'manifest': spec['manifest'], 'audit_sha256': spec['audit_sha256'], 'seal_sha256': spec['seal_sha256'],
            'media_reference_count': sum(media_counts.values()), 'media_fields': dict(media_counts),
            'unique_media_assets': len(assets), 'sealed_record_matches': len(records),
            'root_exact_matches': sum(not r['root_differing_fields'] for r in records),
            'root_content_media_matches': sum(not (set(r['root_differing_fields']) - {'category'}) for r in records),
            'records': records}


def worker(request):
    data_dir = Path(os.environ['KSAT_DATA_DIR']).resolve()
    require(data_dir == Path(request['data_dir']).resolve() and data_dir.name.startswith('ksat-import-smoke-'),
            'Worker requires its own disposable data directory')
    require(not any(data_dir.iterdir()), 'Worker data directory must be empty')
    sys.path.insert(0, str(ROOT))
    import app  # Import only inside this fresh process, after setting KSAT_DATA_DIR.
    require(app.DATA_DIR == data_dir and app.DB_PATH.parent == data_dir, 'Application data isolation failed')
    require(app.APP_VERSION == '2.2.3', 'Expected current-main Coordinator 2.2.3')
    app.ensure_schema()
    specs = request['specs']
    for spec in specs:
        require(sha(Path(spec['path']).read_bytes()) == spec['sha256'], 'Input changed before import')
    paths = [Path(request['master'])] if request.get('master') else [Path(s['path']) for s in specs]
    for path in paths:
        content = path.read_bytes()
        limit = app.MAX_UPLOAD_BYTES if request.get('master') else app.MAX_PACKAGE_BYTES
        require(len(content) <= limit, 'Question-bank ZIP exceeds its compressed upload limit')
        result = app.import_uploaded_package(content, path.name, False)
        require(all(r['ok'] for r in result.get('results', [])), f'Importer reported failed chapters: {result}')
    banks = [inspect_bank(app, spec) for spec in specs]
    with app.db() as connection:
        require(connection.execute('SELECT count(*) FROM question_banks').fetchone()[0] == len(specs), 'Unexpected bank inventory')
    return {'kind': 'master' if request.get('master') else 'chapters', 'banks': banks,
            'version': app.APP_VERSION, 'data_dir': str(data_dir),
            'limits': {'upload_bytes': app.MAX_UPLOAD_BYTES, 'chapter_bytes': app.MAX_PACKAGE_BYTES,
                       'expanded_bytes': app.MAX_PACKAGE_UNPACKED_BYTES,
                       'files': app.MAX_PACKAGE_FILES}}


def run(packages, chapter_dirs, master=None):
    require(len(packages) == len(chapter_dirs) and packages, 'Pair every --package with one --chapter-dir')
    specs = [load_sealed(p, d) for p, d in zip(packages, chapter_dirs)]
    if master:
        with zipfile.ZipFile(master) as archive:
            members = [m for m in archive.infolist() if m.filename.lower().endswith('.zip')]
            require(Counter(sha(archive.read(m)) for m in members) == Counter(s['sha256'] for s in specs),
                    'Master chapter bytes do not match the supplied sealed packages')
    phases, temp_paths = [], []
    for selected_master in ([None, str(Path(master).resolve())] if master else [None]):
        with tempfile.TemporaryDirectory(prefix='ksat-import-smoke-') as temporary:
            temp_paths.append(Path(temporary))
            request = {'specs': specs, 'master': selected_master, 'data_dir': temporary}
            result = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), '--worker'],
                                    cwd=ROOT, env={**os.environ, 'KSAT_DATA_DIR': temporary},
                                    input=json.dumps(request), text=True, encoding='utf-8',
                                    capture_output=True, timeout=300)
            require(result.returncode == 0, f'Isolated import failed: {result.stderr.strip()}')
            phases.append(json.loads(result.stdout))
    require(all(not p.exists() for p in temp_paths), 'Temporary data cleanup failed')
    for spec in specs:
        require(sha(Path(spec['path']).read_bytes()) == spec['sha256'], 'Sealed package changed during smoke')
    application_files = [ROOT / 'app.py', ROOT / 'question_media.py', ROOT / 'chapter_repairs.py',
                         *sorted(p for p in (ROOT / 'static').rglob('*') if p.is_file())]
    return {'status': 'passed', 'scope': 'import/render-data compatibility only; no screenshots or approvals',
            'application': {'root': str(ROOT), 'version': phases[0]['version'],
                            'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                            'file_sha256': {p.relative_to(ROOT).as_posix(): sha(p.read_bytes()) for p in application_files}},
            'temporary_data_removed': True, 'phases': phases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, action='append', default=[])
    parser.add_argument('--chapter-dir', type=Path, action='append', default=[])
    parser.add_argument('--master', type=Path)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(json.load(sys.stdin))))
        return 0
    require(args.report is not None and args.report.suffix == '.json', '--report must name a JSON report')
    report_path = args.report.resolve()
    require(all(not report_path.is_relative_to(p.resolve()) for p in args.chapter_dir), 'Report must not overwrite ROOT chapter evidence')
    require(report_path not in {p.resolve() for p in args.package}, 'Report must not overwrite a sealed package')
    try:
        report = run(args.package, args.chapter_dir, args.master)
    except Exception as error:
        report = {'status': 'failed', 'error': str(error), 'scope': 'import compatibility only; no approval'}
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(canonical(report) + b'\n')
    print(json.dumps({'status': report['status'], 'report': str(report_path)}))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
