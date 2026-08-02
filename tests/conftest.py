from __future__ import annotations

import lzma
import os
import shutil
import sqlite3
import zipfile
from collections.abc import Sequence
from pathlib import Path

import pytest

# Minimal subset of the PipePipe (Room) schema, copied verbatim from a real export
# so the fixtures exercise the same column types and constraints.
SCHEMA = """
CREATE TABLE `streams` (
    `uid` INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    `service_id` INTEGER NOT NULL, `url` TEXT NOT NULL, `title` TEXT NOT NULL,
    `stream_type` TEXT NOT NULL, `duration` INTEGER NOT NULL,
    `uploader` TEXT NOT NULL, `uploader_url` TEXT, `thumbnail_url` TEXT,
    `view_count` INTEGER, `textual_upload_date` TEXT, `upload_date` INTEGER,
    `is_upload_date_approximation` INTEGER, is_paid INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX `index_streams_service_id_url`
    ON `streams` (`service_id`, `url`);
CREATE TABLE `stream_history` (
    `stream_id` INTEGER NOT NULL, `access_date` INTEGER NOT NULL,
    `repeat_count` INTEGER NOT NULL,
    PRIMARY KEY(`stream_id`, `access_date`),
    FOREIGN KEY(`stream_id`) REFERENCES `streams`(`uid`)
        ON UPDATE CASCADE ON DELETE CASCADE);
CREATE TABLE `stream_state` (
    `stream_id` INTEGER NOT NULL, `progress_time` INTEGER NOT NULL,
    PRIMARY KEY(`stream_id`),
    FOREIGN KEY(`stream_id`) REFERENCES `streams`(`uid`)
        ON UPDATE CASCADE ON DELETE CASCADE);
CREATE TABLE `search_history` (
    `creation_date` INTEGER, `service_id` INTEGER NOT NULL, `search` TEXT,
    `id` INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL);
CREATE TABLE `subscriptions` (
    `uid` INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    `service_id` INTEGER NOT NULL, `url` TEXT, `name` TEXT, `avatar_url` TEXT,
    `subscriber_count` INTEGER, `description` TEXT,
    `notification_mode` INTEGER NOT NULL);
CREATE TABLE `playlists` (
    `uid` INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, `name` TEXT,
    `thumbnail_url` TEXT, `display_index` INTEGER NOT NULL);
CREATE TABLE `playlist_stream_join` (
    `playlist_id` INTEGER NOT NULL, `stream_id` INTEGER NOT NULL,
    `join_index` INTEGER NOT NULL, PRIMARY KEY(`playlist_id`, `join_index`));
CREATE TABLE `remote_playlists` (
    `uid` INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    `service_id` INTEGER NOT NULL, `name` TEXT, `url` TEXT,
    `thumbnail_url` TEXT, `uploader` TEXT, `display_index` INTEGER NOT NULL,
    `stream_count` INTEGER);
CREATE TABLE `feed` (
    `stream_id` INTEGER NOT NULL, `subscription_id` INTEGER NOT NULL,
    PRIMARY KEY(`stream_id`, `subscription_id`));
"""

_INSERT_STREAM = """
INSERT INTO streams
    (uid, service_id, url, title, stream_type, duration, uploader,
     upload_date, is_upload_date_approximation)
VALUES (?, ?, ?, ?, 'VIDEO_STREAM', ?, 'someone', NULL, 0)
"""

_INSERT_SEARCH = """
INSERT INTO search_history (creation_date, service_id, search) VALUES (?, ?, ?)
"""

_INSERT_SUBSCRIPTION = """
INSERT INTO subscriptions (uid, service_id, url, name, notification_mode)
VALUES (?, ?, ?, ?, 0)
"""

_INSERT_REMOTE_PLAYLIST = """
INSERT INTO remote_playlists
    (service_id, name, url, uploader, display_index, stream_count)
VALUES (?, ?, ?, ?, 0, ?)
"""


