"""Exercise the installed Bruno transport through the public collector CLI."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from email.utils import format_datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from scripts.boise_collect.collector import MAX_BODY, request_path
from scripts.boise_collect.tests.test_collector import (
    DAY, FixtureServer, day_body, division_body,
)


REPOSITORY = Path(__file__).resolve().parents[3]
NEXT_DAY = "2010-09-27"
PRIVATE_MARKER = "DO-NOT-STORE-ME"


class BrunoIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "archive"

    def tearDown(self):
        self.temporary.cleanup()

    def cli(self, command, *args, expected):
        environment = dict(
            os.environ,
            HTTP_PROXY="http://unusable.invalid:9",
            HTTPS_PROXY="http://unusable.invalid:9",
            ALL_PROXY="http://unusable.invalid:9",
            NO_PROXY="",
        )
        completed = subprocess.run(
            [sys.executable, "-m", "scripts.boise_collect", command,
             "--data-dir", str(self.root), *args],
            cwd=REPOSITORY, env=environment, capture_output=True,
            text=True, timeout=45,
        )
        self.assertEqual(completed.returncode, expected, completed.stderr + completed.stdout)
        self.assertNotIn(PRIVATE_MARKER, completed.stdout + completed.stderr)
        return json.loads(completed.stdout.splitlines()[-1])

    def scan(self, server, *, end=DAY, budget=1, expected=2):
        return self.cli(
            "scan", "--start", DAY, "--end", end, "--base-url", server.origin,
            "--delay", "0.001", "--max-requests", str(budget), expected=expected,
        )

    def rows(self, sql):
        with closing(sqlite3.connect(self.root / "collector.sqlite3")) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(sql)]

    def assert_no_failed_evidence(self):
        self.assertEqual(self.rows("SELECT count(*) AS n FROM snapshots"), [{"n": 0}])
        self.assertEqual(list((self.root / "evidence").glob("*.json")), [])
        report = json.loads((self.root / "reports/coverage.json").read_text())
        self.assertFalse(report["inventory_complete_for_declared_interval"])
        self.assertEqual(report["coverage"]["empty_successful_dates"], 0)

    def test_discovery_commits_exact_byte_hashes_and_sanitized_evidence_then_resumes(self):
        body = day_body()
        body["unknown_integer"] = 900719925474099312345
        body["data"]["1"][0]["game"]["home_team"]["team_name"] = "Caf\u00e9"
        raw = b" \n" + json.dumps(body, ensure_ascii=False, indent=3).encode() + b"\n\t"
        routes = {
            request_path("day", DAY): (200, raw, {"Set-Cookie": "session=DO-NOT-STORE-ME"}),
            "/division_seasons/11": (200, division_body(), {}),
            "/division_seasons/12": (200, division_body(12), {}),
        }
        with FixtureServer(routes) as server:
            first = self.scan(server)
            self.assertEqual(first["requests"], 1)
            self.assertEqual(first["successes"], 1)
            self.assertEqual(first["unfinished"], 2)
            second = self.cli("resume", "--delay", "0.001", expected=0)
            self.assertEqual(second["requests"], 2)
            third = self.cli("resume", "--delay", "0.001", expected=0)
            self.assertEqual(third["requests"], 0)
            self.assertEqual([path for path, _ in server.calls], [
                request_path("day", DAY), "/division_seasons/11", "/division_seasons/12",
            ])
            for _, headers in server.calls:
                lowered = {key.lower(): value for key, value in headers.items()}
                self.assertNotIn("authorization", lowered)
                self.assertNotIn("cookie", lowered)
                self.assertEqual(lowered["accept-encoding"], "identity")
            self.cli("report", expected=0)
            self.assertEqual(len(server.calls), 3)

        snapshot = self.rows("SELECT * FROM snapshots WHERE kind='day'")[0]
        self.assertEqual(snapshot["body_sha256"], hashlib.sha256(raw).hexdigest())
        evidence = (self.root / snapshot["evidence_path"]).read_bytes()
        self.assertEqual(snapshot["evidence_sha256"], hashlib.sha256(evidence).hexdigest())
        self.assertNotEqual(snapshot["body_sha256"], snapshot["evidence_sha256"])
        for path in [self.root / "collector.sqlite3", *self.root.glob("evidence/*.json")]:
            self.assertNotIn(PRIVATE_MARKER.encode(), path.read_bytes())
        self.assertEqual(self.rows("SELECT count(*) AS n FROM games"), [{"n": 1}])
        self.assertEqual(self.rows("SELECT count(*) AS n FROM divisions"), [{"n": 2}])
        self.assertEqual(self.rows("PRAGMA integrity_check"), [{"integrity_check": "ok"}])
        self.assertEqual(self.rows("PRAGMA foreign_key_check"), [])
        report = json.loads((self.root / "reports/coverage.json").read_text())
        self.assertTrue(report["evidence"]["complete"])
        self.assertTrue(report["inventory_complete_for_declared_interval"])
        self.assertFalse(report["all_time_complete"])

    def test_both_retry_after_forms_survive_cli_restart(self):
        for form, status in (("seconds", 429), ("date", 503)):
            with self.subTest(form=form), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary) / "archive"
                started = time.time()
                deadline = int(started) + 600
                retry = "600" if form == "seconds" else format_datetime(
                    datetime.fromtimestamp(deadline, timezone.utc), usegmt=True,
                )
                routes = {request_path("day", DAY): (status, PRIVATE_MARKER.encode(), {"Retry-After": retry})}
                with FixtureServer(routes) as server:
                    first = self.scan(server, end=NEXT_DAY, budget=2)
                    self.assertEqual(first["requests"], 1)
                    self.assertEqual(first["reason"], "deferred")
                    persisted = float(self.rows("SELECT value FROM meta WHERE key='next_request_at'")[0]["value"])
                    self.assertGreaterEqual(persisted, deadline)
                    if form == "date":
                        self.assertEqual(persisted, deadline)
                    again = self.cli("resume", "--delay", "0.001", expected=2)
                    self.assertEqual(again["requests"], 0)
                    self.assertEqual(len(server.calls), 1)
                attempt = self.rows("SELECT * FROM attempts")[0]
                self.assertEqual(attempt["http_status"], status)
                self.assertEqual(attempt["retry_at"], persisted)
                self.assert_no_failed_evidence()

    def test_authentication_refusal_stops_before_the_next_date(self):
        for status in (401, 403):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary) / "archive"
                routes = {request_path("day", DAY): (status, PRIVATE_MARKER.encode(), {})}
                with FixtureServer(routes) as server:
                    result = self.scan(server, end=NEXT_DAY, budget=2)
                    self.assertEqual(len(server.calls), 1)
                self.assertEqual(result["reason"], "blocked")
                self.assertEqual(result["resource_states"], {"blocked": 1, "pending": 1})
                self.assert_no_failed_evidence()

    def test_redirect_is_recorded_without_following_location(self):
        routes = {request_path("day", DAY): (302, PRIVATE_MARKER.encode(), {"Location": "/unexpected"})}
        with FixtureServer(routes) as server:
            result = self.scan(server)
            self.assertEqual(len(server.calls), 1)
        self.assertEqual(result["resource_states"], {"invalid": 1})
        self.assertEqual(self.rows("SELECT http_status FROM attempts"), [{"http_status": 302}])
        self.assert_no_failed_evidence()

    def test_invalid_and_missing_responses_never_become_empty_success(self):
        cases = [(200, b"{malformed", "invalid"), (200, {}, "invalid"),
                 (404, PRIVATE_MARKER.encode(), "missing")]
        for status, body, outcome in cases:
            with self.subTest(status=status, body=type(body).__name__), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary) / "archive"
                with FixtureServer({request_path("day", DAY): (status, body, {})}) as server:
                    result = self.scan(server)
                    self.assertEqual(len(server.calls), 1)
                self.assertEqual(result["resource_states"], {outcome: 1})
                self.assert_no_failed_evidence()

    def test_wrong_discovered_division_identity_and_facility_are_rejected(self):
        for field in ("UDivSeasonID", "FacilityID"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary) / "archive"
                invalid = division_body()
                invalid["division_season"][field] = 99
                routes = {request_path("day", DAY): (200, day_body(), {}),
                          "/division_seasons/11": (200, invalid, {})}
                with FixtureServer(routes) as server:
                    result = self.scan(server, budget=2)
                    self.assertEqual(len(server.calls), 2)
                self.assertEqual(result["resource_states"], {"success": 1, "invalid": 1, "pending": 1})
                self.assertEqual(self.rows("SELECT count(*) AS n FROM divisions"), [{"n": 0}])
                self.assertEqual(self.rows("SELECT kind FROM snapshots"), [{"kind": "day"}])

    def test_oversized_response_keeps_status_and_server_backoff(self):
        routes = {request_path("day", DAY): (503, b"x" * (MAX_BODY + 1), {"Retry-After": "600"})}
        started = time.time()
        with FixtureServer(routes) as server:
            server.server.handle_error = lambda *_: None
            result = self.scan(server, end=NEXT_DAY, budget=2)
            self.assertEqual(result["requests"], 1)
            self.assertEqual(result["reason"], "deferred")
            again = self.cli("resume", "--delay", "0.001", expected=2)
            self.assertEqual(again["requests"], 0)
            self.assertEqual(len(server.calls), 1)
        attempt = self.rows("SELECT * FROM attempts")[0]
        self.assertEqual(attempt["error_code"], "body_too_large")
        self.assertEqual(attempt["http_status"], 503)
        self.assertGreaterEqual(attempt["retry_at"], started + 600)
        self.assertIsNone(attempt["body_sha256"])
        self.assert_no_failed_evidence()


if __name__ == "__main__":
    unittest.main()
