"""Offline behavior tests for the public wire boundary; fixtures are excerpts."""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from scripts.boise_collect.model import Parsed, ValidationError, parse_response


FIXTURES = Path(__file__).with_name("fixtures")


def fixture(name="114512"):
    return json.loads((FIXTURES / f"model-division-{name}.json").read_text())


def team(ident, division=10):
    return {"UTeamID": ident, "FacilityID": 4, "UDivSeasonID": division,
            "team_name": f"Team {ident}", "Season": 175}


def game(ident=1, start="2026-10-12T17:00:00.000Z", result=""):
    return {"UGameID": ident, "FacilityID": 4, "UTeam1": 101, "UTeam2": 102,
            "SchedGameDateTime": start, "schedGameEndTime": None, "Field": 1,
            "result": result, "winner_id": None,
            "home_team": team(101), "visitor_team": team(102, 11)}


def day_body():
    return {"data": {"1": [{"game": game(), "field": 1, "duration": 35,
                             "time_to_order": "2026-10-12T17:00:00.000Z",
                             "end": "2026-10-12T17:35:00.000Z"}]}}


def overnight_day_body():
    slots = []
    for ident, start, end in (
        (1, "2026-10-12T23:30:00.000Z", "2026-10-13T00:20:00.000Z"),
        (2, "2026-10-13T00:20:00.000Z", "2026-10-13T01:10:00.000Z"),
    ):
        slots.append({"game": game(ident=ident, start=start), "field": 1,
                      "duration": 50, "time_to_order": start, "end": end})
    return {"data": {"1": slots}, "open_at": "2026-10-12T16:00:00.000Z",
            "close_at": "2026-10-12T23:59:00.000Z"}


def division_body():
    return {"division_season": {"UDivSeasonID": 10, "FacilityID": 4},
            "games": [], "teams": None}


