import sqlite3


def _ensure_column(connection: sqlite3.Connection, table: str, definition: str) -> None:
    column = definition.split()[0]
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def migrate_distributed_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS devices (
          device_id TEXT PRIMARY KEY,
          label TEXT NOT NULL,
          public_key_b64 TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active',
          enrolled_at TEXT NOT NULL,
          last_seen_at TEXT
        );
        CREATE TABLE IF NOT EXISTS assessment_releases (
          release_id TEXT PRIMARY KEY,
          test_id INTEGER NOT NULL UNIQUE,
          state TEXT NOT NULL,
          duration_seconds INTEGER NOT NULL,
          manifest_json TEXT NOT NULL,
          content_pack_filename TEXT NOT NULL,
          content_hash TEXT NOT NULL,
          content_signature_b64 TEXT NOT NULL,
          wrapped_content_key_b64 TEXT NOT NULL,
          launch_opens_at TEXT,
          launch_closes_at TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY(test_id) REFERENCES tests(test_id)
        );
        CREATE TABLE IF NOT EXISTS release_questions (
          release_id TEXT NOT NULL,
          question_id INTEGER NOT NULL,
          canonical_order INTEGER NOT NULL,
          options_json TEXT,
          correct_answer TEXT,
          category TEXT,
          chapter TEXT,
          PRIMARY KEY(release_id, question_id),
          UNIQUE(release_id, canonical_order),
          FOREIGN KEY(release_id) REFERENCES assessment_releases(release_id)
        );
        CREATE TABLE IF NOT EXISTS submissions (
          attempt_id TEXT PRIMARY KEY,
          bundle_hash TEXT NOT NULL,
          bundle_json TEXT NOT NULL,
          accepted_at TEXT NOT NULL,
          receipt_json TEXT,
          FOREIGN KEY(attempt_id) REFERENCES attempts(attempt_id)
        );
        CREATE TABLE IF NOT EXISTS audit_events (
          audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
          event_type TEXT NOT NULL,
          actor_id TEXT NOT NULL,
          attempt_id TEXT,
          test_id INTEGER,
          details_json TEXT NOT NULL DEFAULT '{}',
          occurred_at TEXT NOT NULL
        );
        """
    )
    _ensure_column(connection, "tests", "release_id TEXT")
    _ensure_column(connection, "tests", "launch_closes_at TEXT")
    _ensure_column(connection, "attempts", "release_id TEXT")
    _ensure_column(connection, "attempts", "device_id TEXT")
    _ensure_column(connection, "attempts", "order_seed TEXT")
    _ensure_column(connection, "attempts", "ticket_json TEXT")
    _ensure_column(connection, "attempts", "sealed_at TEXT")
    _ensure_column(connection, "attempts", "submission_hash TEXT")
    _ensure_column(connection, "release_questions", "options_json TEXT")
    _ensure_column(connection, "release_questions", "correct_answer TEXT")
    _ensure_column(connection, "release_questions", "category TEXT")
    _ensure_column(connection, "release_questions", "chapter TEXT")
    _ensure_column(connection, "submissions", "receipt_json TEXT")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_assessment_releases_state ON assessment_releases(state)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_attempts_release_student ON attempts(release_id, student_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_audit_events_attempt ON audit_events(attempt_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_audit_events_test ON audit_events(test_id)")
