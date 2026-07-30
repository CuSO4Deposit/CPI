"""Types for PipePipe data.

PipePipe is a hard fork of NewPipe; its export is a plain Android Room SQLite
database. All timestamps in that database are milliseconds since the Unix epoch,
in UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum
from functools import cached_property


class Service(IntEnum):
    """PipePipe's service ids.

    These are NOT the same as upstream NewPipe's numbering, since the fork adds
    its own services. Verified against a real export (user_version 901).
    """

    YOUTUBE = 0
    SOUNDCLOUD = 1
    MEDIA_CCC = 2
    PEERTUBE = 3
    BANDCAMP = 4
    BILIBILI = 5
    NICONICO = 6

    @classmethod
    def _missing_(cls, value: object) -> None:
        # A new fork release can add services; callers get the raw int instead of
        # a crash. Keeping the export readable matters more than a tidy enum.
        return None


def from_millis(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


@dataclass(frozen=True)
class Stream:
    """A video, as PipePipe knows it. Identity is `(service_id, url)`.

    Deliberately not carrying the database's `uid`: that is a per-export
    autoincrement which renumbers when the app is reinstalled, so it is
    meaningless across snapshots.
    """

    service_id: int
    url: str
    title: str
    stream_type: str
    duration_s: int
    uploader: str
    uploader_url: str | None
    thumbnail_url: str | None
    view_count: int | None
    upload_date_ms: int | None
    upload_date_is_approximate: bool

    @property
    def service(self) -> Service | None:
        return Service(self.service_id)

    @property
    def key(self) -> tuple[int, str]:
        return (self.service_id, self.url)

    @cached_property
    def upload_date(self) -> datetime | None:
        """When the video was published, or None if unknown.

        Check `upload_date_is_approximate` before relying on this: PipePipe
        derives some of these from relative strings like "3 days ago".
        """
        if self.upload_date_ms is None:
            return None
        return from_millis(self.upload_date_ms)


@dataclass(frozen=True)
class WatchRecord:
    """One `stream_history` row, as observed in one snapshot.

    Beware: this is not a single view. PipePipe keeps exactly one history row per
    stream, overwriting `access_date` on every play and incrementing
    `repeat_count`. So this means "as of `snapshot`, last watched at
    `last_watched`, `repeat_count` times in total".
    """

    stream: Stream
    last_watched_ms: int
    repeat_count: int
    progress_ms: int | None

    snapshot: str
    """Filename of the export this was read from."""

    schema_version: int
    """The export's Room `user_version`, e.g. 901.

    Provenance only — deliberately not used to decide how to parse anything. The
    assumption that matters here (one history row per video) lives in
    `onViewed()`, not in the schema, so it can change without this number moving,
    and this number can move for migrations that don't affect us. Feature-detect
    columns with `PRAGMA table_info` when they start drifting.
    """

    @cached_property
    def last_watched(self) -> datetime:
        return from_millis(self.last_watched_ms)

    @property
    def marked_only(self) -> bool:
        """True if manually marked as watched rather than actually played.

        PipePipe's `markAsWatched` inserts a history row with repeat_count 0.
        """
        return self.repeat_count == 0

    @property
    def progress_ratio(self) -> float | None:
        """How far into the video playback got, 0..1, if both values are known."""
        if self.progress_ms is None or self.stream.duration_s <= 0:
            return None
        return min(1.0, self.progress_ms / (self.stream.duration_s * 1000))


@dataclass(frozen=True)
class Search:
    service_id: int
    query: str
    when_ms: int
    snapshot: str
    schema_version: int

    @cached_property
    def when(self) -> datetime:
        return from_millis(self.when_ms)

    @property
    def key(self) -> tuple[int, str, int]:
        return (self.service_id, self.query, self.when_ms)


@dataclass(frozen=True)
class Subscription:
    service_id: int
    url: str
    name: str | None
    avatar_url: str | None
    subscriber_count: int | None
    snapshot: str
    schema_version: int

    @property
    def key(self) -> tuple[int, str]:
        return (self.service_id, self.url)


@dataclass(frozen=True)
class FeedEntry:
    """A video that appeared in a subscription's feed.

    Interesting precisely because it is independent of watch history: a feed entry
    with no matching `WatchRecord` is something that was put in front of you and
    passed over. The app prunes old feed rows, so successive snapshots see
    different windows.
    """

    stream: Stream
    subscription: Subscription
    snapshot: str
    schema_version: int

    @property
    def key(self) -> tuple[int, str, int, str]:
        return (*self.stream.key, *self.subscription.key)


@dataclass(frozen=True)
class RemotePlaylist:
    """A playlist owned by the service, bookmarked rather than built locally.

    Only the reference is stored: PipePipe records the name and stream count but
    not the member videos, so there is no way to expand one from an export alone.
    """

    service_id: int
    url: str
    name: str | None
    uploader: str | None
    thumbnail_url: str | None
    stream_count: int | None
    snapshot: str
    schema_version: int

    @property
    def service(self) -> Service | None:
        return Service(self.service_id)

    @property
    def key(self) -> tuple[int, str]:
        return (self.service_id, self.url)


@dataclass(frozen=True)
class PlaylistEntry:
    """A stream's membership in a local playlist, as of one snapshot."""

    playlist_name: str | None
    index: int
    stream: Stream
    snapshot: str
    schema_version: int
