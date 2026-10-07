"""Book metadata and assessment source identities, independent of HTTP."""
from __future__ import annotations

import json
import sqlite3
import unicodedata


def normalize_title(title: str) -> str:
    if not isinstance(title, str):
        raise ValueError("Book title must be text.")
    title = unicodedata.normalize("NFC", title.strip())
    if not title or len(title) > 200 or any(unicodedata.category(c) == "Cc" for c in title):
        raise ValueError("Book title must contain 1–200 characters without control characters.")
    return title


def source_bank_ids(connection: sqlite3.Connection, test_id: int) -> list[int]:
    test = connection.execute("SELECT bank_id,composition FROM tests WHERE test_id=?", (test_id,)).fetchone()
    if test is None:
        return []
    ids = {r[0] for r in connection.execute("SELECT bank_id FROM test_source_banks WHERE test_id=?", (test_id,))}
    if test["bank_id"] is not None:
        ids.add(test["bank_id"])
    rules = json.loads(test["composition"] or "[]")
    if isinstance(rules, list):
        ids.update(r["bank_id"] for r in rules if isinstance(r, dict) and type(r.get("bank_id")) is int)
    ids.update(r[0] for r in connection.execute("""SELECT DISTINCT q.bank_id FROM responses r
        JOIN attempts a ON a.attempt_id=r.attempt_id JOIN questions q ON q.question_id=r.question_id
        WHERE a.test_id=? AND q.bank_id IS NOT NULL""", (test_id,)))
    return sorted(ids)


def record_test_sources(connection: sqlite3.Connection, test_id: int) -> None:
    connection.executemany("INSERT OR IGNORE INTO test_source_banks(test_id,bank_id) VALUES (?,?)",
                           [(test_id, bank_id) for bank_id in source_bank_ids(connection, test_id)])


def ensure_book_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS books (
        book_id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, title_key TEXT NOT NULL UNIQUE)""")
    if "book_id" not in {r[1] for r in connection.execute("PRAGMA table_info(question_banks)")}:
        connection.execute("ALTER TABLE question_banks ADD COLUMN book_id INTEGER REFERENCES books(book_id)")
    connection.execute("""CREATE TABLE IF NOT EXISTS test_source_banks (
        test_id INTEGER NOT NULL REFERENCES tests(test_id) ON DELETE CASCADE,
        bank_id INTEGER NOT NULL REFERENCES question_banks(bank_id),
        PRIMARY KEY(test_id,bank_id))""")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_test_sources_bank ON test_source_banks(bank_id)")
    for row in connection.execute("SELECT test_id FROM tests").fetchall():
        record_test_sources(connection, row[0])


def assign_book(connection: sqlite3.Connection, bank_ids: list[int], *,
                book_id: int | None = None, book_title: str | None = None) -> dict:
    if (book_id is None) == (book_title is None):
        raise ValueError("Choose an existing book or enter a new book title.")
    if not bank_ids or len(bank_ids) > 1000 or any(type(b) is not int or b <= 0 for b in bank_ids):
        raise ValueError("Choose valid question banks.")
    bank_ids = sorted(set(bank_ids))
    for bank in bank_ids:
        if not connection.execute("SELECT 1 FROM question_banks WHERE bank_id=?", (bank,)).fetchone():
            raise ValueError("A selected question bank no longer exists.")
    if book_title is not None:
        title = normalize_title(book_title)
        connection.execute("INSERT OR IGNORE INTO books(title,title_key) VALUES (?,?)", (title, title.casefold()))
        book = connection.execute("SELECT book_id,title FROM books WHERE title_key=?", (title.casefold(),)).fetchone()
    else:
        book = connection.execute("SELECT book_id,title FROM books WHERE book_id=?", (book_id,)).fetchone()
    if book is None:
        raise ValueError("The selected book no longer exists.")
    connection.executemany("UPDATE question_banks SET book_id=? WHERE bank_id=?",
                           [(book["book_id"], bank) for bank in bank_ids])
    return {**dict(book), "bank_ids": bank_ids}


def rename_book(connection: sqlite3.Connection, book_id: int, title: str) -> dict:
    title = normalize_title(title)
    if not connection.execute("SELECT 1 FROM books WHERE book_id=?", (book_id,)).fetchone():
        raise LookupError("Book not found.")
    if connection.execute("SELECT 1 FROM books WHERE title_key=? AND book_id<>?", (title.casefold(), book_id)).fetchone():
        raise ValueError("A book with this title already exists.")
    connection.execute("UPDATE books SET title=?,title_key=? WHERE book_id=?", (title, title.casefold(), book_id))
    return {"book_id": book_id, "title": title}
