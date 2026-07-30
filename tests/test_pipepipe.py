from __future__ import annotations

import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import make_export

from cuso4d import pipepipe
from cuso4d.pipepipe import parser
from cuso4d.pipepipe.models import Service

MAR = 1711179872273  # 2024-03-23T07:44:32Z, from a real export
JUL = 1785117189717  # 2026-07-27T01:53:09Z


def test_parses_a_snapshot(tmp_path: Path) -> None:
    db = make_export(
        tmp_path / "pipepipe-20260101T000000Z.db",
        streams=[(1, 5, "https://b.tv/BV1", "video one", 600)],
        history=[(1, MAR, 3)],
        progress=[(1, 300_000)],
        search=[(MAR, 0, "python asyncio")],
    )

    (rec,) = list(parser.watched(db))
    assert rec.stream.url == "https://b.tv/BV1"
    assert rec.stream.service is Service.BILIBILI
    assert rec.repeat_count == 3
    assert rec.last_watched == datetime(2024, 3, 23, 7, 44, 32, 273000, tzinfo=UTC)
    assert rec.progress_ratio == 0.5
    assert not rec.marked_only
    assert rec.snapshot == "pipepipe-20260101T000000Z.db"
    assert rec.schema_version == 901

    (search,) = list(parser.searches(db))
    assert search.query == "python asyncio"


def test_repeat_count_zero_means_merely_marked(tmp_path: Path) -> None:
    # PipePipe's markAsWatched writes repeat_count 0; it is not "watched 0 times".
    db = make_export(
        tmp_path / "e.db",
        streams=[(1, 0, "https://yt/x", "t", 100)],
        history=[(1, MAR, 0)],
    )
    (rec,) = list(parser.watched(db))
    assert rec.marked_only


@pytest.mark.parametrize("placeholder", [0, -1])
def test_duration_unknown_for_live_streams(tmp_path: Path, placeholder: int) -> None:
    """Live streams store 0 or -1, which must not read as a real length.

    Summing `duration_s` blindly made the reference export report a negative
    total number of hours for LIVE_STREAM.
    """
    db = make_export(
        tmp_path / f"e{placeholder}.db",
        streams=[(1, 0, "https://yt/x", "t", placeholder)],
        history=[(1, MAR, 1)],
        progress=[(1, 5000)],
    )
    (rec,) = list(parser.watched(db))
    assert rec.stream.duration_s == placeholder  # raw column still available
    assert rec.stream.duration is None
    assert rec.progress_ratio is None


def test_duration_is_a_timedelta_when_known(tmp_path: Path) -> None:
    db = make_export(
        tmp_path / "e.db",
        streams=[(1, 0, "https://yt/x", "t", 661)],
        history=[(1, MAR, 1)],
    )
    (rec,) = list(parser.watched(db))
    assert rec.stream.duration == timedelta(seconds=661)


def test_progress_beyond_duration_is_clamped(tmp_path: Path) -> None:
    """The player rounds, so a finished video can report one second too many."""
    db = make_export(
        tmp_path / "e.db",
        streams=[(1, 5, "https://b.tv/x", "t", 79)],
        history=[(1, MAR, 1)],
        progress=[(1, 80_000)],
    )
    (rec,) = list(parser.watched(db))
    assert rec.progress_ratio == 1.0


def test_reads_export_from_zip(tmp_path: Path) -> None:
    db = make_export(
        tmp_path / "newpipe.db",
        streams=[(1, 5, "https://b.tv/BV1", "t", 60)],
        history=[(1, MAR, 1)],
    )
    archive = tmp_path / "pipepipe-20260101T000000Z.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(db, "newpipe.db")
        zf.writestr("newpipe.settings", "irrelevant")

    (rec,) = list(parser.watched(archive))
    assert rec.stream.url == "https://b.tv/BV1"
    assert rec.snapshot == archive.name


