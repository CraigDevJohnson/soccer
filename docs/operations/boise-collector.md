# Local Boise history collector

The collector builds an evidence-backed local inventory from the public LPS API. Bruno CLI **4.4.0** executes each HTTP request. Python **3.11 or newer** owns the durable queue, pacing, validation, SQLite and sanitized evidence. Python uses only the standard library. The collector does not serve a screen, change the calendar backend, contact LPS staff, configure AWS, or start a scheduled job.

For development, install Node.js 24+ and run `task collector:setup` once. This installs the locked Bruno dependency locally without lifecycle scripts or live collection. Run the commands below from this repository's root. For a durable Mac installation outside a disposable worktree, follow [local setup](boise-collector-local-setup.md).

## Request execution

The collector invokes its own small, checked-in Bruno collection, isolated from the personal collection under `Documents/bruno`. It does not load that collection's environments, native secrets, cookies or proxy configuration. There is no Python HTTP fallback. Missing or malformed Bruno output and subprocess failure remain failed attempts, never successful empty dates.

The bridge uses Developer Mode for access to Bruno's exact response buffer. Only the trusted collector request script runs in that mode. Python captures the result in bounded memory; no raw response reports are written. The request disables redirects and response decompression, asks for identity encoding, and limits the response to 8 MiB. A malformed or unexpectedly encoded response fails validation. HTTP status and `Retry-After` survive response-size failures. Python persists the server deadline before completing the failed attempt.

The runtime is version-locked because exact buffers and transfer limits use Bruno's request/response internals. Upgrade Bruno only with the offline transport and CLI acceptance tests. The [bridge contract](../research/bruno-collector-bridge.md) records this dependency and its validation requirements.

## Start with an explicit interval

```sh
python3 -m scripts.boise_collect scan \
  --data-dir dist/boise-history \
  --start 2010-09-20 --end 2010-09-26 \
  --max-requests 30
```

Dates are inclusive and use `YYYY-MM-DD`. Every successful day response can discover division-season IDs, which are fetched before continuing to the next day. Verified Boise crossover references extend that queue. The same division is requested only once during a backfill, even when many days or teams refer to it. There is no command for guessing or enumerating global division IDs.

Each invocation makes at most **100 HTTP requests by default**. `--max-requests` sets a different positive budget. Requests are serial and start at least one second apart; `--delay` increases that spacing. The API's supported rate limits remain undocumented, so this default is a local pacing choice, not a provider-approved quota.

The writer lock and pacing state belong to one data directory. Separate directories have independent limits; use one live writer at a time when collecting from this API.

```sh
python3 -m scripts.boise_collect resume --data-dir dist/boise-history --max-requests 100
python3 -m scripts.boise_collect report --data-dir dist/boise-history
```

Committed successful work is skipped across restarts. An interrupted, uncommitted request is marked `interrupted` in the attempt ledger and may be fetched again. Repeating `scan` for the same dates does not reset their checkpoints; adding an overlapping interval queues only previously unseen dates. `resume` processes all outstanding work in the data directory.

Ctrl-C and SIGTERM stop the Bruno runner and return interruption code 130. Resume recovers the unfinished attempt while retaining committed snapshots.

The full example interval from the investigation is **2010-01-01 through 2026-10-12** (6,129 dates). To register and begin it, deliberately pass those dates with a bounded budget:

```sh
python3 -m scripts.boise_collect scan \
  --data-dir dist/boise-history \
  --start 2010-01-01 --end 2026-10-12 \
  --max-requests 100
```

This command starts network collection; it is not a dry run. No full-range scan runs during tests or installation. Daily discovery requires one request per date plus requests for unique divisions. 2010 is a chosen lower bound, not a proven start of upstream retention.

## Discover future divisions and refresh selectively

Pass `--include-upcoming` on `scan` to request the facility-filtered upcoming-leagues catalog. This can discover divisions before they have fixtures. Valid catalog entries populate season, definition and division metadata with per-field snapshot provenance. They do not establish that division details, standings or games were supplied. Each subsequent detail response must still match the requested ID and facility 4.

