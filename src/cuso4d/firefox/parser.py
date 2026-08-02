"""Reading a single archived Firefox `places.sqlite`.

Every function here takes an explicit path and reads exactly one snapshot. No
configuration, no cross-snapshot logic, so tests can point at a fixture file.

Each reader needs to know which machine the snapshot came from, since page ids and
guids are both profile-local. That is the `Snapshot` argument, which callers get
from `cuso4d.firefox.snapshot_of` rather than from anything in this module — a
parser has no business parsing filenames.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

from cuso4d.core.sqlite import sqlite_ro, sqlite_ro_xz
from cuso4d.firefox.models import (
    Bookmark,
    Download,
    Engagement,
    Origin,
    Page,
    Snapshot,
    TypedInput,
    Visit,
)

#: Annotation names Firefox uses to record downloads. There is no download table.
ANNO_DESTINATION = "downloads/destinationFileURI"
ANNO_METADATA = "downloads/metaData"


@contextmanager
def open_export(path: Path) -> Iterator[sqlite3.Connection]:
    """Open a snapshot, whether xz-compressed or a bare database file."""
    if path.suffix == ".xz":
        with sqlite_ro_xz(path) as conn:
            yield conn
    else:
        with sqlite_ro(path) as conn:
            yield conn


def schema_version(conn: sqlite3.Connection) -> int:
    """The `places.sqlite` schema version, e.g. 86. Provenance, never branched on."""
    version: int = conn.execute("PRAGMA user_version").fetchone()[0]
    return version


_PAGE_COLUMNS = """
    p.url, p.title, p.description, p.preview_image_url, p.site_name,
    p.visit_count, p.typed, p.hidden, p.frecency, p.last_visit_date, p.guid
