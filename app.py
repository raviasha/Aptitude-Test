"""Local-LAN Student Aptitude Assessment MVP.

Run with: uvicorn app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import csv
import html
import io
import json
import os
import random
import shutil
import socket
import sqlite3
import sys
import threading
import uuid
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional

import bcrypt
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

SOURCE_ROOT = Path(__file__).resolve().parent
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", SOURCE_ROOT))
if getattr(sys, "frozen", False):
    DATA_DIR = Path(os.getenv("PROGRAMDATA", r"C:\ProgramData")) / "Aptitude Lab"
else:
    DATA_DIR = SOURCE_ROOT / "data"
DB_PATH = DATA_DIR / "aptitude.db"
BACKUP_DIR = DATA_DIR / "backups"
QUESTION_BANKS_DIR = DATA_DIR / "Question Banks"
STATIC_DIR = BUNDLE_DIR / "static"
TEMPLATE_DIR = BUNDLE_DIR / "templates"
SERVER_URL = "http://127.0.0.1:8000"

CATEGORIES = [
    "Quantitative Aptitude",
    "Logical Reasoning",
    "Data Interpretation",
    "Verbal Ability",
    "Coding / Computational Thinking",
]

DEFAULT_COMPOSITION = {category: 6 for category in CATEGORIES}

app = FastAPI(title="Aptitude Lab")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "replace-this-before-production"), https_only=False, same_site="lax")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class LoginPayload(BaseModel):
    identifier: str
    password: str
    role: str = "student"


class AnswerPayload(BaseModel):
    answer: Optional[str] = Field(None, pattern="^[ABCD]$")


class SubmitPayload(BaseModel):
    confirmed: bool = False


class StudentPayload(BaseModel):
    student_id: str
    name: str
    student_class: str = "AI & DS"
    section: str = "A"
    password: str = "student123"


class RegistrationPayload(BaseModel):
    student_id: str
    name: str
    student_class: str = "AI & DS"
    section: str = "A"
    password: str


class TestPayload(BaseModel):
    test_name: str
    composition: Dict[str, int]
    bank_id: int


class FolderImportPayload(BaseModel):
    html_filename: str
    answer_key_filename: str


class RegistrationError(Exception):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def check_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def register_student(student_id: str, name: str, student_class: str, section: str, password: str) -> Dict[str, str]:
    normalized_id = student_id.strip().upper()
    normalized_name = name.strip()
    normalized_class = student_class.strip() or "AI & DS"
    normalized_section = section.strip() or "A"
    if not normalized_id or not normalized_name or len(password) < 6:
        raise RegistrationError("Enter a Student ID, name, and a password of at least 6 characters.")
    with db() as connection:
        try:
            connection.execute(
                "INSERT INTO students VALUES (?, ?, ?, ?, ?, ?)",
                (normalized_id, normalized_name, hash_password(password), normalized_class, normalized_section, now()),
            )
        except sqlite3.IntegrityError as error:
            raise RegistrationError("That Student ID is already registered.") from error
    return {"student_id": normalized_id, "name": normalized_name}


def delete_student(student_id: str) -> Dict[str, str]:
    normalized_id = student_id.strip().upper()
    with db() as connection:
        if not connection.execute("SELECT 1 FROM students WHERE student_id = ?", (normalized_id,)).fetchone():
            raise RegistrationError("Student not found.")
        attempt_ids = [row["attempt_id"] for row in connection.execute("SELECT attempt_id FROM attempts WHERE student_id = ?", (normalized_id,)).fetchall()]
        if attempt_ids:
            placeholders = ",".join("?" for _ in attempt_ids)
            connection.execute(f"DELETE FROM responses WHERE attempt_id IN ({placeholders})", attempt_ids)
            connection.execute(f"DELETE FROM attempts WHERE attempt_id IN ({placeholders})", attempt_ids)
        connection.execute("DELETE FROM students WHERE student_id = ?", (normalized_id,))
    return {"student_id": normalized_id}


def student_available_tests(connection: sqlite3.Connection) -> List[Dict[str, Any]]:
    launched = connection.execute("SELECT * FROM tests WHERE active = 1 AND launched = 1 ORDER BY test_id DESC").fetchall()
    available = launched or connection.execute("SELECT * FROM tests WHERE active = 1 ORDER BY test_id DESC").fetchall()
    return [dict(test) for test in available]


def feedback_allowed(test: sqlite3.Row | Dict[str, Any]) -> bool:
    return not bool(test["launched"])


def rows(items: List[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [dict(item) for item in items]


def ensure_column(connection: sqlite3.Connection, table: str, definition: str) -> None:
    column = definition.split()[0]
    existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


class QuestionSectionParser(HTMLParser):
    """Extract the inner HTML from <section data-question-key="..."> blocks."""

    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.fragments: Dict[str, List[str]] = {}
        self.current_key: Optional[str] = None
        self.depth = 0
        self.errors: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        attributes = dict(attrs)
        if self.current_key is None:
            if tag == "section" and attributes.get("data-question-key"):
                key = attributes["data-question-key"].strip()
                if key in self.fragments:
                    self.errors.append(f"Duplicate data-question-key: {key}")
                    return
                self.current_key, self.depth = key, 1
                self.fragments[key] = []
            return
        self.fragments[self.current_key].append(self.get_starttag_text())
        if tag not in self.VOID_TAGS:
            self.depth += 1

    def handle_startendtag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        if self.current_key:
            self.fragments[self.current_key].append(self.get_starttag_text())

    def handle_endtag(self, tag: str) -> None:
        if not self.current_key:
            return
        if tag == "section" and self.depth == 1:
            self.current_key, self.depth = None, 0
            return
        self.fragments[self.current_key].append(f"</{tag}>")
        if tag not in self.VOID_TAGS:
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if self.current_key:
            self.fragments[self.current_key].append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if self.current_key:
            self.fragments[self.current_key].append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self.current_key:
            self.fragments[self.current_key].append(f"&#{name};")

    def close(self) -> None:
        super().close()
        if self.current_key:
            self.errors.append(f"Question block {self.current_key!r} is missing its closing </section> tag.")


class SafeVisualHTML(HTMLParser):
    """Allow static question markup and SVG while removing executable/browser-active HTML."""

    SAFE_TAGS = {
        "p", "div", "span", "strong", "em", "b", "i", "small", "sub", "sup", "br", "hr", "ul", "ol", "li",
        "h1", "h2", "h3", "h4", "h5", "h6", "table", "thead", "tbody", "tfoot", "tr", "th", "td", "figure",
        "figcaption", "svg", "g", "path", "rect", "circle", "line", "polyline", "polygon", "text", "ellipse", "defs",
        "lineargradient", "stop", "title", "desc",
    }
    BLOCKED_TAGS = {"script", "style", "iframe", "object", "embed", "link", "meta", "base", "form", "input", "button", "foreignobject"}
    VOID_TAGS = {"br", "hr"}
    GLOBAL_ATTRS = {"class", "title", "role", "aria-label"}
    VISUAL_ATTRS = {
        "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "d", "points", "fill", "fill-opacity",
        "stroke", "stroke-width", "stroke-dasharray", "stroke-linecap", "opacity", "text-anchor", "font-size", "font-family",
        "font-weight", "transform", "dominant-baseline", "viewbox", "preserveaspectratio", "width", "height", "xmlns",
        "colspan", "rowspan", "scope",
    }
    ATTR_CASE = {"viewbox": "viewBox", "preserveaspectratio": "preserveAspectRatio"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.output: List[str] = []
        self.drop_depth = 0
        self.open_tags: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if self.drop_depth:
            self.drop_depth += 1
            return
        if tag in self.BLOCKED_TAGS:
            self.drop_depth = 1
            return
        if tag not in self.SAFE_TAGS:
            return
        safe_attrs = []
        for name, value in attrs:
            name = name.lower()
            if name.startswith("on") or name not in self.GLOBAL_ATTRS | self.VISUAL_ATTRS or value is None:
                continue
            safe_attrs.append(f' {self.ATTR_CASE.get(name, name)}="{html.escape(value, quote=True)}"')
        self.output.append(f"<{tag}{''.join(safe_attrs)}>")
        if tag not in self.VOID_TAGS:
            self.open_tags.append(tag)

    def handle_startendtag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.drop_depth:
            self.drop_depth -= 1
            return
        if tag in self.SAFE_TAGS and tag in self.open_tags:
            self.output.append(f"</{tag}>")
            self.open_tags.remove(tag)

    def handle_data(self, data: str) -> None:
        if not self.drop_depth:
            self.output.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if not self.drop_depth:
            self.output.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self.drop_depth:
            self.output.append(f"&#{name};")

    def result(self) -> str:
        return "".join(self.output).strip()


class PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def value(self) -> str:
        return " ".join(" ".join(self.parts).split())


def sanitize_visual_html(fragment: str) -> str:
    sanitizer = SafeVisualHTML()
    sanitizer.feed(fragment)
    sanitizer.close()
    return sanitizer.result()


def question_summary(fragment: str) -> str:
    parser = PlainText()
    parser.feed(fragment)
    parser.close()
    return parser.value()[:300]


def parse_question_bank(html_source: str, answer_key_source: str) -> tuple[str, List[Dict[str, Any]]]:
    parser = QuestionSectionParser()
    parser.feed(html_source)
    parser.close()
    if parser.errors:
        raise HTTPException(400, "; ".join(parser.errors))
    if not parser.fragments:
        raise HTTPException(400, "The HTML file must contain <section data-question-key=\"...\"> blocks.")
    try:
        answer_key = json.loads(answer_key_source)
    except json.JSONDecodeError as error:
        raise HTTPException(400, f"The answer key is not valid JSON: {error.msg}.")
    if not isinstance(answer_key, dict) or not isinstance(answer_key.get("questions"), list):
        raise HTTPException(400, "The answer-key JSON must contain a 'questions' array.")
    bank_name = str(answer_key.get("bank_name", "Untitled question bank")).strip()
    if not bank_name:
        raise HTTPException(400, "The answer key needs a non-empty bank_name.")
    records: Dict[str, Dict[str, Any]] = {}
    for entry in answer_key["questions"]:
        if not isinstance(entry, dict):
            raise HTTPException(400, "Every answer-key question must be an object.")
        key = str(entry.get("key", "")).strip()
        if not key or key in records:
            raise HTTPException(400, "Every answer-key question needs a unique key.")
        category = entry.get("category")
        if category not in CATEGORIES:
            raise HTTPException(400, f"Question {key!r} has an invalid category.")
        difficulty = entry.get("difficulty", "Medium")
        if difficulty not in {"Easy", "Medium", "Hard"}:
            raise HTTPException(400, f"Question {key!r} has an invalid difficulty.")
        options = entry.get("options")
        if not isinstance(options, dict) or set(options) != {"A", "B", "C", "D"} or not all(isinstance(options[value], str) and options[value].strip() for value in options):
            raise HTTPException(400, f"Question {key!r} needs non-empty A, B, C and D options.")
        correct = entry.get("correct_answer")
        if correct not in {"A", "B", "C", "D"}:
            raise HTTPException(400, f"Question {key!r} needs correct_answer A, B, C or D.")
        records[key] = {"category": category, "difficulty": difficulty, "options": options, "correct_answer": correct, "explanation": str(entry.get("explanation", "")).strip()}
    if set(records) != set(parser.fragments):
        missing = sorted(set(parser.fragments) - set(records))
        extra = sorted(set(records) - set(parser.fragments))
        details = []
        if missing: details.append("missing answer-key entries: " + ", ".join(missing))
        if extra: details.append("answer-key entries without HTML blocks: " + ", ".join(extra))
        raise HTTPException(400, "; ".join(details))
    parsed = []
    for key, source in parser.fragments.items():
        visual_html = sanitize_visual_html("".join(source))
        summary = question_summary(visual_html)
        if not summary:
            raise HTTPException(400, f"Question {key!r} has no readable question text.")
        parsed.append({"key": key, "question_html": visual_html, "question_text": summary, **records[key]})
    return bank_name, parsed


def ensure_schema() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    BACKUP_DIR.mkdir(exist_ok=True)
    QUESTION_BANKS_DIR.mkdir(exist_ok=True)
    with db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS students (
              student_id TEXT PRIMARY KEY, name TEXT NOT NULL, password_hash TEXT NOT NULL,
              class TEXT NOT NULL, section TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admins (
              username TEXT PRIMARY KEY, name TEXT NOT NULL, password_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS question_banks (
              bank_id INTEGER PRIMARY KEY AUTOINCREMENT, bank_name TEXT NOT NULL,
              source_html_filename TEXT NOT NULL, answer_key_filename TEXT NOT NULL,
              imported_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS questions (
              question_id INTEGER PRIMARY KEY AUTOINCREMENT, question_text TEXT NOT NULL,
              category TEXT NOT NULL, difficulty TEXT NOT NULL, option_a TEXT NOT NULL,
              option_b TEXT NOT NULL, option_c TEXT NOT NULL, option_d TEXT NOT NULL,
              correct_answer TEXT NOT NULL, explanation TEXT DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
              bank_id INTEGER, question_html TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tests (
              test_id INTEGER PRIMARY KEY AUTOINCREMENT, test_name TEXT NOT NULL,
              composition TEXT NOT NULL, bank_id INTEGER, created_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS attempts (
              attempt_id TEXT PRIMARY KEY, student_id TEXT NOT NULL, test_id INTEGER NOT NULL,
              started_at TEXT NOT NULL, submitted_at TEXT, status TEXT NOT NULL DEFAULT 'in_progress',
              total_questions INTEGER NOT NULL, attempted INTEGER NOT NULL DEFAULT 0,
              correct INTEGER NOT NULL DEFAULT 0, score INTEGER NOT NULL DEFAULT 0,
              percentage REAL NOT NULL DEFAULT 0,
              FOREIGN KEY(student_id) REFERENCES students(student_id),
              FOREIGN KEY(test_id) REFERENCES tests(test_id)
            );
            CREATE TABLE IF NOT EXISTS responses (
              response_id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT NOT NULL,
              question_id INTEGER NOT NULL, selected_answer TEXT, correct INTEGER,
              category TEXT NOT NULL, question_order INTEGER NOT NULL,
              FOREIGN KEY(attempt_id) REFERENCES attempts(attempt_id),
              FOREIGN KEY(question_id) REFERENCES questions(question_id),
              UNIQUE(attempt_id, question_id)
            );
            CREATE INDEX IF NOT EXISTS idx_attempts_student ON attempts(student_id);
            CREATE INDEX IF NOT EXISTS idx_responses_attempt ON responses(attempt_id);
            """
        )
        ensure_column(connection, "questions", "bank_id INTEGER")
        ensure_column(connection, "questions", "question_html TEXT NOT NULL DEFAULT ''")
        ensure_column(connection, "tests", "bank_id INTEGER")
        ensure_column(connection, "tests", "launched INTEGER NOT NULL DEFAULT 0")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_questions_bank ON questions(bank_id)")


