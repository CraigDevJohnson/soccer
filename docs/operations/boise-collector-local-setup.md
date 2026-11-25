# Local Mac collector setup

The collector runs on demand on this Mac. Its launcher, versioned source, SQLite database and sanitized evidence live outside Git worktrees. Installation does not contact LPS or create a schedule.

## Install a verified release

Use Python 3.11+, Node.js 24+, npm and Git. From the soccer repository:

```sh
task collector:setup
task collector:test
# Commit the verified collector changes before installation.
task collector:install
```

The installer copies the committed collector and runbooks, installs Bruno 4.4.0 from the committed npm lockfile with lifecycle scripts disabled, checks the installed CLI and launcher, then switches `current` to the release. It refuses uncommitted collector or runbook edits. Unrelated checkout changes are allowed and are not copied.

The default layout is:

```text
~/Library/Application Support/LPS Boise Collector/
  bin/lps-boise-collector
  current -> releases/<source-commit>
  releases/<source-commit>/
    scripts/boise_collect/
    docs/
    release.json
  data/boise-history/
  backups/
```

`release.json` records the commit, source-file hashes and absolute Python, Node and npm paths. The installer preserves existing data and releases. Reinstalling the same revision checks and reuses its release. If dependency installation fails, the previous launcher and release remain selected. Rerun installation after upgrading or removing the recorded Python or Node installation.

For another local destination, use `task collector:install -- --prefix /absolute/path`. The destination must be outside the source checkout. No shell profile or global executable is changed.

## Run an explicit bounded collection

The stable launcher defaults to the durable data directory:

```sh
collector="$HOME/Library/Application Support/LPS Boise Collector/bin/lps-boise-collector"
"$collector" --help
"$collector" scan --start 2010-09-26 --end 2010-09-26 --max-requests 10
"$collector" resume --max-requests 10
"$collector" report
```

The `scan` and `resume` commands above make live public API requests. Choose a cumulative request budget for each session; each invocation has its own limit. `resume` also handles other outstanding resources in the same dataset. A drained resume makes no requests. `report` works offline. Pass `--data-dir /absolute/path` to deliberately select another dataset.

The first scan creates a new dataset. The historical October dataset was not recovered. See [the operating contract](boise-collector.md) for failures, retry deadlines, refresh selection and exit codes. An exit code of 2 can mean a budget or server deadline was reached; inspect the report and ledger before deciding whether to resume.

Only one live writer should use the provider at a time. Each data directory has an independent writer lock and pacing state. The collector waits at least one second after each request completes before starting another request runner. This conservatively preserves minimum HTTP spacing when Bruno startup time varies. A provider `Retry-After` deadline can extend that delay across restarts.

## Preserve and recover data

Stop collection before making a backup. Copy the entire `data/boise-history` directory into a new dated directory under `backups`, including SQLite, evidence and reports. A backup on this Mac protects against worktree removal and accidental edits; it is not protection against loss of the Mac. Include the installation directory in the user's existing external backup process where available.

Do not edit evidence or SQLite records to hide an error. For recovery, copy a trustworthy whole-dataset backup into a separate directory, run SQLite integrity and foreign-key checks, and use `report --data-dir` to verify every evidence hash before collection resumes. Keep the current dataset intact until the backup has passed verification.

The collector retains published standings alongside calculated score audits. It does not claim all-time completeness, infer permanent team identities or reconstruct missing player records. A small successful validation is evidence that collection and recovery work for that sample, not a completed historical backfill.

## Development and delivery

`task ci` runs the Go, collector and offline OpenTofu checks after `task collector:setup`. `task lambda` still builds only the calendar API. The local collector installation does not deploy that Lambda or activate the separate portfolio archive.

Changes to the repository's `Taskfile.yaml` can trigger its existing calendar Deploy workflow when later merged to `main`. Local installation does not push, create a PR or merge. Review those delivery actions separately when requested.
