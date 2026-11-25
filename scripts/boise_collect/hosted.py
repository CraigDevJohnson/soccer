"""Run one bounded Boise refresh using an S3 checkpoint and immutable request journal."""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
from datetime import date, datetime, timedelta
import json
import os
import re
import shutil
from pathlib import Path
import time

from .bruno_client import BrunoClient
from .collector import BOISE, ORIGIN, collect, read_origin
from .hosted_storage import Conflict, Generations, S3Objects
from .model import Parsed
from .store import Store


ACTIVE = "control/active.json"
WORKER = "control/worker.json"
REQUEST_TIMEOUT = 30
# Bruno grants the child process 15 seconds beyond its HTTP timeout.
REQUEST_RESERVE = REQUEST_TIMEOUT + 15 + 5
PUBLISH_RESERVE = 180
TERMINAL = {"completed", "completed_with_gaps"}


def scope(event):
    if not isinstance(event, dict):
        raise ValueError("expected a named archive operation")
    operation = event.get("operation")
    if operation in ("status", "resume") and set(event) == {"operation"}:
        return operation, {}
    if operation == "resume" and set(event) == {"operation", "job", "retry_blocked_attempt"}:
        if (not isinstance(event["job"], str) or not re.fullmatch(r"refresh-\d{4}-\d{2}-\d{2}", event["job"])
                or type(event["retry_blocked_attempt"]) is not int or event["retry_blocked_attempt"] < 1):
            raise ValueError("invalid blocked attempt reference")
        return operation, event
    if operation == "resume" and set(event) == {"operation", "job", "request_id", "additional_budget"}:
        if not isinstance(event["request_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", event["request_id"]):
            raise ValueError("invalid budget extension ID")
        if not isinstance(event["job"], str) or not re.fullmatch(r"refresh-\d{4}-\d{2}-\d{2}", event["job"]):
            raise ValueError("invalid budget extension job")
        if type(event["additional_budget"]) is not int or not 1 <= event["additional_budget"] <= 100:
            raise ValueError("invalid budget extension amount")
        return operation, event
    if operation == "scheduled" and set(event) == {"operation", "scheduled_time"}:
        stamp = datetime.fromisoformat(event["scheduled_time"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("scheduled time must include its UTC offset")
        return operation, {"through": stamp.astimezone(BOISE).date().isoformat(), "days": 14, "budget": 100}
    if operation == "refresh" and set(event) == {"operation", "through", "days", "budget"}:
        end = date.fromisoformat(event["through"])
        if end.isoformat() != event["through"] or end.year < 2010:
            raise ValueError("invalid refresh date")
        if type(event["days"]) is not int or not 1 <= event["days"] <= 28:
            raise ValueError("refresh must cover 1 through 28 days")
        if type(event["budget"]) is not int or not 1 <= event["budget"] <= 100:
            raise ValueError("initial request budget must be 1 through 100")
        return operation, {k: event[k] for k in ("through", "days", "budget")}
    raise ValueError("invalid archive operation or fields")


def public_status(state):
    return {key: state[key] for key in
            ("status", "job", "through", "days", "budget", "spent", "next_request_at", "gaps", "generation")
            if key in state}


class Checkpoint:
    def __init__(self, objects):
        self.objects = objects
        try:
            self.value = {"version": 1, "status": "idle"}
            self.etag = objects.write(WORKER, self.value)
        except Conflict:
            self.value, self.etag = objects.read(WORKER)
        if self.value.get("version") != 1:
            raise ValueError("unknown archive worker checkpoint format")

    def save(self, **changes):
        updated = {**self.value, **changes}
        self.etag = self.objects.write(WORKER, updated, self.etag)
        previous_status = self.value["status"]
        self.value = updated
        if updated["status"] in ("paused_budget", "blocked", "interrupted") and updated["status"] != previous_status:
            print(json.dumps({"event": "archive_attention", "reason": updated["status"]}), flush=True)

    def status(self):
        return public_status(self.value)

    def journal_key(self, number):
        return f"journal/{self.value['job']}/{number:06d}.json"

    def complete_attempt(self, outcome):
        value = self.value
        event = {**value["pending"], **outcome}
        key = self.journal_key(value["spent"])
        try:
            self.objects.write(key, event)
        except Conflict:
            existing, _ = self.objects.read(key)
            if existing != event:
                raise Conflict("request already has a different journal outcome")
        self.save(pending=None, next_request_at=event["next_request_at"],
                  status="blocked" if event["outcome"] == "blocked" else "running")

    def recover(self, now):
        pending = self.value["pending"]
        if not pending:
            return True
        # The worker's reserved concurrency is one. Leave additional time for
        # the outstanding subprocess and minimum pacing before declaring a loss.
        if now < pending["expires"]:
            return False
        key = self.journal_key(self.value["spent"])
        interrupted = {**pending, "outcome": "interrupted", "finished": now, "next_request_at": now + 1}
        try:
            self.objects.write(key, interrupted)
            event = interrupted
        except Conflict:
            event, _ = self.objects.read(key)
        if any(event[k] != pending[k] for k in pending):
            raise ValueError("journal does not match the reserved attempt")
        # A lost reply could have carried a provider backoff. An operator has to
        # resume this job explicitly after checking an uncertain request.
        status = event["outcome"] if event["outcome"] in ("interrupted", "blocked") else "running"
        self.save(pending=None, status=status, next_request_at=event["next_request_at"])
        return True


class YieldWorker(Exception):
    pass


class CollectionDisabled(Exception):
    pass


def enabled(mode, operation):
    return mode == "daily" or (mode == "manual" and operation != "scheduled")


class HostedStore(Store):
    """Record accepted outcomes in S3 before changing the disposable SQLite copy."""

    def attach(self, checkpoint, remaining, operation):
        self.checkpoint = checkpoint
        self.remaining = remaining
        self.operation = operation

    def start_attempt(self, kind, key, now):
        state = self.checkpoint.value
        if state["spent"] >= state["budget"] or self.remaining() < PUBLISH_RESERVE + REQUEST_RESERVE:
            raise YieldWorker
        if not enabled(self.checkpoint.objects.mode(), self.operation):
            raise CollectionDisabled
        pending = {"kind": kind, "key": key, "number": state["spent"] + 1, "started": now,
                   "expires": now + REQUEST_RESERVE}
        self.checkpoint.save(spent=pending["number"], pending=pending, status="running")
        return super().start_attempt(kind, key, now)

    def succeed(self, attempt_id, parsed, body_sha256, now, refresh_job_id=None):
        fields = asdict(parsed)
        fields["discoveries"] = sorted(parsed.discoveries)
        self.checkpoint.complete_attempt({"outcome": "success", "parsed": fields,
                                         "digest": body_sha256, "finished": now,
                                         "next_request_at": self.next_request_at()})
        return super().succeed(attempt_id, parsed, body_sha256, now, refresh_job_id=refresh_job_id)

    def fail(self, attempt_id, outcome, http_status, error_code, body_sha256, retry_at, now):
        self.checkpoint.complete_attempt({"outcome": outcome, "http_status": http_status,
                                         "error": error_code, "digest": body_sha256,
                                         "retry_at": retry_at, "finished": now,
                                         "next_request_at": self.next_request_at()})
        super().fail(attempt_id, outcome, http_status, error_code, body_sha256, retry_at, now)


def sqlite_job(state):
    # Previous checkpoints used the public identifier inside SQLite.
    return state.get("sqlite_job", state["job"])


def replay(store, checkpoint):
    job = checkpoint.value
    end = date.fromisoformat(job["through"])
    store.begin_refresh(sqlite_job(job), end - timedelta(days=job["days"] - 1), end, True, job["created"])
    for number in range(1, job["spent"] + 1):
        event, _ = checkpoint.objects.read(checkpoint.journal_key(number))
        if event["number"] != number:
            raise ValueError("missing or unordered archive journal")
        attempt = Store.start_attempt(store, event["kind"], event["key"], event["started"])
        outcome = event["outcome"]
        if outcome == "success":
            parsed = Parsed(**event["parsed"])
            parsed.discoveries = set(parsed.discoveries)
            Store.succeed(store, attempt, parsed, event["digest"], event["finished"], refresh_job_id=sqlite_job(job))
        elif outcome == "interrupted":
            store.recover_interrupted(event["finished"])
        else:
            Store.fail(store, attempt, outcome, event["http_status"], event["error"], event["digest"],
                       event["retry_at"], event["finished"])
        if outcome == "blocked" and number in job["released_blocks"]:
            store.requeue(event["kind"], event["key"])
        store.set_next_request_at(event["next_request_at"])


def close_gaps(store, job, now):
    # A hosted run reports source-invalid and missing records as gaps. Keep
    # their failed attempts and the prior successful snapshots intact. An
    # authentication block pauses collection across jobs for operator review.
    with store.conn:
        store.conn.execute("""UPDATE refresh_job_resources AS j SET status='skipped',
              skip_reason=(SELECT status FROM resources r WHERE (r.kind,r.key)=(j.kind,j.key))
            WHERE job_id=? AND status='pending' AND EXISTS (
              SELECT 1 FROM resources r WHERE (r.kind,r.key)=(j.kind,j.key)
              AND r.status IN ('invalid','missing'))""", (job,))
    return store.finish_refresh(job, now)


def finish_publication(checkpoint, pointer, etag):
    state = checkpoint.value
    if pointer["generation"] != state["generation"]:
        if pointer["generation"] != state["base"] or etag != state["base_etag"]:
            raise Conflict("active generation changed during collection")
        checkpoint.objects.write(ACTIVE, {"generation": state["generation"]}, etag)
    checkpoint.save(status="completed_with_gaps" if state["gaps"] else "completed")
    return checkpoint.status()


def publish_job(checkpoint, generations, pointer, etag, remaining):
    state = checkpoint.value
    if remaining() < PUBLISH_RESERVE:
        return checkpoint.status()
    # Replay from the immutable baseline even on the first publish. The local
    # collection path has extra pacing and job-completion commits. Building
    # one canonical SQLite copy fixes page layout and timestamps on retries.
    dataset = generations.restore(state["base"])
    with closing(Store(dataset, read_origin(dataset))) as store:
        replay(store, checkpoint)
        summary = close_gaps(store, sqlite_job(state), state["completed_at"])
        if summary["pending"] or summary["skipped"] != state["gaps"]:
            raise ValueError("replayed job summary differs from its durable checkpoint")
    generation = generations.publish(dataset, state["completed_at"])
    checkpoint.save(status="publishing", generation=generation)
    return finish_publication(checkpoint, pointer, etag)


def run(event, objects, generations, client, remaining, clock=time.time, sleep=time.sleep):
    operation, request = scope(event)
    if operation == "status":
        return public_status(objects.status(WORKER))
    if not enabled(objects.mode(), operation):
        return {"status": "collection_disabled"}
    if (operation in ("refresh", "scheduled")
            and date.fromisoformat(request["through"]) > datetime.fromtimestamp(clock(), BOISE).date()):
        raise ValueError("refresh through date is in the future")
    checkpoint = Checkpoint(objects)
    state = checkpoint.value
    pointer, etag = objects.read(ACTIVE)
    if not isinstance(pointer.get("generation"), str) or not re.fullmatch(r"[0-9a-f]{64}", pointer["generation"]):
        raise ValueError("invalid active archive generation")
    if state["status"] == "publishing":
        return finish_publication(checkpoint, pointer, etag)
    if operation == "resume" and state["status"] in TERMINAL | {"idle"}:
        return checkpoint.status()
    dataset = None
    if operation in ("refresh", "scheduled"):
        through = request["through"]
        if state.get("through", "") > through:
            return checkpoint.status()
        if through == state.get("through"):
            if operation == "refresh" and (request["days"], request["budget"]) != (state["days"], state["initial_budget"]):
                raise ValueError("date already has a job with different parameters")
            if state["status"] in TERMINAL:
                return checkpoint.status()
        elif state["status"] in TERMINAL | {"idle"}:
            if remaining() < PUBLISH_RESERVE + REQUEST_RESERVE:
                return {"status": "deferred", "spent": 0}
            dataset = generations.restore(pointer["generation"])
            job = f"refresh-{through}"
            internal_job = "aws-" + job
            origin = read_origin(dataset)
            if origin != client.base_url:
                raise ValueError("archive source and worker origin differ")
            with closing(Store(dataset, origin)) as source:
                if source.active_refresh_job_id():
                    raise ValueError("resolve the unfinished source refresh before importing it for collection")
                if source.conn.execute("SELECT 1 FROM refresh_jobs WHERE id=?", (internal_job,)).fetchone():
                    raise ValueError("imported generation already contains this hosted job")
                # Exercise work selection against the restored database before
                # admitting a job whose source cannot support it.
                end = date.fromisoformat(through)
                source.begin_refresh(internal_job, end - timedelta(days=request["days"] - 1), end, True, clock())
            checkpoint.save(status="running", job=job, sqlite_job=internal_job, **request, initial_budget=request["budget"],
                            base=pointer["generation"], base_etag=etag, spent=0, pending=None, extensions={}, released_blocks=[],
                            next_request_at=state.get("next_request_at", 0), created=clock(), gaps=0,
                            generation=None, completed_at=None)
        elif operation == "refresh":
            raise ValueError("a previous refresh is unfinished; resume that job")
    state = checkpoint.value
    if pointer["generation"] != state["base"] or etag != state["base_etag"]:
        raise Conflict("active archive changed; reconcile the pending job before continuing")
    if state["status"] == "finalizing":
        return publish_job(checkpoint, generations, pointer, etag, remaining)
    if request.get("additional_budget"):
        if request["job"] != state["job"]:
            raise ValueError("budget extension refers to a different job")
        previous = state["extensions"].get(request["request_id"])
        if previous is not None and previous != request["additional_budget"]:
            raise ValueError("budget extension ID reused with different amount")
        if previous is None:
            if state["status"] != "paused_budget":
                raise ValueError("only a budget-paused job can receive additional requests")
            checkpoint.save(budget=state["budget"] + request["additional_budget"],
                            extensions={**state["extensions"], request["request_id"]: request["additional_budget"]})
    had_pending = checkpoint.value["pending"] is not None
    if not checkpoint.recover(clock()):
        return checkpoint.status()
    state = checkpoint.value
    if state["status"] == "paused_budget" and state["spent"] >= state["budget"]:
        return checkpoint.status()
    if state["status"] == "blocked":
        if not request.get("retry_blocked_attempt"):
            return checkpoint.status()
        if request["job"] != state["job"] or request["retry_blocked_attempt"] != state["spent"]:
            raise ValueError("manual retry must identify the current blocked attempt")
        checkpoint.save(status="running", released_blocks=[*state["released_blocks"], state["spent"]])
    elif request.get("retry_blocked_attempt") and (
            request["job"] != state["job"] or request["retry_blocked_attempt"] not in state["released_blocks"]):
        raise ValueError("the job does not have this blocked attempt")
    if state["status"] == "interrupted" and (operation != "resume" or had_pending):
        return checkpoint.status()
    if state["next_request_at"] > clock() + 2 or remaining() < PUBLISH_RESERVE + REQUEST_RESERVE:
        return checkpoint.status()
    state = checkpoint.value
    if dataset is None:
        dataset = generations.restore(state["base"])
    origin = read_origin(dataset)
    if origin != client.base_url:
        raise ValueError("archive source and worker origin differ")
    with closing(HostedStore(dataset, origin)) as store:
        replay(store, checkpoint)
        store.set_next_request_at(max(store.next_request_at(), state["next_request_at"]))
        store.attach(checkpoint, remaining, operation)
        available = state["budget"] - state["spent"]
        reason = None
        if available:
            try:
                result = collect(store, client, max_requests=available, refresh_job_id=sqlite_job(state),
                                 clock=clock, sleep=sleep)
                reason = result["reason"]
            except YieldWorker:
                reason = "deadline"
            except CollectionDisabled:
                return {**checkpoint.status(), "status": "collection_disabled"}
        summary = close_gaps(store, sqlite_job(state), clock())
        gaps = summary["skipped"]
        if summary["pending"]:
            if reason == "blocked":
                status = "blocked"
            elif checkpoint.value["spent"] >= checkpoint.value["budget"]:
                status = "paused_budget"
            elif reason == "deferred":
                status = "waiting"
            else:
                status = "running"
            checkpoint.save(status=status, gaps=gaps, next_request_at=store.next_request_at())
            return checkpoint.status()
    checkpoint.save(status="finalizing", completed_at=clock(), gaps=gaps)
    return publish_job(checkpoint, generations, pointer, etag, remaining)


def lambda_handler(event, context):
    import boto3
    from botocore.config import Config

    operation, _ = scope(event)
    s3 = boto3.client("s3", config=Config(connect_timeout=3, read_timeout=30, retries={"total_max_attempts": 3}))
    objects = S3Objects(s3, os.environ["ARCHIVE_BUCKET"])
    if operation == "status":
        return public_status(objects.status(WORKER))
    # Lambda may preserve /tmp after killing a timed-out process. This path is
    # exclusively owned by this function and its concurrency is fixed at one.
    folder = Path("/tmp/boise-archive-worker")
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(mode=0o700)
    try:
        return run(event, objects, Generations(objects, folder, os.environ["ARCHIVE_SOURCE_REVISION"]),
                   BrunoClient(ORIGIN, REQUEST_TIMEOUT), lambda: context.get_remaining_time_in_millis() / 1000)
    finally:
        shutil.rmtree(folder)
