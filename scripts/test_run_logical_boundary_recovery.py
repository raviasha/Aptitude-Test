"""Real-file recovery tests; only the external Codex process is substituted."""
import copy
import contextlib
import importlib
import io
import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from PIL import Image
from jsonschema import Draft202012Validator


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(Path('scripts/run_logical_boundary_recovery.py').exists(),
                        'recovery runner has not been implemented')
        self.r = importlib.import_module('scripts.run_logical_boundary_recovery')
        # No test may contact a live backend, even if a new test forgets its mock.
        guard = patch.object(self.r.subprocess, 'run', side_effect=AssertionError(
            'Live subprocess forbidden in recovery unit tests'))
        guard.start()
        self.addCleanup(guard.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pages = self.root / 'pages'
        self.reviews = self.root / 'reviews'
        self.pages.mkdir()
        self.reviews.mkdir()
        self.source = self.pages / 'page-0029.png'
        Image.new('RGB', (320, 1801), 'white').save(self.source)
        self.proposal = self.reviews / 'old.json'
        self.proposal.write_text(json.dumps({'page': 29, 'exercise_id': 'Exercise 1',
            'question_blocks': [{'printed_number': 1, 'top': 5, 'bottom': 9}]}))

    def job(self, **kwargs):
        return self.r.prepare_job(self.pages, self.reviews, self.root / 'out', 29, **kwargs)

    def result(self, job):
        return {'job_id': job['job_id'], 'job_fingerprint': job['job_fingerprint'],
                'verdict': 'approved', 'notes': 'Visually inspected all rows.',
                'pages': [{'page': 29, 'coverage_complete': True,
                    'observed_counts': {'question_blocks': 1, 'answer_solution_blocks': 0,
                                        'shared_context_blocks': 0},
                    'exercises': [{'exercise_id': 'Exercise 1',
                        'question_blocks': [{'printed_number': 1, 'regions': [self.region()],
                                             'continuation': False, 'start_question': True}],
                        'answer_solution_blocks': [], 'shared_context_blocks': []}]}]}

    def region(self, left=10, top=100, right=300, bottom=160):
        return dict(left=left, top=top, right=right, bottom=bottom)

    def store_fixture_result(self, value, job):
        """Supply explicit real-file transport evidence for cache unit fixtures."""
        attempt = Path(job['directory']) / ('attempt-' + uuid.uuid4().hex)
        attempt.mkdir()
        (attempt / 'prompt.txt').write_bytes(Path(job['directory'], 'prompt.txt').read_bytes())
        (attempt / 'response.json').write_text(json.dumps(value), encoding='utf-8')
        (attempt / 'stdout.log').write_text('{"type":"turn.completed"}', encoding='utf-8')
        (attempt / 'stderr.log').write_text('', encoding='utf-8')
        (attempt / 'process.json').write_text('{"returncode":0}', encoding='utf-8')
        (attempt / 'command.json').write_text('["fixture"]', encoding='utf-8')
        self.r.store_result(value, job, attempt=attempt)

    def test_validation_retry_preserves_attempts_and_binds_success_evidence(self):
        job = self.job()
        seen = []
        def external(command, **kwargs):
            attempt = Path(command[command.index('-o') + 1]).parent
            seen.append((attempt, kwargs['input'], command))
            value = self.result(job)
            if len(seen) == 1:
                value['pages'][0]['exercises'][0]['question_blocks'][0]['regions'][0]['bottom'] = 100
            (attempt / 'response.json').write_text(json.dumps(value))
            return SimpleNamespace(returncode=0, stdout='{"type":"turn.completed"}\n', stderr='worker diagnostic')
        with patch.object(self.r.subprocess, 'run', side_effect=external) as process:
            self.assertEqual(self.r.run_one(job, attempts=3), 'approved')
            self.assertEqual(self.r.run_one(job, attempts=3), 'cached-approved')
            self.assertEqual(process.call_count, 2)
        first, second = seen
        self.assertEqual(first[1].encode(), Path(job['directory'], 'prompt.txt').read_bytes())
        failure = json.loads((first[0] / 'failure.json').read_bytes())
        self.assertIn(failure['error'], second[1])
        self.assertIn('"bottom": 100', second[1])
        self.assertEqual((second[0] / 'prompt.txt').read_bytes(), second[1].encode())
        self.assertEqual(first[2][-8:], second[2][-8:])  # Same four bound images.
        receipt = json.loads(Path(job['directory'], 'receipt.json').read_bytes())
        evidence = receipt['successful_attempt']
        self.assertEqual(evidence['attempt_id'], second[0].name)
        for field, filename in [('prompt_sha256', 'prompt.txt'), ('response_sha256', 'response.json'),
                                ('stdout_sha256', 'stdout.log'), ('stderr_sha256', 'stderr.log')]:
            path = second[0] / filename
            original = path.read_bytes()
            self.assertEqual(evidence[field], hashlib.sha256(original).hexdigest())
            path.write_bytes(original + b' ')
            self.assertFalse(self.r.valid_cache(job), filename)
            path.write_bytes(original)
        self.assertTrue(self.r.valid_cache(job))

    def test_validation_retry_exhausts_exact_budget(self):
        job = self.job()
        def external(command, **kwargs):
            value = self.result(job)
            value['pages'][0]['coverage_complete'] = False
            Path(command[command.index('-o') + 1]).write_text(json.dumps(value))
            return SimpleNamespace(returncode=0, stdout='{"type":"turn.completed"}', stderr='')
        with patch.object(self.r.subprocess, 'run', side_effect=external) as process:
            with self.assertRaisesRegex(ValueError, 'Incomplete'):
                self.r.run_one(job, attempts=2)
        self.assertEqual(process.call_count, 2)
        directory = Path(job['directory'])
        self.assertEqual(len(list(directory.glob('attempt-*/failure.json'))), 2)
        self.assertEqual(len(list(directory.glob('attempt-*/prompt.txt'))), 2)
        self.assertFalse((directory / 'receipt.json').exists())

    def failed_attempt(self, job, note):
        seen = []
        def external(command, **kwargs):
            path = Path(command[command.index('-o') + 1])
            value = self.result(job)
            value['notes'] = note
            value['pages'][0]['coverage_complete'] = False
            path.write_text(json.dumps(value))
            seen.append(path.parent)
            return SimpleNamespace(returncode=0, stdout='{"type":"turn.completed"}', stderr='')
        with patch.object(self.r.subprocess, 'run', side_effect=external):
            with self.assertRaises(self.r.OutputValidationError):
                self.r.run_one(job, attempts=1)
        return seen[0]

    def test_cross_run_continues_latest_verified_failed_response(self):
        job = self.job()
        first = self.failed_attempt(job, 'FIRST prior response')
        second = self.failed_attempt(job, 'LATEST prior response')
        original_evidence = {p: p.read_bytes() for attempt in (first, second)
                             for p in attempt.iterdir() if p.is_file()}
        captured = []
        def external(command, **kwargs):
            captured.append(kwargs['input'])
            Path(command[command.index('-o') + 1]).write_text(json.dumps(self.result(job)))
            return SimpleNamespace(returncode=0, stdout='{"type":"turn.completed"}', stderr='')
        with patch.object(self.r.subprocess, 'run', side_effect=external) as process:
            self.assertEqual(self.r.run_one(job, attempts=1), 'approved')
            self.assertEqual(self.r.run_one(job), 'cached-approved')
            self.assertEqual(process.call_count, 1)
        self.assertIn('LATEST prior response', captured[0])
        self.assertNotIn('FIRST prior response', captured[0])
        self.assertIn(json.loads((second / 'failure.json').read_bytes())['error'], captured[0])
        for path, original in original_evidence.items():
            self.assertEqual(path.read_bytes(), original)

    def test_cross_run_ignores_tampered_nonretryable_and_partial_evidence(self):
        for filename in ('prompt.txt', 'response.json', 'failure.json', 'stdout.log',
                         'stderr.log', 'process.json', 'command.json', 'retry-evidence.json',
                         'retry-completed.json', 'unsealed', 'partial', 'nonretryable'):
            with self.subTest(filename=filename):
                job = self.job(model=filename)
                failed = self.failed_attempt(job, 'DO NOT REUSE THIS RESPONSE')
                if filename == 'partial':
                    (failed / 'response.json').unlink()
                elif filename == 'unsealed':
                    (failed / 'retry-completed.json').unlink()
                elif filename == 'nonretryable':
                    failure = json.loads((failed / 'failure.json').read_bytes())
                    failure.update(retryable=False, error_type='ValueError')
                    (failed / 'failure.json').write_text(json.dumps(failure))
                else:
                    path = failed / filename
                    path.write_bytes(path.read_bytes() + b' ')
                prompts = []
                def external(command, **kwargs):
                    prompts.append(kwargs['input'])
                    Path(command[command.index('-o') + 1]).write_text(json.dumps(self.result(job)))
                    return SimpleNamespace(returncode=0, stdout='{"type":"turn.completed"}', stderr='')
                with patch.object(self.r.subprocess, 'run', side_effect=external):
                    self.assertEqual(self.r.run_one(job, attempts=1), 'approved')
                self.assertEqual(prompts, [Path(job['directory'], 'prompt.txt').read_bytes().decode()])

    def test_cross_run_does_not_search_other_fingerprint_directories(self):
        old_job = self.job(model='old-fingerprint')
        self.failed_attempt(old_job, 'OLD FINGERPRINT RESPONSE')
        job = self.job()
        prompts = []
        def external(command, **kwargs):
            prompts.append(kwargs['input'])
            Path(command[command.index('-o') + 1]).write_text(json.dumps(self.result(job)))
            return SimpleNamespace(returncode=0, stdout='{"type":"turn.completed"}', stderr='')
        with patch.object(self.r.subprocess, 'run', side_effect=external):
            self.r.run_one(job, attempts=1)
        self.assertEqual(prompts, [Path(job['directory'], 'prompt.txt').read_bytes().decode()])

    def test_retry_never_repeats_transport_tool_evidence_or_internal_failures(self):
        for mode in ('nonzero', 'timeout', 'tool', 'turn_failed', 'error_event', 'missing', 'json', 'schema', 'mutation', 'internal'):
            with self.subTest(mode=mode):
                job = self.job(model=mode)
                original = self.proposal.read_bytes()
                def external(command, **kwargs):
                    if mode == 'timeout':
                        raise self.r.subprocess.TimeoutExpired(command, 1, output=b'partial', stderr=b'partial-error')
                    if mode == 'internal':
                        raise RuntimeError('internal error')
                    path = Path(command[command.index('-o') + 1])
                    if mode != 'missing':
                        path.write_text('bad json' if mode == 'json' else
                                        '{}' if mode == 'schema' else json.dumps(self.result(job)))
                    if mode == 'mutation':
                        self.proposal.write_bytes(original + b' ')
                    stdout = ('{"type":"item.completed","item":{"type":"command_execution"}}'
                              if mode == 'tool' else '{"type":"turn.completed"}')
                    if mode in ('turn_failed', 'error_event'):
                        stdout = json.dumps({'type': 'turn.failed' if mode == 'turn_failed' else 'error'})
                    return SimpleNamespace(returncode=1 if mode == 'nonzero' else 0, stdout=stdout, stderr='')
                with patch.object(self.r.subprocess, 'run', side_effect=external) as process:
                    with self.assertRaises(Exception):
                        self.r.run_one(job, attempts=3)
                self.assertEqual(process.call_count, 1)
                self.assertFalse(Path(job['directory'], 'receipt.json').exists())
                self.assertEqual(len(list(Path(job['directory']).glob('attempt-*/failure.json'))), 1)
                self.proposal.write_bytes(original)

    def test_internal_validator_error_is_not_a_model_output_retry(self):
        job = self.job()
        def external(command, **kwargs):
            Path(command[command.index('-o') + 1]).write_text(json.dumps(self.result(job)))
            return SimpleNamespace(returncode=0, stdout='{"type":"turn.completed"}', stderr='')
        with patch.object(self.r.subprocess, 'run', side_effect=external) as process, \
                patch.object(self.r, 'validate_result', side_effect=ValueError('internal validator defect')):
            with self.assertRaisesRegex(ValueError, 'internal validator defect'):
                self.r.run_one(job)
        self.assertEqual(process.call_count, 1)
        self.assertFalse(Path(job['directory'], 'receipt.json').exists())

    def test_cli_attempts_positive_and_passed_to_retry_budget(self):
        args = ['--pages-root', str(self.pages), '--reviews-root', str(self.reviews),
                '--output-root', str(self.root / 'out'), '--pages', '29', '29']
        for count in ('0', '-1'):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                self.r.main(args + ['--attempts', count])
            self.assertEqual(error.exception.code, 2)
        def external(command, **kwargs):
            directory = Path(command[command.index('-o') + 1]).parent.parent
            job = json.loads((directory / 'job.json').read_bytes())
            value = self.result(job)
            value['pages'][0]['coverage_complete'] = False
            Path(command[command.index('-o') + 1]).write_text(json.dumps(value))
            return SimpleNamespace(returncode=0, stdout='{"type":"turn.completed"}', stderr='')
        for extra, wanted in (([], 3), (['--attempts', '2'], 2)):
            with contextlib.redirect_stdout(io.StringIO()), patch.object(self.r.subprocess, 'run', side_effect=external) as process:
                self.assertEqual(self.r.main(args + extra), 1)
            self.assertEqual(process.call_count, wanted)

    def test_tiles_cover_original_with_overlap_and_global_grid(self):
        job = self.job()
        self.assertEqual([(x['top'], x['bottom']) for x in job['images'][1:]],
                         [(0, 900), (800, 1700), (1600, 1801)])
        self.assertEqual(self.source.read_bytes(), Path(job['images'][0]['path']).read_bytes())
        for tile in job['images'][1:]:
            with Image.open(tile['path']) as image:
                self.assertEqual(image.size, (320, tile['bottom'] - tile['top']))
                self.assertNotEqual(image.getpixel((200, 100)), (255, 255, 255))

    def test_schema_replaces_vertical_crops_with_nonempty_regions(self):
        schema = json.loads(self.r.DEFAULT_SCHEMA.read_text(encoding='utf-8'))
        validator = Draft202012Validator(schema)
        job = self.job()
        self.assertEqual(list(validator.iter_errors(self.result(job))), [])
        legacy = self.result(job)
        block = legacy['pages'][0]['exercises'][0]['question_blocks'][0]
        block.pop('regions')
        block.update(top=100, bottom=160)
        self.assertTrue(list(validator.iter_errors(legacy)))
        empty = self.result(job)
        empty['pages'][0]['exercises'][0]['question_blocks'][0]['regions'] = []
        self.assertTrue(list(validator.iter_errors(empty)))

    def test_fingerprint_changes_for_each_bound_input(self):
        first = self.job()
        self.assertEqual(first['job_fingerprint'], self.job()['job_fingerprint'])
        self.assertNotEqual(first['job_fingerprint'], self.job(model='different')['job_fingerprint'])
        self.proposal.write_text(self.proposal.read_text() + ' ')
        second = self.job()
        self.assertNotEqual(first['job_fingerprint'], second['job_fingerprint'])
        Image.new('RGB', (320, 1801), 'black').save(self.source)
        self.assertNotEqual(second['job_fingerprint'], self.job()['job_fingerprint'])
        schema = self.root / 'schema.json'
        schema.write_bytes(self.r.DEFAULT_SCHEMA.read_bytes() + b'\n')
        self.assertNotEqual(self.job()['job_fingerprint'], self.job(schema=schema)['job_fingerprint'])

    def test_invalid_or_missing_evidence_cannot_approve(self):
        job = self.job()
        good = self.result(job)
        self.r.validate_result(good, job)
        cases = []
        for top, bottom in [(10, 10), (-1, 5), (0, 1802), (True, 100)]:
            bad = copy.deepcopy(good)
            bad['pages'][0]['exercises'][0]['question_blocks'][0]['regions'][0].update(top=top, bottom=bottom)
            cases.append(bad)
        bad = copy.deepcopy(good)
        bad['pages'][0]['exercises'][0]['question_blocks'] *= 2
        cases.append(bad)
        for field, value in [('exercises', []), ('coverage_complete', False), ('page', 30)]:
            bad = copy.deepcopy(good)
            bad['pages'][0][field] = value
            cases.append(bad)
        bad = copy.deepcopy(good)
        bad['pages'][0]['exercises'][0]['question_blocks'][0]['printed_number'] = 2
        cases.append(bad)
        for bad in cases:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.r.validate_result(bad, job)
        blocked = copy.deepcopy(good)
        blocked.update(verdict='blocked', notes='Cannot identify the continued exercise.')
        blocked['pages'][0].update(coverage_complete=False, exercises=[],
            observed_counts=dict.fromkeys(self.r.ROLES, 0))
        self.r.validate_result(blocked, job)

    def test_cache_requires_result_digest_and_all_bound_files(self):
        job = self.job()
        self.store_fixture_result(self.result(job), job)
        self.assertTrue(self.r.valid_cache(job))
        path = Path(job['directory']) / 'result.json'
        path.write_text(path.read_text() + ' ')
        self.assertFalse(self.r.valid_cache(job))
        self.store_fixture_result(self.result(job), job)
        Path(job['images'][1]['path']).write_bytes(b'corrupt')
        self.assertFalse(self.r.valid_cache(job))

    def test_input_mutation_between_prepare_and_run_fails_closed(self):
        job = self.job()
        self.proposal.write_text('{}')
        with self.assertRaises(ValueError):
            self.r.run_one(job, timeout=1)

    def test_subprocess_result_then_resume_without_second_call(self):
        job = self.job()
        def external(command, **kwargs):
            self.assertEqual(command[0:2], ['codex', 'exec'])
            self.assertEqual(command.count('-i'), 4)
            self.assertIn('read-only', command)
            self.assertIn('--json', command)
            Path(command[command.index('-o') + 1]).write_text(json.dumps(self.result(job)))
            return SimpleNamespace(returncode=0, stdout='{"type":"turn.completed"}\n', stderr='')
        with patch.object(self.r.subprocess, 'run', side_effect=external) as process:
            self.assertEqual(self.r.run_one(job, timeout=1), 'approved')
            self.assertEqual(self.r.run_one(job, timeout=1), 'cached-approved')
            self.assertEqual(process.call_count, 1)

    def test_missing_ambiguous_pages_and_overlapping_output_are_rejected(self):
        with self.assertRaises(ValueError):
            self.r.prepare_job(self.pages, self.reviews, self.pages / 'out', 29)
        with self.assertRaises(ValueError):
            self.r.prepare_job(self.pages, self.reviews, self.root / 'out', 30)
        Image.new('RGB', (320, 1801)).save(self.pages / 'page-29-180dpi.png')
        with self.assertRaises(ValueError):
            self.job()

    def test_renderer_filename_uses_page_number_not_dpi(self):
        self.source.rename(self.pages / 'page-029-180dpi.png')
        Image.new('RGB', (320, 1801)).save(self.pages / 'grid-029.png')
        self.assertEqual(self.job()['binding']['page'], 29)

    def test_normalized_exercise_ids_and_shared_answer_row(self):
        proposal = json.loads(self.proposal.read_text())
        proposal['exercise_id'] = 'Exercise1A'
        self.proposal.write_text(json.dumps(proposal))
        job = self.job()
        value = self.result(job)
        exercise = value['pages'][0]['exercises'][0]
        exercise['exercise_id'] = '1a'
        exercise['answer_solution_blocks'] = [
            {'printed_number': n, 'regions': [self.region(top=500, bottom=550)],
             'continuation': False, 'start_question': False} for n in (1, 2)]
        value['pages'][0]['observed_counts']['answer_solution_blocks'] = 2
        self.r.validate_result(value, job)
        exercise['answer_solution_blocks'][1]['printed_number'] = 1
        with self.assertRaises(ValueError):
            self.r.validate_result(value, job)

    def test_explicit_start_and_continuation_and_shared_targets(self):
        job = self.job()
        value = self.result(job)
        exercise = value['pages'][0]['exercises'][0]
        block = exercise['question_blocks'][0]
        block['start_question'] = True
        exercise['shared_context_blocks'] = [{'printed_number': None, 'regions': [self.region(top=30, bottom=70)],
            'continuation': False, 'start_question': False,
            'target_printed_number_start': 1, 'target_printed_number_end': 3}]
        value['pages'][0]['observed_counts']['shared_context_blocks'] = 1
        self.r.validate_result(value, job)
        block['continuation'] = True
        with self.assertRaises(ValueError):
            self.r.validate_result(value, job)
        block['start_question'] = False
        self.r.validate_result(value, job)
        exercise['shared_context_blocks'][0]['target_printed_number_end'] = 0
        with self.assertRaises(ValueError):
            self.r.validate_result(value, job)

    def test_cli_limit_and_failure_do_not_approve(self):
        with contextlib.redirect_stdout(io.StringIO()), patch.object(self.r.subprocess, 'run', return_value=SimpleNamespace(
                returncode=1, stdout='', stderr='failed')) as process:
            code = self.r.main(['--pages-root', str(self.pages), '--reviews-root', str(self.reviews),
                '--output-root', str(self.root / 'out'), '--pages', '29', '31', '--limit', '1'])
        self.assertEqual(code, 1)
        self.assertEqual(process.call_count, 1)
        self.assertEqual(list((self.root / 'out').rglob('receipt.json')), [])

    def test_original_page29_reproduces_question36_option_clipping(self):
        source = self.r.PROJECT_ROOT / 'tmp/logical-reasoning-v2/recovery/source-pages/page-029-180dpi.png'
        if not source.is_file():
            self.skipTest('Local source-page acceptance fixture is unavailable')
        # Coordinates inspected in the original raster, never PDF/text-layer geometry.
        with Image.open(source) as image:
            self.assertEqual(image.size, (1530, 2340))
            options = image.convert('L').crop((210, 1640, 1470, 1675))
            ink = options.point(lambda pixel: 255 if pixel < 100 else 0)
            self.assertEqual(ink.getbbox(), (94, 4, 1119, 33))
            self.assertIsNotNone(ink.crop((0, 11, 1260, 35)).getbbox(),
                                 'Old bottom=1651 must demonstrably truncate printed options')

    def test_context_is_full_original_bound_and_target_tiles_only(self):
        for n in (28, 30):
            Image.new('RGB', (400, 1000), 'gray').save(self.pages / f'page-{n:03d}-180dpi.png')
        job = self.job()
        context = [x for x in job['images'] if x['role'] == 'context']
        self.assertEqual([x['page'] for x in context], [28, 30])
        self.assertEqual(len(job['images']), 6)
        for item in context:
            self.assertEqual(Path(item['path']).read_bytes(),
                (self.pages / f'page-{item["page"]:03d}-180dpi.png').read_bytes())
        self.assertEqual(len(self.job(context_radius=0)['images']), 4)
        prompt = Path(job['directory'], 'prompt.txt').read_text(encoding='utf-8')
        for instruction in ('exercise identity only', 'ONLY for the target page',
                            'exclude adjacent pixels', 'damaged numbers',
                            'Shared targets are visible-only',
                            'at least 3 clear white pixels'):
            self.assertIn(instruction, prompt)
        (self.pages / 'page-028-180dpi.png').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            self.r.verify_job(job)

    def test_inventory_add_remove_and_unrelated_changes_invalidate(self):
        extra = self.reviews / 'other.json'
        extra.write_text('{"page": 30}')
        job = self.job()
        for operation in ('change', 'remove', 'add'):
            with self.subTest(operation=operation):
                if operation == 'change':
                    extra.write_text('{"page": 31}')
                elif operation == 'remove':
                    extra.unlink()
                else:
                    extra.write_text('{"page": 30}')
                    (self.reviews / 'new.json').write_text('{"page": 99}')
                with self.assertRaises(ValueError):
                    self.r.verify_job(job)
        self.assertNotEqual(job['job_fingerprint'], self.job()['job_fingerprint'])

    def test_regions_required_nonempty_in_bounds_and_disjoint(self):
        job = self.job()
        good = self.result(job)
        block = good['pages'][0]['exercises'][0]['question_blocks'][0]
        for regions in ([], [self.region(left=-1)], [self.region(right=321)],
                        [self.region(right=10)], [self.region(), self.region(top=150)]):
            bad = copy.deepcopy(good)
            bad['pages'][0]['exercises'][0]['question_blocks'][0]['regions'] = regions
            with self.subTest(regions=regions), self.assertRaises(ValueError):
                self.r.validate_result(bad, job)
        second = copy.deepcopy(block)
        second.update(printed_number=2, regions=[self.region(top=150, bottom=200)])
        good['pages'][0]['exercises'][0]['question_blocks'].append(second)
        third = copy.deepcopy(block)
        third.update(printed_number=3, regions=[self.region(top=155, bottom=210)])
        good['pages'][0]['exercises'][0]['question_blocks'].append(third)
        good['pages'][0]['observed_counts']['question_blocks'] = 3
        with self.assertRaises(ValueError) as failure:
            self.r.validate_result(good, job)
        self.assertIn('printed_number=2', str(failure.exception))
        self.assertIn('printed_number=3', str(failure.exception))
        second['regions'] = [self.region(top=160, bottom=200)]
        third['regions'] = [self.region(top=200, bottom=240)]
        self.r.validate_result(good, job)  # Touching, not intersecting.

    def test_approved_ink_border_two_pixels_and_page_edge_exception(self):
        for xy in ((10, 120), (11, 120), (299, 120), (298, 120), (100, 100),
                   (100, 101), (100, 159), (100, 158)):
            with self.subTest(xy=xy):
                im = Image.new('RGB', (320, 1801), 'white')
                im.putpixel(xy, (0, 0, 0))
                im.save(self.source)
                job = self.job()
                value = self.result(job)
                with self.assertRaisesRegex(ValueError, 'border'):
                    self.r.validate_result(value, job)
                value['verdict'] = 'blocked'
                self.r.validate_result(value, job)
        im = Image.new('RGB', (320, 1801), 'white')
        im.putpixel((0, 120), (0, 0, 0))
        im.save(self.source)
        job = self.job()
        value = self.result(job)
        value['pages'][0]['exercises'][0]['question_blocks'][0]['regions'][0]['left'] = 0
        self.r.validate_result(value, job)

    def test_clipping_check_reads_immutable_target_original_asset(self):
        job = self.job()
        changed_source = Image.new('RGB', (320, 1801), 'white')
        changed_source.putpixel((10, 120), (0, 0, 0))
        changed_source.save(self.source)
        self.r.validate_result(self.result(job), job)

    def test_context_manifest_routes_deduplicated_originals_and_binds_changes(self):
        Image.new('RGB', (320, 900), 'gray').save(self.pages / 'page-028-180dpi.png')
        manifest = self.root / 'routing.json'
        manifest.write_text(json.dumps({'pages': {'29': {
            'context_pages': [28, 29], 'exercise_ids': ['Exercise 1B']}}}))
        job = self.job(context_radius=0, context_manifest=manifest)
        self.assertEqual([i['page'] for i in job['images'] if i['role'] == 'context'], [28])
        self.assertEqual([i['page'] for i in self.job(context_manifest=manifest)['images']
                          if i['role'] == 'context'], [28])
        self.store_fixture_result(self.result(job), job)
        self.assertTrue(self.r.valid_cache(job))
        manifest.write_text(manifest.read_text() + ' ')
        self.assertFalse(self.r.valid_cache(job))
        with self.assertRaises(ValueError):
            self.r.verify_job(job)
        self.assertNotEqual(job['job_fingerprint'],
                            self.job(context_radius=0, context_manifest=manifest)['job_fingerprint'])

    def test_context_manifest_rejects_malformed_and_missing_evidence(self):
        Image.new('RGB', (320, 900), 'gray').save(self.pages / 'page-028.png')
        manifest = self.root / 'routing.json'
        entry = {'context_pages': [28], 'exercise_ids': ['Exercise 1B']}
        cases = [[], {}, {'pages': []}, {'pages': {'029': entry}},
                 {'pages': {'0': entry}}, {'pages': {'99': entry}},
                 {'pages': {'29': dict(entry, extra=1)}}]
        for field, values in [('context_pages', [[], [True], [0], [28, 28], ['28'], [99], '28']),
                              ('exercise_ids', [[], [''], ['garbage'], [1], ['Exercise 0'],
                                                ['Exercise 1B', '1b'], 'Exercise 1B'])]:
            cases.extend({'pages': {'29': dict(entry, **{field: v})}} for v in values)
        for value in cases:
            manifest.write_text(json.dumps(value))
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.job(context_manifest=manifest)
        manifest.write_text('{"pages":{"29":{},"29":{}}}')
        with self.assertRaises(ValueError):
            self.job(context_manifest=manifest)
        manifest.write_text(json.dumps({'pages': {'29': entry}}))
        (self.pages / 'page-028.png').write_bytes(b'not a png')
        with self.assertRaises(ValueError):
            self.job(context_manifest=manifest)

    def test_cli_passes_context_manifest_into_real_job(self):
        Image.new('RGB', (320, 900), 'gray').save(self.pages / 'page-025.png')
        manifest = self.root / 'routing.json'
        manifest.write_text('{"pages":{"29":{"context_pages":[25],"exercise_ids":["Exercise 1B"]}}}')
        def external(command, **kwargs):
            attachments = [command[i + 1] for i, arg in enumerate(command) if arg == '-i']
            self.assertIn('context-page-0025.png', [Path(p).name for p in attachments])
            # Exercise identity routing is included in the prompt actually sent to the worker.
            self.assertIn('"exercise_ids": ["Exercise 1B"]', kwargs['input'])
            return SimpleNamespace(returncode=1, stdout='', stderr='mock failure')
        with contextlib.redirect_stdout(io.StringIO()), patch.object(self.r.subprocess, 'run', side_effect=external) as process:
            status = self.r.main(['--pages-root', str(self.pages), '--reviews-root', str(self.reviews),
                '--output-root', str(self.root / 'out'), '--pages', '29', '29',
                '--context-manifest', str(manifest)])
        self.assertEqual(status, 1)
        self.assertEqual(process.call_count, 1)

    def test_runner_bytes_and_policy_bind_jobs_and_receipts_without_real_source_mutation(self):
        real_source = Path(self.r.__file__)
        original = real_source.read_bytes()
        source_copy = self.root / 'runner-copy.py'
        source_copy.write_bytes(original)
        with patch.object(self.r, '__file__', str(source_copy)):
            job = self.job()
            expected_sha = hashlib.sha256(original).hexdigest()
            self.assertEqual(job['binding']['runner_sha256'], expected_sha)
            self.assertTrue(job['binding']['policy_version'])
            self.store_fixture_result(self.result(job), job)
            receipt_path = Path(job['directory']) / 'receipt.json'
            receipt = json.loads(receipt_path.read_bytes())
            self.assertEqual(receipt['runner_sha256'], expected_sha)
            self.assertEqual(receipt['policy_version'], job['binding']['policy_version'])
            self.assertTrue(self.r.valid_cache(job))
            receipt['runner_sha256'] = '0' * 64
            receipt_path.write_text(json.dumps(receipt))
            self.assertFalse(self.r.valid_cache(job))
            self.store_fixture_result(self.result(job), job)
            source_copy.write_bytes(original + b'\n# simulated policy change\n')
            with self.assertRaisesRegex(ValueError, 'runner|policy'):
                self.r.verify_job(job)
            self.assertFalse(self.r.valid_cache(job))
            with self.assertRaisesRegex(ValueError, 'runner|source|drift'):
                self.job()
            source_copy.write_bytes(original)
            with patch.object(self.r, 'POLICY_VERSION', 'changed-test-policy'):
                with self.assertRaises(ValueError):
                    self.r.verify_job(job)
                self.assertFalse(self.r.valid_cache(job))
                self.assertNotEqual(job['job_fingerprint'], self.job()['job_fingerprint'])
        self.assertEqual(real_source.read_bytes(), original)

    def test_all_manifest_pages_selects_only_numeric_keys_and_applies_limit(self):
        for n in (2, 10, 28, 31):
            Image.new('RGB', (320, 900), 'white').save(self.pages / f'page-{n:03d}.png')
        manifest = self.root / 'routing.json'
        entry = {'context_pages': [28], 'exercise_ids': ['Exercise 1B']}
        manifest.write_text(json.dumps({'pages': {'29': entry, '10': entry, '2': entry}}))
        for limit, wanted in ((None, [2, 10, 29]), (2, [2, 10])):
            seen = []
            def external(command, **kwargs):
                directory = Path(command[command.index('-o') + 1]).parent.parent
                job = json.loads((directory / 'job.json').read_bytes())
                seen.append(job['binding']['page'])
                return SimpleNamespace(returncode=1, stdout='', stderr='mock failure')
            args = ['--pages-root', str(self.pages), '--reviews-root', str(self.reviews),
                    '--output-root', str(self.root / 'out'), '--workers', '1',
                    '--context-manifest', str(manifest), '--all-manifest-pages']
            if limit is not None:
                args += ['--limit', str(limit)]
            with contextlib.redirect_stdout(io.StringIO()), patch.object(self.r.subprocess, 'run', side_effect=external):
                self.assertEqual(self.r.main(args), 1)
            self.assertEqual(seen, wanted)

    def test_page_source_inventory_binds_absent_neighbors_and_duplicate_candidates(self):
        job = self.job()
        for name in ('page-028.png', 'page-029-180dpi.png', 'page-030.png'):
            path = self.pages / name
            Image.new('RGB', (320, 900), 'white').save(path)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'inventory|source'):
                self.r.verify_job(job)
            self.assertFalse(self.r.valid_cache(job))
            path.unlink()
        Image.new('RGB', (320, 900), 'white').save(self.pages / 'page-099.png')
        self.r.verify_job(job)  # An unrelated page is outside this job's context.
        Image.new('RGB', (320, 900), 'white').save(self.pages / 'page-028.png')
        context_job = self.job()
        Image.new('RGB', (320, 900), 'white').save(self.pages / 'page-028-180dpi.png')
        with self.assertRaises(ValueError):
            self.r.verify_job(context_job)

    def test_validator_diagnostics_identify_regions_and_border_sides(self):
        image = Image.new('RGB', (320, 1801), 'white')
        image.putpixel((10, 120), (0, 0, 0))
        image.putpixel((100, 159), (0, 0, 0))
        image.putpixel((80, 300), (0, 0, 0))
        image.save(self.source)
        job = self.job()
        value = self.result(job)
        value['pages'][0]['exercises'][0]['question_blocks'][0]['regions'].append(
            self.region(left=50, top=300, right=150, bottom=350))
        with self.assertRaises(ValueError) as failure:
            self.r.validate_result(value, job)
        for detail in ('Exercise 1', 'question_blocks', 'printed_number=1', 'block=0',
                       'region=0', 'left', 'bottom', 'region=1', 'top'):
            self.assertIn(detail, str(failure.exception))
        value = self.result(job)
        value['pages'][0]['exercises'][0]['question_blocks'][0]['regions'][0]['right'] = 400
        with self.assertRaises(ValueError) as failure:
            self.r.validate_result(value, job)
        self.assertIn('region=0', str(failure.exception))
        self.assertIn('printed_number=1', str(failure.exception))

    def test_all_manifest_pages_requires_manifest_and_rejects_range_or_invalid_entries(self):
        base = ['--pages-root', str(self.pages), '--reviews-root', str(self.reviews),
                '--output-root', str(self.root / 'out')]
        manifest = self.root / 'routing.json'
        manifest.write_text('{"pages":{"29":{"context_pages":[999],"exercise_ids":["Exercise 1B"]}}}')
        cases = [['--all-manifest-pages'],
                 ['--all-manifest-pages', '--pages', '29', '29'],
                 ['--all-manifest-pages', '--context-manifest', str(manifest), '--limit', '1']]
        for args in cases:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                self.r.main(base + args)
            self.assertEqual(error.exception.code, 2)
        self.assertFalse((self.root / 'out').exists())

    def test_tool_events_and_missing_last_message_never_get_receipt(self):
        for event in ({'type': 'item.started', 'item': {'type': 'command_execution'}},
                      {'type': 'item.completed', 'item': {'type': 'mcp_tool_call'}},
                      {'type': 'response.function_call_arguments.done'},
                      {'type': 'item.completed', 'item': {'type': 'web_search'}},
                      {'type': 'unknown.event'}, None):
            job = self.job(model=str(event))
            def external(command, **kwargs):
                if event is not None:
                    Path(command[command.index('-o') + 1]).write_text(json.dumps(self.result(job)))
                events = ([event] if event else []) + [{'type': 'turn.completed'}]
                return SimpleNamespace(returncode=0, stdout='\n'.join(map(json.dumps, events)), stderr='')
            with patch.object(self.r.subprocess, 'run', side_effect=external):
                with self.subTest(event=event), self.assertRaises(ValueError):
                    self.r.run_one(job)
            self.assertFalse((Path(job['directory']) / 'receipt.json').exists())


if __name__ == '__main__':
    unittest.main()
