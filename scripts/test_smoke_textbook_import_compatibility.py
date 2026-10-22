from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/smoke_textbook_import_compatibility.py'


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


class ImportCompatibilitySmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='compatibility-smoke-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.protected = self.root / 'user-data'
        self.protected.mkdir()
        (self.protected / 'aptitude.db').write_bytes(b'do not open or modify this user database')

    def fixture(self, name='Example bank', book_title=None):
        chapter = self.root / name.replace(' ', '-')
        chapter.mkdir()
        image = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXioAAAAASUVORK5CYII=')
        digest = hashlib.sha256(image).hexdigest()
        question = {'key': 'ch401-q0001', 'category': name, 'chapter': '401',
                    'difficulty': 'Medium', 'question_text': 'Choose the pictured answer.',
                    'options': {'A': 'One', 'B': 'Two', 'C': 'Three', 'D': 'Four', 'E': 'Five'},
                    'correct_answer': 'E', 'solution_steps': ['The key marks E.'],
                    'display_media': {'question': {'asset': 'assets/figure.png', 'sha256': digest,
                                                 'alt_text': 'Full question and diagram'}}}
        manifest = {'format_version': 3, 'bank_name': name, 'question_files': ['questions.jsonl'],
                    'audit_summary_file': 'metadata/audit.json', 'lineage_file': 'metadata/lineage.json'}
        if book_title is not None:
            manifest['book_title'] = book_title
        audit_hash = hashlib.sha256(b'fixture-ledger').hexdigest()
        audit = {'audit_sha256': audit_hash, 'approved_count': 1, 'reviewed_rejection_count': 0}
        lineage = {'records': [{'key': question['key'], 'candidate_sha256': 'a' * 64}]}
        package = chapter / 'candidate.zip'
        with zipfile.ZipFile(package, 'w') as z:
            for path, value in {'manifest.json': encoded(manifest), 'questions.jsonl': encoded(question),
                                'metadata/audit.json': encoded(audit), 'metadata/lineage.json': encoded(lineage),
                                'assets/figure.png': image}.items():
                z.writestr(path, value)
        sha = hashlib.sha256(package.read_bytes()).hexdigest()
        (chapter / 'candidate.package.json').write_bytes(encoded({'candidate_sha256': sha, 'audit_sha256': audit_hash}))
        (chapter / 'package-result.json').write_bytes(encoded({'path': str(package), 'sha256': sha,
            'audit_sha256': audit_hash, 'question_count': 1, 'rejected_count': 0}))
        reference = {**question, 'category': 'Quantitative Aptitude', 'question_html': '', 'explanation': '',
                     'display_media': {'question': {'sha256': digest, 'alt_text': 'Full question and diagram',
                                                  'source_path': 'expired-temporary-path.png'}}}
        evidence = {'source_key': question['key'], 'record': reference,
                    'imported_record_sha256': hashlib.sha256(encoded(reference)).hexdigest()}
        evidence_path = chapter / 'import-evidence.json'
        evidence_path.write_bytes(encoded(evidence))
        (chapter / 'state').mkdir()
        (chapter / 'state/renders.json').write_bytes(encoded({'records': [{'question_number': 1, 'artifacts': {
            'source_key': question['key'], 'candidate_sha256': 'a' * 64,
            'import_evidence_path': str(evidence_path),
            'import_evidence_sha256': hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
            'imported_record_sha256': evidence['imported_record_sha256']}}]}))
        return package, chapter

    def run_smoke(self, pairs, *extra):
        self.assertTrue(SCRIPT.is_file(), 'Reusable compatibility smoke script is missing')
        report_path = self.root / 'report.json'
        command = [sys.executable, '-B', str(SCRIPT), '--report', str(report_path)]
        for package, chapter in pairs:
            command += ['--package', str(package), '--chapter-dir', str(chapter)]
        command += list(extra)
        result = subprocess.run(command, cwd=ROOT, env={**os.environ, 'KSAT_DATA_DIR': str(self.protected)},
                                capture_output=True, text=True, encoding='utf-8', timeout=90)
        self.assertTrue(report_path.is_file(), result.stderr)
        self.assertEqual((self.protected / 'aptitude.db').read_bytes(), b'do not open or modify this user database')
        self.assertEqual(sorted(p.name for p in self.protected.iterdir()), ['aptitude.db'])
        return result, json.loads(report_path.read_text(encoding='utf-8'))

    def test_real_import_preserves_records_media_and_metadata_in_disposable_data(self):
        result, report = self.run_smoke([self.fixture(book_title=' Example Book ')])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['application']['version'], '2.2.2')
        self.assertTrue(report['temporary_data_removed'])
        bank = report['phases'][0]['banks'][0]
        self.assertEqual(bank['question_count'], 1)
        self.assertEqual(bank['media_reference_count'], 1)
        self.assertEqual(bank['unique_media_assets'], 1)
        self.assertEqual(bank['book_title'], 'Example Book')
        self.assertEqual(bank['sealed_record_matches'], 1)
        self.assertEqual(bank['root_content_media_matches'], 1)
        self.assertEqual(bank['root_exact_matches'], 0)
        self.assertEqual(bank['records'][0]['root_differing_fields'], ['category'])

    def test_bad_seal_fails_without_importing_or_touching_user_database(self):
        pair = self.fixture()
        (pair[1] / 'candidate.package.json').write_bytes(encoded({'candidate_sha256': '0' * 64}))
        result, report = self.run_smoke([pair])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report['status'], 'failed')
        self.assertIn('seal', report['error'].lower())

    def test_tampered_root_reference_is_rejected_not_reported_as_matching(self):
        pair = self.fixture()
        (pair[1] / 'import-evidence.json').write_bytes(b'{}')
        result, report = self.run_smoke([pair])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('reference', report['error'].lower())

    def test_master_uses_fresh_database_and_matches_sealed_chapter_bytes(self):
        pairs = [self.fixture('First'), self.fixture('Second')]
        master = self.root / 'master.zip'
        with zipfile.ZipFile(master, 'w') as z:
            for index, (package, _) in enumerate(pairs):
                z.writestr(f'chapters/{index}.zip', package.read_bytes())
        result, report = self.run_smoke(pairs, '--master', str(master))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual([p['kind'] for p in report['phases']], ['chapters', 'master'])
        self.assertEqual([len(p['banks']) for p in report['phases']], [2, 2])
        self.assertTrue(report['temporary_data_removed'])

    def test_hash_consistent_root_content_mismatch_fails(self):
        pair = self.fixture()
        evidence_path = pair[1] / 'import-evidence.json'
        evidence = json.loads(evidence_path.read_bytes())
        evidence['record']['question_text'] = 'A different ROOT question.'
        evidence['imported_record_sha256'] = hashlib.sha256(encoded(evidence['record'])).hexdigest()
        evidence_path.write_bytes(encoded(evidence))
        state_path = pair[1] / 'state/renders.json'
        state = json.loads(state_path.read_bytes())
        artifacts = state['records'][0]['artifacts']
        artifacts['import_evidence_sha256'] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        artifacts['imported_record_sha256'] = evidence['imported_record_sha256']
        state_path.write_bytes(encoded(state))

        result, report = self.run_smoke([pair])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report['status'], 'failed')
        self.assertIn('ROOT render-data mismatch', report['error'])
        self.assertIn('question_text', report['error'])


if __name__ == '__main__':
    unittest.main()
