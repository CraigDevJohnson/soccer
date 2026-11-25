"""Read-only browsing and bounded SQL over a local collector archive."""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time

from .explorer_metadata import (
    DEFINITION_FIELDS, DIVISION_FIELDS, SESSION_FIELDS, TEAM_FIELDS,
    load_metadata, metadata_for, source, timing_summaries,
)

SQL_TIMEOUT = 2.0
SQL_ROW_LIMIT = 500
SQL_LENGTH_LIMIT = 20_000
SQL_CELL_LIMIT = 65_536
SQL_OUTPUT_LIMIT = 1_048_576
KINDS = {"sessions", "divisions", "teams", "players", "games"}
FILTERS = {
    "sessions": {"search", "year"},
    "divisions": {"search", "year", "session"},
    "teams": {"search", "year", "session", "division", "player"},
    "players": {"search", "year", "session", "division", "team"},
    "games": {"search", "year", "session", "division", "team", "from", "to", "status", "field"},
}
_ID = re.compile(r"[1-9][0-9]{0,29}\Z")


class QueryError(ValueError):
    """A requested archive query is invalid or exceeds its bounds."""


class RecordNotFound(QueryError):
    """The archive has no record with the requested identity."""


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _epoch(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z") if value is not None else None


def _ident(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise QueryError("IDs must be positive decimal strings of at most 30 digits")
    return value


def _iso_day(value, name):
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except (TypeError, ValueError):
        raise QueryError(f"{name} must be YYYY-MM-DD") from None
    return value


def _bounds(identifiers, games):
    starts = [games[ident]["start_local"][:10] for ident in identifiers if games[ident]["start_local"]]
    return (min(starts), max(starts)) if starts else (None, None)


def _player_summary(ident, entries):
    player = {"id": ident, "entry_count": len(entries),
              "team_count": len({entry["team_id"] for entry in entries}),
              "session_count": len({entry["session_id"] for entry in entries if entry["session_id"] is not None})}
    for metric in ("goals", "assists", "points"):
        values = [entry[metric] for entry in entries]
        player["recorded_" + metric] = sum(values) if all(value is not None for value in values) else None
    return player


class Archive:
    """Cache one coherent browse snapshot; SQL queries always use fresh connections."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.database = self.root / "collector.sqlite3"
        if not self.database.is_file():
            raise QueryError("No collector database exists in the selected archive")
        self._reload_lock = threading.Lock()
        self.reload()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            connection.row_factory = sqlite3.Row
            if hasattr(connection, "enable_load_extension"):
                connection.enable_load_extension(False)
            connection.execute("PRAGMA query_only=ON")
            yield connection
        finally:
            connection.close()

    def _source_stamp(self):
        stat = self.database.stat()
        return stat.st_size, stat.st_mtime_ns

    def reload(self):
        with self._reload_lock:
            state = self._load()
            self._state = state
        return {"loaded_at": state["overview"]["archive"]["loaded_at"],
                "counts": dict(state["overview"]["counts"])}

    def _load(self):
        loaded_at = _now()
        source_stamp = self._source_stamp()
        report = {}
        report_mtime = 0
        try:
            report_path = self.root / "reports" / "coverage.json"
            report = json.loads(report_path.read_text())
            report_mtime = report_path.stat().st_mtime_ns
        except (OSError, ValueError):
            pass
        labels = {row["division_id"]: row for row in report.get("inventory", {}).get("by_division", [])}
        items = {kind: {} for kind in KINDS}
        sessions, divisions, teams, players, games = (items[kind] for kind in ("sessions", "divisions", "teams", "players", "games"))
        game_index = {kind: defaultdict(set) for kind in ("session", "division", "team", "year")}
        years = {kind: defaultdict(set) for kind in KINDS}
        leaders, standings = [], []
        game_snapshots = {}
        records = {kind: {} for kind in ("sessions", "definitions", "divisions", "teams", "games")}
        with self._connection() as db:
            db.execute("BEGIN")
            meta = {row["key"]: row["value"] for row in db.execute("SELECT key,value FROM meta")}
            if meta.get("schema_version") != "1":
                raise QueryError("Unsupported collector database schema")
            resources = {(row["kind"], row["key"]): dict(row) for row in db.execute("SELECT * FROM resources")}
            intervals = [dict(row) for row in db.execute("SELECT start_day,end_day FROM intervals ORDER BY start_day,end_day")]
            snapshot_count, latest_fetch, max_snapshot_id = db.execute("SELECT count(*),max(fetched_at),max(id) FROM snapshots").fetchone()
            snapshot_rows = [dict(row) for row in db.execute("SELECT * FROM snapshots")]
            snapshot_info = {row["id"]: source(row) for row in snapshot_rows}
            number_ids = defaultdict(list)
            for row in db.execute("SELECT id,data_json,snapshot_id FROM seasons"):
                record = json.loads(row["data_json"])
                records["sessions"][row["id"]] = {**record, "snapshot_id": row["snapshot_id"]}
                sessions[row["id"]] = {"id": row["id"], "number": record.get("number")}
                if record.get("number") is not None:
                    number_ids[record["number"]].append(row["id"])
            unique_number_ids = {number: ids[0] for number, ids in number_ids.items() if len(ids) == 1}
            for row in db.execute("SELECT id,data_json,snapshot_id FROM division_definitions"):
                records["definitions"][row["id"]] = {**json.loads(row["data_json"]), "snapshot_id": row["snapshot_id"]}
            for row in db.execute("SELECT id,name,season_id,season_number,snapshot_id,data_json FROM divisions"):
                records["divisions"][row["id"]] = {**json.loads(row["data_json"]), "snapshot_id": row["snapshot_id"]}
                label = labels.get(row["id"], {})
                if label.get("snapshot_id") != row["snapshot_id"]:
                    label = {}
                divisions[row["id"]] = {
                    "id": row["id"], "name": label.get("name") or row["name"] or f"Division {row['id']}",
                    "session_id": row["season_id"], "session_number": row["season_number"],
                    "details_available": resources.get(("division", row["id"]), {}).get("last_success_snapshot_id") is not None,
                }
                divisions[row["id"]]["catalog_only"] = not divisions[row["id"]]["details_available"]
            for row in db.execute("SELECT id,name,division_id,season_number,data_json,snapshot_id FROM teams"):
                records["teams"][row["id"]] = {**json.loads(row["data_json"]), "snapshot_id": row["snapshot_id"]}
                division_id = row["division_id"]
                if division_id and division_id not in divisions:
                    divisions[division_id] = {
                        "id": division_id, "name": f"Division {division_id}",
                        "session_id": unique_number_ids.get(row["season_number"]),
                        "session_number": row["season_number"], "details_available": False, "catalog_only": False,
                    }
                division = divisions.get(division_id, {})
                teams[row["id"]] = {
                    "id": row["id"], "name": row["name"] or f"Team {row['id']}",
                    "color": json.loads(row["data_json"]).get("color"),
                    "division_id": division_id, "division_name": division.get("name"),
                    "session_id": division.get("session_id") or unique_number_ids.get(row["season_number"]),
                    "session_number": row["season_number"],
                }
            for row in db.execute("SELECT * FROM games"):
                records["games"][row["id"]] = {**json.loads(row["data_json"]), "snapshot_id": row["snapshot_id"]}
                home, away = teams.get(row["home_id"], {}), teams.get(row["away_id"], {})
                game = {key: row[key] for key in ("id", "start_local", "result", "home_score", "away_score", "home_id", "away_id")}
                game["field"] = json.loads(row["data_json"]).get("field")
                game["winner_id"] = row["winner_id"]
                game["fetched_at"] = snapshot_info.get(row["snapshot_id"], {}).get("fetched_at")
                game["scored"] = row["home_score"] is not None and row["away_score"] is not None
                for side, team in (("home", home), ("away", away)):
                    game[side + "_name"] = team.get("name")
                    for key in ("division_id", "division_name", "session_id", "session_number"):
                        game[side + "_" + key] = team.get(key)
                    for kind, ident in (("team", row[side + "_id"]), ("division", team.get("division_id")),
                                        ("session", team.get("session_id"))):
                        if ident is not None:
                            game_index[kind][ident].add(row["id"])
                games[row["id"]] = game
                game_snapshots[row["id"]] = row["snapshot_id"]
                if row["start_local"]:
                    game_index["year"][row["start_local"][:4]].add(row["id"])
            for row in db.execute(
                "SELECT p.player_id,p.team_id,p.division_id,p.goals,p.assists,p.points,p.snapshot_id "
                "FROM player_leaders p JOIN resources r ON r.kind='division' AND r.key=p.division_id "
                "AND r.last_success_snapshot_id=p.snapshot_id"
            ):
                entry = dict(row)
                team, division = teams.get(row["team_id"], {}), divisions.get(row["division_id"], {})
                entry.update(team_name=team.get("name"), division_name=division.get("name"),
                             session_id=division.get("session_id"), session_number=division.get("session_number"))
                leaders.append(entry)
            for row in db.execute(
                "SELECT s.snapshot_id,s.division_id,s.team_id,s.position,s.games_played,s.wins,s.losses,s.ties,"
                "s.goals_for,s.goals_against,s.points FROM standings s JOIN resources r "
                "ON r.kind='division' AND r.key=s.division_id AND r.last_success_snapshot_id=s.snapshot_id"
            ):
                standing = dict(row)
                standing["team_name"] = teams.get(row["team_id"], {}).get("name")
                standings.append(standing)
            timings = timing_summaries(db, records["games"], snapshot_info)
            refresh_job = None
            if db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='refresh_jobs'").fetchone():
                latest_job = db.execute("SELECT * FROM refresh_jobs ORDER BY created_at DESC,id DESC LIMIT 1").fetchone()
                if latest_job:
                    statuses = dict(db.execute("SELECT status,count(*) FROM refresh_job_resources WHERE job_id=? GROUP BY status",
                                               (latest_job["id"],)))
                    refresh_job = {"id": latest_job["id"], "status": latest_job["status"],
                                   "start_date": latest_job["start_day"], "end_date": latest_job["end_day"],
                                   "completed": statuses.get("success", 0), "pending": statuses.get("pending", 0),
                                   "skipped": statuses.get("skipped", 0), "total": sum(statuses.values())}

        # Evidence files are checked after releasing the SQLite read transaction.
        saved, saved_sources, snapshot_info, metadata_issues, catalog_counts = load_metadata(
            self.root, snapshot_rows, {row["last_success_snapshot_id"] for row in resources.values()})
        fields = {"sessions": set(SESSION_FIELDS.values()),
                  "definitions": set(DEFINITION_FIELDS.values()),
                  "divisions": set(DIVISION_FIELDS.values()) | {"definition_id", "scoring_rules", "fees", "fees_active", "individual_fee_active"},
                  "teams": set(TEAM_FIELDS.values())}
        for kind in ("sessions", "divisions", "teams"):
            for ident, item in items[kind].items():
                record = records[kind].get(ident, {})
                item.update(metadata_for(kind, ident, record, record.get("snapshot_id"),
                                         saved, saved_sources, snapshot_info, fields[kind]))
                if kind == "divisions":
                    definition_id = record.get("definition_id")
                    definition = records["definitions"].get(definition_id, {})
                    extra = metadata_for("definitions", definition_id, definition, definition.get("snapshot_id"),
                                         saved, saved_sources, snapshot_info, fields["definitions"])
                    item["metadata"].update(extra["metadata"])
                    item["metadata_sources"].update(extra["metadata_sources"])
                    if not item["name"] or item["name"] == f"Division {ident}":
                        item["name"] = definition.get("name") or item["name"]
        for ident, team in teams.items():
            team["division_name"] = divisions.get(team["division_id"], {}).get("name") or team["division_name"]
        for ident, game in games.items():
            game["timing"] = timings[ident]
            for side in ("home", "away"):
                game[side + "_division_name"] = divisions.get(game.get(side + "_division_id"), {}).get("name") or game.get(side + "_division_name")

        team_ids_by_session, team_ids_by_division = defaultdict(set), defaultdict(set)
        division_ids_by_session, team_ids_by_name = defaultdict(set), defaultdict(set)
        for ident, team in teams.items():
            team_ids_by_session[team["session_id"]].add(ident)
            team_ids_by_division[team["division_id"]].add(ident)
            team_ids_by_name[team["name"].strip().casefold()].add(ident)
        for ident, division in divisions.items():
            division_ids_by_session[division["session_id"]].add(ident)
        for kind, plural in (("session", "sessions"), ("division", "divisions"), ("team", "teams")):
            for ident, item in items[plural].items():
                identifiers = game_index[kind].get(ident, set())
                item["game_count"] = len(identifiers)
                item["start_date"], item["end_date"] = _bounds(identifiers, games)
                years[plural][ident] = {games[g]["start_local"][:4] for g in identifiers if games[g]["start_local"]}
                if kind == "session":
                    item["team_count"] = len(team_ids_by_session.get(ident, ()))
                    item["division_count"] = len(division_ids_by_session.get(ident, ()))
                    session_divisions = division_ids_by_session.get(ident, set())
                    item["catalog_only"] = bool(session_divisions) and all(divisions[key].get("catalog_only") for key in session_divisions)
                elif kind == "division":
                    item["team_count"] = len(team_ids_by_division.get(ident, ()))
        entries_by_player, entries_by_team, entries_by_division = defaultdict(list), defaultdict(list), defaultdict(list)
        for entry in leaders:
            entries_by_player[entry["player_id"]].append(entry)
            entries_by_team[entry["team_id"]].append(entry)
            entries_by_division[entry["division_id"]].append(entry)
        for ident, entries in entries_by_player.items():
            players[ident] = _player_summary(ident, entries)
            years["players"][ident] = set().union(*(years["sessions"].get(e["session_id"], set()) for e in entries))

        order = {
            "sessions": sorted(sessions, key=lambda k: (sessions[k]["number"] or -1, int(k)), reverse=True),
            "divisions": sorted(divisions, key=lambda k: (-(divisions[k]["session_number"] or 0), divisions[k]["name"].casefold(), int(k))),
            "teams": sorted(teams, key=lambda k: (teams[k]["name"].casefold(), -(teams[k]["session_number"] or 0), int(k))),
            "players": sorted(players, key=lambda k: (-(players[k]["recorded_points"] or 0), int(k))),
            "games": sorted(games, key=lambda k: (games[k]["start_local"] or "", int(k)), reverse=True),
        }
        day_resources = [row for (kind, _), row in resources.items() if kind == "day"]
        failed_divisions = [row for (kind, _), row in resources.items() if kind == "division" and row["status"] != "success"]
        gap_fields = ("kind", "key", "status", "http_status", "last_error", "attempts", "last_success_snapshot_id")
        report_fresh = (report.get("inventory", {}).get("snapshots") == snapshot_count
                        and report.get("ledger", {}).get("resource_count") == len(resources)
                        and report_mtime >= source_stamp[1])
        summary = {
            "requested_dates": len(day_resources),
            "validated_dates": sum(row["status"] == "success" for row in day_resources),
            "invalid_dates": sum(row["status"] == "invalid" for row in day_resources),
            "failed_divisions": len(failed_divisions),
            "evidence_complete": report.get("evidence", {}).get("complete") if report_fresh else None,
            "scoring_discrepancies": len(report.get("discrepancies", [])) if report_fresh else None,
        }
        catalog_resource = resources.get(("catalog", "upcoming"), {})
        catalog_snapshot = catalog_resource.get("last_success_snapshot_id")
        catalog = {"status": catalog_resource.get("status", "not_collected"),
                   "last_success_at": _epoch(catalog_resource.get("last_success_at")),
                   "snapshot_id": catalog_snapshot, "division_count": catalog_counts.get(catalog_snapshot)}
        freshness = {"latest_" + kind + "_fetch": _epoch(max(
            (row["last_success_at"] for (resource_kind, _), row in resources.items()
             if resource_kind == kind and row["last_success_at"] is not None), default=None))
            for kind in ("day", "division", "catalog")}
        freshness["refresh_job"] = refresh_job
        coverage = {
            **summary, "report_fresh": report_fresh, "report_generated_at": report.get("generated_at"),
            "all_time_complete": False, "retention_boundary": "unresolved",
            "resource_gaps": [{key: row[key] for key in gap_fields} for row in resources.values() if row["status"] != "success"],
            "yearly_dates": report.get("coverage", {}).get("by_year", []) if report_fresh else [],
            "discovery": {key: report.get("discovery_coverage", {}).get(key) for key in (
                "complete", "participant_mappings_complete", "observed_game_participants", "unresolved_participant_count")}
                if report_fresh else None,
            "source_conflicts": len(report.get("source_conflicts", [])) if report_fresh else None,
            "discrepancy_categories": dict(Counter(row["metric"] for row in report.get("discrepancies", []))) if report_fresh else {},
            "caveats": report.get("caveats", []),
            "catalog": catalog, "freshness": freshness, "metadata_evidence_issues": len(metadata_issues),
        }
        counts = {"games": len(games), "teams": len(teams), "players": len(players),
                  "sessions": len(sessions), "divisions": len(divisions)}
        overview = {
            "counts": counts, "years": sorted(int(year) for year in game_index["year"]),
            "archive": {"start_date": min((row["start_day"] for row in intervals), default=None),
                        "end_date": max((row["end_day"] for row in intervals), default=None),
                        "latest_fetch": _epoch(latest_fetch), "loaded_at": loaded_at, "timezone": "America/Denver"},
            "coverage": summary,
            "catalog": catalog, "freshness": freshness,
        }
        return {
            "items": items, "order": order, "years": years, "game_index": game_index,
            "leaders": leaders, "entries_by_player": entries_by_player, "entries_by_team": entries_by_team,
            "entries_by_division": entries_by_division, "standings": standings,
            "team_ids_by_name": team_ids_by_name, "division_ids_by_session": division_ids_by_session,
            "overview": overview, "coverage": coverage, "source_stamp": source_stamp,
            "game_snapshots": game_snapshots, "max_snapshot_id": max_snapshot_id,
        }

    def overview(self):
        state = self._state
        result = deepcopy(state["overview"])
        result["archive"]["source_changed"] = self._source_stamp() != state["source_stamp"]
        return result

    def coverage(self):
        state = self._state
        result = deepcopy(state["coverage"])
        result["source_changed"] = self._source_stamp() != state["source_stamp"]
        return result

    def _params(self, kind, params):
        if kind not in KINDS:
            raise QueryError("Unknown record kind")
        if not isinstance(params, dict) or set(params) - FILTERS[kind] - {"limit", "offset"}:
            raise QueryError("Unknown filter for this record kind")
        values = {}
        for key, value in params.items():
            if not isinstance(value, str):
                raise QueryError("Filters must have one string value")
            if key in ("limit", "offset"):
                if not re.fullmatch(r"[0-9]{1,10}", value):
                    raise QueryError(f"{key} must be a nonnegative integer")
                values[key] = int(value)
            elif key in ("session", "division", "team", "player", "field"):
                values[key] = _ident(value)
            elif key == "year":
                if not re.fullmatch(r"[0-9]{4}", value) or not 1 <= int(value) <= 9999:
                    raise QueryError("year must be a four-digit year")
                values[key] = value
            elif key in ("from", "to"):
                values[key] = _iso_day(value, key)
            elif key == "status":
                if value not in {"scored", "unscored"}:
                    raise QueryError("status must be scored or unscored")
                values[key] = value
            else:
                if len(value) > 200:
                    raise QueryError("Search is limited to 200 characters")
                values[key] = value.strip().casefold()
        values.setdefault("limit", 50)
        values.setdefault("offset", 0)
        if not 1 <= values["limit"] <= 200:
            raise QueryError("limit must be between 1 and 200")
        if values.get("from") and values.get("to") and values["from"] > values["to"]:
            raise QueryError("from must not be after to")
        return values

    def list_items(self, kind, params):
        values = self._params(kind, params)
        state = self._state
        candidates = None
        if kind == "games":
            for key in ("session", "division", "team", "year"):
                if key in values:
                    members = state["game_index"][key].get(values[key], set())
                    candidates = members if candidates is None else candidates & members
        elif kind == "teams" and "player" in values:
            candidates = {entry["team_id"] for entry in state["entries_by_player"].get(values["player"], ())}
        matched, scoped_players = [], {}
        for ident in state["order"][kind]:
            if candidates is not None and ident not in candidates:
                continue
            item = state["items"][kind][ident]
            if kind != "games" and "year" in values and values["year"] not in state["years"][kind].get(ident, set()):
                continue
            if kind in ("teams", "divisions") and any(item[key + "_id"] != values[key] for key in ("session", "division") if key in values):
                continue
            if kind == "players":
                entries = [entry for entry in state["entries_by_player"][ident]
                           if all(entry[key + "_id"] == values[key] for key in ("session", "division", "team") if key in values)
                           and ("year" not in values or values["year"] in state["years"]["sessions"].get(entry["session_id"], set()))]
                if not entries:
                    continue
                if any(key in values for key in ("session", "division", "team", "year")):
                    scoped_players[ident] = _player_summary(ident, entries)
            if kind == "games":
                day = (item["start_local"] or "")[:10]
                if ("from" in values and (not day or day < values["from"])) or ("to" in values and (not day or day > values["to"])):
                    continue
                if "status" in values and item["scored"] != (values["status"] == "scored"):
                    continue
                if "field" in values and str(item.get("field")) != values["field"]:
                    continue
            search = values.get("search")
            if search:
                fields = ("id", "name", "number", "home_name", "away_name", "home_id", "away_id", "division_name", "result")
                if not (any(search in str(item.get(key) or "").casefold() for key in fields)
                        or any(search in str(value).casefold() for value in item.get("metadata", {}).values())):
                    continue
            matched.append(ident)
        if scoped_players:
            matched.sort(key=lambda ident: (-(scoped_players[ident]["recorded_points"] or 0), int(ident)))
        start, limit = values["offset"], values["limit"]
        return {"items": [dict(scoped_players.get(ident, state["items"][kind][ident])) for ident in matched[start:start + limit]],
                "total": len(matched), "limit": limit, "offset": start}

    def detail(self, kind, ident):
        if kind not in KINDS:
            raise QueryError("Unknown record kind")
        ident = _ident(ident)
        state = self._state
        if ident not in state["items"][kind]:
            raise RecordNotFound("Record not found in this archive")
        item = state["items"][kind][ident]
        result = {"item": dict(item)}
        if kind == "sessions":
            result["divisions"] = [dict(state["items"]["divisions"][key]) for key in state["order"]["divisions"]
                                   if key in state["division_ids_by_session"].get(ident, ())]
        elif kind == "divisions":
            result["standings"] = [dict(row) for row in state["standings"] if row["division_id"] == ident]
            result["standings"].sort(key=lambda row: (row["position"] is None, row["position"] or 0, int(row["team_id"])))
            result["leaders"] = [dict(row) for row in state["entries_by_division"].get(ident, ())]
        elif kind == "teams":
            result["standing"] = next((dict(row) for row in state["standings"] if row["team_id"] == ident and row["division_id"] == item["division_id"]), None)
            result["leaders"] = [dict(row) for row in state["entries_by_team"].get(ident, ())]
            related = state["team_ids_by_name"].get(item["name"].strip().casefold(), ())
            result["other_registrations"] = [dict(state["items"]["teams"][key]) for key in state["order"]["teams"] if key in related and key != ident]
            result["other_registrations_note"] = "Matching names only; these are separate registrations, not established club continuity."
        elif kind == "players":
            result["entries"] = [dict(row) for row in state["entries_by_player"][ident]]
            result["scope"] = "Supplied leaderboard entries only. Player names, rosters and game appearances are unavailable."
        else:
            with self._connection() as db:
                db.execute("BEGIN")
                row = db.execute("SELECT data_json FROM game_observations WHERE game_id=? AND snapshot_id=?",
                                 (ident, state["game_snapshots"][ident])).fetchone()
                result["record"] = json.loads(row[0]) if row else None
                result["observations"] = [{"kind": row["kind"], "key": row["key"], "snapshot_id": row["snapshot_id"],
                                           "fetched_at": _epoch(row["fetched_at"]), "record": json.loads(row["data_json"])}
                                          for row in db.execute(
                                              "SELECT s.kind,s.key,s.fetched_at,o.snapshot_id,o.data_json FROM game_observations o "
                                              "JOIN snapshots s ON s.id=o.snapshot_id WHERE o.game_id=? AND s.id<=? ORDER BY s.fetched_at DESC,s.id DESC",
                                              (ident, state["max_snapshot_id"]))]
        return result

    def schema(self):
        with self._connection() as db:
            tables = []
            for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
                columns = [{"name": column["name"], "type": column["type"],
                            "nullable": not bool(column["notnull"] or column["pk"]), "primary_key": bool(column["pk"])}
                           for column in db.execute("SELECT name,type,\"notnull\",pk FROM pragma_table_info(?)", (row["name"],))]
                tables.append({"name": row["name"], "columns": columns})
        return {"tables": tables, "examples": [
            {"name": "Games by year", "sql": "SELECT substr(start_local,1,4) AS year, count(*) AS games FROM games GROUP BY year ORDER BY year"},
            {"name": "Unresolved source records", "sql": "SELECT kind,key,status,http_status,last_error FROM resources WHERE status <> 'success' ORDER BY kind,key"},
            {"name": "Latest supplied leader entries", "sql": "SELECT p.player_id,p.team_id,p.division_id,p.goals,p.assists,p.points FROM player_leaders p JOIN resources r ON r.kind='division' AND r.key=p.division_id AND r.last_success_snapshot_id=p.snapshot_id LIMIT 100"},
        ]}

    def sql(self, statement):
        if not isinstance(statement, str) or not statement.strip():
            raise QueryError("Enter one SELECT or WITH query")
        try:
            statement_size = len(statement.encode("utf-8"))
        except UnicodeError:
            raise QueryError("SQL must contain valid Unicode text") from None
        if statement_size > SQL_LENGTH_LIMIT:
            raise QueryError("SQL is limited to 20,000 bytes")
        started = time.monotonic()
        deadline = started + SQL_TIMEOUT
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
        authorization_denied = False

        def authorize(action, first, second, database, trigger):
            nonlocal authorization_denied
            if action not in allowed:
                authorization_denied = True
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_FUNCTION and (second or first or "").lower() in {
                "load_extension", "readfile", "writefile", "edit", "fts3_tokenizer",
            }:
                authorization_denied = True
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        try:
            with self._connection() as db:
                db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, SQL_OUTPUT_LIMIT)
                db.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, SQL_LENGTH_LIMIT)
                db.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 200)
                db.setlimit(sqlite3.SQLITE_LIMIT_LIKE_PATTERN_LENGTH, 1000)
                db.set_authorizer(authorize)
                db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                cursor = db.execute(statement)
                if cursor.description is None:
                    raise QueryError("Only SELECT and WITH queries are allowed")
                columns = [column[0] for column in cursor.description]
                rows, truncated = [], False
                used = len(json.dumps(columns).encode("utf-8")) + 256
                for row in cursor:
                    if len(rows) >= SQL_ROW_LIMIT:
                        truncated = True
                        break
                    values = []
                    for value in row:
                        if isinstance(value, bytes):
                            value = "0x" + value.hex()
                        if isinstance(value, float) and not math.isfinite(value):
                            value = str(value)
                        if isinstance(value, str) and len(value.encode("utf-8")) > SQL_CELL_LIMIT:
                            raise QueryError("A result cell exceeds the 64 KiB limit")
                        values.append(value)
                    size = len(json.dumps(values, ensure_ascii=True).encode("utf-8")) + 2
                    if used + size > SQL_OUTPUT_LIMIT:
                        truncated = True
                        break
                    rows.append(values)
                    used += size
                return {"columns": columns, "rows": rows, "truncated": truncated,
                        "row_limit": SQL_ROW_LIMIT, "elapsed_ms": round((time.monotonic() - started) * 1000, 2)}
        except sqlite3.Error as exc:
            if time.monotonic() >= deadline or "interrupted" in str(exc).lower():
                raise QueryError("Query exceeded the two-second execution limit") from None
            message = " ".join(str(exc).split())
            if not authorization_denied and (
                re.fullmatch(r"(?:no such (?:column|table|function)|ambiguous column name): [\w.$ ]+", message)
                or re.fullmatch(r'near "[\w(),.;*+=<>!-]+": syntax error', message)
                or message == "incomplete input"
            ):
                raise QueryError(("SQL error: " + message)[:240]) from None
            raise QueryError("Query rejected: use one bounded SELECT or WITH query over archive tables") from None
