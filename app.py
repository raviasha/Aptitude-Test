"""Local-LAN Student Aptitude Assessment MVP.

Run with: uvicorn app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import math
import os
import random
import re
import shutil
import socket
import sqlite3
import sys
import threading
import unicodedata
import uuid
import webbrowser
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
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
LEGACY_OPTION_KEYS = ("A", "B", "C", "D")
SUPPORTED_OPTION_KEYS = (*LEGACY_OPTION_KEYS, "E")
UNCATEGORIZED_CHAPTER = "Uncategorized"
MAX_ASSESSMENT_QUESTIONS = 500
MAX_PRACTICE_QUESTIONS = 100
QUESTION_DIFFICULTIES = ("Easy", "Medium", "Hard")
SECONDS_PER_FACULTY_QUESTION = 60
EXAM_VIOLATION_LABELS = {
    "fullscreen_exit": "Exited full-screen mode",
    "focus_lost": "Changed tab, window, or minimized the exam",
    "copy": "Attempted to copy exam content",
    "cut": "Attempted to cut exam content",
    "paste": "Attempted to paste into the exam",
    "context_menu": "Attempted to open the browser context menu",
}
MAX_LEGACY_FILE_BYTES = 25_000_000
MAX_PACKAGE_BYTES = 50_000_000
MAX_PACKAGE_UNPACKED_BYTES = 150_000_000
MAX_PACKAGE_FILES = 10_000
ALLOWED_ASSET_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".svg"}

SOLUTION_STEP_OVERRIDES = {
    "The total amount spent by the family on Groceries, Entertainment and Investments together forms approximately what percent of the amount spent on Commuting ?": [
        "Groceries, Entertainment and Investments = (23% + 10% + 15%) × ₹45,800 = 48% × ₹45,800 = ₹21,984.",
        "Commuting = 22% × ₹45,800 = ₹10,076.",
        "Required percentage = (₹21,984 ÷ ₹10,076) × 100 = 218.18% ≈ 218%. Therefore, option E is correct.",
    ],
    "If the imports in 2008 was ₹ 250 crores and the total exports in the years 2008 and 2009 together was ₹ 500 crores, then the imports in 2009 was": [
        "The graph shows imports are 125% of exports in 2008. So ₹250 crores = 125% of exports, and exports in 2008 = ₹250 ÷ 1.25 = ₹200 crores.",
        "Total exports in 2008 and 2009 are ₹500 crores. Therefore, exports in 2009 = ₹500 − ₹200 = ₹300 crores.",
        "Imports in 2009 are 140% of exports. So imports = 1.40 × ₹300 = ₹420 crores. Therefore, option D is correct.",
    ],
}

SOLUTION_REVIEW_NOTICE = [
    "This source calculation could not be displayed reliably because its PDF formula layout was damaged during import. The correct answer is shown above; the detailed solution is awaiting faculty verification."
]

app = FastAPI(title="Aptitude Lab")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "replace-this-before-production"), https_only=False, same_site="lax")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class LoginPayload(BaseModel):
    identifier: str
    password: str
    role: str = "student"


class AnswerPayload(BaseModel):
    answer: Optional[str] = Field(None, pattern="^[A-E]$")


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


class SelectionRule(BaseModel):
    category: str
    chapter: str = UNCATEGORIZED_CHAPTER
    quantity: int = Field(ge=0, le=MAX_ASSESSMENT_QUESTIONS)


class TestPayload(BaseModel):
    test_name: str
    bank_id: int
    selection_rules: List[SelectionRule] = Field(default_factory=list)
    composition: Dict[str, int] = Field(default_factory=dict)
    difficulties: List[str] = Field(default_factory=lambda: list(QUESTION_DIFFICULTIES))


class PracticePayload(BaseModel):
    bank_id: int
    selection_rules: List[SelectionRule]
    difficulties: List[str] = Field(default_factory=lambda: list(QUESTION_DIFFICULTIES))


class ExamViolationPayload(BaseModel):
    violation_type: str


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
            connection.execute(f"DELETE FROM exam_violations WHERE attempt_id IN ({placeholders})", attempt_ids)
            connection.execute(f"DELETE FROM responses WHERE attempt_id IN ({placeholders})", attempt_ids)
            connection.execute(f"DELETE FROM attempts WHERE attempt_id IN ({placeholders})", attempt_ids)
        connection.execute("DELETE FROM tests WHERE owner_student_id = ?", (normalized_id,))
        connection.execute("DELETE FROM student_sessions WHERE student_id = ?", (normalized_id,))
        connection.execute("DELETE FROM students WHERE student_id = ?", (normalized_id,))
    return {"student_id": normalized_id}


def student_available_tests(connection: sqlite3.Connection, student_id: Optional[str] = None) -> List[Dict[str, Any]]:
    exclusion = ""
    parameters: List[Any] = []
    if student_id:
        exclusion = """AND NOT EXISTS (
            SELECT 1 FROM attempts a
            WHERE a.test_id = tests.test_id AND a.student_id = ? AND a.status = 'submitted'
        )"""
        parameters.append(student_id)
    launched = connection.execute(
        f"SELECT * FROM tests WHERE active = 1 AND launched = 1 AND mode = 'faculty' {exclusion} ORDER BY test_id DESC",
        parameters,
    ).fetchall()
    available = launched or connection.execute(
        f"SELECT * FROM tests WHERE active = 1 AND mode = 'faculty' {exclusion} ORDER BY test_id DESC",
        parameters,
    ).fetchall()
    return [dict(test) for test in available]


def feedback_allowed(test: sqlite3.Row | Dict[str, Any]) -> bool:
    keys = set(test.keys())
    if "mode" in keys and test["mode"] == "student_practice":
        return True
    if "expires_at" in keys and test["expires_at"]:
        return False
    return not bool(test["launched"])


def answer_locked(response: sqlite3.Row | Dict[str, Any]) -> bool:
    return feedback_allowed(response) and response["selected_answer"] is not None


def rows(items: List[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [dict(item) for item in items]


def normalize_difficulties(values: Optional[List[str]]) -> List[str]:
    requested = values or list(QUESTION_DIFFICULTIES)
    normalized: List[str] = []
    for value in requested:
        difficulty = str(value).strip().title()
        if difficulty not in QUESTION_DIFFICULTIES:
            raise HTTPException(400, f"Difficulty must be one of: {', '.join(QUESTION_DIFFICULTIES)}.")
        if difficulty not in normalized:
            normalized.append(difficulty)
    if not normalized:
        raise HTTPException(400, "Choose at least one difficulty level.")
    return normalized


def decode_difficulties(raw: Optional[str]) -> List[str]:
    try:
        stored = json.loads(raw or "[]")
    except json.JSONDecodeError as error:
        raise HTTPException(500, "The saved difficulty filter is invalid.") from error
    if not isinstance(stored, list):
        raise HTTPException(500, "The saved difficulty filter is invalid.")
    return normalize_difficulties(stored)


def question_assets_dir() -> Path:
    return DATA_DIR / "Question Assets"


def normalize_selection_rules(
    selection_rules: List[SelectionRule] | List[Dict[str, Any]],
    composition: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """Normalize v2 leaf rules and the legacy category-count mapping."""
    merged: Dict[tuple[str, str], int] = {}
    raw_rules: List[Any] = list(selection_rules or [])
    if not raw_rules and composition:
        raw_rules = [
            {"category": category, "chapter": UNCATEGORIZED_CHAPTER, "quantity": quantity}
            for category, quantity in composition.items()
        ]
    for rule in raw_rules:
        if isinstance(rule, SelectionRule):
            category, chapter, quantity = rule.category, rule.chapter, rule.quantity
        elif isinstance(rule, dict):
            category = rule.get("category", "")
            chapter = rule.get("chapter", UNCATEGORIZED_CHAPTER)
            quantity = rule.get("quantity", 0)
        else:
            raise HTTPException(400, "Every selection rule must be an object.")
        category = str(category).strip()
        chapter = str(chapter or UNCATEGORIZED_CHAPTER).strip() or UNCATEGORIZED_CHAPTER
        try:
            quantity = int(quantity)
        except (TypeError, ValueError) as error:
            raise HTTPException(400, "Question quantities must be whole numbers.") from error
        if not category or len(category) > 100 or len(chapter) > 120:
            raise HTTPException(400, "Every selection rule needs a valid category and chapter.")
        if quantity < 0:
            raise HTTPException(400, "Question quantities cannot be negative.")
        if quantity:
            merged[(category, chapter)] = merged.get((category, chapter), 0) + quantity
    return [
        {"category": category, "chapter": chapter, "quantity": quantity}
        for (category, chapter), quantity in merged.items()
    ]


def decode_selection_rules(raw: str) -> List[Dict[str, Any]]:
    try:
        stored = json.loads(raw or "[]")
    except json.JSONDecodeError as error:
        raise HTTPException(500, "The saved test composition is invalid.") from error
    if isinstance(stored, dict):
        return normalize_selection_rules([], stored)
    if isinstance(stored, list):
        return normalize_selection_rules(stored)
    raise HTTPException(500, "The saved test composition is invalid.")


def question_bank_taxonomy(connection: sqlite3.Connection, bank_id: int) -> Dict[str, Any]:
    bank = connection.execute(
        "SELECT bank_id, bank_name FROM question_banks WHERE bank_id = ?", (bank_id,)
    ).fetchone()
    if not bank:
        raise HTTPException(404, "Question bank not found.")
    grouped = connection.execute(
        """SELECT category, COALESCE(NULLIF(chapter, ''), ?) AS chapter, difficulty, COUNT(*) AS count
           FROM questions WHERE bank_id = ? AND active = 1
           GROUP BY category, COALESCE(NULLIF(chapter, ''), ?), difficulty
           ORDER BY category, chapter, difficulty""",
        (UNCATEGORIZED_CHAPTER, bank_id, UNCATEGORIZED_CHAPTER),
    ).fetchall()
    categories: Dict[str, Dict[str, Any]] = {}
    for item in grouped:
        category = categories.setdefault(item["category"], {"name": item["category"], "count": 0, "chapters": []})
        category["count"] += item["count"]
        chapter = next((entry for entry in category["chapters"] if entry["name"] == item["chapter"]), None)
        if chapter is None:
            chapter = {"name": item["chapter"], "count": 0, "difficulties": {difficulty: 0 for difficulty in QUESTION_DIFFICULTIES}}
            category["chapters"].append(chapter)
        chapter["count"] += item["count"]
        chapter["difficulties"][item["difficulty"]] = item["count"]
    return {
        "bank_id": bank["bank_id"],
        "bank_name": bank["bank_name"],
        "question_count": sum(category["count"] for category in categories.values()),
        "categories": list(categories.values()),
    }


def validate_selection_rules(
    connection: sqlite3.Connection,
    bank_id: int,
    rules: List[Dict[str, Any]],
    maximum: int,
    difficulties: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    if not connection.execute("SELECT 1 FROM question_banks WHERE bank_id = ?", (bank_id,)).fetchone():
        raise HTTPException(404, "Choose an imported question bank.")
    total = sum(rule["quantity"] for rule in rules)
    if total <= 0:
        raise HTTPException(400, "Choose at least one question.")
    if total > maximum:
        raise HTTPException(400, f"Choose no more than {maximum} questions.")
    selected_difficulties = normalize_difficulties(difficulties)
    placeholders = ",".join("?" for _ in selected_difficulties)
    availability = {
        (row["category"], row["chapter"]): row["count"]
        for row in connection.execute(
            f"""SELECT category, COALESCE(NULLIF(chapter, ''), ?) AS chapter, COUNT(*) AS count
               FROM questions WHERE bank_id = ? AND active = 1 AND difficulty IN ({placeholders})
               GROUP BY category, COALESCE(NULLIF(chapter, ''), ?)""",
            (UNCATEGORIZED_CHAPTER, bank_id, *selected_difficulties, UNCATEGORIZED_CHAPTER),
        ).fetchall()
    }
    shortages = [
        f"{rule['category']} / {rule['chapter']}: need {rule['quantity']}, have {availability.get((rule['category'], rule['chapter']), 0)}"
        for rule in rules
        if rule["quantity"] > availability.get((rule["category"], rule["chapter"]), 0)
    ]
    if shortages:
        raise HTTPException(400, "Not enough active questions — " + "; ".join(shortages) + ".")
    return rules


def sample_questions(
    connection: sqlite3.Connection,
    bank_id: int,
    rules: List[Dict[str, Any]],
    difficulties: Optional[List[str]] = None,
) -> List[sqlite3.Row]:
    selected_difficulties = normalize_difficulties(difficulties)
    placeholders = ",".join("?" for _ in selected_difficulties)
    selected: List[sqlite3.Row] = []
    selected_ids: set[int] = set()
    for rule in rules:
        pool = connection.execute(
            f"""SELECT question_id, category, COALESCE(NULLIF(chapter, ''), ?) AS chapter, stimulus_id
               FROM questions
               WHERE bank_id = ? AND active = 1 AND category = ? AND difficulty IN ({placeholders})
                  AND COALESCE(NULLIF(chapter, ''), ?) = ?""",
            (UNCATEGORIZED_CHAPTER, bank_id, rule["category"], *selected_difficulties, UNCATEGORIZED_CHAPTER, rule["chapter"]),
        ).fetchall()
        available = [question for question in pool if question["question_id"] not in selected_ids]
        chosen = random.sample(available, rule["quantity"])
        selected.extend(chosen)
        selected_ids.update(question["question_id"] for question in chosen)

    grouped: Dict[str, List[sqlite3.Row]] = {}
    for question in selected:
        group_key = f"stimulus:{question['stimulus_id']}" if question["stimulus_id"] else f"question:{question['question_id']}"
        grouped.setdefault(group_key, []).append(question)
    question_groups = list(grouped.values())
    random.shuffle(question_groups)
    for group in question_groups:
        random.shuffle(group)
    return [question for group in question_groups for question in group]


def create_attempt_from_questions(
    connection: sqlite3.Connection,
    student_id: str,
    test_id: int,
    selected: List[sqlite3.Row],
    time_limit_seconds: Optional[int] = None,
) -> str:
    attempt_id = str(uuid.uuid4())
    started_at = now()
    expires_at = (
        (datetime.now(timezone.utc) + timedelta(seconds=time_limit_seconds)).isoformat(timespec="seconds")
        if time_limit_seconds else None
    )
    connection.execute(
        """INSERT INTO attempts
           (attempt_id, student_id, test_id, started_at, total_questions, expires_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (attempt_id, student_id, test_id, started_at, len(selected), expires_at),
    )
    for index, question in enumerate(selected, start=1):
        connection.execute(
            """INSERT INTO responses (attempt_id, question_id, category, chapter, question_order)
               VALUES (?, ?, ?, ?, ?)""",
            (attempt_id, question["question_id"], question["category"], question["chapter"], index),
        )
    return attempt_id


