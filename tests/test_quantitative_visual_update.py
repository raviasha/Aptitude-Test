"""Verify exact chapter embedding and the reviewed chart edit inventory."""
import hashlib
import copy
import json
from pathlib import Path
import zipfile

import pytest

from scripts.build_quantitative_visual_update import write_master, read_package
from scripts import build_quantitative_visual_update as assembly
from scripts.prepare_quantitative_chart_text_review import records, verify


def test_master_embeds_exact_chapter_bytes_with_hash_inventory(tmp_path):
    first, second = tmp_path/'first.zip', tmp_path/'second.zip'
    first.write_bytes(b'first exact chapter bytes')
    second.write_bytes(b'second exact chapter bytes')
    target = tmp_path/'master.zip'
    report = write_master(target, [first, second], 'Instructions')
    with zipfile.ZipFile(target) as archive:
        assert archive.read('chapters/first.zip') == first.read_bytes()
        assert archive.read('chapters/second.zip') == second.read_bytes()
        assert archive.read('README.txt') == b'Instructions'
        assert json.loads(archive.read('SHA256SUMS.json')) == {
            'chapters/first.zip': hashlib.sha256(first.read_bytes()).hexdigest(),
            'chapters/second.zip': hashlib.sha256(second.read_bytes()).hexdigest(),
        }
    assert report['chapters'] == 2
    assert report['sha256'] == hashlib.sha256(target.read_bytes()).hexdigest()
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        write_master(target, [first], 'Changed')
    assert target.read_bytes() == before


def test_all_reviewed_chart_stems_and_required_notes_survive():
    verify(records())


def test_read_package_keeps_stable_keys_and_rejects_duplicates(tmp_path):
    source = tmp_path/'chapter.zip'
    manifest = {'bank_name': 'Example', 'question_files': ['questions.jsonl']}
    row = {'key': 'ch24-q1', 'question_text': 'Prompt', 'correct_answer': 'B'}
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('manifest.json', json.dumps(manifest))
        archive.writestr('questions.jsonl', json.dumps(row) + '\n')
    assert read_package(source) == (manifest, {row['key']: row})
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('manifest.json', json.dumps(manifest))
        archive.writestr('questions.jsonl', (json.dumps(row) + '\n') * 2)
    with pytest.raises(ValueError, match='Duplicate source key'):
        read_package(source)


@pytest.mark.parametrize('relative', ['latest/quantitative/new-build', 'latest/another-book', 'latest'])
def test_assembly_rejects_protected_outputs_before_creating_anything(tmp_path, relative):
    baseline = tmp_path/'latest/quantitative'
    baseline.mkdir(parents=True)
    target = tmp_path/relative
    before = set(tmp_path.rglob('*'))
    with pytest.raises(ValueError, match='protected'):
        assembly.validate_output_dir(target, baseline)
    assert set(tmp_path.rglob('*')) == before
    assembly.validate_output_dir(tmp_path/'staged', baseline)
    assert not (tmp_path/'staged').exists()


def test_assembly_rejects_changed_chart_corpus_even_with_unchanged_stems(monkeypatch):
    original = records()
    changed = copy.deepcopy(original)
    changed[0]['display_media']['question']['sha256'] = '0' * 64
    monkeypatch.setattr(assembly, 'chart_source_records', lambda: changed)
    with pytest.raises(AssertionError, match='Reviewed package records changed'):
        assembly.validate_chart_review([])


def test_assembly_rejects_review_not_matching_explicit_reviewed_selections():
    with pytest.raises(ValueError, match='reviewed selections'):
        assembly.validate_chart_review([])
