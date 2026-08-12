"""Schema tests. Run against a local SQLite file — never live Turso."""

import sqlite3

from src.db import SCHEMA


def test_schema_is_idempotent(tmp_path):
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.executescript(SCHEMA)
    conn.executescript(SCHEMA)  # applying twice must be safe
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"quotes", "posts"} <= tables


def test_quote_text_is_unique(tmp_path):
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO quotes (quote_text, created_at) VALUES (?, ?)",
        ("Trust the glider.", "2026-01-01T00:00:00Z"),
    )
    try:
        conn.execute(
            "INSERT INTO quotes (quote_text, created_at) VALUES (?, ?)",
            ("Trust the glider.", "2026-01-02T00:00:00Z"),
        )
    except sqlite3.IntegrityError:
        pass
    else:  # pragma: no cover
        raise AssertionError("duplicate quote_text should violate the UNIQUE constraint")