def clean_display_text(value: str) -> str:
    """Remove unsupported private-use glyphs produced by older PDF extraction."""
    for _ in range(2):
        if not any(marker in value for marker in ("\u00c3", "\u00c2", "\u00e2")):
            break
        try:
            value = value.encode("latin-1").decode("utf-8")
        except UnicodeError:
            break
    value = unicodedata.normalize("NFKC", value).replace("\u00a0", " ")
    return re.sub(r"[\uE000-\uF8FF]", "", value)


def has_critical_solution_artifacts(steps: List[str]) -> bool:
    text = "\n".join(steps)
    markers = ("×=", "=×", "==", "%%", "% %", "× ×", "{}", "{ }")
    return (
        any("\uE000" <= char <= "\uF8FF" for char in text)
        or any(marker in text for marker in ("\u00c3", "\u00c2", "\u00e2"))
        or bool(re.search(r"(?<![\u0400-\u04FF])[\u0432\u0412](?![\u0400-\u04FF])", text))
        or any(marker in text for marker in markers)
        or bool(re.search(r"×\s+×", text))
        or bool(re.search(r"(?:\b\d\s+){5,}\d\b", text))
        or bool(re.search(r"\b\d{4,}\s+\d{4,}\b", text))
    )


def clean_display_value(value: Any) -> Any:
    if isinstance(value, str):
        return clean_display_text(value)
    if isinstance(value, list):
        cleaned = [clean_display_value(item) for item in value]
        if len(cleaned) == 1 and isinstance(cleaned[0], str) and cleaned[0].startswith("Amount spent on Groceries, Entertainment and Investments"):
            return next(iter(SOLUTION_STEP_OVERRIDES.values()))
        if cleaned and isinstance(cleaned[0], str) and cleaned[0].startswith("Let the value of export in 2008"):
            return list(SOLUTION_STEP_OVERRIDES.values())[1]
        if all(isinstance(item, str) for item in cleaned) and has_critical_solution_artifacts(cleaned):
            return SOLUTION_REVIEW_NOTICE
        return cleaned
    if isinstance(value, dict):
        return {key: clean_display_value(item) for key, item in value.items()}
    return value


def display_solution_steps(question_text: str, stored_steps: str) -> List[str]:
    """Use repaired steps where PDF extraction did not preserve formula order."""
    normalized_question_text = clean_display_text(question_text)
    if normalized_question_text in SOLUTION_STEP_OVERRIDES:
        return SOLUTION_STEP_OVERRIDES[normalized_question_text]
    return clean_display_value(json.loads(stored_steps))


def question_options(question: sqlite3.Row | Dict[str, Any]) -> Dict[str, str]:
    """Return JSON-defined choices, falling back to legacy A-D columns."""
    columns = set(question.keys())
    raw_options = question["options_json"] if "options_json" in columns else None
    if raw_options:
        try:
            options = json.loads(raw_options)
        except (TypeError, json.JSONDecodeError):
            options = None
        if isinstance(options, dict) and options and all(isinstance(key, str) and isinstance(value, str) for key, value in options.items()):
            return clean_display_value(options)
    return {key: clean_display_text(question[f"option_{key.lower()}"]) for key in LEGACY_OPTION_KEYS}


