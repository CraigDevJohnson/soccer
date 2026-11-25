"""Validate routine archive changes and identify the plan displayed for approval."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time


MONITORING = {"aws_cloudwatch_metric_alarm", "aws_cloudwatch_log_metric_filter"}
UPDATE_ONLY = {
    "aws_cloudwatch_log_group", "aws_lambda_function", "aws_lambda_function_event_invoke_config",
    "aws_s3_bucket_lifecycle_configuration", "aws_scheduler_schedule",
}


def grants_archive_expiration(rule):
    if rule.get("expiration") or rule.get("transition") or rule.get("noncurrent_version_transition"):
        return True
    if rule.get("noncurrent_version_expiration"):
        filters = rule.get("filter", [])
        return len(filters) != 1 or filters[0].get("prefix") != "control/"
    return False


def unknown_value(value):
    if isinstance(value, dict):
        return any(unknown_value(v) for v in value.values())
    if isinstance(value, list):
        return any(unknown_value(v) for v in value)
    return value is True


def reviewed_digest(plan):
    if plan.get("errored") or plan.get("deferred_changes"):
        raise ValueError("the archive plan has errors or deferred changes")
    changes = []
    for resource in plan.get("resource_changes", []):
        change = resource["change"]
        actions = change["actions"]
        if resource.get("previous_address") or change.get("importing") is not None:
            raise ValueError(f"{resource['address']}: state moves and imports require separate approval")
        if actions == ["no-op"] or (resource["mode"] == "data" and actions == ["read"]):
            continue
        kind, address = resource["type"], resource["address"]
        allowed = ((kind in MONITORING and actions in (["update"], ["create"]))
                   or (kind in UPDATE_ONLY and actions == ["update"]))
        if not allowed:
            raise ValueError(f"{address}: {actions} needs a separately approved infrastructure plan")
        before, after = change["before"], change["after"]
        unknown = change.get("after_unknown", {})
        if kind == "aws_lambda_function":
            if unknown.get("role") or before["role"] != after["role"]:
                raise ValueError("worker role changes require a separately approved plan")
            if after.get("reserved_concurrent_executions") != 1:
                raise ValueError("archive collection requires exactly one worker")
        if kind == "aws_cloudwatch_metric_alarm":
            topic = "arn:aws:sns:us-west-2:" + plan["variables"]["account_id"]["value"] + ":alerts"
            for field in ("alarm_actions", "ok_actions", "insufficient_data_actions"):
                if unknown_value(unknown.get(field)) or set(after.get(field) or []) - {topic}:
                    raise ValueError("archive alarms may notify only the workloads alerts topic")
        if kind == "aws_scheduler_schedule" and (unknown.get("state") or before["state"] != after["state"]):
            raise ValueError("the activation workflow owns the scheduler state")
        if kind == "aws_s3_bucket_lifecycle_configuration":
            if (address != "aws_s3_bucket_lifecycle_configuration.archive" or unknown_value(unknown.get("rule"))
                    or any(grants_archive_expiration(rule) for rule in after["rule"])):
                raise ValueError("archive lifecycle may expire only superseded control versions")
        changes.append((address, change))
    outputs = {k: v for k, v in plan.get("output_changes", {}).items() if v["actions"] != ["no-op"]}
    # Replanning on the protected runner keeps the binary state-bearing plan off public artifacts.
    # Compare every before/after value and unknown marker, independent of ordering of resources.
    content = json.dumps([sorted(changes), outputs], sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(content).hexdigest()


def changing_image(plan):
    return any(r["type"] == "aws_lambda_function" and r["change"]["actions"] == ["update"]
               and r["change"]["before"].get("image_uri") != r["change"]["after"].get("image_uri")
               for r in plan.get("resource_changes", []))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--check-release", nargs=2, metavar=("ENV", "ACCOUNT"))
    args = parser.parse_args()
    plan = json.loads(args.plan.read_bytes())
    fingerprint = reviewed_digest(plan)
    if args.check_release and changing_image(plan):
        from scripts.boise_collect.activation import read_peer_object, require_idle_peer
        from scripts.boise_collect.transfer import S3

        store = S3(*args.check_release)
        with tempfile.TemporaryDirectory(prefix="archive-release-check-") as folder:
            require_idle_peer(lambda key: read_peer_object(store, key, folder), time.time())
    print(fingerprint)


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, RuntimeError, ValueError) as error:
        sys.exit(str(error))
