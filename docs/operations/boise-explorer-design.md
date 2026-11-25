# Boise archive explorer

Build a local browser application for reviewing the completed archive. The existing SQLite database and evidence stay unchanged. The server binds only to 127.0.0.1 and makes no LPS requests. The user requested implementation; local design and implementation choices stay within that scope. Cloud publication, account enrichment and new collection are separate work.

## Experience

The entry view is a session browser with a year strip and searchable table. Navigation exposes Sessions, Teams, Players, Games, Query and Coverage. Rows open linked detail views; games link both team registrations, teams link their session/division and supplied leaders, and players link their recorded leaderboard entries. Search and filters are reflected in URL fragments for back/forward navigation. Tables are paginated and their displayed rows can be exported to CSV.

Player names and game appearances are unavailable. Player browsing uses source IDs and clearly labels totals as sums of supplied leaderboard entries, never complete career statistics. Same-name team registrations are shown separately, without inferring club continuity. Displayed session dates come from observed games, not unreliable estimated source dates.

The Query view offers bounded read-only SQL, example queries and a schema reference. Coverage exposes rejected dates, unavailable divisions, report freshness and reconciliation limitations.

## Visual direction

Use a clear match-log layout with a restrained navy/teal palette: paper #f3f7fa, white #ffffff, ink #18303b, slate #526773, teal #16736b and amber #95580f. A condensed system display face provides scorebook character; system sans body and monospace identifiers support dense readable tables. The year strip is the signature interaction, connecting sessions to the archive's actual time span. Use responsive tables/cards, visible focus, text labels and reduced-motion support. No external fonts, images or remote dependencies are needed.

## Implementation ownership and contract

- `explorer_data.py`: `Archive(root: Path)`, `QueryError(ValueError)`, `RecordNotFound(QueryError)`. Methods: `overview()`, `list_items(kind, params)`, `detail(kind, ident)`, `coverage()`, `schema()`, `sql(statement)`.
- `explorer.py`: localhost HTTP server and CLI. GET `/api/overview`, `/api/coverage`, `/api/schema`; GET `/api/{kind}` and `/api/{kind}/{id}` for sessions/divisions/teams/players/games; POST `/api/query` with `{sql}`. Explicit static routes serve `web/index.html`, `web/app.css`, `web/app.js`. Errors are `{error: message}` with appropriate 400/404/500 status.
- `web/`: buildless frontend. No data mutation, network collection or cloud calls.
- Existing installer adds a stable `bin/lps-boise-explorer` launcher, defaulting to the durable archive. The collector launcher and its behavior remain intact.

List parameters use `search`, `year`, `session` (canonical season ID), `division`, `team`, `player`, `from`, `to`, `status` (`scored`/`unscored`), `limit` and `offset` where applicable. List responses are `{items, total, limit, offset}`. Detail responses use `{item, ...}`. Default page size is 50; maximum 200. Unknown or malformed filters fail explicitly.

Rows use these names:

- Session: `id`, `number`, `start_date`, `end_date`, `division_count`, `team_count`, `game_count`.
- Division: `id`, `name`, `session_id`, `session_number`, `team_count`, `game_count`, `start_date`, `end_date`, `details_available`.
- Team: `id`, `name`, `color`, `division_id`, `division_name`, `session_id`, `session_number`, `game_count`, `start_date`, `end_date`.
- Player: `id`, `entry_count`, `team_count`, `session_count`, `recorded_goals`, `recorded_assists`, `recorded_points`.
- Game: `id`, `start_local`, `field`, `result`, `home_score`, `away_score`, `home_id`, `home_name`, `away_id`, `away_name`, `home_division_id`, `away_division_id`, `home_division_name`, `away_division_name`, `home_session_id`, `away_session_id`, `home_session_number`, `away_session_number`, `scored`.

Detail payloads add: session `divisions`; division `standings` and `leaders`; team `standing`, `leaders`, `other_registrations`; player `entries`; game `observations` and `record`. Standing rows retain supplied official metrics and add `team_name`. Leader entries include player/team/division/session IDs and labels plus supplied goals/assists/points. Game observations expose source kind/key, snapshot ID, fetch time and normalized observation only.

Overview contains `counts` (games, teams, players, sessions, divisions), `years`, `archive` (start/end discovery dates, latest fetch, timezone), and `coverage` (requested/validated/invalid dates, failed divisions, evidence integrity, scoring discrepancy count). Coverage may add precise gaps and yearly counts; do not send the entire per-team audit in the initial response.

SQL returns `{columns, rows, truncated, row_limit, elapsed_ms}` with arrays of row values. Bound SQL size, output size, returned rows and execution time. Open SQLite using mode=ro plus query_only; deny writes, attachment, extension loading and schema changes. Local HTTP Host/Origin checks and a same-origin CSP protect the loopback reader. Never expose arbitrary filesystem paths as routes or log SQL/search contents.

## Data rules

Use global games and either participant's registration for session/division filtering, deduplicating game IDs. Crossovers must remain visible. The archive has five cross-session games; a game can therefore appear under both relevant sessions without being duplicated globally. Twelve registrations belong to the two unavailable division details. Retain their games and use the uniquely verified source season-number mapping rather than dropping them through an inner join.

Use latest division snapshots for supplied standings and player leaders to avoid double-counting refresh history. Source season IDs and session numbers remain distinct. The coverage report can supply validated fallback division names and source-gap metadata; its season/division game rollups are not the dashboard's game universe because daily-only facts also exist.

## Verification and delivery

Test filtering, crossovers, missing division details, latest-snapshot leaders, pagination, parameter binding, read-only SQL enforcement and query cancellation. Exercise HTTP routes and actual browser flows on desktop and a narrow viewport. Verify the database and evidence remain unchanged. Commit task-owned source locally, install the verified release, launch the local explorer and open it for the user. No push, public deployment or infrastructure change is part of this delivery.
