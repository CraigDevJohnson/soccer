"""Serial, public-only collection with durable pacing and bounded work."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from .model import ValidationError, parse_response
from .store import Store

ORIGIN = "https://lps-api-prod.lps-test.com"
MAX_BODY = 8 * 1024 * 1024
BOISE = ZoneInfo("America/Denver")


def validated_origin(value: str) -> str:
    parts = urllib.parse.urlsplit(value)
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("base URL must not contain credentials, a query or a fragment")
    if parts.path not in ("", "/"):
        raise ValueError("base URL must be an origin without a path")
    # No general proxy or alternate production provider: loopback is for fixtures.
    if value.rstrip("/") == ORIGIN:
        return ORIGIN
    if parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1"):
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError("invalid loopback port")
        return value.rstrip("/")
    raise ValueError("base URL must be the LPS HTTPS origin or a loopback HTTP fixture")


def request_path(kind: str, key: str) -> str:
    if kind == "day":
        day = date.fromisoformat(key)
        if day.isoformat() != key:
            raise ValueError("day key must be YYYY-MM-DD")
        return "/games?" + urllib.parse.urlencode(
            {"facility_id": "4", "date": day.strftime("%d-%m-%Y"), "complete_information": "true"}
        )
    if kind == "division" and re.fullmatch(r"[1-9][0-9]{0,29}", key):
        return "/division_seasons/" + key
    if kind == "catalog" and key == "upcoming":
        return "/division_seasons/upcoming_leagues?facility_id=4"
    raise ValueError("unknown resource kind or invalid resource key")


@dataclass
class Response:
    status: int
    body: bytes
    retry_after: str | None = None


class FetchError(Exception):
    def __init__(self, code: str, status: int | None = None, retry_after: str | None = None):
        super().__init__(code)
        self.status = status
        self.retry_after = retry_after


class CollectorClient(Protocol):
    base_url: str

    def get(self, kind: str, key: str) -> Response: ...


def retry_deadline(value: str | None, now: float) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]+", value):
        try:
            result = now + int(value)
        except (ValueError, OverflowError):
            raise ValueError("unrepresentable Retry-After") from None
        if not math.isfinite(result):
            raise ValueError("unrepresentable Retry-After")
        return result
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return None
        return max(now, parsed.timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


@contextlib.contextmanager
def writer_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "collector.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another collector is writing this data directory") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_origin(root: Path) -> str:
    import sqlite3

    path = (root / "collector.sqlite3").resolve()
    with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='base_url'").fetchone()
    if row is None:
        raise ValueError("collector has no recorded source origin")
    return validated_origin(row[0])


def queue_refresh(
    store: Store, now: float, active_hours: float, historical_days: float,
    division_ids: list[str] | None = None,
) -> set[tuple[str, str]]:
    """Refresh known divisions; recency is a polling class, not completeness."""
    if store.active_refresh_job_id():
        raise ValueError("a named refresh job is unfinished; resume it before planning another refresh")
    if division_ids:
        ids = sorted(set(division_ids))
        for ident in ids:
            if store.conn.execute(
                "SELECT 1 FROM discoveries WHERE division_id=?", (ident,)
            ).fetchone() is None:
                raise ValueError("refresh accepts only previously discovered division IDs")
            status = store.conn.execute("SELECT status FROM resources WHERE kind='division' AND key=?", (ident,)).fetchone()[0]
            if status not in {"success", "pending"}:
                raise ValueError("refresh does not retry failed or invalid resources; diagnose them before an explicit retry")
        for ident in ids:
            store.requeue("division", ident)
        return {("division", ident) for ident in ids}

    recent_boundary = (datetime.fromtimestamp(now, BOISE).date() - timedelta(days=14)).isoformat()
    queued = set()
    rows = store.conn.execute(
        "SELECT r.*, d.snapshot_id FROM resources r LEFT JOIN divisions d ON d.id=r.key "
        "WHERE r.kind='division' AND r.status='success'"
    ).fetchall()
    for row in rows:
        starts = []
        if row["snapshot_id"]:
            for observation in store.conn.execute(
                "SELECT data_json FROM game_observations WHERE snapshot_id=?", (row["snapshot_id"],)
            ):
                start = json.loads(observation[0]).get("start_local")
                if start:
                    starts.append(start[:10])
        recent = not starts or max(starts) >= recent_boundary
        age = active_hours * 3600 if recent else historical_days * 86400
        if row["last_success_at"] is None or now - row["last_success_at"] >= age:
            store.requeue("division", row["key"])
            queued.add(("division", row["key"]))
    catalog = store.conn.execute(
        "SELECT * FROM resources WHERE kind='catalog' AND key='upcoming'"
    ).fetchone()
    if catalog and catalog["status"] == "success" and (
        catalog["last_success_at"] is None or now - catalog["last_success_at"] >= active_hours * 3600
    ):
        store.requeue("catalog", "upcoming")
        queued.add(("catalog", "upcoming"))
    return queued


def validate_deferred_divisions(store: Store, division_ids: list[str] | None) -> set[tuple[str, str]]:
    """Select explicit upstream failures without changing their ledger or deadlines."""
    deferred = set()
    for ident in sorted(set(division_ids or [])):
        request_path("division", ident)
        row = store.conn.execute(
            "SELECT status,attempts,http_status,last_error FROM resources "
            "WHERE kind='division' AND key=? AND EXISTS "
            "(SELECT 1 FROM discoveries WHERE division_id=?)", (ident, ident),
        ).fetchone()
        if (row is None or row["status"] != "failed" or row["attempts"] < 5
                or row["http_status"] is None or not 500 <= row["http_status"] <= 599
                or row["last_error"] != f"http_{row['http_status']}"):
            raise ValueError(
                "defer-division requires a discovered division with at least five attempts "
                "and a current HTTP 5xx failure"
            )
        deferred.add(("division", ident))
    return deferred


def collect(
    store: Store, client: CollectorClient, max_requests: int = 100, delay: float = 1,
    clock=time.time, sleep=time.sleep, progress=None,
    allowed_resources: set[tuple[str, str]] | None = None,
    deferred_resources: set[tuple[str, str]] | None = None,
    refresh_job_id: str | None = None,
) -> dict:
    if max_requests < 1 or not math.isfinite(delay) or delay < 0:
        raise ValueError("request budget must be positive and delay finite/nonnegative")
    if client.base_url == ORIGIN and delay < 1:
        raise ValueError("live requests must be paced at least one second apart")
    store.recover_interrupted(clock())
    active_job = store.active_refresh_job_id()
    if refresh_job_id is not None and active_job not in (None, refresh_job_id):
        raise ValueError("another refresh job is unfinished; resume that job first")
    if active_job and refresh_job_id is None and allowed_resources is not None:
        raise ValueError("a named refresh job owns collection; resume it without an unrelated resource selection")
    refresh_job_id = refresh_job_id or active_job
    if refresh_job_id is not None:
        allowed_resources = store.refresh_resources(refresh_job_id)
    deferred = set(deferred_resources or ())
    attempted: set[tuple[str, str]] = set()
    result = {"requests": 0, "successes": 0, "failures": 0, "reason": "drained",
              "deferred_resource_count": len(deferred),
              "deferred_divisions": sorted(key for kind, key in deferred)}
    while result["requests"] < max_requests:
        now = clock()
        excluded = attempted | deferred
        if allowed_resources is not None:
            excluded.update(
                (row[0], row[1]) for row in store.conn.execute("SELECT kind,key FROM resources")
                if (row[0], row[1]) not in allowed_resources
            )
        resource = store.next_resource(now, excluded, refresh=refresh_job_id is not None)
        if resource is None:
            break
        wait = store.next_request_at() - now
        if wait > 30:
            result["reason"] = "deferred"
            break
        if wait > 0:
            sleep(wait)
            continue
        kind, key = resource["kind"], resource["key"]
        request_path(kind, key)
        if kind == "division" and store.conn.execute(
            "SELECT 1 FROM discoveries WHERE division_id=?", (key,)
        ).fetchone() is None:
            raise ValueError("division has no verified discovery source")
        attempted.add((kind, key))
        prior_failures = store.retry_failures(kind, key)
        attempt = store.start_attempt(kind, key, now)
        store.set_next_request_at(now + delay)
        result["requests"] += 1
        status = None
        digest = None
        retry_at = None
        try:
            try:
                response = client.get(kind, key)
            finally:
                # Runner startup varies. Space requests from completion as well,
                # so a faster next subprocess cannot compress the actual HTTP gap.
                store.set_next_request_at(max(store.next_request_at(), clock() + delay))
        except FetchError as exc:
            status = exc.status
            outcome = "invalid" if str(exc) == "body_too_large" else "failed"
            error = str(exc)
            if status in (401, 403):
                outcome, error = "blocked", "authentication_refused"
            try:
                retry_at = retry_deadline(exc.retry_after, clock())
            except ValueError:
                outcome, error = "blocked", "invalid_retry_after"
            if retry_at is None and outcome == "failed":
                retry_at = clock() + min(3600, 30 * 2 ** min(prior_failures, 7))
        else:
            status = response.status
            digest = hashlib.sha256(response.body).hexdigest()
            try:
                retry_at = retry_deadline(response.retry_after, clock())
            except ValueError:
                outcome, error = "blocked", "invalid_retry_after"
            else:
                if status == 200:
                    try:
                        body = json.loads(response.body)
                        parsed = parse_response(kind, key, body)
                    except (ValueError, UnicodeDecodeError, ValidationError):
                        outcome, error = "invalid", "invalid_source_response"
                    else:
                        # Persist the server boundary before the success checkpoint.
                        # A crash must not let another queued resource bypass it.
                        if retry_at is not None:
                            store.set_next_request_at(max(store.next_request_at(), retry_at))
                        store.succeed(attempt, parsed, digest, clock(), refresh_job_id=refresh_job_id)
                        if refresh_job_id is not None:
                            allowed_resources = store.refresh_resources(refresh_job_id)
                        elif allowed_resources is not None:
                            allowed_resources.update(("division", ident) for ident in parsed.discoveries)
                        result["successes"] += 1
                        if progress:
                            progress({"kind": kind, "key": key, "outcome": "success", "status": status})
                        continue
                elif status in (401, 403):
                    outcome, error = "blocked", "authentication_refused"
                elif status in (404, 410):
                    outcome, error = "missing", "source_not_found"
                elif status in (408, 425, 429) or status >= 500:
                    outcome, error = "failed", "http_" + str(status)
                    if retry_at is None:
                        retry_at = clock() + min(3600, 30 * 2 ** min(prior_failures, 7))
                else:
                    outcome, error = "invalid", "http_" + str(status)
        if retry_at is not None:
            # Server Retry-After and transient-error backoff apply to the whole client.
            # Write this before finalizing the attempt, so interruption is conservative.
            store.set_next_request_at(max(store.next_request_at(), retry_at))
        store.fail(attempt, outcome, status, error, digest, retry_at, clock())
        result["failures"] += 1
        if progress:
            progress({"kind": kind, "key": key, "outcome": outcome, "status": status, "error": error})
        if outcome == "blocked" or status == 429 or (retry_at is not None and retry_at > clock()):
            result["reason"] = "blocked" if outcome == "blocked" else "deferred"
            break
    else:
        result["reason"] = "request_budget"
    result["resource_states"] = {
        row[0]: row[1] for row in store.conn.execute("SELECT status, COUNT(*) FROM resources GROUP BY status")
    }
    result["next_request_at"] = store.next_request_at()
    result["archive_unfinished"] = sum(v for k, v in result["resource_states"].items() if k != "success")
    result["unfinished"] = sum(
        1 for row in store.conn.execute("SELECT kind,key,status FROM resources")
        if row[2] != "success" and (row[0], row[1]) not in deferred
        and (allowed_resources is None or (row[0], row[1]) in allowed_resources)
    )
    if not result["unfinished"]:
        result["reason"] = "drained"
    if result["unfinished"] and result["reason"] == "drained":
        now = clock()
        waiting = any(
            max(result["next_request_at"], row["next_attempt_at"] or 0) > now
            for row in store.conn.execute("SELECT kind,key,next_attempt_at FROM resources WHERE status IN ('pending','failed')")
            if (row["kind"], row["key"]) not in deferred
            and (allowed_resources is None or (row["kind"], row["key"]) in allowed_resources)
        )
        result["reason"] = "deferred" if waiting else "unresolved_resources"
    if refresh_job_id is not None:
        result["refresh_job"] = store.finish_refresh(refresh_job_id, clock())
    return result