def test_uid_reuse_across_snapshots_does_not_confuse_streams(tmp_path: Path) -> None:
    """The Chrome-catastrophe case: uid 1 means a different video after a reinstall.

    Identity is (service_id, url), so the two must stay distinct rather than one
    silently inheriting the other's history.
    """
    old = make_export(
        tmp_path / "pipepipe-20240101T000000Z.db",
        streams=[(1, 5, "https://b.tv/OLD", "old video", 60)],
        history=[(1, MAR, 1)],
    )
    new = make_export(
        tmp_path / "pipepipe-20260101T000000Z.db",
        streams=[(1, 5, "https://b.tv/NEW", "new video", 60)],
        history=[(1, JUL, 1)],
    )

    records = [r for path in (old, new) for r in parser.watched(path)]
    assert {r.stream.url for r in records} == {"https://b.tv/OLD", "https://b.tv/NEW"}
    by_url = {r.stream.url: r for r in records}
    assert by_url["https://b.tv/OLD"].last_watched_ms == MAR
    assert by_url["https://b.tv/NEW"].last_watched_ms == JUL


def test_accumulation_recovers_a_timeline(tmp_path: Path) -> None:
    """Successive snapshots turn an overwritten access_date into two observations."""
    first = make_export(
        tmp_path / "pipepipe-20240401T000000Z.db",
        streams=[(1, 5, "https://b.tv/BV1", "t", 60)],
        history=[(1, MAR, 1)],
    )
    second = make_export(
        tmp_path / "pipepipe-20260728T000000Z.db",
        streams=[(7, 5, "https://b.tv/BV1", "t", 60)],  # uid differs, url does not
        history=[(7, JUL, 4)],
    )

    seen: set[tuple[int, str, int]] = set()
    out = []
    for path in (first, second):
        for rec in parser.watched(path):
            key = (*rec.stream.key, rec.last_watched_ms)
            if key not in seen:
                seen.add(key)
                out.append(rec)

    assert [r.last_watched_ms for r in out] == [MAR, JUL]
    # Three of the four plays happened between exports and are unrecoverable.
    assert out[-1].repeat_count == 4


def test_watched_accumulates_across_a_glob(tmp_path: Path) -> None:
    """The public entry point takes paths, so this needs no ambient config."""
    make_export(
        tmp_path / "PipePipeData-20240401_000000.db",
        streams=[(1, 5, "https://b.tv/BV1", "t", 60)],
        history=[(1, MAR, 1)],
    )
    make_export(
        tmp_path / "PipePipeData-20260728_000000.db",
        streams=[(7, 5, "https://b.tv/BV1", "t", 60)],  # uid differs, url does not
        history=[(7, JUL, 4)],
    )

    records = list(pipepipe.watched(str(tmp_path / "PipePipeData-*.db")))
    assert [r.last_watched_ms for r in records] == [MAR, JUL]

    # Same data, undeduplicated, is what watched_snapshots is for.
    raw = list(pipepipe.watched_snapshots(tmp_path))
    assert len(raw) == 2


def test_watched_deduplicates_an_unchanged_record(tmp_path: Path) -> None:
    """Two exports with no watching in between must not double-count."""
    for name in ("PipePipeData-20240401_000000.db", "PipePipeData-20240501_000000.db"):
        make_export(
            tmp_path / name,
            streams=[(1, 5, "https://b.tv/BV1", "t", 60)],
            history=[(1, MAR, 1)],
        )

    assert len(list(pipepipe.watched(tmp_path))) == 1
    assert len(list(pipepipe.watched_snapshots(tmp_path))) == 2


def test_records_carry_their_snapshot_version(tmp_path: Path) -> None:
    """Provenance travels with each record, so an odd row can be traced back."""
    make_export(
        tmp_path / "PipePipeData-20240401_000000.db",
        streams=[(1, 5, "https://b.tv/BV1", "t", 60)],
        history=[(1, MAR, 1)],
        user_version=901,
    )
    make_export(
        tmp_path / "PipePipeData-20260728_000000.db",
        streams=[(1, 5, "https://b.tv/BV1", "t", 60)],
        history=[(1, JUL, 2)],
        user_version=907,
    )

    versions = {r.last_watched_ms: r.schema_version for r in pipepipe.watched(tmp_path)}
    assert versions == {MAR: 901, JUL: 907}


