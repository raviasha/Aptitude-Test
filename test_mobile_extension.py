import importlib
import json
import os
import re
import sys
from pathlib import Path

import app as core


os.environ.setdefault("APTITUDE_MOBILE_APP_SECRET", "test-mobile-secret")
MOBILE_PYTHON = Path(__file__).parent / "android" / "app" / "src" / "main" / "python"
sys.path.insert(0, str(MOBILE_PYTHON))
mobile_api = importlib.import_module("mobile_api")
sys.path.remove(str(MOBILE_PYTHON))


def use_temporary_database(monkeypatch, tmp_path):
    data_dir = tmp_path / "mobile-data"
    monkeypatch.setattr(core, "DATA_DIR", data_dir)
    monkeypatch.setattr(core, "DB_PATH", data_dir / "aptitude.db")
    monkeypatch.setattr(core, "BACKUP_DIR", data_dir / "backups")
    monkeypatch.setattr(core, "QUESTION_BANKS_DIR", data_dir / "Question Banks")
    core.ensure_schema()
    mobile_api.ensure_mobile_schema()


def test_drive_folder_link_parser_accepts_folder_urls_and_ids():
    folder_id = "1AbCdEfGhIjKlMnOpQrStUvWxYz"
    assert mobile_api.folder_id_from_url(folder_id) == folder_id
    assert mobile_api.folder_id_from_url(f"https://drive.google.com/drive/folders/{folder_id}?usp=sharing") == folder_id
    assert mobile_api.folder_id_from_url(f"https://drive.google.com/open?id={folder_id}") == folder_id
    assert mobile_api.folder_id_from_url("https://example.com/not-drive") is None
    assert mobile_api.folder_id_from_url("") is None


def test_mobile_source_generation_removes_desktop_seed_questions():
    source = (Path(__file__).parent / "app.py").read_text(encoding="utf-8")
    seed_pattern = re.compile(
        r"\nQUESTION_SEEDS = \[.*?\n\]\n\n\ndef server_is_already_running",
        re.DOTALL,
    )
    generated = seed_pattern.sub(
        "\nQUESTION_SEEDS = []\n\n\ndef server_is_already_running", source, count=1
    )
    assert generated != source
    assert "What is 15% of 240?" not in generated
    assert "QUESTION_SEEDS = []" in generated


def test_deleting_mobile_bank_preserves_result_snapshot(monkeypatch, tmp_path):
    use_temporary_database(monkeypatch, tmp_path)
    question = {
        "key": "mobile-q-1",
        "question_text": "What is 2 + 2?",
        "question_html": "",
        "category": core.CATEGORIES[0],
        "chapter": "Numbers",
        "stimulus_id": None,
        "difficulty": "Easy",
        "options": {"A": "3", "B": "4", "C": "5", "D": "6"},
        "correct_answer": "B",
        "explanation": "Two plus two is four.",
        "solution_steps": ["Add the two values."],
        "option_explanations": {},
    }
    imported = core.save_v2_question_bank("Mobile sample", [question], [], "mobile-sample.zip")
    bank_id = imported["bank_id"]
    with core.db() as connection:
        connection.execute(
            "INSERT INTO students VALUES (?, ?, ?, ?, ?, ?)",
            (mobile_api.MOBILE_STUDENT_ID, "Student", "google-managed", "Mobile", "", core.now()),
        )
        question_id = connection.execute(
            "SELECT question_id FROM questions WHERE bank_id = ?", (bank_id,)
        ).fetchone()["question_id"]
        test_id = connection.execute(
            """INSERT INTO tests
               (test_name, composition, bank_id, created_at, active, launched, mode, owner_student_id, difficulties)
               VALUES (?, '{}', ?, ?, 1, 0, 'student_practice', ?, '[\"Easy\"]')""",
            ("Mobile sample practice", bank_id, core.now(), mobile_api.MOBILE_STUDENT_ID),
        ).lastrowid
        attempt_id = "mobile-attempt-1"
        connection.execute(
            """INSERT INTO attempts
               (attempt_id, student_id, test_id, started_at, submitted_at, status,
                total_questions, attempted, correct, score, percentage)
               VALUES (?, ?, ?, ?, ?, 'submitted', 1, 1, 1, 1, 100)""",
            (attempt_id, mobile_api.MOBILE_STUDENT_ID, test_id, core.now(), core.now()),
        )
        connection.execute(
            """INSERT INTO responses
               (attempt_id, question_id, selected_answer, correct, category, chapter, question_order)
               VALUES (?, ?, 'B', 1, ?, 'Numbers', 1)""",
            (attempt_id, question_id, core.CATEGORIES[0]),
        )
        connection.execute(
            """INSERT INTO mobile_bank_sources
               (bank_id, remote_file_id, remote_modified_time, remote_size, storage_mode, installed_at)
               VALUES (?, 'drive-file-12345', '2026-08-19T00:00:00Z', 1024, 'permanent', ?)""",
            (bank_id, core.now()),
        )

    result = mobile_api.purge_bank(bank_id)
    assert result["deleted"] is True
    with core.db() as connection:
        assert connection.execute(
            "SELECT 1 FROM question_banks WHERE bank_id = ?", (bank_id,)
        ).fetchone() is None
        snapshot = connection.execute(
            "SELECT result_json FROM mobile_result_snapshots WHERE attempt_id = 'mobile-attempt-1'"
        ).fetchone()
    saved = json.loads(snapshot["result_json"])
    assert saved["attempt"]["score"] == 1
    assert saved["chapters"][0]["chapter"] == "Numbers"
