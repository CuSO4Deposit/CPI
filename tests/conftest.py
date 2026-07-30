from __future__ import annotations

import sqlite3
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


def make_export(
    path: Path,
    *,
    streams: Sequence[tuple[int, int, str, str, int]],
    history: Sequence[tuple[int, int, int]],
    progress: Sequence[tuple[int, int]] = (),
    search: Sequence[tuple[int, int, str]] = (),
    user_version: int = 901,
) -> Path:
    """Build a fake PipePipe export.

    streams: (uid, service_id, url, title, duration)
    history: (stream_id, access_date_ms, repeat_count)
    progress: (stream_id, progress_ms)
    search: (creation_date_ms, service_id, query)
    """
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {int(user_version)}")
    conn.executemany(_INSERT_STREAM, streams)
    conn.executemany("INSERT INTO stream_history VALUES (?, ?, ?)", history)
    conn.executemany("INSERT INTO stream_state VALUES (?, ?)", progress)
    conn.executemany(_INSERT_SEARCH, search)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def real_export() -> Path:
    """A real PipePipe database, if one happens to be lying around."""
    path = Path("/home/cuso4d/empty/newpipe.db")
    if not path.exists():
        pytest.skip("no real export available")
    return path
