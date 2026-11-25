"""Read-only explorer behavior over a small archive with crossover and missing data."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts.boise_collect.explorer_data import Archive, QueryError, RecordNotFound
from scripts.boise_collect.model import parse_response
from scripts.boise_collect.report import write_report
from scripts.boise_collect.store import Store


DAY = "2010-09-26"


def team(ident, division, season, name):
    return {"UTeamID": ident, "FacilityID": 4, "UDivSeasonID": division,
            "Season": season, "team_name": name, "division_name": f"League {division}"}


TEAMS = {
    31: team(31, 11, 75, "Repeated name"),
    32: team(32, 12, 76, "Opponent"),
    33: team(33, 13, 75, "Repeated name"),
}


def game(ident=101, home=31, away=32, hour=10, result="2 - 1"):
    return {"UGameID": ident, "FacilityID": 4, "UTeam1": home, "UTeam2": away,
            "SchedGameDateTime": f"{DAY}T{hour:02}:00:00.000Z", "schedGameEndTime": None,
            "Field": 1, "result": result, "winner_id": None,
            "home_team": deepcopy(TEAMS[home]), "visitor_team": deepcopy(TEAMS[away])}


def division(ident, goals, result="2 - 1"):
    own = 31 if ident == 11 else 32
    season, season_id = (75, 327) if ident == 11 else (76, 328)
    leaders = [{"UDivSeasonID": ident, "UTeamID": own, "UPlayerID": 7,
                "goals": goals, "assists": 1, "points": goals + 1}]
    if ident == 12:
        leaders.append({"UDivSeasonID": ident, "UTeamID": own, "UPlayerID": 8,
                        "goals": None, "assists": 0, "points": 0})
    return {"division_name": None,
            "division_season": {"UDivSeasonID": ident, "FacilityID": 4, "UDivisionID": ident + 200,
                                "season_id": season_id, "Season": season, "NumGames": 1,
                                "GameLength": 50, "est_start_date": "1910-01-01T00:00:00Z"},
            "games": [game(result=result)],
            "teams": [{**deepcopy(TEAMS[own]), "gamesplayed": 1, "wins": 1, "losses": 0,
                       "ties": 0, "goalsfor": 2, "goalsagainst": 1, "points": goals, "position": 1}],
            "player_standings": leaders, "win_point": 10, "tie_point": 5, "loss_point": 0, "goal_point": 5}


class ExplorerDataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.now = 100.0
        self.store = Store(self.root, "http://127.0.0.1:12345")
        self.store.queue_interval(date.fromisoformat(DAY), date.fromisoformat(DAY))
        body = {"data": {"1": [
            {"game": game(), "field": 1, "duration": 50, "end": DAY + "T10:50:00.000Z"},
            {"game": game(102, 33, 31, 11, ""), "field": 1, "duration": 50, "end": DAY + "T11:50:00.000Z"},
        ]}}
        self.succeed("day", DAY, body)
        self.succeed("division", "11", division(11, 2))
        self.succeed("division", "12", division(12, 3))
        self.store.requeue("division", "11")
        self.succeed("division", "11", division(11, 5))
        attempt = self.store.start_attempt("division", "13", self.now)
        self.store.fail(attempt, "failed", 500, "http_500", None, None, self.now)
        self.store.close()
        write_report(self.root, self.root / "reports")
        self.archive = Archive(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def succeed(self, kind, key, body):
        self.now += 1
        attempt = self.store.start_attempt(kind, key, self.now)
        raw = json.dumps(body).encode()
        self.store.succeed(attempt, parse_response(kind, key, body), hashlib.sha256(raw).hexdigest(), self.now)

    def hashes(self):
        return {str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in [self.root / "collector.sqlite3", *sorted((self.root / "evidence").glob("*.json"))]}

    def test_global_games_include_daily_only_facts_and_either_crossover_participant(self):
        self.assertEqual(self.archive.overview()["counts"], {"games": 2, "teams": 3, "players": 2, "sessions": 2, "divisions": 3})
        self.assertEqual(self.archive.list_items("games", {})["total"], 2)
        self.assertEqual(self.archive.list_items("games", {"session": "327"})["total"], 2)
        self.assertEqual(self.archive.list_items("games", {"session": "328"})["total"], 1)
        self.assertEqual(self.archive.list_items("games", {"division": "13"})["items"][0]["id"], "102")
        self.assertEqual(self.archive.list_items("games", {"team": "33"})["total"], 1)
        missing = self.archive.detail("divisions", "13")
        self.assertFalse(missing["item"]["details_available"])
        self.assertEqual(missing["item"]["session_id"], "327")
        self.assertEqual(missing["item"]["session_number"], 75)
        self.assertEqual(missing["item"]["game_count"], 1)
        self.assertEqual(missing["standings"], [])
        self.assertEqual(self.archive.detail("sessions", "327")["item"]["division_count"], 2)

    def test_team_filter_uses_game_ids_when_registration_metadata_is_missing(self):
        sparse = game(103)
        del sparse["home_team"], sparse["visitor_team"]
        sparse.update(UTeam1=41, UTeam2=42)
        body = {"data": {"1": [{"game": sparse, "field": 1}]}}
        with closing(Store(self.root, "http://127.0.0.1:12345")) as store:
            store.requeue("day", DAY)
            attempt = store.start_attempt("day", DAY, 300)
            store.succeed(attempt, parse_response("day", DAY, body), "c" * 64, 301)
        self.archive.reload()
        self.assertEqual(self.archive.list_items("games", {})["total"], 3)
        for participant in ("41", "42"):
            with self.subTest(participant=participant):
                matches = self.archive.list_items("games", {"team": participant})
                self.assertEqual([item["id"] for item in matches["items"]], ["103"])

    def test_archive_opens_without_sqlite_extension_loading_support(self):
        class NoExtensionSupport(sqlite3.Connection):
            @property
            def enable_load_extension(self):
                raise AttributeError("SQLite extension loading unavailable")

        connect = sqlite3.connect
        with patch("scripts.boise_collect.explorer_data.sqlite3.connect",
                   side_effect=lambda *a, **kw: connect(*a, factory=NoExtensionSupport, **kw)):
            archive = Archive(self.root)
            self.assertEqual(archive.sql("SELECT COUNT(*) FROM games")["rows"], [[2]])
            with self.assertRaises(QueryError):
                archive.sql("DELETE FROM games")

    def test_names_use_verified_report_labels_and_dates_use_observations(self):
        self.assertEqual(self.archive.detail("divisions", "11")["item"]["name"], "League 11")
        session = self.archive.detail("sessions", "327")["item"]
        self.assertEqual((session["start_date"], session["end_date"]), (DAY, DAY))
        self.assertEqual(self.archive.list_items("sessions", {"year": "2010"})["total"], 2)
        self.assertEqual(self.archive.list_items("sessions", {"year": "1910"})["total"], 0)
        self.assertEqual(self.archive.overview()["years"], [2010])
        self.assertTrue(self.archive.coverage()["report_fresh"])

    def test_leaders_and_standings_use_latest_snapshots_without_appearance_claims(self):
        player = self.archive.detail("players", "7")
        self.assertEqual(player["item"]["entry_count"], 2)
        self.assertEqual(player["item"]["recorded_goals"], 8)
        self.assertEqual(player["item"]["team_count"], 2)
        self.assertEqual(player["item"]["session_count"], 2)
        self.assertNotIn("games", player)
        self.assertNotIn("name", player["item"])
        self.assertIn("appearances are unavailable", player["scope"])
        self.assertIsNone(self.archive.detail("players", "8")["item"]["recorded_goals"])
        team_detail = self.archive.detail("teams", "31")
        self.assertEqual(team_detail["standing"]["points"], 5)
        self.assertEqual([row["goals"] for row in team_detail["leaders"]], [5])
        self.assertEqual([row["id"] for row in team_detail["other_registrations"]], ["33"])
        self.assertIn("not established club continuity", team_detail["other_registrations_note"])
        self.assertEqual(self.archive.list_items("teams", {"player": "7"})["total"], 2)
        with self.assertRaises(QueryError):
            self.archive.list_items("games", {"player": "7"})

    def test_player_list_totals_follow_the_selected_entry_filters(self):
        selected = self.archive.list_items("players", {"session": "327"})
        self.assertEqual(selected["total"], 1)
        self.assertEqual(selected["items"][0]["recorded_goals"], 5)
        self.assertEqual(selected["items"][0]["entry_count"], 1)
        self.assertEqual(selected["items"][0]["session_count"], 1)
        self.assertEqual(self.archive.list_items("players", {"division": "12", "team": "31"})["total"], 0)
        self.assertEqual(self.archive.detail("players", "7")["item"]["recorded_goals"], 8)

    def test_filters_pagination_and_invalid_input_are_explicit(self):
        first = self.archive.list_items("games", {"limit": "1"})
        second = self.archive.list_items("games", {"limit": "1", "offset": "1"})
        self.assertEqual(first["total"], 2)
        self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])
        self.assertEqual(self.archive.list_items("games", {"status": "unscored"})["items"][0]["id"], "102")
        self.assertEqual(self.archive.list_items("games", {"from": DAY, "to": DAY, "status": "scored"})["total"], 1)
        self.assertEqual(self.archive.list_items("teams", {"search": "repeated"})["total"], 2)
        self.assertEqual(self.archive.list_items("teams", {"search": "' OR 1=1 --"})["total"], 0)
        for params in ({"limit": "201"}, {"offset": "-1"}, {"year": "2010 OR 1"}, {"session": "327;DROP"},
                       {"from": "2010-02-30"}, {"from": "2011-01-01", "to": DAY}, {"status": "all"},
                       {"unknown": "x"}, {"limit": 2}):
            with self.subTest(params=params), self.assertRaises(QueryError):
                self.archive.list_items("games", params)
        with self.assertRaises(RecordNotFound):
            self.archive.detail("teams", "999")

    def test_saved_timing_is_exposed_separately_from_the_latest_record(self):
        before = self.hashes()
        detail = self.archive.detail("games", "101")
        self.assertIsNone(detail["record"]["slot_end"])
        timing = detail["item"]["timing"]
        self.assertIsNone(timing["latest_slot_end"])
        self.assertEqual(timing["known_slot_end"], DAY + "T10:50:00.000Z")
        self.assertEqual(timing["known_slot_minutes"], 50)
        self.assertEqual(timing["provenance"], "earlier_observation")
        self.assertEqual(timing["sources"][0]["kind"], "day")
        self.assertEqual(self.archive.list_items("games", {"field": "1"})["total"], 2)
        self.assertEqual(self.archive.list_items("games", {"field": "2"})["total"], 0)
        with self.assertRaises(QueryError):
            self.archive.list_items("games", {"field": "1 OR 1"})
        self.assertEqual(before, self.hashes())

    def test_timing_conflicts_and_changed_fixtures_do_not_fill_missing_values(self):
        with closing(sqlite3.connect(self.root / "collector.sqlite3")) as db:
            row = db.execute("SELECT data_json FROM game_observations WHERE game_id='101' AND snapshot_id=2").fetchone()
            changed = json.loads(row[0])
            changed.update(slot_end=DAY + "T10:45:00.000Z", slot_minutes=45)
            db.execute("UPDATE game_observations SET data_json=? WHERE game_id='101' AND snapshot_id=2", (json.dumps(changed),))
            db.commit()
        self.archive.reload()
        timing = self.archive.detail("games", "101")["item"]["timing"]
        self.assertEqual(timing["provenance"], "conflict")
        self.assertIsNone(timing["known_slot_end"])
        with closing(sqlite3.connect(self.root / "collector.sqlite3")) as db:
            record = json.loads(db.execute("SELECT data_json FROM games WHERE id='101'").fetchone()[0])
            record["start_local"] = DAY + "T14:00:00"
            db.execute("UPDATE games SET data_json=? WHERE id='101'", (json.dumps(record),))
            db.commit()
        self.archive.reload()
        self.assertEqual(self.archive.detail("games", "101")["item"]["timing"]["provenance"], "unknown")

    def test_legacy_evidence_metadata_is_verified_and_has_sources(self):
        store = Store(self.root, "http://127.0.0.1:12345")
        body = division(11, 5)
        body["division_season"].update(DeadlineDate="2010-09-01", suppress_leaderboard=False)
        body["games"][0]["home_team"].update(Dropped=False, div_code="COED", Color="GREEN")
        store.requeue("division", "11")
        attempt = store.start_attempt("division", "11", 200)
        snapshot = store.succeed(attempt, parse_response("division", "11", body), "a" * 64, 201)
        with store.conn:
            record = json.loads(store.conn.execute("SELECT data_json FROM teams WHERE id='31'").fetchone()[0])
            for key in ("dropped", "div_code", "division_name"):
                record.pop(key, None)
            store.conn.execute("UPDATE teams SET data_json=? WHERE id='31'", (json.dumps(record),))
        store.close()
        self.archive.reload()
        team = self.archive.detail("teams", "31")["item"]
        self.assertIs(team["metadata"]["dropped"], False)
        self.assertEqual(team["metadata"]["div_code"], "COED")
        self.assertEqual(team["metadata_sources"]["dropped"]["snapshot_id"], snapshot)
        own = self.archive.detail("divisions", "11")["item"]
        self.assertEqual(own["metadata"]["deadline"], "2010-09-01")
        self.assertEqual(own["metadata"]["scoring_rules"]["win_point"], 10)
        self.assertTrue(own["metadata_warnings"])
        with closing(sqlite3.connect(self.root / "collector.sqlite3")) as db:
            path = db.execute("SELECT evidence_path FROM snapshots WHERE id=?", (snapshot,)).fetchone()[0]
        (self.root / path).write_text('{}\n')
        self.archive.reload()
        self.assertEqual(self.archive.coverage()["metadata_evidence_issues"], 1)
        self.assertNotIn("dropped", self.archive.detail("teams", "31")["item"]["metadata"])

    def test_latest_duration_is_preserved_and_incompatible_old_end_is_rejected(self):
        store = Store(self.root, "http://127.0.0.1:12345")
        try:
            for minutes, expected_source, expected_end in ((60, "conflict", None),
                    (50, "earlier_observation", DAY + "T10:50:00.000Z")):
                store.requeue("day", DAY)
                body = {"data": {"1": [{"game": game(), "field": 1, "duration": minutes}]}}
                attempt = store.start_attempt("day", DAY, 400 - minutes)
                store.succeed(attempt, parse_response("day", DAY, body), "c" * 64, 401 - minutes)
                self.archive.reload()
                timing = self.archive.detail("games", "101")["item"]["timing"]
                self.assertEqual(timing["latest_slot_minutes"], minutes)
                self.assertEqual(timing["known_slot_minutes"], minutes)
                self.assertEqual(timing["known_slot_end"], expected_end)
                self.assertEqual(timing["provenance"], expected_source)
        finally:
            store.close()

    def test_catalog_entities_and_metadata_keep_detail_and_observation_scopes(self):
        store = Store(self.root, "http://127.0.0.1:12345")
        try:
            original = store.conn.execute("SELECT last_success_snapshot_id FROM resources WHERE kind='division' AND key='11'").fetchone()[0]
            store.queue_catalog()
            body = [{"UDivSeasonID": ident, "GameLength": 50, "NumGames": 8,
                     "team_fee": "625.00", "DeadlineDate": "2026-11-11",
                     "season": {"id": sid, "SeasonNumber": number, "StartDate": "1926-11-02",
                                "EndDate": "1927-01-03", "Open": True},
                     "division": {"UDivisionID": ident + 200, "DivisionName": "Catalog league",
                                  "DivisionCode": "C3", "DivClass": "COED", "PlayerType": "Adult",
                                  "Gender": "Coed", "Age": "O18"}}
                    for ident, sid, number in ((11, 327, 75), (44, 900, 177))]
            attempt = store.start_attempt("catalog", "upcoming", 300)
            snapshot = store.succeed(attempt, parse_response("catalog", "upcoming", body), "b" * 64, 301)
            store.begin_refresh("example-refresh", date.fromisoformat(DAY), date.fromisoformat(DAY), False, 302)
        finally:
            store.close()
        self.archive.reload()
        new = self.archive.detail("divisions", "44")["item"]
        self.assertFalse(new["details_available"])
        self.assertTrue(new["catalog_only"])
        self.assertEqual(new["game_count"], 0)
        self.assertEqual(new["metadata"]["code"], "C3")
        self.assertEqual(new["metadata"]["fees"]["team"], "625.00")
        self.assertEqual(new["metadata_sources"]["fees.team"]["snapshot_id"], snapshot)
        self.assertTrue(self.archive.detail("sessions", "900")["item"]["catalog_only"])
        session = self.archive.detail("sessions", "327")["item"]
        self.assertEqual(session["start_date"], DAY)
        self.assertEqual(session["metadata"]["raw_start"], "1926-11-02")
        self.assertTrue(session["metadata_warnings"])
        existing = self.archive.detail("divisions", "11")
        self.assertTrue(existing["item"]["details_available"])
        self.assertEqual(existing["standings"][0]["snapshot_id"], original)
        self.assertEqual(existing["item"]["metadata_sources"]["code"]["snapshot_id"], snapshot)
        self.assertEqual(self.archive.list_items("divisions", {"search": "O18"})["total"], 2)
        self.assertEqual(self.archive.overview()["catalog"]["division_count"], 2)
        job = self.archive.coverage()["freshness"]["refresh_job"]
        self.assertEqual((job["id"], job["status"], job["pending"], job["skipped"]),
                         ("example-refresh", "active", 3, 1))

    def test_known_source_game_dates_survive_a_sparse_detail_refresh(self):
        store = Store(self.root, "http://127.0.0.1:12345")
        try:
            snapshot = None
            for moment, include_dates in ((500, True), (510, False)):
                body = division(11, 5)
                if include_dates:
                    body["division_season"].update(FirstGameDate=DAY, LastGameDate=DAY)
                store.requeue("division", "11")
                attempt = store.start_attempt("division", "11", moment)
                current = store.succeed(attempt, parse_response("division", "11", body), "d" * 64, moment + 1)
                if include_dates:
                    snapshot = current
        finally:
            store.close()
        self.archive.reload()
        item = self.archive.detail("divisions", "11")["item"]
        self.assertEqual(item["metadata"]["first_game_date"], DAY)
        self.assertEqual(item["metadata"]["last_game_date"], DAY)
        self.assertEqual(item["metadata_sources"]["first_game_date"]["snapshot_id"], snapshot)

    def test_sql_is_read_only_and_schema_is_available(self):
        before = self.hashes()
        target = str(self.root / "unwanted.sqlite").replace("'", "''")
        statements = [
            "DELETE FROM games", "UPDATE games SET result='9 - 9'", "DROP TABLE games",
            "CREATE TABLE unwanted(id)", "PRAGMA query_only=OFF", "PRAGMA writable_schema=ON",
            f"ATTACH DATABASE '{target}' AS other", f"VACUUM INTO '{target}'",
            "SELECT load_extension('unwanted')", "SELECT 1; DELETE FROM games",
        ]
        for statement in statements:
            with self.subTest(statement=statement), self.assertRaises(QueryError):
                self.archive.sql(statement)
        result = self.archive.sql("SELECT count(*) AS games FROM games")
        self.assertEqual(result["columns"], ["games"])
        self.assertEqual(result["rows"], [[2]])
        self.assertFalse(result["truncated"])
        self.assertIn("games", {table["name"] for table in self.archive.schema()["tables"]})
        self.assertEqual(before, self.hashes())
        self.assertFalse((self.root / "unwanted.sqlite").exists())
        json.dumps(self.archive.sql("SELECT 1e999"), allow_nan=False)

    def test_sql_limits_rows_cells_output_and_execution_time(self):
        query = "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<700) SELECT x FROM n"
        result = self.archive.sql(query)
        self.assertEqual(len(result["rows"]), 500)
        self.assertTrue(result["truncated"])
        for query in ("SELECT '" + "x" * 20_000 + "'", "SELECT printf('%100000s','x')", "SELECT zeroblob(2000000)", "SELECT '\ud800'"):
            with self.subTest(length=len(query)), self.assertRaises(QueryError):
                self.archive.sql(query)
        with patch("scripts.boise_collect.explorer_data.SQL_OUTPUT_LIMIT", 4096):
            result = self.archive.sql("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<100) SELECT printf('%1000s','x') FROM n")
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(json.dumps(result).encode()), 4096)
        with patch("scripts.boise_collect.explorer_data.SQL_TIMEOUT", 0.01):
            with self.assertRaisesRegex(QueryError, "execution limit"):
                self.archive.sql("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n")

    def test_sql_errors_explain_typos_but_keep_denials_and_paths_generic(self):
        for statement, expected in (
            ("SELECT game_cout FROM games", "no such column: game_cout"),
            ("SELECT id FROM gaems", "no such table: gaems"),
            ("SELECT FROM games", 'near "FROM": syntax error'),
        ):
            with self.subTest(statement=statement), self.assertRaises(QueryError) as raised:
                self.archive.sql(statement)
            self.assertIn(expected, str(raised.exception))
        with self.assertRaises(QueryError) as raised:
            self.archive.sql("SELECT " + "x" * 400 + " FROM games")
        self.assertLessEqual(len(str(raised.exception)), 240)
        for statement in ("DELETE FROM games", "SELECT load_extension('/private/hidden')",
                          'SELECT * FROM "/private/hidden.sqlite"'):
            with self.subTest(statement=statement), self.assertRaises(QueryError) as raised:
                self.archive.sql(statement)
            self.assertNotIn("/private", str(raised.exception))
            self.assertNotIn("load_extension", str(raised.exception))
            self.assertIn("bounded SELECT or WITH", str(raised.exception))

    def test_reload_is_atomic_and_old_details_keep_their_snapshot(self):
        self.store = Store(self.root, "http://127.0.0.1:12345")
        self.store.requeue("division", "11")
        self.succeed("division", "11", division(11, 9, "9 - 1"))
        self.store.close()
        self.assertTrue(self.archive.overview()["archive"]["source_changed"])
        old = self.archive.detail("games", "101")
        self.assertEqual(old["item"]["home_score"], 2)
        self.assertEqual(old["record"]["home_score"], 2)
        self.assertTrue(all(row["record"]["home_score"] == 2 for row in old["observations"]))
        with ThreadPoolExecutor(max_workers=4) as executor:
            calls = [executor.submit(self.archive.overview), executor.submit(self.archive.reload),
                     executor.submit(self.archive.list_items, "games", {}),
                     executor.submit(self.archive.sql, "SELECT count(*) FROM games")]
            for call in calls:
                self.assertIsInstance(call.result(), dict)
        self.assertEqual(self.archive.detail("games", "101")["record"]["home_score"], 9)
        self.assertFalse(self.archive.coverage()["report_fresh"])
        self.assertFalse(self.archive.overview()["archive"]["source_changed"])


if __name__ == "__main__":
    unittest.main()
