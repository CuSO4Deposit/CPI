"""Firefox: browsing history, bookmarks, and per-page engagement.

Reads archived copies of `places.sqlite`. Nothing is persisted; the full view is
assembled at runtime by walking snapshots in time order.

    >>> from cuso4d.firefox import visits
    >>> for visit in visits("~/archives/*/firefox/places-*.sqlite.xz"):
    ...     print(visit.when, visit.visit_type, visit.url)

Why snapshots matter here is Firefox's own expiration. Visits and engagement rows
are immutable once written — unlike PipePipe, a single snapshot holds a real
timeline — but Firefox deletes old ones on its own schedule, engagement sooner than
history. In one reference pair, history reached back 288 days while engagement
reached only 134. Old snapshots are the only record of the difference, so
accumulation is a union by natural key, as with Gadgetbridge, rather than a replay.

This is the one CPI source that is genuinely multi-machine, which changes two
things. Ids are not identity: `moz_places.id` is a per-profile autoincrement, and
across two reference snapshots 11093 ids appear in both while exactly one refers to
the same URL. Even `guid` is profile-local — those two snapshots share no page guid
at all, and only the six bookmark tree roots. So identity is `(host, url)`, the host
comes from the filename, and every record carries a `Snapshot` naming its machine.

Two kinds of entry point, worth knowing apart:

- Accumulated across every snapshot, because Firefox expires rows — `visits`,
  `engagement`, `downloads`, and `pages` (merged, newest snapshot winning, since
  titles get rewritten and counts climb).
- Read from each host's newest snapshot, because they describe present state rather
  than a log — `bookmarks`, `origins`, `typed_inputs`. Deleting a bookmark leaves no
  trace, so no snapshot records that it existed. Note *each host's* newest, not the
  newest overall: one machine's exports must not shadow another's bookmarks.

Aggregation is deliberately absent, as in every CPI module. Grouping visits by day
or summing view time is a `Counter` or a `groupby` at the call site. What belongs
here is what encodes Firefox's own semantics, where a mistake would be silent — the
three timestamp units, `visit_count` excluding reloads and downloads, and the
frecency sentinel. See `models` and `parser`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from cuso4d.core.files import Paths, get_files
from cuso4d.firefox import parser
from cuso4d.firefox.models import (
    ROOT_GUIDS,
    UNCOUNTED_VISIT_TYPES,
    Bookmark,
    BookmarkType,
    DocumentType,
    Download,
    Engagement,
    Origin,
    Page,
    Snapshot,
    TypedInput,
    Visit,
    VisitSource,
    VisitType,
)

#: Archived snapshots are named `places-<hostname>-<ISO8601>Z.sqlite.xz`.
GLOB = "places-*.sqlite.xz"

#: Filename pattern, anchored on the timestamp rather than on the host.
#:
#: The host is whatever sits between the prefix and the timestamp, matched greedily
#: because a hostname contains dashes of its own — `nightcord-lexikos` is one host,
#: not a host and a profile. Splitting on dashes from the left would silently take
#: `nightcord` as the machine and merge two machines' histories into one.
FILENAME = re.compile(r"^places-(?P<host>.+)-(?P<stamp>\d{8}T\d{6}Z)\.sqlite(?:\.xz)?$")

_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"


def snapshot_of(path: Path) -> Snapshot:
    """Read provenance out of an archived snapshot's filename.

    Raises `ValueError` on a name that does not match `FILENAME`. Deliberately
    strict: the host decides record identity and the timestamp decides accumulation
    order, so guessing either from a name that does not fit would corrupt the merge
    quietly. mtime is not a fallback — it does not survive syncing between machines.
    """
    match = FILENAME.match(path.name)
    if match is None:
        raise ValueError(
            f"not a Firefox snapshot filename: {path.name!r} "
            f"(expected places-<host>-<YYYYmmddTHHMMSSZ>.sqlite.xz)"
        )
    stamp = datetime.strptime(match["stamp"], _STAMP_FORMAT).replace(tzinfo=UTC)
    return Snapshot(taken_at=stamp, host=match["host"], filename=path.name)


def sort_key(path: Path) -> tuple[datetime, str, str]:
    """Order snapshots by when they were taken, then host, then name.

    `get_files` sorts by filename by default, which is chronological only for an
    exporter that puts the timestamp first. Firefox's puts the host first, so
    filename order groups by machine and interleaves time — with two machines the
    default would present a year of one host's history before the other host's
    first visit, and any "newest snapshot" lookup would return whichever machine
    sorts last alphabetically.
    """
    snapshot = snapshot_of(path)
    return (snapshot.taken_at, snapshot.host, snapshot.filename)


def snapshots(exports: Paths) -> tuple[tuple[Path, Snapshot], ...]:
    """Every archived snapshot, oldest first, paired with its provenance."""
    paths = get_files(exports, glob=GLOB, key=sort_key)
    return tuple((path, snapshot_of(path)) for path in paths)


def latest_per_host(exports: Paths) -> tuple[tuple[Path, Snapshot], ...]:
    """The newest snapshot of each host, ordered by host.

    What "the latest snapshot" has to mean for a present-state table once there is
    more than one machine. Taking the single newest file would let whichever
    machine exported most recently define the bookmarks of every other.
    """
    newest: dict[str, tuple[Path, Snapshot]] = {}
    for path, snapshot in snapshots(exports):
        newest[snapshot.host] = (path, snapshot)
    return tuple(newest[host] for host in sorted(newest))


def pages(exports: Paths) -> Iterator[Page]:
    """Every URL any snapshot knows about, per host, visited or not.

    Includes pages with no visit — ones reachable only through a bookmark, which
    `visits()` cannot see. That makes this the entry point for "what did I save and
    never open".

    Yielded once per `(host, url)`. Where snapshots disagree the newest wins, since
    titles get rewritten and `visit_count` climbs; the same rule as PipePipe's
    `streams`. Note that a page dropped by expiration and later revisited comes back
    with a `visit_count` restarted from 1 — the count is Firefox's current opinion,
    not a lifetime total.
    """
    merged: dict[tuple[str, str], Page] = {}
    for path, snapshot in snapshots(exports):
        for page in parser.pages(path, snapshot):
            merged[page.key] = page
    yield from merged.values()


def visits(exports: Paths) -> Iterator[Visit]:
    """Every navigation across every snapshot, oldest first.

    Deduplicated by `(host, url, visit_date)`. Two hosts stay distinct even for the
    same URL at the same instant: they are separate events on separate machines, and
    with microsecond timestamps a genuine collision does not arise.

    Where a visit appears in several snapshots the earliest one to hold it wins, so
    a record is attributed to the snapshot that first preserved it.
    """
    seen: dict[tuple[str, str, int], Visit] = {}
    for path, snapshot in snapshots(exports):
        for visit in parser.visits(path, snapshot):
            seen.setdefault(visit.key, visit)
    yield from sorted(seen.values(), key=lambda v: (v.visit_date_us, v.snapshot.host))


def engagement(exports: Paths) -> Iterator[Engagement]:
    """Per-page-view attention metrics across every snapshot, oldest first.

    The entry point where accumulation earns the most: Firefox expires these rows
    faster than it expires history, so the union across snapshots is strictly more
    than the newest one holds.

    Deduplicated by `(host, url, created_at)`, matching the table's own unique
    index. Read `Engagement.total_view_time_ms` before summing anything — it counts
    only foreground, non-idle time and is approximate.
    """
    seen: dict[tuple[str, str, int], Engagement] = {}
    for path, snapshot in snapshots(exports):
        for record in parser.engagement(path, snapshot):
            seen.setdefault(record.key, record)
    yield from sorted(seen.values(), key=lambda e: (e.created_at_ms, e.snapshot.host))


def downloads(exports: Paths) -> Iterator[Download]:
    """Completed downloads across every snapshot, oldest first.

    Accumulated, not read from the newest snapshot, for two reasons: Firefox expires
    annotations, and the download annotation is keyed per page, so downloading the
    same URL twice overwrites the first record. Old snapshots are the only place the
    earlier download survives.
    """
    seen: dict[tuple[str, str, int], Download] = {}
    for path, snapshot in snapshots(exports):
        for download in parser.downloads(path, snapshot):
            seen.setdefault(download.key, download)
    yield from sorted(seen.values(), key=lambda d: (d.date_added_us, d.snapshot.host))


def bookmarks(exports: Paths) -> Iterator[Bookmark]:
    """The bookmark tree of each host, from that host's newest snapshot.

    Present state, not a log: deleting a bookmark removes its row, and unless Sync
    is on there is no tombstone anywhere. So this answers "what is bookmarked now"
    and not "what have I ever bookmarked". Walk `parser.bookmarks` over `snapshots`
    yourself for the latter.
    """
    for path, snapshot in latest_per_host(exports):
        yield from parser.bookmarks(path, snapshot)


def origins(exports: Paths) -> Iterator[Origin]:
    """Sites and their frecency, from each host's newest snapshot.

    Newest only because frecency is a current score that Firefox recomputes and
    decays; an old snapshot's value is not a historical observation worth merging.
    """
    for path, snapshot in latest_per_host(exports):
        yield from parser.origins(path, snapshot)


def typed_inputs(exports: Paths) -> Iterator[TypedInput]:
    """Address bar inputs, from each host's newest snapshot. Present state.

    Firefox keeps only what the address bar still needs and decays `use_count`, so
    this is what is currently reached by typing, not a log of what was ever typed.
    """
    for path, snapshot in latest_per_host(exports):
        yield from parser.typed_inputs(path, snapshot)


__all__ = [
    "FILENAME",
    "GLOB",
    "ROOT_GUIDS",
    "UNCOUNTED_VISIT_TYPES",
    "Bookmark",
    "BookmarkType",
    "DocumentType",
    "Download",
    "Engagement",
    "Origin",
    "Page",
    "Snapshot",
    "TypedInput",
    "Visit",
    "VisitSource",
    "VisitType",
    "bookmarks",
    "downloads",
    "engagement",
    "latest_per_host",
    "origins",
    "pages",
    "parser",
    "snapshot_of",
    "snapshots",
    "sort_key",
    "typed_inputs",
    "visits",
]
