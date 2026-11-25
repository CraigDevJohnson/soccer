# Collector validation — October 14, 2026

This is bounded acceptance evidence for the local collector, not a full Boise archive. The implementation was based on repository revision `d0dc7e1`; no Go application behavior or production infrastructure changed.

## Offline checks

- `task collector:test`: **76 tests passed** on local Python 3.15.0. CI configuration also targets Python 3.11; remote CI was not run as part of this local delivery.
- `task ci`: passed Go formatting, vet and tests, collector tests, and OpenTofu validation/tests for both roots (13 service tests and 7 bootstrap tests). Collector tests were repeated after the final Python changes.
- `task lambda`: built the existing arm64 Lambda package successfully.
- Independent review found and verified repairs for a Retry-After crash window and a false completeness claim when participant/division mappings were missing. Regression coverage also checks verified mappings supplied by leaderboard team objects.
- SQLite `integrity_check` returned `ok`; `foreign_key_check` returned no violations for both live sample databases.

The tests cover malformed/cross-facility/wrong-identity data, crossovers, official-versus-calculated standings, private-field removal, missing/corrupt evidence, rollback and interruption, process restart, server backoff, redirect refusal, response bounds, and selective refresh. HTTP tests use loopback fixtures only.

## Live historical and tournament sample

Four explicit one-day scans were collected into `dist/boise-collector-validation`, using serial requests within each run and a one-second request-start minimum:

| Discovery date | Source season | Divisions | Team registrations | Unique games |
|---|---:|---:|---:|---:|
| 2010-09-26 | 75 | 1 | 8 | 52 |
| 2015-09-26 | 101 | 5 | 28 | 112 |
| 2018-09-29 | 119 | 10 | 71 | 283 |
| 2026-09-26 | 174 | 7 | 29 | 49 |
| Total | 4 seasons | **23** | **136** | **496** |

All four declared dates and their discovered division references are resolved, with intact evidence. They are four sampled dates, not continuous coverage from 2010 through 2026. All-time completeness remains false.

The ledger retains one initial invalid attempt caused by a historical leaderboard player's changed home facility. After correcting that semantic validation and explicitly retrying the already-discovered division, the resource succeeded. This exercised visible failure recovery without losing the successful day checkpoint.

A forced refresh of known division `13394` issued one request and added a snapshot while preserving the earlier observations/standings. The final database has **27 successful resources, 28 successful snapshots and 29 attempts**. Subsequent `resume --max-requests 1` returned **zero requests**. The report retains **55 metric discrepancies** and no differing known game fields across the latest endpoint snapshots; discrepancies include expected tournament counts and administrative-score differences, not asserted source defects.

Generated local artifacts (ignored by Git):

- [Coverage report](../../dist/boise-collector-validation/reports/coverage.md)
- [Coverage JSON](../../dist/boise-collector-validation/reports/coverage.json)
- [SQLite database](../../dist/boise-collector-validation/collector.sqlite3)

## Unpublished schedule and budget boundary

A separate `scan --include-upcoming --max-requests 2` in `dist/boise-collector-upcoming-validation` fetched the catalog and division `115460`. The division is valid, with zero games, null standings, and both schedule-publication flags false. It was preserved as unpublished data.

The run stopped at its explicit budget with **54 pending resources**: 53 discovered divisions and the requested day. Its report correctly marks the inventory incomplete. Running ordinary `refresh` immediately afterward made **zero requests** and did not process those pending historical/date-discovery tasks, because no successful resource was old enough to refresh.

- [Partial coverage report](../../dist/boise-collector-upcoming-validation/reports/coverage.md)
- [Partial coverage JSON](../../dist/boise-collector-upcoming-validation/reports/coverage.json)

No full 6,129-date backfill, scheduled polling, inquiries, access applications, deployment, push, or pull request was performed. See the [runbook](boise-collector.md) for deliberate scan/resume/refresh commands.
