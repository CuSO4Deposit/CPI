from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import make_places_export

from cuso4d import firefox
from cuso4d.firefox import parser
from cuso4d.firefox.models import (
    UNCOUNTED_VISIT_TYPES,
    BookmarkType,
    DocumentType,
    Snapshot,
    VisitSource,
    VisitType,
)

# Timestamps taken from a real snapshot, kept in their native units.
JAN_US = 1767225600_000000  # 2026-01-01T00:00:00Z, microseconds
JUL_US = 1785669803_270035  # 2026-08-02T11:23:23.270035Z
JAN_MS = 1767225600_000  # same instant, milliseconds

LEX = "places-nightcord-lexikos-20260802T124748Z.sqlite.xz"
LAB = "places-nightcord-laborari-20260802T130713Z.sqlite.xz"

SNAP = Snapshot(
    taken_at=datetime(2026, 8, 2, 12, 47, 48, tzinfo=UTC),
    host="nightcord-lexikos",
    filename=LEX,
)


def test_parses_a_snapshot(tmp_path: Path) -> None:
    db = make_places_export(
        tmp_path / LEX,
        places=[(1, "https://example.org/a", "Page A", 2)],
        visits=[(10, 1, JAN_US, 1, 0), (11, 1, JUL_US, 2, 10)],
        metadata=[(1, JAN_MS, JAN_MS + 60_000, 45_000)],
        origins=[("https://", "example.org", 2000)],
        inputs=[(1, "exam", 1.25)],
    )

    (page,) = list(parser.pages(db, SNAP))
    assert page.url == "https://example.org/a"
    assert page.title == "Page A"
    assert page.key == ("nightcord-lexikos", "https://example.org/a")
    assert page.last_visit == datetime(2026, 8, 2, 11, 23, 23, 270035, tzinfo=UTC)
    assert page.snapshot.host == "nightcord-lexikos"

    first, second = list(parser.visits(db, SNAP))
    assert first.when == datetime(2026, 1, 1, tzinfo=UTC)
    assert first.visit_type is VisitType.LINK
    assert first.source is VisitSource.ORGANIC
    assert first.referrer_url is None
    # The second visit's from_visit points at the first, resolved to a URL.
    assert second.referrer_url == "https://example.org/a"
    assert second.visit_type is VisitType.TYPED

    (engagement,) = list(parser.engagement(db, SNAP))
    assert engagement.view_time == timedelta(seconds=45)
    assert engagement.created_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert engagement.document_type is DocumentType.GENERIC

    (origin,) = list(parser.origins(db, SNAP))
    assert origin.url_prefix == "https://example.org"
    assert origin.frecency == 2000

    (typed,) = list(parser.typed_inputs(db, SNAP))
    assert typed.input == "exam"
    assert typed.use_count == 1.25


def test_three_timestamp_units_do_not_get_mixed(tmp_path: Path) -> None:
    """A visit is microseconds and an engagement milliseconds, for the same instant.

    Reading a visit as milliseconds lands in the year 58000, which is the kind of
    error that produces a plausible-looking chart with one absurd outlier.
    """
    db = make_places_export(
        tmp_path / LEX,
        places=[(1, "https://example.org/a", "A", 1)],
        visits=[(10, 1, JAN_US, 1, 0)],
        metadata=[(1, JAN_MS, JAN_MS, 0)],
        bookmarks=[(20, 1, 5, 1, "bkmk00000001", "A")],
        downloads=[(1, "file:///home/x/f.pdf", '{"endTime":1767225600000}', JAN_US)],
    )
    instant = datetime(2026, 1, 1, tzinfo=UTC)

    (visit,) = list(parser.visits(db, SNAP))
    (engagement,) = list(parser.engagement(db, SNAP))
    (download,) = list(parser.downloads(db, SNAP))
    bookmark = next(b for b in parser.bookmarks(db, SNAP) if b.guid == "bkmk00000001")

    assert visit.when == instant
    assert engagement.created_at == instant
    assert download.date_added == instant  # microseconds, like the visit
    assert download.ended_at == instant  # milliseconds, unlike its own dateAdded
    assert bookmark.date_added == datetime(1970, 1, 1, 0, 0, 1, tzinfo=UTC)