def seed_data() -> None:
    with db() as connection:
        if not connection.execute("SELECT 1 FROM admins LIMIT 1").fetchone():
            connection.execute("INSERT INTO admins VALUES (?, ?, ?)", ("faculty", "Dr. Meera Rao", hash_password("faculty123")))
        if not connection.execute("SELECT 1 FROM students LIMIT 1").fetchone():
            connection.execute(
                "INSERT INTO students VALUES (?, ?, ?, ?, ?, ?)",
                ("1KS23AI042", "Aarav Sharma", hash_password("student123"), "AI & DS", "A", now()),
            )
            connection.execute(
                "INSERT INTO students VALUES (?, ?, ?, ?, ?, ?)",
                ("1KS23AI018", "Nisha Patel", hash_password("student123"), "AI & DS", "A", now()),
            )
        starter_bank = connection.execute("SELECT bank_id FROM question_banks WHERE bank_name = ?", ("Starter general aptitude",)).fetchone()
        if not starter_bank:
            cursor = connection.execute(
                "INSERT INTO question_banks (bank_name, source_html_filename, answer_key_filename, imported_at) VALUES (?, ?, ?, ?)",
                ("Starter general aptitude", "seeded-content", "seeded-answer-key", now()),
            )
            starter_bank_id = cursor.lastrowid
        else:
            starter_bank_id = starter_bank["bank_id"]
        if not connection.execute("SELECT 1 FROM questions LIMIT 1").fetchone():
            for item in QUESTION_SEEDS:
                connection.execute(
                    """INSERT INTO questions
                    (question_text, category, difficulty, option_a, option_b, option_c, option_d, correct_answer, explanation, bank_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (*item, starter_bank_id, now()),
                )
        connection.execute("UPDATE questions SET bank_id = ? WHERE bank_id IS NULL", (starter_bank_id,))
        if not connection.execute("SELECT 1 FROM tests LIMIT 1").fetchone():
            connection.execute(
                "INSERT INTO tests (test_name, composition, bank_id, created_at) VALUES (?, ?, ?, ?)",
                ("Placement Readiness · Set 01", json.dumps(DEFAULT_COMPOSITION), starter_bank_id, now()),
            )
        connection.execute("UPDATE tests SET bank_id = ? WHERE bank_id IS NULL", (starter_bank_id,))


def copy_starter_question_files() -> None:
    """Place the visual example pair in the writable folder without overwriting faculty files."""
    QUESTION_BANKS_DIR.mkdir(exist_ok=True)
    for source in TEMPLATE_DIR.glob("*"):
        if source.is_file() and source.suffix.lower() in {".html", ".htm", ".json"}:
            destination = QUESTION_BANKS_DIR / source.name
            if not destination.exists():
                shutil.copy2(source, destination)


def staged_question_bank_pairs() -> List[Dict[str, Any]]:
    """Find pairs of files manually copied into the server's Question Banks folder."""
    QUESTION_BANKS_DIR.mkdir(exist_ok=True)
    by_stem: Dict[str, Dict[str, str]] = {}
    for candidate in QUESTION_BANKS_DIR.iterdir():
        if not candidate.is_file():
            continue
        extension = candidate.suffix.lower()
        if extension in {".html", ".htm"}:
            by_stem.setdefault(candidate.stem, {})["html_filename"] = candidate.name
        elif extension == ".json":
            by_stem.setdefault(candidate.stem, {})["answer_key_filename"] = candidate.name
    return [
        {"stem": stem, "ready": "html_filename" in pair and "answer_key_filename" in pair, **pair}
        for stem, pair in sorted(by_stem.items(), key=lambda item: item[0].lower())
    ]


def read_staged_pair(payload: FolderImportPayload) -> tuple[str, str]:
    names = (Path(payload.html_filename).name, Path(payload.answer_key_filename).name)
    if names != (payload.html_filename, payload.answer_key_filename):
        raise HTTPException(400, "Only files in the Question Banks folder can be imported.")
    html_path, answer_path = QUESTION_BANKS_DIR / names[0], QUESTION_BANKS_DIR / names[1]
    if not html_path.is_file() or html_path.suffix.lower() not in {".html", ".htm"}:
        raise HTTPException(404, "The selected HTML file is no longer in the Question Banks folder.")
    if not answer_path.is_file() or answer_path.suffix.lower() != ".json":
        raise HTTPException(404, "The selected answer-key JSON file is no longer in the Question Banks folder.")
    try:
        return html_path.read_text(encoding="utf-8-sig"), answer_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "Use UTF-8 encoded files for the question-bank pair.")


