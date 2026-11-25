from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts.boise_collect import collector as collector_module
from scripts.boise_collect.__main__ import main as cli_main, parser as cli_parser
from scripts.boise_collect.collector import (
    ORIGIN, Response, collect, queue_refresh, request_path,
    retry_deadline, validated_origin, writer_lock,
)
from scripts.boise_collect.bruno_client import BrunoClient
from scripts.boise_collect.model import parse_response
from scripts.boise_collect.report import build_report
from scripts.boise_collect.store import Store


DAY = "2010-09-26"


def team(ident=31, division=11):
    return {"UTeamID": ident, "UDivSeasonID": division, "FacilityID": 4,
            "Season": 75, "team_name": "Team " + str(ident), "division_name": "Coed",
            "EMailAddress": "private@example.test", "UCoachID": 999}


def game():
    return {"UGameID": 101, "FacilityID": 4, "UTeam1": 31, "UTeam2": 32,
            "SchedGameDateTime": DAY + "T10:00:00.000Z", "schedGameEndTime": None,
            "Field": 1, "field_name": "Field 1", "result": "0 - 0", "winner_id": None,
            "home_team": team(), "visitor_team": team(32, 12)}


def day_body():
    return {"data": {"1": [{"game": game(), "field": 1, "duration": 50,
                            "end": DAY + "T10:50:00.000Z"}]}, "jwt": "DO-NOT-STORE-ME"}


def division_body(ident=11):
    own = 31 if ident == 11 else 32
    return {"division_season": {"UDivSeasonID": ident, "UDivisionID": 201,
                                "season_id": 327, "Season": 75, "FacilityID": 4,
                                "NumGames": 1, "GameLength": 50,
                                "show_all_games": True, "suppress_leaderboard": False},
            "division_name": "Coed", "games": [game()], "teams": [
                {"UTeamID": own, "UDivSeasonID": ident, "FacilityID": 4, "Season": 75,
                 "TeamName": "Team " + str(own), "gamesplayed": 1, "wins": 0,
                 "losses": 1, "ties": 0, "goalsfor": 0, "goalsagainst": 0,
                 "points": 0, "position": 1}], "player_standings": [],
            "win_point": 10, "tie_point": 5, "loss_point": 0, "goal_point": 5,
            "roster": [{"FirstName": "DO-NOT-STORE-ME"}]}


def response(body, status=200, retry=None):
    return Response(status, json.dumps(body).encode(), retry)