def test_visit_count_excludes_reloads_and_downloads(tmp_path: Path) -> None:
    """Firefox's own count skips types 7 and 9, so it disagrees with the row count.

    Verified exhaustively against two real snapshots: excluding exactly these two
    makes the stored count agree for every page, and excluding either alone leaves
    hundreds disagreeing.
    """
    db = make_places_export(
        tmp_path / LEX,
        # One real navigation plus three reloads and a download: Firefox stores 1.
        places=[(1, "https://example.org/a", "A", 1)],
        visits=[
            (10, 1, JAN_US, 1, 0),
            (11, 1, JAN_US + 1, 9, 10),
            (12, 1, JAN_US + 2, 9, 10),
            (13, 1, JAN_US + 3, 9, 10),
            (14, 1, JAN_US + 4, 7, 0),
        ],
    )

    (page,) = list(parser.pages(db, SNAP))
    visits = list(parser.visits(db, SNAP))
    assert page.visit_count == 1
    assert len(visits) == 5
    assert sum(v.counted for v in visits) == page.visit_count
    assert {v.visit_type_raw for v in visits if not v.counted} == UNCOUNTED_VISIT_TYPES


def test_dangling_referrer_reads_as_none(tmp_path: Path) -> None:
    """Firefox expires the referring visit but leaves from_visit pointing at it.

    382 of 23627 rows in the reference snapshot. A None must not be read as "no
    referrer" — `Visit.referrer_url` documents the two as indistinguishable.
    """
    db = make_places_export(
        tmp_path / LEX,
        places=[(1, "https://example.org/a", "A", 1)],
        visits=[(10, 1, JAN_US, 1, 999)],  # 999 no longer exists
    )
    (visit,) = list(parser.visits(db, SNAP))
    assert visit.referrer_url is None


def test_negative_frecency_is_not_a_rank(tmp_path: Path) -> None:
    """A negative frecency means "not yet computed", not "least relevant"."""
    db = make_places_export(
        tmp_path / LEX,
        places=[
            (1, "https://example.org/a", "A", 1),
            (2, "https://example.org/b", "B", 1),
        ],
        visits=[],
        frecency={1: -1, 2: 500},
        origins=[("https://", "example.org", -1)],
    )
    by_url = {p.url: p for p in parser.pages(db, SNAP)}
    assert by_url["https://example.org/a"].frecency is None
    assert by_url["https://example.org/a"].frecency_raw == -1
    assert by_url["https://example.org/b"].frecency == 500

    (origin,) = list(parser.origins(db, SNAP))
    assert origin.frecency is None


def test_undecodable_text_does_not_lose_the_table(tmp_path: Path) -> None:
    """One truncated emoji must not cost every page in the snapshot.

    A real snapshot has a description ending in a lone UTF-16 high surrogate, left
    by Firefox truncating to 256 characters mid-surrogate-pair. sqlite3's default
    text factory raises from the cursor, taking the whole read down.
    """
    db = make_places_export(
        tmp_path / LEX,
        places=[
            (1, "https://example.org/a", "A", 1),
            (2, "https://example.org/b", "B", 1),
        ],
        visits=[],
        descriptions={1: b"good text\xed\xa0\xbe"},
    )
    pages = {p.url: p for p in parser.pages(db, SNAP)}
    assert len(pages) == 2
    description = pages["https://example.org/a"].description
    assert description is not None
    assert description.startswith("good text")
    assert "�" in description


def test_bookmark_paths_exclude_the_blank_root(tmp_path: Path) -> None:
    """The tree root's title is empty and must not appear in any path."""
    db = make_places_export(
        tmp_path / LEX,
        places=[(1, "https://example.org/a", "A", 0)],
        bookmarks=[
            (20, 2, 5, None, "fldr0000ustc", "USTC"),
            (21, 2, 20, None, "fldr00002022", "2022 Fall"),
            (22, 1, 21, 1, "bkmk0000000a", "A"),
        ],
    )
    by_guid = {b.guid: b for b in parser.bookmarks(db, SNAP)}

    assert by_guid["root________"].path == ()
    assert by_guid["unfiled_____"].path == ()
    assert by_guid["unfiled_____"].root == "unfiled"
    assert by_guid["fldr0000ustc"].path == ("unfiled",)
    assert by_guid["fldr00002022"].path == ("unfiled", "USTC")
    assert by_guid["bkmk0000000a"].path == ("unfiled", "USTC", "2022 Fall")
    assert by_guid["bkmk0000000a"].root == "unfiled"
    assert by_guid["bkmk0000000a"].parent_guid == "fldr00002022"
    assert by_guid["bkmk0000000a"].type is BookmarkType.BOOKMARK
    assert by_guid["bkmk0000000a"].url == "https://example.org/a"
    # A folder carries no url, and every path element is a real title.
    assert by_guid["fldr0000ustc"].type is BookmarkType.FOLDER
    assert by_guid["fldr0000ustc"].url is None
    assert all("" not in b.path for b in by_guid.values())