def test_streams_includes_never_watched(tmp_path: Path) -> None:
    """A video with no history row is invisible to watched() but must show up here."""
    make_export(
        tmp_path / "PipePipeData-20260101_000000.db",
        streams=[
            (1, 5, "https://b.tv/SEEN", "watched", 60),
            (2, 5, "https://b.tv/UNSEEN", "saved for later", 60),
        ],
        history=[(1, MAR, 1)],
    )

    assert [r.stream.url for r in pipepipe.watched(tmp_path)] == ["https://b.tv/SEEN"]
    assert {s.url for s in pipepipe.streams(tmp_path)} == {
        "https://b.tv/SEEN",
        "https://b.tv/UNSEEN",
    }


def test_streams_takes_metadata_from_newest_snapshot(tmp_path: Path) -> None:
    """Titles get edited and view counts climb, so the latest snapshot wins."""
    make_export(
        tmp_path / "PipePipeData-20240401_000000.db",
        streams=[(1, 5, "https://b.tv/BV1", "old title", 60)],
        history=[],
    )
    make_export(
        tmp_path / "PipePipeData-20260728_000000.db",
        streams=[(1, 5, "https://b.tv/BV1", "new title", 60)],
        history=[],
    )

    (stream,) = list(pipepipe.streams(tmp_path))
    assert stream.title == "new title"


def test_feed_accumulates_because_the_app_prunes_it(tmp_path: Path) -> None:
    """Each export holds only a recent window, so the union beats any one of them."""
    subs = [(10, 5, "https://b.tv/user/1", "a channel")]
    make_export(
        tmp_path / "PipePipeData-20240401_000000.db",
        streams=[(1, 5, "https://b.tv/OLD", "old", 60)],
        history=[],
        subscriptions=subs,
        feed=[(1, 10)],
    )
    make_export(
        tmp_path / "PipePipeData-20260728_000000.db",
        streams=[(2, 5, "https://b.tv/NEW", "new", 60)],
        history=[],
        subscriptions=subs,
        feed=[(2, 10)],  # the old entry has been pruned
    )

    entries = list(pipepipe.feed(tmp_path))
    assert {e.stream.url for e in entries} == {"https://b.tv/OLD", "https://b.tv/NEW"}
    assert {e.subscription.name for e in entries} == {"a channel"}


def test_remote_playlists_from_latest_snapshot(tmp_path: Path) -> None:
    make_export(
        tmp_path / "PipePipeData-20240401_000000.db",
        streams=[],
        history=[],
        remote_playlists=[(5, "gone since", "https://b.tv/p/1", "someone", 3)],
    )
    make_export(
        tmp_path / "PipePipeData-20260728_000000.db",
        streams=[],
        history=[],
        remote_playlists=[(0, "a course", "https://yt/playlist?list=X", "prof", 37)],
    )

    (playlist,) = list(pipepipe.remote_playlists(tmp_path))
    assert playlist.name == "a course"
    assert playlist.stream_count == 37
    assert playlist.service is Service.YOUTUBE


def test_real_export_upholds_the_row_per_video_assumption(real_export: Path) -> None:
    """The one thing about upstream that this module is not free to be wrong about.

    `watched()` treats a history row as "last watch + total count" rather than a
    single view. If a release ever starts appending a row per play, that reading
    silently understates everything, so assert it against whatever real export is
    available.
    """
    records = list(parser.watched(real_export))
    if not records:
        pytest.skip("export has no watch history")

    assert len({r.stream.key for r in records}) == len(records)
    assert records == sorted(records, key=lambda r: r.last_watched_ms)
    assert all(r.repeat_count >= 0 for r in records)


def test_real_export_is_internally_consistent(real_export: Path) -> None:
    """Cross-table invariants, on whatever export is at hand."""
    streams = list(parser.streams(real_export))
    stream_keys = {s.key for s in streams}
    assert len(stream_keys) == len(streams)  # (service_id, url) is unique

    # Everything reachable another way is a stream; history is a subset of it.
    assert {r.stream.key for r in parser.watched(real_export)} <= stream_keys
    assert {e.stream.key for e in parser.feed(real_export)} <= stream_keys
    assert {e.stream.key for e in parser.playlist_entries(real_export)} <= stream_keys

    # A placeholder duration must never surface as a real length.
    for s in streams:
        assert (s.duration is None) == (s.duration_s <= 0)