"""


def _page(row: sqlite3.Row, snapshot: Snapshot) -> Page:
    return Page(
        url=row["url"],
        title=row["title"],
        description=row["description"],
        preview_image_url=row["preview_image_url"],
        site_name=row["site_name"],
        visit_count=row["visit_count"],
        typed=row["typed"],
        hidden=bool(row["hidden"]),
        frecency_raw=row["frecency"],
        last_visit_date_us=row["last_visit_date"],
        guid=row["guid"],
        snapshot=snapshot,
    )


def pages(path: Path, snapshot: Snapshot) -> Iterator[Page]:
    """Every URL this snapshot knows about, visited or not.

    Includes pages reachable only through a bookmark, which `visits()` cannot see.
    Rows with a NULL url are skipped; the column is nullable in the schema even
    though neither reference snapshot has one.
    """
    query = f"SELECT {_PAGE_COLUMNS} FROM moz_places p WHERE p.url IS NOT NULL"
    with open_export(path) as conn:
        for row in conn.execute(query):
            yield _page(row, snapshot)


def visits(path: Path, snapshot: Snapshot) -> Iterator[Visit]:
    """Every navigation in this snapshot, oldest first.

    `from_visit` is resolved to a referrer URL here rather than exposed: it is a
    visit id, meaningless outside this one file, and 382 of them in the reference
    snapshot point at visits Firefox has already expired. The LEFT JOIN turns both
    the absent and the dangling case into None, which `Visit.referrer_url`
    documents as indistinguishable.
    """
    query = """
        SELECT p.url, v.visit_date, v.visit_type, v.source, rp.url AS referrer_url
        FROM moz_historyvisits v
        JOIN moz_places p ON p.id = v.place_id
        LEFT JOIN moz_historyvisits rv ON rv.id = v.from_visit
        LEFT JOIN moz_places rp ON rp.id = rv.place_id
        ORDER BY v.visit_date
    """
    with open_export(path) as conn:
        for row in conn.execute(query):
            yield Visit(
                url=row["url"],
                visit_date_us=row["visit_date"],
                visit_type_raw=row["visit_type"],
                source_raw=row["source"],
                referrer_url=row["referrer_url"],
                snapshot=snapshot,
            )


def engagement(path: Path, snapshot: Snapshot) -> Iterator[Engagement]:
    """Per-page-view attention metrics from this snapshot, oldest first.

    Firefox expires these rows on age while keeping the corresponding visits, so a
    snapshot's engagement window is shorter than its history: in one reference pair
    history reaches back 288 days and engagement only 134. Old snapshots are the
    only record of the difference, which is why accumulation matters here.
    """
    query = """
        SELECT p.url, m.created_at, m.updated_at, m.total_view_time,
               m.typing_time, m.key_presses, m.scrolling_time,
               m.scrolling_distance, m.document_type, rp.url AS referrer_url
        FROM moz_places_metadata m
        JOIN moz_places p ON p.id = m.place_id
        LEFT JOIN moz_places rp ON rp.id = m.referrer_place_id
        ORDER BY m.created_at
    """
    with open_export(path) as conn:
        for row in conn.execute(query):
            yield Engagement(
                url=row["url"],
                created_at_ms=row["created_at"],
                updated_at_ms=row["updated_at"],
                total_view_time_ms=row["total_view_time"],
                typing_time_ms=row["typing_time"],
                key_presses=row["key_presses"],
                scrolling_time_ms=row["scrolling_time"],
                scrolling_distance=row["scrolling_distance"],
                document_type_raw=row["document_type"],
                referrer_url=row["referrer_url"],
                snapshot=snapshot,
            )


def bookmarks(path: Path, snapshot: Snapshot) -> Iterator[Bookmark]:
    """The bookmark tree from this snapshot, parents before children.

    Ancestor titles are resolved into `Bookmark.path` with a recursive CTE. The
    tree is shallow in practice — depth 4 in both reference snapshots — but there
    is no reason to assume that of anyone's profile.
    """
    # The root's own title is blank and must not enter any path, so the recursive
    # step appends the *parent's* title only once the parent is itself below the
    # root (depth >= 1). Paths are newline-joined because a folder title may
    # contain anything else, including the separators one would reach for first.
    query = """
        WITH RECURSIVE tree(id, depth, path) AS (
            SELECT id, 0, ''
            FROM moz_bookmarks WHERE parent = 0
            UNION ALL
            SELECT b.id, t.depth + 1,
                   CASE WHEN t.depth = 0 THEN ''
                        WHEN t.path = '' THEN ifnull(p.title, '')
                        ELSE t.path || char(10) || ifnull(p.title, '') END
            FROM moz_bookmarks b
            JOIN tree t ON b.parent = t.id
            JOIN moz_bookmarks p ON p.id = b.parent
        )
        SELECT b.guid, b.type, pl.url, b.title, b.position, b.dateAdded,
               b.lastModified, parent.guid AS parent_guid, t.path
        FROM moz_bookmarks b
        JOIN tree t ON t.id = b.id
        LEFT JOIN moz_places pl ON pl.id = b.fk
        LEFT JOIN moz_bookmarks parent ON parent.id = b.parent
        ORDER BY t.depth, b.parent, b.position
    """
    with open_export(path) as conn:
        for row in conn.execute(query):
            path_parts = tuple(row["path"].split("\n")) if row["path"] else ()
            yield Bookmark(
                guid=row["guid"],
                type_raw=row["type"],
                url=row["url"],
                title=row["title"],
                position=row["position"],
                parent_guid=row["parent_guid"],
                path=path_parts,
                date_added_us=row["dateAdded"],
                last_modified_us=row["lastModified"],
                snapshot=snapshot,
            )


class _DownloadMeta(NamedTuple):
    state: int | None
    ended_at_ms: int | None
    file_size: int | None
    deleted: bool | None


_NO_META = _DownloadMeta(None, None, None, None)


def _download_metadata(raw: str | None) -> _DownloadMeta:
    """Pull the fields out of a `downloads/metaData` annotation.

    Firefox writes JSON into an annotation, so this is the one place in CPI where a
    column has to be parsed rather than read. A malformed or absent blob yields
    None throughout rather than raising: the annotation is independent of the
    destination one, and one reference snapshot has a destination without it.

    Types are checked individually because this is a JSON blob in a text column,
    with no schema to stop a future release changing a field. `bool` before `int`
    for `state` would be wrong the other way round — in Python `True` is an `int`,
    so `deleted` has to be tested as `bool` explicitly.
    """
    if raw is None:
        return _NO_META
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return _NO_META
    if not isinstance(parsed, dict):
        return _NO_META

    def as_int(key: str) -> int | None:
        value = parsed.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    deleted = parsed.get("deleted")
    return _DownloadMeta(
        state=as_int("state"),
        ended_at_ms=as_int("endTime"),
        file_size=as_int("fileSize"),
        deleted=deleted if isinstance(deleted, bool) else None,
    )


def downloads(path: Path, snapshot: Snapshot) -> Iterator[Download]:
    """Completed downloads from this snapshot, oldest first.

    Reconstructed from the two `downloads/*` annotations, joined on the page they
    annotate. Only the latest download of a given URL exists: the annotation is
    keyed `(place_id, anno_attribute_id)`, so downloading the same file twice
    overwrites rather than appends.
    """
    query = f"""
        SELECT p.url, dest.content AS destination, meta.content AS metadata,
               dest.dateAdded
        FROM moz_annos dest
        JOIN moz_anno_attributes da ON da.id = dest.anno_attribute_id
        JOIN moz_places p ON p.id = dest.place_id
        LEFT JOIN moz_anno_attributes ma ON ma.name = '{ANNO_METADATA}'
        LEFT JOIN moz_annos meta
               ON meta.place_id = dest.place_id
              AND meta.anno_attribute_id = ma.id
        WHERE da.name = '{ANNO_DESTINATION}'
        ORDER BY dest.dateAdded
    """
    with open_export(path) as conn:
        for row in conn.execute(query):
            meta = _download_metadata(row["metadata"])
            yield Download(
                url=row["url"],
                destination=row["destination"],
                file_size=meta.file_size,
                ended_at_ms=meta.ended_at_ms,
                state_raw=meta.state,
                deleted=meta.deleted,
                date_added_us=row["dateAdded"],
                snapshot=snapshot,
            )


def origins(path: Path, snapshot: Snapshot) -> Iterator[Origin]:
    """Sites this snapshot knows, with Firefox's own per-site relevance score."""
    query = """
        SELECT prefix, host, frecency FROM moz_origins ORDER BY prefix, host
    """
    with open_export(path) as conn:
        for row in conn.execute(query):
            yield Origin(
                prefix=row["prefix"],
                site=row["host"],
                frecency_raw=row["frecency"],
                snapshot=snapshot,
            )


def typed_inputs(path: Path, snapshot: Snapshot) -> Iterator[TypedInput]:
    """Address bar strings and the pages they were used to reach."""
    query = """
        SELECT i.input, p.url, i.use_count
        FROM moz_inputhistory i
        JOIN moz_places p ON p.id = i.place_id
        ORDER BY i.use_count DESC
    """
    with open_export(path) as conn:
        for row in conn.execute(query):
            yield TypedInput(
                input=row["input"],
                url=row["url"],
                use_count=row["use_count"],
                snapshot=snapshot,
            )


def stats(path: Path) -> dict[str, int]:
    """Row counts per non-empty table, plus the schema version. For probing.

    Skips empty tables, as the Gadgetbridge equivalent does: `places.sqlite` has
    several that only Sync or a Mozilla-account feature ever fills, and an empty
    one is a finding in itself — it is how you learn this profile has no tags and
    no saved keywords.
    """
    with open_export(path) as conn:
        tables = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        counts = {
            t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables
        }
        out = {t: n for t, n in counts.items() if n}
        out["_user_version"] = schema_version(conn)
        return out
