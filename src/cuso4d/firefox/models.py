"""Types for Firefox `places.sqlite` data.

Verified against two real snapshots from different machines, `user_version = 86`.

Three timestamp units live in one file, which is the easiest thing here to get
wrong. Visits, pages, bookmarks and annotations are **microseconds** since the
epoch; `moz_places_metadata` is **milliseconds**. Reading a visit as milliseconds
puts it in the year 58000. Each model converts its own column and exposes an aware
`datetime`, so nothing downstream has to remember which is which.

The other recurring theme is that a row id is never an identity. `moz_places.id`
is a per-profile autoincrement: across the two reference snapshots 11093 ids are
present in both, and exactly one of them refers to the same URL. Even `guid`,
which is stable *within* a profile, does not carry across machines — the two
snapshots share zero page guids. So identity here is `(host, url)`, and `host` is
part of every key below.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from functools import cached_property

#: Visit types excluded from `moz_places.visit_count` by Firefox itself. Verified
#: by exhaustive comparison: excluding exactly these two makes the stored count
#: agree with the row count for every page in both reference snapshots (13783 and
#: 32580), while excluding either one alone leaves hundreds disagreeing.
UNCOUNTED_VISIT_TYPES = frozenset({7, 9})


class VisitType(IntEnum):
    """`nsINavHistoryService.TRANSITION_*` — how a page was reached."""

    LINK = 1
    TYPED = 2
    BOOKMARK = 3
    EMBED = 4
    REDIRECT_PERMANENT = 5
    REDIRECT_TEMPORARY = 6
    DOWNLOAD = 7
    FRAMED_LINK = 8
    RELOAD = 9

    @classmethod
    def _missing_(cls, value: object) -> None:
        # A future Firefox can add a transition; callers get the raw int rather
        # than a crash. Keeping the archive readable wins over a tidy enum.
        return None


class VisitSource(IntEnum):
    """`nsINavHistoryService.VISIT_SOURCE_*` — where the navigation originated.

    `ORGANIC` is the default and dominates. Both reference snapshots use only
    0, 2 and 3; `SPONSORED` never appears, which is what you would expect from a
    profile that does not click sponsored tiles.
    """

    ORGANIC = 0
    SPONSORED = 1
    BOOKMARKED = 2
    SEARCHED = 3

    @classmethod
    def _missing_(cls, value: object) -> None:
        return None


class DocumentType(IntEnum):
    """`Interactions.DOCUMENT_TYPE` — what kind of page an engagement was on.

    `MEDIA` is written for video-serving pages. Neither reference snapshot has a
    single row of it, so treat this as unexercised rather than verified.
    """

    GENERIC = 0
    MEDIA = 1

    @classmethod
    def _missing_(cls, value: object) -> None:
        return None


class BookmarkType(IntEnum):
    """`nsINavBookmarksService.TYPE_*`."""

    BOOKMARK = 1
    FOLDER = 2
    SEPARATOR = 3

    @classmethod
    def _missing_(cls, value: object) -> None:
        return None


#: The six `guid`s Firefox gives the roots of the bookmark tree. These are the only
#: guids shared between the two reference snapshots — everything else is
#: profile-local, which is why `Bookmark.key` includes the host.
ROOT_GUIDS = {
    "root________": "root",
    "menu________": "menu",
    "toolbar_____": "toolbar",
    "tags________": "tags",
    "unfiled_____": "unfiled",
    "mobile______": "mobile",
}


def from_micros(us: int) -> datetime:
    return datetime.fromtimestamp(us / 1_000_000, tz=UTC)


def from_millis(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


@dataclass(frozen=True, order=True)
class Snapshot:
    """Provenance for one archived `places.sqlite`.

    Carries the host rather than only a filename, unlike the other CPI sources:
    Firefox is the one source here that genuinely runs on several machines, and
    since page ids and guids are both profile-local, a record is only meaningful
    alongside the machine it came from.

    Ordered by `(taken_at, host)` so sorting snapshots is chronological regardless
    of which machine wrote them.
    """

    taken_at: datetime
    host: str
    filename: str


@dataclass(frozen=True)
class Page:
    """A row of `moz_places` — a URL Firefox knows about, visited or not.

    An entity in its own right, not an appendix to history. In the reference
    snapshot 852 of 13783 pages have no visit at all; every one of them is
    referenced by a bookmark, so anything built on `visits()` alone silently omits
    what was saved and never opened.

    Identity is `(host, url)`. Deliberately not carrying the database's `id`, which
    renumbers per profile and collides meaninglessly across machines.
    """

    url: str
    title: str | None
    """Page title, or None if Firefox never got one. 1620 of 13783 in the
    reference snapshot — a redirect hop or a download target has no title."""

    description: str | None
    preview_image_url: str | None
    site_name: str | None
    """Almost always None: 13783 of 13783 in one reference snapshot, so do not
    build anything on it being populated."""

    visit_count: int
    """Firefox's own count, which is **not** the number of visit rows.

    It excludes `DOWNLOAD` and `RELOAD` transitions (see `UNCOUNTED_VISIT_TYPES`),
    so a page reloaded twenty times and never navigated to reports 0. Count
    `visits()` yourself if you want every row; use this when you want to agree
    with what the browser UI shows.
    """

    typed: int
    """How often the URL was reached by typing. A count, despite the name."""

    hidden: bool
    """Set for pages Firefox hides from the UI, typically redirect hops and
    framed loads. All 632 hidden pages in the reference snapshot have visits."""

    frecency_raw: int
    """The stored `frecency` column, verbatim. Prefer `frecency`."""

    last_visit_date_us: int | None
    guid: str
    """Stable within a profile, but not across machines: the two reference
    snapshots share none. Fine as a key for tracking a page over successive
    snapshots of *one* host, useless for matching between hosts."""

    snapshot: Snapshot

    @property
    def key(self) -> tuple[str, str]:
        return (self.snapshot.host, self.url)

    @property
    def frecency(self) -> int | None:
        """Firefox's relevance score, or None when it has not been computed.

        A negative value is a "needs recalculation" sentinel, not a rank below
        zero: both reference snapshots carry a handful (2 and 4). Sorting on the
        raw column puts those pages at the bottom as though they were the least
        relevant things in the profile.
        """
        return None if self.frecency_raw < 0 else self.frecency_raw

    @cached_property
    def last_visit(self) -> datetime | None:
        """When the page was last visited, or None if never.

        Redundant with `max(v.when)` over its visits — verified to agree exactly
        for every page in both reference snapshots — but available without
        reading the visit table.
        """
        if self.last_visit_date_us is None:
            return None
        return from_micros(self.last_visit_date_us)


@dataclass(frozen=True)
class Visit:
    """One row of `moz_historyvisits`: a single navigation to a page.

    Unlike PipePipe's history, this really is one row per event and nothing is
    overwritten, so a single snapshot holds a genuine timeline. Successive
    snapshots still matter, because Firefox expires old rows on its own.

    Identity is `(host, url, visit_date_us)`, verified unique in both reference
    snapshots.
    """

    url: str
    visit_date_us: int
    visit_type_raw: int
    source_raw: int

    referrer_url: str | None
    """The page navigated from, resolved within this snapshot, or None.

    None means one of three things, indistinguishably: the visit had no referrer
    (7005 of 23627 rows store 0), or Firefox has since expired the referring
    visit, leaving a dangling id (382 rows), or the referrer is a page this
    snapshot no longer holds. Do not read a None as "typed directly".
    """

    snapshot: Snapshot

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.snapshot.host, self.url, self.visit_date_us)

    @cached_property
    def when(self) -> datetime:
        return from_micros(self.visit_date_us)

    @property
    def visit_type(self) -> VisitType | None:
        """The transition, or None for a value no known Firefox defines."""
        return VisitType(self.visit_type_raw)

    @property
    def source(self) -> VisitSource | None:
        return VisitSource(self.source_raw)

    @property
    def counted(self) -> bool:
        """Whether Firefox counts this visit in `Page.visit_count`."""
        return self.visit_type_raw not in UNCOUNTED_VISIT_TYPES


@dataclass(frozen=True)
class Engagement:
    """One row of `moz_places_metadata`: how one page view was actually used.

    The only record of attention rather than mere arrival — dwell time, keys
    pressed, distance scrolled. One row per page view, not per day: a page can
    hold dozens in an afternoon (65 for one page on one day in the reference
    snapshot). Firefox also splits a long-lived tab into a fresh row after an
    hour without updates, so two rows for one URL need not mean two navigations.

    Identity is `(host, url, created_at_ms)`, matching the table's own unique
    index on `(place_id, created_at)`.
    """

    url: str
    created_at_ms: int
    updated_at_ms: int

    total_view_time_ms: int
    """Foreground, non-idle time on the page, in milliseconds.

    Approximate by construction: Firefox accumulates it only while the window is
    focused and the user is not idle (60s threshold), and its own comment admits
    losing ~2 seconds around an idle transition. It is therefore normally well
    below `updated_at - created_at`, and is not "how long the tab was open".
    """

    typing_time_ms: int
    key_presses: int
    scrolling_time_ms: int
    scrolling_distance: int
    document_type_raw: int
    referrer_url: str | None

    snapshot: Snapshot

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.snapshot.host, self.url, self.created_at_ms)

    @cached_property
    def created_at(self) -> datetime:
        return from_millis(self.created_at_ms)

    @cached_property
    def updated_at(self) -> datetime:
        return from_millis(self.updated_at_ms)

    @property
    def document_type(self) -> DocumentType | None:
        return DocumentType(self.document_type_raw)

    @cached_property
    def view_time(self) -> timedelta:
        """`total_view_time_ms` as a duration. See that field before summing it."""
        return timedelta(milliseconds=self.total_view_time_ms)

    @cached_property
    def typing_time(self) -> timedelta:
        return timedelta(milliseconds=self.typing_time_ms)

    @cached_property
    def scrolling_time(self) -> timedelta:
        return timedelta(milliseconds=self.scrolling_time_ms)


@dataclass(frozen=True)
class Bookmark:
    """One row of `moz_bookmarks` — a bookmark, a folder or a separator.

    Present state, not a log: deleting a bookmark removes the row and no later
    snapshot records that it existed. `moz_bookmarks_deleted` holds tombstones for
    Sync, and is empty without it.

    Identity is `(host, guid)`. The guid is stable within a profile but shared
    across machines only for the six tree roots, so bookmarks that are "the same"
    on two machines are separate records here unless Sync is on.
    """

    guid: str
    type_raw: int
    url: str | None
    """None for folders and separators, always set for bookmarks."""

    title: str | None
    position: int
    parent_guid: str | None
    """None only for the tree root itself."""

    path: tuple[str, ...]
    """Titles of the containing folders, outermost first, excluding this item.

    Resolved within the snapshot. Firefox stores root titles as bare strings like
    `unfiled`, which is what shows up here rather than a localized label.
    """

    date_added_us: int
    last_modified_us: int
    snapshot: Snapshot

    @property
    def key(self) -> tuple[str, str]:
        return (self.snapshot.host, self.guid)

    @property
    def type(self) -> BookmarkType | None:
        return BookmarkType(self.type_raw)

    @property
    def root(self) -> str | None:
        """Which tree root this sits under (`toolbar`, `unfiled`, ...), if any."""
        return ROOT_GUIDS.get(self.guid) or (self.path[0] if self.path else None)

    @cached_property
    def date_added(self) -> datetime:
        return from_micros(self.date_added_us)

    @cached_property
    def last_modified(self) -> datetime:
        return from_micros(self.last_modified_us)


@dataclass(frozen=True)
class Download:
    """A completed download, reconstructed from two `moz_annos` rows.

    Firefox has no download table: it annotates the source page with
    `downloads/destinationFileURI` and `downloads/metaData`. Both are keyed
    `(place_id, anno_attribute_id)`, so only the *most recent* download of a given
    URL survives — re-downloading the same file overwrites the annotation.

    The two annotations are also independent, and one reference snapshot has a
    destination with no metadata, so every metadata-derived field is optional.
    """

    url: str
    destination: str | None
    """`file://` URI the download was written to. A local path, so treat it as
    identifying: it usually contains the account name."""

    file_size: int | None
    ended_at_ms: int | None
    state_raw: int | None
    deleted: bool | None
    """Whether the downloaded file was later removed from disk."""

    date_added_us: int
    snapshot: Snapshot

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.snapshot.host, self.url, self.date_added_us)

    @cached_property
    def date_added(self) -> datetime:
        """When the annotation was written, i.e. when the download finished."""
        return from_micros(self.date_added_us)

    @cached_property
    def ended_at(self) -> datetime | None:
        return None if self.ended_at_ms is None else from_millis(self.ended_at_ms)


@dataclass(frozen=True)
class Origin:
    """One row of `moz_origins`: a site, with Firefox's own relevance score.

    Identity is `(host, prefix, site)` — the table's unique index plus the machine.
    `prefix` is part of it because `http://` and `https://` for one site are two
    rows, and merging them would double-count.
    """

    prefix: str
    site: str
    """The `host` column, renamed: `Snapshot.host` is the machine, this is the
    website, and confusing the two would be easy and silent."""

    frecency_raw: int
    snapshot: Snapshot

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.snapshot.host, self.prefix, self.site)

    @property
    def url_prefix(self) -> str:
        return f"{self.prefix}{self.site}"

    @property
    def frecency(self) -> int | None:
        """Site relevance, or None when not computed. See `Page.frecency`."""
        return None if self.frecency_raw < 0 else self.frecency_raw


@dataclass(frozen=True)
class TypedInput:
    """One row of `moz_inputhistory`: a string typed in the address bar, and how
    heavily it was used to reach a page.

    Present state and a small table — 50 and 144 rows in the reference snapshots.
    Firefox keeps only what the address bar needs and decays the score, so this is
    "what I currently reach by typing", not a log of what was ever typed.
    """

    input: str
    url: str
    use_count: float
    """A decaying weight, not a count: observed between 0.11 and 1.53. Useful for
    ranking, meaningless as a number of uses."""

    snapshot: Snapshot

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.snapshot.host, self.url, self.input)
