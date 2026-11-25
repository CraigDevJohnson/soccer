"""Persistence and transaction tests independent of the wire-format parser."""

from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.boise_collect.store import Store


ORIGIN = "https://lps-api-prod.lps-test.com"
BODY_HASH = hashlib.sha256(b"raw response never retained").hexdigest()
DAY = "2010-09-01"


def parsed(**changes):
    values = dict(evidence={"games": []}, division=None, season=None, definition=None,
                  teams=[], games=[], standings=[], leaders=[], discoveries=set(), warnings=[])
    values.update(changes)
    return SimpleNamespace(**values)


def division_response(score=2):
    return parsed(
        evidence={"division_id": "17", "home_score": score},
        division={"id": "17", "facility_id": 4, "definition_id": "8", "season_id": "9",
                  "season_number": 21, "name": "Historical division", "expected_games": 13,
                  "game_minutes": 35, "standings_state": "present"},
        season={"id": "9", "facility_id": 4, "number": 21, "raw_start": None, "raw_end": None},
        definition={"id": "8", "facility_id": 4, "name": "Historical division", "code": "C"},
        teams=[{"id": "1", "facility_id": 4, "division_id": "17", "season_number": 21, "name": "Home"},
               {"id": "2", "facility_id": 4, "division_id": "17", "season_number": 21, "name": "Away"}],
        games=[{"id": "100", "facility_id": 4, "home_id": "1", "away_id": "2",
                "start_local": "2010-09-01T09:00:00", "home_score": score, "away_score": 1,
                "result": f"{score} - 1", "winner_id": "1", "slot_minutes": 35}],
        standings=[{"division_id": "17", "team_id": "1", "position": 1, "games_played": 1,
                    "wins": 1, "losses": 0, "ties": 0, "goals_for": score, "goals_against": 1, "points": 3}],
        leaders=[{"division_id": "17", "team_id": "1", "player_id": "300",
                  "goals": 1, "assists": 0, "points": 1}],
        discoveries={"17", "18"}, warnings=["sample warning"],
    )


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root, ORIGIN)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def row(self, kind="day", key=DAY):
        return self.store.conn.execute("SELECT * FROM resources WHERE kind=? AND key=?", (kind, key)).fetchone()

    def seed_division(self):
        self.store.queue_interval(date.fromisoformat(DAY), date.fromisoformat(DAY))
        attempt = self.store.start_attempt("day", DAY, 1)
        self.store.succeed(attempt, parsed(discoveries={"17"}), BODY_HASH, 2)

    def collect_division(self, score=2, now=3):
        attempt = self.store.start_attempt("division", "17", now)
        return self.store.succeed(attempt, division_response(score), BODY_HASH, now + 1)

    def reopen(self, origin=ORIGIN):
        self.store.close()
        self.store = Store(self.root, origin)

    def test_intervals_overlap_without_resetting_success(self):
        self.store.queue_interval(date(2010, 9, 1), date(2010, 9, 2))
        attempt = self.store.start_attempt("day", DAY, 1)
        self.store.succeed(attempt, parsed(), BODY_HASH, 2)
        self.store.queue_interval(date(2010, 9, 1), date(2010, 9, 3))
        self.store.queue_interval(date(2010, 9, 1), date(2010, 9, 2))
        self.assertEqual(3, self.store.conn.execute("SELECT count(*) FROM resources").fetchone()[0])
        self.assertEqual(2, self.store.conn.execute("SELECT count(*) FROM intervals").fetchone()[0])
        self.assertEqual("success", self.row()["status"])
        self.assertEqual("2010-09-02", self.store.next_resource(5, set())["key"])
        self.assertEqual("2010-09-03", self.store.next_resource(5, {("day", "2010-09-02")})["key"])
        with self.assertRaises(ValueError):
            self.store.queue_interval(date(2010, 9, 2), date(2010, 9, 1))

    def test_success_is_durable_and_content_is_addressed(self):
        self.seed_division()
        snapshot = self.collect_division()
        self.store.set_next_request_at(123.75)
        self.reopen(ORIGIN + "/")
        self.assertEqual(123.75, self.store.next_request_at())
        row = self.row("division", "17")
        self.assertEqual("success", row["status"])
        self.assertEqual(snapshot, row["last_success_snapshot_id"])
        self.assertEqual(200, row["http_status"])
        self.assertEqual("18", self.store.next_resource(5, set())["key"])
        self.assertIsNone(self.store.next_resource(5, {("division", "18")}))
        source = self.store.conn.execute("SELECT * FROM snapshots WHERE id=?", (snapshot,)).fetchone()
        data = (self.root / source["evidence_path"]).read_bytes()
        self.assertEqual(source["evidence_sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(BODY_HASH, source["body_sha256"])
        self.assertEqual({"division_id": "17", "home_score": 2}, json.loads(data))
        self.assertEqual(["sample warning"], json.loads(source["warnings_json"]))
        self.assertFalse(list((self.root / "evidence").glob(".pending-*")))
        attempt = self.store.conn.execute("SELECT * FROM attempts WHERE id=?", (source["attempt_id"],)).fetchone()
        self.assertEqual(("success", 200), (attempt["outcome"], attempt["http_status"]))
        for table, count in [("seasons", 1), ("division_definitions", 1), ("divisions", 1),
                             ("teams", 2), ("games", 1), ("game_observations", 1),
                             ("standings", 1), ("player_leaders", 1)]:
            self.assertEqual(count, self.store.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], table)

    def test_discovery_queue_deduplicates_and_prioritizes(self):
        self.store.queue_interval(date(2010, 9, 1), date(2010, 9, 3))
        self.store.queue_catalog()
        self.store.queue_catalog()
        self.assertEqual("catalog", self.store.next_resource(1, set())["kind"])
        first = self.store.start_attempt("day", DAY, 1)
        self.store.succeed(first, parsed(discoveries={"17", "18"}), BODY_HASH, 2)
        second = self.store.start_attempt("day", "2010-09-02", 3)
        self.store.succeed(second, parsed(discoveries={"17"}), BODY_HASH, 4)
        self.assertEqual(2, self.store.conn.execute("SELECT count(*) FROM resources WHERE kind='division'").fetchone()[0])
        self.assertEqual(3, self.store.conn.execute("SELECT count(*) FROM discoveries").fetchone()[0])
        self.assertEqual("division", self.store.next_resource(5, set())["kind"])
        self.assertEqual("17", self.store.next_resource(5, set())["key"])

    def test_interrupted_recovery_keeps_visible_ledger(self):
        self.seed_division()
        attempt = self.store.start_attempt("division", "17", 3)
        self.store.set_next_request_at(500)
        self.reopen()
        self.assertEqual("running", self.row("division", "17")["status"])
        self.store.recover_interrupted(4)
        self.store.recover_interrupted(5)
        row = self.row("division", "17")
        self.assertEqual("pending", row["status"])
        self.assertEqual(1, row["attempts"])
        ledger = self.store.conn.execute("SELECT * FROM attempts WHERE id=?", (attempt,)).fetchone()
        self.assertEqual(("interrupted", 4), (ledger["outcome"], ledger["finished_at"]))
        self.assertEqual(500, self.store.next_request_at())
        self.assertEqual("17", self.store.next_resource(5, set())["key"])

    def test_failed_refresh_preserves_last_good_snapshot(self):
        self.seed_division()
        snapshot = self.collect_division()
        self.store.requeue("division", "17")
        attempt = self.store.start_attempt("division", "17", 5)
        self.store.fail(attempt, "failed", 503, "http_503", BODY_HASH, 100, 6)
        self.reopen()
        row = self.row("division", "17")
        self.assertEqual(("failed", snapshot, 4, 2),
                         (row["status"], row["last_success_snapshot_id"], row["last_success_at"], row["attempts"]))
        self.assertEqual(1, self.store.conn.execute("SELECT count(*) FROM games").fetchone()[0])
        self.assertIsNone(self.store.next_resource(99, {("division", "18")}))
        self.assertEqual("17", self.store.next_resource(100, set())["key"])
        with self.assertRaises(ValueError):
            self.store.start_attempt("division", "17", 99)

    def test_nonretryable_states_require_requeue(self):
        self.seed_division()
        for count, outcome in enumerate(("missing", "invalid", "blocked")):
            if count:
                self.store.requeue("division", "17")
            attempt = self.store.start_attempt("division", "17", count * 3 + 3)
            self.store.fail(attempt, outcome, 404, "source_problem", BODY_HASH, 0, count * 3 + 4)
            self.assertIsNone(self.store.next_resource(100, set()))
        self.store.requeue("division", "17")
        self.assertEqual("pending", self.row("division", "17")["status"])
        self.assertEqual("17", self.store.next_resource(100, set())["key"])

    def test_refresh_preserves_observations_and_official_rows(self):
        self.seed_division()
        older = self.collect_division(2)
        self.store.requeue("division", "17")
        newer = self.collect_division(4, now=5)
        self.assertNotEqual(older, newer)
        self.assertEqual(4, self.store.conn.execute("SELECT home_score FROM games").fetchone()[0])
        rows = self.store.conn.execute("SELECT data_json FROM game_observations ORDER BY snapshot_id").fetchall()
        self.assertEqual([2, 4], [json.loads(row[0])["home_score"] for row in rows])
        self.assertEqual([2, 4], [r[0] for r in self.store.conn.execute("SELECT goals_for FROM standings ORDER BY snapshot_id")])
        self.assertEqual(2, self.store.conn.execute("SELECT count(*) FROM discoveries WHERE division_id='18'").fetchone()[0])
        self.assertEqual(2, self.store.conn.execute("SELECT count(*) FROM resources WHERE kind='division'").fetchone()[0])

    def test_identical_snapshot_reuses_evidence_without_replacing(self):
        self.seed_division()
        older = self.collect_division()
        path = self.root / self.store.conn.execute("SELECT evidence_path FROM snapshots WHERE id=?", (older,)).fetchone()[0]
        before = path.stat().st_ino
        self.store.requeue("division", "17")
        newer = self.collect_division(now=5)
        self.assertNotEqual(older, newer)
        self.assertEqual(before, path.stat().st_ino)
        self.assertEqual(2, len(list((self.root / "evidence").glob("*.json"))))

    def test_corrupt_existing_evidence_is_not_overwritten(self):
        self.seed_division()
        snapshot = self.collect_division()
        path = self.root / self.store.conn.execute("SELECT evidence_path FROM snapshots WHERE id=?", (snapshot,)).fetchone()[0]
        path.write_bytes(b"corrupt evidence")
        self.store.requeue("division", "17")
        attempt = self.store.start_attempt("division", "17", 5)
        with self.assertRaisesRegex(ValueError, "content address"):
            self.store.succeed(attempt, division_response(), BODY_HASH, 6)
        self.assertEqual(b"corrupt evidence", path.read_bytes())
        self.assertEqual(snapshot, self.row("division", "17")["last_success_snapshot_id"])
        self.assertEqual("running", self.row("division", "17")["status"])

    def test_database_failure_rolls_back_whole_ingestion(self):
        self.seed_division()
        snapshot = self.collect_division()
        self.store.requeue("division", "17")
        attempt = self.store.start_attempt("division", "17", 5)
        bad = division_response(9)
        bad.discoveries.add("19")
        bad.standings.append(dict(bad.standings[0]))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.succeed(attempt, bad, BODY_HASH, 6)
        self.assertEqual(snapshot, self.row("division", "17")["last_success_snapshot_id"])
        self.assertEqual(2, self.store.conn.execute("SELECT home_score FROM games").fetchone()[0])
        self.assertEqual(2, self.store.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0])
        self.assertIsNone(self.row("division", "19"))
        self.assertEqual("running", self.store.conn.execute("SELECT outcome FROM attempts WHERE id=?", (attempt,)).fetchone()[0])
        self.store.fail(attempt, "invalid", 200, "invalid_fixture", BODY_HASH, None, 7)
        self.assertEqual("invalid", self.row("division", "17")["status"])

    def test_evidence_write_failure_cannot_commit_success(self):
        self.seed_division()
        attempt = self.store.start_attempt("division", "17", 3)
        with patch("scripts.boise_collect.store.os.link", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.store.succeed(attempt, division_response(), BODY_HASH, 4)
        self.assertEqual("running", self.row("division", "17")["status"])
        self.assertEqual(1, self.store.conn.execute("SELECT count(*) FROM snapshots").fetchone()[0])
        self.assertFalse(list((self.root / "evidence").glob(".pending-*")))
        self.store.recover_interrupted(5)
        self.assertEqual("pending", self.row("division", "17")["status"])

    def test_rejects_unknown_origins_versions_and_unversioned_databases(self):
        with self.assertRaisesRegex(ValueError, "different API origin"):
            Store(self.root, "http://127.0.0.1:1234")
        self.store.conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
        self.store.conn.commit()
        with self.assertRaisesRegex(ValueError, "schema version"):
            Store(self.root, ORIGIN)
        self.store.conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
        self.store.conn.commit()
        with tempfile.TemporaryDirectory() as directory:
            conn = sqlite3.connect(Path(directory) / "collector.sqlite3")
            conn.execute("CREATE TABLE unrelated (value TEXT)")
            conn.close()
            with self.assertRaisesRegex(ValueError, "schema metadata"):
                Store(Path(directory), ORIGIN)

    def test_undiscovered_resources_cannot_be_requested_or_requeued(self):
        with self.assertRaisesRegex(ValueError, "undiscovered"):
            self.store.start_attempt("division", "17", 1)
        with self.assertRaisesRegex(ValueError, "undiscovered"):
            self.store.requeue("division", "17")
        self.store.conn.execute("INSERT INTO resources(kind,key) VALUES ('division','17')")
        self.store.conn.commit()
        with self.assertRaisesRegex(ValueError, "source discovery"):
            self.store.start_attempt("division", "17", 1)

    def test_attempt_cannot_finish_twice_or_be_requeued_while_running(self):
        self.store.queue_catalog()
        attempt = self.store.start_attempt("catalog", "upcoming", 1)
        with self.assertRaises(ValueError):
            self.store.requeue("catalog", "upcoming")
        with self.assertRaises(ValueError):
            self.store.start_attempt("catalog", "upcoming", 2)
        self.store.succeed(attempt, parsed(), BODY_HASH, 3)
        with self.assertRaisesRegex(ValueError, "already finished"):
            self.store.succeed(attempt, parsed(), BODY_HASH, 4)
        with self.assertRaisesRegex(ValueError, "already finished"):
            self.store.fail(attempt, "failed", 500, "http_500", BODY_HASH, None, 4)

    def test_validation_rejects_raw_error_text_and_invalid_checkpoints(self):
        self.store.queue_catalog()
        attempt = self.store.start_attempt("catalog", "upcoming", 1)
        with self.assertRaisesRegex(ValueError, "short identifier"):
            self.store.fail(attempt, "failed", 500, '{"token":"private"}', BODY_HASH, None, 2)
        for timestamp in (float("nan"), float("inf"), -1):
            with self.assertRaises(ValueError):
                self.store.set_next_request_at(timestamp)
        with self.assertRaises(ValueError):
            self.store.succeed(attempt, parsed(), "not-a-digest", 2)
        self.assertEqual("running", self.row("catalog", "upcoming")["status"])


if __name__ == "__main__":
    unittest.main()
