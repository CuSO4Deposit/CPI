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

Two kinds of entry point, worth knowing apart when picking one:

- Accumulated across every snapshot, because the app discards data over time —
  `watched`, `searches`, `streams`, `feed`.
- Read from the newest snapshot only, because they describe present state rather
  than a log — `subscriptions`, `playlists`, `remote_playlists`. Removing a
  subscription deletes its row, so no export records that it ever existed; walk
  `parser` over `get_files` yourself if you want to see such things come and go.

Aggregation is deliberately absent. Grouping by uploader or histogramming
completion is a `Counter` or a `groupby` at the call site, and wrapping those
would only take flexibility away. What does belong here is anything encoding
PipePipe's own semantics, where a mistake would be silent — the cross-snapshot
deduplication in `watched` being the main example.
"""

from __future__ import annotations

from collections.abc import Iterator

from cuso4d.core.files import Paths, SortKey, get_files
from cuso4d.pipepipe import parser
from cuso4d.pipepipe.models import (
    FeedEntry,
    PlaylistEntry,
    RemotePlaylist,
    Search,
    Service,
    Stream,
    Subscription,
    WatchRecord,
)

__all__ = [
    "FeedEntry",
    "PlaylistEntry",
    "RemotePlaylist",
    "Search",
    "Service",
    "Stream",
    "Subscription",
    "WatchRecord",
    "feed",
    "playlists",
    "remote_playlists",
    "searches",
    "streams",
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


def streams(exports: Paths, key: SortKey | None = None) -> Iterator[Stream]:
    """Every video any snapshot knows about, watched or not.

    Includes videos with no watch history at all — ones reachable only through a
    playlist or a subscription feed, which `watched()` cannot see. That makes this
    the entry point for questions like "what did I save and never get to".

    Yielded once per `(service, url)`, in order of first appearance. Where
    snapshots disagree about a video's metadata the newest wins, since titles get
    edited and view counts climb.
    """
    merged: dict[tuple[int, str], Stream] = {}
    for path in get_files(exports, key=key):
        for stream in parser.streams(path):
            merged[stream.key] = stream
    yield from merged.values()


def feed(exports: Paths, key: SortKey | None = None) -> Iterator[FeedEntry]:
    """Subscription feed entries accumulated across snapshots, deduplicated.

    Accumulated rather than read from the latest snapshot: the app prunes old feed
    rows, so each export only holds a recent window and the union across snapshots
    is strictly more than any one of them.

    An entry with no matching `WatchRecord` is a video that was put in front of you
    and passed over, which is the main reason to look at this.
    """
    seen: set[tuple[int, str, int, str]] = set()
    for path in get_files(exports, key=key):
        for entry in parser.feed(path):
            if entry.key in seen:
                continue
            seen.add(entry.key)
            yield entry


def subscriptions(exports: Paths, key: SortKey | None = None) -> Iterator[Subscription]:
    """Current subscriptions, from the most recent snapshot.

    Present state, not a log: unsubscribing removes the row, so this answers "what
    am I subscribed to now" and not "what have I ever followed". Walk
    `parser.subscriptions` over `get_files` yourself if you want the latter.
    """
    yield from parser.subscriptions(get_files(exports, key=key)[-1])


def remote_playlists(
    exports: Paths, key: SortKey | None = None
) -> Iterator[RemotePlaylist]:
    """Bookmarked service-side playlists, from the most recent snapshot.

    Only references are stored — name, uploader and stream count, never the member
    videos — so a playlist cannot be expanded from an export alone.
    """
    yield from parser.remote_playlists(get_files(exports, key=key)[-1])


def playlists(exports: Paths, key: SortKey | None = None) -> Iterator[PlaylistEntry]:
    """Local playlist contents, from the most recent snapshot. Present state."""
    yield from parser.playlist_entries(get_files(exports, key=key)[-1])
