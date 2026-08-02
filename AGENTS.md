# CPI

A read-only, typed access layer over my personal data exports, in the spirit of
[HPI](https://beepb00p.xyz/hpi.html). Package namespace is `cuso4d.*`.

## Invariants

**Archives are the source of truth.** Exports are read verbatim; the full view is
assembled at runtime. Never normalize into a persistent database — a parsing
mistake must stay a code change, never a corrupted archive, and there is no schema
to migrate for data whose schema we don't own.
See [Against unnecessary databases](https://beepb00p.xyz/unnecessary-db.html).

**CPI only reads.** No writing, archiving, renaming, `mkdir`, or `chmod` anywhere
in this package. Getting exports onto disk is a separate concern.

**No ambient state.** Paths are parameters. No config file, no module to import,
nothing that depends on what exists on a particular machine.

**Don't guess an app's export filename format** in generic code. A wrong guess
misorders snapshots silently, and accumulation depends on that order. `get_files`
sorts by filename and takes an optional `key` for sources that need their own.

**Never trust upstream autoincrement ids** (`uid`, `id`) as identity across
snapshots — they renumber after a reinstall. Use natural keys, and don't carry the
id on the dataclass at all.

**Read the documented member name; never fall back to a lookalike.** A Gadgetbridge
backup contains a second, byte-identical database under `files/`, which is whatever
the last *manual* export left there. Guessing at it would parse perfectly and report
stale data. When the documented path is missing, fail.

**"The newest snapshot" needs a host once a source is multi-machine.** Firefox runs
on several machines and its ids and guids are both profile-local, so identity there
is `(host, url)` and a present-state table is read from *each host's* newest
snapshot. Taking the single newest file would let whichever machine exported most
recently define every other machine's bookmarks.

**A carry-forward must not span a gap.** Where an app stores "same as before"
instead of repeating a state, resolving it is required — but a gap in the record
means the device wasn't syncing, not that the old state held. Gadgetbridge's own
resolution has no such limit because it only ever runs over a chart's window; ours
runs over years and would carry a state across a 67-day hole.

## Structure

    src/cuso4d/core/       file globbing, read-only sqlite
    src/cuso4d/<source>/   one directory per data source

Each source has two layers: `parser.py` reads exactly one snapshot from an
explicit path (pure, so tests point at fixtures), and the package `__init__`
accumulates across snapshots.

What accumulation *means* differs by source, so check before assuming:

- **PipePipe** overwrites — one row per video, timestamp replaced on every play. Old
  snapshots hold events the app has destroyed, and merging replays them.
- **Gadgetbridge** appends — one immutable row per minute, so a single backup holds
  the whole history and merging is deduplication by natural key. Old backups matter
  only for stretches the band never synced before overwriting its ~1 week buffer.
- **Firefox** appends, then expires. Rows are immutable, so one snapshot holds a real
  timeline, but Firefox deletes old ones itself — engagement sooner than history, and
  a re-download overwrites the annotation of the earlier one. Merging is a union by
  natural key, and old snapshots are the only record of what has been expired.

Where a cross-snapshot merge feeds a transform that depends on preceding rows, merge
first and transform once. Transforming per snapshot then merging silently gives the
worse answer, since each snapshot's leading rows lack the context to resolve.

## Working here

`nix develop` (or `direnv allow`) sets up the venv via uv. Then `pytest` and
`mypy src tests` — both must be clean, mypy is strict. ruff and nixfmt run from
the pre-commit hook.

Verify parsing claims against a real export rather than documentation; upstream
wikis for these apps are thin and often wrong.

## Tests

Real exports are personal data and never enter the repo. Tests build fixtures with
`conftest.make_export` (PipePipe), `make_gb_export` (Gadgetbridge) or
`make_places_export` (Firefox), using schema copied verbatim from a real database.
Copy the constraints too, not just the columns: `WITHOUT ROWID` and `ON CONFLICT
REPLACE` are what make a Gadgetbridge sample's identity `(timestamp, device)`, and
Firefox's UNIQUE indexes are what make a visit's `(place, visit_date)` — which
accumulation relies on in both cases.

A test may also opt into a real export via the `real_export` / `real_gb_export` /
`real_places_export` fixtures, pointed at by `CPI_TEST_PIPEPIPE_EXPORT` /
`CPI_TEST_GADGETBRIDGE_EXPORT` / `CPI_TEST_FIREFOX_EXPORT`; they skip when unset. Such tests may only assert invariants that hold for **any**
export — uniqueness, ordering, subset relations between tables, sentinels never
reaching a caller. Never assert a count from one particular database: those numbers
say nothing about the code, break for anyone else, and quietly leak how much of what
someone watched or how much they slept.
