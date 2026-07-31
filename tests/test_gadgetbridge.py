"""Tests for `cuso4d.gadgetbridge`.

The carry-forward gets the most attention here, because it is the one piece whose
failure is invisible: get it wrong and every number still looks like a number, sleep
just quietly drops from a third of the record to a fiftieth.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import make_gb_export

from cuso4d.gadgetbridge import (
    ActivityKind,
    StressType,
    activity,
    battery,
    devices,
    parser,
    stress,
    user_attributes,
)
from cuso4d.gadgetbridge import discharge_runs as gb_discharge_runs
from cuso4d.gadgetbridge import sleep_sessions as gb_sleep_sessions

MINUTE = 60
# Any epoch second works; a round one keeps the arithmetic in tests readable.
T0 = 1_700_000_000 // MINUTE * MINUTE

# Raw bytes as the band actually writes them, taken from a real export: the same
# state appears under many values because only the low nibble is the kind.
RAW_LIGHT_SLEEP = 249  # 0xf9 -> 9
RAW_DEEP_SLEEP = 219  # 0xdb -> 11
RAW_ACTIVITY = 1
RAW_NO_CHANGE = 80  # 0x50 -> 0, the single most common byte in the export
RAW_IGNORE = 90  # 0x5a -> 10
RAW_NONWEAR = 243  # 0xf3 -> 3


def _activity_row(
    offset_minutes: int,
    raw_kind: int,
    *,
    device_id: int = 1,
    intensity: int = 10,
    steps: int = 0,
    heart_rate: int = 70,
) -> tuple[int, int, int, int, int, int]:
    return (
        T0 + offset_minutes * MINUTE,
        device_id,
        intensity,
        steps,
        raw_kind,
        heart_rate,
    )


def test_activity_masks_high_bits(tmp_path: Path) -> None:
    """The stored byte is not the kind; only its low nibble is."""
    export = make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        activity=[
            _activity_row(0, RAW_LIGHT_SLEEP),
            _activity_row(1, RAW_DEEP_SLEEP),
            _activity_row(2, RAW_NONWEAR),
        ],
    )
    kinds = [s.kind for s in parser.activity(export)]
    assert kinds == [
        ActivityKind.LIGHT_SLEEP,
        ActivityKind.DEEP_SLEEP,
        ActivityKind.NONWEAR,
    ]


def test_carry_forward_resolves_no_change_and_ignore(tmp_path: Path) -> None:
    """84% of real rows say "same as before"; they must inherit, not report 0."""
    export = make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        activity=[
            _activity_row(0, RAW_LIGHT_SLEEP),
            _activity_row(1, RAW_NO_CHANGE),
            _activity_row(2, RAW_IGNORE),
            _activity_row(3, RAW_NO_CHANGE),
            _activity_row(4, RAW_ACTIVITY),
            _activity_row(5, RAW_NO_CHANGE),
        ],
    )
    kinds = [s.kind for s in parser.activity(export)]
    assert kinds == [
        ActivityKind.LIGHT_SLEEP,
        ActivityKind.LIGHT_SLEEP,
        ActivityKind.LIGHT_SLEEP,
        ActivityKind.LIGHT_SLEEP,
        ActivityKind.ACTIVITY,
        ActivityKind.ACTIVITY,
    ]
    # The failure this guards against: without the carry, five of six rows report
    # NO_CHANGE/IGNORE and only one minute of sleep survives.
    assert sum(s.asleep for s in parser.activity(export)) == 4


def test_carry_forward_does_not_cross_a_long_gap(tmp_path: Path) -> None:
    """A gap means the band was not syncing, not that the old state persisted.

    Three of the four multi-day gaps in the reference archive begin with a carry
    instruction, so without a limit the state from before a 67-day hole reappears
    after it.
    """
    export = make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        activity=[
            _activity_row(0, RAW_DEEP_SLEEP),
            # Two days later, and the first row back is a carry instruction.
            _activity_row(2 * 24 * 60, RAW_NO_CHANGE),
            _activity_row(2 * 24 * 60 + 1, RAW_ACTIVITY),
        ],
    )
    kinds = [s.kind for s in parser.activity(export)]
    assert kinds == [
        ActivityKind.DEEP_SLEEP,
        ActivityKind.UNSET,
        ActivityKind.ACTIVITY,
    ]


def test_carry_forward_spans_a_whole_night(tmp_path: Path) -> None:
    """The limit must not truncate legitimate runs, which reach ~400 minutes."""
    rows = [_activity_row(0, RAW_LIGHT_SLEEP)]
    rows += [_activity_row(i, RAW_NO_CHANGE) for i in range(1, 400)]
    export = make_gb_export(tmp_path / "gadgetbridge_20250101000000.zip", activity=rows)
    samples = list(parser.activity(export))
    assert all(s.kind is ActivityKind.LIGHT_SLEEP for s in samples)


def test_carry_limit_is_adjustable(tmp_path: Path) -> None:
    export = make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        activity=[
            _activity_row(0, RAW_LIGHT_SLEEP),
            _activity_row(10, RAW_NO_CHANGE),
        ],
    )
    assert [s.kind for s in parser.activity(export, carry_limit_s=60)] == [
        ActivityKind.LIGHT_SLEEP,
        ActivityKind.UNSET,
    ]
    assert [s.kind for s in parser.activity(export, carry_limit_s=3600)] == [
        ActivityKind.LIGHT_SLEEP,
        ActivityKind.LIGHT_SLEEP,
    ]


def test_leading_carry_stays_unset(tmp_path: Path) -> None:
    """Nothing precedes the first row, so there is nothing to inherit."""
    export = make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        activity=[_activity_row(0, RAW_NO_CHANGE), _activity_row(1, RAW_ACTIVITY)],
    )
    assert [s.kind for s in parser.activity(export)] == [
        ActivityKind.UNSET,
        ActivityKind.ACTIVITY,
    ]


def test_unset_is_not_masked_into_fifteen(tmp_path: Path) -> None:
    """-1 & 0xf is 15, a kind that does not exist. It must pass through as UNSET."""
    export = make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        activity=[_activity_row(0, -1)],
    )
    (sample,) = parser.activity(export)
    assert sample.masked_kind == -1
    assert sample.kind is ActivityKind.UNSET


def test_heart_rate_sentinels_become_none(tmp_path: Path) -> None:
    """255 is a failed reading and is plausible enough to poison an average."""
    export = make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        activity=[
            _activity_row(0, RAW_ACTIVITY, heart_rate=255),
            _activity_row(1, RAW_ACTIVITY, heart_rate=-1),
            _activity_row(2, RAW_ACTIVITY, heart_rate=0),
            _activity_row(3, RAW_ACTIVITY, heart_rate=64),
        ],
    )
    assert [s.heart_rate for s in parser.activity(export)] == [None, None, None, 64]


def test_realtime_rows_are_flagged(tmp_path: Path) -> None:
    """Sub-minute rows written during a live measurement carry no intensity.

    Counting rows as minutes is what puts a month's coverage above 100%.
    """
    export = make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        activity=[
            _activity_row(0, RAW_ACTIVITY, intensity=10),
            (T0 + 25, 1, -1, 0, RAW_ACTIVITY, 71),
        ],
    )
    samples = list(parser.activity(export))
    assert [s.is_realtime for s in samples] == [False, True]
    assert [s.intensity for s in samples] == [10, None]
    assert len({s.timestamp_s // MINUTE for s in samples}) == 1


def test_worn_excludes_charging_and_nonwear(tmp_path: Path) -> None:
    export = make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        activity=[
            _activity_row(0, RAW_ACTIVITY),
            _activity_row(1, RAW_NONWEAR),
            _activity_row(2, 134),  # 0x86 -> 6, CHARGING
        ],
    )
    assert [s.worn for s in parser.activity(export)] == [True, False, False]


def test_stress_timestamps_are_milliseconds(tmp_path: Path) -> None:
    """Unlike the activity table. Read as seconds, these land in the year 56783."""
    when = datetime(2025, 6, 1, 12, 30, tzinfo=UTC)
    export = make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        stress=[(int(when.timestamp() * 1000), 1, 1, 42)],
    )
    (sample,) = parser.stress(export)
    assert sample.when == when
    assert sample.type is StressType.AUTOMATIC
    assert sample.stress_pct == 42


def test_stress_out_of_range_is_none(tmp_path: Path) -> None:
    """Documented as 0-100, not enforced; a real export has values past 250."""
    export = make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        stress=[(1_700_000_000_000, 1, 1, 254), (1_700_000_300_000, 1, 0, 50)],
    )
    samples = list(parser.stress(export))
    assert [s.stress_pct for s in samples] == [None, 50]
    assert [s.stress for s in samples] == [254, 50]
    assert samples[1].type is StressType.MANUAL


def test_stress_minute_joins_to_activity(tmp_path: Path) -> None:
    """The two series can only be compared through a common key."""
    export = make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        activity=[_activity_row(0, RAW_ACTIVITY, heart_rate=88)],
        stress=[((T0 + 17) * 1000, 1, 1, 30)],
    )
    (act,) = parser.activity(export)
    (st,) = parser.stress(export)
    assert st.minute_s == act.timestamp_s


def test_battery_timestamps_are_seconds(tmp_path: Path) -> None:
    when = datetime(2025, 6, 1, 12, 30, tzinfo=UTC)
    export = make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        battery=[(int(when.timestamp()), 1, 87, 0)],
    )
    (level,) = parser.battery(export)
    assert level.when == when
    assert level.level == 87


def test_devices_keyed_by_local_id(tmp_path: Path) -> None:
    export = make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        devices=[(1, "Mi Smart Band 5", "Xiaomi", "AA:BB:CC:DD:EE:FF", "MIBAND5")],
    )
    found = parser.devices(export)
    assert set(found) == {1}
    assert found[1].identifier == "AA:BB:CC:DD:EE:FF"
    assert found[1].type_name == "MIBAND5"


def test_user_attributes_ordered_with_current_last(tmp_path: Path) -> None:
    """Append-only: superseded rows keep a valid_to, the current one has none."""
    export = make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        user_attributes=[
            (1, 175, 70, 8000, 1_726_759_906_274, 1_726_760_379_670),
            (2, 174, 50, 8000, 1_726_760_439_670, None),
        ],
    )
    rows = list(parser.user_attributes(export))
    assert [r.height_cm for r in rows] == [175, 174]
    assert rows[0].valid_to is not None
    assert rows[-1].valid_to is None


def test_stats_omits_empty_tables(tmp_path: Path) -> None:
    """131 tables ship; listing the empty ones buries the two that matter."""
    export = make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        activity=[_activity_row(0, RAW_ACTIVITY)],
    )
    found = parser.stats(export)
    assert found == {"MI_BAND_ACTIVITY_SAMPLE": 1}
    assert "HUAMI_SPO2_SAMPLE" not in found


def test_reads_a_bare_database(tmp_path: Path) -> None:
    export = make_gb_export(
        tmp_path / "Gadgetbridge",
        activity=[_activity_row(0, RAW_ACTIVITY)],
        as_zip=False,
    )
    assert len(list(parser.activity(export))) == 1


def test_rejects_a_zip_without_the_documented_member(tmp_path: Path) -> None:
    """Never guess at another member: `files/Gadgetbridge` is a stale manual export
    that would parse perfectly and report older data."""
    export = make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        activity=[_activity_row(0, RAW_ACTIVITY)],
        db_member="files/Gadgetbridge",
    )
    with pytest.raises(ValueError, match="database/Gadgetbridge"):
        list(parser.activity(export))


def test_prefers_the_documented_member_over_the_files_copy(tmp_path: Path) -> None:
    export = make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        activity=[_activity_row(0, RAW_ACTIVITY)],
        extra_members=["files/Gadgetbridge"],
    )
    assert len(list(parser.activity(export))) == 1


# --- accumulation across snapshots -----------------------------------------------


def test_accumulation_unions_overlapping_snapshots(tmp_path: Path) -> None:
    """Rows are immutable, so overlap is duplication, not history to replay."""
    make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        activity=[_activity_row(0, RAW_ACTIVITY), _activity_row(1, RAW_ACTIVITY)],
    )
    make_gb_export(
        tmp_path / "gadgetbridge_20250201000000.zip",
        activity=[_activity_row(1, RAW_ACTIVITY), _activity_row(2, RAW_ACTIVITY)],
    )
    samples = list(activity(tmp_path))
    assert [s.timestamp_s for s in samples] == [T0, T0 + MINUTE, T0 + 2 * MINUTE]
    # Attributed to the backup that first preserved each minute.
    assert samples[1].snapshot == "gadgetbridge_20250101000000.zip"


def test_accumulation_recovers_minutes_a_later_backup_lost(tmp_path: Path) -> None:
    """The reason to keep old backups: the band drops what was never synced."""
    make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        activity=[_activity_row(0, RAW_ACTIVITY)],
    )
    make_gb_export(
        tmp_path / "gadgetbridge_20250201000000.zip",
        activity=[_activity_row(1, RAW_ACTIVITY)],
    )
    assert len(list(activity(tmp_path))) == 2


def test_accumulation_resolves_kinds_after_merging(tmp_path: Path) -> None:
    """Resolving per snapshot then merging loses the context to resolve at all.

    The second backup starts with a carry instruction. Resolved on its own that is
    UNSET, and merging first-wins would keep that. Merged first, the sleep state
    from the earlier backup is there to inherit.
    """
    make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        activity=[_activity_row(0, RAW_LIGHT_SLEEP)],
    )
    make_gb_export(
        tmp_path / "gadgetbridge_20250201000000.zip",
        activity=[_activity_row(1, RAW_NO_CHANGE)],
    )
    assert [s.kind for s in activity(tmp_path)] == [
        ActivityKind.LIGHT_SLEEP,
        ActivityKind.LIGHT_SLEEP,
    ]


def test_carry_does_not_cross_devices(tmp_path: Path) -> None:
    """One band's state must never become another's."""
    make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        activity=[
            _activity_row(0, RAW_LIGHT_SLEEP, device_id=1),
            _activity_row(1, RAW_NO_CHANGE, device_id=2),
            _activity_row(2, RAW_NO_CHANGE, device_id=1),
        ],
    )
    by_device = {(s.device_id, s.timestamp_s): s.kind for s in activity(tmp_path)}
    assert by_device[(2, T0 + MINUTE)] is ActivityKind.UNSET
    assert by_device[(1, T0 + 2 * MINUTE)] is ActivityKind.LIGHT_SLEEP


def test_accumulation_ignores_unrelated_files(tmp_path: Path) -> None:
    make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        activity=[_activity_row(0, RAW_ACTIVITY)],
    )
    (tmp_path / "notes.txt").write_text("not a backup", encoding="utf-8")
    (tmp_path / "PipePipeData-20250101_000000.zip").write_bytes(b"not a backup")
    assert len(list(activity(tmp_path))) == 1


def test_accumulation_is_ordered_by_filename_not_mtime(tmp_path: Path) -> None:
    """mtime does not survive copying off a phone; the filename is the record."""
    newer = make_gb_export(
        tmp_path / "gadgetbridge_20250201000000.zip",
        activity=[_activity_row(1, RAW_ACTIVITY)],
    )
    make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        activity=[_activity_row(0, RAW_ACTIVITY)],
    )
    # Touch the newer backup so mtime order contradicts filename order.
    newer.touch()
    samples = list(activity(tmp_path))
    assert [s.snapshot for s in samples] == [
        "gadgetbridge_20250101000000.zip",
        "gadgetbridge_20250201000000.zip",
    ]


def test_stress_and_battery_accumulate(tmp_path: Path) -> None:
    make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        stress=[(1_700_000_000_000, 1, 1, 20)],
        battery=[(T0, 1, 90, 0)],
    )
    make_gb_export(
        tmp_path / "gadgetbridge_20250201000000.zip",
        stress=[(1_700_000_000_000, 1, 1, 20), (1_700_000_300_000, 1, 1, 25)],
        battery=[(T0, 1, 90, 0), (T0 + MINUTE, 1, 89, 0)],
    )
    assert [s.stress for s in stress(tmp_path)] == [20, 25]
    assert [b.level for b in battery(tmp_path)] == [90, 89]


def test_present_state_comes_from_the_newest_backup(tmp_path: Path) -> None:
    make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        devices=[(1, "Mi Smart Band 5", "Xiaomi", "AA:BB:CC:DD:EE:FF", "MIBAND5")],
        user_attributes=[(1, 175, 70, 8000, 1, None)],
    )
    make_gb_export(
        tmp_path / "gadgetbridge_20250201000000.zip",
        devices=[(1, "Mi Smart Band 5", "Xiaomi", "AA:BB:CC:DD:EE:FF", "MIBAND5")],
        user_attributes=[(1, 175, 70, 8000, 1, 2), (2, 174, 68, 9000, 3, None)],
    )
    assert [r.weight_kg for r in user_attributes(tmp_path)] == [70, 68]
    assert devices(tmp_path)[1].snapshot == "gadgetbridge_20250201000000.zip"


def test_missing_exports_raise(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        list(activity(tmp_path))


# --- sleep sessions ---------------------------------------------------------------


def _sleep_export(tmp_path: Path, rows: list[tuple[int, int]]) -> Path:
    """rows: (offset_minutes, raw_kind)."""
    return make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        activity=[_activity_row(offset, kind) for offset, kind in rows],
    )


def test_sleep_session_spans_a_brief_awakening(tmp_path: Path) -> None:
    """A few minutes awake is part of a night, not the end of one."""
    export = _sleep_export(
        tmp_path,
        [(i, RAW_LIGHT_SLEEP) for i in range(60)]
        # Ten minutes awake, then back to sleep.
        + [(i, RAW_ACTIVITY) for i in range(60, 70)]
        + [(i, RAW_LIGHT_SLEEP) for i in range(70, 130)],
    )
    (session,) = parser.sleep_sessions(parser.activity(export))
    assert session.asleep_minutes == 120
    # The awake minutes fall inside the span but not the total asleep.
    assert session.span == timedelta(minutes=130)


def test_a_long_awakening_splits_the_session(tmp_path: Path) -> None:
    export = _sleep_export(
        tmp_path,
        [(i, RAW_LIGHT_SLEEP) for i in range(60)]
        + [(i, RAW_ACTIVITY) for i in range(60, 200)]
        + [(i, RAW_LIGHT_SLEEP) for i in range(200, 260)],
    )
    first, second = parser.sleep_sessions(parser.activity(export))
    assert first.asleep_minutes == 60
    assert second.asleep_minutes == 60


def test_sleep_gap_is_adjustable(tmp_path: Path) -> None:
    export = _sleep_export(
        tmp_path,
        [(0, RAW_LIGHT_SLEEP), (30, RAW_LIGHT_SLEEP)],
    )
    assert len(list(parser.sleep_sessions(parser.activity(export), gap_s=600))) == 2
    assert len(list(parser.sleep_sessions(parser.activity(export), gap_s=3600))) == 1


def test_sleep_session_counts_stages_and_heart_rates(tmp_path: Path) -> None:
    export = make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        activity=[
            _activity_row(0, RAW_LIGHT_SLEEP, heart_rate=60),
            _activity_row(1, RAW_DEEP_SLEEP, heart_rate=55),
            _activity_row(2, RAW_DEEP_SLEEP, heart_rate=255),
        ],
    )
    (session,) = parser.sleep_sessions(parser.activity(export))
    assert (session.light_minutes, session.deep_minutes) == (1, 2)
    assert session.deep_share == pytest.approx(2 / 3)
    # The failed reading is absent rather than present as 255.
    assert session.heart_rates == (60, 55)


def test_sleep_sessions_see_carried_minutes(tmp_path: Path) -> None:
    """The carry-forward is what makes most sleeping minutes visible at all."""
    export = _sleep_export(
        tmp_path,
        [(0, RAW_LIGHT_SLEEP)] + [(i, RAW_NO_CHANGE) for i in range(1, 300)],
    )
    (session,) = parser.sleep_sessions(parser.activity(export))
    assert session.asleep_minutes == 300


def test_no_sleep_yields_no_sessions(tmp_path: Path) -> None:
    export = _sleep_export(tmp_path, [(0, RAW_ACTIVITY), (1, RAW_ACTIVITY)])
    assert list(parser.sleep_sessions(parser.activity(export))) == []


def test_sleep_sessions_accumulate_across_snapshots(tmp_path: Path) -> None:
    make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        activity=[_activity_row(i, RAW_LIGHT_SLEEP) for i in range(60)],
    )
    make_gb_export(
        tmp_path / "gadgetbridge_20250201000000.zip",
        activity=[_activity_row(i, RAW_NO_CHANGE) for i in range(60, 120)],
    )
    # One session, not two: merging happens before the sessions are cut, so the
    # second backup's carried minutes join the first backup's night.
    (session,) = gb_sleep_sessions(tmp_path)
    assert session.asleep_minutes == 120


# --- discharge runs ---------------------------------------------------------------


def test_discharge_runs_split_at_charges(tmp_path: Path) -> None:
    day = 24 * 60 * MINUTE
    make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        battery=[
            (T0, 1, 100, 0),
            (T0 + day, 1, 90, 0),
            (T0 + 2 * day, 1, 80, 0),
            # Charged back up.
            (T0 + 2 * day + 3600, 1, 100, 0),
            (T0 + 3 * day, 1, 95, 0),
        ],
    )
    first, second = gb_discharge_runs(tmp_path)
    assert (first.from_level, first.to_level, first.drop) == (100, 80, 20)
    assert first.duration == timedelta(days=2)
    assert first.percent_per_day == pytest.approx(10)
    assert (second.from_level, second.to_level) == (100, 95)


def test_small_wobbles_are_not_charges(tmp_path: Path) -> None:
    """Whole-percent reporting wobbles; a stricter rule would fragment every run."""
    day = 24 * 60 * MINUTE
    make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        battery=[
            (T0, 1, 100, 0),
            (T0 + day, 1, 96, 0),
            # Reads two points higher, then resumes falling.
            (T0 + day + 60, 1, 98, 0),
            (T0 + 2 * day, 1, 90, 0),
        ],
    )
    (run,) = gb_discharge_runs(tmp_path)
    assert run.drop == 10


def test_charge_rise_is_adjustable(tmp_path: Path) -> None:
    """The same series is one run or two depending on the threshold."""
    day = 24 * 60 * MINUTE
    make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        battery=[
            (T0, 1, 100, 0),
            (T0 + day, 1, 96, 0),
            (T0 + day + 60, 1, 98, 0),
            (T0 + 2 * day, 1, 90, 0),
        ],
    )
    # A threshold of 1 treats the two-point wobble as a charge and cuts there.
    assert [r.drop for r in gb_discharge_runs(tmp_path, charge_rise=1)] == [4, 8]
    assert [r.drop for r in gb_discharge_runs(tmp_path, charge_rise=3)] == [10]


def test_percent_per_day_does_not_extrapolate(tmp_path: Path) -> None:
    """A tiny drop must not become a huge projected runtime.

    Dividing duration by drop to get "days a full charge lasts" put runs of one or
    two points past 400 days, which then dominated any median.
    """
    day = 24 * 60 * MINUTE
    make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        battery=[
            (T0, 1, 100, 0),
            (T0 + day, 1, 99, 0),
            (T0 + day + 60, 1, 100, 0),
        ],
    )
    (run,) = gb_discharge_runs(tmp_path, charge_rise=0)
    assert run.drop == 1
    assert run.percent_per_day == pytest.approx(1)


def test_final_run_is_emitted_without_a_closing_charge(tmp_path: Path) -> None:
    """The band is still discharging; that run is the one a reader cares most about.

    An earlier version only emitted a run when a charge closed it, silently dropping
    the most recent one.
    """
    day = 24 * 60 * MINUTE
    make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        battery=[(T0, 1, 100, 0), (T0 + day, 1, 95, 0), (T0 + 2 * day, 1, 90, 0)],
    )
    (run,) = gb_discharge_runs(tmp_path)
    assert (run.from_level, run.to_level) == (100, 90)
    assert run.percent_per_day == pytest.approx(5)


def test_runs_without_drain_are_dropped(tmp_path: Path) -> None:
    make_gb_export(
        tmp_path / "gadgetbridge_20250101000000.zip",
        battery=[(T0, 1, 90, 0), (T0 + 60, 1, 100, 0), (T0 + 120, 1, 100, 0)],
    )
    # The first run gained charge rather than losing it; the second never drained.
    assert list(gb_discharge_runs(tmp_path)) == []


# --- against a real backup, if one is available ----------------------------------


def test_real_backup_invariants(real_gb_export: Path) -> None:
    """Only assertions that hold for any backup: no counts from one database."""
    samples = list(parser.activity(real_gb_export))
    if not samples:
        pytest.skip("backup has no activity samples")

    assert [s.timestamp_s for s in samples] == sorted(s.timestamp_s for s in samples)
    assert len({s.key for s in samples}) == len(samples)

    # The carry-forward's whole point: instructions must not survive into output.
    assert not any(
        s.masked_kind in (ActivityKind.NO_CHANGE, ActivityKind.IGNORE) for s in samples
    )
    # Rows are not minutes; they can only be equal or more.
    assert len({s.timestamp_s // MINUTE for s in samples}) <= len(samples)
    # Sentinels never reach a caller as numbers.
    assert all(s.heart_rate is None or 10 <= s.heart_rate <= 250 for s in samples)
    assert all(s.intensity is None or s.intensity >= 0 for s in samples)

    for st in parser.stress(real_gb_export):
        assert st.stress_pct is None or 0 <= st.stress_pct <= 100
        # Milliseconds, so the epoch seconds must be a sane recent date.
        assert st.when.year >= 2014

    assert set(parser.devices(real_gb_export)) >= {s.device_id for s in samples[:1]}
