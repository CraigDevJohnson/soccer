"""Durable local request queue and immutable, sanitized source observations.

The caller serializes writers and passes only fully validated Parsed responses.
Evidence is durable before the database transaction is committed; an interrupted
transaction may leave an unreferenced evidence file, never a success without one.
"""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any


SCHEMA_VERSION = "1"
KINDS = {"day", "division", "catalog"}
FAILURES = {"failed", "missing", "invalid", "blocked"}
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[1-9][0-9]*\Z")

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE intervals (
    start_day TEXT NOT NULL, end_day TEXT NOT NULL,
    PRIMARY KEY (start_day, end_day), CHECK (start_day <= end_day)
);
CREATE TABLE resources (
    kind TEXT NOT NULL, key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at REAL, last_success_at REAL, last_success_snapshot_id INTEGER,
    next_attempt_at REAL, last_error TEXT, http_status INTEGER,
    PRIMARY KEY (kind, key),
    CHECK (kind IN ('day', 'division', 'catalog')),
    CHECK (status IN ('pending','running','success','failed','missing','invalid','blocked'))
);
CREATE TABLE attempts (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL,
    started_at REAL NOT NULL, finished_at REAL, http_status INTEGER,
    outcome TEXT NOT NULL, error_code TEXT, body_sha256 TEXT, retry_at REAL,
    FOREIGN KEY (kind, key) REFERENCES resources(kind, key)
);
CREATE UNIQUE INDEX attempts_one_running ON attempts(kind, key) WHERE outcome = 'running';
CREATE TABLE snapshots (
    id INTEGER PRIMARY KEY, attempt_id INTEGER NOT NULL UNIQUE,
    kind TEXT NOT NULL, key TEXT NOT NULL, fetched_at REAL NOT NULL,
    evidence_path TEXT NOT NULL, evidence_sha256 TEXT NOT NULL,
    body_sha256 TEXT NOT NULL, warnings_json TEXT NOT NULL,
    FOREIGN KEY (attempt_id) REFERENCES attempts(id)
);
CREATE TABLE discoveries (
    division_id TEXT NOT NULL, source_kind TEXT NOT NULL, source_key TEXT NOT NULL,
    snapshot_id INTEGER NOT NULL,
    PRIMARY KEY (division_id, source_kind, source_key, snapshot_id),
    FOREIGN KEY (snapshot_id) REFERENCES snapshots(id)
);
CREATE TABLE seasons (
    id TEXT PRIMARY KEY, data_json TEXT NOT NULL, snapshot_id INTEGER NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES snapshots(id)
);
CREATE TABLE division_definitions (
    id TEXT PRIMARY KEY, data_json TEXT NOT NULL, snapshot_id INTEGER NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES snapshots(id)
);
CREATE TABLE divisions (
    id TEXT PRIMARY KEY, facility_id INTEGER NOT NULL, definition_id TEXT,
    season_id TEXT, season_number INTEGER, name TEXT, expected_games INTEGER,
    game_minutes INTEGER, snapshot_id INTEGER NOT NULL, data_json TEXT NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES snapshots(id)
);
CREATE TABLE teams (
    id TEXT PRIMARY KEY, facility_id INTEGER NOT NULL, division_id TEXT,
    season_number INTEGER, name TEXT, snapshot_id INTEGER NOT NULL, data_json TEXT NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES snapshots(id)
);
CREATE TABLE games (
    id TEXT PRIMARY KEY, facility_id INTEGER NOT NULL, home_id TEXT, away_id TEXT,
    start_local TEXT, home_score INTEGER, away_score INTEGER, result TEXT,
    winner_id TEXT, snapshot_id INTEGER NOT NULL, data_json TEXT NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES snapshots(id)
);
CREATE TABLE game_observations (
    snapshot_id INTEGER NOT NULL, game_id TEXT NOT NULL, data_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, game_id),
    FOREIGN KEY (snapshot_id) REFERENCES snapshots(id)
);
CREATE TABLE standings (
    snapshot_id INTEGER NOT NULL, division_id TEXT NOT NULL, team_id TEXT NOT NULL,
    position INTEGER, games_played INTEGER, wins INTEGER, losses INTEGER, ties INTEGER,
    goals_for INTEGER, goals_against INTEGER, points INTEGER, data_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, division_id, team_id),
    FOREIGN KEY (snapshot_id) REFERENCES snapshots(id)
);
CREATE TABLE player_leaders (
    snapshot_id INTEGER NOT NULL, division_id TEXT NOT NULL, team_id TEXT,
    player_id TEXT NOT NULL, goals INTEGER, assists INTEGER, points INTEGER,
    data_json TEXT NOT NULL, FOREIGN KEY (snapshot_id) REFERENCES snapshots(id)
);
CREATE INDEX resources_due ON resources(status, next_attempt_at, kind, key);
CREATE INDEX observations_game ON game_observations(game_id);
CREATE INDEX snapshots_resource ON snapshots(kind, key, fetched_at);
"""

# Additive bookkeeping keeps existing schema-v1 archives and readers compatible.
_REFRESH_SCHEMA = """
CREATE TABLE IF NOT EXISTS refresh_jobs (
    id TEXT PRIMARY KEY, start_day TEXT NOT NULL, end_day TEXT NOT NULL,
    include_catalog INTEGER NOT NULL, created_at REAL NOT NULL,
    completed_at REAL, status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS refresh_job_resources (
    job_id TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL,
    status TEXT NOT NULL, baseline_snapshot_id INTEGER, completed_snapshot_id INTEGER,
    skip_reason TEXT,
    PRIMARY KEY (job_id, kind, key),
    FOREIGN KEY (job_id) REFERENCES refresh_jobs(id),
    FOREIGN KEY (kind,key) REFERENCES resources(kind,key),
    FOREIGN KEY (baseline_snapshot_id) REFERENCES snapshots(id),
    FOREIGN KEY (completed_snapshot_id) REFERENCES snapshots(id)
);
CREATE TABLE IF NOT EXISTS refresh_gap_acknowledgments (
    job_id TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind='day'), key TEXT NOT NULL,
    body_sha256 TEXT NOT NULL, previous_attempt_id INTEGER NOT NULL,
    latest_attempt_id INTEGER NOT NULL, reason TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    PRIMARY KEY (job_id,kind,key),
    FOREIGN KEY (job_id,kind,key) REFERENCES refresh_job_resources(job_id,kind,key),
    FOREIGN KEY (previous_attempt_id) REFERENCES attempts(id),
    FOREIGN KEY (latest_attempt_id) REFERENCES attempts(id)
);
CREATE TRIGGER IF NOT EXISTS refresh_gap_acknowledgments_no_update
BEFORE UPDATE ON refresh_gap_acknowledgments BEGIN
    SELECT RAISE(ABORT,'refresh gap acknowledgments are immutable');