def make_export(
    path: Path,
    *,
    streams: Sequence[tuple[int, int, str, str, int]],
    history: Sequence[tuple[int, int, int]],
    progress: Sequence[tuple[int, int]] = (),
    search: Sequence[tuple[int, int, str]] = (),
    subscriptions: Sequence[tuple[int, int, str, str]] = (),
    feed: Sequence[tuple[int, int]] = (),
    remote_playlists: Sequence[tuple[int, str, str, str, int]] = (),
    user_version: int = 901,
) -> Path:
    """Build a fake PipePipe export.

    streams: (uid, service_id, url, title, duration)
    history: (stream_id, access_date_ms, repeat_count)
    progress: (stream_id, progress_ms)
    search: (creation_date_ms, service_id, query)
    subscriptions: (uid, service_id, url, name)
    feed: (stream_id, subscription_id)
    remote_playlists: (service_id, name, url, uploader, stream_count)
    """
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {int(user_version)}")
    conn.executemany(_INSERT_STREAM, streams)
    conn.executemany("INSERT INTO stream_history VALUES (?, ?, ?)", history)
    conn.executemany("INSERT INTO stream_state VALUES (?, ?)", progress)
    conn.executemany(_INSERT_SEARCH, search)
    conn.executemany(_INSERT_SUBSCRIPTION, subscriptions)
    conn.executemany("INSERT INTO feed VALUES (?, ?)", feed)
    conn.executemany(_INSERT_REMOTE_PLAYLIST, remote_playlists)
    conn.commit()
    conn.close()
    return path


# Minimal subset of the Gadgetbridge (greenDAO) schema, copied verbatim from a real
# backup. `WITHOUT ROWID` and `ON CONFLICT REPLACE` are load-bearing: they are what
# make a sample's identity `(timestamp, device)`, which accumulation relies on.
GB_SCHEMA = """
CREATE TABLE "MI_BAND_ACTIVITY_SAMPLE" ("TIMESTAMP" INTEGER  NOT NULL ,
    "DEVICE_ID" INTEGER  NOT NULL ,"USER_ID" INTEGER NOT NULL ,
    "RAW_INTENSITY" INTEGER NOT NULL ,"STEPS" INTEGER NOT NULL ,
    "RAW_KIND" INTEGER NOT NULL ,"HEART_RATE" INTEGER NOT NULL ,
    PRIMARY KEY ("TIMESTAMP" ,"DEVICE_ID" ) ON CONFLICT REPLACE) WITHOUT ROWID;
CREATE TABLE "HUAMI_STRESS_SAMPLE" ("TIMESTAMP" INTEGER  NOT NULL ,
    "DEVICE_ID" INTEGER  NOT NULL ,"USER_ID" INTEGER NOT NULL ,
    "TYPE_NUM" INTEGER NOT NULL ,"STRESS" INTEGER NOT NULL ,
    PRIMARY KEY ("TIMESTAMP" ,"DEVICE_ID" ) ON CONFLICT REPLACE) WITHOUT ROWID;
CREATE TABLE "BATTERY_LEVEL" ("TIMESTAMP" INTEGER  NOT NULL ,
    "DEVICE_ID" INTEGER  NOT NULL ,"LEVEL" INTEGER NOT NULL ,
    "BATTERY_INDEX" INTEGER  NOT NULL ,
    PRIMARY KEY ("TIMESTAMP" ,"DEVICE_ID" ,"BATTERY_INDEX" ) ON CONFLICT REPLACE)
    WITHOUT ROWID;
CREATE TABLE "DEVICE" ("_id" INTEGER PRIMARY KEY ,"NAME" TEXT NOT NULL ,
    "MANUFACTURER" TEXT NOT NULL ,"IDENTIFIER" TEXT NOT NULL UNIQUE ,
    "TYPE" INTEGER NOT NULL ,"TYPE_NAME" TEXT NOT NULL ,"MODEL" TEXT,"ALIAS" TEXT,
    "PARENT_FOLDER" TEXT);
CREATE TABLE "USER_ATTRIBUTES" ("_id" INTEGER PRIMARY KEY ,
    "HEIGHT_CM" INTEGER NOT NULL ,"WEIGHT_KG" INTEGER NOT NULL ,
    "SLEEP_GOAL_HPD" INTEGER,"STEPS_GOAL_SPD" INTEGER,"VALID_FROM_UTC" INTEGER,
    "VALID_TO_UTC" INTEGER,"USER_ID" INTEGER NOT NULL );
CREATE TABLE "HUAMI_SPO2_SAMPLE" ("TIMESTAMP" INTEGER NOT NULL ,
    "DEVICE_ID" INTEGER NOT NULL ,"USER_ID" INTEGER NOT NULL ,
    "TYPE_NUM" INTEGER NOT NULL ,"SPO2" INTEGER NOT NULL ,
    PRIMARY KEY ("TIMESTAMP" ,"DEVICE_ID" ) ON CONFLICT REPLACE) WITHOUT ROWID;
"""