For a bounded refresh of recent dates, related divisions and the catalog, use a
named job. The installed launcher's default data directory is the durable archive:

```sh
"$HOME/Library/Application Support/LPS Boise Collector/bin/lps-boise-collector" refresh \
  --start 2026-10-29 --end 2026-11-11 --include-upcoming \
  --job-id refresh-2026-11-11 --max-requests 100 --delay 1 --timeout 60

"$HOME/Library/Application Support/LPS Boise Collector/bin/lps-boise-collector" resume \
  --job-id refresh-2026-11-11 --max-requests 100 --delay 1 --timeout 60
```

These commands make live requests through Bruno. The job records its date range,
catalog choice and resource membership in SQLite. It collects the catalog and
days before refreshing their discovered divisions, including previously known
division references. A resource is refreshed once per job; new crossovers and
catalog divisions join the same job. Budget exhaustion or interruption leaves
durable pending work. Reusing the same job ID resumes it without requeueing
completed members; repeating a completed job makes zero requests. A later fresh
observation needs a new job ID. The same ID cannot be reused with a different
date range or catalog choice. No recurring schedule is installed.

Existing invalid, missing, blocked and failed resources are recorded as skipped
job gaps and remain unchanged. New terminal failures leave the job in
`needs_attention`; diagnose them before an explicit retry. Resource and global
server deadlines remain effective across planning, retries and process restarts.
`complete_with_gaps` describes the selected job, not a complete archive.

For a diagnosed, repeat-confirmed source-invalid response, a job gap can be
acknowledged without changing the resource or its history:

```sh
"$HOME/Library/Application Support/LPS Boise Collector/bin/lps-boise-collector" acknowledge-gap \
  --job-id refresh-2026-11-11 --kind day --key 2026-11-09 \
  --body-sha256 '<exact SHA-256 from the diagnosed response>' \
  --reason confirmed_empty_game_slot
```

This makes no request. It requires a pending member of that job, current
`invalid`/HTTP 200/`invalid_source_response` state, and two consecutive matching
failed attempts with the supplied body hash. It cannot acknowledge authentication
blocks, missing resources or server failures. Matching hashes alone do not
diagnose a cause; inspect the exact failure first. The operation appends an
immutable audit record with both attempt IDs and marks only the job member
skipped. It preserves the invalid resource, all snapshots, attempt history and
server deadlines. Repeating the identical acknowledgment is a no-op. Resume the
same job afterward to finish any remaining work and regenerate coverage.

Ordinary `resume` continues the active named refresh when one exists. Starting a
scan or an unrelated legacy refresh while it is unfinished is refused. Prefer
the explicit `--job-id` form in saved run commands.

The older age-based division refresh remains available when no named job is active:

```sh
python3 -m scripts.boise_collect scan \
  --data-dir dist/boise-history --start 2026-10-12 --end 2026-10-12 \
  --include-upcoming --max-requests 100

python3 -m scripts.boise_collect refresh --data-dir dist/boise-history
```

Without a named date range, `refresh` operates on known division resources and an already-enabled catalog. This legacy mode does **not** scan or refresh dates. Default refresh eligibility is:

- Divisions with no dated fixtures or a latest fixture within the last 14 days or in the future: at least 24 hours since the previous successful fetch.
- Older historical candidates: at least 30 days since the previous successful fetch.
- An enabled upcoming catalog: at least 24 hours since the previous successful fetch.

These are recency classes, not claims that a season is complete. `--active-hours` and `--historical-days` change the intervals. They control eligibility when you invoke the command; no scheduler is installed. Newly discovered crossover/catalog divisions are collected within the same bounded refresh run.

To recheck specific **already discovered** divisions without waiting for their refresh age:

```sh
python3 -m scripts.boise_collect refresh \
  --data-dir dist/boise-history --division 13394 --max-requests 10
```