def save_question_bank(bank_name: str, questions: List[Dict[str, Any]], html_name: str, answer_name: str) -> Dict[str, Any]:
    with db() as connection:
        if connection.execute("SELECT 1 FROM question_banks WHERE bank_name = ?", (bank_name,)).fetchone():
            raise HTTPException(409, "A question bank with this name already exists. Use a versioned bank_name to import another copy.")
        cursor = connection.execute(
            "INSERT INTO question_banks (bank_name, source_html_filename, answer_key_filename, imported_at) VALUES (?, ?, ?, ?)",
            (bank_name, html_name, answer_name, now()),
        )
        bank_id = cursor.lastrowid
        for question in questions:
            options = question["options"]
            connection.execute(
                """INSERT INTO questions
                (question_text, question_html, category, difficulty, option_a, option_b, option_c, option_d, correct_answer, explanation, bank_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (question["question_text"], question["question_html"], question["category"], question["difficulty"], options["A"], options["B"], options["C"], options["D"], question["correct_answer"], question["explanation"], bank_id, now()),
            )
    return {"imported": True, "bank_id": bank_id, "bank_name": bank_name, "question_count": len(questions)}


def require_user(request: Request, role: Optional[str] = None) -> Dict[str, str]:
    user = request.session.get("user")
    if not user or (role and user["role"] != role):
        raise HTTPException(401, "Please sign in to continue.")
    return user


def get_attempt(connection: sqlite3.Connection, attempt_id: str) -> sqlite3.Row:
    attempt = connection.execute("SELECT a.*, t.launched FROM attempts a JOIN tests t ON t.test_id = a.test_id WHERE a.attempt_id = ?", (attempt_id,)).fetchone()
    if not attempt:
        raise HTTPException(404, "Assessment attempt not found.")
    return attempt


def assert_student_attempt(connection: sqlite3.Connection, attempt_id: str, student_id: str) -> sqlite3.Row:
    attempt = get_attempt(connection, attempt_id)
    if attempt["student_id"] != student_id:
        raise HTTPException(403, "You can only access your own attempts.")
    return attempt


def serialize_attempt(connection: sqlite3.Connection, attempt: sqlite3.Row, include_answers: bool = False) -> Dict[str, Any]:
    include_answers = include_answers or feedback_allowed(attempt)
    response_rows = connection.execute(
        """SELECT r.question_order, r.selected_answer, r.category, q.question_id, q.question_text, q.question_html,
                  q.difficulty, q.option_a, q.option_b, q.option_c, q.option_d, q.explanation, q.correct_answer
           FROM responses r JOIN questions q ON q.question_id = r.question_id
           WHERE r.attempt_id = ? ORDER BY r.question_order""",
        (attempt["attempt_id"],),
    ).fetchall()
    questions = []
    for row in response_rows:
        question = {
            "question_id": row["question_id"], "question_text": row["question_text"], "question_html": row["question_html"],
            "category": row["category"], "difficulty": row["difficulty"],
            "options": {"A": row["option_a"], "B": row["option_b"], "C": row["option_c"], "D": row["option_d"]},
            "selected_answer": row["selected_answer"],
        }
        if include_answers:
            question.update({"correct_answer": row["correct_answer"], "explanation": row["explanation"]})
        questions.append(question)
    return {**dict(attempt), "feedback_allowed": feedback_allowed(attempt), "questions": questions}


def result_for_attempt(connection: sqlite3.Connection, attempt_id: str) -> Dict[str, Any]:
    attempt = get_attempt(connection, attempt_id)
    if not feedback_allowed(attempt):
        return {"attempt": {"attempt_id": attempt_id, "status": attempt["status"]}, "feedback_allowed": False}
    result_rows = connection.execute(
        """SELECT r.category, COUNT(*) AS total, SUM(r.selected_answer IS NOT NULL) AS attempted,
                  SUM(r.correct = 1) AS correct
           FROM responses r WHERE r.attempt_id = ? GROUP BY r.category ORDER BY r.category""",
        (attempt_id,),
    ).fetchall()
    categories = []
    for row in result_rows:
        total, attempted, correct = row["total"], row["attempted"] or 0, row["correct"] or 0
        categories.append({"category": row["category"], "total": total, "attempted": attempted, "correct": correct, "percentage": round(correct / total * 100, 1) if total else 0})
    return {
        "attempt": dict(attempt), "categories": categories,
        "incorrect": attempt["attempted"] - attempt["correct"],
        "unanswered": attempt["total_questions"] - attempt["attempted"],
    }


@app.on_event("startup")
def startup() -> None:
    ensure_schema()
    seed_data()
    copy_starter_question_files()


@app.get("/")
def home() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/me")
def me(request: Request) -> Dict[str, Any]:
    return {"user": request.session.get("user")}


@app.post("/api/login")
def login(payload: LoginPayload, request: Request) -> Dict[str, Any]:
    role = payload.role.lower()
    if role not in {"student", "admin"}:
        raise HTTPException(400, "Choose student or faculty access.")
    with db() as connection:
        if role == "student":
            account = connection.execute("SELECT * FROM students WHERE student_id = ?", (payload.identifier.strip().upper(),)).fetchone()
            user_id, label = "student_id", "name"
        else:
            account = connection.execute("SELECT * FROM admins WHERE username = ?", (payload.identifier.strip().lower(),)).fetchone()
            user_id, label = "username", "name"
        if not account or not check_password(payload.password, account["password_hash"]):
            raise HTTPException(401, "Incorrect ID or password.")
        request.session["user"] = {"role": role, "id": account[user_id], "name": account[label]}
    return {"user": request.session["user"]}


@app.post("/api/register")
def register(payload: RegistrationPayload) -> Dict[str, Any]:
    try:
        student = register_student(payload.student_id, payload.name, payload.student_class, payload.section, payload.password)
    except RegistrationError as error:
        raise HTTPException(400, str(error)) from error
    return {"registered": True, "student": student}


@app.post("/api/logout")
def logout(request: Request) -> Dict[str, bool]:
    request.session.clear()
    return {"ok": True}


@app.post("/api/admin/shutdown")
def shutdown_server(request: Request) -> Dict[str, bool]:
    require_user(request, "admin")
    threading.Timer(0.5, os._exit, args=(0,)).start()
    return {"stopping": True}


@app.get("/api/student/dashboard")
def student_dashboard(request: Request) -> Dict[str, Any]:
    user = require_user(request, "student")
    with db() as connection:
        student = connection.execute("SELECT student_id, name, class, section FROM students WHERE student_id = ?", (user["id"],)).fetchone()
        tests = student_available_tests(connection)
        active = connection.execute("SELECT * FROM attempts WHERE student_id = ? AND status = 'in_progress' ORDER BY started_at DESC LIMIT 1", (user["id"],)).fetchone()
        history = connection.execute(
            """SELECT a.attempt_id, a.score, a.total_questions, a.percentage, a.submitted_at, t.test_name
               FROM attempts a JOIN tests t ON t.test_id = a.test_id
               WHERE a.student_id = ? AND a.status = 'submitted' ORDER BY a.submitted_at DESC""", (user["id"],)
        ).fetchall()
        trend = connection.execute(
            """SELECT r.category, ROUND(AVG(r.correct) * 100, 1) AS percentage
               FROM responses r JOIN attempts a ON a.attempt_id = r.attempt_id
               WHERE a.student_id = ? AND a.status = 'submitted' GROUP BY r.category""", (user["id"],)
        ).fetchall()
    return {"student": dict(student), "tests": tests, "test": tests[0] if tests else None, "launched": bool(tests and tests[0]["launched"]), "active_attempt": dict(active) if active else None, "history": rows(history), "category_trend": rows(trend)}


@app.post("/api/tests/{test_id}/start")
def start_test(test_id: int, request: Request) -> Dict[str, Any]:
    user = require_user(request, "student")
    with db() as connection:
        existing = connection.execute("SELECT attempt_id FROM attempts WHERE student_id = ? AND test_id = ? AND status = 'in_progress'", (user["id"], test_id)).fetchone()
        if existing:
            return {"attempt_id": existing["attempt_id"], "resumed": True}
        test = connection.execute("SELECT * FROM tests WHERE test_id = ? AND active = 1", (test_id,)).fetchone()
        if not test:
            raise HTTPException(404, "This test is not currently available.")
        launched = connection.execute("SELECT 1 FROM tests WHERE active = 1 AND launched = 1 LIMIT 1").fetchone()
        if launched and not test["launched"]:
            raise HTTPException(409, "Faculty has launched another test. Please attempt the launched test.")
        composition = json.loads(test["composition"])
        selected: List[sqlite3.Row] = []
        for category, count in composition.items():
            pool = connection.execute("SELECT question_id FROM questions WHERE active = 1 AND category = ? AND bank_id = ?", (category, test["bank_id"])).fetchall()
            if len(pool) < count:
                raise HTTPException(400, f"Not enough active questions in {category}. Add at least {count} questions.")
            selected.extend(random.sample(pool, count))
        random.shuffle(selected)
        attempt_id = str(uuid.uuid4())
        connection.execute("INSERT INTO attempts (attempt_id, student_id, test_id, started_at, total_questions) VALUES (?, ?, ?, ?, ?)", (attempt_id, user["id"], test_id, now(), len(selected)))
        for index, question in enumerate(selected, start=1):
            category = connection.execute("SELECT category FROM questions WHERE question_id = ?", (question["question_id"],)).fetchone()["category"]
            connection.execute("INSERT INTO responses (attempt_id, question_id, category, question_order) VALUES (?, ?, ?, ?)", (attempt_id, question["question_id"], category, index))
    return {"attempt_id": attempt_id, "resumed": False}


@app.get("/api/attempts/{attempt_id}")
def get_student_attempt(attempt_id: str, request: Request) -> Dict[str, Any]:
    user = require_user(request, "student")
    with db() as connection:
        attempt = assert_student_attempt(connection, attempt_id, user["id"])
        return serialize_attempt(connection, attempt)


@app.put("/api/attempts/{attempt_id}/responses/{question_id}")
def save_answer(attempt_id: str, question_id: int, payload: AnswerPayload, request: Request) -> Dict[str, Any]:
    user = require_user(request, "student")
    with db() as connection:
        attempt = assert_student_attempt(connection, attempt_id, user["id"])
        if attempt["status"] != "in_progress":
            raise HTTPException(400, "Submitted tests cannot be changed.")
        response = connection.execute("SELECT 1 FROM responses WHERE attempt_id = ? AND question_id = ?", (attempt_id, question_id)).fetchone()
        if not response:
            raise HTTPException(404, "Question is not part of this assessment.")
        connection.execute("UPDATE responses SET selected_answer = ? WHERE attempt_id = ? AND question_id = ?", (payload.answer, attempt_id, question_id))
        attempted = connection.execute("SELECT COUNT(*) AS count FROM responses WHERE attempt_id = ? AND selected_answer IS NOT NULL", (attempt_id,)).fetchone()["count"]
        connection.execute("UPDATE attempts SET attempted = ? WHERE attempt_id = ?", (attempted, attempt_id))
        feedback = None
        if feedback_allowed(connection.execute("SELECT t.launched FROM tests t JOIN attempts a ON a.test_id = t.test_id WHERE a.attempt_id = ?", (attempt_id,)).fetchone()):
            question = connection.execute("SELECT correct_answer, explanation, option_a, option_b, option_c, option_d FROM questions WHERE question_id = ?", (question_id,)).fetchone()
            feedback = {"correct": payload.answer == question["correct_answer"], "correct_answer": question["correct_answer"], "explanation": question["explanation"], "option_explanations": {key: ("Correct answer." if key == question["correct_answer"] else "This option does not match the question's correct answer.") for key in "ABCD"}}
    return {"saved": True, "attempted": attempted, "feedback": feedback}


@app.post("/api/attempts/{attempt_id}/submit")
def submit_attempt(attempt_id: str, payload: SubmitPayload, request: Request) -> Dict[str, Any]:
    user = require_user(request, "student")
    with db() as connection:
        attempt = assert_student_attempt(connection, attempt_id, user["id"])
        if attempt["status"] == "submitted":
            return result_for_attempt(connection, attempt_id)
        unanswered = connection.execute("SELECT COUNT(*) AS count FROM responses WHERE attempt_id = ? AND selected_answer IS NULL", (attempt_id,)).fetchone()["count"]
        if unanswered and not payload.confirmed:
            return {"requires_confirmation": True, "unanswered": unanswered, "attempted": attempt["total_questions"] - unanswered}
        connection.execute(
            """UPDATE responses SET correct = CASE
               WHEN selected_answer IS NULL THEN 0
               WHEN selected_answer = (SELECT correct_answer FROM questions WHERE questions.question_id = responses.question_id) THEN 1
               ELSE 0 END WHERE attempt_id = ?""", (attempt_id,)
        )
        stats = connection.execute("SELECT COUNT(*) AS total, SUM(selected_answer IS NOT NULL) AS attempted, SUM(correct = 1) AS correct FROM responses WHERE attempt_id = ?", (attempt_id,)).fetchone()
        total, attempted, correct = stats["total"], stats["attempted"] or 0, stats["correct"] or 0
        connection.execute("UPDATE attempts SET submitted_at = ?, status = 'submitted', attempted = ?, correct = ?, score = ?, percentage = ? WHERE attempt_id = ?", (now(), attempted, correct, correct, round(correct / total * 100, 1), attempt_id))
        if not feedback_allowed(attempt):
            return {"submitted": True, "feedback_allowed": False}
        return result_for_attempt(connection, attempt_id)


@app.get("/api/attempts/{attempt_id}/result")
def get_result(attempt_id: str, request: Request) -> Dict[str, Any]:
    user = require_user(request, "student")
    with db() as connection:
        attempt = assert_student_attempt(connection, attempt_id, user["id"])
        if attempt["status"] != "submitted":
            raise HTTPException(400, "Submit the assessment to view results.")
        if not feedback_allowed(attempt):
            raise HTTPException(403, "Exam results are held by Faculty.")
        return result_for_attempt(connection, attempt_id)


@app.get("/api/admin/dashboard")
def admin_dashboard(request: Request) -> Dict[str, Any]:
    require_user(request, "admin")
    with db() as connection:
        totals = connection.execute("SELECT (SELECT COUNT(*) FROM students) AS students, (SELECT COUNT(*) FROM attempts WHERE status = 'submitted') AS completed, ROUND(COALESCE(AVG(percentage), 0), 1) AS average FROM attempts WHERE status = 'submitted'").fetchone()
        students = connection.execute(
            """SELECT s.student_id, s.name, s.class, s.section, COUNT(a.attempt_id) AS tests,
                      ROUND(COALESCE(AVG(a.percentage), 0), 1) AS average
               FROM students s LEFT JOIN attempts a ON a.student_id = s.student_id AND a.status = 'submitted'
               GROUP BY s.student_id ORDER BY s.name"""
        ).fetchall()
        category = connection.execute(
            """SELECT r.category, ROUND(AVG(r.correct) * 100, 1) AS percentage
               FROM responses r JOIN attempts a ON a.attempt_id = r.attempt_id
               WHERE a.status = 'submitted' GROUP BY r.category ORDER BY percentage DESC"""
        ).fetchall()
        recent = connection.execute(
            """SELECT a.attempt_id, s.name, s.student_id, t.test_name, a.score, a.total_questions, a.percentage, a.submitted_at
               FROM attempts a JOIN students s ON s.student_id = a.student_id JOIN tests t ON t.test_id = a.test_id
               WHERE a.status = 'submitted' ORDER BY a.submitted_at DESC LIMIT 10"""
        ).fetchall()
    return {"totals": dict(totals), "students": rows(students), "category_performance": rows(category), "recent_attempts": rows(recent)}


@app.get("/api/admin/students")
def list_students(request: Request) -> Dict[str, Any]:
    require_user(request, "admin")
    with db() as connection:
        return {"students": rows(connection.execute("SELECT student_id, name, class, section, created_at FROM students ORDER BY name").fetchall())}


@app.post("/api/admin/students")
def create_student(payload: StudentPayload, request: Request) -> Dict[str, Any]:
    require_user(request, "admin")
    with db() as connection:
        try:
            connection.execute("INSERT INTO students VALUES (?, ?, ?, ?, ?, ?)", (payload.student_id.strip().upper(), payload.name.strip(), hash_password(payload.password), payload.student_class.strip(), payload.section.strip(), now()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "That Student ID already exists.")
    return {"created": True}


@app.delete("/api/admin/students/{student_id}")
def remove_student(student_id: str, request: Request) -> Dict[str, Any]:
    require_user(request, "admin")
    try:
        return {"deleted": delete_student(student_id)}
    except RegistrationError as error:
        raise HTTPException(404, str(error)) from error


@app.get("/api/admin/questions")
def list_questions(request: Request) -> Dict[str, Any]:
    require_user(request, "admin")
    with db() as connection:
        return {"questions": rows(connection.execute(
            """SELECT q.question_id, q.question_text, q.category, q.difficulty, q.active,
                      q.question_html != '' AS has_visual, b.bank_name
               FROM questions q LEFT JOIN question_banks b ON b.bank_id = q.bank_id
               ORDER BY b.imported_at DESC, q.category, q.question_id"""
        ).fetchall())}


@app.get("/api/admin/question-banks")
def list_question_banks(request: Request) -> Dict[str, Any]:
    require_user(request, "admin")
    with db() as connection:
        banks = connection.execute(
            """SELECT b.bank_id, b.bank_name, b.source_html_filename, b.answer_key_filename, b.imported_at,
                      COUNT(q.question_id) AS question_count,
                      SUM(q.question_html != '') AS visual_question_count
               FROM question_banks b LEFT JOIN questions q ON q.bank_id = b.bank_id
               GROUP BY b.bank_id ORDER BY b.imported_at DESC"""
        ).fetchall()
    return {"banks": rows(banks)}


@app.get("/api/admin/question-banks/folder")
def question_bank_folder(request: Request) -> Dict[str, str]:
    require_user(request, "admin")
    return {"path": str(QUESTION_BANKS_DIR)}


@app.get("/api/admin/question-banks/staged")
def list_staged_question_banks(request: Request) -> Dict[str, Any]:
    require_user(request, "admin")
    return {"pairs": staged_question_bank_pairs()}


@app.post("/api/admin/question-banks/import")
async def import_question_bank(
    request: Request,
    html_file: UploadFile = File(...),
    answer_key_file: UploadFile = File(...),
) -> Dict[str, Any]:
    require_user(request, "admin")
    html_name = Path(html_file.filename or "").name
    answer_name = Path(answer_key_file.filename or "").name
    if not html_name.lower().endswith((".html", ".htm")):
        raise HTTPException(400, "The first file must be an HTML document (.html or .htm).")
    if not answer_name.lower().endswith(".json"):
        raise HTTPException(400, "The second file must be an answer-key JSON document (.json).")
    html_bytes, answer_bytes = await html_file.read(), await answer_key_file.read()
    if not html_bytes or not answer_bytes:
        raise HTTPException(400, "Both the HTML file and the answer-key JSON file are required.")
    if len(html_bytes) > 2_000_000 or len(answer_bytes) > 1_000_000:
        raise HTTPException(413, "The HTML file must be under 2 MB and the answer key under 1 MB.")
    try:
        html_source, answer_key_source = html_bytes.decode("utf-8-sig"), answer_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "Use UTF-8 encoded files for the question-bank pair.")
    bank_name, questions = parse_question_bank(html_source, answer_key_source)
    return save_question_bank(bank_name, questions, html_name, answer_name)


@app.post("/api/admin/question-banks/import-from-folder")
def import_question_bank_from_folder(payload: FolderImportPayload, request: Request) -> Dict[str, Any]:
    require_user(request, "admin")
    html_source, answer_key_source = read_staged_pair(payload)
    bank_name, questions = parse_question_bank(html_source, answer_key_source)
    return save_question_bank(bank_name, questions, payload.html_filename, payload.answer_key_filename)


@app.get("/api/admin/question-bank-template/html")
def download_html_template(request: Request) -> FileResponse:
    require_user(request, "admin")
    return FileResponse(TEMPLATE_DIR / "visual-data-interpretation.html", media_type="text/html", filename="visual-data-interpretation.html")


@app.get("/api/admin/question-bank-template/answer-key")
def download_answer_key_template(request: Request) -> FileResponse:
    require_user(request, "admin")
    return FileResponse(TEMPLATE_DIR / "visual-data-interpretation.json", media_type="application/json", filename="visual-data-interpretation.json")


@app.patch("/api/admin/questions/{question_id}/active")
def toggle_question(question_id: int, request: Request) -> Dict[str, Any]:
    require_user(request, "admin")
    with db() as connection:
        question = connection.execute("SELECT active FROM questions WHERE question_id = ?", (question_id,)).fetchone()
        if not question:
            raise HTTPException(404, "Question not found.")
        connection.execute("UPDATE questions SET active = ? WHERE question_id = ?", (0 if question["active"] else 1, question_id))
    return {"updated": True}


@app.get("/api/admin/tests")
def list_tests(request: Request) -> Dict[str, Any]:
    require_user(request, "admin")
    with db() as connection:
        tests = rows(connection.execute(
            """SELECT t.*, b.bank_name FROM tests t
               LEFT JOIN question_banks b ON b.bank_id = t.bank_id ORDER BY t.created_at DESC"""
        ).fetchall())
        for test in tests:
            test["composition"] = json.loads(test["composition"])
        banks = rows(connection.execute(
            """SELECT b.bank_id, b.bank_name, COUNT(q.question_id) AS question_count
               FROM question_banks b LEFT JOIN questions q ON q.bank_id = b.bank_id AND q.active = 1
               GROUP BY b.bank_id ORDER BY b.imported_at DESC"""
        ).fetchall())
        return {"tests": tests, "categories": CATEGORIES, "banks": banks}


@app.post("/api/admin/tests")
def create_test(payload: TestPayload, request: Request) -> Dict[str, Any]:
    require_user(request, "admin")
    clean = {category: int(payload.composition.get(category, 0)) for category in CATEGORIES}
    if not payload.test_name.strip() or sum(clean.values()) == 0 or any(value < 0 for value in clean.values()):
        raise HTTPException(400, "Provide a name and at least one question.")
    with db() as connection:
        bank = connection.execute("SELECT bank_name FROM question_banks WHERE bank_id = ?", (payload.bank_id,)).fetchone()
        if not bank:
            raise HTTPException(404, "Choose an imported question bank.")
        availability = {
            row["category"]: row["count"]
            for row in connection.execute(
                "SELECT category, COUNT(*) AS count FROM questions WHERE bank_id = ? AND active = 1 GROUP BY category", (payload.bank_id,)
            ).fetchall()
        }
        shortages = [f"{category}: need {count}, have {availability.get(category, 0)}" for category, count in clean.items() if count > availability.get(category, 0)]
        if shortages:
            raise HTTPException(400, "This bank does not have enough active questions — " + "; ".join(shortages) + ".")
        connection.execute(
            "INSERT INTO tests (test_name, composition, bank_id, created_at) VALUES (?, ?, ?, ?)",
            (payload.test_name.strip(), json.dumps(clean), payload.bank_id, now()),
        )
    return {"created": True}


@app.post("/api/admin/tests/{test_id}/launch")
def launch_test(test_id: int, request: Request) -> Dict[str, bool]:
    require_user(request, "admin")
    with db() as connection:
        if not connection.execute("SELECT 1 FROM tests WHERE test_id = ? AND active = 1", (test_id,)).fetchone():
            raise HTTPException(404, "Test not found.")
        connection.execute("UPDATE tests SET launched = 0")
        connection.execute("UPDATE tests SET launched = 1 WHERE test_id = ?", (test_id,))
    return {"launched": True}


@app.post("/api/admin/tests/{test_id}/close")
def close_test(test_id: int, request: Request) -> Dict[str, bool]:
    require_user(request, "admin")
    with db() as connection:
        connection.execute("UPDATE tests SET launched = 0 WHERE test_id = ?", (test_id,))
    return {"closed": True}


@app.get("/api/admin/export")
def export_results(request: Request) -> StreamingResponse:
    require_user(request, "admin")
    with db() as connection:
        result = connection.execute(
            """SELECT s.student_id, s.name, t.test_name, a.submitted_at, a.score, a.total_questions, a.percentage,
                 ROUND(AVG(CASE WHEN r.category = 'Quantitative Aptitude' THEN r.correct END) * 100, 1) AS quantitative,
                 ROUND(AVG(CASE WHEN r.category = 'Logical Reasoning' THEN r.correct END) * 100, 1) AS logical,
                 ROUND(AVG(CASE WHEN r.category = 'Data Interpretation' THEN r.correct END) * 100, 1) AS data_interpretation,
                 ROUND(AVG(CASE WHEN r.category = 'Verbal Ability' THEN r.correct END) * 100, 1) AS verbal,
                 ROUND(AVG(CASE WHEN r.category = 'Coding / Computational Thinking' THEN r.correct END) * 100, 1) AS coding
               FROM attempts a JOIN students s ON s.student_id = a.student_id JOIN tests t ON t.test_id = a.test_id
               JOIN responses r ON r.attempt_id = a.attempt_id WHERE a.status = 'submitted' GROUP BY a.attempt_id ORDER BY a.submitted_at DESC"""
        ).fetchall()
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["Student ID", "Student Name", "Test", "Date", "Overall Score", "Total Questions", "Percentage", "Quantitative", "Logical Reasoning", "Data Interpretation", "Verbal Ability", "Coding"])
    writer.writeheader()
    for item in result:
        writer.writerow({"Student ID": item["student_id"], "Student Name": item["name"], "Test": item["test_name"], "Date": item["submitted_at"], "Overall Score": item["score"], "Total Questions": item["total_questions"], "Percentage": item["percentage"], "Quantitative": item["quantitative"], "Logical Reasoning": item["logical"], "Data Interpretation": item["data_interpretation"], "Verbal Ability": item["verbal"], "Coding": item["coding"]})
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=aptitude-results.csv"})


@app.get("/api/admin/backup")
def download_backup(request: Request) -> FileResponse:
    require_user(request, "admin")
    filename = f"backup_{datetime.now().strftime('%Y_%m_%d_%H%M%S')}.db"
    backup_path = BACKUP_DIR / filename
    with db() as connection:
        destination = sqlite3.connect(backup_path)
        connection.backup(destination)
        destination.close()
    return FileResponse(backup_path, media_type="application/x-sqlite3", filename=filename)


QUESTION_SEEDS = [
    # text, category, difficulty, A, B, C, D, correct, explanation
    ("What is 15% of 240?", CATEGORIES[0], "Easy", "30", "36", "42", "48", "B", "15% of 240 is 0.15 × 240 = 36."),
    ("A train travels 60 km in 45 minutes. What is its average speed?", CATEGORIES[0], "Easy", "60 km/h", "70 km/h", "80 km/h", "90 km/h", "C", "45 minutes is 0.75 hours; 60 ÷ 0.75 = 80."),
    ("Find the simple interest on ₹1,000 at 5% per annum for 2 years.", CATEGORIES[0], "Easy", "₹50", "₹100", "₹150", "₹200", "B", "SI = PRT/100 = 1000 × 5 × 2 / 100."),
    ("The ratio of boys to girls is 2:3. If there are 50 students, how many are boys?", CATEGORIES[0], "Medium", "20", "25", "30", "35", "A", "Two of five equal parts are boys: 2/5 × 50 = 20."),
    ("What is the average of 12, 16 and 20?", CATEGORIES[0], "Easy", "14", "15", "16", "17", "C", "(12 + 16 + 20) ÷ 3 = 16."),
    ("A shirt marked ₹500 is sold at a 20% discount. What is the sale price?", CATEGORIES[0], "Easy", "₹350", "₹380", "₹400", "₹420", "C", "20% of 500 is 100; 500 − 100 = 400."),
    ("What is the perimeter of a square with side 12 cm?", CATEGORIES[0], "Easy", "24 cm", "36 cm", "48 cm", "144 cm", "C", "Perimeter of a square is 4 × side."),
    ("If x + 7 = 19, what is x?", CATEGORIES[0], "Easy", "10", "11", "12", "13", "C", "Subtract 7 from both sides."),
    ("Find the next number: 2, 6, 12, 20, __.", CATEGORIES[1], "Medium", "28", "30", "32", "34", "B", "Differences are +4, +6, +8, so next is +10."),
    ("All coders are logical. Ria is a coder. What must be true?", CATEGORIES[1], "Easy", "Ria is logical", "All logical people are coders", "Ria is not logical", "No conclusion can be made", "A", "If all coders are logical, every coder including Ria is logical."),
    ("Today is Wednesday. What day will it be after 10 days?", CATEGORIES[1], "Easy", "Friday", "Saturday", "Sunday", "Monday", "B", "10 days after Wednesday is Saturday."),
    ("A person walks 4 km north and then 3 km east. In which direction is the person from the start?", CATEGORIES[1], "Easy", "North-west", "North-east", "South-east", "South-west", "B", "Moving north then east places the person north-east of the start."),
    ("Choose the odd one out: Triangle, Square, Circle, Rectangle.", CATEGORIES[1], "Easy", "Triangle", "Square", "Circle", "Rectangle", "C", "A circle has no sides; the others are polygons."),
    ("If CAT is coded as DBU, how is DOG coded?", CATEGORIES[1], "Medium", "EPH", "EOH", "DPH", "FPH", "A", "Each letter advances by one: D→E, O→P, G→H."),
    ("Five friends A, B, C, D and E sit in a row. C is between B and D. Which statement can be true?", CATEGORIES[1], "Medium", "B, C, D are consecutive", "C is at both ends", "B and D occupy the same seat", "E is between B and C always", "A", "C can be directly between B and D."),
    ("A is taller than B, and B is taller than C. Who is shortest?", CATEGORIES[1], "Easy", "A", "B", "C", "Cannot say", "C", "The order is A > B > C."),
    ("A class has 40 students. 18 chose Python, 12 chose Java and the rest chose C. How many chose C?", CATEGORIES[2], "Easy", "8", "10", "12", "14", "B", "40 − 18 − 12 = 10."),
    ("Monthly sales were 120, 150, 180 and 150 units. What is the total sales figure?", CATEGORIES[2], "Easy", "540", "570", "600", "630", "C", "120 + 150 + 180 + 150 = 600."),
    ("A survey shows 30% prefer tea, 45% coffee and 25% juice. Which is most preferred?", CATEGORIES[2], "Easy", "Tea", "Coffee", "Juice", "All equal", "B", "45% is the largest value."),
    ("The marks are 65, 70, 80, 75 and 60. What is the highest mark?", CATEGORIES[2], "Easy", "65", "70", "75", "80", "D", "80 is the greatest listed mark."),
    ("A shop's revenue rose from ₹80,000 to ₹100,000. What is the percentage increase?", CATEGORIES[2], "Medium", "20%", "25%", "30%", "40%", "B", "Increase is 20,000 over 80,000, or 25%."),
    ("In a pie chart, a sector of 90° represents what fraction of the whole?", CATEGORIES[2], "Easy", "1/2", "1/3", "1/4", "1/5", "C", "90° out of 360° is one quarter."),
    ("The attendance for three days is 42, 39 and 45. What is the average attendance?", CATEGORIES[2], "Medium", "40", "41", "42", "43", "C", "(42 + 39 + 45) ÷ 3 = 42."),
    ("A table has 250 entries, of which 40 are incomplete. What percentage is complete?", CATEGORIES[2], "Medium", "76%", "80%", "84%", "86%", "C", "210 complete out of 250 is 84%."),
    ("Choose the word closest in meaning to 'diligent'.", CATEGORIES[3], "Easy", "Careless", "Hardworking", "Noisy", "Uncertain", "B", "Diligent means showing careful and persistent effort."),
    ("Choose the opposite of 'scarce'.", CATEGORIES[3], "Easy", "Rare", "Limited", "Abundant", "Small", "C", "Abundant means plentiful."),
    ("Select the grammatically correct sentence.", CATEGORIES[3], "Easy", "She do not like tea.", "She does not likes tea.", "She does not like tea.", "She not like tea.", "C", "With 'does', use the base form 'like'."),
    ("Complete the sentence: Neither the teacher nor the students ___ late.", CATEGORIES[3], "Medium", "was", "were", "is", "has", "B", "The verb agrees with the nearer plural subject, students."),
    ("What does the idiom 'break the ice' mean?", CATEGORIES[3], "Medium", "To end a friendship", "To begin a friendly conversation", "To become angry", "To stop working", "B", "It means to make people feel more comfortable at the start."),
    ("Choose the correctly spelled word.", CATEGORIES[3], "Easy", "Accomodation", "Acommodation", "Accommodation", "Accommadation", "C", "Accommodation has double c and double m."),
    ("Which word best completes: 'The lecture was ___, so everyone understood it.'", CATEGORIES[3], "Easy", "confusing", "clear", "late", "silent", "B", "A clear lecture is easy to understand."),
    ("Choose the best concise rewrite: 'Due to the fact that it rained, the match stopped.'", CATEGORIES[3], "Medium", "It rained, the match stopped.", "Because it rained, the match stopped.", "The match stopped due rain.", "Rain was causing the match." , "B", "'Because' is the concise, grammatical option."),
    ("What is printed? x = 5; x = x + 3; print(x)", CATEGORIES[4], "Easy", "5", "8", "10", "53", "B", "The assignment updates x from 5 to 8."),
    ("What is the time complexity of binary search on a sorted array?", CATEGORIES[4], "Medium", "O(1)", "O(log n)", "O(n)", "O(n²)", "B", "Binary search halves the remaining range each step."),
    ("Which data structure follows Last In, First Out (LIFO)?", CATEGORIES[4], "Easy", "Queue", "Stack", "Tree", "Graph", "B", "A stack removes the most recently added item first."),
    ("What does the condition i % 2 == 0 check?", CATEGORIES[4], "Easy", "i is positive", "i is prime", "i is even", "i is zero", "C", "An even integer leaves remainder 0 when divided by 2."),
    ("How many times does this loop run? for i in range(3): print(i)", CATEGORIES[4], "Easy", "2", "3", "4", "Infinitely", "B", "range(3) provides 0, 1 and 2."),
    ("Which SQL command retrieves data from a table?", CATEGORIES[4], "Easy", "SELECT", "INSERT", "UPDATE", "DELETE", "A", "SELECT reads rows from a table."),
    ("What is the output of: len('CODE')?", CATEGORIES[4], "Easy", "3", "4", "5", "Error", "B", "The word CODE contains four characters."),
    ("Which practice makes a function easier to test?", CATEGORIES[4], "Medium", "Give it many unrelated jobs", "Use clear inputs and one responsibility", "Avoid returning values", "Hide all errors", "B", "Small, focused functions are easier to test."),
]


def server_is_already_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=0.3):
            return True
    except OSError:
        return False


def open_local_browser() -> None:
    webbrowser.open(SERVER_URL, new=1)


if __name__ == "__main__":
    if server_is_already_running():
        open_local_browser()
    else:
        threading.Timer(1.1, open_local_browser).start()
        import uvicorn

        uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning", use_colors=False)
