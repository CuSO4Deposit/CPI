"""Gadgetbridge: activity, sleep, heart rate, stress and battery from a fitness band.

Reads the zip backups Gadgetbridge's own export produces. Nothing is persisted; the
full view is assembled at runtime by walking archived backups in filename order.

    >>> from cuso4d.gadgetbridge import activity
    >>> for sample in activity("~/archives/gadgetbridge/gadgetbridge_*.zip"):
    ...     print(sample.when, sample.kind, sample.heart_rate)

Why snapshots matter here is the opposite of PipePipe's reason. Rows are immutable
and accumulate — one per minute, keyed by `(timestamp, device)`, never rewritten —
so a single backup already holds the entire history the app has, and per-day charts
over years are sound from one file. What a backup cannot recover is data the band
never handed over: it buffers roughly a week and then overwrites, so any stretch
without a sync is gone for good. The reference archive has four such holes, the
longest 67 days. Taking backups regularly is what prevents new ones; taking several
is what lets you notice the old ones.

Accumulation is therefore a union, not a replay. Where PipePipe needs successive
snapshots to reconstruct events the app overwrote, here later snapshots mostly
re-state what earlier ones said, and merging is deduplication by natural key.

Every entry point takes the backups to read, so a caller decides where its data
lives and nothing depends on ambient state.

Aggregation is deliberately absent, as in every CPI module: grouping by day or
histogramming heart rate is a `groupby` at the call site. What does belong here is
anything encoding Gadgetbridge's semantics, where a mistake is silent rather than
loud — the activity-kind carry-forward above all, plus the sentinel values and the
seconds-versus-milliseconds split between tables. See `models` and `parser`.
"""

from __future__ import annotations

from collections.abc import Iterator

from cuso4d.core.files import Paths, get_files
from cuso4d.gadgetbridge import parser
from cuso4d.gadgetbridge.models import (
    ActivityKind,
    ActivitySample,
    BatteryLevel,
    Device,
    DischargeRun,
    SleepSession,
    StressSample,
    StressType,
    UserAttributes,
)
from cuso4d.gadgetbridge.parser import (
    DEFAULT_CARRY_LIMIT_S,
    DEFAULT_CHARGE_RISE,
    DEFAULT_SLEEP_GAP_S,
)

#: Gadgetbridge names its backups `gadgetbridge_20260730160959.zip`, which sorts
#: chronologically and records the real export time. mtime does not survive copying
#: off the phone, so the filename is the only reliable ordering.
GLOB = "gadgetbridge_*.zip"

__all__ = [
    "DEFAULT_CARRY_LIMIT_S",
    "DEFAULT_CHARGE_RISE",
    "DEFAULT_SLEEP_GAP_S",
    "GLOB",
    "ActivityKind",
    "ActivitySample",
    "BatteryLevel",
    "Device",
    "DischargeRun",
    "SleepSession",
    "StressSample",
    "StressType",
    "UserAttributes",
    "activity",
    "battery",
    "devices",
    "discharge_runs",
    "parser",
    "sleep_sessions",
    "stress",
    "user_attributes",
]


def activity(
    exports: Paths,
    *,
    carry_limit_s: int = DEFAULT_CARRY_LIMIT_S,
) -> Iterator[ActivitySample]:
    """The per-minute activity series across every backup, oldest first.

    Merged by `(timestamp, device)`, the table's own primary key. Where snapshots
    overlap — which is most of their extent — the earliest backup to report a minute
    wins. They agree in practice, the band being the only writer and each minute
    being written once; keeping the first is simply deterministic, and it attributes
    a sample to the backup that first preserved it.

    The carry-forward runs once over the merged series rather than per backup.
    Resolving each separately then merging would give a different, worse answer: a
    backup's first rows are usually carry instructions with nothing before them, so
    they would resolve to `UNSET` and then win the merge over a later backup that
    had the context to resolve them properly.
    """
    merged: dict[tuple[int, int], ActivitySample] = {}
    for path in get_files(exports, glob=GLOB):
        for sample in parser.activity_raw(path):
            merged.setdefault(sample.key, sample)

    ordered = sorted(merged.values(), key=lambda s: s.key)
    by_device: dict[int, list[ActivitySample]] = {}
    for sample in ordered:
        by_device.setdefault(sample.device_id, []).append(sample)

    # Resolved per device: a carry must never inherit across two bands. Sorting the
    # results back together keeps the series in time order for a caller that does
    # not care which device a sample came from.
    resolved = [
        sample
        for samples in by_device.values()
        for sample in parser.resolve_kinds(samples, carry_limit_s=carry_limit_s)
    ]
    resolved.sort(key=lambda s: s.key)
    yield from resolved


def sleep_sessions(
    exports: Paths,
    *,
    carry_limit_s: int = DEFAULT_CARRY_LIMIT_S,
    gap_s: int = DEFAULT_SLEEP_GAP_S,
) -> Iterator[SleepSession]:
    """Sleep sessions across every backup, oldest first.

    Sessions, not nights: a nap is one too. Filtering by length and assigning a local
    date are the caller's, since both need a timezone and a purpose.
    """
    yield from parser.sleep_sessions(
        activity(exports, carry_limit_s=carry_limit_s), gap_s=gap_s
    )


def discharge_runs(
    exports: Paths,
    *,
    charge_rise: int = DEFAULT_CHARGE_RISE,
) -> Iterator[DischargeRun]:
    """Battery discharge runs across every backup, oldest first."""
    yield from parser.discharge_runs(battery(exports), charge_rise=charge_rise)


def stress(exports: Paths) -> Iterator[StressSample]:
    """Stress readings across every backup, oldest first.

    Merged by `(timestamp, device)`, as with `activity`.
    """
    merged: dict[tuple[int, int], StressSample] = {}
    for path in get_files(exports, glob=GLOB):
        for sample in parser.stress(path):
            merged.setdefault(sample.key, sample)
    yield from sorted(merged.values(), key=lambda s: s.key)


def battery(exports: Paths) -> Iterator[BatteryLevel]:
    """Battery readings across every backup, oldest first."""
    merged: dict[tuple[int, int, int], BatteryLevel] = {}
    for path in get_files(exports, glob=GLOB):
        for level in parser.battery(path):
            merged.setdefault(level.key, level)
    yield from sorted(merged.values(), key=lambda b: b.key)


def devices(exports: Paths) -> dict[int, Device]:
    """Paired devices from the newest backup, keyed by the id its samples use.

    Newest only, and deliberately not merged: this describes present state, and the
    `_id` keying it is a local autoincrement that renumbers on restore, so ids from
    different backups are not comparable. Resolve `sample.device_id` against the
    same snapshot the sample came from if a restore ever happened mid-archive.
    """
    return parser.devices(get_files(exports, glob=GLOB)[-1])


def user_attributes(exports: Paths) -> Iterator[UserAttributes]:
    """Body metrics from the newest backup, oldest validity window first.

    Newest only: the table is append-only within one database, so the latest backup
    already contains every window, including the superseded ones.
    """
    yield from parser.user_attributes(get_files(exports, glob=GLOB)[-1])
