"""Image selection and its fallback chain."""

import dataclasses
import json
import random

import pytest

from src.pick_image import ImageChoice, NoImagesAvailable, load_manifest, pick_image


@pytest.fixture
def library(tmp_path):
    """A temp repo root with library/ and generated/ directories."""

    class Library:
        def __init__(self):
            self.root = tmp_path
            self.library_dir = tmp_path / "images" / "library"
            self.generated_dir = tmp_path / "images" / "generated"
            self.library_dir.mkdir(parents=True)
            self.generated_dir.mkdir(parents=True)

        def add(self, filename, directory=None):
            (directory or self.library_dir).joinpath(filename).write_bytes(b"fake-jpeg")

        def add_generated(self, filename):
            self.add(filename, self.generated_dir)

        def manifest(self, entries):
            (self.library_dir / "manifest.json").write_text(
                json.dumps({"version": 1, "images": entries}), encoding="utf-8"
            )

        def pick(self, theme, seed=0):
            return pick_image(
                theme,
                library_dir=self.library_dir,
                generated_dir=self.generated_dir,
                repo_root=self.root,
                rng=random.Random(seed),
            )

    return Library()


# --- theme matching ----------------------------------------------------


def test_exact_theme_match_wins(library):
    library.add("fear.jpg")
    library.add("other.jpg")
    library.manifest(
        [
            {"filename": "fear.jpg", "themes": ["fear"], "alt_text": "A pilot on launch."},
            {"filename": "other.jpg", "themes": ["technique"]},
        ]
    )
    choice = library.pick("fear")
    assert choice.source == "theme-match"
    assert choice.path.name == "fear.jpg"
    assert choice.alt_text == "A pilot on launch."


def test_theme_match_is_case_insensitive(library):
    library.add("a.jpg")
    library.manifest([{"filename": "a.jpg", "themes": ["Commitment"]}])
    assert library.pick("  COMMITMENT ").source == "theme-match"


def test_mood_and_site_are_matchable_tags(library):
    library.add("a.jpg")
    library.add("b.jpg")
    library.manifest(
        [
            {"filename": "a.jpg", "themes": ["risk"], "mood": "serene"},
            {"filename": "b.jpg", "themes": ["risk"], "site": "Bir Billing"},
        ]
    )
    assert library.pick("serene").path.name == "a.jpg"
    assert library.pick("bir billing").path.name == "b.jpg"


def test_themes_may_be_a_bare_string(library):
    library.add("a.jpg")
    library.manifest([{"filename": "a.jpg", "themes": "fear"}])
    assert library.pick("fear").source == "theme-match"


def test_relative_path_uses_forward_slashes(library):
    library.add("a.jpg")
    library.manifest([{"filename": "a.jpg", "themes": ["fear"]}])
    assert library.pick("fear").relative_path == "images/library/a.jpg"


# --- fallbacks ---------------------------------------------------------


def test_no_theme_match_falls_back_to_library(library):
    library.add("a.jpg")
    library.manifest([{"filename": "a.jpg", "themes": ["technique"]}])
    choice = library.pick("fear")
    assert choice.source == "library-random"
    assert choice.path.name == "a.jpg"


def test_missing_theme_falls_back_to_library(library):
    library.add("a.jpg")
    library.manifest([{"filename": "a.jpg", "themes": ["technique"]}])
    assert library.pick(None).source == "library-random"


def test_images_without_a_manifest_are_still_usable(library):
    library.add("a.jpg")
    choice = library.pick("fear")
    assert choice.source == "library-random"
    assert choice.alt_text is None


def test_empty_library_falls_back_to_generated(library, caplog):
    library.add_generated("ai.png")
    with caplog.at_level("WARNING"):
        choice = library.pick("fear")
    assert choice.source == "generated-fallback"
    assert "library is empty" in caplog.text.lower()


def test_nothing_available_raises(library):
    with pytest.raises(NoImagesAvailable, match="No usable images"):
        library.pick("fear")


# --- robustness --------------------------------------------------------


def test_manifest_entry_with_missing_file_is_skipped(library, caplog):
    library.add("real.jpg")
    library.manifest(
        [
            {"filename": "ghost.jpg", "themes": ["fear"]},
            {"filename": "real.jpg", "themes": ["technique"]},
        ]
    )
    with caplog.at_level("WARNING"):
        choice = library.pick("fear")
    # ghost.jpg matched the theme but does not exist, so selection degrades.
    assert choice.path.name == "real.jpg"
    assert "ghost.jpg" in caplog.text


def test_malformed_manifest_does_not_fail_the_run(library, caplog):
    library.add("a.jpg")
    (library.library_dir / "manifest.json").write_text("{not json", encoding="utf-8")
    with caplog.at_level("WARNING"):
        choice = library.pick("fear")
    assert choice.source == "library-random"
    assert "not valid json" in caplog.text.lower()


def test_non_image_files_are_ignored(library):
    library.add("notes.txt")
    library.add_generated("ai.png")
    assert library.pick("fear").path.name == "ai.png"


def test_selection_is_deterministic_for_a_given_seed(library):
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        library.add(name)
    library.manifest([{"filename": n, "themes": ["fear"]} for n in ("a.jpg", "b.jpg", "c.jpg")])
    picks = {library.pick("fear", seed=7).path.name for _ in range(5)}
    assert len(picks) == 1


def test_load_manifest_on_missing_file_returns_empty(tmp_path):
    assert load_manifest(tmp_path / "nope.json") == []


def test_image_choice_is_immutable(library):
    library.add("a.jpg")
    choice = library.pick(None)
    assert isinstance(choice, ImageChoice)
    with pytest.raises(dataclasses.FrozenInstanceError):
        choice.path = "elsewhere"
