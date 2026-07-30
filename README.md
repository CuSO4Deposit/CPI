# CPI

A typed access layer over my personal data exports, in the spirit of
[HPI](https://beepb00p.xyz/hpi.html).

Modules live under `cuso4d.*`:

```python
from cuso4d.pipepipe import watched

for rec in watched("~/archives/pipepipe/*.zip"):
    print(rec.last_watched, rec.repeat_count, rec.stream.title)
```

## Design

Archives are the source of truth. Exports are stored verbatim under timestamped
filenames and never modified; the full view is assembled at runtime by walking
snapshots in filename order. Nothing is normalized into a persistent database —
see [Against unnecessary databases](https://beepb00p.xyz/unnecessary-db.html).
A parsing mistake is then just a code change, never a corrupted archive, and
there are no schema migrations to own for data whose schema I don't control.

Each source has two layers: `parser.py` reads exactly one snapshot from an
explicit path (pure, so tests point at fixtures), and the package `__init__`
accumulates across snapshots. Where the exports live is a parameter, not
configuration — there is no config file and no ambient state to import.

CPI only reads. Getting exports onto disk is a separate concern and deliberately
not part of this package.

## Layout

    src/cuso4d/core/       file globbing, read-only sqlite
    src/cuso4d/pipepipe/   watch history, searches, subscriptions

## Archiving

PipePipe keeps one history row per video and overwrites its `access_date` on every
play, so the app only knows the *last* time each video was watched. Archived
snapshots are the only record of anything earlier — export regularly, or that
history is gone.

Exports keep the name the app gave them (`PipePipeData-20260727_085246.zip`),
which sorts chronologically and records the real export time; mtime does not
survive copying off the phone. Drop them in the archive directory as-is.

## Development

`nix develop` (or `direnv allow`) sets up the venv via uv.

```console
$ pytest
$ mypy src
```