class ModelTests(unittest.TestCase):
    def test_representative_eras_and_tournament_are_retained(self):
        for name, minutes, year in (("2010", 50, "2010"), ("114512", 45, "2026"), ("114742", 35, "2026")):
            with self.subTest(name=name):
                source = fixture(name)
                parsed = parse_response("division", str(source["division_season"]["UDivSeasonID"]), source)
                self.assertIsInstance(parsed, Parsed)
                self.assertEqual(parsed.division["game_minutes"], minutes)
                self.assertEqual(len(parsed.games), 3)
                self.assertTrue(all(x["start_local"].startswith(year) for x in parsed.games))
                self.assertEqual(parsed.evidence["win_point"], source["win_point"])
                self.assertEqual(parsed.standings[0]["games_played"], source["teams"][0]["gamesplayed"])
                self.assertEqual(parsed.division["standings_state"], "present")
        tournament = parse_response("division", "114742", fixture("114742"))
        self.assertTrue(tournament.division["suppress_leaderboard"])
        self.assertIsNone(tournament.division["name"])
        self.assertEqual(tournament.leaders, [])
        self.assertTrue(tournament.division["raw_est_start"].startswith("1926"))

    def test_day_uses_wall_time_and_keeps_checked_slot_duration(self):
        parsed = parse_response("day", "2026-10-12", day_body())
        row = parsed.games[0]
        self.assertEqual(row["start_local"], "2026-10-12T17:00:00")
        self.assertEqual(row["start_utc"], "2026-10-12T23:00:00Z")
        self.assertEqual(row["slot_minutes"], 35)
        self.assertEqual(row["slot_end"], "2026-10-12T17:35:00.000Z")
        self.assertEqual(parsed.discoveries, {"10", "11"})
        self.assertEqual(len(parsed.teams), 2)

    def test_day_retains_overnight_games_without_rewriting_dates(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                source = overnight_day_body()
                if reverse:
                    source["data"]["1"].reverse()
                original = deepcopy(source)
                parsed = parse_response("day", "2026-10-12", source)
                games = {row["id"]: row for row in parsed.games}
                self.assertEqual(set(games), {"1", "2"})
                self.assertEqual(games["2"]["start_local"], "2026-10-13T00:20:00")
                self.assertEqual(games["2"]["start_utc"], "2026-10-13T06:20:00Z")
                self.assertEqual(games["2"]["slot_minutes"], 50)
                self.assertEqual(games["2"]["slot_end"], "2026-10-13T01:10:00.000Z")
                self.assertEqual(parsed.evidence, original)
                self.assertEqual(source, original)
                self.assertEqual(parsed.discoveries, {"10", "11"})
                self.assertTrue(any("game 2" in warning and "next-day" in warning
                                    for warning in parsed.warnings))

    def test_next_day_game_with_a_gap_uses_dated_metadata_and_same_field_anchor(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                source = overnight_day_body()
                first = source["data"]["1"][0]
                first["game"]["SchedGameDateTime"] = first["time_to_order"] = "2026-10-12T22:40:00.000Z"
                first["end"] = "2026-10-12T23:30:00.000Z"
                if reverse:
                    source["data"]["1"].reverse()
                original = deepcopy(source)
                parsed = parse_response("day", "2026-10-12", source)
                self.assertEqual({row["id"] for row in parsed.games}, {"1", "2"})
                self.assertEqual(parsed.evidence, original)
                self.assertEqual(source, original)
                self.assertTrue(any("game 2" in warning and "next-day" in warning
                                    for warning in parsed.warnings))

    def test_multiple_next_day_games_do_not_need_continuity_or_inferred_end_times(self):
        source = overnight_day_body()
        del source["data"]["1"][0]["end"]
        third = deepcopy(source["data"]["1"][1])
        third["game"]["UGameID"] = 3
        third["game"]["SchedGameDateTime"] = third["time_to_order"] = "2026-10-13T01:30:00.000Z"
        third["end"] = "2026-10-13T02:20:00.000Z"
        source["data"]["1"].insert(0, third)
        parsed = parse_response("day", "2026-10-12", source)
        self.assertEqual({row["id"] for row in parsed.games}, {"1", "2", "3"})
        self.assertEqual(sum("next-day" in warning for warning in parsed.warnings), 2)
        self.assertEqual(parsed.evidence, source)

    def test_missing_same_field_anchor_and_nonadjacent_days_still_fail(self):
        for scenario in ("isolated", "cross_field", "multi_day", "prior_day", "non_game", "next_day_anchor"):
            with self.subTest(scenario=scenario):
                source = overnight_day_body()
                first, second = source["data"]["1"]
                if scenario == "isolated":
                    source["data"]["1"] = [second]
                elif scenario == "cross_field":
                    source["data"]["1"] = [first]
                    source["data"]["2"] = [second]
                    second["field"] = second["game"]["Field"] = 2
                elif scenario == "multi_day":
                    second["game"]["SchedGameDateTime"] = second["time_to_order"] = "2026-10-14T00:20:00.000Z"
                    second["end"] = "2026-10-14T01:10:00.000Z"
                elif scenario == "prior_day":
                    second["game"]["SchedGameDateTime"] = second["time_to_order"] = "2026-10-11T00:20:00.000Z"
                    second["end"] = "2026-10-11T01:10:00.000Z"
                elif scenario == "next_day_anchor":
                    first["game"]["SchedGameDateTime"] = first["time_to_order"] = "2026-10-13T00:00:00.000Z"
                    first["end"] = "2026-10-13T00:50:00.000Z"
                else:
                    del first["game"]
                    first["practice"] = {"contact": "private"}
                with self.assertRaisesRegex(ValidationError, "game date"):
                    parse_response("day", "2026-10-12", source)

    def test_next_day_games_require_both_date_metadata_anchors(self):
        for name in ("open_at", "close_at"):
            for value in ("absent", None, "2026-10-11T16:00:00.000Z", "2026-10-13T16:00:00.000Z", "malformed"):
                with self.subTest(name=name, value=value):
                    source = overnight_day_body()
                    if value == "absent":
                        del source[name]
                    else:
                        source[name] = value
                    with self.assertRaises(ValidationError):
                        parse_response("day", "2026-10-12", source)

    def test_empty_day_must_agree_with_supplied_date_anchors(self):
        wrong_day = "2026-10-11T16:00:00.000Z"
        same_day = "2026-10-12T16:00:00.000Z"
        for data in ({}, {"1": []}, {"1": [{"practice": None}]}):
            for anchors in ({"open_at": wrong_day}, {"close_at": wrong_day},
                            {"open_at": wrong_day, "close_at": wrong_day},
                            {"open_at": same_day, "close_at": wrong_day}):
                with self.subTest(data=data, anchors=anchors), self.assertRaises(ValidationError):
                    parse_response("day", "2026-10-12", {"data": data, **anchors})
            for anchors in ({}, {"open_at": None}, {"open_at": same_day, "close_at": same_day}):
                with self.subTest(data=data, anchors=anchors):
                    self.assertEqual(parse_response("day", "2026-10-12", {"data": data, **anchors}).games, [])

    def test_same_day_games_remain_valid_without_date_metadata(self):
        for metadata in ({}, {"open_at": None, "close_at": None}):
            with self.subTest(metadata=metadata):
                source = {**day_body(), **metadata}
                parsed = parse_response("day", "2026-10-12", source)
                self.assertEqual(len(parsed.games), 1)
                self.assertEqual(parsed.evidence, source)

    def test_next_day_games_and_their_anchors_keep_identity_and_slot_checks(self):
        for scenario, error in (("facility", "facility 4"), ("identity", "game side"),
                                ("duration", "duration"), ("start", "slot start"),
                                ("invalid_anchor", "facility 4")):
            with self.subTest(scenario=scenario):
                source = overnight_day_body()
                second = source["data"]["1"][1]
                if scenario == "facility":
                    second["game"]["FacilityID"] = 5
                elif scenario == "identity":
                    second["game"]["home_team"]["UTeamID"] = 999
                elif scenario == "duration":
                    second["duration"] = 45
                elif scenario == "invalid_anchor":
                    source["data"]["1"][0]["game"]["FacilityID"] = 5
                else:
                    second["time_to_order"] = "2026-10-13T00:21:00.000Z"
                with self.assertRaisesRegex(ValidationError, error):
                    parse_response("day", "2026-10-12", source)

    def test_winter_time_uses_mountain_standard_time(self):
        source = division_body()
        source["games"] = [game(start="2010-12-12T17:00:00.000Z")]
        parsed = parse_response("division", "10", source)
        self.assertEqual(parsed.games[0]["start_utc"], "2010-12-13T00:00:00Z")

    def test_unpublished_standings_and_unknown_metadata_are_not_zero(self):
        source = division_body()
        source["games"] = [game(start=None)]
        parsed = parse_response("division", "10", source)
        self.assertEqual(parsed.division["standings_state"], "null")
        self.assertIsNone(parsed.division["expected_games"])
        self.assertIsNone(parsed.division["season_id"])
        self.assertIsNone(parsed.season)
        self.assertIsNone(parsed.definition)
        self.assertIsNone(parsed.games[0]["start_local"])
        self.assertTrue(any("schedule date unavailable" in x for x in parsed.warnings))
        self.assertIsNone(parsed.evidence["teams"])

    def test_null_official_statistics_are_preserved(self):
        source = division_body()
        source["teams"] = [dict(team(101), wins=None, gamesplayed=0, points=None)]
        row = parse_response("division", "10", source).standings[0]
        self.assertIsNone(row["wins"])
        self.assertEqual(row["games_played"], 0)
        self.assertIsNone(row["points"])
        self.assertIsNone(row["goals_for"])

    def test_scores_are_not_reinterpreted_as_official_forfeits(self):
        source = division_body()
        source["games"] = [game(1, result="0 - 0"), game(2, result="Forfeit"),
                           game(3, result=""), game(4, result=None)]
        source["teams"] = [dict(team(101), gamesplayed=1, wins=0, losses=1, ties=0,
                                goalsfor=0, goalsagainst=0, points=0)]
        parsed = parse_response("division", "10", source)
        self.assertEqual(parsed.games[0]["home_score"], 0)
        self.assertEqual(parsed.games[1]["result"], "Forfeit")
        self.assertIsNone(parsed.games[1]["home_score"])
        self.assertEqual(parsed.games[2]["result"], "")
        self.assertIsNone(parsed.games[3]["result"])
        self.assertEqual(parsed.standings[0]["losses"], 1)
        self.assertEqual(parsed.standings[0]["ties"], 0)
        self.assertTrue(any("not classified" in x for x in parsed.warnings))

    def test_explicit_crossover_discovers_only_nonzero_identity(self):
        source = division_body()
        source["division_season"]["UCrossOverDivSeasonID"] = "00012"
        self.assertEqual(parse_response("division", "10", source).discoveries, {"12"})
        source["division_season"]["UCrossOverDivSeasonID"] = 0
        self.assertEqual(parse_response("division", "10", source).discoveries, set())

    def test_identical_crossover_games_are_deduplicated(self):
        source = division_body()
        source["games"] = [game(), game()]
        parsed = parse_response("division", "10", source)
        self.assertEqual(len(parsed.games), 1)
        self.assertEqual(len(parsed.teams), 2)
        self.assertEqual(len(parsed.evidence["games"]), 2)

    def test_inconsistent_duplicate_game_or_team_rejects_whole_response(self):
        for mutation in ("score", "team"):
            with self.subTest(mutation=mutation):
                source = division_body()
                source["games"] = [game(), game()]
                if mutation == "score":
                    source["games"][1]["result"] = "2 - 1"
                else:
                    source["games"][1]["home_team"]["UDivSeasonID"] = 999
                with self.assertRaisesRegex(ValidationError, "conflicting duplicate"):
                    parse_response("division", "10", source)

    def test_duplicate_standings_rejects_conflict(self):
        source = division_body()
        source["teams"] = [dict(team(101), wins=1), dict(team(101), wins=2)]
        with self.assertRaisesRegex(ValidationError, "conflicting duplicate"):
            parse_response("division", "10", source)

    def test_every_known_facility_boundary_is_validated(self):
        for location in ("root", "division", "game", "team", "standing", "leader"):
            with self.subTest(location=location):
                source = fixture()
                targets = {"root": source, "division": source["division_season"], "game": source["games"][0],
                           "team": source["games"][0]["home_team"], "standing": source["teams"][0],
                           "leader": source["player_standings"][0]}
                targets[location]["FacilityID"] = 5
                with self.assertRaisesRegex(ValidationError, "facility 4"):
                    parse_response("division", "114512", source)

    def test_player_home_facility_is_excluded_and_does_not_scope_historical_stats(self):
        source = fixture()
        player = source["player_standings"][0]["player"]
        player["FacilityID"] = 5
        player["FirstName"] = "Private profile name"
        parsed = parse_response("division", "114512", source)
        safe = parsed.evidence["player_standings"][0]["player"]
        self.assertEqual(safe, {"UPlayerID": player["UPlayerID"]})
        self.assertEqual(parsed.leaders[0]["player_id"], str(player["UPlayerID"]))
        player["UPlayerID"] += 1
        with self.assertRaisesRegex(ValidationError, "nested player identity mismatch"):
            parse_response("division", "114512", source)

    def test_mismatched_requested_division_and_game_side_ids_fail(self):
        with self.assertRaisesRegex(ValidationError, "requested division"):
            parse_response("division", "114513", fixture())
        source = day_body()
        source["data"]["1"][0]["game"]["home_team"]["UTeamID"] = 103
        with self.assertRaisesRegex(ValidationError, "game side"):
            parse_response("day", "2026-10-12", source)

    def test_wrong_day_field_duration_and_bad_dates_fail(self):
        edits = (("SchedGameDateTime", "2026-10-13T17:00:00.000Z"),
                 ("SchedGameDateTime", "not-a-date"), ("Field", 2))
        for key, value in edits:
            source = day_body()
            source["data"]["1"][0]["game"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValidationError):
                parse_response("day", "2026-10-12", source)
        source = day_body()
        source["data"]["1"][0]["duration"] = 50
        with self.assertRaisesRegex(ValidationError, "duration"):
            parse_response("day", "2026-10-12", source)

    def test_bad_identity_types_and_missing_required_shape_fail(self):
        for ident in (None, True, 1.0, "1e3", "-1", "", 0, -1):
            source = division_body()
            source["division_season"]["UDivSeasonID"] = ident
            with self.subTest(ident=ident), self.assertRaises(ValidationError):
                parse_response("division", "10", source)
        for source in ({}, {"data": None}, {"data": []}, {"data": {"1": [{}]}}):
            with self.subTest(source=source), self.assertRaises(ValidationError):
                parse_response("day", "2026-10-12", source)
        for field in ("games", "teams", "division_season"):
            source = division_body()
            del source[field]
            with self.subTest(field=field), self.assertRaises(ValidationError):
                parse_response("division", "10", source)

    def test_sanitization_is_recursive_allowlist_and_does_not_mutate_input(self):
        source = fixture()
        secret = "not-for-evidence"
        source["token"] = secret
        source["users"] = [{"email": secret}]
        source["division_season"]["admin_notes"] = secret
        source["games"][0]["home_team"]["UCoachID"] = 999999
        source["games"][0]["home_team"]["email"] = secret
        source["games"][0]["home_team"]["roster"] = [{"name": secret}]
        source["player_standings"][0]["player"]["FirstName"] = secret
        source["player_standings"][0]["player"]["phone"] = secret
        original = deepcopy(source)
        parsed = parse_response("division", "114512", source)
        self.assertEqual(source, original)
        encoded = json.dumps(parsed.evidence)
        self.assertNotIn(secret, encoded)
        for hidden in ("token", "UCoachID", "email", "roster", "FirstName", "phone"):
            self.assertNotIn(hidden, encoded)
        self.assertIn("UPlayerID", encoded)
        self.assertNotIn("name", parsed.leaders[0])

    def test_non_game_bookings_are_excluded_and_empty_date_remains_valid(self):
        source = {"data": {"1": [{"blank_space": None, "contact": "secret"},
                                 {"practice": {"email": "secret"}},
                                 {"event": {"owner": "secret"}}]}}
        parsed = parse_response("day", "2026-10-12", source)
        self.assertEqual(parsed.games, [])
        self.assertEqual(parsed.discoveries, set())
        self.assertNotIn("secret", json.dumps(parsed.evidence))
        self.assertEqual(len(parsed.warnings), 3)
        self.assertEqual(parse_response("day", "2026-10-12", {"data": {}}).games, [])

    def test_catalog_has_verified_discoveries_and_nested_metadata(self):
        source = [{"UDivSeasonID": 20, "NumGames": 8, "GameLength": 45,
                   "division": {"UDivisionID": 5, "DivisionName": "Youth", "DivisionCode": "Y"},
                   "season": {"id": 64, "SeasonNumber": 175, "StartDate": "1926-11-02T00:00:00Z"}}]
        parsed = parse_response("catalog", "upcoming", source)
        self.assertEqual(parsed.discoveries, {"20"})
        self.assertEqual(parsed.evidence["requested_facility_id"], 4)
        self.assertEqual(parsed.evidence["upcoming_leagues"][0]["season"]["id"], 64)
        self.assertIsNone(parsed.division)
        source[0]["season"]["FacilityID"] = 2
        with self.assertRaises(ValidationError):
            parse_response("catalog", "upcoming", source)

    def test_catalog_nested_identity_and_duplicate_metadata_conflicts_fail(self):
        source = [{"UDivSeasonID": 20, "season_id": 64,
                   "season": {"id": 65}}]
        with self.assertRaisesRegex(ValidationError, "season identity"):
            parse_response("catalog", "upcoming", source)
        source = [{"UDivSeasonID": 20, "NumGames": 8}, {"UDivSeasonID": 20, "NumGames": 9}]
        with self.assertRaisesRegex(ValidationError, "conflicting duplicate"):
            parse_response("catalog", "upcoming", source)

    def test_result_winner_disagreement_is_evidence_not_a_rewrite(self):
        source = division_body()
        source["games"] = [dict(game(result="0 - 5"), winner_id=101)]
        parsed = parse_response("division", "10", source)
        self.assertEqual(parsed.games[0]["winner_id"], "101")
        self.assertEqual(parsed.games[0]["away_score"], 5)
        self.assertTrue(any("winner differs" in x for x in parsed.warnings))

    def test_known_optional_fields_with_malformed_types_fail(self):
        for field, value in (("show_all_games", "true"), ("GameLength", -1),
                             ("NumGames", 3.5), ("NumGames", 10**400),
                             ("est_start_date", "October 12")):
            source = division_body()
            source["division_season"][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                parse_response("division", "10", source)

    def test_excessive_numeric_score_fails_as_validation_error(self):
        source = division_body()
        source["games"] = [game(result="9" * 5000 + " - 0")]
        with self.assertRaises(ValidationError):
            parse_response("division", "10", source)

    def test_official_counts_and_seasons_must_fit_sqlite_integer_range(self):
        for field in ("gamesplayed", "Season"):
            for value in (2**63, 10**400):
                source = division_body()
                source["teams"] = [dict(team(101), **{field: value})]
                # Exercise normal JSON decoding as well as the domain boundary.
                wire = json.loads(json.dumps(source))
                with self.subTest(field=field, digits=len(str(value))), self.assertRaises(ValidationError):
                    parse_response("division", "10", wire)
        source = division_body()
        source["teams"] = [dict(team(101), gamesplayed=2**63-1)]
        self.assertEqual(parse_response("division", "10", source).standings[0]["games_played"], 2**63-1)

    def test_giant_identity_is_a_domain_validation_error(self):
        source = division_body()
        source["division_season"]["UDivSeasonID"] = 10**5000
        with self.assertRaises(ValidationError):
            parse_response("division", "10", source)


if __name__ == "__main__":
    unittest.main()