_GB_INSERT_ACTIVITY = """
INSERT INTO MI_BAND_ACTIVITY_SAMPLE
    (TIMESTAMP, DEVICE_ID, USER_ID, RAW_INTENSITY, STEPS, RAW_KIND, HEART_RATE)
VALUES (?, ?, 1, ?, ?, ?, ?)
"""


def make_gb_export(
    path: Path,
    *,
    activity: Sequence[tuple[int, int, int, int, int, int]] = (),
    stress: Sequence[tuple[int, int, int, int]] = (),
    battery: Sequence[tuple[int, int, int, int]] = (),
    devices: Sequence[tuple[int, str, str, str, str]] = (),
    user_attributes: Sequence[tuple[int, int, int, int, int | None, int | None]] = (),
    as_zip: bool = True,
    db_member: str = "database/Gadgetbridge",
    extra_members: Sequence[str] = (),
) -> Path:
    """Build a fake Gadgetbridge backup.

    activity: (timestamp_s, device_id, raw_intensity, steps, raw_kind, heart_rate)
    stress: (timestamp_ms, device_id, type_num, stress)
    battery: (timestamp_s, device_id, level, battery_index)
    devices: (_id, name, manufacturer, identifier, type_name)
    user_attributes: (_id, height_cm, weight_kg, steps_goal, valid_from_ms, valid_to_ms)

    Defaults to a zip, since that is what the app produces and what the member-name
    handling has to get right. `db_member` and `extra_members` exist to build
    malformed backups.
    """
    db = path.parent / "Gadgetbridge" if as_zip else path
    conn = sqlite3.connect(db)
    conn.executescript(GB_SCHEMA)
    conn.executemany(_GB_INSERT_ACTIVITY, activity)
    conn.executemany(
        "INSERT INTO HUAMI_STRESS_SAMPLE (TIMESTAMP, DEVICE_ID, USER_ID, TYPE_NUM,"
        " STRESS) VALUES (?, ?, 1, ?, ?)",
        stress,
    )
    conn.executemany("INSERT INTO BATTERY_LEVEL VALUES (?, ?, ?, ?)", battery)
    conn.executemany(
        "INSERT INTO DEVICE (_id, NAME, MANUFACTURER, IDENTIFIER, TYPE, TYPE_NAME)"
        " VALUES (?, ?, ?, ?, 0, ?)",
        devices,
    )
    conn.executemany(
        "INSERT INTO USER_ATTRIBUTES (_id, HEIGHT_CM, WEIGHT_KG, STEPS_GOAL_SPD,"
        " VALID_FROM_UTC, VALID_TO_UTC, USER_ID) VALUES (?, ?, ?, ?, ?, ?, 1)",
        user_attributes,
    )
    conn.commit()
    conn.close()

    if not as_zip:
        return path

    with zipfile.ZipFile(path, "w") as zf:
        zf.write(db, db_member)
        zf.writestr("gadgetbridge.json", '{"appVersionName": "0.86.1"}')
        for member in extra_members:
            zf.write(db, member)
    db.unlink()
    return path


