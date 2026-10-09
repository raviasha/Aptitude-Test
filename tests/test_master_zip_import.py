import io
import json
import zipfile
import asyncio
import threading
import httpx
from unittest.mock import patch

import app
from tests.book_test_support import BookFixture


def archive_bytes(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return stream.getvalue()


def chapter(name):
    return archive_bytes([
        ('manifest.json', json.dumps({'format_version': 2, 'bank_name': name,
                                     'book_title': 'Example Book', 'question_files': ['questions.json']})),
        ('questions.json', json.dumps([{'key': 'q1', 'category': 'Logic', 'chapter': name,
            'difficulty': 'Easy', 'question_text': 'Choose one',
            'options': {'A': '1', 'B': '2', 'C': '3', 'D': '4'}, 'correct_answer': 'A'}]))])


class MasterZipTests(BookFixture):
    def test_import_does_not_block_other_server_requests(self):
        entered, release = threading.Event(), threading.Event()
        original = app.import_uploaded_package

        def held_import(*args):
            entered.set()
            release.wait(3)
            return original(*args)

        async def exercise():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),
                    base_url='http://testserver', cookies=dict(self.client.cookies)) as client:
                pending = asyncio.create_task(client.post('/api/admin/question-banks/import-package',
                    files={'package_file': ('chapter.zip', chapter('First'))}, headers=self.headers))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    self.assertEqual((await client.get('/api/build')).status_code, 200)
                    self.assertFalse(pending.done(), 'Import must not occupy the event loop')
                finally:
                    release.set()
                    result = await pending
                self.assertEqual(result.status_code, 200, result.text)

        with patch.object(app, 'import_uploaded_package', side_effect=held_import):
            asyncio.run(exercise())

    def test_product_version_is_distinct_from_legacy_client_handshake(self):
        self.assertEqual(self.client.get('/api/build').json(), {'version': '2.1.0'})
        self.assertEqual(self.client.get('/api/coordinator-build').json(), {'version': '2.2.0'})

    def upload(self, content, replace=False):
        return self.client.post('/api/admin/question-banks/import-package',
            files={'package_file': ('textbook.zip', content, 'application/zip')},
            data={'replace_existing': str(replace).lower()}, headers=self.headers)

    def test_master_imports_chapters_and_preserves_book_metadata(self):
        result = self.upload(archive_bytes([('README.txt', 'Read me'),
            ('chapters/01.zip', chapter('First')), ('chapters/02.zip', chapter('Second'))]))
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual([r['ok'] for r in result.json()['results']], [True, True])
        banks = self.client.get('/api/admin/question-banks').json()['banks']
        imported = [b for b in banks if b['bank_name'] in ('First', 'Second')]
        self.assertEqual(len(imported), 2)
        self.assertEqual({b['book_title'] for b in imported}, {'Example Book'})

    def test_bad_chapter_does_not_hide_successes(self):
        result = self.upload(archive_bytes([('01.zip', chapter('First')),
            ('02.zip', b'broken'), ('03.zip', chapter('Last'))]))
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual([r['ok'] for r in result.json()['results']], [True, False, True])

    def test_malformed_manifest_does_not_discard_committed_results(self):
        bad = archive_bytes([('manifest.json', json.dumps({'format_version': 2,
            'bank_name': 'Bad', 'question_files': ['questions.json'], 'stimuli': None})),
            ('questions.json', zipfile.ZipFile(io.BytesIO(chapter('Bad'))).read('questions.json'))])
        result = self.upload(archive_bytes([('01.zip', chapter('First')),
                                           ('02.zip', bad), ('03.zip', chapter('Last'))]))
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual([r['ok'] for r in result.json()['results']], [True, False, True])

    def test_normalized_manifest_path_works_in_master(self):
        with zipfile.ZipFile(io.BytesIO(chapter('First'))) as source:
            normalized = archive_bytes([('./' + n, source.read(n)) for n in source.namelist()])
        result = self.upload(archive_bytes([('01.zip', normalized)]))
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()['results'][0]['ok'], result.text)

    def test_damaged_deflate_chapter_does_not_stop_remaining_imports(self):
        damaged = bytearray(chapter('Damaged'))
        with zipfile.ZipFile(io.BytesIO(damaged)) as archive:
            entry = archive.getinfo('questions.json')
            offset = entry.header_offset + 30 + len(entry.filename.encode()) + len(entry.extra)
        damaged[offset] = 7  # DEFLATE reserved block type: invalid compressed stream.
        result = self.upload(archive_bytes([('01.zip', chapter('First')),
            ('02.zip', damaged), ('03.zip', chapter('Last'))]))
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual([r['ok'] for r in result.json()['results']], [True, False, True])

    def test_duplicate_is_reported_and_existing_bank_not_overwritten(self):
        self.assertEqual(self.upload(chapter('First')).status_code, 200)
        result = self.upload(archive_bytes([('01.zip', chapter('First'))]))
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(result.json()['results'][0]['ok'])
        retry = self.upload(archive_bytes([('01.zip', chapter('First'))]), replace=True)
        self.assertTrue(retry.json()['results'][0]['ok'], retry.text)

    def test_unsafe_outer_path_rejected_before_any_import(self):
        result = self.upload(archive_bytes([('01.zip', chapter('First')),
                                           ('../02.zip', chapter('Second'))]))
        self.assertEqual(result.status_code, 400, result.text)
        with app.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM question_banks WHERE bank_name='First'").fetchone()[0], 0)

    def test_source_pages_have_clear_error(self):
        pages = archive_bytes([('manifest.json', json.dumps({'format_version': 1,
            'package_type': 'all_vision_image_only', 'pages': []})), ('pages/001.png', b'page')])
        result = self.upload(archive_bytes([('chapters/source.zip', pages)]))
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(result.json()['results'][0]['ok'])
        self.assertIn('source-page', result.json()['results'][0]['message'].lower())

    def test_duplicate_outer_names_rejected(self):
        result = self.upload(archive_bytes([('01.zip', chapter('First')), ('01.zip', chapter('Second'))]))
        self.assertEqual(result.status_code, 400, result.text)

    def test_nested_master_is_not_recursively_imported(self):
        result = self.upload(archive_bytes([('nested.zip', archive_bytes([('01.zip', chapter('First'))]))]))
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(result.json()['results'][0]['ok'])

    def test_master_unpacked_limit_is_checked_before_import(self):
        with patch.object(app, 'MAX_PACKAGE_UNPACKED_BYTES', 10):
            result = self.upload(archive_bytes([('01.zip', chapter('First'))]))
        self.assertEqual(result.status_code, 413, result.text)

    def test_combined_inner_expansion_is_bounded_before_saving(self):
        large = archive_bytes([('manifest.json', '{}'), ('padding.txt', 'x' * 6000)])
        with patch.object(app, 'MAX_PACKAGE_UNPACKED_BYTES', 10000):
            result = self.upload(archive_bytes([('01.zip', large), ('02.zip', large)]))
        self.assertEqual(result.status_code, 413, result.text)
