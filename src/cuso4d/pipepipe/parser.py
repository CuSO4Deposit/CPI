"""Reading a single PipePipe export.

Every function here takes an explicit path and reads exactly one snapshot. No
configuration, no cross-snapshot logic, so tests can point at a fixture file.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from cuso4d.core.sqlite import sqlite_ro, sqlite_ro_in_zip, zip_members
from cuso4d.pipepipe.models import (
    FeedEntry,
    PlaylistEntry,
    RemotePlaylist,
    Search,
    Stream,
    Subscription,
    WatchRecord,
)

# Inside a PipePipe export zip the database is stored under this name (inherited
# from NewPipe, which ships the file as newpipe.db).
DB_MEMBER = "newpipe.db"

_STREAM_COLUMNS = """
    s.service_id, s.url, s.title, s.stream_type, s.duration, s.uploader,
    s.uploader_url, s.thumbnail_url, s.view_count, s.upload_date,
    s.is_upload_date_approximation
"""


@contextmanager
def open_export(path: Path) -> Iterator[sqlite3.Connection]:
    """Open an export, whether it is a bare .db or a .zip containing one."""
    if path.suffix == ".zip":
        members = zip_members(path)
        db = next((m for m in members if Path(m).name == DB_MEMBER), None)
        if db is None:
            db = next((m for m in members if m.endswith(".db")), None)
        if db is None:
            raise ValueError(f"no database found inside export {path.name}: {members}")
        with sqlite_ro_in_zip(path, db) as conn:
            yield conn
    else:
        with sqlite_ro(path) as conn:
            yield conn


def schema_version(conn: sqlite3.Connection) -> int:
    """The export's Room `user_version`. Recorded for provenance, never branched on."""
    version: int = conn.execute("PRAGMA user_version").fetchone()[0]
    return version


def _stream(row: sqlite3.Row) -> Stream:
    return Stream(
        service_id=row["service_id"],
        url=row["url"],
        title=row["title"],
        stream_type=row["stream_type"],
        duration_s=row["duration"],
        uploader=row["uploader"],
        uploader_url=row["uploader_url"],
        thumbnail_url=row["thumbnail_url"],
        view_count=row["view_count"],
        upload_date_ms=row["upload_date"],
        upload_date_is_approximate=bool(row["is_upload_date_approximation"]),
    )


def watched(path: Path) -> Iterator[WatchRecord]:
    """Watch history from one snapshot, oldest first."""
    query = f"""
        SELECT {_STREAM_COLUMNS},
               h.access_date, h.repeat_count, st.progress_time
        FROM stream_history h
        JOIN streams s ON s.uid = h.stream_id
        LEFT JOIN stream_state st ON st.stream_id = h.stream_id
        ORDER BY h.access_date
    """
    name = path.name
    with open_export(path) as conn:
        version = schema_version(conn)
        for row in conn.execute(query):
            yield WatchRecord(
                stream=_stream(row),
                last_watched_ms=row["access_date"],
                repeat_count=row["repeat_count"],
                progress_ms=row["progress_time"],
                snapshot=name,
                schema_version=version,
            )


def searches(path: Path) -> Iterator[Search]:
    query = """
        SELECT service_id, search, creation_date
        FROM search_history
        WHERE search IS NOT NULL AND creation_date IS NOT NULL
        ORDER BY creation_date
    """
    name = path.name
    with open_export(path) as conn:
        version = schema_version(conn)
        for row in conn.execute(query):
            yield Search(
                service_id=row["service_id"],
                query=row["search"],
                when_ms=row["creation_date"],
                snapshot=name,
                schema_version=version,
            )


_SUBSCRIPTION_COLUMNS = """
    b.service_id AS sub_service_id, b.url AS sub_url, b.name AS sub_name,
    b.avatar_url AS sub_avatar_url, b.subscriber_count AS sub_subscriber_count
"""


def _subscription(row: sqlite3.Row, snapshot: str, version: int) -> Subscription:
    return Subscription(
        service_id=row["sub_service_id"],
        url=row["sub_url"],
        name=row["sub_name"],
        avatar_url=row["sub_avatar_url"],
        subscriber_count=row["sub_subscriber_count"],
        snapshot=snapshot,
        schema_version=version,
    )


def subscriptions(path: Path) -> Iterator[Subscription]:
    query = f"""
        SELECT {_SUBSCRIPTION_COLUMNS}
        FROM subscriptions b
        WHERE b.url IS NOT NULL
        ORDER BY b.name
    """
    name = path.name
    with open_export(path) as conn:
        version = schema_version(conn)
        for row in conn.execute(query):
            yield _subscription(row, name, version)


def streams(path: Path) -> Iterator[Stream]:
    """Every video the app knows about, watched or not.

    The `streams` table is an entity in its own right, not an appendix to history:
    videos reachable only through a playlist or a subscription feed have no
    history row at all, and are invisible to `watched()`.
    """
    query = f"SELECT {_STREAM_COLUMNS} FROM streams s ORDER BY s.uid"
    with open_export(path) as conn:
        for row in conn.execute(query):
            yield _stream(row)


def feed(path: Path) -> Iterator[FeedEntry]:
    """Videos that showed up in a subscription's feed, in this snapshot's window."""
    query = f"""
        SELECT {_STREAM_COLUMNS}, {_SUBSCRIPTION_COLUMNS}
        FROM feed f
        JOIN streams s ON s.uid = f.stream_id
        JOIN subscriptions b ON b.uid = f.subscription_id
        ORDER BY s.upload_date
    """
    name = path.name
    with open_export(path) as conn:
        version = schema_version(conn)
        for row in conn.execute(query):
            yield FeedEntry(
                stream=_stream(row),
                subscription=_subscription(row, name, version),
                snapshot=name,
                schema_version=version,
            )


def remote_playlists(path: Path) -> Iterator[RemotePlaylist]:
    """Bookmarked service-side playlists. Members are not stored, only the count."""
    query = """
        SELECT service_id, url, name, uploader, thumbnail_url, stream_count
        FROM remote_playlists
        WHERE url IS NOT NULL
        ORDER BY name
    """
    name = path.name
    with open_export(path) as conn:
        version = schema_version(conn)
        for row in conn.execute(query):
            yield RemotePlaylist(
                service_id=row["service_id"],
                url=row["url"],
                name=row["name"],
                uploader=row["uploader"],
                thumbnail_url=row["thumbnail_url"],
                stream_count=row["stream_count"],
                snapshot=name,
                schema_version=version,
            )


def playlist_entries(path: Path) -> Iterator[PlaylistEntry]:
    """Local playlist contents. Remote playlists are just references, skipped here."""
    query = f"""
        SELECT {_STREAM_COLUMNS}, p.name AS playlist_name, j.join_index
        FROM playlist_stream_join j
        JOIN playlists p ON p.uid = j.playlist_id
        JOIN streams s ON s.uid = j.stream_id
        ORDER BY p.name, j.join_index
    """
    name = path.name
    with open_export(path) as conn:
        version = schema_version(conn)
        for row in conn.execute(query):
            yield PlaylistEntry(
                playlist_name=row["playlist_name"],
                index=row["join_index"],
                stream=_stream(row),
                snapshot=name,
                schema_version=version,
            )


def stats(path: Path) -> dict[str, int]:
    """Row counts per table, plus the Room schema version. For probing an export."""
    with open_export(path) as conn:
        tables = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'android_%'"
            )
        ]
        out = {
            t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables
        }
        out["_user_version"] = schema_version(conn)
        return out