def migrate_question_options(connection: sqlite3.Connection) -> None:
    """Populate options_json for databases created before dynamic choices."""
    questions = connection.execute(
        "SELECT question_id, option_a, option_b, option_c, option_d, options_json FROM questions"
    ).fetchall()
    for question in questions:
        try:
            stored_options = json.loads(question["options_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            stored_options = {}
        if isinstance(stored_options, dict) and stored_options:
            continue
        legacy_options = {key: question[f"option_{key.lower()}"] for key in LEGACY_OPTION_KEYS}
        connection.execute(
            "UPDATE questions SET options_json = ? WHERE question_id = ?",
            (json.dumps(legacy_options), question["question_id"]),
        )


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
        category = str(entry.get("category", "")).strip()
        chapter = str(entry.get("chapter", UNCATEGORIZED_CHAPTER) or UNCATEGORIZED_CHAPTER).strip()
        stimulus_id = str(entry.get("stimulus_id", "")).strip() or None
        if not category or len(category) > 100:
            raise HTTPException(400, f"Question {key!r} has an invalid category.")
        if not chapter or len(chapter) > 120:
            raise HTTPException(400, f"Question {key!r} has an invalid chapter.")
        difficulty = entry.get("difficulty", "Medium")
        if difficulty not in {"Easy", "Medium", "Hard"}:
            raise HTTPException(400, f"Question {key!r} has an invalid difficulty.")
        options = entry.get("options")
        option_keys = set(options) if isinstance(options, dict) else set()
        if option_keys not in (set(LEGACY_OPTION_KEYS), set(SUPPORTED_OPTION_KEYS)) or not all(isinstance(options[value], str) and options[value].strip() for value in options):
            raise HTTPException(400, f"Question {key!r} needs non-empty A, B, C and D options, with optional E.")
        options = {option: options[option] for option in SUPPORTED_OPTION_KEYS if option in options}
        correct = entry.get("correct_answer")
        if correct not in options:
            raise HTTPException(400, f"Question {key!r} needs a correct_answer matching one of its options.")
        steps = entry.get("solution_steps", [])
        option_explanations = entry.get("option_explanations", {})
        records[key] = {
            "category": category,
            "chapter": chapter,
            "stimulus_id": stimulus_id,
            "difficulty": difficulty,
            "options": options,
            "correct_answer": correct,
            "explanation": str(entry.get("explanation", "")).strip(),
            "solution_steps": steps if isinstance(steps, list) else [],
            "option_explanations": option_explanations if isinstance(option_explanations, dict) else {},
        }
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
    question_assets_dir().mkdir(exist_ok=True)
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
              imported_at TEXT NOT NULL, format_version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS student_sessions (
              student_id TEXT PRIMARY KEY, session_token TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
              FOREIGN KEY(student_id) REFERENCES students(student_id)
            );
            CREATE TABLE IF NOT EXISTS questions (
              question_id INTEGER PRIMARY KEY AUTOINCREMENT, question_text TEXT NOT NULL,
              source_key TEXT, category TEXT NOT NULL, chapter TEXT NOT NULL DEFAULT 'Uncategorized',
              difficulty TEXT NOT NULL, option_a TEXT NOT NULL,
              option_b TEXT NOT NULL, option_c TEXT NOT NULL, option_d TEXT NOT NULL,
              options_json TEXT NOT NULL DEFAULT '{}',
              correct_answer TEXT NOT NULL, explanation TEXT DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
              bank_id INTEGER, stimulus_id TEXT, question_html TEXT NOT NULL DEFAULT '',
              solution_steps TEXT NOT NULL DEFAULT '[]', option_explanations TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stimuli (
              bank_id INTEGER NOT NULL, stimulus_id TEXT NOT NULL, stimulus_type TEXT NOT NULL,
              title TEXT NOT NULL DEFAULT '', alt_text TEXT NOT NULL DEFAULT '',
              asset_filename TEXT, content_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
              PRIMARY KEY(bank_id, stimulus_id),
              FOREIGN KEY(bank_id) REFERENCES question_banks(bank_id)
            );
            CREATE TABLE IF NOT EXISTS tests (
              test_id INTEGER PRIMARY KEY AUTOINCREMENT, test_name TEXT NOT NULL,
              composition TEXT NOT NULL, bank_id INTEGER, created_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
              mode TEXT NOT NULL DEFAULT 'faculty', owner_student_id TEXT
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
              category TEXT NOT NULL, chapter TEXT NOT NULL DEFAULT 'Uncategorized', question_order INTEGER NOT NULL,
              FOREIGN KEY(attempt_id) REFERENCES attempts(attempt_id),
              FOREIGN KEY(question_id) REFERENCES questions(question_id),
              UNIQUE(attempt_id, question_id)
            );
            CREATE TABLE IF NOT EXISTS exam_violations (
              violation_id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT NOT NULL,
              violation_type TEXT NOT NULL, occurred_at TEXT NOT NULL,
              FOREIGN KEY(attempt_id) REFERENCES attempts(attempt_id)
            );
            CREATE INDEX IF NOT EXISTS idx_attempts_student ON attempts(student_id);
            CREATE INDEX IF NOT EXISTS idx_responses_attempt ON responses(attempt_id);
            """
        )
        ensure_column(connection, "questions", "bank_id INTEGER")
        ensure_column(connection, "questions", "source_key TEXT")
        ensure_column(connection, "questions", "chapter TEXT NOT NULL DEFAULT 'Uncategorized'")
        ensure_column(connection, "questions", "stimulus_id TEXT")
        ensure_column(connection, "questions", "question_html TEXT NOT NULL DEFAULT ''")
        ensure_column(connection, "questions", "solution_steps TEXT NOT NULL DEFAULT '[]'")
        ensure_column(connection, "questions", "option_explanations TEXT NOT NULL DEFAULT '{}'")
        ensure_column(connection, "questions", "options_json TEXT NOT NULL DEFAULT '{}'")
        migrate_question_options(connection)
        ensure_column(connection, "question_banks", "format_version INTEGER NOT NULL DEFAULT 1")
        ensure_column(connection, "tests", "bank_id INTEGER")
        ensure_column(connection, "tests", "launched INTEGER NOT NULL DEFAULT 0")
        ensure_column(connection, "tests", "mode TEXT NOT NULL DEFAULT 'faculty'")
        ensure_column(connection, "tests", "owner_student_id TEXT")
        ensure_column(connection, "tests", "difficulties TEXT NOT NULL DEFAULT '[\"Easy\",\"Medium\",\"Hard\"]'")
        ensure_column(connection, "attempts", "expires_at TEXT")
        ensure_column(connection, "responses", "chapter TEXT NOT NULL DEFAULT 'Uncategorized'")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_questions_bank ON questions(bank_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_questions_taxonomy ON questions(bank_id, category, chapter, active)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_tests_mode ON tests(mode, active, launched)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_exam_violations_attempt ON exam_violations(attempt_id)")


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
                    (question_text, category, difficulty, option_a, option_b, option_c, option_d, options_json, correct_answer, explanation, bank_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (*item[:7], json.dumps(dict(zip(LEGACY_OPTION_KEYS, item[3:7]))), *item[7:], starter_bank_id, now()),
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
                (source_key, question_text, question_html, category, chapter, stimulus_id, difficulty,
                 option_a, option_b, option_c, option_d, options_json, correct_answer, explanation,
                 bank_id, created_at, solution_steps, option_explanations)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    question.get("key"), question["question_text"], question["question_html"], question["category"],
                    question.get("chapter", UNCATEGORIZED_CHAPTER), question.get("stimulus_id"), question["difficulty"],
                    options["A"], options["B"], options["C"], options["D"], json.dumps(options),
                    question["correct_answer"], question["explanation"], bank_id, now(),
                    json.dumps(question["solution_steps"]), json.dumps(question["option_explanations"]),
                ),
            )
    return {"imported": True, "bank_id": bank_id, "bank_name": bank_name, "question_count": len(questions)}


def validate_package_member(name: str) -> str:
    if not name or "\\" in name:
        raise HTTPException(400, "Question-bank packages must use safe forward-slash paths.")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or any(not part for part in path.parts):
        raise HTTPException(400, f"Unsafe package path: {name!r}.")
    return path.as_posix()


def validate_svg_asset(content: bytes, filename: str) -> None:
    try:
        source = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise HTTPException(400, f"SVG asset {filename!r} must be UTF-8 encoded.") from error
    blocked = re.compile(
        r"<\s*(script|foreignObject|iframe|object|embed)\b|\bon[a-z]+\s*=|(?:href|src)\s*=\s*['\"]\s*(?:https?:|data:|javascript:)",
        re.IGNORECASE,
    )
    if blocked.search(source):
        raise HTTPException(400, f"SVG asset {filename!r} contains executable or external content.")


def parse_v2_question(entry: Any, origin: str) -> Dict[str, Any]:
    if not isinstance(entry, dict):
        raise HTTPException(400, f"Every question in {origin} must be an object.")
    key = str(entry.get("key", "")).strip()
    category = str(entry.get("category", "")).strip()
    chapter = str(entry.get("chapter", "")).strip()
    if not key or len(key) > 160:
        raise HTTPException(400, f"Every question in {origin} needs a valid key.")
    if not category or not chapter or len(category) > 100 or len(chapter) > 120:
        raise HTTPException(400, f"Question {key!r} needs a valid category and chapter.")
    difficulty = str(entry.get("difficulty", "Medium")).strip()
    if difficulty not in {"Easy", "Medium", "Hard"}:
        raise HTTPException(400, f"Question {key!r} has an invalid difficulty.")
    options = entry.get("options")
    option_keys = set(options) if isinstance(options, dict) else set()
    if option_keys not in (set(LEGACY_OPTION_KEYS), set(SUPPORTED_OPTION_KEYS)):
        raise HTTPException(400, f"Question {key!r} needs A-D options, with optional E.")
    options = {option: str(options[option]).strip() for option in SUPPORTED_OPTION_KEYS if option in options}
    if not all(options.values()):
        raise HTTPException(400, f"Question {key!r} has an empty option.")
    correct = entry.get("correct_answer")
    if correct not in options:
        raise HTTPException(400, f"Question {key!r} needs a correct_answer matching one option.")
    question_html = sanitize_visual_html(str(entry.get("question_html", "")))
    question_text = str(entry.get("question_text", "")).strip() or question_summary(question_html)
    if not question_text:
        raise HTTPException(400, f"Question {key!r} needs question_text or readable question_html.")
    steps = entry.get("solution_steps", [])
    option_explanations = entry.get("option_explanations", {})
    return {
        "key": key,
        "question_text": question_text,
        "question_html": question_html,
        "category": category,
        "chapter": chapter,
        "stimulus_id": str(entry.get("stimulus_id", "")).strip() or None,
        "difficulty": difficulty,
        "options": options,
        "correct_answer": correct,
        "explanation": str(entry.get("explanation", "")).strip(),
        "solution_steps": steps if isinstance(steps, list) else [],
        "option_explanations": option_explanations if isinstance(option_explanations, dict) else {},
    }


def parse_v2_package(package_file: Any) -> tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    try:
        archive = zipfile.ZipFile(package_file)
    except (zipfile.BadZipFile, OSError) as error:
        raise HTTPException(400, "The uploaded v2 question bank is not a valid ZIP file.") from error
    with archive:
        members = archive.infolist()
        if len(members) > MAX_PACKAGE_FILES:
            raise HTTPException(413, f"A package may contain at most {MAX_PACKAGE_FILES} files.")
        if sum(member.file_size for member in members) > MAX_PACKAGE_UNPACKED_BYTES:
            raise HTTPException(413, "The unpacked question-bank package is too large.")
        names = {validate_package_member(member.filename): member for member in members if not member.is_dir()}
        manifest_info = names.get("manifest.json")
        if not manifest_info or manifest_info.file_size > 1_000_000:
            raise HTTPException(400, "A v2 package needs a manifest.json under 1 MB.")
        try:
            manifest = json.loads(archive.read(manifest_info).decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HTTPException(400, "manifest.json must be valid UTF-8 JSON.") from error
        if not isinstance(manifest, dict) or manifest.get("format_version") != 2:
            raise HTTPException(400, "manifest.json must declare format_version 2.")
        bank_name = str(manifest.get("bank_name", "")).strip()
        if not bank_name:
            raise HTTPException(400, "manifest.json needs a non-empty bank_name.")
        question_files = manifest.get("question_files")
        if not isinstance(question_files, list) or not question_files:
            raise HTTPException(400, "manifest.json needs a non-empty question_files list.")

        questions: List[Dict[str, Any]] = []
        question_keys: set[str] = set()
        for raw_name in question_files:
            filename = validate_package_member(str(raw_name))
            member = names.get(filename)
            if not member or member.file_size > MAX_LEGACY_FILE_BYTES:
                raise HTTPException(400, f"Question file {filename!r} is missing or too large.")
            try:
                source = archive.read(member).decode("utf-8-sig")
            except UnicodeDecodeError as error:
                raise HTTPException(400, f"Question file {filename!r} must be UTF-8 encoded.") from error
            entries: Any
            try:
                if filename.lower().endswith(".jsonl"):
                    entries = [json.loads(line) for line in source.splitlines() if line.strip()]
                elif filename.lower().endswith(".json"):
                    decoded = json.loads(source)
                    entries = decoded.get("questions") if isinstance(decoded, dict) else decoded
                else:
                    raise HTTPException(400, "Question files must use .json or .jsonl.")
            except json.JSONDecodeError as error:
                raise HTTPException(400, f"Question file {filename!r} contains invalid JSON.") from error
            if not isinstance(entries, list):
                raise HTTPException(400, f"Question file {filename!r} must contain a question list.")
            for entry in entries:
                question = parse_v2_question(entry, filename)
                if question["key"] in question_keys:
                    raise HTTPException(400, f"Duplicate question key: {question['key']!r}.")
                question_keys.add(question["key"])
                questions.append(question)
        if not questions:
            raise HTTPException(400, "The package does not contain any questions.")

        stimuli: List[Dict[str, Any]] = []
        stimulus_ids: set[str] = set()
        for entry in manifest.get("stimuli", []):
            if not isinstance(entry, dict):
                raise HTTPException(400, "Every stimulus must be an object.")
            stimulus_id = str(entry.get("id", "")).strip()
            stimulus_type = str(entry.get("type", "image")).strip().lower()
            if not stimulus_id or stimulus_id in stimulus_ids:
                raise HTTPException(400, "Every stimulus needs a unique non-empty id.")
            stimulus_ids.add(stimulus_id)
            asset_bytes = None
            extension = None
            asset_path = entry.get("file")
            content = entry.get("content", {})
            if asset_path:
                filename = validate_package_member(str(asset_path))
                member = names.get(filename)
                extension = PurePosixPath(filename).suffix.lower()
                if not member or extension not in ALLOWED_ASSET_EXTENSIONS or member.file_size > 15_000_000:
                    raise HTTPException(400, f"Stimulus asset {filename!r} is missing, unsupported, or too large.")
                asset_bytes = archive.read(member)
                if extension == ".svg":
                    validate_svg_asset(asset_bytes, filename)
                stimulus_type = "image"
            elif stimulus_type not in {"chart", "table"} or not isinstance(content, dict):
                raise HTTPException(400, f"Stimulus {stimulus_id!r} needs an image file or structured chart/table content.")
            stimuli.append({
                "stimulus_id": stimulus_id,
                "stimulus_type": stimulus_type,
                "title": str(entry.get("title", "")).strip(),
                "alt_text": str(entry.get("alt_text", "")).strip(),
                "content": content if isinstance(content, dict) else {},
                "asset_bytes": asset_bytes,
                "extension": extension,
            })
        unknown_stimuli = sorted({question["stimulus_id"] for question in questions if question["stimulus_id"]} - stimulus_ids)
        if unknown_stimuli:
            raise HTTPException(400, "Questions reference missing stimuli: " + ", ".join(unknown_stimuli[:20]))
        return bank_name, questions, stimuli


def save_v2_question_bank(
    bank_name: str,
    questions: List[Dict[str, Any]],
    stimuli: List[Dict[str, Any]],
    package_name: str,
) -> Dict[str, Any]:
    bank_asset_dir: Optional[Path] = None
    try:
        with db() as connection:
            if connection.execute("SELECT 1 FROM question_banks WHERE bank_name = ?", (bank_name,)).fetchone():
                raise HTTPException(409, "A question bank with this name already exists. Use a versioned bank_name.")
            bank_id = connection.execute(
                """INSERT INTO question_banks
                   (bank_name, source_html_filename, answer_key_filename, imported_at, format_version)
                   VALUES (?, ?, ?, ?, 2)""",
                (bank_name, package_name, "manifest.json", now()),
            ).lastrowid
            bank_asset_dir = question_assets_dir() / str(bank_id)
            bank_asset_dir.mkdir(parents=True, exist_ok=False)
            for stimulus in stimuli:
                asset_filename = None
                if stimulus["asset_bytes"] is not None:
                    digest = hashlib.sha256(stimulus["asset_bytes"]).hexdigest()
                    asset_filename = digest + stimulus["extension"]
                    (bank_asset_dir / asset_filename).write_bytes(stimulus["asset_bytes"])
                connection.execute(
                    """INSERT INTO stimuli
                       (bank_id, stimulus_id, stimulus_type, title, alt_text, asset_filename, content_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        bank_id, stimulus["stimulus_id"], stimulus["stimulus_type"], stimulus["title"],
                        stimulus["alt_text"], asset_filename, json.dumps(stimulus["content"]), now(),
                    ),
                )
            for question in questions:
                options = question["options"]
                connection.execute(
                    """INSERT INTO questions
                       (source_key, question_text, question_html, category, chapter, stimulus_id, difficulty,
                        option_a, option_b, option_c, option_d, options_json, correct_answer, explanation,
                        bank_id, created_at, solution_steps, option_explanations)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        question["key"], question["question_text"], question["question_html"], question["category"],
                        question["chapter"], question["stimulus_id"], question["difficulty"], options["A"],
                        options["B"], options["C"], options["D"], json.dumps(options), question["correct_answer"],
                        question["explanation"], bank_id, now(), json.dumps(question["solution_steps"]),
                        json.dumps(question["option_explanations"]),
                    ),
                )
        return {
            "imported": True,
            "format_version": 2,
            "bank_id": bank_id,
            "bank_name": bank_name,
            "question_count": len(questions),
            "stimulus_count": len(stimuli),
        }
    except Exception:
        if bank_asset_dir and bank_asset_dir.is_dir():
            shutil.rmtree(bank_asset_dir)
        raise


def require_user(request: Request, role: Optional[str] = None) -> Dict[str, str]:
    user = request.session.get("user")
    if not user or (role and user["role"] != role):
        raise HTTPException(401, "Please sign in to continue.")
    if user["role"] == "student" and user.get("login_token"):
        with db() as connection:
            active = connection.execute(
                "SELECT session_token FROM student_sessions WHERE student_id = ?", (user["id"],)
            ).fetchone()
        if not active or active["session_token"] != user["login_token"]:
            request.session.clear()
            raise HTTPException(401, "This student login is no longer active. Ask Faculty to remove the account if you need to register again.")
    return user


def get_attempt(connection: sqlite3.Connection, attempt_id: str) -> sqlite3.Row:
    attempt = connection.execute(
        """SELECT a.*, t.launched, t.mode, t.owner_student_id, t.bank_id
           FROM attempts a JOIN tests t ON t.test_id = a.test_id WHERE a.attempt_id = ?""",
        (attempt_id,),
    ).fetchone()
    if not attempt:
        raise HTTPException(404, "Assessment attempt not found.")
    return attempt


def parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def seconds_remaining(attempt: sqlite3.Row | Dict[str, Any]) -> Optional[int]:
    expires_at = parse_timestamp(attempt["expires_at"] if "expires_at" in attempt.keys() else None)
    if not expires_at:
        return None
    return max(0, math.ceil((expires_at - datetime.now(timezone.utc)).total_seconds()))


def finalize_attempt(connection: sqlite3.Connection, attempt_id: str) -> sqlite3.Row:
    attempt = get_attempt(connection, attempt_id)
    if attempt["status"] == "submitted":
        return attempt
    connection.execute(
        """UPDATE responses SET correct = CASE
           WHEN selected_answer IS NULL THEN 0
           WHEN selected_answer = (SELECT correct_answer FROM questions WHERE questions.question_id = responses.question_id) THEN 1
           ELSE 0 END WHERE attempt_id = ?""",
        (attempt_id,),
    )
    stats = connection.execute(
        """SELECT COUNT(*) AS total, SUM(selected_answer IS NOT NULL) AS attempted,
                  SUM(correct = 1) AS correct FROM responses WHERE attempt_id = ?""",
        (attempt_id,),
    ).fetchone()
    total, attempted, correct = stats["total"], stats["attempted"] or 0, stats["correct"] or 0
    percentage = round(correct / total * 100, 1) if total else 0
    connection.execute(
        """UPDATE attempts SET submitted_at = ?, status = 'submitted', attempted = ?,
                  correct = ?, score = ?, percentage = ? WHERE attempt_id = ?""",
        (now(), attempted, correct, correct, percentage, attempt_id),
    )
    return get_attempt(connection, attempt_id)


def expire_attempt_if_needed(connection: sqlite3.Connection, attempt: sqlite3.Row) -> sqlite3.Row:
    if attempt["status"] == "in_progress" and seconds_remaining(attempt) == 0:
        return finalize_attempt(connection, attempt["attempt_id"])
    return attempt


def finalize_expired_attempts(connection: sqlite3.Connection) -> int:
    expired = connection.execute(
        """SELECT attempt_id FROM attempts
           WHERE status = 'in_progress' AND expires_at IS NOT NULL AND expires_at <= ?""",
        (now(),),
    ).fetchall()
    for attempt in expired:
        finalize_attempt(connection, attempt["attempt_id"])
    return len(expired)


def assert_student_attempt(connection: sqlite3.Connection, attempt_id: str, student_id: str) -> sqlite3.Row:
    attempt = get_attempt(connection, attempt_id)
    if attempt["student_id"] != student_id:
        raise HTTPException(403, "You can only access your own attempts.")
    return attempt


def serialize_attempt(connection: sqlite3.Connection, attempt: sqlite3.Row, include_answers: bool = False) -> Dict[str, Any]:
    attempt = expire_attempt_if_needed(connection, attempt)
    include_answers = include_answers or feedback_allowed(attempt)
    response_rows = connection.execute(
        """SELECT r.question_order, r.selected_answer, r.category, r.chapter,
                  q.question_id, q.question_text, q.question_html, q.bank_id, q.stimulus_id,
                  q.difficulty, q.option_a, q.option_b, q.option_c, q.option_d, q.options_json,
                  q.explanation, q.correct_answer, q.solution_steps, q.option_explanations,
                  s.stimulus_type, s.title AS stimulus_title, s.alt_text, s.asset_filename, s.content_json
           FROM responses r JOIN questions q ON q.question_id = r.question_id
           LEFT JOIN stimuli s ON s.bank_id = q.bank_id AND s.stimulus_id = q.stimulus_id
           WHERE r.attempt_id = ? ORDER BY r.question_order""",
        (attempt["attempt_id"],),
    ).fetchall()
    questions = []
    for row in response_rows:
        question = {
            "question_id": row["question_id"], "question_text": clean_display_text(row["question_text"]), "question_html": clean_display_text(row["question_html"]),
            "category": row["category"], "chapter": row["chapter"], "difficulty": row["difficulty"],
            "options": question_options(row),
            "selected_answer": row["selected_answer"],
        }
        if row["stimulus_id"] and row["stimulus_type"]:
            stimulus = {
                "id": row["stimulus_id"],
                "type": row["stimulus_type"],
                "title": row["stimulus_title"],
                "alt_text": row["alt_text"],
            }
            if row["asset_filename"]:
                stimulus["url"] = f"/api/question-banks/{row['bank_id']}/stimuli/{row['stimulus_id']}"
            else:
                stimulus["content"] = json.loads(row["content_json"] or "{}")
            question["stimulus"] = stimulus
        if include_answers:
            question.update({"correct_answer": row["correct_answer"], "explanation": clean_display_text(row["explanation"]), "solution_steps": display_solution_steps(row["question_text"], row["solution_steps"]), "option_explanations": clean_display_value(json.loads(row["option_explanations"]))})
            if row["selected_answer"] is not None and feedback_allowed(attempt):
                question["feedback"] = {
                    "correct": row["selected_answer"] == row["correct_answer"],
                    "correct_answer": row["correct_answer"],
                    "explanation": clean_display_text(row["explanation"]),
                    "solution_steps": display_solution_steps(row["question_text"], row["solution_steps"]),
                    "option_explanations": clean_display_value(json.loads(row["option_explanations"])),
                }
        questions.append(question)
    return {
        **dict(attempt),
        "feedback_allowed": feedback_allowed(attempt),
        "proctored": attempt["mode"] == "faculty" and bool(attempt["expires_at"]),
        "remaining_seconds": seconds_remaining(attempt),
        "server_time": now(),
        "questions": questions,
    }


def result_for_attempt(connection: sqlite3.Connection, attempt_id: str) -> Dict[str, Any]:
    attempt = get_attempt(connection, attempt_id)
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
    chapter_rows = connection.execute(
        """SELECT r.category, r.chapter, COUNT(*) AS total, SUM(r.selected_answer IS NOT NULL) AS attempted,
                  SUM(r.correct = 1) AS correct
           FROM responses r WHERE r.attempt_id = ?
           GROUP BY r.category, r.chapter ORDER BY r.category, r.chapter""",
        (attempt_id,),
    ).fetchall()
    chapters = []
    for row in chapter_rows:
        total, attempted, correct = row["total"], row["attempted"] or 0, row["correct"] or 0
        chapters.append({
            "category": row["category"], "chapter": row["chapter"], "total": total,
            "attempted": attempted, "correct": correct,
            "percentage": round(correct / total * 100, 1) if total else 0,
        })
    violation_rows = connection.execute(
        """SELECT violation_type, occurred_at FROM exam_violations
           WHERE attempt_id = ? ORDER BY occurred_at, violation_id""",
        (attempt_id,),
    ).fetchall()
    violations = [
        {
            "type": row["violation_type"],
            "label": EXAM_VIOLATION_LABELS.get(row["violation_type"], row["violation_type"]),
            "occurred_at": row["occurred_at"],
        }
        for row in violation_rows
    ]
    return {
        "attempt": dict(attempt), "categories": categories, "chapters": chapters,
        "incorrect": attempt["attempted"] - attempt["correct"],
        "unanswered": attempt["total_questions"] - attempt["attempted"],
        "feedback_allowed": feedback_allowed(attempt),
        "violation_flag": bool(violations),
        "violations": violations,
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
        user = {"role": role, "id": account[user_id], "name": account[label]}
        if role == "student":
            login_token = str(uuid.uuid4())
            try:
                connection.execute(
                    "INSERT INTO student_sessions (student_id, session_token, created_at) VALUES (?, ?, ?)",
                    (account[user_id], login_token, now()),
                )
            except sqlite3.IntegrityError as error:
                raise HTTPException(
                    409,
                    "This USN is already signed in on another browser. Sign out there, or ask Faculty to delete the student account so it can be registered again.",
                ) from error
            user["login_token"] = login_token
        request.session["user"] = user
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
    user = request.session.get("user")
    if user and user.get("role") == "student" and user.get("login_token"):
        with db() as connection:
            connection.execute(
                "DELETE FROM student_sessions WHERE student_id = ? AND session_token = ?",
                (user["id"], user["login_token"]),
            )
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
        tests = student_available_tests(connection, user["id"])
        launched_test = connection.execute(
            "SELECT test_id FROM tests WHERE active = 1 AND launched = 1 AND mode = 'faculty' LIMIT 1"
        ).fetchone()
        active = connection.execute(
            """SELECT a.*, t.test_name, t.mode FROM attempts a JOIN tests t ON t.test_id = a.test_id
               WHERE a.student_id = ? AND a.status = 'in_progress' ORDER BY a.started_at DESC LIMIT 1""",
            (user["id"],),
        ).fetchone()
        if active:
            active = expire_attempt_if_needed(connection, get_attempt(connection, active["attempt_id"]))
            if active["status"] == "submitted":
                active = None
        history = connection.execute(
            """SELECT a.attempt_id, a.score, a.total_questions, a.percentage, a.submitted_at, t.test_name, t.mode
               FROM attempts a JOIN tests t ON t.test_id = a.test_id
               WHERE a.student_id = ? AND a.status = 'submitted' ORDER BY a.submitted_at DESC""", (user["id"],)
        ).fetchall()
        trend = connection.execute(
            """SELECT r.category, ROUND(AVG(r.correct) * 100, 1) AS percentage
               FROM responses r JOIN attempts a ON a.attempt_id = r.attempt_id
               WHERE a.student_id = ? AND a.status = 'submitted' GROUP BY r.category""", (user["id"],)
        ).fetchall()
    return {"student": dict(student), "tests": tests, "test": tests[0] if tests else None, "launched": bool(launched_test), "active_attempt": dict(active) if active else None, "history": rows(history), "category_trend": rows(trend)}


@app.get("/api/student/practice/catalog")
def student_practice_catalog(request: Request) -> Dict[str, Any]:
    require_user(request, "student")
    with db() as connection:
        banks = connection.execute(
            """SELECT b.bank_id, b.bank_name, COUNT(q.question_id) AS question_count
               FROM question_banks b JOIN questions q ON q.bank_id = b.bank_id AND q.active = 1
               GROUP BY b.bank_id ORDER BY b.bank_name"""
        ).fetchall()
    return {"banks": rows(banks), "maximum_questions": MAX_PRACTICE_QUESTIONS}


@app.post("/api/student/practice/start")
def start_student_practice(payload: PracticePayload, request: Request) -> Dict[str, Any]:
    user = require_user(request, "student")
    rules = normalize_selection_rules(payload.selection_rules)
    difficulties = normalize_difficulties(payload.difficulties)
    with db() as connection:
        if connection.execute(
            "SELECT 1 FROM tests WHERE active = 1 AND launched = 1 AND mode = 'faculty' LIMIT 1"
        ).fetchone():
            raise HTTPException(409, "Personal practice is paused while Faculty has a launched assessment.")
        rules = validate_selection_rules(connection, payload.bank_id, rules, MAX_PRACTICE_QUESTIONS, difficulties)
        bank = connection.execute(
            "SELECT bank_name FROM question_banks WHERE bank_id = ?", (payload.bank_id,)
        ).fetchone()
        test_id = connection.execute(
            """INSERT INTO tests
               (test_name, composition, bank_id, created_at, active, launched, mode, owner_student_id, difficulties)
               VALUES (?, ?, ?, ?, 0, 0, 'student_practice', ?, ?)""",
            (f"Practice · {bank['bank_name']}", json.dumps(rules), payload.bank_id, now(), user["id"], json.dumps(difficulties)),
        ).lastrowid
        selected = sample_questions(connection, payload.bank_id, rules, difficulties)
        attempt_id = create_attempt_from_questions(connection, user["id"], test_id, selected)
    return {"attempt_id": attempt_id, "resumed": False}


@app.post("/api/tests/{test_id}/start")
def start_test(test_id: int, request: Request) -> Dict[str, Any]:
    user = require_user(request, "student")
    with db() as connection:
        existing = connection.execute(
            "SELECT attempt_id, status FROM attempts WHERE student_id = ? AND test_id = ? ORDER BY started_at DESC LIMIT 1",
            (user["id"], test_id),
        ).fetchone()
        if existing:
            if existing["status"] == "in_progress":
                return {"attempt_id": existing["attempt_id"], "resumed": True}
            raise HTTPException(409, "You have already completed this Faculty assessment. Each student may take it only once.")
        test = connection.execute(
            "SELECT * FROM tests WHERE test_id = ? AND active = 1 AND mode = 'faculty'", (test_id,)
        ).fetchone()
        if not test:
            raise HTTPException(404, "This test is not currently available.")
        launched = connection.execute(
            "SELECT 1 FROM tests WHERE active = 1 AND launched = 1 AND mode = 'faculty' LIMIT 1"
        ).fetchone()
        if launched and not test["launched"]:
            raise HTTPException(409, "Faculty has launched another test. Please attempt the launched test.")
        difficulties = decode_difficulties(test["difficulties"])
        rules = validate_selection_rules(
            connection, test["bank_id"], decode_selection_rules(test["composition"]), MAX_ASSESSMENT_QUESTIONS, difficulties
        )
        selected = sample_questions(connection, test["bank_id"], rules, difficulties)
        time_limit = len(selected) * SECONDS_PER_FACULTY_QUESTION if test["launched"] else None
        attempt_id = create_attempt_from_questions(connection, user["id"], test_id, selected, time_limit)
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
        attempt = expire_attempt_if_needed(connection, attempt)
        if attempt["status"] != "in_progress":
            raise HTTPException(400, "This assessment has already been submitted or its timer has ended.")
        response = connection.execute(
            """SELECT r.selected_answer, q.option_a, q.option_b, q.option_c, q.option_d, q.options_json
               FROM responses r JOIN questions q ON q.question_id = r.question_id
               WHERE r.attempt_id = ? AND r.question_id = ?""",
            (attempt_id, question_id),
        ).fetchone()
        if not response:
            raise HTTPException(404, "Question is not part of this assessment.")
        if payload.answer is not None and payload.answer not in question_options(response):
            raise HTTPException(400, "The selected answer is not an option for this question.")
        if answer_locked({**dict(attempt), "selected_answer": response["selected_answer"]}):
            raise HTTPException(409, "Practice answers cannot be changed after feedback is shown.")
        if feedback_allowed(attempt):
            updated = connection.execute(
                "UPDATE responses SET selected_answer = ? WHERE attempt_id = ? AND question_id = ? AND selected_answer IS NULL",
                (payload.answer, attempt_id, question_id),
            )
            if updated.rowcount != 1:
                raise HTTPException(409, "Practice answers cannot be changed after feedback is shown.")
        else:
            connection.execute("UPDATE responses SET selected_answer = ? WHERE attempt_id = ? AND question_id = ?", (payload.answer, attempt_id, question_id))
        attempted = connection.execute("SELECT COUNT(*) AS count FROM responses WHERE attempt_id = ? AND selected_answer IS NOT NULL", (attempt_id,)).fetchone()["count"]
        connection.execute("UPDATE attempts SET attempted = ? WHERE attempt_id = ?", (attempted, attempt_id))
        feedback = None
        if feedback_allowed(attempt):
            question = connection.execute("SELECT correct_answer, explanation, solution_steps, option_explanations FROM questions WHERE question_id = ?", (question_id,)).fetchone()
            feedback = {"correct": payload.answer == question["correct_answer"], "correct_answer": question["correct_answer"], "explanation": clean_display_text(question["explanation"]), "solution_steps": clean_display_value(json.loads(question["solution_steps"])), "option_explanations": clean_display_value(json.loads(question["option_explanations"]))}
    return {"saved": True, "attempted": attempted, "feedback": feedback}


@app.post("/api/attempts/{attempt_id}/violations")
def record_exam_violation(attempt_id: str, payload: ExamViolationPayload, request: Request) -> Dict[str, Any]:
    user = require_user(request, "student")
    if payload.violation_type not in EXAM_VIOLATION_LABELS:
        raise HTTPException(400, "Unknown exam violation type.")
    with db() as connection:
        attempt = expire_attempt_if_needed(connection, assert_student_attempt(connection, attempt_id, user["id"]))
        if attempt["status"] != "in_progress" or attempt["mode"] != "faculty" or not attempt["expires_at"]:
            raise HTTPException(400, "Violations can only be recorded during a live proctored assessment.")
        occurred_at = now()
        latest = connection.execute(
            """SELECT violation_type, occurred_at FROM exam_violations
               WHERE attempt_id = ? ORDER BY violation_id DESC LIMIT 1""",
            (attempt_id,),
        ).fetchone()
        if latest and latest["violation_type"] == payload.violation_type:
            elapsed = datetime.now(timezone.utc) - parse_timestamp(latest["occurred_at"])
            if elapsed.total_seconds() < 2:
                count = connection.execute(
                    "SELECT COUNT(*) AS count FROM exam_violations WHERE attempt_id = ?", (attempt_id,)
                ).fetchone()["count"]
                return {"recorded": False, "violation_count": count}
        connection.execute(
            "INSERT INTO exam_violations (attempt_id, violation_type, occurred_at) VALUES (?, ?, ?)",
            (attempt_id, payload.violation_type, occurred_at),
        )
        count = connection.execute(
            "SELECT COUNT(*) AS count FROM exam_violations WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()["count"]
    return {"recorded": True, "violation_count": count}


@app.post("/api/attempts/{attempt_id}/submit")
def submit_attempt(attempt_id: str, payload: SubmitPayload, request: Request) -> Dict[str, Any]:
    user = require_user(request, "student")
    with db() as connection:
        attempt = assert_student_attempt(connection, attempt_id, user["id"])
        attempt = expire_attempt_if_needed(connection, attempt)
        if attempt["status"] == "submitted":
            return result_for_attempt(connection, attempt_id)
        unanswered = connection.execute("SELECT COUNT(*) AS count FROM responses WHERE attempt_id = ? AND selected_answer IS NULL", (attempt_id,)).fetchone()["count"]
        if unanswered and not payload.confirmed:
            return {"requires_confirmation": True, "unanswered": unanswered, "attempted": attempt["total_questions"] - unanswered}
        finalize_attempt(connection, attempt_id)
        return result_for_attempt(connection, attempt_id)


@app.get("/api/attempts/{attempt_id}/result")
def get_result(attempt_id: str, request: Request) -> Dict[str, Any]:
    user = require_user(request, "student")
    with db() as connection:
        attempt = expire_attempt_if_needed(connection, assert_student_attempt(connection, attempt_id, user["id"]))
        if attempt["status"] != "submitted":
            raise HTTPException(400, "Submit the assessment to view results.")
        return result_for_attempt(connection, attempt_id)


@app.post("/api/student/practice/{attempt_id}/retry-incorrect")
def retry_incorrect_practice(attempt_id: str, request: Request) -> Dict[str, Any]:
    user = require_user(request, "student")
    with db() as connection:
        attempt = expire_attempt_if_needed(connection, assert_student_attempt(connection, attempt_id, user["id"]))
        if attempt["mode"] != "student_practice" or attempt["status"] != "submitted":
            raise HTTPException(400, "Only completed personal practice sessions can be retried.")
        selected = connection.execute(
            """SELECT q.question_id, q.category,
                      COALESCE(NULLIF(q.chapter, ''), ?) AS chapter, q.stimulus_id
               FROM responses r JOIN questions q ON q.question_id = r.question_id
               WHERE r.attempt_id = ? AND r.selected_answer IS NOT NULL AND r.correct = 0
               ORDER BY r.question_order""",
            (UNCATEGORIZED_CHAPTER, attempt_id),
        ).fetchall()
        if not selected:
            raise HTTPException(400, "There are no incorrect answers to retry.")
        test_id = connection.execute(
            """INSERT INTO tests
               (test_name, composition, bank_id, created_at, active, launched, mode, owner_student_id)
               VALUES (?, '[]', ?, ?, 0, 0, 'student_practice', ?)""",
            ("Practice · Retry incorrect", attempt["bank_id"], now(), user["id"]),
        ).lastrowid
        new_attempt_id = create_attempt_from_questions(connection, user["id"], test_id, list(selected))
    return {"attempt_id": new_attempt_id}


@app.get("/api/admin/dashboard")
def admin_dashboard(request: Request) -> Dict[str, Any]:
    require_user(request, "admin")
    with db() as connection:
        finalize_expired_attempts(connection)
        totals = connection.execute(
            """SELECT (SELECT COUNT(*) FROM students) AS students, COUNT(a.attempt_id) AS completed,
                      ROUND(COALESCE(AVG(a.percentage), 0), 1) AS average
               FROM attempts a JOIN tests t ON t.test_id = a.test_id
               WHERE a.status = 'submitted' AND t.mode = 'faculty'"""
        ).fetchone()
        students = connection.execute(
            """SELECT s.student_id, s.name, s.class, s.section,
                      COUNT(CASE WHEN t.mode = 'faculty' THEN a.attempt_id END) AS tests,
                      ROUND(COALESCE(AVG(CASE WHEN t.mode = 'faculty' THEN a.percentage END), 0), 1) AS average
               FROM students s LEFT JOIN attempts a ON a.student_id = s.student_id AND a.status = 'submitted'
               LEFT JOIN tests t ON t.test_id = a.test_id
               GROUP BY s.student_id ORDER BY s.name"""
        ).fetchall()
        category = connection.execute(
            """SELECT r.category, ROUND(AVG(r.correct) * 100, 1) AS percentage
               FROM responses r JOIN attempts a ON a.attempt_id = r.attempt_id
               JOIN tests t ON t.test_id = a.test_id
               WHERE a.status = 'submitted' AND t.mode = 'faculty'
               GROUP BY r.category ORDER BY percentage DESC"""
        ).fetchall()
        recent = connection.execute(
            """SELECT a.attempt_id, s.name, s.student_id, t.test_name, a.score, a.total_questions, a.percentage, a.submitted_at,
                      (SELECT COUNT(*) FROM exam_violations ev WHERE ev.attempt_id = a.attempt_id) AS violation_count,
                      (SELECT GROUP_CONCAT(DISTINCT ev.violation_type) FROM exam_violations ev WHERE ev.attempt_id = a.attempt_id) AS violation_types
               FROM attempts a JOIN students s ON s.student_id = a.student_id JOIN tests t ON t.test_id = a.test_id
               WHERE a.status = 'submitted' AND t.mode = 'faculty'
               ORDER BY a.submitted_at DESC"""
        ).fetchall()
    recent_attempts = rows(recent)
    for attempt in recent_attempts:
        types = [item for item in (attempt.pop("violation_types") or "").split(",") if item]
        attempt["violations"] = [EXAM_VIOLATION_LABELS.get(item, item) for item in types]
    return {"totals": dict(totals), "students": rows(students), "category_performance": rows(category), "recent_attempts": recent_attempts}


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
            """SELECT q.question_id, q.question_text, q.category, q.chapter, q.difficulty, q.active,
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
                      b.format_version,
                      COUNT(q.question_id) AS question_count,
                      SUM(q.question_html != '') AS visual_question_count,
                      (SELECT COUNT(*) FROM stimuli s WHERE s.bank_id = b.bank_id) AS stimulus_count,
                      (SELECT COUNT(*) FROM tests t WHERE t.bank_id = b.bank_id) AS test_count
               FROM question_banks b LEFT JOIN questions q ON q.bank_id = b.bank_id
               GROUP BY b.bank_id ORDER BY b.imported_at DESC"""
        ).fetchall()
    return {"banks": rows(banks)}


@app.delete("/api/admin/question-banks/{bank_id}")
def delete_question_bank(bank_id: int, request: Request) -> Dict[str, Any]:
    require_user(request, "admin")
    with db() as connection:
        bank = connection.execute("SELECT bank_name FROM question_banks WHERE bank_id = ?", (bank_id,)).fetchone()
        if not bank:
            raise HTTPException(404, "Question bank not found.")

        deleted_counts = {
            "tests": connection.execute(
                "SELECT COUNT(*) AS count FROM tests WHERE bank_id = ?", (bank_id,)
            ).fetchone()["count"],
            "attempts": connection.execute(
                """SELECT COUNT(*) AS count FROM attempts
                   WHERE test_id IN (SELECT test_id FROM tests WHERE bank_id = ?)""",
                (bank_id,),
            ).fetchone()["count"],
            "responses": connection.execute(
                """SELECT COUNT(*) AS count FROM responses
                   WHERE attempt_id IN (
                     SELECT a.attempt_id FROM attempts a
                     JOIN tests t ON t.test_id = a.test_id
                     WHERE t.bank_id = ?
                   )
                   OR question_id IN (SELECT question_id FROM questions WHERE bank_id = ?)""",
                (bank_id, bank_id),
            ).fetchone()["count"],
            "questions": connection.execute(
                "SELECT COUNT(*) AS count FROM questions WHERE bank_id = ?", (bank_id,)
            ).fetchone()["count"],
            "stimuli": connection.execute(
                "SELECT COUNT(*) AS count FROM stimuli WHERE bank_id = ?", (bank_id,)
            ).fetchone()["count"],
        }

        connection.execute(
            """DELETE FROM exam_violations
               WHERE attempt_id IN (
                 SELECT a.attempt_id FROM attempts a
                 JOIN tests t ON t.test_id = a.test_id
                 WHERE t.bank_id = ?
               )""",
            (bank_id,),
        )
        connection.execute(
            """DELETE FROM responses
               WHERE attempt_id IN (
                 SELECT a.attempt_id FROM attempts a
                 JOIN tests t ON t.test_id = a.test_id
                 WHERE t.bank_id = ?
               )
               OR question_id IN (SELECT question_id FROM questions WHERE bank_id = ?)""",
            (bank_id, bank_id),
        )
        connection.execute(
            "DELETE FROM attempts WHERE test_id IN (SELECT test_id FROM tests WHERE bank_id = ?)",
            (bank_id,),
        )
        connection.execute("DELETE FROM tests WHERE bank_id = ?", (bank_id,))
        connection.execute("DELETE FROM stimuli WHERE bank_id = ?", (bank_id,))
        connection.execute("DELETE FROM questions WHERE bank_id = ?", (bank_id,))
        connection.execute("DELETE FROM question_banks WHERE bank_id = ?", (bank_id,))
    asset_directory = question_assets_dir() / str(bank_id)
    if asset_directory.exists():
        shutil.rmtree(asset_directory)
    return {"deleted": True, "bank_name": bank["bank_name"], "deleted_counts": deleted_counts}


@app.get("/api/admin/question-banks/folder")
def question_bank_folder(request: Request) -> Dict[str, str]:
    require_user(request, "admin")
    return {"path": str(QUESTION_BANKS_DIR)}


@app.get("/api/question-banks/{bank_id}/taxonomy")
def get_question_bank_taxonomy(bank_id: int, request: Request) -> Dict[str, Any]:
    require_user(request)
    with db() as connection:
        return question_bank_taxonomy(connection, bank_id)


@app.get("/api/question-banks/{bank_id}/stimuli/{stimulus_id}")
def get_stimulus_asset(bank_id: int, stimulus_id: str, request: Request) -> FileResponse:
    require_user(request)
    with db() as connection:
        stimulus = connection.execute(
            "SELECT asset_filename FROM stimuli WHERE bank_id = ? AND stimulus_id = ?",
            (bank_id, stimulus_id),
        ).fetchone()
    if not stimulus or not stimulus["asset_filename"]:
        raise HTTPException(404, "Stimulus asset not found.")
    filename = Path(stimulus["asset_filename"]).name
    if filename != stimulus["asset_filename"]:
        raise HTTPException(404, "Stimulus asset not found.")
    asset_path = question_assets_dir() / str(bank_id) / filename
    if not asset_path.is_file():
        raise HTTPException(404, "Stimulus asset not found.")
    return FileResponse(asset_path)


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
    if len(html_bytes) > MAX_LEGACY_FILE_BYTES or len(answer_bytes) > MAX_LEGACY_FILE_BYTES:
        raise HTTPException(413, "Each legacy question-bank file must be under 25 MB.")
    try:
        html_source, answer_key_source = html_bytes.decode("utf-8-sig"), answer_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "Use UTF-8 encoded files for the question-bank pair.")
    bank_name, questions = parse_question_bank(html_source, answer_key_source)
    return save_question_bank(bank_name, questions, html_name, answer_name)


@app.post("/api/admin/question-banks/import-package")
async def import_question_bank_package(
    request: Request,
    package_file: UploadFile = File(...),
) -> Dict[str, Any]:
    require_user(request, "admin")
    package_name = Path(package_file.filename or "").name
    if not package_name.lower().endswith(".zip"):
        raise HTTPException(400, "A v2 question bank must be uploaded as a ZIP file.")
    package_bytes = await package_file.read(MAX_PACKAGE_BYTES + 1)
    if not package_bytes:
        raise HTTPException(400, "Choose a non-empty question-bank package.")
    if len(package_bytes) > MAX_PACKAGE_BYTES:
        raise HTTPException(413, "The compressed question-bank package must be under 50 MB.")
    bank_name, questions, stimuli = parse_v2_package(io.BytesIO(package_bytes))
    return save_v2_question_bank(bank_name, questions, stimuli, package_name)


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
            """SELECT t.*, b.bank_name,
                      (SELECT COUNT(*) FROM attempts a WHERE a.test_id = t.test_id) AS attempt_count
               FROM tests t
               LEFT JOIN question_banks b ON b.bank_id = t.bank_id
               WHERE t.mode = 'faculty' ORDER BY t.created_at DESC"""
        ).fetchall())
        for test in tests:
            test["selection_rules"] = decode_selection_rules(test["composition"])
            test["difficulty_levels"] = decode_difficulties(test["difficulties"])
        banks = rows(connection.execute(
            """SELECT b.bank_id, b.bank_name, COUNT(q.question_id) AS question_count
               FROM question_banks b LEFT JOIN questions q ON q.bank_id = b.bank_id AND q.active = 1
               GROUP BY b.bank_id ORDER BY b.imported_at DESC"""
        ).fetchall())
        return {"tests": tests, "categories": CATEGORIES, "banks": banks}


@app.post("/api/admin/tests")
def create_test(payload: TestPayload, request: Request) -> Dict[str, Any]:
    require_user(request, "admin")
    rules = normalize_selection_rules(payload.selection_rules, payload.composition)
    difficulties = normalize_difficulties(payload.difficulties)
    if not payload.test_name.strip():
        raise HTTPException(400, "Provide a test name.")
    with db() as connection:
        rules = validate_selection_rules(connection, payload.bank_id, rules, MAX_ASSESSMENT_QUESTIONS, difficulties)
        bank = connection.execute("SELECT bank_name FROM question_banks WHERE bank_id = ?", (payload.bank_id,)).fetchone()
        if not bank:
            raise HTTPException(404, "Choose an imported question bank.")
        connection.execute(
            "INSERT INTO tests (test_name, composition, bank_id, created_at, difficulties) VALUES (?, ?, ?, ?, ?)",
            (payload.test_name.strip(), json.dumps(rules), payload.bank_id, now(), json.dumps(difficulties)),
        )
    return {"created": True}


@app.delete("/api/admin/tests/{test_id}")
def delete_test(test_id: int, request: Request) -> Dict[str, Any]:
    require_user(request, "admin")
    with db() as connection:
        test = connection.execute(
            "SELECT test_name FROM tests WHERE test_id = ? AND mode = 'faculty'", (test_id,)
        ).fetchone()
        if not test:
            raise HTTPException(404, "Faculty assessment not found.")
        attempt_ids = [
            row["attempt_id"]
            for row in connection.execute("SELECT attempt_id FROM attempts WHERE test_id = ?", (test_id,)).fetchall()
        ]
        if attempt_ids:
            placeholders = ",".join("?" for _ in attempt_ids)
            connection.execute(f"DELETE FROM exam_violations WHERE attempt_id IN ({placeholders})", attempt_ids)
            connection.execute(f"DELETE FROM responses WHERE attempt_id IN ({placeholders})", attempt_ids)
            connection.execute(f"DELETE FROM attempts WHERE attempt_id IN ({placeholders})", attempt_ids)
        connection.execute("DELETE FROM tests WHERE test_id = ?", (test_id,))
    return {"deleted": True, "test_name": test["test_name"], "attempts_deleted": len(attempt_ids)}


@app.post("/api/admin/tests/{test_id}/launch")
def launch_test(test_id: int, request: Request) -> Dict[str, bool]:
    require_user(request, "admin")
    with db() as connection:
        if not connection.execute(
            "SELECT 1 FROM tests WHERE test_id = ? AND active = 1 AND mode = 'faculty'", (test_id,)
        ).fetchone():
            raise HTTPException(404, "Test not found.")
        connection.execute("UPDATE tests SET launched = 0 WHERE mode = 'faculty'")
        connection.execute("UPDATE tests SET launched = 1 WHERE test_id = ?", (test_id,))
    return {"launched": True}


@app.post("/api/admin/tests/{test_id}/close")
def close_test(test_id: int, request: Request) -> Dict[str, bool]:
    require_user(request, "admin")
    with db() as connection:
        connection.execute("UPDATE tests SET launched = 0 WHERE test_id = ? AND mode = 'faculty'", (test_id,))
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
               JOIN responses r ON r.attempt_id = a.attempt_id
               WHERE a.status = 'submitted' AND t.mode = 'faculty'
               GROUP BY a.attempt_id ORDER BY a.submitted_at DESC"""
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
