"""Compile current, cache-validated recovery evidence from an operator plan.

Usage: python scripts/compile_logical_recovery_results.py --plan plan.json
    --results-root boundary-reviews --output-root reviewed-manifests [--force]

Plan: {"chapter_id": 101, "exercises": [{"exercise_id": "1A",
"internal_number_start": 1, "expected_printed_numbers": [1, 2],
"source_order": [29, 30], "adjudications": []}]}.
Exercises are ordered and contiguous from internal number 1. source_order is
the explicit page roster and reading order, not the printed-number order.
Optional adjudications are objects with page, exercise_id, role, block_index,
printed_number, reviewer and reason. They can only resolve null numbers as
permitted by reviewed_recovery_markers; they cannot override other gates.
Optional context_adjudications entries contain exactly page, exercise_id,
block_index, target_printed_number_start, target_printed_number_end, role,
reviewer and reason. They map to (page, canonical exercise_id, shared-block index).
Only two explicitly null endpoints in a blocked result can be adjudicated; the
inclusive range must be wholly in expected_printed_numbers. role is question,
answer_key or solution. Clearance remaining_nulls must separately inventory each
null endpoint with role=shared_context_blocks and its target_printed_number_* field.
Each exercise may also supply cleared_blockers keyed by the exact blocked job
fingerprint. Each clearance contains result_sha256 (SHA-256 of sorted, compact,
UTF-8 JSON with ensure_ascii=False), reviewer, reason, cleared_checks, other_blockers,
remaining_nulls, and source_evidence. cleared_checks must list geometry, coverage,
exercise_identity, content_completeness, other_notes; other_blockers must be [].
remaining_nulls entries contain page, exercise_id, role, block_index and field.
source_evidence uses positive decimal page keys and sha256, width, height,
review_reference entries. The recovery API verifies these exact attestations;
the compiler always supplies cache-bound target-original pixels for validation.

Outputs are exercise-0001-1a.json etc. (sortable in plan order), and the plain
shared-contexts.json mapping accepted by chapter_config. No backend is invoked.
Output directories are owned generations with a digest inventory. Force replaces
only an unchanged owned generation; previous generations remain in sibling
.backup-* directories. Staged directory swaps restore the old generation on
publication errors. A process crash during the swap may leave the output absent;
the complete backup remains recoverable. Unmanaged directories are never moved.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / 'data-engineering'):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from scripts import run_logical_boundary_recovery as runner
from logical_reasoning_v2.book_map import chapters
from logical_reasoning_v2.marker_review import reviewed_recovery_markers


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate JSON key: {key}')
        result[key] = value
    return result


def _read(path):
    return json.loads(Path(path).read_bytes(), object_pairs_hook=_unique_object)


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:Exercise\s+)?\d+[A-Za-z0-9]*', value.strip(), re.I):
        raise ValueError('Invalid exercise_id')
    return runner.exercise_id(value)


def _positive_list(value, label):
    if (not isinstance(value, list) or not value or
            any(type(n) is not int or n <= 0 for n in value) or len(set(value)) != len(value)):
        raise ValueError(f'{label} must be a nonempty list of unique positive integers')


def _plan(path):
    plan = _read(path)
    if not isinstance(plan, dict) or set(plan) != {'chapter_id', 'exercises'}:
        raise ValueError('Plan requires only chapter_id and ordered exercises')
    chapter_id = plan['chapter_id']
    chapter = next((c for c in chapters() if type(chapter_id) is int and c.pipeline_id == chapter_id), None)
    if chapter is None:
        raise ValueError('Unknown chapter_id')
    if not isinstance(plan['exercises'], list) or not plan['exercises']:
        raise ValueError('Plan requires nonempty ordered exercises')
    seen, start = set(), 1
    required = {'exercise_id', 'internal_number_start', 'expected_printed_numbers', 'source_order'}
    for exercise in plan['exercises']:
        if (not isinstance(exercise, dict) or not required <= exercise.keys() or
                exercise.keys() - required - {'adjudications', 'context_adjudications', 'cleared_blockers'}):
            raise ValueError('Exercise requires explicit identity, internal start, expected roster and source_order')
        identity = _identity(exercise['exercise_id'])
        if identity in seen:
            raise ValueError('Duplicate exercise identity in plan')
        if chapter_id == 101 and identity == 'Exercise 1J':
            raise ValueError('Exercise 1J is excluded from Chapter 101')
        seen.add(identity)
        exercise['exercise_id'] = identity
        if type(exercise['internal_number_start']) is not int or exercise['internal_number_start'] != start:
            raise ValueError('Exercise internal_number_start must be contiguous in plan order from 1')
        _positive_list(exercise['expected_printed_numbers'], 'expected_printed_numbers')
        _positive_list(exercise['source_order'], 'source_order')
        if any(not chapter.pdf_page_start <= page <= chapter.pdf_page_end for page in exercise['source_order']):
            raise ValueError('source_order page is outside the selected chapter')
        start += len(exercise['expected_printed_numbers'])
    return plan


def _decisions(exercise):
    values = exercise.get('adjudications', [])
    if not isinstance(values, list):
        raise ValueError('Adjudications must be a list of narrowly keyed decisions')
    decisions = {}
    fields = {'page', 'exercise_id', 'role', 'block_index', 'printed_number', 'reviewer', 'reason'}
    for value in values:
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError('Adjudication requires exact page/exercise/role/block key, number, reviewer and reason')
        if (type(value['page']) is not int or value['page'] not in exercise['source_order'] or
                _identity(value['exercise_id']) != exercise['exercise_id'] or
                value['role'] not in ('question_blocks', 'answer_solution_blocks') or
                type(value['block_index']) is not int or value['block_index'] < 0 or
                type(value['printed_number']) is not int or value['printed_number'] <= 0 or
                any(not isinstance(value[k], str) or not value[k].strip() for k in ('reviewer', 'reason'))):
            raise ValueError('Invalid or out-of-scope adjudication')
        key = (value['page'], exercise['exercise_id'], value['role'], value['block_index'])
        if key in decisions:
            raise ValueError('Duplicate adjudication key')
        decisions[key] = {k: value[k] for k in ('printed_number', 'reviewer', 'reason')}
    return decisions


def _context_decisions(exercise):
    values = exercise.get('context_adjudications', [])
    if not isinstance(values, list):
        raise ValueError('Context adjudications must be a list of narrowly keyed decisions')
    fields = {'page', 'exercise_id', 'block_index', 'target_printed_number_start',
              'target_printed_number_end', 'role', 'reviewer', 'reason'}
    roster = set(exercise['expected_printed_numbers'])
    decisions = {}
    for value in values:
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError('Context adjudication requires exact page/exercise/block, endpoints, role, reviewer and reason')
        try:
            identity = _identity(value['exercise_id'])
        except ValueError as error:
            raise ValueError('Invalid context adjudication exercise identity') from error
        if (type(value['page']) is not int or value['page'] not in exercise['source_order'] or
                identity != exercise['exercise_id'] or type(value['block_index']) is not int or value['block_index'] < 0 or
                value['role'] not in ('question', 'answer_key', 'solution') or
                any(not isinstance(value[k], str) or not value[k].strip() for k in ('reviewer', 'reason'))):
            raise ValueError('Invalid or out-of-scope context adjudication')
        start, end = value['target_printed_number_start'], value['target_printed_number_end']
        if (type(start) is not int or type(end) is not int or start <= 0 or end < start or
                end - start + 1 > len(roster) or any(n not in roster for n in range(start, end + 1))):
            raise ValueError('Context adjudication range must be ordered and wholly within the expected roster')
        key = (value['page'], identity, value['block_index'])
        if key in decisions:
            raise ValueError('Duplicate context adjudication key')
        decisions[key] = {k: value[k] for k in ('target_printed_number_start', 'target_printed_number_end',
                                               'role', 'reviewer', 'reason')}
    return decisions


def _clearances(exercise):
    values = exercise.get('cleared_blockers', {})
    if not isinstance(values, dict):
        raise ValueError('cleared_blockers must be an object keyed by job fingerprint')
    fields = {'result_sha256', 'reviewer', 'reason', 'cleared_checks', 'other_blockers',
              'remaining_nulls', 'source_evidence'}
    result = {}
    for fingerprint, clearance in values.items():
        if (not isinstance(fingerprint, str) or not re.fullmatch(r'[0-9a-f]{64}', fingerprint)
                or not isinstance(clearance, dict) or set(clearance) != fields):
            raise ValueError('Invalid cleared_blockers fingerprint or clearance fields')
        nulls = clearance['remaining_nulls']
        if not isinstance(nulls, list) or any(
            not isinstance(item, dict) or set(item) != {'page', 'exercise_id', 'role', 'block_index', 'field'}
            for item in nulls
        ):
            raise ValueError('Invalid remaining_nulls inventory fields')
        seen_nulls = set()
        for item in nulls:
            if (type(item['page']) is not int or item['page'] not in exercise['source_order'] or
                    type(item['block_index']) is not int or item['block_index'] < 0 or
                    (item['role'], item['field']) not in (
                        ('question_blocks', 'printed_number'), ('answer_solution_blocks', 'printed_number'),
                        ('shared_context_blocks', 'target_printed_number_start'),
                        ('shared_context_blocks', 'target_printed_number_end'))):
                raise ValueError('Invalid remaining-null inventory entry')
            key = (item['page'], _identity(item['exercise_id']), item['role'], item['block_index'], item['field'])
            if key in seen_nulls:
                raise ValueError('Duplicate remaining-null inventory entry')
            seen_nulls.add(key)
        evidence = clearance['source_evidence']
        if not isinstance(evidence, dict):
            raise ValueError('source_evidence must be an object keyed by page')
        normalized = {}
        for page, binding in evidence.items():
            if (not isinstance(page, str) or not re.fullmatch(r'[1-9][0-9]*', page) or
                    not isinstance(binding, dict) or
                    set(binding) != {'sha256', 'width', 'height', 'review_reference'}):
                raise ValueError('Invalid source_evidence page or binding fields')
            normalized[int(page)] = dict(binding)
        result[fingerprint] = dict(clearance, source_evidence=normalized)
    return result


def _source_image(job):
    originals = [item for item in job['images'] if item['role'] == 'target-original']
    bindings = [item for item in job['binding']['images'] if item['role'] == 'target-original']
    if (len(originals) != 1 or len(bindings) != 1 or
            {k: v for k, v in originals[0].items() if k != 'path'} != bindings[0]):
        raise ValueError('Selected job needs exactly one bound target-original image')
    path = Path(originals[0]['path'])
    if (path.resolve() != (Path(job['directory']) / bindings[0]['name']).resolve() or
            runner.digest(path.read_bytes()) != bindings[0]['sha256']):
        raise ValueError('Target-original image differs from its job binding')
    return path


def _assert_unchanged(snapshot):
    for path, digest in snapshot.items():
        if not path.is_file() or runner.digest(path.read_bytes()) != digest:
            raise ValueError(f'Consumed evidence changed since snapshot: {path}')


def _pin_job(job, job_raw, receipt_raw):
    """Capture every file checked by the runner, including successful transport."""
    directory = Path(job['directory'])
    binding = job['binding']
    receipt = json.loads(receipt_raw, object_pairs_hook=_unique_object)
    attempt_id = receipt['successful_attempt']['attempt_id']
    if not isinstance(attempt_id, str) or not re.fullmatch(r'attempt-[0-9a-f]{32}', attempt_id):
        raise ValueError('Invalid successful attempt identity')
    attempt = directory / attempt_id
    paths = {directory / name for name in ('job.json', 'result.json', 'receipt.json', 'prompt.txt', 'schema.json')}
    paths.update(attempt / name for name in ('prompt.txt', 'response.json', 'stdout.log', 'stderr.log',
                                             'process.json', 'command.json'))
    paths.update((Path(job['schema_source']), Path(binding['source_path']), Path(runner.__file__)))
    paths.update(Path(p['path']) for p in binding['proposals'])
    paths.update(Path(p['path']) for p in job['images'])
    paths.update(Path(p['source_path']) for p in binding['images'])
    paths.update(Path(binding['reviews_root']) / p['relative_path'] for p in binding['proposal_inventory'])
    if binding.get('context_manifest'):
        paths.add(Path(binding['context_manifest']['path']))
    captured = {p.resolve(): p.read_bytes() for p in paths}
    if (captured[(directory / 'job.json').resolve()] != job_raw or
            captured[(directory / 'receipt.json').resolve()] != receipt_raw):
        raise ValueError('Job or receipt changed during snapshot')
    result_raw = captured[(directory / 'result.json').resolve()]
    if runner.digest(result_raw) != receipt['result_sha256']:
        raise ValueError('Result changed during snapshot')
    return json.loads(result_raw, object_pairs_hook=_unique_object), {
        path: runner.digest(raw) for path, raw in captured.items()}


def _current_results(root):
    if not root.is_dir():
        raise ValueError(f'Results root does not exist: {root}')
    current = {}
    for path in sorted(root.rglob('job.json')):
        try:
            job_raw = path.read_bytes()
            job = json.loads(job_raw, object_pairs_hook=_unique_object)
            if not isinstance(job, dict) or Path(job['directory']).resolve() != path.parent.resolve():
                continue
            result, snapshot = _pin_job(job, job_raw, (path.parent / 'receipt.json').read_bytes())
            if not runner.valid_cache(job):
                continue
            _assert_unchanged(snapshot)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        page = job['binding']['page']
        if page in current:
            raise ValueError(f'Duplicate current valid results for page {page}')
        current[page] = (job, result, snapshot)
    return current


def _owned_generation(root):
    """Refuse to swap an unrelated directory or a modified published generation."""
    marker = root / '_generation.json'
    try:
        metadata = _read(marker)
        files = metadata['files']
        if (root.is_symlink() or not root.is_dir() or
                metadata['artifact_type'] != 'logical-recovery-generation' or not isinstance(files, dict) or
                set(p.name for p in root.iterdir()) != set(files) | {'_generation.json'} or
                any(Path(name).name != name or (root / name).is_symlink() or
                    not (root / name).is_file() or runner.digest((root / name).read_bytes()) != digest
                    for name, digest in files.items())):
            raise ValueError('Invalid generation inventory')
        return {p.resolve(): runner.digest(p.read_bytes()) for p in root.iterdir()}
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(f'Unmanaged or changed output directory; refusing replacement: {root}') from error


def _publish(outputs, root, force, verify):
    root = root.absolute()
    root.parent.mkdir(parents=True, exist_ok=True)
    lock = root.with_name(f'.{root.name}.publish.lock')
    # Exclusive creation serializes writers; never remove a lock owned by another run.
    with lock.open('x', encoding='utf-8'):
        pass
    stage = None
    try:
        previous = None
        if root.exists() or root.is_symlink():
            if not force:
                raise ValueError('Output already exists; use --force to overwrite')
            previous = _owned_generation(root)
        stage = Path(tempfile.mkdtemp(prefix=f'.{root.name}.stage-', dir=root.parent))
        inventory = {}
        for name, payload in outputs.items():
            raw = runner.encoded(payload)
            with (stage / name).open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            inventory[name] = runner.digest(raw)
        with (stage / '_generation.json').open('xb') as stream:
            stream.write(runner.encoded({'artifact_type': 'logical-recovery-generation', 'files': inventory}))
            stream.flush()
            os.fsync(stream.fileno())
        verify()
        backup = None
        if previous is not None:
            if _owned_generation(root) != previous:
                raise ValueError('Output generation changed during staging')
            backup = root.with_name(f'.{root.name}.backup-{uuid.uuid4().hex}')
            root.rename(backup)
        elif root.exists():
            raise ValueError('Output appeared during staging')
        try:
            stage.rename(root)
        except OSError:
            if backup is not None:
                backup.rename(root)
            raise
    finally:
        # Only the uniquely created staging directory can be removed here.
        if stage is not None and stage.exists() and stage.parent.resolve() == root.parent.resolve():
            shutil.rmtree(stage)
        lock.unlink()


def compile_results(plan_path, results_root, output_root, *, force=False):
    """Validate the entire plan and evidence before creating any output files."""
    plan_snapshot = {Path(plan_path).resolve(): runner.digest(Path(plan_path).read_bytes())}
    plan = _plan(plan_path)
    _assert_unchanged(plan_snapshot)
    current = _current_results(Path(results_root))
    outputs, shared, selected_jobs = {}, {}, []
    for index, exercise in enumerate(plan['exercises'], 1):
        results, sizes, source_images = [], {}, {}
        for page in exercise['source_order']:
            if page not in current:
                raise ValueError(f'No current valid cache for required page {page}')
            job, result, snapshot = current[page]
            identities = {_identity(e['exercise_id']) for e in result['pages'][0]['exercises']}
            if exercise['exercise_id'] not in identities:
                raise ValueError(f"Exercise {exercise['exercise_id']} is missing on selected page {page}")
            sizes[page] = (job['binding']['width'], job['binding']['height'])
            source_images[page] = _source_image(job)
            results.append(result)
            selected_jobs.append((job, snapshot))
        markers, contexts = reviewed_recovery_markers(
            results, exercise_id=exercise['exercise_id'], page_sizes=sizes,
            internal_number_start=exercise['internal_number_start'], source_order=exercise['source_order'],
            expected_printed_numbers=exercise['expected_printed_numbers'], adjudications=_decisions(exercise),
            context_adjudications=_context_decisions(exercise),
            cleared_blockers=_clearances(exercise), source_images=source_images)
        payload = dict(schema_version=1, artifact_type='logical-reasoning-reviewed-exercise-markers',
                       chapter_id=plan['chapter_id'], **asdict(markers))
        slug = markers.exercise_id.removeprefix('Exercise ').lower()
        outputs[f'exercise-{index:04d}-{slug}.json'] = payload
        for role, groups in contexts.items():
            target = shared.setdefault(role, {})
            if target.keys() & groups.keys():
                raise ValueError('Duplicate shared-context identity')
            target.update(groups)
    outputs['shared-contexts.json'] = shared
    output_root = Path(output_root)
    paths = [output_root / name for name in outputs]
    def verify():
        _assert_unchanged(plan_snapshot)
        for job, snapshot in selected_jobs:
            _assert_unchanged(snapshot)
            if not runner.valid_cache(job):
                raise ValueError('Selected cache changed during compilation; no outputs written')
            _assert_unchanged(snapshot)
    _publish(outputs, output_root, force, verify)
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--results-root', type=Path, required=True)
    parser.add_argument('--output-root', type=Path, required=True)
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args(argv)
    try:
        paths = compile_results(args.plan, args.results_root, args.output_root, force=args.force)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps({'status': 'written', 'paths': [str(path) for path in paths]}, sort_keys=True))


if __name__ == '__main__':
    main()
