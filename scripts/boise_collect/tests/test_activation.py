import copy
import json
from pathlib import Path
import tempfile
import unittest
from subprocess import CompletedProcess
from unittest.mock import patch

from scripts.boise_collect.activation import (
    KEY, CollectionMode, activate, configure_schedule, daily_schedule, read_peer_object, require_idle_peer,
)
from scripts.boise_collect.transfer import S3


ACCOUNT = "111122223333"


class Objects:
    def __init__(self):
        self.data = None
        self.version = 0
        self.conflict = False

    def create(self, key, path):
        assert key == KEY
        if self.data is None:
            self.data = path.read_bytes()
            self.version = 1

    def call(self, operation, key, *args):
        etag = f'"{self.version}"'
        assert key == KEY
        if operation == "head-object":
            return {"ContentLength": len(self.data), "ETag": etag}
        if operation == "get-object":
            assert args[:2] == ("--if-match", etag)
            Path(args[-1]).write_bytes(self.data)
            return {}
        assert operation == "put-object" and args[0] == "--if-match" and args[2] == "--body"
        if self.conflict or args[1] != etag:
            raise RuntimeError("PreconditionFailed")
        self.data = Path(args[3]).read_bytes()
        self.version += 1
        return {}


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.objects = Objects()
        self.mode = CollectionMode(self.objects, self.temp.name)
        self.changes = []
        self.schedule = daily_schedule(ACCOUNT, False)

    def scheduler(self, expected):
        def api(operation, *args):
            if operation == "get-schedule":
                self.assertEqual(args, ("--group-name", "soccer-archive-prod", "--name", "refresh"))
                return copy.deepcopy(self.schedule) | {"Arn": "schedule-arn", "CreationDate": "2026-11-01"}
            self.assertEqual(operation, "update-schedule")
            self.assertEqual(args[0], "--cli-input-json")
            self.changes.append((self.mode.read()[0], json.loads(args[1])["State"]))
            self.schedule = json.loads(args[1])
            return {}
        configure_schedule(expected, call=api, sleep=lambda _: None)

    def test_daily_sets_complete_schedule_then_admits_network_requests(self):
        result = activate("prod", ACCOUNT, "daily", self.mode, lambda: None, self.scheduler)
        self.assertEqual(result, {"environment": "prod", "mode": "daily", "daily_schedule_enabled": True})
        self.assertEqual(self.changes, [("off", "ENABLED")])
        self.assertEqual(self.schedule["ScheduleExpressionTimezone"], "America/Boise")
        self.assertEqual(self.schedule["ScheduleExpression"], "cron(0 4 * * ? *)")
        self.assertEqual(self.schedule["FlexibleTimeWindow"], {"Mode": "OFF"})
        self.assertEqual(self.schedule["Target"], {
            "Arn": f"arn:aws:lambda:us-west-2:{ACCOUNT}:function:soccer-archive-prod-worker",
            "RoleArn": f"arn:aws:iam::{ACCOUNT}:role/soccer-archive-prod-scheduler",
            "RetryPolicy": {"MaximumRetryAttempts": 1, "MaximumEventAgeInSeconds": 3600},
            "Input": '{"operation":"scheduled","scheduled_time":"<aws.scheduler.scheduled-time>"}',
        })
        self.assertEqual(self.mode.read()[0], "daily")
        activate("prod", ACCOUNT, "daily", self.mode, lambda: None, self.scheduler)
        self.assertEqual(len(self.changes), 1)

    def test_stop_closes_network_gate_before_disabling_schedule(self):
        activate("prod", ACCOUNT, "daily", self.mode, lambda: None, self.scheduler)
        activate("prod", ACCOUNT, "off", self.mode, lambda: None, self.scheduler)
        self.assertEqual(self.changes[-1], ("off", "DISABLED"))
        self.assertEqual(self.mode.read()[0], "off")

    def test_dev_allows_manual_and_has_no_scheduler_access(self):
        self.assertRaises(ValueError, activate, "dev", ACCOUNT, "daily", self.mode, lambda: None, self.fail)
        self.assertIsNone(self.objects.data)
        activate("dev", ACCOUNT, "manual", self.mode, lambda: None, self.fail)
        self.assertEqual(self.mode.read()[0], "manual")

    def test_failed_schedule_update_or_readback_keeps_gate_closed(self):
        def fail(schedule):
            raise RuntimeError("update failed")

        self.assertRaises(RuntimeError, activate, "prod", ACCOUNT, "daily", self.mode, lambda: None, fail)
        self.assertEqual(self.mode.read()[0], "off")
        activate("prod", ACCOUNT, "daily", self.mode, lambda: None, self.scheduler)
        self.assertRaises(RuntimeError, activate, "prod", ACCOUNT, "off", self.mode, lambda: None, fail)
        self.assertEqual(self.mode.read()[0], "off")

        def stale_response(operation, *args):
            return {"Name": "wrong"} if operation == "get-schedule" else {}
        with self.assertRaisesRegex(RuntimeError, "readback differed"):
            configure_schedule(daily_schedule(ACCOUNT, True), call=stale_response, sleep=lambda _: None)

    def test_conflict_never_overwrites_another_mode(self):
        self.objects.conflict = True
        with self.assertRaisesRegex(RuntimeError, "PreconditionFailed"):
            activate("dev", ACCOUNT, "manual", self.mode, lambda: None, self.fail)
        self.assertEqual(self.mode.read()[0], "off")

    def test_malformed_settings_fail_closed(self):
        self.mode.initialize()
        self.objects.data = b'{"mode":"sometimes"}'
        with self.assertRaisesRegex(ValueError, "invalid stored"):
            activate("prod", ACCOUNT, "daily", self.mode, lambda: None, self.fail)

    def test_only_stopped_peer_with_elapsed_backoff_may_yield_the_writer(self):
        for mode, worker in (
            (None, None),
            ({"mode": "off"}, {"version": 1, "status": "idle"}),
            ({"mode": "off"}, {"version": 1, "status": "completed_with_gaps", "pending": None, "next_request_at": 10}),
        ):
            require_idle_peer({KEY: mode, "control/worker.json": worker}.get, 10)
        completed = {"version": 1, "status": "completed", "next_request_at": 10}
        for mode, worker in (
            ({"mode": "manual"}, None), ({"mode": "daily"}, None), ({}, None),
            (None, completed | {"pending": {"number": 1}}), (None, completed | {"status": "waiting"}),
            (None, completed | {"next_request_at": 11}), (None, completed | {"next_request_at": None}),
            (None, completed | {"next_request_at": float("nan")}), (None, completed | {"version": 2}),
        ):
            with self.subTest(mode=mode, worker=worker), self.assertRaises(ValueError):
                require_idle_peer({KEY: mode, "control/worker.json": worker}.get, 10)

    def test_peer_check_precedes_activation_and_never_blocks_a_stop(self):
        def denied():
            raise RuntimeError("peer unknown")
        self.assertRaises(RuntimeError, activate, "prod", ACCOUNT, "daily", self.mode, denied, self.fail)
        self.assertIsNone(self.objects.data)
        activate("dev", ACCOUNT, "off", self.mode, denied, self.fail)
        self.assertEqual(self.mode.read()[0], "off")

    def test_missing_bucket_and_exact_key_differ_from_access_denied(self):
        peer = S3("prod", ACCOUNT)
        for response in (
            CompletedProcess([], 254, b"", b"An error occurred (NoSuchBucket)"),
            CompletedProcess([], 0, json.dumps({"Name": peer.bucket, "Prefix": KEY, "MaxKeys": 1, "KeyCount": 1,
                                                "Contents": [{"Key": KEY + ".old"}]}).encode(), b""),
        ):
            with patch("scripts.boise_collect.activation.subprocess.run", return_value=response) as cli:
                self.assertIsNone(read_peer_object(peer, KEY, self.temp.name))
                argv = cli.call_args.args[0]
                self.assertEqual(argv[argv.index("--prefix") + 1], KEY)
                self.assertEqual(argv[argv.index("--max-keys") + 1], "1")
        with patch("scripts.boise_collect.activation.subprocess.run",
                   return_value=CompletedProcess([], 254, b"", b"An error occurred (AccessDenied)")):
            with self.assertRaisesRegex(RuntimeError, "could not verify"):
                read_peer_object(peer, KEY, self.temp.name)


if __name__ == "__main__":
    unittest.main()
