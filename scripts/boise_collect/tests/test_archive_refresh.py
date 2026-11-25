"""Catalog normalization and durable bounded refresh behavior."""

from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import date
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts.boise_collect.__main__ import main
from scripts.boise_collect.collector import collect, request_path
from scripts.boise_collect.model import parse_response
from scripts.boise_collect.store import Store
from scripts.boise_collect.tests.test_collector import Client, Clock, DAY, FixtureServer, day_body, division_body, response


def catalog():
    return [
        {"UDivSeasonID": ident, "GameLength": 50, "NumGames": 1, "DeadlineDate": "2026-11-04",
         "team_fee": "625.00", "ref_fee": "20.00", "deposit_amount": "100.00",
         "season": {"id": 327, "SeasonNumber": 75, "StartDate": "1926-11-02T00:00:00Z",
                    "EndDate": "1927-01-03T00:00:00Z", "Open": True},
         "division": {"UDivisionID": 201, "DivisionName": "Coed", "DivisionCode": "C",
                      "DivClass": "COED", "PlayerType": "Adult", "Gender": "Coed", "Age": "O18"}}
        for ident in (11, 12)
    ]


def empty_division(ident):
    return {"division_season": {"UDivSeasonID": ident, "FacilityID": 4},
            "games": [], "teams": None, "player_standings": []}


class ArchiveRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.store = Store(self.root, Client.base_url)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def run_collect(self, bodies, *, job=None, budget=100, allowed=None):
        client = Client(self.clock, bodies)
        result = collect(self.store, client, max_requests=budget, delay=1,
                         clock=self.clock.time, sleep=self.clock.sleep,
                         refresh_job_id=job, allowed_resources=allowed)
        return result, client

    def seed(self):
        self.store.queue_interval(date.fromisoformat(DAY), date.fromisoformat(DAY))
        result, _ = self.run_collect([response(day_body()), response(division_body()), response(division_body(12))])
        self.assertEqual(result["unfinished"], 0)

    def record(self, table, ident):
        row = self.store.conn.execute(f"SELECT * FROM {table} WHERE id=?", (ident,)).fetchone()
        return dict(row), json.loads(row["data_json"])

    def resource(self, kind, key):
        return self.store.conn.execute("SELECT * FROM resources WHERE kind=? AND key=?", (kind, key)).fetchone()

    def test_catalog_normalizes_every_row_with_raw_dates_and_sparse_details_keep_metadata(self):
        self.store.queue_catalog()
        result, _ = self.run_collect([response(catalog())], budget=1)
        snapshot = self.resource("catalog", "upcoming")["last_success_snapshot_id"]
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM divisions").fetchone()[0], 2)
        self.assertIsNone(self.resource("division", "11")["last_success_snapshot_id"])
        _, session = self.record("seasons", "327")
        self.assertEqual(session["raw_start"], "1926-11-02T00:00:00Z")
        self.assertEqual(session["date_warnings"], ["source_dates_before_2000"])
        _, definition = self.record("division_definitions", "201")
        self.assertEqual((definition["code"], definition["gender"], definition["age"]), ("C", "Coed", "O18"))
        self.run_collect([response(division_body()), response(division_body(12))])
        detail = self.resource("division", "11")["last_success_snapshot_id"]
        row, division = self.record("divisions", "11")
        self.assertEqual(row["snapshot_id"], detail)
        self.assertEqual(division["fees"]["referee"], "20.00")
        self.assertEqual(division["_field_sources"]["fees.referee"], snapshot)
        self.assertEqual(division["scoring_rules"]["win_point"], 10)
        _, session = self.record("seasons", "327")
        self.assertEqual(session["_field_sources"]["raw_start"], snapshot)
        self.assertEqual(session["date_warnings"], ["source_dates_before_2000"])
        self.assertEqual(self.record("division_definitions", "201")[1]["code"], "C")

        fresh = catalog()
        fresh[0]["DeadlineDate"] = "2026-11-11"
        self.store.requeue("catalog", "upcoming")
        self.run_collect([response(fresh)], allowed={("catalog", "upcoming")})
        row, division = self.record("divisions", "11")
        self.assertEqual(row["snapshot_id"], detail, "catalog cannot become the detailed standings anchor")
        self.assertEqual(division["standings_state"], "present")
        self.assertEqual(division["deadline"], "2026-11-11")
        self.assertNotEqual(division["_field_sources"]["deadline"], detail)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM standings WHERE snapshot_id=?", (detail,)).fetchone()[0], 1)

    def test_metadata_merge_does_not_restore_a_retracted_game_score(self):
        self.seed()
        self.store.requeue("division", "11")
        body = division_body()
        body["games"][0]["result"] = None
        self.run_collect([response(body)], allowed={("division", "11")})
        row = self.store.conn.execute("SELECT home_score,away_score FROM games WHERE id='101'").fetchone()
        self.assertEqual(tuple(row), (None, None))
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM game_observations WHERE game_id='101'").fetchone()[0], 4)

    def test_sparse_catalog_rows_share_rich_season_and_definition_metadata(self):
        rows = catalog()
        rows[1]["season"] = {"id": 327, "SeasonNumber": 75}
        rows[1]["division"] = {"UDivisionID": 201}
        self.store.queue_catalog()
        self.run_collect([response(rows)], budget=1)
        _, season = self.record("seasons", "327")
        self.assertEqual(season["raw_start"], rows[0]["season"]["StartDate"])
        self.assertEqual(season["date_warnings"], ["source_dates_before_2000"])
        self.assertEqual(self.record("division_definitions", "201")[1]["code"], "C")

    def test_named_refresh_resumes_and_discovers_each_division_once(self):
        self.seed()
        start = date.fromisoformat(DAY)
        self.store.begin_refresh("recent", start, start, True, self.clock.time())
        first, client = self.run_collect([response(catalog() + [{"UDivSeasonID": 13}])], job="recent", budget=1)
        self.assertEqual(client.calls[0][:2], ("catalog", "upcoming"))
        self.assertEqual(first["refresh_job"]["completed"], 1)
        self.store.close()
        self.store = Store(self.root, Client.base_url)
        same = self.store.begin_refresh("recent", start, start, True, self.clock.time())
        self.assertEqual(same["completed"], 1)
        self.assertEqual(self.resource("catalog", "upcoming")["status"], "success")
        second, client = self.run_collect([response(day_body())], job="recent", budget=1)
        self.assertEqual(client.calls[0][:2], ("day", DAY))
        third, client = self.run_collect([response(division_body()), response(division_body(12)), response(empty_division(13))], job="recent")
        self.assertEqual([x[:2] for x in client.calls], [("division", "11"), ("division", "12"), ("division", "13")])
        self.assertEqual(third["refresh_job"]["status"], "complete")
        self.store.begin_refresh("recent", start, start, True, self.clock.time())
        resumed, _ = self.run_collect([], job="recent")
        self.assertEqual(resumed["requests"], 0)
        self.assertEqual(self.resource("day", DAY)["attempts"], 2)
        self.assertEqual(self.resource("division", "11")["attempts"], 2)
        self.assertEqual(self.resource("catalog", "upcoming")["attempts"], 1)

    def test_existing_gaps_and_deadlines_survive_refresh_planning(self):
        self.seed()
        earlier = "2010-09-25"
        self.store.queue_interval(date.fromisoformat(earlier), date.fromisoformat(earlier))
        self.run_collect([response({"bad": "shape"})], allowed={("day", earlier)})
        self.store.requeue("division", "12")
        self.run_collect([response({}, 503, "120")], allowed={("division", "12")})
        deadline = self.resource("division", "12")["next_attempt_at"]
        global_deadline = self.store.next_request_at()
        self.store.begin_refresh("safe", date.fromisoformat(earlier), date.fromisoformat(DAY), False, self.clock.time())
        self.assertEqual(self.resource("day", earlier)["status"], "invalid")
        self.assertEqual(self.resource("division", "12")["status"], "failed")
        self.assertEqual(self.resource("division", "12")["next_attempt_at"], deadline)
        self.assertEqual(self.store.next_request_at(), global_deadline)
        blocked, _ = self.run_collect([], job="safe")
        self.assertEqual(blocked["requests"], 0)
        self.clock.sleep(120)
        done, _ = self.run_collect([response(day_body()), response(division_body())], job="safe")
        self.assertEqual(done["refresh_job"]["status"], "complete_with_gaps")
        self.assertEqual(done["refresh_job"]["skipped"], 2)
        self.assertEqual(done["unfinished"], 0)
        self.assertEqual(done["archive_unfinished"], 2)

    def test_job_owned_retry_preserves_deadline_and_uses_consecutive_failures(self):
        self.seed()
        start = date.fromisoformat(DAY)
        self.store.begin_refresh("retry", start, start, False, self.clock.time())
        now = self.clock.time()
        failed, client = self.run_collect([response({}, 503)], job="retry")
        self.assertEqual(self.resource("day", DAY)["next_attempt_at"], client.calls[0][2] + 30)
        first_deadline = self.resource("day", DAY)["next_attempt_at"]
        self.store.begin_refresh("retry", start, start, False, self.clock.time())
        self.assertEqual(self.resource("day", DAY)["next_attempt_at"], first_deadline)
        self.clock.sleep(30)
        now = self.clock.time()
        failed, _ = self.run_collect([response({}, 503)], job="retry")
        self.assertEqual(self.resource("day", DAY)["next_attempt_at"], now + 60)
        self.store.requeue("day", DAY)
        self.assertEqual(self.resource("day", DAY)["next_attempt_at"], now + 60)
        with self.assertRaisesRegex(ValueError, "deadline"):
            self.store.start_attempt("day", DAY, now + 1)

    def test_new_terminal_failure_stays_attention_on_resume(self):
        start = date.fromisoformat(DAY)
        self.store.begin_refresh("bad", start, start, False, self.clock.time())
        bad, _ = self.run_collect([response({"bad": "shape"})], job="bad")
        self.assertEqual(bad["refresh_job"]["status"], "needs_attention")
        self.assertEqual(bad["unfinished"], 1)
        self.store.begin_refresh("bad", start, start, False, self.clock.time())
        again, _ = self.run_collect([], job="bad")
        self.assertEqual(again["requests"], 0)
        self.assertEqual(self.resource("day", DAY)["status"], "invalid")
        with self.assertRaisesRegex(ValueError, "unfinished"):
            self.store.begin_refresh("different", start, start, False, self.clock.time())
        with self.assertRaisesRegex(ValueError, "different dates"):
            self.store.begin_refresh("bad", start, start, True, self.clock.time())

    def test_named_refresh_reports_backoff_when_no_member_is_due(self):
        start = date.fromisoformat(DAY)
        self.store.begin_refresh("backoff", start, start, False, self.clock.time())
        self.run_collect([response({}, 503, "120")], job="backoff")
        self.store.close()
        self.store = Store(self.root, Client.base_url)
        waiting, _ = self.run_collect([], job="backoff")
        self.assertEqual((waiting["requests"], waiting["reason"]), (0, "deferred"))
        self.assertEqual(waiting["refresh_job"]["status"], "active")

    def test_job_membership_and_success_checkpoint_are_one_transaction(self):
        start = date.fromisoformat(DAY)
        self.store.begin_refresh("atomic", start, start, False, self.clock.time())
        self.store.conn.execute("CREATE TRIGGER fail_membership BEFORE INSERT ON refresh_job_resources WHEN NEW.kind='division' BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_collect([response(day_body())], job="atomic")
        self.assertEqual(self.store.refresh_job("atomic")["completed"], 0)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0], 0)
        self.assertEqual(self.resource("day", DAY)["status"], "running")
        self.assertIsNone(self.resource("division", "11"))
        self.store.conn.execute("DROP TRIGGER fail_membership")
        done, _ = self.run_collect([response(day_body()), response(division_body()), response(division_body(12))], job="atomic")
        self.assertEqual(done["refresh_job"]["status"], "complete")
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM attempts WHERE outcome='interrupted'").fetchone()[0], 1)
        self.assertEqual(list(self.store.conn.execute("PRAGMA foreign_key_check")), [])

    def test_cli_named_job_budget_resume_and_completed_replay(self):
        self.seed()
        self.store.set_next_request_at(0)
        self.store.close()
        first_client = Client(self.clock, [response(catalog())])
        with patch("scripts.boise_collect.__main__.BrunoClient", return_value=first_client), \
                patch("scripts.boise_collect.__main__.write_report", return_value={"evidence": {"complete": True}}), \
                redirect_stdout(io.StringIO()):
            first = main(["refresh", "--data-dir", str(self.root), "--start", DAY, "--end", DAY,
                          "--include-upcoming", "--job-id", "cli", "--max-requests", "1", "--delay", "0.001"])
        self.assertEqual(first, 2)
        second_client = Client(self.clock, [response(day_body()), response(division_body()), response(division_body(12))])
        with patch("scripts.boise_collect.__main__.BrunoClient", return_value=second_client), \
                patch("scripts.boise_collect.__main__.write_report", return_value={"evidence": {"complete": True}}), \
                redirect_stdout(io.StringIO()):
            second = main(["resume", "--data-dir", str(self.root), "--job-id", "cli", "--delay", "0.001"])
        self.assertEqual(second, 0)
        replay = Client(self.clock, [])
        with patch("scripts.boise_collect.__main__.BrunoClient", return_value=replay), \
                patch("scripts.boise_collect.__main__.write_report", return_value={"evidence": {"complete": True}}), \
                redirect_stdout(io.StringIO()):
            last = main(["refresh", "--data-dir", str(self.root), "--start", DAY, "--end", DAY,
                         "--include-upcoming", "--job-id", "cli", "--delay", "0.001"])
        self.assertEqual(last, 0)
        self.assertEqual(replay.calls, [])
        self.store = Store(self.root, Client.base_url)

    def test_real_bruno_cli_refresh_catalog_budget_and_zero_request_resume(self):
        routes = {
            request_path("catalog", "upcoming"): (200, catalog(), {}),
            request_path("day", DAY): (200, day_body(), {}),
            request_path("division", "11"): (200, division_body(), {}),
            request_path("division", "12"): (200, division_body(12), {}),
        }
        root = self.root / "bruno-fixture"
        with FixtureServer(routes) as server:
            Store(root, server.origin).close()
            common = ["--data-dir", str(root), "--job-id", "real-cli", "--max-requests", "2", "--delay", "0.001"]
            with redirect_stdout(io.StringIO()):
                first = main(["refresh", *common, "--start", DAY, "--end", DAY, "--include-upcoming"])
            self.assertEqual(first, 2)
            self.assertEqual([path for path, _ in server.calls], [request_path("catalog", "upcoming"), request_path("day", DAY)])
            with redirect_stdout(io.StringIO()):
                second = main(["resume", *common])
                third = main(["resume", *common])
            self.assertEqual((second, third), (0, 0))
            self.assertEqual(len(server.calls), 4)
            for _, headers in server.calls:
                self.assertNotIn("authorization", {k.lower() for k in headers})
                self.assertNotIn("cookie", {k.lower() for k in headers})
            report = json.loads((root / "reports/coverage.json").read_text())
            self.assertTrue(report["evidence"]["complete"])
            with closing(sqlite3.connect(root / "collector.sqlite3")) as db:
                self.assertEqual(db.execute("SELECT status FROM refresh_jobs WHERE id='real-cli'").fetchone()[0], "complete")
                self.assertEqual(db.execute("SELECT count(*) FROM snapshots").fetchone()[0], 4)

    def test_ordinary_resume_automatically_keeps_active_job_scope(self):
        self.seed()
        start = date.fromisoformat(DAY)
        other = date(2010, 9, 27)
        self.store.queue_interval(other, other)
        self.store.begin_refresh("named", start, start, False, self.clock.time())
        done, client = self.run_collect([response(day_body()), response(division_body()), response(division_body(12))])
        self.assertEqual(done["refresh_job"]["status"], "complete")
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(self.resource("day", other.isoformat())["status"], "pending")
        self.store.begin_refresh("next", start, start, False, self.clock.time())

    def test_newer_successes_from_an_older_collector_reconcile_without_refetch(self):
        self.seed()
        start = date.fromisoformat(DAY)
        self.store.begin_refresh("compatible", start, start, False, self.clock.time())
        self.run_collect([response(day_body()), response(division_body()), response(division_body(12))], job="compatible")
        # Reproduce the state left by a collector that saved resource successes
        # without updating the additive job bookkeeping.
        with self.store.conn:
            self.store.conn.execute("UPDATE refresh_job_resources SET status='pending',completed_snapshot_id=NULL")
            self.store.conn.execute("UPDATE refresh_jobs SET status='active',completed_at=NULL")
        fixed, client = self.run_collect([], job="compatible")
        self.assertEqual(fixed["requests"], 0)
        self.assertEqual(fixed["refresh_job"]["status"], "complete")
        self.assertEqual(fixed["refresh_job"]["pending"], 0)
        self.store.begin_refresh("next", start, start, False, self.clock.time())

    def test_direct_store_success_updates_the_active_job_and_rejects_unrelated_work(self):
        start = date.fromisoformat(DAY)
        other = date(2010, 9, 27)
        self.store.queue_interval(other, other)
        self.store.begin_refresh("direct", start, start, False, self.clock.time())
        with self.assertRaisesRegex(ValueError, "unfinished refresh"):
            self.store.start_attempt("day", other.isoformat(), self.clock.time())
        attempt = self.store.start_attempt("day", DAY, self.clock.time())
        self.store.succeed(attempt, parse_response("day", DAY, {"data": {}}), "a" * 64, self.clock.time())
        self.assertEqual(self.store.refresh_job("direct")["completed"], 1)
        done, _ = self.run_collect([])
        self.assertEqual(done["refresh_job"]["status"], "complete")

    def test_cli_guards_other_planning_and_plain_resume_guidance_keeps_job_id(self):
        self.seed()
        start = date.fromisoformat(DAY)
        self.store.begin_refresh("guarded", start, start, False, self.clock.time())
        self.store.set_next_request_at(0)
        self.store.close()
        for args in (["scan", "--start", "2010-09-27", "--end", "2010-09-27"], ["refresh"]):
            stderr = io.StringIO()
            with patch("scripts.boise_collect.__main__.BrunoClient", return_value=Client(self.clock, [])), redirect_stderr(stderr):
                result = main([*args, "--data-dir", str(self.root), "--delay", "0.001"])
            self.assertEqual(result, 1)
            self.assertIn("resume --job-id guarded", stderr.getvalue())
        client = Client(self.clock, [])
        client.get = lambda *args: (_ for _ in ()).throw(KeyboardInterrupt())
        stderr = io.StringIO()
        with patch("scripts.boise_collect.__main__.BrunoClient", return_value=client), redirect_stderr(stderr):
            result = main(["resume", "--data-dir", str(self.root), "--delay", "0.001"])
        self.assertEqual(result, 130)
        self.assertIn("resume --job-id guarded", stderr.getvalue())
        self.store = Store(self.root, Client.base_url)
        self.assertIsNone(self.resource("day", "2010-09-27"))


if __name__ == "__main__":
    unittest.main()
