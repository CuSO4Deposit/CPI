"""PipePipe: watch history, searches and subscriptions.

The full view is assembled at runtime by walking archived exports in filename
order. Nothing is persisted; a parsing mistake is a code change, never a corrupted
archive.

Why snapshots matter here: PipePipe keeps one `stream_history` row per video and
overwrites its `access_date` on every play, so the app itself only ever knows the
*last* time you watched something. Successive archived exports are the only record
of the times before that.

Every entry point takes the exports to read, so a caller decides where its data
lives and nothing depends on ambient state:

    >>> from cuso4d.pipepipe import watched
    >>> for rec in watched("~/archives/pipepipe/*.zip"):
    ...     print(rec.last_watched, rec.repeat_count, rec.stream.title)
"""

from __future__ import annotations

from collections.abc import Iterator

from cuso4d.core.files import Paths, SortKey, get_files
from cuso4d.pipepipe import parser
from cuso4d.pipepipe.models import (
    PlaylistEntry,
    Search,
    Service,
    Stream,
    Subscription,
    WatchRecord,
)

__all__ = [
    "PlaylistEntry",
    "Search",
    "Service",
    "Stream",
    "Subscription",
    "WatchRecord",
    "playlists",
    "searches",
    "subscriptions",
    "watched",
    "watched_snapshots",
]


def watched_snapshots(
    exports: Paths, key: SortKey | None = None
) -> Iterator[WatchRecord]:
    """Every history row from every snapshot, without deduplication.

    Use this when you care about how the record itself changed over time.
    """
    for path in get_files(exports, key=key):
        yield from parser.watched(path)


def watched(exports: Paths, key: SortKey | None = None) -> Iterator[WatchRecord]:
    """Watch history accumulated across snapshots, oldest observation first.

    Each `(service, url, last_watched)` combination is emitted once. Because
    PipePipe overwrites `access_date` in place, watching a video again before the
    next export shows up as a *new* record with a later timestamp — which is how a
    timeline gets recovered from lossy snapshots.

    Two caveats worth knowing before counting anything:

    - Plays that happened between two exports are invisible. `repeat_count` says
      how many were missed, but not when they were.
    - The same video therefore yields several records, one per distinct
      `last_watched`. The number of records is not a number of views; the highest
      `repeat_count` is.
    """
    seen: set[tuple[int, str, int]] = set()
    for record in watched_snapshots(exports, key=key):
        dedup_key = (*record.stream.key, record.last_watched_ms)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        yield record


def searches(exports: Paths, key: SortKey | None = None) -> Iterator[Search]:
    """Search history accumulated across snapshots, deduplicated."""
    seen: set[tuple[int, str, int]] = set()
    for path in get_files(exports, key=key):
        for search in parser.searches(path):
            if search.key in seen:
                continue
            seen.add(search.key)
            yield search


def subscriptions(exports: Paths, key: SortKey | None = None) -> Iterator[Subscription]:
    """Current subscriptions, from the most recent snapshot."""
    yield from parser.subscriptions(get_files(exports, key=key)[-1])


def playlists(exports: Paths, key: SortKey | None = None) -> Iterator[PlaylistEntry]:
    """Local playlist contents, from the most recent snapshot."""
    yield from parser.playlist_entries(get_files(exports, key=key)[-1])
