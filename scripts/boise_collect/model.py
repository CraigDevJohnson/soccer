"""Validate LPS public responses and retain only allowlisted soccer facts."""

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import math
import re
from zoneinfo import ZoneInfo


class ValidationError(ValueError):
    """The response cannot safely be accepted as the requested source."""


@dataclass
class Parsed:
    evidence: dict = field(default_factory=dict)
    division: dict | None = None
    season: dict | None = None
    definition: dict | None = None
    seasons: list[dict] = field(default_factory=list)
    definitions: list[dict] = field(default_factory=list)
    divisions: list[dict] = field(default_factory=list)
    teams: list[dict] = field(default_factory=list)
    games: list[dict] = field(default_factory=list)
    standings: list[dict] = field(default_factory=list)
    leaders: list[dict] = field(default_factory=list)
    discoveries: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)


_ZONE = ZoneInfo("America/Denver")
_ID = re.compile(r"[0-9]+\Z")
_SCORE = re.compile(r"\s*([0-9]+)\s*-\s*([0-9]+)\s*\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


def _object(value, path):
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ValidationError(f"{path}: expected an object")
    return value


def _array(value, path):
    if not isinstance(value, list):
        raise ValidationError(f"{path}: expected an array")
    return value


def _id(value, path, *, required=False, zero=False):
    if value is None:
        if required:
            raise ValidationError(f"{path}: missing identity")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValidationError(f"{path}: expected a positive decimal identity")
    if isinstance(value, int) and (value < 0 or value >= 10**30):
        raise ValidationError(f"{path}: invalid identity")
    text = str(value)
    if not _ID.fullmatch(text) or len(text) > 30:
        raise ValidationError(f"{path}: invalid identity")
    number = int(text)
    if number == 0 and zero:
        return None
    if number <= 0:
        raise ValidationError(f"{path}: identity must be positive")
    return str(number)


def _number(value, path, *, positive=False):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{path}: expected a number or null")
    if ((isinstance(value, float) and not math.isfinite(value))
            or value < 0 or value > 2**63 - 1 or (positive and value == 0)):
        raise ValidationError(f"{path}: invalid nonnegative number")
    return value


def _integer(value, path, *, positive=False):
    value = _number(value, path, positive=positive)
    if value is not None and int(value) != value:
        raise ValidationError(f"{path}: expected an integer")
    return None if value is None else int(value)


def _string(value, path):
    if value is not None and not isinstance(value, str):
        raise ValidationError(f"{path}: expected a string or null")
    return value


def _boolean(value, path):
    if value is not None and not isinstance(value, bool):
        raise ValidationError(f"{path}: expected a boolean or null")
    return value


def _facility(obj, path, *, required=False):
    present = False
    for key in ("FacilityID", "facility_id"):
        if key in obj:
            present = True
            if _id(obj[key], f"{path}.{key}", required=True) != "4":
                raise ValidationError(f"{path}.{key}: expected facility 4")
    if required and not present:
        raise ValidationError(f"{path}: missing facility identity")


def _pick(obj, keys):
    return {key: obj[key] for key in keys if key in obj}


def _timestamp(value, path, *, allow_date=False):
    """LPS encodes its Mountain wall clock with Z; retain that convention."""
    if value is None:
        return None
    _string(value, path)
    try:
        if allow_date and _DATE.fullmatch(value):
            return datetime.combine(date.fromisoformat(value), datetime.min.time())
        if "T" not in value:
            raise ValueError("missing time")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() != 0:
            raise ValueError("unsupported source offset")
        return parsed.replace(tzinfo=None)
    except (ValueError, OverflowError) as exc:
        raise ValidationError(f"{path}: invalid source date/time") from exc


def _field(value, path):
    if value is None:
        return None
    return _id(value, path, required=True)


class _Parser:
    def __init__(self):
        self.parsed = Parsed()
        self.teams = {}
        self.games = {}
        self.standings = {}
        self.leaders = {}

    def warn(self, message):
        if message not in self.parsed.warnings:
            self.parsed.warnings.append(message)

    def remember(self, table, key, record, path, *, partial=False):
        previous = table.get(key)
        if previous is None:
            table[key] = record
            return
        for name, value in record.items():
            old = previous.get(name)
            if partial and (value is None or old is None):
                if old is None:
                    previous[name] = value
            elif old != value:
                raise ValidationError(f"{path}: conflicting duplicate identity ({name})")

    def team(self, value, path, *, expected_id=None, expected_division=None):
        obj = _object(value, path)
        _facility(obj, path, required=True)
        ident = _id(obj.get("UTeamID"), f"{path}.UTeamID", required=True)
        division = _id(obj.get("UDivSeasonID"), f"{path}.UDivSeasonID", zero=True)
        if expected_id is not None and ident != expected_id:
            raise ValidationError(f"{path}: team identity disagrees with game side")
        if expected_division is not None and division is not None and division != expected_division:
            raise ValidationError(f"{path}: team identity disagrees with division")
        names = [_string(obj[k], f"{path}.{k}") for k in ("team_name", "TeamName") if k in obj]
        nonnull = {name for name in names if name is not None}
        if len(nonnull) > 1:
            raise ValidationError(f"{path}: conflicting team names")
        name = next(iter(nonnull), None)
        season = _integer(obj.get("Season"), f"{path}.Season", positive=True)
        color = _string(obj.get("Color"), f"{path}.Color")
        for key in ("division_name", "div_code"):
            _string(obj.get(key), f"{path}.{key}")
        _boolean(obj.get("Dropped"), f"{path}.Dropped")
        record = {"id": ident, "facility_id": 4, "division_id": division,
                  "season_number": season, "name": name, "color": color,
                  "dropped": obj.get("Dropped"), "division_name": obj.get("division_name"),
                  "div_code": obj.get("div_code")}
        self.remember(self.teams, ident, record, path, partial=True)
        if division:
            self.parsed.discoveries.add(division)
        else:
            self.warn(f"team {ident}: division reference unavailable")
        return _pick(obj, ("UTeamID", "FacilityID", "facility_id", "UDivSeasonID", "Season",
                           "team_name", "TeamName", "Color", "division_name", "div_code", "Dropped"))

    def game(self, value, path, *, day=None, slot=None, group=None, allow_next_day=False):
        obj = _object(value, path)
        _facility(obj, path, required=True)
        ident = _id(obj.get("UGameID"), f"{path}.UGameID", required=True)
        home = _id(obj.get("UTeam1"), f"{path}.UTeam1", required=True)
        away = _id(obj.get("UTeam2"), f"{path}.UTeam2", required=True)
        if home == away:
            raise ValidationError(f"{path}: identical game opponents")
        winner = _id(obj.get("winner_id"), f"{path}.winner_id", zero=True)
        if winner is not None and winner not in (home, away):
            raise ValidationError(f"{path}: winner is not a game participant")
        raw_start = obj.get("SchedGameDateTime")
        start = _timestamp(raw_start, f"{path}.SchedGameDateTime")
        if day is not None and (start is None or start.date().isoformat() != day):
            if (start is None or (start.date() - date.fromisoformat(day)).days != 1
                    or not allow_next_day):
                raise ValidationError(f"{path}: game date disagrees with requested day")
            self.warn(f"game {ident}: next-day start published in requested day {day}")
        if start is None:
            self.warn(f"game {ident}: schedule date unavailable")
        raw_end = obj.get("schedGameEndTime")
        end = _timestamp(raw_end, f"{path}.schedGameEndTime")
        if end is not None and start is not None and end <= start:
            raise ValidationError(f"{path}: game end is not after start")
        result = _string(obj.get("result"), f"{path}.result")
        match = _SCORE.fullmatch(result) if result is not None else None
        if match and any(len(part.lstrip("0")) > 19 for part in match.groups()):
            raise ValidationError(f"{path}: score exceeds supported integer range")
        home_score, away_score = tuple(int(part.lstrip("0") or "0") for part in match.groups()) if match else (None, None)
        if match and (home_score > 2**63 - 1 or away_score > 2**63 - 1):
            raise ValidationError(f"{path}: score exceeds supported integer range")
        if result not in (None, "") and match is None:
            self.warn(f"game {ident}: result not classified as a numeric score")
        if match and winner is not None:
            expected_winner = home if home_score > away_score else away if away_score > home_score else None
            if winner != expected_winner:
                self.warn(f"game {ident}: winner differs from numeric score")
        field_id = _field(obj.get("Field"), f"{path}.Field")
        if group is not None and field_id is not None and field_id != group:
            raise ValidationError(f"{path}: game field disagrees with day group")
        slot_end = slot_minutes = None
        if slot is not None:
            slot_field = _field(slot.get("field"), f"{path}.slot.field")
            if slot_field is not None and slot_field not in (field_id, group):
                raise ValidationError(f"{path}: slot field disagrees with game")
            for key in ("start", "time_to_order"):
                if slot.get(key) is not None and _timestamp(slot[key], f"{path}.slot.{key}") != start:
                    raise ValidationError(f"{path}: slot start disagrees with game")
            slot_end = slot.get("end")
            slot_dt = _timestamp(slot_end, f"{path}.slot.end")
            slot_minutes = _number(slot.get("duration"), f"{path}.slot.duration", positive=True)
            if slot_dt is not None:
                if start is None or slot_dt <= start:
                    raise ValidationError(f"{path}: invalid slot end")
                if slot_minutes is not None and abs((slot_dt-start).total_seconds()/60 - slot_minutes) > 0.001:
                    raise ValidationError(f"{path}: slot duration disagrees with end")
            if end is not None and slot_dt is not None and end != slot_dt:
                self.warn(f"game {ident}: game end differs from day slot end")
        safe = _pick(obj, ("UGameID", "UTeam1", "UTeam2", "winner_id", "FacilityID", "facility_id",
                           "SchedGameDateTime", "schedGameEndTime", "Field", "result"))
        for key, team_id in (("home_team", home), ("visitor_team", away)):
            if obj.get(key) is None:
                if key in obj:
                    safe[key] = None
                self.warn(f"game {ident}: {key} metadata unavailable")
            else:
                safe[key] = self.team(obj[key], f"{path}.{key}", expected_id=team_id)
        utc = start.replace(tzinfo=_ZONE).astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if start else None
        record = {"id": ident, "facility_id": 4, "home_id": home, "away_id": away,
                  "start_local": start.isoformat() if start else None, "start_utc": utc,
                  "raw_start": raw_start, "raw_end": raw_end, "field": field_id,
                  "result": result, "home_score": home_score, "away_score": away_score,
                  "winner_id": winner, "slot_end": slot_end, "slot_minutes": slot_minutes}
        self.remember(self.games, ident, record, path)
        return safe

    def season(self, value, path, *, expected_id=None, expected_number=None):
        obj = _object(value, path)
        _facility(obj, path)
        ident = _id(obj.get("id"), f"{path}.id", required=True)
        number = _integer(obj.get("SeasonNumber"), f"{path}.SeasonNumber", positive=True)
        if expected_id is not None and expected_id != ident:
            raise ValidationError(f"{path}: nested season identity mismatch")
        if expected_number is not None and number is not None and expected_number != number:
            raise ValidationError(f"{path}: nested season number mismatch")
        for key in ("StartDate", "EndDate"):
            _timestamp(obj.get(key), f"{path}.{key}", allow_date=True)
        _boolean(obj.get("Open"), f"{path}.Open")
        normalized = {"id": ident, "facility_id": 4, "number": number,
                      "raw_start": obj.get("StartDate"), "raw_end": obj.get("EndDate"),
                      "open": obj.get("Open")}
        dates = [_timestamp(obj.get(k), f"{path}.{k}", allow_date=True) for k in ("StartDate", "EndDate")]
        warnings = []
        if any(value is not None and value.year < 2000 for value in dates):
            warnings.append("source_dates_before_2000")
        if all(value is not None for value in dates) and dates[0] > dates[1]:
            warnings.append("source_dates_reversed")
        # Sparse duplicate catalog rows can contribute the other date later.
        # The stored merged record recomputes these derived flags.
        normalized["date_warnings"] = warnings if all(dates) else None
        for warning in warnings:
            self.warn(f"season {ident}: {warning}")
        safe = _pick(obj, ("id", "FacilityID", "facility_id", "SeasonNumber", "StartDate", "EndDate", "Open"))
        return normalized, safe

    def definition(self, value, path, *, expected_id=None):
        obj = _object(value, path)
        _facility(obj, path)
        ident = _id(obj.get("UDivisionID"), f"{path}.UDivisionID", required=True)
        if expected_id is not None and ident != expected_id:
            raise ValidationError(f"{path}: nested division definition identity mismatch")
        name = _string(obj.get("DivisionName"), f"{path}.DivisionName")
        code = _string(obj.get("DivisionCode"), f"{path}.DivisionCode")
        for key in ("DivClass", "PlayerType", "Gender", "Age"):
            _string(obj.get(key), f"{path}.{key}")
        _boolean(obj.get("Dropped"), f"{path}.Dropped")
        normalized = {"id": ident, "facility_id": 4, "name": name, "code": code,
                      "div_class": obj.get("DivClass"), "player_type": obj.get("PlayerType"),
                      "gender": obj.get("Gender"), "age": obj.get("Age"), "dropped": obj.get("Dropped")}
        safe = _pick(obj, ("UDivisionID", "FacilityID", "facility_id", "DivisionName", "DivisionCode",
                           "DivClass", "PlayerType", "Gender", "Age", "Dropped"))
        return normalized, safe

    def division_metadata(self, value, path, *, catalog=False, expected_id=None, name=None):
        obj = _object(value, path)
        _facility(obj, path, required=not catalog)
        ident = _id(obj.get("UDivSeasonID"), f"{path}.UDivSeasonID", required=True)
        if expected_id is not None and ident != expected_id:
            raise ValidationError(f"{path}: requested division identity mismatch")
        definition_id = _id(obj.get("UDivisionID"), f"{path}.UDivisionID")
        season_id = _id(obj.get("season_id"), f"{path}.season_id")
        season_number = _integer(obj.get("Season"), f"{path}.Season", positive=True)
        expected = _integer(obj.get("NumGames"), f"{path}.NumGames")
        minutes = _number(obj.get("GameLength"), f"{path}.GameLength", positive=True)
        for key in ("show_all_games", "show_first_game", "suppress_leaderboard",
                    "GirlGoalDoubleScore", "GirlGoalDoubleStandings", "tournament_season"):
            _boolean(obj.get(key), f"{path}.{key}")
        for key in ("est_start_date", "est_end_date", "FirstGameDate", "LastGameDate", "DeadlineDate"):
            _timestamp(obj.get(key), f"{path}.{key}", allow_date=True)
        fees = {}
        for source, target in (("Fees", "published"), ("team_fee", "team"), ("ref_fee", "referee"),
                               ("individual_fee", "individual"), ("deposit_amount", "deposit")):
            if source in obj:
                value = obj[source]
                if isinstance(value, str):
                    _string(value, f"{path}.{source}")
                else:
                    _number(value, f"{path}.{source}")
                fees[target] = value
        for key in ("fees_active", "individual_fee_active"):
            _boolean(obj.get(key), f"{path}.{key}")
        crossover = _id(obj.get("UCrossOverDivSeasonID"), f"{path}.UCrossOverDivSeasonID", zero=True)
        if crossover:
            self.parsed.discoveries.add(crossover)
        safe = _pick(obj, ("UDivSeasonID", "FacilityID", "facility_id", "UDivisionID", "season_id", "Season",
                           "NumGames", "GameLength", "show_all_games", "show_first_game", "suppress_leaderboard",
                           "est_start_date", "est_end_date", "FirstGameDate", "LastGameDate", "DeadlineDate",
                           "UCrossOverDivSeasonID", "GirlGoalDoubleScore", "GirlGoalDoubleStandings", "tournament_season",
                           "Fees", "team_fee", "ref_fee", "individual_fee", "deposit_amount", "fees_active", "individual_fee_active"))
        nested_season = nested_definition = None
        if obj.get("season") is not None:
            nested_season, safe["season"] = self.season(obj["season"], path+".season", expected_id=season_id, expected_number=season_number)
            season_id = nested_season["id"]
            season_number = nested_season["number"] if season_number is None else season_number
        elif "season" in obj:
            safe["season"] = None
        if obj.get("division") is not None:
            nested_definition, safe["division"] = self.definition(obj["division"], path+".division", expected_id=definition_id)
            definition_id = nested_definition["id"]
        elif "division" in obj:
            safe["division"] = None
        normalized = {"id": ident, "facility_id": 4, "definition_id": definition_id, "season_id": season_id,
                      "season_number": season_number, "name": name if name is not None else (nested_definition or {}).get("name"),
                      "expected_games": expected, "game_minutes": minutes, "show_all_games": obj.get("show_all_games"),
                      "show_first_game": obj.get("show_first_game"), "suppress_leaderboard": obj.get("suppress_leaderboard"),
                      "raw_est_start": obj.get("est_start_date"), "raw_est_end": obj.get("est_end_date"),
                      "deadline": obj.get("DeadlineDate"), "first_game_date": obj.get("FirstGameDate"),
                      "last_game_date": obj.get("LastGameDate"),
                      "girl_goal_double_score": obj.get("GirlGoalDoubleScore"),
                      "girl_goal_double_standings": obj.get("GirlGoalDoubleStandings"),
                      "tournament_season": obj.get("tournament_season"),
                      "crossover_division_id": crossover,
                      "fees": fees, "fees_active": obj.get("fees_active"),
                      "individual_fee_active": obj.get("individual_fee_active"),
                      "standings_state": "null"}
        return normalized, safe, nested_season, nested_definition

    def division_response(self, key, body):
        obj = _object(body, "division response")
        _facility(obj, "division response")
        name = _string(obj.get("division_name"), "division_name")
        division, safe, season, definition = self.division_metadata(obj.get("division_season"), "division_season", expected_id=key, name=name)
        self.parsed.division = division
        self.parsed.season = season or ({"id": division["season_id"], "facility_id": 4, "number": division["season_number"], "raw_start": None, "raw_end": None} if division["season_id"] else None)
        self.parsed.definition = definition or ({"id": division["definition_id"], "facility_id": 4, "name": name, "code": None} if division["definition_id"] else None)
        evidence = {"division_season": safe, "games": [], "teams": None}
        if "division_name" in obj:
            evidence["division_name"] = name
        for point in ("win_point", "tie_point", "loss_point", "goal_point"):
            if point in obj:
                evidence[point] = _number(obj[point], point)
        division["scoring_rules"] = {k: evidence[k] for k in ("win_point", "tie_point", "loss_point", "goal_point") if k in evidence}
        for i, game in enumerate(_array(obj.get("games"), "games")):
            evidence["games"].append(self.game(game, f"games[{i}]"))
        if "teams" not in obj:
            raise ValidationError("teams: missing standings state")
        if obj["teams"] is not None:
            division["standings_state"] = "present"
            evidence["teams"] = []
            for i, row in enumerate(_array(obj["teams"], "teams")):
                path = f"teams[{i}]"
                row = _object(row, path)
                safe_team = self.team(row, path, expected_division=key)
                team_id = _id(row.get("UTeamID"), path+".UTeamID", required=True)
                record = {"division_id": key, "team_id": team_id}
                for source, target in (("position", "position"), ("gamesplayed", "games_played"), ("wins", "wins"),
                                       ("losses", "losses"), ("ties", "ties"), ("goalsfor", "goals_for"),
                                       ("goalsagainst", "goals_against"), ("points", "points")):
                    record[target] = _integer(row.get(source), path+"."+source)
                    if source in row:
                        safe_team[source] = row[source]
                self.remember(self.standings, team_id, record, path)
                evidence["teams"].append(safe_team)
        if "player_standings" in obj:
            if obj["player_standings"] is None:
                evidence["player_standings"] = None
                self.warn(f"division {key}: player leaderboard unavailable")
            else:
                evidence["player_standings"] = []
                for i, row in enumerate(_array(obj["player_standings"], "player_standings")):
                    evidence["player_standings"].append(self.leader(row, f"player_standings[{i}]", key))
        else:
            self.warn(f"division {key}: player leaderboard not supplied")
        self.parsed.evidence = evidence

    def leader(self, value, path, division_id):
        obj = _object(value, path)
        _facility(obj, path)
        own_division = _id(obj.get("UDivSeasonID"), path+".UDivSeasonID", required=True)
        if own_division != division_id:
            raise ValidationError(f"{path}: leaderboard division identity mismatch")
        player_id = _id(obj.get("UPlayerID"), path+".UPlayerID", required=True)
        team_id = _id(obj.get("UTeamID"), path+".UTeamID", required=True)
        safe = _pick(obj, ("UDivSeasonID", "UTeamID", "UPlayerID", "FacilityID", "facility_id", "goals", "assists", "points"))
        record = {"division_id": division_id, "team_id": team_id, "player_id": player_id}
        for key in ("goals", "assists", "points"):
            record[key] = _integer(obj.get(key), path+"."+key)
        if obj.get("team") is not None:
            safe["team"] = self.team(obj["team"], path+".team", expected_id=team_id, expected_division=division_id)
        if obj.get("player") is not None:
            player = _object(obj["player"], path+".player")
            if _id(player.get("UPlayerID"), path+".player.UPlayerID", required=True) != player_id:
                raise ValidationError(f"{path}: nested player identity mismatch")
            # A player's current home facility can differ from this historical
            # division. It is profile data, not the sporting record's scope.
            safe["player"] = {"UPlayerID": player["UPlayerID"]}
        self.remember(self.leaders, (team_id, player_id), record, path)
        return safe

    def day_response(self, key, body):
        obj = _object(body, "day response")
        _facility(obj, "day response")
        data = _object(obj.get("data"), "data")
        opening = _timestamp(obj.get("open_at"), "open_at")
        closing = _timestamp(obj.get("close_at"), "close_at")
        metadata_matches_day = (opening is not None and closing is not None
                                and opening.date().isoformat() == key
                                and closing.date().isoformat() == key)
        evidence = {"data": {}, **_pick(obj, ("open_at", "close_at"))}
        for group, slots in data.items():
            field_id = _field(group, "data field")
            # Validate requested-day anchors first without reordering evidence.
            ordered_slots = []
            for i, slot in enumerate(_array(slots, "data."+group)):
                path = f"data.{group}[{i}]"
                slot = _object(slot, path)
                start = None
                if slot.get("game") is not None:
                    source_game = _object(slot["game"], path+".game")
                    start = _timestamp(source_game.get("SchedGameDateTime"), path+".game.SchedGameDateTime")
                ordered_slots.append((start or datetime.min, i, slot))
            clean_slots = [None] * len(ordered_slots)
            has_requested_day_game = False
            for start, i, slot in sorted(ordered_slots, key=lambda item: (item[0], item[1])):
                path = f"data.{group}[{i}]"
                _facility(slot, path)
                if slot.get("game") is not None:
                    if any(slot.get(k) is not None for k in ("practice", "event", "blank_space")):
                        raise ValidationError(f"{path}: mixed game and non-game slot")
                    game = self.game(slot["game"], path+".game", day=key, slot=slot,
                                     group=field_id, allow_next_day=metadata_matches_day and has_requested_day_game)
                    if start.date().isoformat() == key:
                        has_requested_day_game = True
                    clean = _pick(slot, ("time_to_order", "start", "end", "field", "duration"))
                    clean["game"] = game
                    clean_slots[i] = clean
                elif any(k in slot for k in ("blank_space", "practice", "event")):
                    # Non-game booking details can contain personal information.
                    kind = next(k for k in ("practice", "event", "blank_space") if k in slot)
                    clean_slots[i] = {kind: None}
                    self.warn(f"day {key}: non-game {kind} slots excluded")
                else:
                    raise ValidationError(f"{path}: unrecognized schedule slot")
            evidence["data"][group] = clean_slots
        if not self.games and any(anchor.date().isoformat() != key for anchor in (opening, closing) if anchor):
            raise ValidationError("empty schedule date disagrees with requested day")
        self.parsed.evidence = evidence

    def catalog_response(self, body):
        rows = _array(body, "catalog")
        seen = {}
        seasons = {}
        definitions = {}
        divisions = {}
        evidence = []
        for i, row in enumerate(rows):
            path = f"catalog[{i}]"
            division, safe, season, definition = self.division_metadata(row, path, catalog=True)
            self.remember(seen, division["id"], safe, path)
            self.remember(divisions, division["id"], division, path)
            if season:
                self.remember(seasons, season["id"], season, path+".season", partial=True)
            if definition:
                self.remember(definitions, definition["id"], definition, path+".division", partial=True)
            self.parsed.discoveries.add(division["id"])
            evidence.append(safe)
        self.parsed.evidence = {"upcoming_leagues": evidence, "requested_facility_id": 4}
        self.parsed.seasons = list(seasons.values())
        self.parsed.definitions = list(definitions.values())
        self.parsed.divisions = list(divisions.values())


def parse_response(kind: str, key: str, body: object) -> Parsed:
    """Validate the entire response before returning discoveries or evidence."""
    parser = _Parser()
    if kind == "day":
        if not isinstance(key, str) or not _DATE.fullmatch(key):
            raise ValidationError("day key: expected YYYY-MM-DD")
        try:
            date.fromisoformat(key)
        except ValueError as exc:
            raise ValidationError("day key: invalid date") from exc
        parser.day_response(key, body)
    elif kind == "division":
        ident = _id(key, "division key", required=True)
        parser.division_response(ident, body)
    elif kind == "catalog":
        if key != "upcoming":
            raise ValidationError("catalog key: expected upcoming")
        parser.catalog_response(body)
    else:
        raise ValidationError("unsupported resource kind")
    parser.parsed.teams = list(parser.teams.values())
    parser.parsed.games = list(parser.games.values())
    parser.parsed.standings = list(parser.standings.values())
    parser.parsed.leaders = list(parser.leaders.values())
    return parser.parsed