def test_download_without_metadata_still_reads(tmp_path: Path) -> None:
    """The two annotations are independent; a real snapshot has one without metadata."""
    meta = '{"state":1,"deleted":false,"fileSize":4096,"endTime":1767225600000}'
    db = make_places_export(
        tmp_path / LEX,
        places=[
            (1, "https://example.org/f.pdf", None, 0),
            (2, "https://example.org/g", None, 0),
        ],
        downloads=[
            (1, "file:///home/x/f.pdf", meta, JAN_US),
            (2, "file:///home/x/g.bin", None, JAN_US + 1),
        ],
    )
    by_url = {d.url: d for d in parser.downloads(db, SNAP)}

    full = by_url["https://example.org/f.pdf"]
    assert full.file_size == 4096
    assert full.state_raw == 1
    assert full.deleted is False
    assert full.ended_at == datetime(2026, 1, 1, tzinfo=UTC)

    bare = by_url["https://example.org/g"]
    assert bare.destination == "file:///home/x/g.bin"
    assert (bare.file_size, bare.state_raw, bare.deleted, bare.ended_at) == (
        None,
        None,
        None,
        None,
    )


@pytest.mark.parametrize("blob", ['{"state":true}', "not json at all", "[1,2]", "null"])
def test_malformed_download_metadata_is_not_fatal(tmp_path: Path, blob: str) -> None:
    """It is JSON in a text column with no schema; a future release may change it.

    `{"state":true}` is the subtle one: in Python `True` is an `int`, so a naive
    isinstance check would let a boolean through as a state code.
    """
    db = make_places_export(
        tmp_path / LEX,
        places=[(1, "https://example.org/f", None, 0)],
        downloads=[(1, "file:///home/x/f", blob, JAN_US)],
    )
    (download,) = list(parser.downloads(db, SNAP))
    assert download.destination == "file:///home/x/f"
    assert download.state_raw is None
    assert download.file_size is None


def test_reads_a_bare_uncompressed_database(tmp_path: Path) -> None:
    db = make_places_export(
        tmp_path / "places-nightcord-lexikos-20260802T124748Z.sqlite",
        places=[(1, "https://example.org/a", "A", 1)],
        visits=[(10, 1, JAN_US, 1, 0)],
        compress=False,
    )
    (visit,) = list(parser.visits(db, SNAP))
    assert visit.url == "https://example.org/a"


def test_stats_reports_only_nonempty_tables(tmp_path: Path) -> None:
    db = make_places_export(
        tmp_path / LEX,
        places=[(1, "https://example.org/a", "A", 1)],
        visits=[(10, 1, JAN_US, 1, 0)],
    )
    stats = parser.stats(db)
    assert stats["moz_places"] == 1
    assert stats["moz_historyvisits"] == 1
    assert stats["_user_version"] == 86
    assert "moz_places_metadata" not in stats  # empty, so omitted


# --- filename parsing and ordering -----------------------------------------


def test_snapshot_filename_keeps_a_dashed_hostname_whole() -> None:
    """`nightcord-lexikos` is one host, not a host and something else.

    Splitting on dashes from the left would take `nightcord` as the machine and
    merge two machines' histories into one.
    """
    snapshot = firefox.snapshot_of(Path(LEX))
    assert snapshot.host == "nightcord-lexikos"
    assert snapshot.taken_at == datetime(2026, 8, 2, 12, 47, 48, tzinfo=UTC)
    assert snapshot.filename == LEX


@pytest.mark.parametrize(
    "name",
    [
        "places.sqlite.xz",
        "places-nightcord-lexikos.sqlite.xz",
        "places-nightcord-lexikos-20260802.sqlite.xz",
        "places-nightcord-lexikos-2026-08-02T124748Z.sqlite.xz",
        "gadgetbridge_20260730160959.zip",
    ],
)
def test_unparseable_filename_is_an_error(name: str) -> None:
    """Refusing to guess: the host decides identity, the stamp decides merge order."""
    with pytest.raises(ValueError, match="not a Firefox snapshot filename"):
        firefox.snapshot_of(Path(name))


