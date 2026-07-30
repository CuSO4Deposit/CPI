# pipepipe

[PipePipe](https://github.com/InfinityLoop1308/PipePipe) is a hard fork of NewPipe.
Its export is a plain Android Room SQLite database, either bare or inside a zip
alongside a settings file. No official SDK; there is nothing to depend on.

Verified against a real export, `user_version = 901`.

## Schema facts

Timestamps are **milliseconds** since the Unix epoch, UTC.

`stream_history(stream_id, access_date, repeat_count)` — the primary key is
`(stream_id, access_date)`, which suggests one row per view, but there is **exactly
one row per video**. Upstream `onViewed()` deletes the row and reinserts it with
`access_date` bumped to now and `repeat_count + 1`; delete-then-insert is how it
does an update, since `access_date` is in the key. So the app only ever knows the
*last* watch. This is a deliberate tradeoff for its history screen, not a bug.

Consequences, all of which the API already reflects:

- Plays between two exports are unrecoverable. `repeat_count` says how many were
  missed, not when. Never present record counts as view counts.
- Successive snapshots are the only record of earlier watches, so `watched()`
  emits one record per distinct `access_date` observed.
- `access_date` is overwritten in place, so every day without an export loses
  history permanently.

`repeat_count = 0` means manually marked as watched (`markAsWatched`), not zero
views. `is_upload_date_approximation = 1` means `upload_date` was derived from a
relative string like "3 days ago" — don't treat it as exact.

`streams` is an entity in its own right, not an appendix to history. In the
reference export 400 of 3634 videos have no history row (134 reachable only via a
local playlist, 272 only via the feed, 3 orphaned), so anything built on `watched()`
alone silently omits them.

`is_paid` is visibly a later `ALTER TABLE` addition — unquoted and last, where Room
quotes every generated column name. Older exports may lack it. When a column starts
drifting, feature-detect with `PRAGMA table_info` rather than branching on
`user_version`.

`uid` is a per-export autoincrement and renumbers after a reinstall. Identity is
`(service_id, url)`, which has a UNIQUE index. `Stream` deliberately omits `uid`.

Service ids are the fork's **own** numbering, not upstream NewPipe's. Confirmed:
`0` YouTube, `5` bilibili, `6` niconico. `Service._missing_` returns `None` rather
than raising, so a new service in a future release doesn't break reading an export.

## State vs log

Which tables accumulate decides which entry points may read only the newest
snapshot. `feed` gets pruned by the app and `stream_history` / `search_history`
grow, so those accumulate across snapshots. `subscriptions`, `playlists` and
`remote_playlists` describe present state — a removed row leaves no trace in any
later export, so nothing can reconstruct when it went away.

`remote_playlists` stores only a reference (name, uploader, `stream_count`), never
the member videos; a bookmarked playlist cannot be expanded from an export alone.

## Cautions

Check the row-per-video assumption still holds before trusting any aggregate; the
fork is active and `user_version` is already 901. `test_real_export_matches_known_shape`
is there to fail loudly if it changes.
