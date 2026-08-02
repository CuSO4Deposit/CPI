# CuSO4D Programming Interface

A typed access layer over CuSO4D data, in the spirit of
[HPI](https://beepb00p.xyz/hpi.html).

Modules live under `cuso4d.*`:

```python
from cuso4d.pipepipe import watched

for rec in watched("~/archives/pipepipe/*.zip"):
    print(rec.last_watched, rec.repeat_count, rec.stream.title)
```

```python
from cuso4d.gadgetbridge import activity

for sample in activity("~/archives/gadgetbridge/gadgetbridge_*.zip"):
    print(sample.when, sample.kind, sample.heart_rate)
```

```python
from cuso4d.firefox import visits

for visit in visits("~/archives/*/firefox/places-*.sqlite.xz"):
    print(visit.when, visit.snapshot.host, visit.visit_type, visit.url)
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

    src/cuso4d/core/          file globbing, read-only sqlite
    src/cuso4d/pipepipe/      watch history, searches, subscriptions
    src/cuso4d/gadgetbridge/  activity, sleep, heart rate, stress, battery
    src/cuso4d/firefox/       browsing history, bookmarks, page engagement

## Expected export files structure

Exports keep the name the app gave them (`PipePipeData-20260727_085246.zip`,
`gadgetbridge_20260730160959.zip`), which sorts chronologically and records the
real export time; mtime does not survive copying off the phone. Drop them in the
archive directory as-is.

Firefox has no export of its own, so its snapshots are named on the way in:
`places-<hostname>-<ISO8601>Z.sqlite.xz`, e.g.
`places-nightcord-lexikos-20260802T124748Z.sqlite.xz`. The hostname is needed
because Firefox runs on several machines and its page ids are per-profile, so the
same id means different things on each. The timestamp goes last, and
`cuso4d.firefox` sorts on it rather than on the filename.

How much old snapshots matter depends on the app. PipePipe overwrites a video's
timestamp on every play, so an old export is the only record of the plays before
it. Gadgetbridge appends immutable per-minute rows, so one backup already holds
everything the app knows — but the band itself only buffers about a week, so any
stretch you never synced is gone regardless of how many backups you take later.
Firefox appends and then expires: it deletes per-page engagement rows well before
the matching history, so an old snapshot is the only record of how long you spent
on something months ago.

## Development

`nix develop` (or `direnv allow`) sets up the venv via uv.

```console
$ pytest
$ mypy src
```