END;
CREATE TRIGGER IF NOT EXISTS refresh_gap_acknowledgments_no_replace
BEFORE INSERT ON refresh_gap_acknowledgments
WHEN EXISTS (SELECT 1 FROM refresh_gap_acknowledgments
             WHERE job_id=NEW.job_id AND kind=NEW.kind AND key=NEW.key) BEGIN
    SELECT RAISE(ABORT,'refresh gap acknowledgments are immutable');
END;
CREATE TRIGGER IF NOT EXISTS refresh_gap_acknowledgments_no_delete
BEFORE DELETE ON refresh_gap_acknowledgments BEGIN
    SELECT RAISE(ABORT,'refresh gap acknowledgments are immutable');
END;
CREATE INDEX IF NOT EXISTS attempts_resource_history ON attempts(kind,key,id);
"""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _epoch(value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError("timestamp must be a finite nonnegative number")
    return value


def _digest(value: str | None, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError("body_sha256 must be a lowercase SHA-256 digest")
    return value


def _resource_key(kind: str, key: str) -> None:
    if kind not in KINDS or not isinstance(key, str):
        raise ValueError("invalid resource kind or key")
    if kind == "division" and not _ID.fullmatch(key):
        raise ValueError("division ID must be a positive decimal string")
    if kind == "catalog" and key != "upcoming":
        raise ValueError("unknown catalog")
    if kind == "day":
        try:
            parsed = date.fromisoformat(key)
        except ValueError as exc:
            raise ValueError("day must be an ISO calendar date") from exc
        if parsed.isoformat() != key:
            raise ValueError("day must be an ISO calendar date")


class Store:
    def __init__(self, root: Path, base_url: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.evidence_dir = self.root / "evidence"
        self.base_url = base_url.rstrip("/")
        if not self.base_url:
            raise ValueError("base URL must not be empty")
        self.conn = sqlite3.connect(self.root / "collector.sqlite3", timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA synchronous = FULL")
        try:
            self._initialize()
            self.evidence_dir.mkdir(exist_ok=True, mode=0o700)
        except BaseException:
            self.conn.close()
            raise

    def _initialize(self) -> None:
        tables = {
            row[0] for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if not tables:
            try:
                self.conn.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA + _REFRESH_SCHEMA)
                self.conn.executemany(
                    "INSERT INTO meta(key,value) VALUES (?,?)",
                    [("schema_version", SCHEMA_VERSION), ("base_url", self.base_url),
                     ("next_request_at", "0")],
                )
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise
            return
        if "meta" not in tables:
            raise ValueError("unrecognized collector database: schema metadata is missing")
        metadata = dict(self.conn.execute("SELECT key,value FROM meta"))
        if metadata.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported collector schema version")
        if metadata.get("base_url") != self.base_url:
            raise ValueError("collector database belongs to a different API origin")
        required = {
            "intervals", "resources", "attempts", "snapshots", "discoveries", "seasons",
            "division_definitions", "divisions", "teams", "games", "game_observations",
            "standings", "player_leaders",
        }
        if not required.issubset(tables):
            raise ValueError("collector database is missing required tables")
        self.next_request_at()
        with self.conn:
            self.conn.executescript(_REFRESH_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def queue_interval(self, start: date, end: date) -> None:
        if type(start) is not date or type(end) is not date or start > end:
            raise ValueError("interval requires start and end dates with start <= end")
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO intervals(start_day,end_day) VALUES (?,?)",
                (start.isoformat(), end.isoformat()),
            )
            # Inclusive ordinal iteration also handles date.max without overflow.
            self.conn.executemany(
                "INSERT OR IGNORE INTO resources(kind,key) VALUES ('day',?)",
                ((date.fromordinal(n).isoformat(),) for n in range(start.toordinal(), end.toordinal() + 1)),
            )

    def queue_catalog(self) -> None:
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO resources(kind,key) VALUES ('catalog','upcoming')")

    def refresh_job(self, job_id: str) -> dict:
        if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", job_id):
            raise ValueError("job-id must be a short identifier using letters, digits, dots, dashes or underscores")
        row = self.conn.execute("SELECT * FROM refresh_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("unknown refresh job; start it with refresh --start --end --job-id")
        job = dict(row)
        counts = {r[0]: r[1] for r in self.conn.execute(
            "SELECT status,count(*) FROM refresh_job_resources WHERE job_id=? GROUP BY status", (job_id,),
        )}
        job.update(total=sum(counts.values()), completed=counts.get("success", 0),
                   pending=counts.get("pending", 0), skipped=counts.get("skipped", 0))
        job["skipped_resources"] = [dict(r) for r in self.conn.execute(
            "SELECT kind,key,skip_reason FROM refresh_job_resources WHERE job_id=? AND status='skipped' ORDER BY kind,key",
            (job_id,),
        )]
        return job

    def active_refresh_job_id(self) -> str | None:
        rows = self.conn.execute("SELECT id FROM refresh_jobs WHERE status IN ('active','needs_attention')").fetchall()
        if len(rows) > 1:
            raise ValueError("multiple unfinished refresh jobs require local checkpoint diagnosis")
        return rows[0][0] if rows else None

    def _refresh_add_member(self, job_id: str, kind: str, key: str) -> None:
        if self.conn.execute(
            "SELECT 1 FROM refresh_job_resources WHERE job_id=? AND kind=? AND key=?", (job_id, kind, key),
        ).fetchone():
            return
        resource = self.conn.execute("SELECT * FROM resources WHERE kind=? AND key=?", (kind, key)).fetchone()
        if resource is None:
            raise ValueError("refresh membership requires a discovered resource")
        skipped = resource["status"] in FAILURES
        self.conn.execute(
            "INSERT INTO refresh_job_resources(job_id,kind,key,status,baseline_snapshot_id,skip_reason) VALUES (?,?,?,?,?,?)",
            (job_id, kind, key, "skipped" if skipped else "pending", resource["last_success_snapshot_id"],
             resource["status"] if skipped else None),
        )
        if resource["status"] == "success":
            # A named refresh is not an explicit retry; keep every server deadline.
            self.conn.execute("UPDATE resources SET status='pending' WHERE kind=? AND key=?", (kind, key))

    def begin_refresh(self, job_id: str, start: date, end: date, include_catalog: bool, now: float) -> dict:
        if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", job_id):
            raise ValueError("job-id must be a short identifier using letters, digits, dots, dashes or underscores")
        if type(start) is not date or type(end) is not date or start > end:
            raise ValueError("refresh requires start and end dates with start <= end")
        now = _epoch(now)
        previous = self.conn.execute("SELECT * FROM refresh_jobs WHERE id=?", (job_id,)).fetchone()
        if previous:
            if (previous["start_day"], previous["end_day"], bool(previous["include_catalog"])) != (
                start.isoformat(), end.isoformat(), bool(include_catalog)
            ):
                raise ValueError("existing refresh job has different dates or catalog scope; use its original scope")
            return self.refresh_job(job_id)
        if self.conn.execute("SELECT 1 FROM refresh_jobs WHERE status IN ('active','needs_attention') LIMIT 1").fetchone():
            raise ValueError("another refresh job is unfinished; resume it before starting a new job")
        with self.conn:
            self.conn.execute(
                "INSERT INTO refresh_jobs(id,start_day,end_day,include_catalog,created_at) VALUES (?,?,?,?,?)",
                (job_id, start.isoformat(), end.isoformat(), int(bool(include_catalog)), now),
            )
            self.conn.execute("INSERT OR IGNORE INTO intervals(start_day,end_day) VALUES (?,?)", (start.isoformat(), end.isoformat()))
            for ordinal in range(start.toordinal(), end.toordinal() + 1):
                key = date.fromordinal(ordinal).isoformat()
                self.conn.execute("INSERT OR IGNORE INTO resources(kind,key) VALUES ('day',?)", (key,))
                self._refresh_add_member(job_id, "day", key)
            divisions = {r[0] for r in self.conn.execute(
                "SELECT DISTINCT division_id FROM discoveries WHERE source_kind='day' AND source_key BETWEEN ? AND ?",
                (start.isoformat(), end.isoformat()),
            )}
            divisions.update(r[0] for r in self.conn.execute(
                "SELECT DISTINCT t.division_id FROM games g JOIN teams t ON t.id=g.home_id OR t.id=g.away_id "
                "WHERE substr(g.start_local,1,10) BETWEEN ? AND ? AND t.division_id IS NOT NULL",
                (start.isoformat(), end.isoformat()),
            ))
            for ident in sorted(divisions):
                self._refresh_add_member(job_id, "division", ident)
            if include_catalog:
                self.conn.execute("INSERT OR IGNORE INTO resources(kind,key) VALUES ('catalog','upcoming')")
                self._refresh_add_member(job_id, "catalog", "upcoming")
        return self.refresh_job(job_id)

    def refresh_resources(self, job_id: str) -> set[tuple[str, str]]:
        self.refresh_job(job_id)
        # A valid newer checkpoint can have been written by an older collector.
        # Reconcile that completed work rather than stranding or refetching it.
        completed = self.conn.execute(
            "SELECT j.kind,j.key,r.last_success_snapshot_id FROM refresh_job_resources j "
            "JOIN resources r ON r.kind=j.kind AND r.key=j.key "
            "WHERE j.job_id=? AND j.status='pending' AND r.status='success' "
            "AND r.last_success_snapshot_id IS NOT NULL AND r.last_success_snapshot_id IS NOT j.baseline_snapshot_id",
            (job_id,),
        ).fetchall()
        with self.conn:
            for row in completed:
                self.conn.execute(
                    "UPDATE refresh_job_resources SET status='success',completed_snapshot_id=? WHERE job_id=? AND kind=? AND key=?",
                    (row["last_success_snapshot_id"], job_id, row["kind"], row["key"]),
                )
                for discovery in self.conn.execute("SELECT division_id FROM discoveries WHERE snapshot_id=?", (row["last_success_snapshot_id"],)).fetchall():
                    self._refresh_add_member(job_id, "division", discovery[0])
        return {(row[0], row[1]) for row in self.conn.execute(
            "SELECT kind,key FROM refresh_job_resources WHERE job_id=? AND status='pending'", (job_id,),
        )}

    def _finish_refresh(self, job_id: str, now: float) -> None:
        job = self.refresh_job(job_id)
        terminal = self.conn.execute(
            "SELECT 1 FROM refresh_job_resources j JOIN resources r ON r.kind=j.kind AND r.key=j.key "
            "WHERE j.job_id=? AND j.status='pending' AND r.status IN ('invalid','missing','blocked') LIMIT 1",
            (job_id,),
        ).fetchone()
        status = ("needs_attention" if terminal else "active") if job["pending"] else (
            "complete_with_gaps" if job["skipped"] else "complete"
        )
        self.conn.execute(
            "UPDATE refresh_jobs SET status=?,completed_at=CASE WHEN ?=0 THEN COALESCE(completed_at,?) ELSE NULL END WHERE id=?",
            (status, job["pending"], _epoch(now), job_id),
        )

    def finish_refresh(self, job_id: str, now: float) -> dict:
        with self.conn:
            self._finish_refresh(job_id, now)
        return self.refresh_job(job_id)

    def acknowledge_refresh_gap(self, job_id: str, kind: str, key: str,
                                body_sha256: str, reason: str, now: float) -> dict:
        """Record an operator-diagnosed empty-game source gap, never a retry."""
        _resource_key(kind, key)
        _digest(body_sha256)
        now = _epoch(now)
        if kind != "day" or reason != "confirmed_empty_game_slot":
            raise ValueError("gap acknowledgment requires a diagnosed day with reason confirmed_empty_game_slot")
        self.refresh_job(job_id)
        with self.conn:
            if not self.conn.in_transaction:
                self.conn.execute("BEGIN IMMEDIATE")
            recorded = self.conn.execute(
                "SELECT * FROM refresh_gap_acknowledgments WHERE job_id=? AND kind=? AND key=?",
                (job_id, kind, key),
            ).fetchone()
            if recorded:
                if recorded["body_sha256"] != body_sha256 or recorded["reason"] != reason:
                    raise ValueError("this job gap already has a different immutable acknowledgment")
                return {"acknowledgment": dict(recorded), "already_recorded": True,
                        "refresh_job": self.refresh_job(job_id)}
            member = self.conn.execute(
                "SELECT status FROM refresh_job_resources WHERE job_id=? AND kind=? AND key=?",
                (job_id, kind, key),
            ).fetchone()
            if member is None or member["status"] != "pending":
                raise ValueError("only a pending member of the named refresh job can be acknowledged")
            resource = self.conn.execute(
                "SELECT status,http_status,last_error FROM resources WHERE kind=? AND key=?", (kind, key),
            ).fetchone()
            if tuple(resource) != ("invalid", 200, "invalid_source_response"):
                raise ValueError("acknowledgment requires the current HTTP 200 invalid_source_response state")
            attempts = self.conn.execute(
                "SELECT id,outcome,http_status,error_code,body_sha256,finished_at FROM attempts "
                "WHERE kind=? AND key=? ORDER BY id DESC LIMIT 2", (kind, key),
            ).fetchall()
            expected = ("invalid", 200, "invalid_source_response", body_sha256)
            if len(attempts) != 2 or any(tuple(row)[1:5] != expected or row["finished_at"] is None for row in attempts):
                raise ValueError("acknowledgment requires two consecutive matching failed-body hashes and HTTP 200 validation failures")
            self.conn.execute(
                "INSERT INTO refresh_gap_acknowledgments(job_id,kind,key,body_sha256,previous_attempt_id,latest_attempt_id,reason,recorded_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (job_id, kind, key, body_sha256, attempts[1]["id"], attempts[0]["id"], reason, now),
            )
            self.conn.execute(
                "UPDATE refresh_job_resources SET status='skipped',skip_reason=? WHERE job_id=? AND kind=? AND key=?",
                (reason, job_id, kind, key),
            )
            self._finish_refresh(job_id, now)
            recorded = self.conn.execute(
                "SELECT * FROM refresh_gap_acknowledgments WHERE job_id=? AND kind=? AND key=?", (job_id, kind, key),
            ).fetchone()
        return {"acknowledgment": dict(recorded), "already_recorded": False,
                "refresh_job": self.refresh_job(job_id)}

    def next_resource(self, now: float, excluded: set[tuple[str, str]], *, refresh=False) -> dict | None:
        now = _epoch(now)
        order = "WHEN 'catalog' THEN 0 WHEN 'day' THEN 1" if refresh else "WHEN 'division' THEN 0 WHEN 'catalog' THEN 1"
        rows = self.conn.execute(
            f"""SELECT * FROM resources
               WHERE status IN ('pending','failed')
                 AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
               ORDER BY CASE kind {order} ELSE 2 END, key""",
            (now,),
        )
        for row in rows:
            if (row["kind"], row["key"]) not in excluded:
                return dict(row)
        return None

    def start_attempt(self, kind: str, key: str, now: float) -> int:
        _resource_key(kind, key)
        now = _epoch(now)
        with self.conn:
            active = self.active_refresh_job_id()
            if active and not self.conn.execute(
                "SELECT 1 FROM refresh_job_resources WHERE job_id=? AND kind=? AND key=? AND status='pending'",
                (active, kind, key),
            ).fetchone():
                raise ValueError("an unfinished refresh job owns collection; resume it before requesting unrelated resources")
            row = self.conn.execute("SELECT * FROM resources WHERE kind=? AND key=?", (kind, key)).fetchone()
            if row is None:
                raise ValueError("cannot request an undiscovered resource")
            if kind == "division" and self.conn.execute(
                "SELECT 1 FROM discoveries WHERE division_id=? LIMIT 1", (key,),
            ).fetchone() is None:
                raise ValueError("division has no verified source discovery")
            if row["status"] not in {"pending", "failed"}:
                raise ValueError("resource must be queued before requesting it")
            if row["next_attempt_at"] is not None and row["next_attempt_at"] > now:
                raise ValueError("resource retry deadline has not elapsed")
            cursor = self.conn.execute(
                "INSERT INTO attempts(kind,key,started_at,outcome) VALUES (?,?,?,'running')",
                (kind, key, now),
            )
            self.conn.execute(
                """UPDATE resources SET status='running', attempts=attempts+1,
                   last_attempt_at=?,next_attempt_at=NULL WHERE kind=? AND key=?""",
                (now, kind, key),
            )
            return int(cursor.lastrowid)

    def _attempt(self, attempt_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if row is None or row["outcome"] != "running" or row["finished_at"] is not None:
            raise ValueError("attempt is absent or already finished")
        return row

    def _write_evidence(self, evidence: dict) -> tuple[str, str]:
        if not isinstance(evidence, dict):
            raise ValueError("sanitized evidence must be an object")
        content = (_json(evidence) + "\n").encode("utf-8")
        sha = hashlib.sha256(content).hexdigest()
        destination = self.evidence_dir / (sha + ".json")

        def check_existing() -> None:
            if destination.is_symlink() or destination.read_bytes() != content:
                raise ValueError("existing evidence does not match its content address")

        if destination.exists() or destination.is_symlink():
            check_existing()
            return destination.relative_to(self.root).as_posix(), sha
        fd, temporary = tempfile.mkstemp(prefix=".pending-", suffix=".json", dir=self.evidence_dir)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                # Linking is atomic and fails instead of replacing existing evidence.
                os.link(temporary, destination)
            except FileExistsError:
                check_existing()
            directory_fd = os.open(self.evidence_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            os.unlink(temporary)
        return destination.relative_to(self.root).as_posix(), sha

    def _upsert(self, table: str, columns: tuple[str, ...], item: dict, snapshot_id: int) -> None:
        names = ("id",) + columns + ("snapshot_id", "data_json")
        values = [item.get(name) for name in ("id",) + columns] + [snapshot_id, _json(item)]
        placeholders = ",".join("?" for _ in names)
        updates = ",".join(f"{name}=excluded.{name}" for name in names if name != "id")
        self.conn.execute(
            f"INSERT INTO {table} ({','.join(names)}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}", values,
        )

    def _upsert_metadata(self, table: str, columns: tuple[str, ...], item: dict,
                         snapshot_id: int, *, catalog=False) -> None:
        previous = self.conn.execute(f"SELECT data_json,snapshot_id FROM {table} WHERE id=?", (item["id"],)).fetchone()
        merged = json.loads(previous["data_json"]) if previous else {}
        sources = merged.pop("_field_sources", {})

        def record_sources(values, prefix, source):
            for key, value in values.items():
                path = prefix + key
                if isinstance(value, dict):
                    record_sources(value, path + ".", source)
                elif value is not None and key != "date_warnings":
                    sources.setdefault(path, source)

        if previous:
            record_sources(merged, "", previous["snapshot_id"])

        def merge_field(target, key, value, path):
            if isinstance(value, dict) and value:
                if not isinstance(target.get(key), dict):
                    target[key] = {}
                for name, child in value.items():
                    merge_field(target[key], name, child, path + "." + name)
            elif value is not None and value != "" and value != {}:
                target[key] = value
                sources[path] = snapshot_id
            elif key not in target:
                target[key] = value

        for key, value in item.items():
            if key == "date_warnings":
                continue
            if catalog and table == "divisions" and key == "standings_state":
                continue
            merge_field(merged, key, value, key)
        if table == "seasons":
            dates = [datetime.fromisoformat(merged[k].replace("Z", "+00:00")) if merged.get(k) else None
                     for k in ("raw_start", "raw_end")]
            merged["date_warnings"] = (["source_dates_before_2000"] if any(d and d.year < 2000 for d in dates) else [])
            if all(dates) and dates[0].replace(tzinfo=None) > dates[1].replace(tzinfo=None):
                merged["date_warnings"].append("source_dates_reversed")
        merged["_field_sources"] = sources
        anchor = snapshot_id
        if catalog and table == "divisions":
            merged.setdefault("standings_state", "unavailable")
            if previous and self.conn.execute("SELECT kind FROM snapshots WHERE id=?", (previous["snapshot_id"],)).fetchone()[0] == "division":
                anchor = previous["snapshot_id"]
        self._upsert(table, columns, merged, anchor)

    def succeed(self, attempt_id: int, parsed: Any, body_sha256: str, now: float,
                *, refresh_job_id: str | None = None) -> int:
        now = _epoch(now)
        _digest(body_sha256)
        attempt = self._attempt(attempt_id)
        active = self.active_refresh_job_id()
        if active:
            if refresh_job_id not in (None, active):
                raise ValueError("another refresh job owns the active checkpoint")
            if not self.conn.execute(
                "SELECT 1 FROM refresh_job_resources WHERE job_id=? AND kind=? AND key=? AND status='pending'",
                (active, attempt["kind"], attempt["key"]),
            ).fetchone():
                raise ValueError("successful resource is outside the active refresh job")
            refresh_job_id = active
        discoveries = sorted(parsed.discoveries)
        for division_id in discoveries:
            _resource_key("division", division_id)
        evidence_path, evidence_sha256 = self._write_evidence(parsed.evidence)
        with self.conn:
            # Recheck inside the transaction before changing an attempt checkpoint.
            attempt = self._attempt(attempt_id)
            cursor = self.conn.execute(
                """INSERT INTO snapshots(attempt_id,kind,key,fetched_at,evidence_path,
                   evidence_sha256,body_sha256,warnings_json) VALUES (?,?,?,?,?,?,?,?)""",
                (attempt_id, attempt["kind"], attempt["key"], now, evidence_path,
                 evidence_sha256, body_sha256, _json(parsed.warnings)),
            )
            snapshot_id = int(cursor.lastrowid)
            for season in ([parsed.season] if parsed.season is not None else []) + list(getattr(parsed, "seasons", [])):
                self._upsert_metadata("seasons", (), season, snapshot_id)
            for definition in ([parsed.definition] if parsed.definition is not None else []) + list(getattr(parsed, "definitions", [])):
                self._upsert_metadata("division_definitions", (), definition, snapshot_id)
            for division in ([parsed.division] if parsed.division is not None else []) + list(getattr(parsed, "divisions", [])):
                self._upsert_metadata("divisions", (
                    "facility_id", "definition_id", "season_id", "season_number", "name",
                    "expected_games", "game_minutes",
                ), division, snapshot_id, catalog=attempt["kind"] == "catalog")
            for team in parsed.teams:
                self._upsert_metadata("teams", ("facility_id", "division_id", "season_number", "name"), team, snapshot_id)
            for game in parsed.games:
                self._upsert("games", (
                    "facility_id", "home_id", "away_id", "start_local", "home_score",
                    "away_score", "result", "winner_id",
                ), game, snapshot_id)
                self.conn.execute(
                    "INSERT INTO game_observations(snapshot_id,game_id,data_json) VALUES (?,?,?)",
                    (snapshot_id, game["id"], _json(game)),
                )
            for standing in parsed.standings:
                columns = ("division_id", "team_id", "position", "games_played", "wins", "losses",
                           "ties", "goals_for", "goals_against", "points")
                self.conn.execute(
                    f"INSERT INTO standings(snapshot_id,{','.join(columns)},data_json) "
                    f"VALUES ({','.join('?' for _ in range(len(columns) + 2))})",
                    [snapshot_id] + [standing.get(column) for column in columns] + [_json(standing)],
                )
            for leader in parsed.leaders:
                columns = ("division_id", "team_id", "player_id", "goals", "assists", "points")
                self.conn.execute(
                    f"INSERT INTO player_leaders(snapshot_id,{','.join(columns)},data_json) "
                    f"VALUES ({','.join('?' for _ in range(len(columns) + 2))})",
                    [snapshot_id] + [leader.get(column) for column in columns] + [_json(leader)],
                )
            for division_id in discoveries:
                self.conn.execute(
                    "INSERT INTO discoveries(division_id,source_kind,source_key,snapshot_id) VALUES (?,?,?,?)",
                    (division_id, attempt["kind"], attempt["key"], snapshot_id),
                )
                self.conn.execute(
                    "INSERT OR IGNORE INTO resources(kind,key) VALUES ('division',?)", (division_id,),
                )
            self.conn.execute(
                """UPDATE attempts SET finished_at=?,http_status=200,outcome='success',
                   body_sha256=?,error_code=NULL,retry_at=NULL WHERE id=?""",
                (now, body_sha256, attempt_id),
            )
            self.conn.execute(
                """UPDATE resources SET status='success',last_success_at=?,last_success_snapshot_id=?,
                   next_attempt_at=NULL,last_error=NULL,http_status=200 WHERE kind=? AND key=?""",
                (now, snapshot_id, attempt["kind"], attempt["key"]),
            )
            if refresh_job_id is not None:
                self.conn.execute(
                    "UPDATE refresh_job_resources SET status='success',completed_snapshot_id=? "
                    "WHERE job_id=? AND kind=? AND key=? AND status='pending'",
                    (snapshot_id, refresh_job_id, attempt["kind"], attempt["key"]),
                )
                for ident in discoveries:
                    self._refresh_add_member(refresh_job_id, "division", ident)
            return snapshot_id

    def fail(self, attempt_id: int, outcome: str, http_status: int | None,
             error_code: str, body_sha256: str | None, retry_at: float | None, now: float) -> None:
        if outcome not in FAILURES:
            raise ValueError("invalid failure outcome")
        now = _epoch(now)
        retry_at = _epoch(retry_at) if retry_at is not None else None
        _digest(body_sha256, optional=True)
        if not isinstance(error_code, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,120}", error_code):
            raise ValueError("error_code must be a short identifier, never raw response content")
        if http_status is not None and (type(http_status) is not int or not 100 <= http_status <= 599):
            raise ValueError("invalid HTTP status")
        with self.conn:
            attempt = self._attempt(attempt_id)
            self.conn.execute(
                """UPDATE attempts SET finished_at=?,http_status=?,outcome=?,error_code=?,
                   body_sha256=?,retry_at=? WHERE id=?""",
                (now, http_status, outcome, error_code, body_sha256, retry_at, attempt_id),
            )
            self.conn.execute(
                """UPDATE resources SET status=?,next_attempt_at=?,last_error=?,http_status=?
                   WHERE kind=? AND key=?""",
                (outcome, retry_at, error_code, http_status, attempt["kind"], attempt["key"]),
            )

    def recover_interrupted(self, now: float) -> None:
        now = _epoch(now)
        with self.conn:
            self.conn.execute(
                """UPDATE resources SET status='pending',last_error='interrupted'
                   WHERE status='running'"""
            )
            self.conn.execute(
                """UPDATE attempts SET outcome='interrupted',finished_at=?,error_code='interrupted'
                   WHERE outcome='running' AND finished_at IS NULL""", (now,),
            )

    def requeue(self, kind: str, key: str) -> None:
        _resource_key(kind, key)
        with self.conn:
            row = self.conn.execute("SELECT status FROM resources WHERE kind=? AND key=?", (kind, key)).fetchone()
            if row is None:
                raise ValueError("cannot queue an undiscovered resource")
            if row["status"] == "running":
                raise ValueError("cannot requeue an active request")
            self.conn.execute(
                "UPDATE resources SET status='pending' WHERE kind=? AND key=?", (kind, key),
            )

    def retry_failures(self, kind: str, key: str) -> int:
        return self.conn.execute(
            "SELECT count(*) FROM attempts WHERE kind=? AND key=? AND outcome='failed' AND id>"
            "COALESCE((SELECT max(id) FROM attempts WHERE kind=? AND key=? AND outcome='success'),0)",
            (kind, key, kind, key),
        ).fetchone()[0]

    def set_next_request_at(self, epoch: float) -> None:
        value = _epoch(epoch)
        with self.conn:
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES ('next_request_at',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(value),),
            )

    def next_request_at(self) -> float:
        row = self.conn.execute("SELECT value FROM meta WHERE key='next_request_at'").fetchone()
        if row is None:
            raise ValueError("collector database is missing its pacing checkpoint")
        return _epoch(row["value"])
