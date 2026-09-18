"""Synthetic files and real runner cache validation; no backend calls."""
import copy
import contextlib
import importlib
import io
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from scripts import run_logical_boundary_recovery as runner


class CompilerTests(unittest.TestCase):
    def setUp(self):
        self.compiler = importlib.import_module('scripts.compile_logical_recovery_results')
        guard = patch.object(runner.subprocess, 'run', side_effect=AssertionError('No live backend'))
        guard.start()
        self.addCleanup(guard.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pages = self.root / 'pages'
        self.reviews = self.root / 'reviews'
        self.results = self.root / 'results'
        self.output = self.root / 'compiled'
        self.pages.mkdir()
        self.reviews.mkdir()
        for page in (29, 30, 31):
            Image.new('RGB', (320, 800), 'white').save(self.pages / f'page-{page:04d}.png')
        self.plan = self.root / 'plan.json'

    def block(self, number=1, top=100, continuation=False, question=True):
        return dict(printed_number=number, continuation=continuation,
                    start_question=question and not continuation,
                    regions=[dict(left=10, top=top, right=300, bottom=top + 40)])

    def cache(self, page=29, exercise='Exercise 1A', continuation=False, blocked=False,
              context=False, model='test', count=1, blocked_number=True, context_targets=(1, 1)):
        job = runner.prepare_job(self.pages, self.reviews, self.results / 'nested', page, model=model)
        entry = dict(exercise_id=exercise,
                     question_blocks=[self.block(None if blocked and blocked_number else n,
                                                top=100 + 50 * (n - 1), continuation=continuation)
                                      for n in range(1, count + 1)],
                     answer_solution_blocks=[self.block(n, top=300 + 50 * (n - 1),
                                                        continuation=continuation, question=False)
                                             for n in range(1, count + 1)],
                     shared_context_blocks=[])
        if context:
            shared = self.block(None, top=500, question=False)
            shared.update(target_printed_number_start=context_targets[0], target_printed_number_end=context_targets[1])
            entry['shared_context_blocks'].append(shared)
        value = dict(job_id=job['job_id'], job_fingerprint=job['job_fingerprint'],
                     verdict='blocked' if blocked else 'approved', notes='Synthetic reviewed evidence',
                     pages=[dict(page=page, coverage_complete=True, exercises=[entry],
                                 observed_counts={role: len(entry[role]) for role in runner.ROLES})])
        attempt = Path(job['directory']) / ('attempt-' + 'a' * 32)
        attempt.mkdir()
        (attempt / 'prompt.txt').write_bytes((Path(job['directory']) / 'prompt.txt').read_bytes())
        (attempt / 'response.json').write_bytes(runner.encoded(value))
        (attempt / 'stdout.log').write_text('{"type":"turn.completed"}\n', encoding='utf-8')
        (attempt / 'stderr.log').write_text('', encoding='utf-8')
        (attempt / 'process.json').write_text('{"returncode": 0}', encoding='utf-8')
        (attempt / 'command.json').write_text('["synthetic-test-only"]', encoding='utf-8')
        runner.store_result(value, job, attempt=attempt)
        self.assertTrue(runner.valid_cache(job))
        return job

    def exercise(self, name='1A', start=1, pages=None):
        return dict(exercise_id=name, internal_number_start=start,
                    expected_printed_numbers=[1], source_order=pages or [29])

    def clearance(self, job):
        result = json.loads((Path(job['directory']) / 'result.json').read_bytes())
        original = next(image for image in job['images'] if image['role'] == 'target-original')
        remaining = []
        for page in result['pages']:
            for exercise in page['exercises']:
                for role in runner.ROLES:
                    for index, block in enumerate(exercise[role]):
                        fields = ('target_printed_number_start', 'target_printed_number_end') if role == 'shared_context_blocks' else ('printed_number',)
                        for field in fields:
                            if block[field] is None:
                                remaining.append(dict(page=page['page'], exercise_id=exercise['exercise_id'],
                                                      role=role, block_index=index, field=field))
        return dict(result_sha256=hashlib.sha256(json.dumps(result, sort_keys=True,
                    separators=(',', ':'), ensure_ascii=False).encode()).hexdigest(),
                    reviewer='Pixel reviewer', reason='All non-number blockers reviewed against source',
                    cleared_checks=['geometry', 'coverage', 'exercise_identity', 'content_completeness', 'other_notes'],
                    other_blockers=[], remaining_nulls=remaining,
                    source_evidence={'29': dict(sha256=runner.digest(Path(original['path']).read_bytes()),
                        width=320, height=800, review_reference='Synthetic pixel review 29')})

    def compile(self, exercises=None, force=False):
        self.plan.write_text(json.dumps(dict(chapter_id=101, exercises=exercises or [self.exercise()])), encoding='utf-8')
        self.compiler.compile_results(self.plan, self.results, self.output, force=force)

    def context_plan(self, job, start=1, end=3, role='question'):
        exercise = dict(self.exercise(), expected_printed_numbers=[1, 2, 3])
        exercise['context_adjudications'] = [dict(page=29, exercise_id='1a', block_index=0,
            target_printed_number_start=start, target_printed_number_end=end, role=role,
            reviewer='Context reviewer', reason='Reviewed printed directions against source')]
        exercise['cleared_blockers'] = {job['job_fingerprint']: self.clearance(job)}
        return exercise

    def test_context_decisions_preserve_whole_exercise_subgroup_roles_and_provenance(self):
        job = self.cache(context=True, blocked=True, blocked_number=False, count=3, context_targets=(None, None))
        for start, end, role in ((1, 3, 'question'), (2, 3, 'answer_key'), (2, 2, 'solution')):
            with self.subTest(start=start, end=end, role=role):
                exercise = self.context_plan(job, start, end, role)
                self.compile([exercise], force=self.output.exists())
                contexts = json.loads((self.output / 'shared-contexts.json').read_bytes())
                self.assertEqual(contexts, {role: {f'exercise-1a-{start}-{end}': {
                    'question_numbers': list(range(start, end + 1)),
                    'segments': [dict(page=29, left=10, top=500, right=300, bottom=540)]}}})
                manifest = json.loads((self.output / 'exercise-0001-1a.json').read_bytes())
                provenance = manifest['review_provenance'][0]
                decision = provenance['context_adjudications'][0]
                self.assertEqual(decision, dict(exercise['context_adjudications'][0], exercise_id='Exercise 1A',
                    original_target_printed_number_start=None, original_target_printed_number_end=None))
                self.assertEqual(provenance['geometry_evidence_mode'], 'bound_source_images')
                original = {p.name: p.read_bytes() for p in self.output.iterdir()}
                self.compile([exercise], force=True)
                self.assertEqual({p.name: p.read_bytes() for p in self.output.iterdir()}, original)

    def test_context_decisions_reject_missing_extra_and_invalid_fields(self):
        job = self.cache(context=True, blocked=True, blocked_number=False, count=3, context_targets=(None, None))
        exercise = self.context_plan(job)
        good = exercise['context_adjudications'][0]
        cases = [[{k: v for k, v in good.items() if k != field}] for field in good]
        cases += [[dict(good, extra='not allowed')], [good, dict(good, exercise_id='Exercise 1A')], {}]
        for key, values in {
            'page': [True, 30, '29'], 'exercise_id': ['1B', None], 'block_index': [True, -1, '0'],
            'target_printed_number_start': [True, 0, 4, '1'], 'target_printed_number_end': [0, 4, None],
            'role': ['shared_context_blocks', 'unknown'], 'reviewer': ['', None], 'reason': [' ', 123]
        }.items():
            cases.extend([[dict(good, **{key: value})] for value in values])
        for decisions in cases:
            with self.subTest(decisions=decisions):
                exercise['context_adjudications'] = decisions
                with self.assertRaisesRegex(ValueError, '[Cc]ontext adjudication'):
                    self.compile([exercise])
                self.assertFalse(self.output.exists())

    def test_context_decisions_require_complete_clearance_and_exact_block(self):
        job = self.cache(context=True, blocked=True, blocked_number=False, count=3, context_targets=(None, None))
        good = self.context_plan(job)
        cases = []
        missing = copy.deepcopy(good)
        missing['cleared_blockers'][job['job_fingerprint']]['remaining_nulls'].pop()
        cases.append(missing)
        extra = copy.deepcopy(good)
        extra['cleared_blockers'][job['job_fingerprint']]['remaining_nulls'].append(
            dict(page=29, exercise_id='1A', role='shared_context_blocks', block_index=0, field='printed_number'))
        cases.append(extra)
        unused = copy.deepcopy(good)
        unused['context_adjudications'][0]['block_index'] = 1
        cases.append(unused)
        missing_decision = copy.deepcopy(good)
        missing_decision['context_adjudications'] = []
        cases.append(missing_decision)
        gap = copy.deepcopy(good)
        gap['expected_printed_numbers'] = [1, 3]
        cases.append(gap)
        for exercise in cases:
            with self.subTest(exercise=exercise), self.assertRaises(ValueError):
                self.compile([exercise])
            self.assertFalse(self.output.exists())

    def test_context_decisions_cannot_alter_visible_endpoints(self):
        for targets in ((1, None), (None, 3), (1, 3)):
            with self.subTest(targets=targets):
                # Separate roots avoid duplicate current-page results between cases.
                self.results = self.root / ('results-' + str(targets))
                job = self.cache(context=True, blocked=True, blocked_number=False, count=3, context_targets=targets)
                with self.assertRaises(ValueError):
                    self.compile([self.context_plan(job, 2, 3)])
                self.assertFalse(self.output.exists())

    def test_valid_cache_yields_compatible_manifest_provenance_and_contexts(self):
        job = self.cache(context=True)
        self.compile()
        manifest = json.loads((self.output / 'exercise-0001-1a.json').read_text())
        self.assertEqual(manifest['artifact_type'], 'logical-reasoning-reviewed-exercise-markers')
        self.assertEqual(manifest['chapter_id'], 101)
        self.assertEqual(manifest['exercise_id'], 'Exercise 1A')
        self.assertEqual(manifest['internal_numbers'], [1])
        self.assertEqual(manifest['review_provenance'][0]['job_fingerprint'], job['job_fingerprint'])
        self.assertEqual(manifest['marker_overrides']['question']['1']['segments'],
                         [dict(page=29, left=10, top=100, right=300, bottom=140)])
        contexts = json.loads((self.output / 'shared-contexts.json').read_text())
        self.assertEqual(contexts['question']['exercise-1a-1-1']['question_numbers'], [1])
        self.assertEqual(contexts['question']['exercise-1a-1-1']['segments'][0]['top'], 500)
        from logical_reasoning_v2.chapter_config import build_chapter_config
        result = build_chapter_config([manifest], source_pdf='synthetic.pdf', source_pdf_sha256='a' * 64,
                                      shared_contexts=contexts)
        self.assertEqual(result['shared_contexts'], contexts)

    def test_response_failure_and_tampered_cache_are_not_results(self):
        job = self.cache()
        directory = Path(job['directory'])
        receipt = (directory / 'receipt.json').read_bytes()
        result = (directory / 'result.json').read_bytes()
        for mode in ('no-receipt', 'tampered-result', 'stale-policy'):
            with self.subTest(mode=mode):
                (directory / 'receipt.json').write_bytes(receipt)
                (directory / 'result.json').write_bytes(result)
                (directory / 'job.json').write_bytes(runner.encoded(job))
                if mode == 'no-receipt':
                    (directory / 'receipt.json').unlink()
                    (directory / 'response.json').write_bytes((directory / 'result.json').read_bytes())
                    (directory / 'failure.json').write_text('{}')
                elif mode == 'tampered-result':
                    with (directory / 'result.json').open('a') as stream:
                        stream.write(' ')
                else:
                    stale = copy.deepcopy(job)
                    stale['binding']['runner_sha256'] = '0' * 64
                    (directory / 'job.json').write_text(json.dumps(stale))
                with self.assertRaisesRegex(ValueError, 'valid.*29|29.*valid'):
                    self.compile()
                self.assertFalse(self.output.exists())

    def test_invalid_old_cache_does_not_hide_current_valid_result(self):
        old = self.cache(model='old')
        (Path(old['directory']) / 'receipt.json').unlink()
        current = self.cache(model='current')
        self.compile()
        manifest = json.loads((self.output / 'exercise-0001-1a.json').read_text())
        self.assertEqual(manifest['review_provenance'][0]['job_fingerprint'], current['job_fingerprint'])

    def test_duplicate_current_valid_page_results_are_rejected(self):
        self.cache(model='first')
        self.cache(model='second')
        with self.assertRaisesRegex(ValueError, '[Dd]uplicate.*29'):
            self.compile()
        self.assertFalse(self.output.exists())

    def test_explicit_nonphysical_page_order_and_exercise_order(self):
        self.cache(31)
        self.cache(29, continuation=True)
        self.cache(30, exercise='Exercise 1B', context=True)
        self.compile([self.exercise(pages=[31, 29]), self.exercise('1B', 2, [30])])
        first = json.loads((self.output / 'exercise-0001-1a.json').read_text())
        second = json.loads((self.output / 'exercise-0002-1b.json').read_text())
        self.assertEqual([s['page'] for s in first['marker_overrides']['question']['1']['segments']], [31, 29])
        self.assertEqual(second['internal_numbers'], [2])
        contexts = json.loads((self.output / 'shared-contexts.json').read_text())
        self.assertEqual(contexts['question']['exercise-1b-1-1']['question_numbers'], [2])

    def test_adjudications_pass_through_with_reviewer_and_reason(self):
        job = self.cache(blocked=True)
        exercise = self.exercise()
        exercise['cleared_blockers'] = {job['job_fingerprint']: self.clearance(job)}
        exercise['adjudications'] = [dict(page=29, exercise_id='1A', role='question_blocks',
            block_index=0, printed_number=1, reviewer='Operator', reason='Checked printed roster')]
        self.compile([exercise])
        manifest = json.loads((self.output / 'exercise-0001-1a.json').read_text())
        provenance = manifest['review_provenance'][0]
        self.assertEqual(provenance['original_verdict'], 'blocked')
        self.assertEqual(provenance['adjudications'][0]['reviewer'], 'Operator')
        self.assertEqual(provenance['adjudications'][0]['reason'], 'Checked printed roster')
        self.assertEqual(manifest['printed_numbers'], [1])
        self.assertEqual(provenance['geometry_evidence_mode'], 'bound_source_images')
        self.assertEqual(provenance['blocker_clearance']['reviewer'], 'Pixel reviewer')
        original = {path.name: path.read_bytes() for path in self.output.iterdir()}
        self.compile([exercise], force=True)
        self.assertEqual({path.name: path.read_bytes() for path in self.output.iterdir()}, original)

    def test_blocked_clearance_requires_exact_result_and_bound_source_pixels(self):
        job = self.cache(blocked=True)
        exercise = self.exercise()
        exercise['adjudications'] = [dict(page=29, exercise_id='1A', role='question_blocks',
            block_index=0, printed_number=1, reviewer='Operator', reason='Checked roster')]
        good = self.clearance(job)
        wrong_pixels = copy.deepcopy(good)
        wrong_pixels['source_evidence']['29']['sha256'] = '0' * 64
        wrong_dimensions = copy.deepcopy(good)
        wrong_dimensions['source_evidence']['29']['width'] = 321
        unknown_field = dict(good, external_pixel_review=True)
        for clearance, error in ((None, 'result-bound'), (dict(good, result_sha256='0' * 64), 'result-bound'),
                                 (wrong_pixels, 'Source image hash'), (wrong_dimensions, 'pixel review evidence'),
                                 (dict(good, remaining_nulls=[]), 'remaining-null'),
                                 (unknown_field, 'clearance fields')):
            with self.subTest(clearance=clearance):
                exercise['cleared_blockers'] = {} if clearance is None else {job['job_fingerprint']: clearance}
                with self.assertRaisesRegex(ValueError, error):
                    self.compile([exercise])
                self.assertFalse(self.output.exists())

    def test_missing_or_duplicate_adjudications_fail_without_outputs(self):
        job = self.cache(blocked=True)
        exercise = self.exercise()
        exercise['cleared_blockers'] = {job['job_fingerprint']: self.clearance(job)}
        decision = dict(page=29, exercise_id='1A', role='question_blocks', block_index=0,
                        printed_number=1, reviewer='Operator', reason='Checked roster')
        for decisions in ([], [dict(decision, reviewer='')], [decision, decision], [dict(decision, page=30)]):
            with self.subTest(decisions=decisions):
                exercise['adjudications'] = decisions
                with self.assertRaises(ValueError):
                    self.compile([exercise])
                self.assertFalse(self.output.exists())

    def test_adjudication_cannot_rewrite_visible_number(self):
        self.cache()
        exercise = self.exercise()
        exercise['adjudications'] = [dict(page=29, exercise_id='1A', role='question_blocks',
            block_index=0, printed_number=2, reviewer='Operator', reason='Attempted override')]
        with self.assertRaisesRegex(ValueError, 'only replace a null'):
            self.compile([exercise])
        self.assertFalse(self.output.exists())

    def test_no_partial_outputs_when_later_exercise_is_incomplete(self):
        self.cache()
        with self.assertRaisesRegex(ValueError, 'valid.*30'):
            self.compile([self.exercise(), self.exercise('1B', 2, [30])])
        self.assertFalse(self.output.exists())

    def test_missing_roster_and_noncontiguous_internal_order_fail_before_writes(self):
        self.cache()
        for updates in ({'expected_printed_numbers': [1, 2]}, {'internal_number_start': 2},
                        {'source_order': [30]}, {'source_order': []}):
            with self.subTest(updates=updates):
                with self.assertRaises(ValueError):
                    self.compile([dict(self.exercise(), **updates)])
                self.assertFalse(self.output.exists())

    def test_cli_overwrite_guard_and_deterministic_force(self):
        self.cache()
        self.compile()
        expected = {p.name: p.read_bytes() for p in self.output.iterdir()}
        args = ['--plan', str(self.plan), '--results-root', str(self.results), '--output-root', str(self.output)]
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            self.compiler.main(args)
        self.assertEqual(caught.exception.code, 2)
        self.assertIn('--force', stderr.getvalue())
        self.assertEqual({p.name: p.read_bytes() for p in self.output.iterdir()}, expected)
        with contextlib.redirect_stdout(io.StringIO()):
            self.compiler.main(args + ['--force'])
        self.assertEqual({p.name: p.read_bytes() for p in self.output.iterdir()}, expected)

    def test_changed_consumed_snapshot_rejected_even_if_cache_remains_valid(self):
        job = self.cache()
        directory = Path(job['directory'])
        original_compile = self.compiler.reviewed_recovery_markers
        for target in ('job.json', 'receipt.json', 'result.json', 'evidence'):
            with self.subTest(target=target):
                def mutate(*args, **kwargs):
                    compiled = original_compile(*args, **kwargs)
                    if target in ('job.json', 'receipt.json'):
                        path = directory / target
                        path.write_bytes(path.read_bytes() + b' ')
                    else:
                        attempt = directory / ('attempt-' + 'a' * 32)
                        value = json.loads((directory / 'result.json').read_bytes())
                        if target == 'result.json':
                            value['notes'] += ' New valid review.'
                            (attempt / 'response.json').write_bytes(runner.encoded(value))
                        else:
                            (attempt / 'stderr.log').write_text('New transport evidence')
                        runner.store_result(value, job, attempt=attempt)
                    self.assertTrue(runner.valid_cache(job))
                    return compiled
                with patch.object(self.compiler, 'reviewed_recovery_markers', side_effect=mutate):
                    with self.assertRaisesRegex(ValueError, '[Cc]hanged|snapshot'):
                        self.compile()
                self.assertFalse(self.output.exists())

    def test_force_publishes_exact_generation_without_old_exercises(self):
        self.cache()
        self.cache(30, exercise='Exercise 1B')
        self.compile([self.exercise(), self.exercise('1B', 2, [30])])
        self.compile(force=True)
        self.assertEqual([p.name for p in self.output.glob('exercise-*.json')], ['exercise-0001-1a.json'])

    def test_force_refuses_unrelated_directory_contents(self):
        self.cache()
        self.output.mkdir()
        unrelated = self.output / 'user-notes.txt'
        unrelated.write_text('Keep this')
        with self.assertRaisesRegex(ValueError, '[Uu]nmanaged|[Uu]nrelated'):
            self.compile(force=True)
        self.assertEqual(unrelated.read_text(), 'Keep this')
        self.assertEqual(list(self.output.iterdir()), [unrelated])

    def test_staged_write_failure_does_not_publish_partial_files(self):
        self.cache()
        original_open = Path.open
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    self.compile()
                before = {p.name: p.read_bytes() for p in self.output.iterdir()} if existing else None
                def failing_open(path, mode='r', *args, **kwargs):
                    if path.name == 'shared-contexts.json' and ('w' in mode or 'x' in mode):
                        raise OSError('simulated disk failure')
                    return original_open(path, mode, *args, **kwargs)
                with patch.object(Path, 'open', failing_open), self.assertRaisesRegex(OSError, 'simulated'):
                    self.compile(force=existing)
                if existing:
                    self.assertEqual({p.name: p.read_bytes() for p in self.output.iterdir()}, before)
                else:
                    self.assertFalse(self.output.exists())

    def test_publish_rename_failure_restores_previous_generation(self):
        self.cache()
        original_rename = Path.rename
        def failing_rename(path, target):
            if '.stage-' in path.name:
                raise OSError('simulated rename failure')
            return original_rename(path, target)
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    self.compile()
                before = {p.name: p.read_bytes() for p in self.output.iterdir()} if existing else None
                with patch.object(Path, 'rename', failing_rename), self.assertRaisesRegex(OSError, 'simulated'):
                    self.compile(force=existing)
                if existing:
                    self.assertEqual({p.name: p.read_bytes() for p in self.output.iterdir()}, before)
                else:
                    self.assertFalse(self.output.exists())

    def test_evidence_changed_during_staging_cannot_replace_existing_generation(self):
        job = self.cache()
        self.compile()
        before = {p.name: p.read_bytes() for p in self.output.iterdir()}
        original_open = Path.open
        changed = False
        def mutate_on_write(path, mode='r', *args, **kwargs):
            nonlocal changed
            if path.name == 'shared-contexts.json' and 'x' in mode and not changed:
                changed = True
                receipt = Path(job['directory']) / 'receipt.json'
                receipt.write_bytes(receipt.read_bytes() + b' ')
            return original_open(path, mode, *args, **kwargs)
        with patch.object(Path, 'open', mutate_on_write), self.assertRaisesRegex(ValueError, 'changed'):
            self.compile(force=True)
        self.assertTrue(runner.valid_cache(job))
        self.assertEqual({p.name: p.read_bytes() for p in self.output.iterdir()}, before)


if __name__ == '__main__':
    unittest.main()
