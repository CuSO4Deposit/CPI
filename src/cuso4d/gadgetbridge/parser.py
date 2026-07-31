"""Reading a single Gadgetbridge backup.

Every function here takes an explicit path and reads exactly one export. No
configuration, no cross-snapshot logic, so tests can point at a fixture file.

Gadgetbridge's database is one greenDAO SQLite file holding a table per device
family, almost all of them empty for any given user. The functions below read the
Huami/Mi Band tables; a different band writes to different tables and would need
its own reader rather than a flag here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

from cuso4d.core.sqlite import sqlite_ro, sqlite_ro_in_zip, zip_members
from cuso4d.gadgetbridge.models import (
    CARRY_FORWARD,
    ActivityKind,
    ActivitySample,
    BatteryLevel,
    Device,
    DischargeRun,
    SleepSession,
    StressSample,
    UserAttributes,
)

# `AbstractZipBackupJob.DATABASE_FILENAME`. A backup zip also contains a `files/`
# tree, which is a verbatim copy of the app's external files directory and in
# practice holds a second database under `files/Gadgetbridge` — the byte-identical
# output of the app's older manual export. We never fall back to it: that copy is
# only as fresh as the last manual export, so a stale one would parse perfectly and
# quietly report older data. Absent the documented member, fail.
DB_MEMBER = "database/Gadgetbridge"

METADATA_MEMBER = "gadgetbridge.json"

#: Beyond this many seconds between consecutive samples, the previous activity kind
#: is not carried into the next row. See `activity`.
DEFAULT_CARRY_LIMIT_S = 3600

#: An awakening up to this long does not end a sleep session. See `sleep_sessions`.
DEFAULT_SLEEP_GAP_S = 3600

#: A rise of more than this many percentage points between consecutive battery
#: readings is a charge rather than rounding noise. See `discharge_runs`.
DEFAULT_CHARGE_RISE = 3


@contextmanager
def open_export(path: Path) -> Iterator[sqlite3.Connection]:
    """Open a backup, whether it is a .zip or a bare database file."""
    if path.suffix == ".zip":
        members = zip_members(path)
        if DB_MEMBER not in members:
            raise ValueError(
                f"no {DB_MEMBER} inside backup {path.name}: {members}",
            )
        with sqlite_ro_in_zip(path, DB_MEMBER) as conn:
            yield conn
    else:
        with sqlite_ro(path) as conn:
            yield conn


def _resolve_kinds(
    rows: Sequence[tuple[int, int]],
    carry_limit_s: int,
) -> list[int]:
    """Resolve `NO_CHANGE`/`IGNORE` rows to the kind they inherit.

    Mirrors `MiBand2SampleProvider.postprocess`, which does this at query time
    rather than on write: the stored byte for most rows says "same as before", so
    without this 84% of the reference export carries no state at all.

    Two deliberate departures from upstream, both because upstream runs over
    whatever window a chart happens to request while this runs over the whole
    series:

    - Upstream seeds the previous kind with a query excluding raw values 16, 80, 96
      and 112 ("all I ever had that are 0 when doing &= 0xf"). That is a workaround
      for starting mid-series; reading from the beginning there is nothing to seed.
    - A carry does not cross a gap longer than `carry_limit_s`. Three of the four
      multi-day gaps in the reference export begin with a carry instruction, so
      without this the state from before a 67-day gap resurfaces after it. Genuine
      runs reach ~400 minutes (a night of sleep), so the limit has to sit above
      that; between one hour and one day the outcome does not move, leaving 0.005%
      of rows unresolved either way.

    Unresolved rows stay `UNSET` rather than being guessed at.
    """
    out: list[int] = []
    previous = int(ActivityKind.UNSET)
    previous_ts: int | None = None

    for timestamp_s, raw_kind in rows:
        if previous_ts is not None and timestamp_s - previous_ts > carry_limit_s:
            previous = int(ActivityKind.UNSET)
        previous_ts = timestamp_s

        masked = raw_kind if raw_kind == ActivityKind.UNSET else raw_kind & 0xF
        if masked in CARRY_FORWARD:
            out.append(previous)
        else:
            out.append(masked)
            previous = masked

    return out


def resolve_kinds(
    samples: Sequence[ActivitySample],
    *,
    carry_limit_s: int = DEFAULT_CARRY_LIMIT_S,
) -> list[ActivitySample]:
    """Rewrite `raw_kind` on a time-ordered series to the resolved activity nibble.

    Separate from reading because the input must be the *whole* series: a carry
    instruction inherits from the row before it, so resolving a subset gives a
    different answer at its leading edge. Cross-snapshot accumulation therefore
    unions raw rows first and calls this once, rather than resolving each snapshot.

    Assumes `samples` is sorted by timestamp and covers one device. Mixing devices
    would let one band's state carry into another's.
    """
    kinds = _resolve_kinds(
        [(s.timestamp_s, s.raw_kind) for s in samples], carry_limit_s
    )
    return [
        replace(sample, raw_kind=kind)
        for sample, kind in zip(samples, kinds, strict=True)
    ]


def activity_raw(path: Path) -> Iterator[ActivitySample]:
    """The per-minute activity series, kinds *unresolved*, oldest first.

    Rarely what you want directly — `raw_kind` here is mostly the instruction
    `NO_CHANGE`, so `kind` is meaningless until `resolve_kinds` has run. Exists so
    accumulation can merge snapshots before resolving.
    """
    query = """
        SELECT TIMESTAMP, DEVICE_ID, USER_ID, RAW_INTENSITY, STEPS, RAW_KIND,
               HEART_RATE
        FROM MI_BAND_ACTIVITY_SAMPLE
        ORDER BY TIMESTAMP
    """
    name = path.name
    with open_export(path) as conn:
        for row in conn.execute(query):
            yield ActivitySample(
                timestamp_s=row["TIMESTAMP"],
                device_id=row["DEVICE_ID"],
                user_id=row["USER_ID"],
                raw_kind=row["RAW_KIND"],
                raw_intensity=row["RAW_INTENSITY"],
                raw_steps=row["STEPS"],
                raw_heart_rate=row["HEART_RATE"],
                snapshot=name,
            )


def activity(
    path: Path,
    *,
    carry_limit_s: int = DEFAULT_CARRY_LIMIT_S,
) -> Iterator[ActivitySample]:
    """The per-minute activity series from one backup, oldest first.

    Yields samples whose `raw_kind` has been rewritten to the resolved activity
    nibble, so `kind` is a state rather than an instruction. The original byte is
    not preserved: its high bits are undecoded by Gadgetbridge itself, and keeping
    it would invite comparing it against `ActivityKind` values, which never matches.

    Reads the whole series into memory first, because resolving a kind requires the
    rows before it. About 900k rows for two years, a few tens of MB — worth it to
    make the result independent of any query window.
    """
    yield from resolve_kinds(list(activity_raw(path)), carry_limit_s=carry_limit_s)


def stress(path: Path) -> Iterator[StressSample]:
    """Stress readings from one backup, oldest first. Millisecond timestamps."""
    query = """
        SELECT TIMESTAMP, DEVICE_ID, USER_ID, TYPE_NUM, STRESS
        FROM HUAMI_STRESS_SAMPLE
        ORDER BY TIMESTAMP
    """
    name = path.name
    with open_export(path) as conn:
        for row in conn.execute(query):
            yield StressSample(
                timestamp_ms=row["TIMESTAMP"],
                device_id=row["DEVICE_ID"],
                user_id=row["USER_ID"],
                type_num=row["TYPE_NUM"],
                stress=row["STRESS"],
                snapshot=name,
            )


def battery(path: Path) -> Iterator[BatteryLevel]:
    """Battery readings from one backup, oldest first. Second timestamps."""
    query = """
        SELECT TIMESTAMP, DEVICE_ID, LEVEL, BATTERY_INDEX
        FROM BATTERY_LEVEL
        ORDER BY TIMESTAMP
    """
    name = path.name
    with open_export(path) as conn:
        for row in conn.execute(query):
            yield BatteryLevel(
                timestamp_s=row["TIMESTAMP"],
                device_id=row["DEVICE_ID"],
                level=row["LEVEL"],
                battery_index=row["BATTERY_INDEX"],
                snapshot=name,
            )


def devices(path: Path) -> dict[int, Device]:
    """Paired devices, keyed by the local `_id` the sample tables refer to.

    Keyed by that id rather than returning a list because resolving
    `sample.device_id` is the only reason to read this table. The key is meaningful
    only within this one snapshot — it renumbers on restore.
    """
    query = """
        SELECT _id, NAME, MANUFACTURER, IDENTIFIER, TYPE_NAME, MODEL, ALIAS
        FROM DEVICE
    """
    name = path.name
    with open_export(path) as conn:
        return {
            row["_id"]: Device(
                identifier=row["IDENTIFIER"],
                name=row["NAME"],
                manufacturer=row["MANUFACTURER"],
                type_name=row["TYPE_NAME"],
                model=row["MODEL"],
                alias=row["ALIAS"],
                snapshot=name,
            )
            for row in conn.execute(query)
        }


def user_attributes(path: Path) -> Iterator[UserAttributes]:
    """Body metrics, oldest validity window first.

    Gadgetbridge appends rather than updates, so expect superseded rows with
    `valid_to` set. The current one is the row with `valid_to` None.
    """
    query = """
        SELECT HEIGHT_CM, WEIGHT_KG, SLEEP_GOAL_HPD, STEPS_GOAL_SPD,
               VALID_FROM_UTC, VALID_TO_UTC
        FROM USER_ATTRIBUTES
        ORDER BY VALID_FROM_UTC
    """
    name = path.name
    with open_export(path) as conn:
        for row in conn.execute(query):
            yield UserAttributes(
                height_cm=row["HEIGHT_CM"],
                weight_kg=row["WEIGHT_KG"],
                sleep_goal_hours=row["SLEEP_GOAL_HPD"],
                steps_goal=row["STEPS_GOAL_SPD"],
                valid_from_ms=row["VALID_FROM_UTC"],
                valid_to_ms=row["VALID_TO_UTC"],
                snapshot=name,
            )


def sleep_sessions(
    samples: Iterable[ActivitySample],
    *,
    gap_s: int = DEFAULT_SLEEP_GAP_S,
) -> Iterator[SleepSession]:
    """Group consecutive sleeping minutes into sessions, oldest first.

    Takes resolved samples — run `resolve_kinds` (or read through `activity`) first,
    or almost every minute will still be a carry instruction and nothing will look
    like sleep.

    A gap of up to `gap_s` does not end a session, because a brief awakening is part
    of a night rather than the end of one. This is device semantics and not a
    presentation choice: the band scores individual minutes and never says where a
    night begins, so any answer about nights depends on a rule like this, and a wrong
    rule fails silently — split one night in two and the median drops by hours while
    every value stays a plausible number of hours.

    What is *not* decided here: which sessions count as nights rather than naps, and
    which local date a night belongs to. Both need a timezone and a purpose, so they
    belong to the caller.
    """
    current: _OpenSession | None = None

    for sample in samples:
        if current is not None and sample.timestamp_s - current.end_s > gap_s:
            yield current.close()
            current = None

        if not sample.asleep:
            continue

        if current is None:
            current = _OpenSession(sample.timestamp_s, sample.timestamp_s)
        current.end_s = sample.timestamp_s
        if sample.kind is ActivityKind.DEEP_SLEEP:
            current.deep += 1
        else:
            current.light += 1
        rate = sample.heart_rate
        if rate is not None:
            current.heart_rates.append(rate)

    if current is not None:
        yield current.close()


@dataclass
class _OpenSession:
    """A session under construction. Mutable, unlike what it produces."""

    start_s: int
    end_s: int
    light: int = 0
    deep: int = 0
    heart_rates: list[int] = field(default_factory=list)

    def close(self) -> SleepSession:
        return SleepSession(
            start_s=self.start_s,
            end_s=self.end_s,
            light_minutes=self.light,
            deep_minutes=self.deep,
            heart_rates=tuple(self.heart_rates),
        )


def discharge_runs(
    levels: Iterable[BatteryLevel],
    *,
    charge_rise: int = DEFAULT_CHARGE_RISE,
) -> Iterator[DischargeRun]:
    """Split a battery series into runs between charges, oldest first.

    A rise of more than `charge_rise` points between consecutive readings is taken as
    a charge. The threshold exists because the band reports whole percents and
    wobbles by one or two, so a stricter rule counts rounding noise as charge cycles
    and chops every run into fragments — which then produce nonsense rates rather
    than an obvious error.

    Runs that gained no charge or spanned no time are dropped: they cannot yield a
    rate, and emitting them would push the decision onto every caller.

    The final run is included even though no charge closed it — the band is simply
    still discharging. Requiring a closing charge would silently drop the most recent
    run, which is the one a reader is most likely to care about.
    """
    start: BatteryLevel | None = None
    previous: BatteryLevel | None = None

    def build(last: BatteryLevel) -> DischargeRun | None:
        assert start is not None
        run = DischargeRun(
            start_s=start.timestamp_s,
            end_s=last.timestamp_s,
            from_level=start.level,
            to_level=last.level,
        )
        if run.drop > 0 and run.duration.total_seconds() > 0:
            return run
        return None

    for level in levels:
        if previous is not None and level.level > previous.level + charge_rise:
            run = build(previous)
            if run is not None:
                yield run
            start = level
        elif start is None:
            start = level
        previous = level

    if start is not None and previous is not None:
        run = build(previous)
        if run is not None:
            yield run


def stats(path: Path) -> dict[str, int]:
    """Row counts per non-empty table. For probing a backup.

    Skips empty tables, unlike the PipePipe equivalent: Gadgetbridge ships 131 of
    them and all but a handful are for hardware you do not own, so listing them all
    buries the interesting ones. An empty table here is also a finding in itself —
    it is how you learn this band records no workouts.
    """
    with open_export(path) as conn:
        tables = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'android_%' "
                "ORDER BY name"
            )
        ]
        counts = {
            t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables
        }
        return {t: n for t, n in counts.items() if n}
