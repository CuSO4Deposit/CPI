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

## Structure

    src/cuso4d/core/       file globbing, read-only sqlite
    src/cuso4d/<source>/   one directory per data source

Each source has two layers: `parser.py` reads exactly one snapshot from an
explicit path (pure, so tests point at fixtures), and the package `__init__`
accumulates across snapshots.

## Working here

`nix develop` (or `direnv allow`) sets up the venv via uv. Then `pytest` and
`mypy src tests` — both must be clean, mypy is strict. ruff and nixfmt run from
the pre-commit hook.

Verify parsing claims against a real export rather than documentation; upstream
wikis for these apps are thin and often wrong. Prefer tests that assert the shape
of real data, so a schema change upstream fails loudly.