def test_snapshots_sort_by_time_not_by_filename(tmp_path: Path) -> None:
    """Filename order groups by host and interleaves time; only the stamp is order.

    `laborari` sorts before `lexikos` alphabetically, so the default filename sort
    would put a later snapshot first — and `latest_per_host` would then be reading
    whichever host happens to sort last.
    """
    early = "places-nightcord-lexikos-20260101T000000Z.sqlite.xz"
    late = "places-nightcord-laborari-20260801T000000Z.sqlite.xz"
    for name in (early, late):
        make_places_export(tmp_path / name, places=[(1, "https://e.org/a", "A", 0)])

    ordered = [s.filename for _, s in firefox.snapshots(tmp_path)]
    assert ordered == [early, late]
    assert sorted([early, late]) != ordered  # the default sort disagrees


def test_latest_per_host_does_not_let_one_machine_shadow_another(
    tmp_path: Path,
) -> None:
    """Present-state tables need each host's newest, not the newest overall."""
    make_places_export(
        tmp_path / "places-hostA-20260101T000000Z.sqlite.xz",
        places=[(1, "https://a.example/1", "old A", 0)],
        bookmarks=[(20, 1, 5, 1, "bkmkA0000old", "old A")],
    )
    make_places_export(
        tmp_path / "places-hostA-20260201T000000Z.sqlite.xz",
        places=[(1, "https://a.example/2", "new A", 0)],
        bookmarks=[(20, 1, 5, 1, "bkmkA0000new", "new A")],
    )
    make_places_export(
        tmp_path / "places-hostB-20260115T000000Z.sqlite.xz",
        places=[(1, "https://b.example/1", "only B", 0)],
        bookmarks=[(20, 1, 5, 1, "bkmkB0000001", "only B")],
    )

    assert [s.host for _, s in firefox.latest_per_host(tmp_path)] == ["hostA", "hostB"]
    titles = {b.title for b in firefox.bookmarks(tmp_path) if b.guid.startswith("bkmk")}
    assert titles == {"new A", "only B"}


# --- accumulation ----------------------------------------------------------


def test_visits_accumulate_across_expiration(tmp_path: Path) -> None:
    """Firefox expires old visits, so an old snapshot holds what a new one lost."""
    make_places_export(
        tmp_path / "places-hostA-20260101T000000Z.sqlite.xz",
        places=[(1, "https://e.org/a", "A", 1)],
        visits=[(10, 1, JAN_US, 1, 0)],
    )
    make_places_export(
        tmp_path / "places-hostA-20260801T000000Z.sqlite.xz",
        # The January visit has expired; a July one has arrived. Note the ids have
        # renumbered, which is exactly why identity is not an id.
        places=[(1, "https://e.org/a", "A", 1)],
        visits=[(1, 1, JUL_US, 2, 0)],
    )

    visits = list(firefox.visits(tmp_path))
    assert [v.visit_date_us for v in visits] == [JAN_US, JUL_US]
    assert [v.snapshot.filename[:19] for v in visits] == [
        "places-hostA-202601",
        "places-hostA-202608",
    ]


def test_unchanged_visit_is_not_double_counted(tmp_path: Path) -> None:
    for stamp in ("20260801T000000Z", "20260802T000000Z"):
        make_places_export(
            tmp_path / f"places-hostA-{stamp}.sqlite.xz",
            places=[(1, "https://e.org/a", "A", 1)],
            visits=[(10, 1, JAN_US, 1, 0)],
        )

    assert len(list(firefox.visits(tmp_path))) == 1


def test_place_ids_colliding_across_hosts_stay_distinct(tmp_path: Path) -> None:
    """Two real snapshots share 11093 place ids, of which one means the same URL.

    Identity is `(host, url)`; an id-keyed merge would fuse unrelated pages.
    """
    make_places_export(
        tmp_path / "places-hostA-20260801T000000Z.sqlite.xz",
        places=[(1, "https://a.example/only-on-A", "A", 1)],
        visits=[(10, 1, JAN_US, 1, 0)],
    )
    make_places_export(
        tmp_path / "places-hostB-20260801T000000Z.sqlite.xz",
        places=[(1, "https://b.example/only-on-B", "B", 1)],
        visits=[(10, 1, JAN_US, 1, 0)],
    )

    pages = list(firefox.pages(tmp_path))
    assert {p.url for p in pages} == {
        "https://a.example/only-on-A",
        "https://b.example/only-on-B",
    }
    # Same id, same visit_date, different machines: two events, not one.
    assert len(list(firefox.visits(tmp_path))) == 2


