"""Offline report checks at the persisted SQLite/evidence boundary."""

from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from scripts.boise_collect.model import Parsed, parse_response
from scripts.boise_collect.report import build_report, write_report
from scripts.boise_collect.store import Store


def game(game_id="100", start="2010-10-01T09:00:00", score=(2, 1)):
    home, away = score
    return {
        "id": game_id, "facility_id": 4, "home_id": "1", "away_id": "2",
        "start_local": start, "start_utc": None, "raw_start": start,
        "raw_end": None, "field": "1", "result": None if home is None else f"{home}-{away}",
        "home_score": home, "away_score": away, "winner_id": None,
        "slot_end": None, "slot_minutes": None,
    }


def standing(team_id="1", division_id="10", wins=1, losses=0, ties=0, gf=2, ga=1, points=12, gp=1):
    return {"division_id": division_id, "team_id": team_id, "position": 1, "games_played": gp,
            "wins": wins, "losses": losses, "ties": ties, "goals_for": gf, "goals_against": ga, "points": points}


def division(division_id="10", games=None, standings=None, *, expected=1, state="present", rules=True, season_id="50", season_number=20):
    games = [game()] if games is None else games
    standings = [standing(division_id=division_id)] if standings is None else standings
    detail = {"id": division_id, "facility_id": 4, "definition_id": "600", "season_id": season_id,
              "season_number": season_number, "name": "Tournament", "expected_games": expected,
              "game_minutes": 35, "show_all_games": True, "show_first_game": True,
              "suppress_leaderboard": False, "raw_est_start": None, "raw_est_end": None,
              "standings_state": state}
    evidence = {"division_season": {"UDivSeasonID": division_id, "FacilityID": 4}, "games": games,
                "teams": None if state == "null" else standings}
    if rules:
        evidence.update(win_point=10, tie_point=5, loss_point=0, goal_point=5)
    return Parsed(evidence=evidence, division=detail,
                  season={"id": season_id, "facility_id": 4, "number": season_number},
                  definition={"id": "600", "facility_id": 4, "name": "Tournament"},
                  games=games, standings=standings,
                  teams=[{"id": row["team_id"], "facility_id": 4, "division_id": division_id,
                          "season_number": season_number, "name": "Team"} for row in standings],
                  discoveries={division_id})


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root, "https://lps-api-prod.lps-test.com/")
        self.addCleanup(self.store.close)
        self.clock = 1000

    def succeed(self, kind, key, parsed):
        self.clock += 2
        resource = self.store.conn.execute("SELECT status FROM resources WHERE kind=? AND key=?", (kind, key)).fetchone()
        if resource and resource["status"] == "success":
            self.store.requeue(kind, key)
        attempt = self.store.start_attempt(kind, key, self.clock)
        return self.store.succeed(attempt, parsed, "a" * 64, self.clock + 1)

    def discover(self, *division_ids):
        self.store.queue_interval(date(2010, 10, 1), date(2010, 10, 1))
        self.succeed("day", "2010-10-01", Parsed(evidence={"data": {}}, discoveries=set(division_ids)))

    def report(self):
        return build_report(self.root, now=2000)

    def source_day(self, *, home_division=None, away_division=None):
        """Ingest a real wire response with optional participant metadata."""
        self.store.queue_interval(date(2010, 10, 1), date(2010, 10, 1))
        source_game = {"UGameID": 100, "FacilityID": 4, "UTeam1": 1, "UTeam2": 2,
                       "SchedGameDateTime": "2010-10-01T09:00:00.000Z", "result": "2-1"}
        for field, team_id, division_id in (("home_team", 1, home_division), ("visitor_team", 2, away_division)):
            source_game[field] = None if division_id is None else {
                "UTeamID": team_id, "FacilityID": 4, "UDivSeasonID": division_id}
        body = {"data": {"1": [{"game": source_game}]}}
        return self.succeed("day", "2010-10-01", parse_response("day", "2010-10-01", body))

    def source_division(self, division_id, team_ids):
        body = {"division_season": {"UDivSeasonID": division_id, "FacilityID": 4},
                "games": [], "teams": [{"UTeamID": team_id, "FacilityID": 4,
                                          "UDivSeasonID": division_id} for team_id in team_ids]}
        return self.succeed("division", str(division_id), parse_response("division", str(division_id), body))

    def source_catalog(self, *division_ids):
        self.store.queue_catalog()
        return self.succeed("catalog", "upcoming", parse_response("catalog", "upcoming", [
            {"UDivSeasonID": division_id, "FacilityID": 4} for division_id in division_ids]))

    def test_successful_day_with_both_team_mappings_missing_is_not_complete_inventory(self):
        self.source_day()
        report = self.report()
        self.assertTrue(report["coverage"]["all_dates_checked"])
        self.assertTrue(report["coverage"]["all_dates_evidenced"])
        self.assertEqual(report["inventory"]["unique_games"], 1)
        self.assertEqual(report["inventory"]["divisions"], 0)
        self.assertFalse(report["inventory_complete_for_declared_interval"])
        self.assertFalse(report["discovery_coverage"]["complete"])
        self.assertEqual(report["discovery_coverage"]["unresolved_participant_count"], 2)
        self.assertEqual({row["team_id"] for row in report["discovery_coverage"]["unresolved_gaps"]}, {"1", "2"})

    def test_one_missing_team_mapping_remains_visible_after_known_division_fetch(self):
        self.source_day(home_division=10)
        self.source_division(10, [1])
        report = self.report()
        self.assertFalse(report["inventory_complete_for_declared_interval"])
        self.assertEqual(report["discovery_coverage"]["participants_with_verified_division_mapping"], 1)
        self.assertEqual(report["discovery_coverage"]["unresolved_gaps"][0]["team_id"], "2")

    def test_another_latest_verified_source_can_resolve_missing_day_mapping(self):
        self.source_day()
        self.source_catalog(10)
        self.source_division(10, [1, 2])
        report = self.report()
        self.assertTrue(report["discovery_coverage"]["complete"])
        self.assertEqual(report["discovery_coverage"]["participants_with_verified_division_mapping"], 2)
        self.assertTrue(report["inventory_complete_for_declared_interval"])
        self.assertFalse(report["all_time_complete"])

    def test_superseded_source_team_mapping_cannot_resolve_discovery_gap(self):
        self.source_day()
        self.source_catalog(10)
        self.source_division(10, [1, 2])
        self.source_division(10, [])
        report = self.report()
        self.assertEqual(report["inventory"]["teams"], 2)
        self.assertFalse(report["discovery_coverage"]["complete"])
        self.assertEqual(report["discovery_coverage"]["unresolved_participant_count"], 2)

    def test_verified_leader_teams_resolve_mappings_when_standings_are_null(self):
        self.source_day()
        self.source_catalog(10)
        body = {
            "division_season": {"UDivSeasonID": 10, "FacilityID": 4},
            "games": [], "teams": None,
            "player_standings": [
                {"UDivSeasonID": 10, "UTeamID": team_id, "UPlayerID": 100 + team_id,
                 "team": {"UTeamID": team_id, "UDivSeasonID": 10, "FacilityID": 4},
                 "player": {"UPlayerID": 100 + team_id, "name": "Private profile name"}}
                for team_id in (1, 2)
            ],
        }
        self.succeed("division", "10", parse_response("division", "10", body))
        report = self.report()
        self.assertEqual(report["division_audits"][0]["standings_state"], "null")
        self.assertTrue(report["discovery_coverage"]["complete"])
        self.assertEqual(report["discovery_coverage"]["participants_with_verified_division_mapping"], 2)
        self.assertTrue(report["inventory_complete_for_declared_interval"])
        self.assertNotIn("Private profile name", json.dumps(report))

    def test_unique_matching_team_division_label_fills_display_name_only(self):
        self.source_day(home_division=10, away_division=10)
        body = {"division_season": {"UDivSeasonID": 10, "FacilityID": 4},
                "division_name": None, "games": [],
                "teams": [{"UTeamID": team_id, "FacilityID": 4, "UDivSeasonID": 10,
                           "division_name": "Sunday Tournament"} for team_id in (1, 2)]}
        self.succeed("division", "10", parse_response("division", "10", body))
        row = self.report()["inventory"]["by_division"][0]
        self.assertEqual(row["name"], "Sunday Tournament")
        self.assertIsNone(row["official_name"])
        self.assertEqual(row["name_source"], "matching_team_metadata")
        stored = json.loads(self.store.conn.execute("SELECT data_json FROM divisions WHERE id='10'").fetchone()[0])
        self.assertIsNone(stored["name"])

    def test_conflicting_matching_team_labels_do_not_fill_display_name(self):
        self.source_day(home_division=10, away_division=10)
        body = {"division_season": {"UDivSeasonID": 10, "FacilityID": 4},
                "division_name": None, "games": [],
                "teams": [{"UTeamID": team_id, "FacilityID": 4, "UDivSeasonID": 10,
                           "division_name": label} for team_id, label in ((1, "Sunday"), (2, "Monday"))]}
        self.succeed("division", "10", parse_response("division", "10", body))
        row = self.report()["inventory"]["by_division"][0]
        self.assertIsNone(row["name"])
        self.assertIsNone(row["name_source"])

    def test_crossover_team_label_cannot_name_different_division(self):
        self.source_day(home_division=10, away_division=10)
        body = {"division_season": {"UDivSeasonID": 10, "FacilityID": 4},
                "division_name": None, "teams": [], "games": [{
                    "UGameID": 100, "FacilityID": 4, "UTeam1": 1, "UTeam2": 2,
                    "SchedGameDateTime": "2010-10-01T09:00:00.000Z", "result": "2-1",
                    "home_team": {"UTeamID": 1, "FacilityID": 4, "UDivSeasonID": 11,
                                  "division_name": "Different Division"},
                    "visitor_team": {"UTeamID": 2, "FacilityID": 4, "UDivSeasonID": 11,
                                     "division_name": "Different Division"}}]}
        self.succeed("division", "10", parse_response("division", "10", body))
        row = self.report()["inventory"]["by_division"][0]
        self.assertIsNone(row["name"])
        self.assertIsNone(row["name_source"])

    def test_conflicting_verified_team_mappings_leave_discovery_gap(self):
        self.source_day(home_division=10, away_division=10)
        self.source_catalog(11)
        self.source_division(10, [2])
        self.source_division(11, [1])
        report = self.report()
        self.assertFalse(report["inventory_complete_for_declared_interval"])
        gap = report["discovery_coverage"]["unresolved_gaps"][0]
        self.assertEqual(gap["team_id"], "1")
        self.assertEqual(gap["candidate_division_ids"], ["10", "11"])
        self.assertEqual(gap["reasons"], ["conflicting_team_division_mappings"])

    def test_legitimate_empty_day_has_no_discovery_gap(self):
        self.store.queue_interval(date(2010, 10, 1), date(2010, 10, 1))
        self.succeed("day", "2010-10-01", parse_response("day", "2010-10-01", {"data": {}}))
        report = self.report()
        self.assertTrue(report["discovery_coverage"]["complete"])
        self.assertEqual(report["discovery_coverage"]["observed_game_participants"], 0)
        self.assertTrue(report["inventory_complete_for_declared_interval"])

    def test_interval_union_counts_empty_success_separately_from_failed_days(self):
        self.store.queue_interval(date(2010, 12, 30), date(2011, 1, 1))
        self.store.queue_interval(date(2010, 12, 31), date(2011, 1, 2))
        self.succeed("day", "2010-12-30", Parsed(evidence={"data": {}}))
        attempt = self.store.start_attempt("day", "2010-12-31", 1010)
        self.store.fail(attempt, "failed", 503, "http_503", "b" * 64, 3000, 1011)
        report = self.report()
        self.assertEqual(report["coverage"]["requested_dates"], 4)
        self.assertEqual(report["coverage"]["dates_with_successful_snapshot"], 1)
        self.assertEqual(report["coverage"]["empty_successful_dates"], 1)
        self.assertEqual(report["coverage"]["dates_unchecked"], 3)
        self.assertEqual([r["requested_dates"] for r in report["coverage"]["by_year"]], [2, 2])
        failed = next(r for r in report["ledger"]["unresolved_resources"] if r["status"] == "failed")
        self.assertEqual(failed["next_attempt_at"], 3000)
        self.assertFalse(report["all_time_complete"])
        self.assertEqual(report["retention_boundary"], "unresolved")

    def test_date_completion_is_not_division_or_all_time_completion(self):
        self.discover("10", "11")
        self.succeed("division", "10", division())
        attempt = self.store.start_attempt("division", "11", 1020)
        self.store.fail(attempt, "missing", 404, "http_404", "b" * 64, None, 1021)
        report = self.report()
        self.assertTrue(report["coverage"]["all_dates_checked"])
        self.assertFalse(report["inventory_complete_for_declared_interval"])
        self.assertEqual(report["ledger"]["resource_states"]["missing"], 1)
        self.assertEqual(report["inventory"]["divisions"], 1)
        self.assertFalse(report["all_time_complete"])

    def test_empty_database_does_not_assert_vacuous_completeness(self):
        report = self.report()
        self.assertFalse(report["coverage"]["all_dates_checked"])
        self.assertFalse(report["inventory_complete_for_declared_interval"])
        self.assertIsNone(report["inventory"]["earliest_observed_game_date"])

    def test_latest_division_snapshot_drives_audit_not_global_game_overwrite(self):
        self.discover("10")
        original = division()
        self.succeed("division", "10", original)
        day_game = game(score=(9, 0))
        self.succeed("day", "2010-10-01", Parsed(evidence={"data": {"1": [{"game": day_game}]}}, games=[day_game], discoveries={"10"}))
        report = self.report()
        team = report["division_audits"][0]["teams"][0]
        self.assertEqual(team["derived_from_numeric_scores"]["goals_for"], 2)
        self.assertEqual(team["derived_from_numeric_scores"]["points"], 12)
        self.assertEqual(team["discrepancies"], [])
        self.assertEqual(report["source_conflicts"][0]["game_id"], "100")
        self.assertIn("home_score", report["source_conflicts"][0]["differences"])

    def test_earlier_versions_of_same_endpoint_do_not_create_conflicts(self):
        self.discover("10")
        self.succeed("division", "10", division())
        new = division(games=[game(score=(3, 1))], standings=[standing(gf=3, points=13)])
        self.succeed("division", "10", new)
        report = self.report()
        self.assertEqual(report["source_conflicts"], [])
        self.assertEqual(report["division_audits"][0]["teams"][0]["derived_from_numeric_scores"]["goals_for"], 3)

    def test_failed_refresh_preserves_previous_audit_and_marks_stale(self):
        self.discover("10")
        snapshot = self.succeed("division", "10", division())
        self.store.requeue("division", "10")
        attempt = self.store.start_attempt("division", "10", 1030)
        self.store.fail(attempt, "failed", 503, "http_503", "b" * 64, 3000, 1031)
        report = self.report()
        audit = report["division_audits"][0]
        self.assertTrue(audit["stale_after_refresh_failure"])
        self.assertEqual(audit["snapshot_id"], snapshot)
        self.assertEqual(audit["games"], 1)
        self.assertEqual(len(report["ledger"]["stale_resources"]), 1)
        self.assertFalse(report["inventory_complete_for_declared_interval"])

    def test_official_double_loss_is_retained_while_numeric_draw_differs(self):
        self.discover("10")
        official = [standing("1", wins=0, losses=1, gf=0, ga=0, points=0),
                    standing("2", wins=0, losses=1, gf=0, ga=0, points=0)]
        self.succeed("division", "10", division(games=[game(score=(0, 0))], standings=official))
        report = self.report()
        for team in report["division_audits"][0]["teams"]:
            self.assertEqual(team["official"]["losses"], 1)
            self.assertEqual(team["official"]["ties"], 0)
            self.assertEqual(team["derived_from_numeric_scores"]["ties"], 1)
            self.assertEqual(team["derived_from_numeric_scores"]["points"], 5)
            self.assertIn("derived_ties", {r["metric"] for r in team["discrepancies"]})
        self.assertNotIn("forfeit", report["division_audits"][0])

    def test_null_empty_unclassified_and_unknown_points_remain_distinct(self):
        self.discover("10", "11", "12")
        self.succeed("division", "10", division(games=[], standings=[], state="null", rules=False))
        self.succeed("division", "11", division("11", games=[], standings=[], rules=False))
        odd = game("101", start=None, score=(None, None))
        odd["result"] = "Cancelled"
        self.succeed("division", "12", division("12", games=[odd], standings=[standing(division_id="12", gp=0, wins=0, gf=0, ga=0, points=0)], rules=False))
        report = self.report()
        audits = {a["division_id"]: a for a in report["division_audits"]}
        self.assertEqual(audits["10"]["standings_state"], "null")
        self.assertEqual(audits["11"]["standings_state"], "empty")
        self.assertEqual(audits["12"]["unclassified_games"], 1)
        self.assertEqual(audits["12"]["undated_games"], 1)
        self.assertIsNone(audits["12"]["teams"][0]["derived_from_numeric_scores"]["points"])
        self.assertEqual(audits["12"]["teams"][0]["derived_points_state"], "unknown_scoring_rules")
        self.assertEqual(report["inventory"]["by_year"][0]["year"], "unknown")

    def test_tournament_expected_count_difference_is_not_automatic_correction(self):
        self.discover("10")
        self.succeed("division", "10", division(expected=8))
        team = self.report()["division_audits"][0]["teams"][0]
        self.assertEqual(team["expected_games"], 8)
        self.assertEqual(team["official"]["games_played"], 1)
        difference = next(d for d in team["discrepancies"] if d["metric"] == "scheduled_vs_expected_games")
        self.assertEqual(difference["difference"], -7)
        self.assertIn("tournaments", difference["interpretation"])

    def test_crossover_opponent_has_counts_without_invented_official_standing(self):
        self.discover("10")
        self.succeed("division", "10", division(expected=8))
        audit = self.report()["division_audits"][0]
        outsider = next(team for team in audit["teams"] if team["team_id"] == "2")
        self.assertEqual(outsider["scored_games"], 1)
        self.assertFalse(outsider["official_standing_present"])
        self.assertIsNone(outsider["official"]["games_played"])
        self.assertIsNone(outsider["expected_games"])
        self.assertEqual(outsider["discrepancies"], [])

    def test_missing_and_corrupt_evidence_flag_incomplete_without_erasing_inventory(self):
        self.discover("10")
        self.succeed("division", "10", division())
        snapshots = self.store.conn.execute("SELECT kind,evidence_path FROM snapshots ORDER BY id").fetchall()
        (self.root / snapshots[0]["evidence_path"]).unlink()
        (self.root / snapshots[1]["evidence_path"]).write_text("corrupt", encoding="utf-8")
        report = self.report()
        self.assertEqual({r["problem"] for r in report["evidence"]["issues"]}, {"missing_file", "hash_mismatch"})
        self.assertTrue(report["coverage"]["all_dates_checked"])
        self.assertFalse(report["coverage"]["all_dates_evidenced"])
        self.assertFalse(report["inventory_complete_for_declared_interval"])
        self.assertEqual(report["inventory"]["unique_games"], 1)
        self.assertFalse(report["division_audits"][0]["evidence_verified"])

    def test_cross_era_inventory_uses_game_dates_not_bad_season_labels(self):
        self.discover("10", "11")
        self.succeed("division", "10", division())
        future = division("11", games=[game("200", "2026-12-31T23:00:00", (None, None)), game("201", "2027-01-01T01:00:00", (None, None))], standings=[standing(division_id="11", gp=0, wins=0, gf=0, ga=0, points=0)], season_id="60", season_number=175)
        future.division["raw_est_start"] = "1926-12-31"
        self.succeed("division", "11", future)
        report = self.report()
        self.assertEqual([r["year"] for r in report["inventory"]["by_year"]], ["2010", "2026", "2027"])
        self.assertEqual(report["inventory"]["earliest_observed_game_date"], "2010-10-01")
        self.assertEqual(report["inventory"]["latest_observed_game_date"], "2027-01-01")
        self.assertEqual(len(report["inventory"]["by_source_season"]), 2)

    def test_report_writes_json_and_markdown_without_mutating_database(self):
        self.discover("10")
        self.succeed("division", "10", division(expected=8))
        before = list(self.store.conn.iterdump())
        output = self.root / "report"
        result = write_report(self.root, output, now=2000)
        self.assertEqual(json.loads((output / "coverage.json").read_text()), result)
        text = (output / "coverage.md").read_text()
        self.assertIn("Individual discrepancy appendix", text)
        self.assertIn("All-time completeness: unproven", text)
        self.assertEqual(list(self.store.conn.iterdump()), before)

    def test_missing_database_is_not_created_by_report(self):
        absent = self.root / "absent"
        with self.assertRaises(FileNotFoundError):
            build_report(absent)
        self.assertFalse(absent.exists())


if __name__ == "__main__":
    unittest.main()
