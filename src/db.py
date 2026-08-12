"""Database access: Turso in CI, a local SQLite file everywhere else.

Both backends speak the same four operations (`script`, `query`, `execute`,
`atomic`) so the query helpers below don't care which one is live. Turso is used
when TURSO_DATABASE_URL is set; otherwise everything falls back to a SQLite file
at LOCAL_DB_PATH, which is what lets the test suite run without credentials.

Initialize a fresh database with:

    python -m src.db init
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from typing import Any

from .config import Config, load_config

Row = dict[str, Any]
Statement = tuple[str, "list[Any]"]

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

CREATE INDEX IF NOT EXISTS idx_posts_quote
    ON posts (quote_id);
"""


def utcnow_iso() -> str:
    """Current UTC time as ISO8601 — the format every timestamp column uses."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _split_statements(script: str) -> list[str]:
    """Split a schema script into individual statements.

    libsql has no `executescript`. The schema is plain CREATE statements with no
    semicolons inside string literals or triggers, so splitting on `;` is safe
    here — it would not be for arbitrary SQL.
    """
    return [stmt.strip() for stmt in script.split(";") if stmt.strip()]


class _SqliteBackend:
    """Local SQLite file. Used for tests, dry runs, and offline development."""

    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    def script(self, sql: str) -> None:
        self._conn.executescript(sql)
        self._conn.commit()

    def query(self, sql: str, params: list[Any] | None = None) -> list[Row]:
        cursor = self._conn.execute(sql, tuple(params or ()))
        return [dict(row) for row in cursor.fetchall()]

    def execute(self, sql: str, params: list[Any] | None = None) -> int:
        with self._conn:
            cursor = self._conn.execute(sql, tuple(params or ()))
        return cursor.rowcount

    def atomic(self, statements: list[Statement]) -> None:
        with self._conn:  # commits on success, rolls back on exception
            for sql, params in statements:
                self._conn.execute(sql, tuple(params))

    def close(self) -> None:
        self._conn.close()


class _LibsqlBackend:
    """Remote Turso database over libsql."""

    def __init__(self, url: str, auth_token: str) -> None:
        import libsql_client  # imported lazily — local runs never need it

        self._lib = libsql_client
        self._client = libsql_client.create_client_sync(url=url, auth_token=auth_token or None)

    def script(self, sql: str) -> None:
        for statement in _split_statements(sql):
            self._client.execute(statement)

    def query(self, sql: str, params: list[Any] | None = None) -> list[Row]:
        result = self._client.execute(sql, list(params or []))
        return [row.asdict() for row in result.rows]

    def execute(self, sql: str, params: list[Any] | None = None) -> int:
        result = self._client.execute(sql, list(params or []))
        return result.rows_affected

    def atomic(self, statements: list[Statement]) -> None:
        # libsql `batch` runs every statement in one transaction and rolls the
        # whole thing back if any of them fails.
        self._client.batch(
            [self._lib.Statement(sql, list(params)) for sql, params in statements]
        )

    def close(self) -> None:
        self._client.close()


class Database:
    """Thin façade over whichever backend is configured."""

    def __init__(self, backend: _SqliteBackend | _LibsqlBackend, describe: str) -> None:
        self._backend = backend
        self.describe = describe

    def init_schema(self) -> None:
        """Create tables and indexes. Safe to run repeatedly."""
        self._backend.script(SCHEMA)

    def query(self, sql: str, params: list[Any] | None = None) -> list[Row]:
        return self._backend.query(sql, params)

    def query_one(self, sql: str, params: list[Any] | None = None) -> Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: list[Any] | None = None) -> int:
        """Run a write. Returns the number of rows affected."""
        return self._backend.execute(sql, params)

    def atomic(self, statements: list[Statement]) -> None:
        self._backend.atomic(statements)

    def close(self) -> None:
        self._backend.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def connect(config: Config | None = None) -> Database:
    """Open the configured database."""
    config = config or load_config()
    if config.use_turso:
        backend = _LibsqlBackend(config.turso_database_url, config.turso_auth_token)
        return Database(backend, f"turso:{config.turso_database_url}")
    return Database(_SqliteBackend(config.local_db_path), f"sqlite:{config.local_db_path}")


# --- Quote pool --------------------------------------------------------


def insert_quotes(db: Database, quotes: list[dict[str, Any]]) -> int:
    """Insert candidate quotes, skipping any whose text is already present.

    Returns the number actually inserted. Deduplication relies on the UNIQUE
    constraint on quote_text, so exact repeats are free; near-duplicates are the
    caller's problem (seed_quotes filters those before it gets here).
    """
    created_at = utcnow_iso()
    inserted = 0
    for quote in quotes:
        text = (quote.get("quote_text") or "").strip()
        if not text:
            continue
        affected = db.execute(
            """
            INSERT OR IGNORE INTO quotes
                (quote_text, author, book_title, source_page, quality_score, theme, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                text,
                quote.get("author"),
                quote.get("book_title"),
                quote.get("source_page"),
                quote.get("quality_score"),
                quote.get("theme"),
                created_at,
            ],
        )
        # INSERT OR IGNORE reports 0 rows affected when the text already
        # existed. lastrowid is not usable here — it keeps its previous value
        # on a skipped insert rather than resetting.
        if affected:
            inserted += 1
    return inserted


