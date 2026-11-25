"""Select a hosted collection mode and read back its effective settings."""

import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from .transfer import S3


KEY = "settings/collection.json"
MODES = ("off", "manual", "daily")
SCHEDULE = Path(__file__).resolve().parents[2] / "infra/archive/daily.json"


class CollectionMode:
    def __init__(self, store, directory):
        self.store = store
        self.path = Path(directory) / "collection.json"

    def initialize(self):
        # Create-only lets us initialize an absent switch without granting ListBucket.
        self.path.write_text('{"mode":"off"}')
        self.store.create(KEY, self.path)

    def read(self):
        head = self.store.call("head-object", KEY)
        if not 0 < head["ContentLength"] <= 256:
            raise ValueError("invalid stored collection mode")
        self.store.call("get-object", KEY, "--if-match", head["ETag"], str(self.path))
        value = json.loads(self.path.read_bytes())
        if not isinstance(value, dict) or set(value) != {"mode"} or value["mode"] not in MODES:
            raise ValueError("invalid stored collection mode")
        return value["mode"], head["ETag"]

    def write(self, mode, previous):
        self.path.write_text(json.dumps({"mode": mode}, separators=(",", ":")))
        self.store.call("put-object", KEY, "--if-match", previous, "--body", str(self.path))


def read_peer_object(store, key, directory):
    result = subprocess.run(
        ["aws", "s3api", "list-objects-v2", "--bucket", store.bucket, "--prefix", key,
         "--max-keys", "1", "--no-paginate", "--expected-bucket-owner", store.account,
         "--region", "us-west-2", "--output", "json", "--no-cli-pager"],
        capture_output=True,
    )
    if result.returncode:
        if b"(NoSuchBucket)" in result.stderr:
            return None
        raise RuntimeError("could not verify the other archive environment")
    listing = json.loads(result.stdout)
    objects = listing.get("Contents", [])
    if (listing.get("Name") != store.bucket or listing.get("Prefix") != key
            or listing.get("MaxKeys") != 1 or listing.get("KeyCount") != len(objects)
            or len(objects) > 1):
        raise RuntimeError("could not verify the other archive environment's key listing")
    if not any(item["Key"] == key for item in objects):
        return None
    path = Path(directory) / "peer.json"
    store.download(key, path, 1024**2)
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("invalid peer archive state")
    return value


def require_idle_peer(read, now):
    mode = read(KEY)
    if mode is not None and mode != {"mode": "off"}:
        raise ValueError("turn off collection in the other environment first")
    worker = read("control/worker.json")
    if worker is None:
        return
    if (type(worker.get("version")) is not int or worker["version"] != 1
            or worker.get("status") not in ("idle", "completed", "completed_with_gaps")
            or worker.get("pending") is not None or worker.get("queued_daily", []) != []):
        raise ValueError("the other archive worker has unfinished work or an unknown state")
    # The seed idle checkpoint predates its first request. A finished job must record pacing.
    until = worker.get("next_request_at", 0 if worker["status"] == "idle" else None)
    if type(until) not in (float, int) or not math.isfinite(until) or until < 0 or until > now:
        raise ValueError("the other archive worker's provider backoff is active or unknown")


def daily_schedule(account, enabled):
    if not re.fullmatch(r"[0-9]{12}", account):
        raise ValueError("expected the workloads account")
    config = json.loads(SCHEDULE.read_text())
    config.update(GroupName="soccer-archive-prod", State="ENABLED" if enabled else "DISABLED")
    config["Target"].update(
        Arn=f"arn:aws:lambda:us-west-2:{account}:function:soccer-archive-prod-worker",
        RoleArn=f"arn:aws:iam::{account}:role/soccer-archive-prod-scheduler",
    )
    return config


def scheduler_call(operation, *arguments):
    result = subprocess.run(
        ["aws", "scheduler", operation, "--region", "us-west-2", "--output", "json",
         "--no-cli-pager", *arguments], capture_output=True,
    )
    if result.returncode:
        error = re.search(rb"\(([A-Za-z0-9_.]+)\)", result.stderr)
        category = error[1].decode() if error else "CLI error"
        raise RuntimeError(f"Scheduler {operation} failed ({category})")
    return json.loads(result.stdout or b"{}")


def configure_schedule(expected, call=scheduler_call, sleep=time.sleep):
    def matches():
        received = call("get-schedule", "--group-name", expected["GroupName"], "--name", expected["Name"])
        for key in ("Arn", "CreationDate", "LastModificationDate"):
            received.pop(key, None)
        return expected == received

    if matches():
        return
    # UpdateSchedule replaces omitted properties with defaults. Send the complete IaC definition.
    call("update-schedule", "--cli-input-json", json.dumps(expected))
    for delay in (0, 1, 2, 4, 8):
        if delay:
            sleep(delay)
        if matches():
            return
    raise RuntimeError("Scheduler readback differed from the approved schedule")


def activate(environment, account, mode, settings, check_peer, update_schedule=configure_schedule):
    if environment not in {"dev", "prod"} or mode not in MODES or (environment == "dev" and mode == "daily"):
        raise ValueError("select off or manual in dev, or off, manual, or daily in prod")
    if mode != "off":
        check_peer()
    settings.initialize()
    current, version = settings.read()
    if mode == "daily":
        # Verify the physical schedule before letting a scheduled event contact the league.
        update_schedule(daily_schedule(account, True))
    if current != mode:
        settings.write(mode, version)
    if environment == "prod" and mode != "daily":
        # Closing the data-plane gate first keeps a failed schedule update safe to retry.
        update_schedule(daily_schedule(account, False))
    if settings.read()[0] != mode:
        raise RuntimeError("collection mode readback changed concurrently")
    return {"environment": environment, "mode": mode, "daily_schedule_enabled": mode == "daily"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--environment", choices=["dev", "prod"], required=True)
    parser.add_argument("--mode", choices=MODES, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    store = S3(args.environment, args.account)
    with tempfile.TemporaryDirectory(prefix="soccer-activation-") as directory:
        peer = S3("prod" if args.environment == "dev" else "dev", args.account)

        def check_peer():
            require_idle_peer(lambda key: read_peer_object(peer, key, directory), time.time())

        result = activate(args.environment, args.account, args.mode, CollectionMode(store, directory), check_peer)
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, RuntimeError, ValueError) as error:
        sys.exit(str(error))
