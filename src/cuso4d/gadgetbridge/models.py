"""Types for Gadgetbridge data.

Gadgetbridge stores everything in one greenDAO SQLite database, exported inside a
zip by its own backup feature. Unlike PipePipe, samples accumulate: one row per
minute per device, never rewritten, so a single export already holds the whole
history.

Timestamp units are not consistent across tables, which is the single easiest
thing to get wrong here. Activity samples and battery levels are **seconds** since
the epoch; stress samples are **milliseconds**. Reading the stress table as seconds
yields plausible-looking rows in the year 56783. Each model below converts its own
column and exposes an aware `datetime`, so nothing downstream has to remember.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from functools import cached_property

# Gadgetbridge's own sentinel for "this field was not measured", shared by every
# numeric column of an activity sample.
NOT_MEASURED = -1

# `HeartRateUtils.isValidHeartRateValue`, which is what the app's own charts apply.
# The band writes 255 for a failed reading; upstream rejects it by being out of
# range rather than by naming it, and this export also holds a handful of 0 and -1.
MIN_HEART_RATE = 10
MAX_HEART_RATE = 250


class ActivityKind(IntEnum):
    """What the band thought was happening, after masking.

    These are the low nibble of `RAW_KIND`, as `HuamiConst` defines them. The
    stored byte carries more than this — see `masked_kind` — so never compare a raw
    column against these values.

    `NO_CHANGE` and `IGNORE` are not states but instructions to keep the previous
    one; they should not survive into anything a reader sees. `parser.activity`
    resolves them, and their presence in output means the carry-forward failed.
    """

    UNSET = -1
    NO_CHANGE = 0
    ACTIVITY = 1
    RUNNING = 2
    NONWEAR = 3
    RIDE_BIKE = 4
    CHARGING = 6
    LIGHT_SLEEP = 9
    IGNORE = 10
    DEEP_SLEEP = 11
    WAKE_UP = 12

    @classmethod
    def _missing_(cls, value: object) -> None:
        # A firmware can emit a nibble no Gadgetbridge release names. Callers get
        # the raw int rather than a crash; keeping the export readable wins.
        return None


#: Kinds that mean "carry the previous kind forward" rather than naming a state.
CARRY_FORWARD = frozenset({ActivityKind.NO_CHANGE, ActivityKind.IGNORE})

#: Kinds counted as asleep. Mi Band 5 through Gadgetbridge has no REM staging, so
#: this is light + deep only; `WAKE_UP` is a mid-sleep awakening and deliberately
#: excluded, being awake time however brief.
SLEEP_KINDS = frozenset({ActivityKind.LIGHT_SLEEP, ActivityKind.DEEP_SLEEP})

#: Kinds meaning the band was not on a wrist. Charging counts: upstream maps it to
#: NOT_WORN too, since you take the band off to charge it.
UNWORN_KINDS = frozenset({ActivityKind.NONWEAR, ActivityKind.CHARGING})


#: Cadence the band records at, when it records. Not a guarantee about any row.
SAMPLE_INTERVAL = timedelta(minutes=1)


def from_seconds(s: int) -> datetime:
    return datetime.fromtimestamp(s, tz=UTC)


def from_millis(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


@dataclass(frozen=True)
class ActivitySample:
    """One row of the per-minute activity series.

    Identity is `(timestamp, device_id)` — the table's own primary key, and stable
    across exports because it comes from the clock rather than a rowid.

    Not every row is a minute. During a live heart rate measurement Gadgetbridge
    writes sub-minute rows, which is why `is_realtime` exists: counting rows to get
    minutes overstates coverage, in the reference export by enough to put a month
    above 100%.
    """

    timestamp_s: int
    device_id: int
    user_id: int

    raw_kind: int
    """The stored `RAW_KIND` byte, verbatim. Prefer `kind`.

    Only the low nibble is the activity type; the high bits carry something else
    that Gadgetbridge does not decode, so the same state appears under many byte
    values (`LIGHT_SLEEP` shows up as 249, 121, 89, 153, 25 ...).
    """

    raw_intensity: int
    raw_steps: int
    raw_heart_rate: int

    snapshot: str
    """Filename of the export this was read from."""

    @property
    def when(self) -> datetime:
        return from_seconds(self.timestamp_s)

    @property
    def key(self) -> tuple[int, int]:
        return (self.timestamp_s, self.device_id)

    @property
    def masked_kind(self) -> int:
        """The activity nibble, before carry-forward.

        `UNSET` is left alone: it is already -1, and masking it would turn it into
        15. In the reference export 84% of rows mask to `NO_CHANGE` or `IGNORE`,
        so grouping by this without resolving them reports 2% of minutes as sleep
        where the true figure is near 33%.
        """
        if self.raw_kind == ActivityKind.UNSET:
            return ActivityKind.UNSET
        return self.raw_kind & 0xF

    @property
    def kind(self) -> ActivityKind | None:
        """The resolved activity kind, or None for a nibble no release names.

        `parser.activity` has already applied the carry-forward, so this is a
        state, not an instruction.
        """
        return ActivityKind(self.masked_kind)

    @property
    def heart_rate(self) -> int | None:
        """Beats per minute, or None if the reading failed.

        The band writes 255 on a failed measurement, which is a plausible number
        and not obviously wrong in an average. Roughly 5% of rows in the reference
        export are such failures.
        """
        if MIN_HEART_RATE <= self.raw_heart_rate <= MAX_HEART_RATE:
            return self.raw_heart_rate
        return None

    @property
    def steps(self) -> int | None:
        return None if self.raw_steps == NOT_MEASURED else self.raw_steps

    @property
    def intensity(self) -> int | None:
        return None if self.raw_intensity == NOT_MEASURED else self.raw_intensity

    @property
    def is_realtime(self) -> bool:
        """True for a sub-minute row written during a live heart rate measurement.

        Identified by carrying no intensity rather than by clock alignment: in the
        reference export the count of `raw_intensity == -1` rows matches the count
        of rows off the minute boundary exactly, and the intensity test does not
        assume the band's clock was ever aligned.
        """
        return self.raw_intensity == NOT_MEASURED

    @property
    def asleep(self) -> bool:
        return self.masked_kind in SLEEP_KINDS

    @property
    def worn(self) -> bool:
        """False when the band was off the wrist or charging.

        The distinction that matters for any daily total: an unworn minute is not a
        minute of zero steps, and averaging it in understates every rate.
        """
        return self.masked_kind not in UNWORN_KINDS


@dataclass(frozen=True)
class SleepSession:
    """One run of sleep, assembled from consecutive sleeping minutes.

    A session, not a night: this is whatever the minutes support, so a nap is one too.
    Deciding which sessions count as nights is a presentation choice and belongs to
    the caller.

    `span` exceeds `asleep` by the awakenings inside the session. Both are wanted —
    one is roughly time in bed, the other time actually asleep — and neither is the
    clinical definition, since the band cannot know when someone got into bed.
    """

    start_s: int
    end_s: int
    light_minutes: int
    deep_minutes: int
    heart_rates: tuple[int, ...]
    """Valid readings taken while asleep, in time order.

    The only route to a resting heart rate for this band, which records no such
    series of its own. A low percentile of these is the usual proxy.
    """

    @property
    def start(self) -> datetime:
        return from_seconds(self.start_s)

    @property
    def end(self) -> datetime:
        return from_seconds(self.end_s)

    @property
    def asleep_minutes(self) -> int:
        return self.light_minutes + self.deep_minutes

    @property
    def asleep(self) -> timedelta:
        return timedelta(minutes=self.asleep_minutes)

    @property
    def span(self) -> timedelta:
        """First to last sleeping minute, inclusive of both."""
        return timedelta(seconds=self.end_s - self.start_s) + SAMPLE_INTERVAL

    @property
    def deep_share(self) -> float | None:
        """Fraction of sleeping minutes scored as deep, or None for an empty session.

        Expect this to run low: a Mi Band 5 through Gadgetbridge has no REM staging
        and its deep detection is weak, landing near 10% against the 13-23% usually
        quoted. Sound for comparing nights to each other, not against a reference.
        """
        if not self.asleep_minutes:
            return None
        return self.deep_minutes / self.asleep_minutes


@dataclass(frozen=True)
class DischargeRun:
    """One stretch of battery drain, from a charge to the next.

    Report `percent_per_day` rather than a projected runtime. Dividing a run's
    duration by its drop to get "days a full charge would last" explodes for small
    drops — runs of one or two points reach 400+ days, enough to dominate a median
    and invent a trend. The rate needs no extrapolation and holds steady across run
    lengths.
    """

    start_s: int
    end_s: int
    from_level: int
    to_level: int

    @property
    def start(self) -> datetime:
        return from_seconds(self.start_s)

    @property
    def end(self) -> datetime:
        return from_seconds(self.end_s)

    @property
    def duration(self) -> timedelta:
        return timedelta(seconds=self.end_s - self.start_s)

    @property
    def drop(self) -> int:
        """Points of charge lost. Positive by construction."""
        return self.from_level - self.to_level

    @property
    def percent_per_day(self) -> float | None:
        """Charge lost per day, or None for a run with no elapsed time."""
        days = self.duration.total_seconds() / 86400
        if days <= 0:
            return None
        return self.drop / days


class StressType(IntEnum):
    """`StressSample.Type`. The reference export is almost entirely automatic."""

    MANUAL = 0
    AUTOMATIC = 1
    UNKNOWN = 2

    @classmethod
    def _missing_(cls, value: object) -> None:
        return None


@dataclass(frozen=True)
class StressSample:
    """One stress reading. Roughly every 5 minutes when measured automatically.

    Note the millisecond timestamp, unlike `ActivitySample`.
    """

    timestamp_ms: int
    device_id: int
    user_id: int
    type_num: int
    stress: int
    """Raw column value.

    Upstream documents 0-100 but does not enforce it, and the reference export has
    a few values past 250. Use `stress_pct`, which drops those, unless you
    specifically want to see them.
    """

    snapshot: str

    @property
    def when(self) -> datetime:
        return from_millis(self.timestamp_ms)

    @property
    def key(self) -> tuple[int, int]:
        return (self.timestamp_ms, self.device_id)

    @property
    def type(self) -> StressType | None:
        return StressType(self.type_num)

    @property
    def stress_pct(self) -> int | None:
        """The reading if it is in the documented 0-100 range, else None."""
        return self.stress if 0 <= self.stress <= 100 else None

    @property
    def minute_s(self) -> int:
        """Timestamp truncated to the second-based minute an `ActivitySample` uses.

        The join key between the two series, which otherwise cannot be compared at
        all given the different units.
        """
        return self.timestamp_ms // 60_000 * 60


@dataclass(frozen=True)
class BatteryLevel:
    """A battery reading, in seconds like the activity table."""

    timestamp_s: int
    device_id: int
    level: int
    battery_index: int
    """Which battery, for devices with more than one. Always 0 on a Mi Band."""

    snapshot: str

    @property
    def when(self) -> datetime:
        return from_seconds(self.timestamp_s)

    @property
    def key(self) -> tuple[int, int, int]:
        return (self.timestamp_s, self.device_id, self.battery_index)


@dataclass(frozen=True)
class Device:
    """A paired device.

    Identity is `identifier`, the MAC address — a personal identifier, so treat it
    as such. Deliberately not carrying the table's `_id`, which is a local
    autoincrement that renumbers on restore; `device_id` on the samples refers to
    it, so resolve those through `parser.devices` within one snapshot rather than
    comparing ids across snapshots.
    """

    identifier: str
    name: str
    manufacturer: str
    type_name: str
    model: str | None
    alias: str | None
    snapshot: str


@dataclass(frozen=True)
class UserAttributes:
    """Body metrics as of a validity window, used by the band to compute its own
    derived figures. Gadgetbridge appends a new row on every edit rather than
    updating, so several may exist with `valid_to` set on the superseded ones."""

    height_cm: int
    weight_kg: int
    sleep_goal_hours: int | None
    steps_goal: int | None
    valid_from_ms: int | None
    valid_to_ms: int | None
    snapshot: str

    @cached_property
    def valid_from(self) -> datetime | None:
        return None if self.valid_from_ms is None else from_millis(self.valid_from_ms)

    @cached_property
    def valid_to(self) -> datetime | None:
        """End of the validity window, or None if this is the current row."""
        return None if self.valid_to_ms is None else from_millis(self.valid_to_ms)
