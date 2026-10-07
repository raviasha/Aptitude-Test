import io
import json
import zipfile
from fastapi.testclient import TestClient
import app
from tests.book_test_support import BookFixture


class QuestionBooksTests(BookFixture):
    def test_assign_existing_banks_without_reimport(self):
        ids = [self.bank("One"), self.bank("Two")]
        with app.db() as db:
            before = [tuple(r) for r in db.execute("SELECT * FROM questions")]
        result = self.post("/api/admin/books/assign", {"bank_ids": ids, "book_title": "Quantitative"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["bank_ids"], ids)
        with app.db() as db:
            self.assertEqual(before, [tuple(r) for r in db.execute("SELECT * FROM questions")])
        banks = self.client.get("/api/admin/question-banks").json()["banks"]
        self.assertEqual([b["book_title"] for b in banks if b["bank_id"] in ids], ["Quantitative"] * 2)

    def test_unicode_casefold_book_identity_and_rename(self):
        bank = self.bank()
        first = self.post("/api/admin/books/assign", {"bank_ids": [bank], "book_title": " Café "})
        self.assertEqual(first.status_code, 200, first.text)
        second = self.post("/api/admin/books/assign", {"bank_ids": [bank], "book_title": "CAFE\u0301"})
        self.assertEqual(first.json()["book_id"], second.json()["book_id"])
        book_id = first.json()["book_id"]
        renamed = self.client.patch(f"/api/admin/books/{book_id}", json={"title": "New title"}, headers=self.headers)
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["book_id"], book_id)

    def test_assignment_is_atomic_for_missing_bank(self):
        bank = self.bank()
        response = self.post("/api/admin/books/assign", {"bank_ids": [bank, 99999], "book_title": "Unused"})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.client.get("/api/admin/books").json(), {"books": []})

    def test_book_routes_require_admin(self):
        anonymous = TestClient(app.app)
        self.addCleanup(anonymous.close)
        self.assertEqual(anonymous.get("/api/admin/books").status_code, 401)

    def test_migration_is_idempotent_and_preserves_legacy_tests(self):
        with app.db() as db:
            before = [tuple(r) for r in db.execute("SELECT * FROM tests")]
        app.ensure_schema()
        app.ensure_schema()
        with app.db() as db:
            self.assertEqual(before, [tuple(r) for r in db.execute("SELECT * FROM tests")])
            sources = db.execute("SELECT test_id,bank_id FROM test_source_banks").fetchall()
            self.assertEqual(len(sources), len(before))

    def test_manifest_book_metadata_and_legacy_parser_contract(self):
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w") as z:
            z.writestr("manifest.json", json.dumps({"format_version": 2, "bank_name": "Book import",
                "book_title": " Reasoning ", "question_files": ["questions.json"]}))
            z.writestr("questions.json", json.dumps([{"key": "q1", "category": "Logic", "chapter": "One",
                "difficulty": "Easy", "question_text": "What?", "options": {"A": "1", "B": "2", "C": "3", "D": "4"},
                "correct_answer": "A"}]))
        parsed = app.parse_question_package(io.BytesIO(content.getvalue()))
        self.assertEqual(len(parsed), 4)
        response = self.client.post("/api/admin/question-banks/import-package",
            files={"package_file": ("book.zip", content.getvalue(), "application/zip")}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        banks = self.client.get("/api/admin/question-banks").json()["banks"]
        self.assertEqual(next(b for b in banks if b["bank_name"] == "Book import")["book_title"], "Reasoning")
