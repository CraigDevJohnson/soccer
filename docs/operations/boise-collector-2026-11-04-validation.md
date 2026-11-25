# Local collector continuation validation

Completed November 4, 2026, after the user selected a local Mac installation with durable storage and a bounded live validation run. This continues the November 3 handoff. It creates a new dataset; the October dataset was not recovered.

## Delivered source and installation

- Branch: `codex/boise-bruno-collector`, based on current calendar revision `c824d81`.
- Restored collector: `81fa90c`, cherry-picked from `9c0344d` without losing newer calendar changes.
- Bruno transport and local installer: `7f86945`.
- Large-response fix and installed release: `c5d34d5466f3007566c9ee0db9023e6d34273e59`.
- Runtime: Python 3.15.2, Node.js 26.13.0, pinned Bruno CLI 4.3.0.

The installed launcher is `~/Library/Application Support/LPS Boise Collector/bin/lps-boise-collector`. Its `current` link selects the committed release under `releases/`. The database, sanitized evidence and coverage reports are under `data/boise-history/`, outside the source checkout. Source-file hashes and runtime paths are recorded in the release's `release.json`.

The launcher was exercised from an unrelated temporary working directory against a loopback server. It collected one day and two discovered divisions, then resumed with zero requests and produced an offline report. The same launcher subsequently completed the live validation.

## Fresh local checks

- Restored baseline: 76 collector tests passed.
- `task ci` passed its Go formatting, vet and tests, collector tests, and offline OpenTofu validation and tests. Infrastructure passed 13 service tests and 7 bootstrap tests.
- `task lambda` built the calendar Lambda ZIP successfully.
- After the subsequent interruption and large-response fixes, the complete collector suite passed **103 tests** in 51.236 seconds. Go and infrastructure source were unchanged by those fixes.
- Final patch checks passed. CI includes locked Bruno installation and Python 3.11/3.15 jobs on Node.js 24; that remote matrix has not been executed for this branch.

The tests use the real pinned Bruno CLI with loopback fixtures. They cover exact response bytes through nearly 8 MiB, discovery, resume, sanitized evidence, persistent backoff, both `Retry-After` forms, failed/malformed subprocess output, identity and facility rejection, redirects, authentication refusal, oversized bodies, variable subprocess startup, Ctrl-C and SIGTERM cleanup, and local installation behavior.

## Bounded live results

The cumulative budget was at most 20 requests, including diagnostic retries. The declared discovery intervals were the individual days **2010-09-26** and **2026-09-26**. Requests used the public LPS origin without account credentials. No catalog or global division-ID enumeration was used.

| Measurement | Verified result |
| --- | --- |
| Actual requests / ledger attempts | 12 |
| Successful snapshots | 10 |
| Historical failed attempts retained | 2 |
| Current resource states | 10 successful, none unresolved |
| Division seasons | 8 |
| Team registrations | 37 |
| Unique games | 101 |
| Repeated resume requests | 0 |
| Sanitized evidence integrity | Complete |
| SQLite integrity | `ok` |
| Foreign-key violations | 0 |
| Score/standing metric discrepancies | 34, preserved |
| Conflicting known game fields across current endpoint snapshots | 0 |

The two failed attempts exposed a transport defect on the first larger division response. Synchronous writes to Node's nonblocking output pipe raised `EAGAIN` and truncated the result around 64 KiB. The collector recorded failures rather than accepting an empty result. A synthetic reproduction at 100 KiB, 1 MiB and nearly 8 MiB verified the cause. The corrected bridge awaits stream completion, passed regression tests, was committed and installed, and then resumed the same dataset successfully. The initial successful day was not fetched again.

The report's declared-interval inventory check is true for those two days and their discovered graph. **All-time completeness remains false**, and the retention boundary remains unresolved. Division expansion includes games beyond the two discovery dates. The retained discrepancies do not establish a universal forfeit or administrative-score rule.

## Backup and recovery evidence

With the dataset writer locked and collection stopped, the whole dataset was copied to:

```text
~/Library/Application Support/LPS Boise Collector/backups/boise-history-20261104T070819Z/
```

All 14 source and backup files matched by SHA-256. The copied SQLite database passed integrity and foreign-key checks, and the copied evidence passed the coverage report's hash verification. This backup is on the same Mac; external-device backup was not verified.

The machine-readable validation summary is `~/Library/Application Support/LPS Boise Collector/validation/2026-11-04.json`. The original handoff was preserved under the installation's `handoffs/` directory. Use the [local setup guide](boise-collector-local-setup.md) and [operating contract](boise-collector.md) for subsequent commands.

## Remaining scope

This delivery is a local installation and bounded measured sample. No full historical backfill, scheduler, AWS activation, calendar deployment, provider inquiry, push, PR or merge was performed. A future backfill needs explicit dates and a cumulative request budget. The personal Bruno workbench and untracked API samples in the main checkout were preserved.