Every successful refresh adds an immutable snapshot, preserving earlier official standings and observations. Current tables point to newer observations; the report identifies disagreements across the latest endpoint snapshots and warns that differing fetch times can explain them. A failed refresh retains the last good snapshot and marks it stale.

## Failures, backoff and repair

The request ledger distinguishes `pending`, `running`, `success`, `failed`, `missing`, `invalid` and `blocked`. The attempt ledger also records interrupted work. Missing or malformed source data never becomes an empty successful date.

- Network errors, HTTP 408/425/429 and server errors are retryable after backoff.
- A valid `Retry-After` delta or HTTP date becomes a persistent deadline for the entire collector. It is stored before finalizing the attempt, so a crash cannot let another queued resource bypass it. The run stops on rate limiting or a future retry deadline; invoke `resume` later. Delays longer than 30 seconds return control rather than holding the CLI open. Server delays are never shortened to fit that wait limit.
- HTTP 401/403 stops the run and records `blocked`. No credentials are sought or attached.
- HTTP 404/410 records `missing`. A malformed shape, wrong facility/identity/day, unsupported redirect, or oversized response records `invalid`. These states require an explicit retry after diagnosis.
- No response bodies from failed requests are saved or printed. Stable error codes, HTTP statuses and hashes are retained. Successful evidence is sanitized before persistence.

```sh
python3 -m scripts.boise_collect retry \
  --data-dir dist/boise-history --kind division --key 13394
python3 -m scripts.boise_collect resume --data-dir dist/boise-history
```

`retry` only requeues an existing resource; it does not make a request itself and cannot introduce an undiscovered division. It preserves both resource and global server backoff deadlines. After an interruption, use `resume` first to recover the in-flight attempt.

After diagnosing a division that repeatedly returns a server error, `scan` and `resume` accept a repeatable `--defer-division ID` option. The division must already have a verified discovery, be in the failed state after at least five attempts, and have a confirmed HTTP 5xx error. Network/Bruno failures, authentication refusals and unexamined divisions cannot be deferred this way.

```sh
python3 -m scripts.boise_collect resume \
  --data-dir dist/boise-history --defer-division 25626 --max-requests 500
```

Deferral applies only to that invocation. Pass the same option on subsequent backfill batches. It preserves the failed resource, request history and retry deadline; the global server/backoff deadline still gates other requests. Rediscovering the division does not re-enable it during that run. The result lists deferred divisions and excludes them from selected unfinished work, while `archive_unfinished` and coverage reports retain the gap. A drained run with deferrals does not establish complete inventory. A later ordinary resume can retry the failed division when its existing deadline allows it.

Exit codes: **0** means the command's selected work drained with intact evidence; **2** means unresolved/budget-limited/deferred work or evidence integrity failure; **1** means a local configuration/storage error; **130** means interrupted. A successful `report` command exits 0 even if it describes incomplete coverage. None of these codes establishes all-time completeness.

## Files and consistency

Each data directory contains:

| Path | Purpose |
|---|---|
| `collector.sqlite3` | Intervals, resource queue, request attempts, source discovery, normalized entities and snapshots |
| `evidence/<sha256>.json` | Content-addressed, sanitized JSON source evidence |
| `collector.lock` | POSIX single-writer lock, released automatically when the process exits |
| `reports/coverage.json` | Complete machine-readable inventory, audits and gaps |
| `reports/coverage.md` | Human-readable tables and discrepancy appendix |

Use ignored `dist/` for disposable development datasets. Keep durable datasets outside the checkout; the local installer uses `~/Library/Application Support/LPS Boise Collector/data/boise-history`. Back up the **whole data directory** with the writer stopped. Copying only the database loses evidence. A database is tied to one source origin and schema version; the CLI refuses to silently reuse it for another origin.

Evidence is fsynced and atomically placed before the SQLite transaction commits the snapshot, normalized facts, discoveries, and success checkpoint. A crash can leave an unreferenced evidence file, but not a committed snapshot whose evidence was never written. Reports check evidence paths, presence, JSON validity and SHA-256 hashes. Do not edit evidence files in place; keep corruption visible and repair the underlying data from a trustworthy backup or a deliberate re-fetch.