def test_same_url_on_two_hosts_is_two_visits(tmp_path: Path) -> None:
    """Without Sync these are separate events, and their keys must not collide."""
    for host in ("hostA", "hostB"):
        make_places_export(
            tmp_path / f"places-{host}-20260801T000000Z.sqlite.xz",
            places=[(1, "https://shared.example/", "shared", 1)],
            visits=[(10, 1, JAN_US, 1, 0)],
        )

    visits = list(firefox.visits(tmp_path))
    assert len(visits) == 2
    assert {v.snapshot.host for v in visits} == {"hostA", "hostB"}
    assert len({v.key for v in visits}) == 2


def test_engagement_accumulates_because_it_expires_first(tmp_path: Path) -> None:
    """Firefox expires engagement sooner than history — 134 days against 288 in one
    reference pair — so the union across snapshots beats the newest one."""
    make_places_export(
        tmp_path / "places-hostA-20260101T000000Z.sqlite.xz",
        places=[(1, "https://e.org/a", "A", 1)],
        metadata=[(1, JAN_MS, JAN_MS + 1000, 1000)],
    )
    make_places_export(
        tmp_path / "places-hostA-20260801T000000Z.sqlite.xz",
        places=[(1, "https://e.org/a", "A", 1)],
        metadata=[(1, JAN_MS + 999_000, JAN_MS + 999_500, 500)],
    )

    engagements = list(firefox.engagement(tmp_path))
    assert [e.created_at_ms for e in engagements] == [JAN_MS, JAN_MS + 999_000]


def test_pages_take_newest_metadata_but_keep_the_never_visited(
    tmp_path: Path,
) -> None:
    """Titles get rewritten, so newest wins; a bookmark-only page still shows up."""
    make_places_export(
        tmp_path / "places-hostA-20260101T000000Z.sqlite.xz",
        places=[(1, "https://e.org/a", "old title", 1)],
        visits=[(10, 1, JAN_US, 1, 0)],
    )
    make_places_export(
        tmp_path / "places-hostA-20260801T000000Z.sqlite.xz",
        places=[
            (1, "https://e.org/a", "new title", 2),
            (2, "https://e.org/saved", "never opened", 0),
        ],
        visits=[(1, 1, JAN_US, 1, 0), (2, 1, JUL_US, 1, 0)],
        bookmarks=[(20, 1, 5, 2, "bkmk00000002", "never opened")],
    )

    pages = {p.url: p for p in firefox.pages(tmp_path)}
    assert pages["https://e.org/a"].title == "new title"
    assert pages["https://e.org/a"].visit_count == 2
    # Visible here despite having no visit at all, which `visits()` cannot show.
    assert pages["https://e.org/saved"].visit_count == 0
    assert "https://e.org/saved" not in {v.url for v in firefox.visits(tmp_path)}


def test_downloads_accumulate_because_the_annotation_is_overwritten(
    tmp_path: Path,
) -> None:
    """`moz_annos` is unique per `(place, attribute)`, so a re-download overwrites.

    The earlier download survives only in an earlier snapshot.
    """
    make_places_export(
        tmp_path / "places-hostA-20260101T000000Z.sqlite.xz",
        places=[(1, "https://e.org/f.pdf", None, 0)],
        downloads=[(1, "file:///home/x/f.pdf", '{"fileSize":100}', JAN_US)],
    )
    make_places_export(
        tmp_path / "places-hostA-20260801T000000Z.sqlite.xz",
        places=[(1, "https://e.org/f.pdf", None, 0)],
        downloads=[(1, "file:///home/x/f-2.pdf", '{"fileSize":200}', JUL_US)],
    )

    downloads = list(firefox.downloads(tmp_path))
    assert [d.file_size for d in downloads] == [100, 200]
    assert [d.destination for d in downloads] == [
        "file:///home/x/f.pdf",
        "file:///home/x/f-2.pdf",
    ]


