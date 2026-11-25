# Local Boise archive explorer

The explorer opens the collected archive in a local browser. It reads SQLite,
verified sanitized evidence and the saved coverage report, makes no LPS requests,
and cannot edit the archive.
Its address defaults to `http://127.0.0.1:8767/`.

## Open the installed tool

From a terminal:

```sh
"$HOME/Library/Application Support/LPS Boise Collector/bin/lps-boise-explorer"
```

Open the printed address in a browser. Keep that terminal running; Ctrl-C stops
the server. `--port 8768` selects another port if the default is occupied. The
server always binds to loopback. It does not install a scheduler or open access
to other computers.

The launcher uses the installed Python interpreter and defaults to
`~/Library/Application Support/LPS Boise Collector/data/boise-history`. Pass
`--data-dir /absolute/path` to inspect another compatible collector archive.

## Browse and query

- **Sessions:** choose an observed game year or search for a session number/ID,
  then open its divisions, teams, or games. Dates come from observed games, not
  unreliable estimated season dates. Source season dates and catalog status are
  shown separately, with warnings for suspect dates.
- **Teams:** search a name or registration ID, optionally within a session or
  division. A team detail shows supplied official standings, its schedule and
  leaderboard entries, color, dropped status and division code. Same-name
  registrations are listed separately.
- **Players:** search by source player ID or filter supplied leaderboard entries
  by team, session or division. Names were not retained. Filtered totals include
  only matching entries; a player detail shows all supplied entries for that ID.
  These totals are not complete career statistics or match appearances.
- **Games:** search a game ID or team name, and filter by session, division, team,
  field, date range and scored/unscored state. Home/away links open each registration.
  Game details distinguish the latest timing from consistent earlier observations.
  Source observations and the unchanged latest record remain available.
- **Query:** run one bounded read-only SQLite query, use a supplied example, or
  inspect the schema. Results are limited to 500 rows, one MiB overall and 64 KiB
  per cell, with a two-second execution budget. Writes, schema changes, external
  attachments, extension loading and file functions are rejected.
- **Coverage:** inspect the saved yearly coverage, unresolved request records,
  evidence status, catalog freshness, named-refresh progress and data limitations.

Export buttons download the displayed table page or query result as CSV. Text
cells that could be interpreted as spreadsheet formulas are escaped. Export does
not change the database. Metadata, warning text and timing/source provenance are
included where supplied.

## Interpret the results

Session and division game filters include either participant, so crossovers remain
visible. Games are deduplicated by source game ID. Five games in this archive cross
session numbers and therefore appear under both relevant sessions, while the
global total counts each once.

The two unavailable division details retain known teams and games supplied by
other records. They remain visibly unavailable; absence of a standing or leaderboard
is not a zero total. Published standings remain separate from score-derived audits.
All displayed kickoff times use the archive's Mountain wall-time convention.

Division details include classification, expected games, match length and scoring
rules. Expand **More source metadata** for supplied fees, publication flags and
source dates. Fees remain raw source values without an assumed currency.
**Where this metadata came from** identifies each field's source snapshot and
fetch time. Catalog-only records have no validated detailed response yet; empty
standings or missing dated games must not be interpreted as zero activity.

Earlier slot timing is exposed only when observations agree on one end time for
the same kickoff, participants and field. Conflicting observations leave the
available end unknown. The browse view does not rewrite the database. Damaged or
missing evidence is excluded from evidence-derived metadata and reported in
Coverage. Raw source dates are never corrected by adding a century.

Browse views use a coherent in-memory snapshot. **Reload archive** refreshes that
snapshot without collecting data. SQL queries use a fresh read-only connection.
After a new collection, reload the explorer to align browsing with the updated
database. If the saved coverage report predates the archive, detailed report
metrics are labeled unavailable until the collector report is regenerated.

## Develop and install

```sh
task explorer:run
task explorer:test
task collector:test
```

`explorer:run` accepts `LPS_DATA_DIR` and extra arguments, for example
`task explorer:run -- --port 8768`. The tests use temporary synthetic archives,
not live collection. The complete collector suite also needs the locally pinned
Bruno runtime installed by `task collector:setup`.

After committing verified changes, `task collector:install` installs both the
collector and explorer launchers. It validates their help entry points before
activating a release, preserves existing data and rejects unmanaged launcher
files. No global shell configuration or cloud publication is required.

The explorer enforces read-only database connections, bounded query execution,
loopback Host/Origin checks and a same-origin content policy. It serves only its
explicit application assets and API routes. Request contents are not logged.