Two hashes have distinct meanings: `body_sha256` hashes the original HTTP bytes in memory; `evidence_sha256` hashes the persisted sanitized JSON. They normally differ. Account tokens, profile contacts, roster details and non-game booking descriptions are excluded. Leaderboard records retain player IDs and supplied numerical totals, not names or contact profiles. A player's current home facility is not treated as the location of a historical game.

Normalized tables include seasons, division definitions, division seasons, teams and games. Immutable game observations and official standings/player-leader rows refer to snapshots. `UTeamID`, `UDivSeasonID`, `UDivisionID`, lowercase `season_id` and the source season number remain separate identifiers. Dates marked `Z` by LPS are interpreted as Mountain wall time consistently with the existing integration, while their original strings remain available. Missing dates and bad season metadata are not silently repaired.

Metadata merges preserve known nonnull fields and their source snapshot IDs when
a later response is sparse. Game scores still reflect the latest game observation;
an older score is not restored over a new null. Catalog metadata cannot replace a
division's detailed standings snapshot. Raw source dates before 2000 or reversed
date ranges remain unchanged and flagged. Existing schema-version-1 archives gain
only additive refresh-job and gap-acknowledgment tables when opened by the collector.

Day responses can publish games starting on the next calendar date, including schedules with idle time between games. The archive accepts these records only when both `open_at` and `close_at` dates match the requested date and the same field contains a fully validated game on that requested date. It preserves the actual timestamp and records a next-day publication warning. This is a conservative archive acceptance policy based on observed responses, not a documented provider operating-hours contract. Isolated next-day records, mismatched or absent date anchors, previous-day records, and dates beyond the immediately following day still fail validation. No clock cutoff or missing intervening game is inferred.

## Read the coverage report

The report makes separate statements about declared dates checked, evidence integrity, discovered resources resolved, and available inventory. It includes:

- The union of explicit intervals, counts by year, empty successful days, and unexamined/failed work.
- Inventory by observed game year, source season and division, including data returned outside the discovery interval.
- Per-team scheduled and scored game counts against official games played and expected games. Upcoming schedules and tournaments can legitimately differ from expected counts.
- Numeric-score W/L/T/goals/points calculations alongside the official table. Differences are retained; a 0–0 is not automatically rewritten as a double forfeit and a 0–5 is not automatically treated as an administrative result.
- Unscored versus unclassified results, null versus empty standings, incomplete discovery relationships, stale snapshots, source conflicts and missing/corrupt evidence.

`all_time_complete` remains **false** and the earliest retention boundary remains **unresolved**. Checking every date in an interval cannot prove that deleted, never-published or never-scheduled divisions were recovered. A successful upcoming catalog is not a complete historical catalog. Supplied leaderboards are not complete player histories. These limits remain visible even when the local queue has drained.

For SQL exploration, official standings for the current division snapshot can be selected without reconstructing them from scores:

```sql
SELECT d.id AS division_id, d.season_id, d.season_number,
       s.team_id, s.games_played, s.wins, s.losses, s.ties, s.points
FROM divisions d JOIN standings s ON s.snapshot_id = d.snapshot_id
ORDER BY d.season_number, d.id, s.position;
```

## Verification

```sh
task collector:setup  # dependency installation only; needed once per checkout
task collector:test
task ci
```

Tests use saved sanitized samples and local HTTP fixtures; they make no LPS requests. They cover multiple eras, crossover duplication, tournament durations, unpublished standings, misleading score totals, privacy filtering, transactional rollback, interruption, both Retry-After forms, source-origin guards, CLI restart, refresh isolation and report integrity. Bruno transport tests run the real pinned CLI against loopback servers. CI exercises Python 3.11 and 3.15 with Node.js 24. For fixture servers only, `--base-url http://127.0.0.1:PORT` and a shorter `--delay` are allowed. Production credentials, proxies, cookies and redirects are not used.