# Subset of the Firefox `places.sqlite` schema, copied verbatim from a real
# snapshot (user_version 86). The UNIQUE indexes are load-bearing, not decoration:
# they are what make a visit's identity `(place, visit_date)` and an engagement's
# `(place_id, created_at)`, which accumulation relies on. `moz_annos`' unique index
# on `(place_id, anno_attribute_id)` is why only the latest download of a URL
# survives in any one snapshot.
PLACES_SCHEMA = """
CREATE TABLE moz_origins ( id INTEGER PRIMARY KEY, prefix TEXT NOT NULL,
    host TEXT NOT NULL, frecency INTEGER NOT NULL,
    recalc_frecency INTEGER NOT NULL DEFAULT 0, alt_frecency INTEGER,
    recalc_alt_frecency INTEGER NOT NULL DEFAULT 0, block_until_ms INTEGER,
    block_pages_until_ms INTEGER, UNIQUE (prefix, host) );
CREATE TABLE moz_places (   id INTEGER PRIMARY KEY, url LONGVARCHAR,
    title LONGVARCHAR, rev_host LONGVARCHAR, visit_count INTEGER DEFAULT 0,
    hidden INTEGER DEFAULT 0 NOT NULL, typed INTEGER DEFAULT 0 NOT NULL,
    frecency INTEGER DEFAULT -1 NOT NULL, last_visit_date INTEGER , guid TEXT,
    foreign_count INTEGER DEFAULT 0 NOT NULL, url_hash INTEGER DEFAULT 0 NOT NULL ,
    description TEXT, preview_image_url TEXT, site_name TEXT,
    origin_id INTEGER REFERENCES moz_origins(id),
    recalc_frecency INTEGER NOT NULL DEFAULT 0, alt_frecency INTEGER,
    recalc_alt_frecency INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX moz_places_guid_uniqueindex ON moz_places (guid);
CREATE TABLE moz_historyvisits (  id INTEGER PRIMARY KEY, from_visit INTEGER,
    place_id INTEGER, visit_date INTEGER, visit_type INTEGER, session INTEGER,
    source INTEGER DEFAULT 0 NOT NULL, triggeringPlaceId INTEGER);
CREATE TABLE moz_places_metadata (id INTEGER PRIMARY KEY,
    place_id INTEGER NOT NULL, referrer_place_id INTEGER,
    created_at INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0,
    total_view_time INTEGER NOT NULL DEFAULT 0,
    typing_time INTEGER NOT NULL DEFAULT 0, key_presses INTEGER NOT NULL DEFAULT 0,
    scrolling_time INTEGER NOT NULL DEFAULT 0,
    scrolling_distance INTEGER NOT NULL DEFAULT 0,
    document_type INTEGER NOT NULL DEFAULT 0, search_query_id INTEGER,
    FOREIGN KEY (place_id) REFERENCES moz_places(id) ON DELETE CASCADE,
    FOREIGN KEY (referrer_place_id) REFERENCES moz_places(id) ON DELETE CASCADE
    CHECK(place_id != referrer_place_id) );
CREATE UNIQUE INDEX moz_places_metadata_placecreated_uniqueindex
    ON moz_places_metadata (place_id, created_at);
CREATE TABLE moz_bookmarks (  id INTEGER PRIMARY KEY, type INTEGER,
    fk INTEGER DEFAULT NULL, parent INTEGER, position INTEGER, title LONGVARCHAR,
    keyword_id INTEGER, folder_type TEXT, dateAdded INTEGER, lastModified INTEGER,
    guid TEXT, syncStatus INTEGER NOT NULL DEFAULT 0,
    syncChangeCounter INTEGER NOT NULL DEFAULT 1);
CREATE UNIQUE INDEX moz_bookmarks_guid_uniqueindex ON moz_bookmarks (guid);
CREATE TABLE moz_anno_attributes (  id INTEGER PRIMARY KEY,
    name VARCHAR(32) UNIQUE NOT NULL);
CREATE TABLE moz_annos (  id INTEGER PRIMARY KEY, place_id INTEGER NOT NULL,
    anno_attribute_id INTEGER, content LONGVARCHAR, flags INTEGER DEFAULT 0,
    expiration INTEGER DEFAULT 0, type INTEGER DEFAULT 0,
    dateAdded INTEGER DEFAULT 0, lastModified INTEGER DEFAULT 0);
CREATE UNIQUE INDEX moz_annos_placeattributeindex
    ON moz_annos (place_id, anno_attribute_id);
CREATE TABLE moz_inputhistory (  place_id INTEGER NOT NULL,
    input LONGVARCHAR NOT NULL, use_count INTEGER,
    PRIMARY KEY (place_id, input));
"""

