# firefox

Reads archived copies of Firefox's `places.sqlite`. No SDK and nothing to depend
on; the schema is Mozilla's and undocumented outside the source tree.

Verified against two real snapshots from different machines, `user_version = 86`.
Where a number appears below it comes from those two, called *lexikos* (13783
pages) and *laborari* (32580).

## Three timestamp units in one file

The easiest thing here to get wrong, and it fails silently in both directions.

- **Microseconds**: `moz_historyvisits.visit_date`, `moz_places.last_visit_date`,
  `moz_bookmarks.dateAdded` / `lastModified`, `moz_annos.dateAdded`.
- **Milliseconds**: `moz_places_metadata.created_at` / `updated_at`, and the
  `endTime` inside a `downloads/metaData` JSON blob — which sits in a row whose own
  `dateAdded` is microseconds.
- **Seconds**: `moz_newtab_shortcuts_interaction.timestamp_s` (not read here).

Reading a visit as milliseconds puts it in the year 58000; reading engagement as
microseconds puts it in 1970. Every model converts its own column.

## `visit_count` is not the number of visits

`moz_places.visit_count` excludes `visit_type` 7 (`DOWNLOAD`) and 9 (`RELOAD`).
Established by exhaustive comparison rather than from documentation: excluding
exactly those two makes the stored count agree with the row count for every page in
both snapshots, while excluding only 9 leaves 47 pages disagreeing and only 7 leaves
239. A page reloaded twenty times and never navigated to therefore reports 0.

`Visit.counted` exposes the rule, and `test_real_snapshot_visit_count_rule_still_holds`
fails loudly if a future Firefox changes it.

## Ids are not identity, and neither is guid

`moz_places.id` is a per-profile autoincrement. Across the two snapshots 11093 ids
appear in both, and **exactly one** refers to the same URL. `guid` is stable within
a profile but not across machines: the two snapshots share zero page guids, and only
the six bookmark tree roots (`root________`, `menu________`, ...).

So identity is `(host, url)`, where the host comes from the filename, and every
record carries a `Snapshot` naming its machine. Ids are never carried on a dataclass.

The one place an id is followed is `from_visit`, resolved to a referrer URL inside
the snapshot it came from. 382 of 23627 rows point at visits Firefox has already
expired, so `referrer_url` is None for three indistinguishable reasons: no referrer
(7005 rows store 0), a dangling id, or a page the snapshot no longer holds. Never
read it as "typed directly".

## What accumulates, and why

Rows are immutable once written, unlike PipePipe's history — a single snapshot holds
a real timeline. What makes old snapshots necessary is Firefox deleting rows itself.

`moz_places_metadata` is expired on age by `PlacesExpiration`'s
`QUERY_EXPIRE_INTERACTIONS`, which deletes by `updated_at`. Observed windows differ
per machine: lexikos holds 134 days of engagement against 288 days of history,
laborari 230 against 427. That the two windows differ from each other means a single
retention constant does not explain them, and the onset may partly be when the pref
was enabled — unconfirmed. The consequence is the same either way: **an old snapshot
holds engagement rows the newest one has deleted.**

History expires too, but only when the database is over its size limit, so it is not
on a schedule you can predict.

`moz_annos` download records are worse than expiring: the unique index is
`(place_id, anno_attribute_id)`, so downloading the same URL twice **overwrites** the
first record. An old snapshot is the only place the earlier download survives.

Present state, read from each host's newest snapshot: `moz_bookmarks`, `moz_origins`,
`moz_inputhistory`. Deleting a bookmark removes the row and leaves no tombstone
without Sync (`moz_bookmarks_deleted` is empty), so nothing can reconstruct when it
went away. *Each host's* newest, not the newest overall — otherwise whichever machine
exported last defines every other machine's bookmarks.

## `moz_places_metadata` semantics

One row per page view, not per day: one page has 65 rows on one day. Firefox also
ends an interaction and starts a fresh row after an hour without updates
(`breakupIfNoUpdatesForSeconds`), so two rows for one URL need not be two
navigations.

`total_view_time` accumulates only while the window is focused and the user is not
idle (60s threshold), and upstream's own comment admits losing ~2 seconds around an
idle transition. It is normally below `updated_at - created_at` — 7827 of 7843 rows,
30721 of 30742 — and is **not** how long the tab was open. A few rows exceed that
span regardless, so do not assume the bound holds.

## Sentinels and dead columns

`frecency` below zero means "needs recalculation", not a rank: 2 pages in one
snapshot, 4 in the other. Sorting on the raw column puts them at the bottom as
though they were the least relevant things in the profile. `Page.frecency` returns
None for those; `frecency_raw` keeps the column.

Empty in both snapshots, so nothing may depend on them being populated: `session`
(all 0), `triggeringPlaceId`, `search_query_id`, `alt_frecency`, `document_type`
(all `GENERIC`, so `MEDIA` is unexercised rather than verified), `site_name` (NULL
for every page). `moz_keywords`, `moz_items_annos`, `moz_bookmarks_deleted`,
`moz_places_metadata_search_queries` and the newtab story tables are empty tables.

## Undecodable text

One page description ends in `\xed\xa0\xbe`, a lone UTF-16 high surrogate: Firefox
truncated the description to 256 characters and cut an emoji's surrogate pair in
half, leaving WTF-8 in a TEXT column. sqlite3's default text factory raises
`OperationalError` from the *cursor*, so that one byte sequence aborts the read of
all 13783 pages.

`core.sqlite` therefore sets a lossy `text_factory` for every archive, not just
this source. `errors="replace"` rather than `surrogateescape`: a surrogate survives
in a `str` but raises again on `print`, `json.dumps` or any encode, moving the
failure far from its cause. The other half of that emoji is already gone, so nothing
is recoverable in any case.

## Filenames

`places-<hostname>-<ISO8601>Z.sqlite.xz`, timestamp **last**. The hostname contains
dashes of its own (`nightcord-lexikos` is one host), so the regex anchors on the
timestamp and takes the host greedily; splitting on dashes from the left would treat
`nightcord` as the machine and merge two machines into one.

This is the source that needs `get_files`' `key` parameter. Filename order groups by
host and interleaves time, so with two machines the default would present a year of
one host's history before the other's first visit, and "newest snapshot" would mean
whichever host sorts last alphabetically. `snapshot_of` raises on a name it cannot
parse rather than guessing; mtime is no fallback, since it does not survive syncing.

## Cautions

Archives only. `sqlite_ro` opens with `immutable=1`, which is a promise that nothing
is writing the file — pointing any of this at a live profile is undefined, since
Firefox keeps a `-wal` that `immutable=1` tells sqlite to ignore.

`places.sqlite` is the whole history of a browser, and `Download.destination` is a
local path that usually contains the account name. Treat output accordingly.
