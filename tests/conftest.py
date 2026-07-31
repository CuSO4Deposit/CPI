from __future__ import annotations

import os
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
