from __future__ import annotations

from pathlib import Path

import pytest

from cuso4d.core.files import get_files


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def test_glob_is_sorted_chronologically(tmp_path: Path) -> None:
    # Created out of order on purpose: ordering must come from the name, not mtime.
    # These are PipePipe's real export names, which sort chronologically.
    for name in [
        "PipePipeData-20260730_120000.zip",
        "PipePipeData-20240101_000000.zip",
        "PipePipeData-20250615_093000.zip",
    ]:
        touch(tmp_path / name)

    found = get_files(str(tmp_path / "*.zip"))
    assert [p.name for p in found] == [
        "PipePipeData-20240101_000000.zip",
        "PipePipeData-20250615_093000.zip",
        "PipePipeData-20260730_120000.zip",
    ]


def test_custom_key_overrides_name_order(tmp_path: Path) -> None:
    # A source whose exports aren't named in sortable order supplies its own key.
    for name in ["b-1.db", "a-2.db"]:
        touch(tmp_path / name)

    found = get_files(tmp_path, key=lambda p: p.name.split("-")[1])
    assert [p.name for p in found] == ["b-1.db", "a-2.db"]


def test_directory_and_single_file(tmp_path: Path) -> None:
    a = touch(tmp_path / "a.db")
    touch(tmp_path / "sub" / "b.db")

    assert get_files(tmp_path) == (a,)  # non-recursive by default
    assert get_files(a) == (a,)
    assert len(get_files(str(tmp_path / "**" / "*.db"))) == 2


def test_deduplicates_overlapping_inputs(tmp_path: Path) -> None:
    a = touch(tmp_path / "a.db")
    assert get_files([a, a, str(tmp_path / "*.db")]) == (a,)


def test_missing_paths_are_loud(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        get_files(tmp_path / "nope.db")
    with pytest.raises(FileNotFoundError):
        get_files(str(tmp_path / "*.zip"))