def test_bookmarks_do_not_resurrect_from_an_old_snapshot(tmp_path: Path) -> None:
    """Present state: a deleted bookmark leaves no tombstone without Sync."""
    make_places_export(
        tmp_path / "places-hostA-20260101T000000Z.sqlite.xz",
        places=[(1, "https://e.org/gone", "deleted since", 0)],
        bookmarks=[(20, 1, 5, 1, "bkmk0000gone", "deleted since")],
    )
    make_places_export(
        tmp_path / "places-hostA-20260801T000000Z.sqlite.xz",
        places=[(1, "https://e.org/kept", "still here", 0)],
        bookmarks=[(20, 1, 5, 1, "bkmk0000kept", "still here")],
    )

    guids = {b.guid for b in firefox.bookmarks(tmp_path)}
    assert "bkmk0000kept" in guids
    assert "bkmk0000gone" not in guids


# --- real snapshot invariants ---------------------------------------------


def test_real_snapshot_identity_holds(real_places_export: Path) -> None:
    """The assumptions accumulation rests on, against whatever snapshot is at hand.

    Only invariants that hold for *any* profile: no counts, which would say nothing
    about the code and would leak what someone read.
    """
    snapshot = firefox.snapshot_of(real_places_export)
    visits = list(parser.visits(real_places_export, snapshot))
    if not visits:
        pytest.skip("snapshot has no history")

    assert len({v.key for v in visits}) == len(visits)
    assert visits == sorted(visits, key=lambda v: v.visit_date_us)

    pages = list(parser.pages(real_places_export, snapshot))
    page_keys = {p.key for p in pages}
    assert len(page_keys) == len(pages)  # (host, url) is unique

    # Everything that references a page is a subset of the page table.
    assert {v.key[:2] for v in visits} <= page_keys
    engagements = list(parser.engagement(real_places_export, snapshot))
    assert {e.key[:2] for e in engagements} <= page_keys
    assert len({e.key for e in engagements}) == len(engagements)
    downloads = list(parser.downloads(real_places_export, snapshot))
    assert {(snapshot.host, d.url) for d in downloads} <= page_keys


def test_real_snapshot_visit_count_rule_still_holds(real_places_export: Path) -> None:
    """The one upstream behaviour this module is not free to be wrong about.

    `Page.visit_count` is documented as excluding reloads and downloads. If a
    future Firefox changes that, every count built on it shifts silently, so assert
    it against whatever real snapshot is available.
    """
    snapshot = firefox.snapshot_of(real_places_export)
    counted: dict[str, int] = {}
    for visit in parser.visits(real_places_export, snapshot):
        counted[visit.url] = counted.get(visit.url, 0) + visit.counted

    pages = list(parser.pages(real_places_export, snapshot))
    if not pages:
        pytest.skip("snapshot has no pages")
    for page in pages:
        assert page.visit_count == counted.get(page.url, 0)


def test_real_snapshot_sentinels_never_reach_a_caller(real_places_export: Path) -> None:
    snapshot = firefox.snapshot_of(real_places_export)

    for page in parser.pages(real_places_export, snapshot):
        assert (page.frecency is None) == (page.frecency_raw < 0)
        assert page.frecency is None or page.frecency >= 0
        # last_visit_date agrees with the visit table, and is None only when absent.
        assert (page.last_visit is None) == (page.last_visit_date_us is None)

    for origin in parser.origins(real_places_export, snapshot):
        assert origin.frecency is None or origin.frecency >= 0

    for bookmark in parser.bookmarks(real_places_export, snapshot):
        # A folder never carries a URL; a bookmark always does.
        if bookmark.type is BookmarkType.FOLDER:
            assert bookmark.url is None
        if bookmark.type is BookmarkType.BOOKMARK:
            assert bookmark.url is not None
        assert "" not in bookmark.path


def test_real_snapshot_last_visit_matches_the_visit_table(
    real_places_export: Path,
) -> None:
    """`Page.last_visit` is documented as redundant with max(visit). Verify it."""
    snapshot = firefox.snapshot_of(real_places_export)
    latest: dict[str, int] = {}
    for visit in parser.visits(real_places_export, snapshot):
        latest[visit.url] = max(latest.get(visit.url, 0), visit.visit_date_us)

    pages = list(parser.pages(real_places_export, snapshot))
    if not pages:
        pytest.skip("snapshot has no pages")
    for page in pages:
        assert page.last_visit_date_us == latest.get(page.url)
