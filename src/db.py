"""Turso/SQLite connection, schema, and query helpers.

Phase 1. Connects to Turso when TURSO_DATABASE_URL is set, otherwise to a local
SQLite file so tests and dry runs need no live credentials.

Planned surface:
    connect()                 -> connection handle
    init_schema(conn)         -> idempotent CREATE TABLE IF NOT EXISTS
    insert_quotes(conn, rows) -> int inserted (skips duplicates on quote_text)
    next_unused_quote(conn)   -> row | None  (used_at IS NULL, quality_score DESC)
    record_post(conn, ...)    -> writes the posts row and stamps quotes.used_at
                                 in a single transaction
"""

from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_text TEXT NOT NULL UNIQUE,
    author TEXT,
    book_title TEXT,
    source_page INTEGER,
    quality_score REAL,
    theme TEXT,
    created_at TEXT NOT NULL,
    used_at TEXT
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_id INTEGER NOT NULL REFERENCES quotes(id),
    caption_body TEXT NOT NULL,
    image_path TEXT NOT NULL,
    image_url TEXT NOT NULL,
    instagram_media_id TEXT,
    posted_at TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quotes_unused
    ON quotes (used_at, quality_score DESC);
"""
