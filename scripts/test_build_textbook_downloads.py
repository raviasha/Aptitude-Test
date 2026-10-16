from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

from scripts.build_textbook_downloads import build


GROUPS = (
    ('quantitative-aptitude', 'quantitative-aptitude-chapters-01-39.zip', 39),
    ('reasoning-source-pages', 'new-reasoning-chapters-01-02-SOURCE-PAGES-ONLY.zip', 2),
    ('new-reasoning', 'new-reasoning-chapters-01-41.zip', 41),
)


class TextbookDownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix='textbook-download-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.latest = self.root / 'question-banks/latest'
        self.output = self.latest / 'textbook-downloads'
        self.records = []
        for group, _, count in GROUPS:
            for number in range(1, count + 1):
                filename = (f'new_reasoning_ch{number:02d}_fixture_v2.zip'
                            if group == 'new-reasoning' else f'ch{number:02d}.zip')
                relative = f'{group}/{filename}'
                path = self.latest / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(path, 'w') as archive:
                    member = zipfile.ZipInfo('fixture.txt', date_time=(2026, 1, 1, 0, 0, 0))
                    member.create_system = 0  # Keep fixture bytes identical on Windows and POSIX.
                    archive.writestr(member, f'{group} chapter {number}\n'.encode())
                self.records.append({
                    'path': relative,
                    'source': f'question-banks/{group}/{filename}',
                    'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                    'questions': 0 if group == 'reasoning-source-pages' else number,
                    'pages': number if group == 'reasoning-source-pages' else 0,
                    'source_commit': 'fixture-only',
                })
        self.write_selection()

    def write_selection(self) -> None:
        (self.latest / 'selection.json').write_text(
            json.dumps({'selected_on': '2026-10-07', 'packages': self.records}, indent=2),
            encoding='utf-8',
        )

    def build(self) -> None:
        with redirect_stdout(io.StringIO()):
            build(self.root)

    def test_new_reasoning_master_contains_41_unchanged_chapter_zips(self) -> None:
        selection_before = (self.latest / 'selection.json').read_bytes()
        self.build()
        path = self.output / 'new-reasoning-chapters-01-41.zip'
        self.assertTrue(path.is_file(), 'The 41-chapter reasoning master was not built')
        records = [r for r in self.records if r['path'].startswith('new-reasoning/')]
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {'README.txt', 'selection.json'} | {
                    f'chapters/new_reasoning_ch{n:02d}_fixture_v2.zip' for n in range(1, 42)
                },
            )
            self.assertEqual(json.loads(archive.read('selection.json')), records)
            self.assertNotIn('manifest.json', archive.namelist())
            for record in records:
                original = self.latest / record['path']
                self.assertEqual(archive.read('chapters/' + original.name), original.read_bytes())
            self.assertIsNone(archive.testzip())
        self.assertEqual((self.latest / 'selection.json').read_bytes(), selection_before)

    def test_existing_groups_keep_exact_baseline_archive_bytes(self) -> None:
        self.build()
        # Legacy ZIP headers retain the host OS marker; fixture ZIP bytes do not.
        # These baselines preserve the original builder's bytes on either host.
        expected = {
            'quantitative-aptitude-chapters-01-39.zip': (
                'd7cea0e96ba1a85077744b10ffcfb13083745793b89656f0a8744a2a6d383f64'
                if os.name == 'nt' else
                '7b68f53d1f7eb401432229e3876e1e926222c12138d649291213bb9e092baf1c'
            ),
            'new-reasoning-chapters-01-02-SOURCE-PAGES-ONLY.zip': (
                '92511556ce08fc924fadd0471096f1aacb620638b82ad4fd5cf31b8ae1db5cc4'
                if os.name == 'nt' else
                '5a6d599fe3f5fd21cca714f4e3afd154099ba43fc7c2694c3cf3cc47afabb1cc'
            ),
        }
        self.assertEqual(
            {name: hashlib.sha256((self.output / name).read_bytes()).hexdigest() for name in expected},
            expected,
        )

    def test_rebuilding_all_three_groups_is_byte_deterministic(self) -> None:
        self.build()
        first = {p.name: p.read_bytes() for p in self.output.glob('*.zip')}
        self.build()
        self.assertEqual({p.name: p.read_bytes() for p in self.output.glob('*.zip')}, first)
        self.assertEqual(set(first), {name for _, name, _ in GROUPS})

    def test_duplicate_selected_paths_are_rejected_for_every_group(self) -> None:
        original = list(self.records)
        for group, _, _ in GROUPS:
            with self.subTest(group=group):
                self.records = list(original)
                indexes = [i for i, r in enumerate(self.records) if r['path'].startswith(group + '/')]
                self.records[indexes[1]] = dict(self.records[indexes[0]])
                self.write_selection()
                with self.assertRaisesRegex(ValueError, 'Duplicate selection path'):
                    self.build()

    def test_colliding_archive_basenames_are_rejected_for_every_group(self) -> None:
        original = list(self.records)
        for group, _, _ in GROUPS:
            for case_variant in (False, True):
                with self.subTest(group=group, case_variant=case_variant):
                    self.records = list(original)
                    indexes = [i for i, r in enumerate(self.records) if r['path'].startswith(group + '/')]
                    first = self.records[indexes[0]]
                    name = Path(first['path']).name
                    relative = f'{group}/nested/{name.upper() if case_variant else name}'
                    destination = self.latest / relative
                    destination.parent.mkdir(exist_ok=True)
                    destination.write_bytes((self.latest / first['path']).read_bytes())
                    self.records[indexes[1]] = {**first, 'path': relative}
                    self.write_selection()
                    with self.assertRaisesRegex(ValueError, 'Duplicate chapter basename'):
                        self.build()

    def test_incomplete_new_group_fails_before_replacing_existing_masters(self) -> None:
        original = list(self.records)
        self.output.mkdir()
        for _, name, _ in GROUPS[:2]:
            (self.output / name).write_bytes(b'previous-master-must-survive')
        for count in (0, 40, 42):
            with self.subTest(count=count):
                self.records = [r for r in original if not r['path'].startswith('new-reasoning/')]
                self.records += [r for r in original if r['path'].startswith('new-reasoning/')][:count]
                if count == 42:
                    self.records.append({**original[-1], 'path': 'new-reasoning/new_reasoning_ch42_fixture_v2.zip'})
                self.write_selection()
                with self.assertRaisesRegex(ValueError, f'new-reasoning: expected 41 chapters, got {count}'):
                    self.build()
                for _, name, _ in GROUPS[:2]:
                    self.assertEqual((self.output / name).read_bytes(), b'previous-master-must-survive')
                self.assertFalse((self.output / GROUPS[2][1]).exists())

    def test_duplicate_new_group_preserves_existing_master_outputs(self) -> None:
        self.output.mkdir()
        for _, name, _ in GROUPS:
            (self.output / name).write_bytes(b'previous-master-must-survive')
        self.records[-1] = dict(self.records[-2])
        self.write_selection()
        with self.assertRaisesRegex(ValueError, 'Duplicate selection path'):
            self.build()
        for _, name, _ in GROUPS:
            self.assertEqual((self.output / name).read_bytes(), b'previous-master-must-survive')

    def test_hash_mismatch_is_rejected_in_new_group(self) -> None:
        self.seed_previous_masters()
        self.records[-1]['sha256'] = '0' * 64
        self.write_selection()
        with self.assertRaisesRegex(ValueError, 'Selection hash mismatch'):
            self.build()
        self.assert_previous_masters_unchanged()

    def test_corrupt_zip_with_matching_hash_is_rejected_in_new_group(self) -> None:
        self.seed_previous_masters()
        path = self.latest / self.records[-1]['path']
        path.write_bytes(b'not a ZIP')
        self.records[-1]['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.write_selection()
        with self.assertRaises(zipfile.BadZipFile):
            self.build()
        self.assert_previous_masters_unchanged()

    def seed_previous_masters(self) -> None:
        self.output.mkdir(exist_ok=True)
        for _, name, _ in GROUPS:
            (self.output / name).write_bytes(b'previous-master-must-survive')

    def assert_previous_masters_unchanged(self) -> None:
        for _, name, _ in GROUPS:
            self.assertEqual((self.output / name).read_bytes(), b'previous-master-must-survive')
        self.assertEqual(list(self.output.glob('*.tmp')), [])

    def test_reasoning_requires_exact_chapter_identities_not_just_41_files(self) -> None:
        original = self.records[-1]
        content = (self.latest / original['path']).read_bytes()
        self.seed_previous_masters()
        for name in ('new_reasoning_ch42_fixture_v2.zip',
                     'new_reasoning_ch01_other_v2.zip', 'unknown_chapter.zip'):
            with self.subTest(name=name):
                relative = 'new-reasoning/' + name
                (self.latest / relative).write_bytes(content)
                self.records[-1] = {**original, 'path': relative}
                self.write_selection()
                with self.assertRaisesRegex(ValueError, 'new-reasoning:.*chapter identities'):
                    self.build()
                self.assert_previous_masters_unchanged()


if __name__ == '__main__':
    unittest.main()
