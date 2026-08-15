"""Database layer. Runs against a local SQLite file — never live Turso."""

import sqlite3

import pytest

from src import db as db_module
from src.db import (
    connect,
    insert_quotes,
    next_unused_quote,
    pool_stats,
    record_failed_post,
    record_successful_post,
    top_unused_quotes,
    unused_quote_count,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A fresh, schema-initialized local database per test."""
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    monkeypatch.setenv("LOCAL_DB_PATH", str(tmp_path / "test.db"))
    with connect() as database:
        database.init_schema()
        yield database


def make_quote(text, score=0.5, theme="risk"):
    return {
        "quote_text": text,
        "author": "A. Author",
        "book_title": "Masterclass",
        "source_page": 42,
        "quality_score": score,
        "theme": theme,
    }


# --- schema ------------------------------------------------------------


def test_init_schema_is_idempotent(db):
    db.init_schema()
    db.init_schema()
    tables = {
        row["name"]
        for row in db.query("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"books", "quotes", "posts"} <= tables


def test_connect_uses_sqlite_without_turso_url(db):
    assert db.describe.startswith("sqlite:")


# --- inserting ---------------------------------------------------------


def test_insert_quotes_returns_insert_count(db):
    assert insert_quotes(db, [make_quote("One."), make_quote("Two.")]) == 2


def test_insert_quotes_skips_exact_duplicates(db):
    insert_quotes(db, [make_quote("Trust the glider.")])
    assert insert_quotes(db, [make_quote("Trust the glider.")]) == 0
    assert unused_quote_count(db) == 1


def test_insert_quotes_ignores_blank_text(db):
    assert insert_quotes(db, [{"quote_text": "   "}, {"quote_text": ""}, {}]) == 0
    assert unused_quote_count(db) == 0


def test_insert_quotes_stores_chapter_and_context_excerpt(db):
    quote = make_quote("Trust the glider.")
    quote["chapter"] = "Valley flow"
    quote["context_excerpt"] = "The full page text surrounding the quote."
    insert_quotes(db, [quote])
    row = db.query_one("SELECT chapter, context_excerpt FROM quotes")
    assert row["chapter"] == "Valley flow"
    assert row["context_excerpt"] == "The full page text surrounding the quote."


def test_insert_quotes_allows_missing_chapter_and_context_excerpt(db):
    insert_quotes(db, [make_quote("No extra metadata.")])
    row = db.query_one("SELECT chapter, context_excerpt FROM quotes")
    assert row["chapter"] is None
    assert row["context_excerpt"] is None


# --- books ---------------------------------------------------------------


def test_insert_quotes_creates_a_book_row(db):
    insert_quotes(db, [make_quote("Trust the glider.")])
    books = db.query("SELECT title, author FROM books")
    assert books == [{"title": "Masterclass", "author": "A. Author"}]


def test_insert_quotes_reuses_the_same_book_across_quotes(db):
    """Seeding a book produces many quotes; they should share one books row,
    not create a duplicate per quote."""
    insert_quotes(db, [make_quote("One."), make_quote("Two."), make_quote("Three.")])
    assert db.query_one("SELECT COUNT(*) AS n FROM books")["n"] == 1


def test_insert_quotes_creates_separate_books_for_different_titles(db):
    other = make_quote("From another book.")
    other["book_title"] = "Second Book"
    other["author"] = "B. Author"
    insert_quotes(db, [make_quote("From the first book."), other])
    assert db.query_one("SELECT COUNT(*) AS n FROM books")["n"] == 2


def test_insert_quotes_without_book_metadata_leaves_quote_unlinked(db):
    insert_quotes(db, [{"quote_text": "No book attached."}])
    assert db.query_one("SELECT COUNT(*) AS n FROM books")["n"] == 0
    assert db.query_one("SELECT book_id FROM quotes")["book_id"] is None


# --- selection ---------------------------------------------------------


def test_selection_prefers_highest_score(db):
    insert_quotes(
        db,
        [make_quote("Low.", 0.2), make_quote("Best.", 0.9), make_quote("Mid.", 0.5)],
    )
    assert next_unused_quote(db)["quote_text"] == "Best."


def test_unscored_quotes_sort_last(db):
    insert_quotes(db, [make_quote("Unscored.", None), make_quote("Scored.", 0.1)])
    assert [q["quote_text"] for q in top_unused_quotes(db, 2)] == ["Scored.", "Unscored."]


def test_selection_is_stable_for_tied_scores(db):
    insert_quotes(db, [make_quote("First.", 0.7), make_quote("Second.", 0.7)])
    for _ in range(3):
        assert next_unused_quote(db)["quote_text"] == "First."


def test_used_quotes_are_never_selected_again(db):
    insert_quotes(db, [make_quote("Only one.", 0.9)])
    quote = next_unused_quote(db)
    record_successful_post(
        db,
        quote_id=quote["id"],
        caption_body="caption",
        image_path="images/library/a.jpg",
        image_url="https://example.com/a.jpg",
        instagram_media_id="17900000000000000",
    )
    assert next_unused_quote(db) is None
    assert unused_quote_count(db) == 0


def test_empty_pool_returns_none(db):
    assert next_unused_quote(db) is None


def test_selected_quote_carries_flat_author_and_book_title(db):
    """generate_post and make_card read quote['author']/quote['book_title']
    directly — the books normalization must not break that shape."""
    insert_quotes(db, [make_quote("Trust the glider.")])
    quote = next_unused_quote(db)
    assert quote["author"] == "A. Author"
    assert quote["book_title"] == "Masterclass"


# --- recording posts ---------------------------------------------------


def test_successful_post_writes_row_and_marks_used(db):
    insert_quotes(db, [make_quote("Commit to the turn.", 0.8)])
    quote = next_unused_quote(db)
    record_successful_post(
        db,
        quote_id=quote["id"],
        caption_body="body",
        image_path="images/library/a.jpg",
        image_url="https://example.com/a.jpg",
        instagram_media_id="ig-123",
    )
    post = db.query_one("SELECT * FROM posts")
    assert post["status"] == "success"
    assert post["instagram_media_id"] == "ig-123"
    assert post["quote_id"] == quote["id"]
    assert db.query_one("SELECT used_at FROM quotes")["used_at"] is not None


def test_failed_post_does_not_burn_the_quote(db):
    insert_quotes(db, [make_quote("Still available.", 0.8)])
    quote = next_unused_quote(db)
    record_failed_post(
        db,
        quote_id=quote["id"],
        caption_body="body",
        image_path="images/library/a.jpg",
        image_url="https://example.com/a.jpg",
    )
    assert db.query_one("SELECT status FROM posts")["status"] == "failed"
    assert next_unused_quote(db)["id"] == quote["id"]


def test_post_and_used_at_are_written_atomically(db, monkeypatch):
    """If the UPDATE fails, the INSERT must roll back with it.

    Otherwise a partial write leaves a 'success' post row for a quote still
    sitting in the pool, and the quote gets posted twice.
    """
    insert_quotes(db, [make_quote("Atomic.", 0.8)])
    quote = next_unused_quote(db)

    original = db_module._SqliteBackend.atomic

    def fail_on_second_statement(self, statements):
        broken = list(statements)
        broken[1] = ("UPDATE quotes SET used_at = ? WHERE no_such_column = ?", [1, 2])
        return original(self, broken)

    monkeypatch.setattr(db_module._SqliteBackend, "atomic", fail_on_second_statement)

    with pytest.raises(sqlite3.OperationalError):
        record_successful_post(
            db,
            quote_id=quote["id"],
            caption_body="body",
            image_path="images/library/a.jpg",
            image_url="https://example.com/a.jpg",
            instagram_media_id="ig-123",
        )

    assert db.query("SELECT * FROM posts") == []
    assert next_unused_quote(db)["id"] == quote["id"]


# --- stats -------------------------------------------------------------


def test_pool_stats_summarizes_scores(db):
    insert_quotes(db, [make_quote("A.", 0.9), make_quote("B.", 0.5), make_quote("C.", 0.1)])
    stats = pool_stats(db)
    assert stats["total"] == 3
    assert stats["unused"] == 3
    assert stats["min_score"] == pytest.approx(0.1)
    assert stats["max_score"] == pytest.approx(0.9)
    assert stats["avg_score"] == pytest.approx(0.5)
    assert sum(bucket["n"] for bucket in stats["buckets"]) == 3


def test_pool_stats_on_empty_database(db):
    stats = pool_stats(db)
    assert stats["total"] == 0
    assert stats["unused"] == 0
    assert stats["buckets"] == []