class Clock:
    def __init__(self):
        self.value = 1800000000.0
        self.sleeps = []

    def time(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


class Client:
    base_url = "http://127.0.0.1:12345"

    def __init__(self, clock, responses):
        self.clock, self.responses, self.calls = clock, iter(responses), []

    def get(self, kind, key):
        self.calls.append((kind, key, self.clock.time()))
        return next(self.responses)


class FixtureServer:
    def __init__(self, routes):
        self.calls = []
        self.routes = routes
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parent.calls.append((self.path, dict(self.headers)))
                status, body, headers = parent.routes.get(self.path, (404, {}, {}))
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clock = Clock()

    def tearDown(self):
        self.tmp.cleanup()

    def store(self):
        return Store(self.root, Client.base_url)

    def run_collect(self, store, client, **kwargs):
        return collect(store, client, clock=self.clock.time, sleep=self.clock.sleep, **kwargs)

    def seed_failed_division(self, store, *, attempts=5, status=503, outcome="failed", error=None, retry_at=None):
        store.queue_interval(date.fromisoformat(DAY), date.fromisoformat(DAY))
        self.run_collect(store, Client(self.clock, [response(day_body())]), max_requests=1)
        for index in range(attempts):
            attempt = store.start_attempt("division", "11", self.clock.time())
            final = index == attempts - 1
            store.fail(attempt, outcome if final else "failed", status,
                       error or (f"http_{status}" if status is not None else "network_error"),
                       None, retry_at if final else None, self.clock.time())

    def test_deferred_failure_stays_a_gap_when_other_work_rediscovers_it(self):
        store = self.store()
        self.seed_failed_division(store)
        before = dict(store.conn.execute("SELECT * FROM resources WHERE kind='division' AND key='11'").fetchone())
        attempts_before = [dict(row) for row in store.conn.execute("SELECT * FROM attempts WHERE kind='division' AND key='11'")]
        store.queue_interval(date(2010, 9, 27), date(2010, 9, 27))
        rediscovery = day_body()
        rediscovery["data"]["1"][0]["game"]["UGameID"] = 102
        rediscovery["data"]["1"][0]["game"]["SchedGameDateTime"] = "2010-09-27T10:00:00.000Z"
        rediscovery["data"]["1"][0]["end"] = "2010-09-27T10:50:00.000Z"
        client = Client(self.clock, [response(division_body(12)), response(rediscovery)])
        result = self.run_collect(store, client, deferred_resources={("division", "11")})
        self.assertEqual([call[:2] for call in client.calls], [("division", "12"), ("day", "2010-09-27")])
        self.assertEqual(result["unfinished"], 0)
        self.assertEqual(result["archive_unfinished"], 1)
        self.assertEqual(result["deferred_resource_count"], 1)
        self.assertEqual(result["deferred_divisions"], ["11"])
        self.assertEqual(result["reason"], "drained")
        self.assertEqual(dict(store.conn.execute("SELECT * FROM resources WHERE kind='division' AND key='11'").fetchone()), before)
        self.assertEqual([dict(row) for row in store.conn.execute("SELECT * FROM attempts WHERE kind='division' AND key='11'")], attempts_before)
        again = self.run_collect(store, Client(self.clock, []), deferred_resources={("division", "11")})
        self.assertEqual(again["requests"], 0)
        store.close()
        report = build_report(self.root)
        self.assertFalse(report["inventory_complete_for_declared_interval"])
        self.assertEqual([(r["kind"], r["key"], r["status"]) for r in report["ledger"]["unresolved_resources"]], [("division", "11", "failed")])

    def test_deferring_a_division_does_not_shorten_global_or_resource_deadlines(self):
        store = self.store()
        deadline = self.clock.time() + 120
        self.seed_failed_division(store, retry_at=deadline)
        store.set_next_request_at(deadline)
        deferred = collector_module.validate_deferred_divisions(store, ["11", "11"])
        self.assertEqual(deferred, {("division", "11")})
        blocked = self.run_collect(store, Client(self.clock, []), deferred_resources=deferred)
        self.assertEqual(blocked["requests"], 0)
        self.assertEqual(blocked["unfinished"], 1)
        self.assertEqual(blocked["archive_unfinished"], 2)
        self.assertEqual(blocked["reason"], "deferred")
        self.assertEqual(store.next_request_at(), deadline)
        self.assertEqual(store.conn.execute("SELECT next_attempt_at FROM resources WHERE key='11'").fetchone()[0], deadline)
        self.clock.sleep(120)
        result = self.run_collect(store, Client(self.clock, [response(division_body(12))]), deferred_resources=deferred)
        self.assertEqual(result["requests"], 1)
        self.assertEqual(result["unfinished"], 0)
        self.assertEqual(result["archive_unfinished"], 1)
        self.assertEqual(store.conn.execute("SELECT next_attempt_at FROM resources WHERE key='11'").fetchone()[0], deadline)
        store.close()

    def test_only_confirmed_repeated_upstream_5xx_divisions_can_be_deferred(self):
        cases = [
            (4, 503, "failed", "http_503"),
            (5, 401, "blocked", "authentication_refused"),
            (5, 403, "blocked", "authentication_refused"),
            (5, 404, "missing", "source_not_found"),
            (5, 200, "invalid", "invalid_source_response"),
            (5, 429, "failed", "http_429"),
            (5, None, "failed", "network_error"),
            (5, 503, "failed", "network_error"),
            (5, 500, "failed", "bruno_process_failed"),
        ]
        for attempts, status, outcome, error in cases:
            with self.subTest(attempts=attempts, status=status, error=error), tempfile.TemporaryDirectory() as folder:
                store = Store(Path(folder), Client.base_url)
                self.seed_failed_division(store, attempts=attempts, status=status, outcome=outcome, error=error)
                with self.assertRaises(ValueError):
                    collector_module.validate_deferred_divisions(store, ["11"])
                store.close()
        store = self.store()
        self.seed_failed_division(store, status=500)
        for ident in ("12", "999", "0", "11?query", DAY):
            with self.subTest(ident=ident), self.assertRaises(ValueError):
                collector_module.validate_deferred_divisions(store, [ident])
        self.assertEqual(collector_module.validate_deferred_divisions(store, ["11"]), {("division", "11")})
        store.requeue("division", "11")
        with self.assertRaises(ValueError):
            collector_module.validate_deferred_divisions(store, ["11"])
        store.close()

    def test_cli_deferral_reports_success_for_selected_work_and_keeps_archive_gap(self):
        self.clock.value = 0
        store = self.store()
        self.seed_failed_division(store)
        store.close()
        stdout, stderr = io.StringIO(), io.StringIO()
        client = Client(self.clock, [response(division_body(12))])
        with patch("scripts.boise_collect.__main__.BrunoClient", return_value=client), redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli_main(["resume", "--data-dir", str(self.root), "--delay", "0.001",
                             "--defer-division", "11", "--defer-division", "11"])
        self.assertEqual(code, 0, stderr.getvalue() + stdout.getvalue())
        result = json.loads(stdout.getvalue().splitlines()[-1])
        self.assertEqual(result["unfinished"], 0)
        self.assertEqual(result["archive_unfinished"], 1)
        self.assertEqual(result["deferred_resource_count"], 1)
        self.assertEqual(result["deferred_divisions"], ["11"])
        report = json.loads((self.root / "reports/coverage.json").read_text())
        self.assertEqual(report["ledger"]["resource_states"]["failed"], 1)
        self.assertFalse(report["inventory_complete_for_declared_interval"])
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("scripts.boise_collect.__main__.BrunoClient", return_value=Client(self.clock, [])), redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli_main(["scan", "--data-dir", str(self.root), "--start", "2020-01-01",
                             "--end", "2020-01-01", "--defer-division", "999"])
        self.assertEqual(code, 1)
        store = self.store()
        self.assertEqual(store.conn.execute("SELECT count(*) FROM resources WHERE key='2020-01-01'").fetchone()[0], 0)
        store.close()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            cli_parser().parse_args(["refresh", "--data-dir", str(self.root), "--defer-division", "11"])
        self.assertEqual(raised.exception.code, 2)

    def test_pacing_accounts_for_variable_bruno_startup(self):
        store = self.store()
        store.queue_interval(date.fromisoformat(DAY), date.fromisoformat(DAY))
        clock = self.clock

        class DelayedClient(Client):
            def get(self, kind, key):
                # First runner startup is slower; calls records actual HTTP starts.
                clock.value += 2 if not self.calls else 0
                return super().get(kind, key)

        client = DelayedClient(clock, [response(day_body()), response(division_body()),
                                      response(division_body(12))])
        result = self.run_collect(store, client, delay=1)
        self.assertEqual(result["successes"], 3)
        starts = [call[2] for call in client.calls]
        self.assertTrue(all(b - a >= 1 for a, b in zip(starts, starts[1:])))
        store.close()

    def test_pacing_and_resume_does_not_replay_committed_success(self):
        store = self.store()
        store.queue_interval(date.fromisoformat(DAY), date.fromisoformat(DAY))
        client = Client(self.clock, [response(day_body())])
        first = self.run_collect(store, client, max_requests=1, delay=2)
        self.assertEqual(first["successes"], 1)
        self.assertEqual(first["unfinished"], 2)
        store.close()
        store = self.store()
        client = Client(self.clock, [response(division_body()), response(division_body(12))])
        second = self.run_collect(store, client, delay=2)
        self.assertEqual(second["requests"], 2)
        self.assertEqual([x[:2] for x in client.calls], [("division", "11"), ("division", "12")])
        self.assertEqual(client.calls[1][2] - client.calls[0][2], 2)
        again = self.run_collect(store, Client(self.clock, []), delay=2)
        self.assertEqual(again["requests"], 0)
        store.close()
        report = build_report(self.root)
        self.assertFalse(report["all_time_complete"])
        self.assertEqual(report["inventory"]["unique_games"], 1)
        for evidence in (self.root / "evidence").glob("*.json"):
            self.assertNotIn("DO-NOT-STORE-ME", evidence.read_text())
            self.assertNotIn("private@example", evidence.read_text())

    def test_retry_after_delta_and_date_persist_across_restart(self):
        for status, header in [(429, "120"), (503, format_datetime(
                datetime.fromtimestamp(self.clock.time() + 120, timezone.utc), usegmt=True))]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = Store(root, Client.base_url)
                store.queue_interval(date(2010, 9, 26), date(2010, 9, 27))
                result = self.run_collect(store, Client(self.clock, [response({}, status, header)]))
                deadline = store.next_request_at()
                self.assertEqual(result["requests"], 1)
                self.assertEqual(result["reason"], "deferred")
                self.assertEqual(deadline, self.clock.time() + 120)
                store.close()
                store = Store(root, Client.base_url)
                result = self.run_collect(store, Client(self.clock, []))
                self.assertEqual(result["requests"], 0)
                self.clock.sleep(120)
                result = self.run_collect(store, Client(self.clock, [response({"data": {}}), response({"data": {}})]))
                self.assertEqual(result["successes"], 2)
                store.close()
            # The HTTP date for the second subcase is recomputed below.
            if status == 429:
                self.clock = Clock()

    def test_retry_header_parsing_never_shortens_valid_server_delay(self):
        now = self.clock.time()
        self.assertEqual(retry_deadline("86400", now), now + 86400)
        self.assertEqual(retry_deadline("0", now), now)
        self.assertIsNone(retry_deadline("invalid", now))
        self.assertIsNone(retry_deadline("-10", now))
        self.assertIsNone(retry_deadline(None, now))
        self.assertEqual(retry_deadline("Wed, 21 Oct 2015 07:28:00 GMT", now), now)

    def test_crash_before_attempt_checkpoint_cannot_bypass_retry_after(self):
        for status, checkpoint in [(429, "fail"), (200, "succeed")]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = Store(root, Client.base_url)
                store.queue_interval(date(2010, 9, 26), date(2010, 9, 27))
                client = Client(self.clock, [response({"data": {}}, status, "120")])
                with patch.object(store, checkpoint, side_effect=RuntimeError("simulated crash")):
                    with self.assertRaises(RuntimeError):
                        self.run_collect(store, client)
                store.close()
                store = Store(root, Client.base_url)
                self.assertEqual(store.next_request_at(), self.clock.time() + 120)
                result = self.run_collect(store, Client(self.clock, []))
                self.assertEqual(result["requests"], 0)
                self.assertEqual(result["reason"], "deferred")
                self.assertEqual(store.conn.execute(
                    "SELECT outcome FROM attempts ORDER BY id LIMIT 1").fetchone()[0], "interrupted")
                store.close()

    def test_missing_invalid_and_authorization_failures_are_not_empty_success(self):
        for status, body, expected in [(404, {}, "missing"), (200, {}, "invalid"),
                                       (200, {"data": {"1": [{"game": {"FacilityID": 99}}]}}, "invalid"),
                                       (401, {"error": "DO-NOT-STORE-ME"}, "blocked")]:
            with self.subTest(status=status, expected=expected), tempfile.TemporaryDirectory() as tmp:
                store = Store(Path(tmp), Client.base_url)
                store.queue_interval(date(2010, 9, 26), date(2010, 9, 26))
                result = self.run_collect(store, Client(self.clock, [response(body, status)]))
                self.assertEqual(result["resource_states"], {expected: 1})
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 0)
                self.assertEqual(self.run_collect(store, Client(self.clock, []))["requests"], 0)
                store.close()

    def test_refresh_preserves_failed_snapshot_and_excludes_historical_days(self):
        store = self.store()
        store.queue_interval(date(2010, 9, 26), date(2010, 9, 26))
        self.run_collect(store, Client(self.clock, [response(day_body()), response(division_body()), response(division_body(12))]))
        old = store.conn.execute("SELECT snapshot_id FROM divisions WHERE id='11'").fetchone()[0]
        store.queue_interval(date(2010, 9, 27), date(2010, 9, 27))
        self.assertEqual(queue_refresh(store, self.clock.time(), 24, 30), set())
        selected = queue_refresh(store, self.clock.time(), 24, 30, ["11"])
        client = Client(self.clock, [response({}, 503, "60")])
        result = self.run_collect(store, client, allowed_resources=selected)
        self.assertEqual(client.calls[0][:2], ("division", "11"))
        self.assertEqual(result["requests"], 1)
        self.assertEqual(store.conn.execute("SELECT snapshot_id FROM divisions WHERE id='11'").fetchone()[0], old)
        self.assertEqual(store.conn.execute("SELECT status FROM resources WHERE kind='day' AND key='2010-09-27'").fetchone()[0], "pending")
        with self.assertRaises(ValueError):
            queue_refresh(store, self.clock.time(), 24, 30, ["999"])
        store.close()

    def test_unpublished_division_and_catalog_refresh_more_often_than_history(self):
        store = self.store()
        store.queue_interval(date(2010, 9, 26), date(2010, 9, 26))
        self.run_collect(store, Client(self.clock, [response(day_body()), response(division_body()), response(division_body(12))]))
        catalog = [{"UDivSeasonID": 13, "GameLength": 45, "NumGames": 8,
                    "season": {"id": 328, "SeasonNumber": 76},
                    "division": {"UDivisionID": 202, "DivisionName": "Future"}}]
        unpublished = {"division_season": {"UDivSeasonID": 13, "FacilityID": 4,
                        "season_id": 328, "Season": 76, "UDivisionID": 202,
                        "NumGames": 8, "GameLength": 45, "show_all_games": False,
                        "show_first_game": False}, "games": [], "teams": None,
                       "player_standings": []}
        store.queue_catalog()
        self.run_collect(store, Client(self.clock, [response(catalog), response(unpublished)]))
        store.queue_interval(date(2010, 9, 27), date(2010, 9, 27))
        self.clock.sleep(24 * 3600 + 1)
        selected = queue_refresh(store, self.clock.time(), 24, 30)
        self.assertEqual(selected, {("division", "13"), ("catalog", "upcoming")})
        client = Client(self.clock, [response(unpublished), response(catalog)])
        result = self.run_collect(store, client, allowed_resources=selected)
        self.assertEqual(result["requests"], 2)
        self.assertEqual([x[:2] for x in client.calls], [("division", "13"), ("catalog", "upcoming")])
        self.assertEqual(result["unfinished"], 0)
        self.assertEqual(result["archive_unfinished"], 1)
        self.clock.sleep(30 * 86400)
        selected = queue_refresh(store, self.clock.time(), 24, 30)
        self.assertIn(("division", "11"), selected)
        self.assertIn(("division", "12"), selected)
        self.assertNotIn(("day", "2010-09-27"), selected)
        store.close()

    def test_lock_and_origin_guard(self):
        with writer_lock(self.root):
            with self.assertRaises(ValueError):
                with writer_lock(self.root):
                    self.fail("two writers acquired the same lock")
        self.assertEqual(validated_origin(ORIGIN + "/"), ORIGIN)
        for url in ("http://lps-api-prod.lps-test.com", "https://example.test", "http://user:secret@localhost:123", "http://localhost:123/path", "http://localhost:123/?secret=x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validated_origin(url)
        with self.assertRaises(ValueError):
            request_path("division", "11?secret=x")

    def test_cli_http_boundary_restart_report_and_no_credentials(self):
        routes = {request_path("day", DAY): (200, day_body(), {"Set-Cookie": "session=secret"}),
                  "/division_seasons/11": (200, division_body(), {}),
                  "/division_seasons/12": (200, division_body(12), {})}
        with FixtureServer(routes) as server:
            def run(*args):
                command = [sys.executable, "-m", "scripts.boise_collect", *args,
                           "--data-dir", str(self.root)]
                env = dict(os.environ, HTTP_PROXY="http://unusable.invalid:9", HTTPS_PROXY="http://unusable.invalid:9")
                return subprocess.run(command, capture_output=True, text=True, env=env, timeout=15)
            first = run("scan", "--start", DAY, "--end", DAY, "--base-url", server.origin,
                        "--delay", "0.001", "--max-requests", "1")
            self.assertEqual(first.returncode, 2, first.stderr + first.stdout)
            second = run("resume", "--delay", "0.001")
            self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
            third = run("resume", "--delay", "0.001")
            self.assertEqual(third.returncode, 0, third.stderr + third.stdout)
            self.assertEqual(json.loads(third.stdout.splitlines()[-1])["requests"], 0)
            self.assertEqual(len(server.calls), 3)
            for _, headers in server.calls:
                self.assertNotIn("Authorization", headers)
                self.assertNotIn("Cookie", headers)
            report = run("report")
            self.assertEqual(report.returncode, 0, report.stderr)
            self.assertEqual(len(server.calls), 3)
            self.assertTrue((self.root / "reports/coverage.md").is_file())
            self.assertNotIn("DO-NOT-STORE-ME", first.stdout + second.stdout)

    def test_redirect_is_not_followed(self):
        routes = {request_path("day", DAY): (302, b"private response", {"Location": "/unexpected"})}
        with FixtureServer(routes) as server:
            reply = BrunoClient(server.origin).get("day", DAY)
            self.assertEqual(reply.status, 302)
            self.assertEqual(len(server.calls), 1)

    def test_oversized_body_does_not_discard_server_retry_after(self):
        routes = {request_path("day", DAY): (503, b"x" * 101, {"Retry-After": "120"})}
        with FixtureServer(routes) as server, patch("scripts.boise_collect.bruno_client.MAX_BODY", 100):
            store = Store(self.root, server.origin)
            store.queue_interval(date(2010, 9, 26), date(2010, 9, 27))
            result = self.run_collect(store, BrunoClient(server.origin))
            self.assertEqual(result["requests"], 1)
            self.assertEqual(store.next_request_at(), self.clock.time() + 120)
            attempt = store.conn.execute("SELECT * FROM attempts").fetchone()
            self.assertEqual(attempt["http_status"], 503)
            self.assertEqual(attempt["error_code"], "body_too_large")
            self.assertIsNone(attempt["body_sha256"])
            store.close()

    def test_retry_after_http_survives_cli_process_restart(self):
        deadline = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + 600, timezone.utc)
        routes = {request_path("day", DAY): (503, b"private server body", {"Retry-After": format_datetime(deadline, usegmt=True)})}
        with FixtureServer(routes) as server:
            base = [sys.executable, "-m", "scripts.boise_collect"]
            first = subprocess.run(base + ["scan", "--data-dir", str(self.root), "--start", DAY,
                "--end", "2010-09-27", "--base-url", server.origin], capture_output=True, text=True, timeout=15)
            self.assertEqual(first.returncode, 2, first.stderr)
            again = subprocess.run(base + ["resume", "--data-dir", str(self.root)], capture_output=True, text=True, timeout=15)
            self.assertEqual(again.returncode, 2, again.stderr)
            self.assertEqual(json.loads(again.stdout.splitlines()[-1])["requests"], 0)
            self.assertEqual(len(server.calls), 1)
            self.assertNotIn("private server body", first.stdout + first.stderr)

    def test_oversized_authentication_refusal_still_stops_the_run(self):
        routes = {request_path("day", DAY): (401, b"x" * 101, {})}
        with FixtureServer(routes) as server, patch("scripts.boise_collect.bruno_client.MAX_BODY", 100):
            store = Store(self.root, server.origin)
            store.queue_interval(date(2010, 9, 26), date(2010, 9, 27))
            result = self.run_collect(store, BrunoClient(server.origin))
            self.assertEqual(result["requests"], 1)
            self.assertEqual(result["reason"], "blocked")
            self.assertEqual(result["resource_states"], {"blocked": 1, "pending": 1})
            store.close()


if __name__ == "__main__":
    unittest.main()