#: The roots Firefox creates in every profile, as `(id, parent, guid, title)`.
#: Copied from a real snapshot: ids 1-6, root's parent is 0, and the titles really
#: are these bare lowercase strings rather than anything localized.
PLACES_ROOTS = (
    (1, 0, "root________", ""),
    (2, 1, "menu________", "menu"),
    (3, 1, "toolbar_____", "toolbar"),
    (4, 1, "tags________", "tags"),
    (5, 1, "unfiled_____", "unfiled"),
    (6, 1, "mobile______", "mobile"),
)

_US = 1_000_000


def make_places_export(
    path: Path,
    *,
    places: Sequence[tuple[int, str, str | None, int]] = (),
    visits: Sequence[tuple[int, int, int, int, int]] = (),
    metadata: Sequence[tuple[int, int, int, int]] = (),
    bookmarks: Sequence[tuple[int, int, int, int | None, str, str | None]] = (),
    downloads: Sequence[tuple[int, str, str | None, int]] = (),
    origins: Sequence[tuple[str, str, int]] = (),
    inputs: Sequence[tuple[int, str, float]] = (),
    frecency: dict[int, int] | None = None,
    descriptions: dict[int, bytes] | None = None,
    user_version: int = 86,
    compress: bool = True,
) -> Path:
    """Build a fake Firefox `places.sqlite` snapshot.

    places: (id, url, title, visit_count)
    visits: (id, place_id, visit_date_us, visit_type, from_visit)
    metadata: (place_id, created_at_ms, updated_at_ms, total_view_time_ms)
    bookmarks: (id, type, parent, fk, guid, title) — roots are inserted already
    downloads: (place_id, destination, metadata_json, dateAdded_us)
    origins: (prefix, host, frecency)
    inputs: (place_id, input, use_count)
    frecency: place_id -> frecency, for testing the negative sentinel
    descriptions: place_id -> raw bytes, for testing undecodable text

    Defaults to writing `.xz`, since that is the archived form. `compress=False`
    gives a bare database, which `parser.open_export` also accepts.
    """
    db = path.parent / f"{path.name}.tmp" if compress else path
    conn = sqlite3.connect(db)
    conn.executescript(PLACES_SCHEMA)
    conn.execute(f"PRAGMA user_version = {int(user_version)}")

    conn.executemany(
        "INSERT INTO moz_places (id, url, title, visit_count, frecency, guid,"
        " last_visit_date) VALUES (?, ?, ?, ?, ?, ?, NULL)",
        [
            (pid, url, title, count, (frecency or {}).get(pid, 100), f"place{pid:08d}")
            for pid, url, title, count in places
        ],
    )
    # CAST, because binding bytes directly stores a BLOB and TEXT affinity does not
    # convert one. A real snapshot has invalid UTF-8 in a genuine TEXT column, and
    # only that reproduces the read failure being tested.
    for pid, raw in (descriptions or {}).items():
        conn.execute(
            "UPDATE moz_places SET description = CAST(? AS TEXT) WHERE id = ?",
            (raw, pid),
        )

    conn.executemany(
        "INSERT INTO moz_historyvisits (id, place_id, visit_date, visit_type,"
        " from_visit, session, source) VALUES (?, ?, ?, ?, ?, 0, 0)",
        visits,
    )
    # last_visit_date is derived rather than asked of the caller, so a fixture
    # cannot drift from what Firefox would store. `moz_places.id` is qualified
    # because moz_historyvisits has an `id` too, and the inner scope would win:
    # a bare `id` here silently compares v.place_id against v.id.
    conn.execute(
        "UPDATE moz_places SET last_visit_date = (SELECT max(v.visit_date)"
        " FROM moz_historyvisits v WHERE v.place_id = moz_places.id)"
    )
    conn.executemany(
        "INSERT INTO moz_places_metadata (place_id, created_at, updated_at,"
        " total_view_time) VALUES (?, ?, ?, ?)",
        metadata,
    )
    conn.executemany(
        "INSERT INTO moz_bookmarks (id, parent, guid, title, type, dateAdded,"
        f" lastModified) VALUES (?, ?, ?, ?, 2, {_US}, {_US})",
        PLACES_ROOTS,
    )
    conn.executemany(
        "INSERT INTO moz_bookmarks (id, type, parent, fk, guid, title, position,"
        f" dateAdded, lastModified) VALUES (?, ?, ?, ?, ?, ?, 0, {_US}, {_US})",
        bookmarks,
    )
    if downloads:
        conn.executemany(
            "INSERT INTO moz_anno_attributes (id, name) VALUES (?, ?)",
            [(1, "downloads/destinationFileURI"), (2, "downloads/metaData")],
        )
        conn.executemany(
            "INSERT INTO moz_annos (place_id, anno_attribute_id, content, dateAdded)"
            " VALUES (?, 1, ?, ?)",
            [(pid, dest, added) for pid, dest, _, added in downloads],
        )
        conn.executemany(
            "INSERT INTO moz_annos (place_id, anno_attribute_id, content, dateAdded)"
            " VALUES (?, 2, ?, ?)",
            [(pid, meta, added) for pid, _, meta, added in downloads if meta],
        )
    conn.executemany(
        "INSERT INTO moz_origins (prefix, host, frecency) VALUES (?, ?, ?)", origins
    )
    conn.executemany("INSERT INTO moz_inputhistory VALUES (?, ?, ?)", inputs)
    conn.commit()
    conn.close()

    if not compress:
        return path
    with open(db, "rb") as src, lzma.open(path, "wb") as dst:
        shutil.copyfileobj(src, dst)
    db.unlink()
    return path