def next_unused_quote(db: Database) -> Row | None:
    """Highest-scoring quote that has never been posted."""
    quotes = top_unused_quotes(db, limit=1)
    return quotes[0] if quotes else None


def top_unused_quotes(db: Database, limit: int) -> list[Row]:
    """The `limit` best unused quotes, best first.

    SQLite sorts NULL below every number, so unscored quotes land at the back
    under DESC. The id tie-break keeps the order stable across runs.
    """
    return db.query(
        """
        SELECT * FROM quotes
        WHERE used_at IS NULL
        ORDER BY quality_score DESC, id ASC
        LIMIT ?
        """,
        [limit],
    )


def unused_quote_count(db: Database) -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM quotes WHERE used_at IS NULL")
    return int(row["n"]) if row else 0


def pool_stats(db: Database) -> dict[str, Any]:
    """Counts and score distribution, for the seed-step summary."""
    totals = db.query_one(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN used_at IS NULL THEN 1 ELSE 0 END) AS unused,
            MIN(quality_score) AS min_score,
            AVG(quality_score) AS avg_score,
            MAX(quality_score) AS max_score
        FROM quotes
        """
    ) or {}
    buckets = db.query(
        """
        SELECT
            CAST(quality_score * 10 AS INTEGER) / 10.0 AS bucket,
            COUNT(*) AS n
        FROM quotes
        WHERE quality_score IS NOT NULL
        GROUP BY bucket
        ORDER BY bucket DESC
        """
    )
    return {
        "total": totals.get("total") or 0,
        "unused": totals.get("unused") or 0,
        "min_score": totals.get("min_score"),
        "avg_score": totals.get("avg_score"),
        "max_score": totals.get("max_score"),
        "buckets": buckets,
    }


# --- Posts -------------------------------------------------------------


def record_successful_post(
    db: Database,
    *,
    quote_id: int,
    caption_body: str,
    image_path: str,
    image_url: str,
    instagram_media_id: str,
) -> None:
    """Write the posts row and mark the quote used, atomically.

    Both statements land together or neither does — a half-applied write would
    either burn a quote with no post to show for it, or leave a published quote
    eligible to be posted again.
    """
    now = utcnow_iso()
    db.atomic(
        [
            (
                """
                INSERT INTO posts
                    (quote_id, caption_body, image_path, image_url,
                     instagram_media_id, posted_at, status)
                VALUES (?, ?, ?, ?, ?, ?, 'success')
                """,
                [quote_id, caption_body, image_path, image_url, instagram_media_id, now],
            ),
            (
                "UPDATE quotes SET used_at = ? WHERE id = ?",
                [now, quote_id],
            ),
        ]
    )


def record_failed_post(
    db: Database,
    *,
    quote_id: int,
    caption_body: str,
    image_path: str,
    image_url: str,
) -> None:
    """Log a failed attempt. Deliberately does not touch quotes.used_at, so the
    quote stays in the pool for tomorrow."""
    db.execute(
        """
        INSERT INTO posts
            (quote_id, caption_body, image_path, image_url, posted_at, status)
        VALUES (?, ?, ?, ?, ?, 'failed')
        """,
        [quote_id, caption_body, image_path, image_url, utcnow_iso()],
    )


def _main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] != "init":
        print("usage: python -m src.db init", file=sys.stderr)
        return 2
    with connect() as db:
        db.init_schema()
        stats = pool_stats(db)
        print(f"Schema ready on {db.describe}")
        print(f"Quotes: {stats['total']} total, {stats['unused']} unused")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
