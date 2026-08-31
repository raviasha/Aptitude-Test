import sqlite3
from pathlib import Path


def connect_sqlite(path: Path, *, wal: bool = True) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=10.0, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    if wal:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
    return connection