@pytest.fixture
def real_places_export() -> Path:
    """A real Firefox snapshot, if `CPI_TEST_FIREFOX_EXPORT` points at one.

    Same rule as the other real-export fixtures: assert only invariants that hold
    for *any* snapshot. A count from one profile says nothing about the code and
    leaks how much of what someone read.
    """
    env = os.environ.get("CPI_TEST_FIREFOX_EXPORT")
    if not env:
        pytest.skip("set CPI_TEST_FIREFOX_EXPORT to run against a real snapshot")
    path = Path(env).expanduser()
    if not path.exists():
        pytest.fail(f"CPI_TEST_FIREFOX_EXPORT points at a missing file: {path}")
    return path


@pytest.fixture
def real_export() -> Path:
    """A real PipePipe export, if `CPI_TEST_PIPEPIPE_EXPORT` points at one.

    Tests using this may only assert invariants that hold for *any* export — never
    counts from one particular database. Real exports are personal data and stay
    out of the repo, so a hardcoded path would mean the test never runs for anyone
    else, and a hardcoded count would fail for them if it did.
    """
    env = os.environ.get("CPI_TEST_PIPEPIPE_EXPORT")
    if not env:
        pytest.skip("set CPI_TEST_PIPEPIPE_EXPORT to run against a real export")
    path = Path(env).expanduser()
    if not path.exists():
        pytest.fail(f"CPI_TEST_PIPEPIPE_EXPORT points at a missing file: {path}")
    return path


@pytest.fixture
def real_gb_export() -> Path:
    """A real Gadgetbridge backup, if `CPI_TEST_GADGETBRIDGE_EXPORT` points at one.

    Same rule as `real_export`: assert only invariants that hold for *any* backup.
    A count from one database says nothing about the code and leaks how much someone
    slept.
    """
    env = os.environ.get("CPI_TEST_GADGETBRIDGE_EXPORT")
    if not env:
        pytest.skip("set CPI_TEST_GADGETBRIDGE_EXPORT to run against a real backup")
    path = Path(env).expanduser()
    if not path.exists():
        pytest.fail(f"CPI_TEST_GADGETBRIDGE_EXPORT points at a missing file: {path}")
    return path
