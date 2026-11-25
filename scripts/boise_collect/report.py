"""Read-only inventory and reconciliation of the local Boise collection.

Reports describe observed records and interval coverage. They never establish
an all-time retention boundary or turn calculated scores into official results.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time


_CAVEATS = [
    "All-time completeness is unproven: the earliest source retention boundary is unresolved and no complete historical source catalog is available.",
    "Checking every date in a declared interval cannot recover deleted, unpublished, or never-scheduled divisions.",
    "Official standings are retained as published at collection time, not as historical weekly snapshots.",
    "Numeric-score calculations do not identify forfeits, administrative decisions, penalties, or tie-break rules.",
    "Expected-game differences can reflect future fixtures, tournaments, publication limits, or source adjustments; they are discrepancies, not proven bugs.",
    "Player leaders are only supplied leaderboard entries; missing entries do not establish zero statistics or complete player histories.",
    "Source conflicts compare the latest successful observation from each endpoint, which may have been fetched at different times.",
    "The overall game inventory retains records previously observed; division audits use only each division endpoint's latest successful snapshot.",
]


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: str | None, default=None):
    return json.loads(value) if value is not None else default


def _numeric(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _game_state(game: dict) -> str:
    scores = (game.get("home_score"), game.get("away_score"))
    if all(_numeric(score) and score >= 0 for score in scores):
        return "scored"
    if all(score is None for score in scores) and not str(game.get("result") or "").strip():
        return "unscored"
    return "unclassified"


def _year(game: dict) -> str:
    value = game.get("start_local")
    if not isinstance(value, str):
        return "unknown"
    try:
        return str(date.fromisoformat(value[:10]).year)
    except ValueError:
        return "unknown"


def _check_evidence(root: Path, snapshots: list[dict]) -> tuple[dict, dict[int, dict]]:
    issues = []
    documents = {}
    root = root.resolve()
    for snapshot in snapshots:
        relative = snapshot["evidence_path"]
        issue = {"snapshot_id": snapshot["id"], "kind": snapshot["kind"], "key": snapshot["key"], "path": relative}
        expected_hash = snapshot["evidence_sha256"]
        expected_path = f"evidence/{expected_hash}.json"
        if relative != expected_path:
            issues.append({**issue, "problem": "unexpected_evidence_path"})
            continue
        path = root / relative
        if not path.resolve().is_relative_to(root / "evidence") or path.is_symlink():
            issues.append({**issue, "problem": "unsafe_evidence_path"})
            continue
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            issues.append({**issue, "problem": "missing_file"})
            continue
        except OSError:
            issues.append({**issue, "problem": "unreadable_file"})
            continue
        if hashlib.sha256(raw).hexdigest() != expected_hash:
            issues.append({**issue, "problem": "hash_mismatch"})
            continue
        try:
            document = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            issues.append({**issue, "problem": "invalid_json"})
            continue
        if not isinstance(document, dict):
            issues.append({**issue, "problem": "invalid_document"})
            continue
        documents[snapshot["id"]] = document
    return {
        "snapshots_checked": len(snapshots),
        "verified_snapshots": len(documents),
        "complete": not issues,
        "issues": issues,
    }, documents


def _coverage(intervals: list[dict], resources: list[dict], documents: dict, snapshot_map: dict, observations: dict) -> dict:
    days = set()
    interval_dates = []
    for interval in intervals:
        start = date.fromisoformat(interval["start_day"])
        end = date.fromisoformat(interval["end_day"])
        requested = {(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)}
        days.update(requested)
        interval_dates.append((interval, requested))
    day_resources = {r["key"]: r for r in resources if r["kind"] == "day"}

    def metrics(selected: set[str]) -> dict:
        checked = 0
        evidenced = 0
        empty = 0
        states = Counter()
        for day in selected:
            resource = day_resources.get(day)
            states[resource["status"] if resource else "not_queued"] += 1
            if resource is None:
                continue
            snapshot_id = resource["last_success_snapshot_id"]
            snapshot = snapshot_map.get(snapshot_id)
            if snapshot is None or (snapshot["kind"], snapshot["key"]) != ("day", day):
                continue
            checked += 1
            if snapshot_id in documents:
                evidenced += 1
                # Valid day evidence has a field-to-slots object. Successful
                # parsing can yield no soccer games even when bookings exist.
                if isinstance(documents[snapshot_id].get("data"), dict) and not observations.get(snapshot_id):
                    empty += 1
        return {
            "requested_dates": len(selected),
            "dates_with_successful_snapshot": checked,
            "dates_with_verified_evidence": evidenced,
            "empty_successful_dates": empty,
            "dates_unchecked": len(selected) - checked,
            "dates_without_verified_evidence": len(selected) - evidenced,
            "resource_states": dict(sorted(states.items())),
            "all_dates_checked": bool(selected) and checked == len(selected),
            "all_dates_evidenced": bool(selected) and evidenced == len(selected),
        }

    yearly = defaultdict(set)
    for day in days:
        yearly[day[:4]].add(day)
    return {
        **metrics(days),
        "intervals": [{**interval, **metrics(selected)} for interval, selected in interval_dates],
        "by_year": [{"year": int(year), **metrics(selected)} for year, selected in sorted(yearly.items())],
        "start_day": min(days) if days else None,
        "end_day": max(days) if days else None,
        "all_time_complete": False,
        "retention_boundary": "unresolved",
    }


def _scoring_rules(evidence: dict | None) -> dict | None:
    keys = ("win_point", "tie_point", "loss_point", "goal_point")
    if evidence is None or not all(_numeric(evidence.get(key)) for key in keys):
        return None
    if any(evidence[key] < 0 for key in keys):
        return None
    return {key: evidence[key] for key in keys}


def _audit_division(division: dict, resource: dict, games: list[dict], standings: list[dict], evidence: dict | None) -> dict:
    rules = _scoring_rules(evidence)
    listed_teams = {row["team_id"] for row in standings}
    game_teams = {g[side] for g in games for side in ("home_id", "away_id") if g.get(side)}
    team_results = []
    # Crossover opponents are useful observations too, but this division's
    # expected-game count and official table do not describe those opponents.
    audit_standings = list(standings)
    for team_id in sorted(game_teams - listed_teams):
        audit_standings.append({"team_id": team_id, **dict.fromkeys(("position", "games_played", "wins", "losses", "ties", "goals_for", "goals_against", "points"))})
    for standing in audit_standings:
        team_id = standing["team_id"]
        team_games = [g for g in games if team_id in (g.get("home_id"), g.get("away_id"))]
        scored = [g for g in team_games if _game_state(g) == "scored"]
        counts = Counter(_game_state(g) for g in team_games)
        derived = {key: 0 for key in ("wins", "losses", "ties", "goals_for", "goals_against")}
        derived["points"] = 0 if rules is not None else None
        for game in scored:
            home = game.get("home_id") == team_id
            goals_for = game["home_score"] if home else game["away_score"]
            goals_against = game["away_score"] if home else game["home_score"]
            result = "wins" if goals_for > goals_against else "losses" if goals_for < goals_against else "ties"
            derived[result] += 1
            derived["goals_for"] += goals_for
            derived["goals_against"] += goals_against
            if rules is not None:
                rule = {"wins": "win_point", "losses": "loss_point", "ties": "tie_point"}[result]
                derived["points"] += rules[rule] + min(goals_for, rules["goal_point"])
        official = {key: standing[key] for key in ("position", "games_played", "wins", "losses", "ties", "goals_for", "goals_against", "points")}
        expected_games = division.get("expected_games") if team_id in listed_teams else None
        discrepancies = []

        def compare(metric: str, observed, expected, interpretation: str):
            if observed is not None and expected is not None and observed != expected:
                discrepancies.append({"metric": metric, "observed": observed, "official_or_expected": expected, "difference": observed - expected, "interpretation": interpretation})

        compare("scored_vs_official_games_played", len(scored), official["games_played"], "Numeric scored games differ from official games played; administrative outcomes or incomplete evidence may explain this.")
        compare("scheduled_vs_official_games_played", len(team_games), official["games_played"], "Scheduled records may include unplayed games.")
        compare("scheduled_vs_expected_games", len(team_games), expected_games, "Expected counts may differ for upcoming fixtures, tournaments, publication limits, or adjustments.")
        for key, value in derived.items():
            compare(f"derived_{key}", value, official[key], "Numeric-score calculation differs from the official standing; retain both values without inferring an administrative outcome.")
        team_results.append({
            "team_id": team_id,
            "official_standing_present": team_id in listed_teams,
            "scheduled_games": len(team_games),
            "dated_games": sum(g.get("start_local") is not None for g in team_games),
            "scored_games": len(scored),
            "unscored_games": counts["unscored"],
            "unclassified_games": counts["unclassified"],
            "expected_games": expected_games,
            "official": official,
            "derived_from_numeric_scores": derived,
            "derived_points_state": "calculated_from_supplied_rules" if rules is not None else "unknown_scoring_rules",
            "discrepancies": discrepancies,
        })
    game_states = Counter(_game_state(g) for g in games)
    state = division.get("standings_state")
    if state == "present" and not standings:
        state = "empty"
    elif state not in {"null", "present"}:
        state = "unknown"
    return {
        "division_id": division["id"],
        "snapshot_id": resource["last_success_snapshot_id"],
        "last_success_at": resource["last_success_at"],
        "resource_status": resource["status"],
        "using_previous_success_while_unresolved": resource["status"] != "success",
        "stale_after_refresh_failure": resource["status"] in {"failed", "missing", "invalid", "blocked"},
        "evidence_verified": evidence is not None,
        "standings_state": state,
        "game_state": "empty" if not games else "present",
        "games": len(games),
        "scored_games": game_states["scored"],
        "unscored_games": game_states["unscored"],
        "unclassified_games": game_states["unclassified"],
        "undated_games": sum(g.get("start_local") is None for g in games),
        "teams_without_standings": sorted(game_teams - listed_teams),
        "scoring_rules": rules,
        "scoring_interpretation": "Outcome points plus one point per goal capped by goal_point per game; numeric results only." if rules else None,
        "teams": sorted(team_results, key=lambda row: row["team_id"]),
    }


def _conflicts(resources: list[dict], observations: dict[int, list[dict]], snapshots: dict[int, dict]) -> list[dict]:
    by_game = defaultdict(list)
    for resource in resources:
        snapshot_id = resource["last_success_snapshot_id"]
        if snapshot_id not in snapshots:
            continue
        snapshot = snapshots[snapshot_id]
        for game in observations.get(snapshot_id, []):
            by_game[game["id"]].append({"game": game, "source": {"kind": resource["kind"], "key": resource["key"], "snapshot_id": snapshot_id, "fetched_at": snapshot["fetched_at"], "resource_status": resource["status"]}})
    fields = ("facility_id", "home_id", "away_id", "start_local", "home_score", "away_score", "winner_id", "field", "slot_end", "slot_minutes", "result")
    conflicts = []
    for game_id, sources in sorted(by_game.items()):
        differences = {}
        for field in fields:
            known = [source for source in sources if source["game"].get(field) is not None]
            if field == "result":
                known = [source for source in known if str(source["game"][field]).strip()]
            # Whitespace differences in numeric score strings are not distinct
            # outcomes, while nonnumeric published statuses must stay visible.
            def comparable(source):
                game = source["game"]
                if field == "result" and _game_state(game) == "scored":
                    return (game["home_score"], game["away_score"])
                return game[field]
            values = {json.dumps(comparable(source), sort_keys=True) for source in known}
            if len(values) > 1:
                differences[field] = [{**source["source"], "value": source["game"][field]} for source in known]
        if differences:
            conflicts.append({"game_id": game_id, "differences": differences})
    return conflicts


def _source_identity(value) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value)
    if not text.isascii() or not text.isdecimal() or int(text) <= 0:
        return None
    return str(int(text))


def _evidence_teams(document: dict):
    """Yield only known team-bearing wire paths, never infer membership by game."""
    for team in document.get("teams") or []:
        if isinstance(team, dict):
            yield team
    for leader in document.get("player_standings") or []:
        if isinstance(leader, dict) and isinstance(leader.get("team"), dict):
            yield leader["team"]
    games = list(document.get("games") or [])
    for slots in (document.get("data") or {}).values():
        for slot in slots:
            if isinstance(slot, dict) and isinstance(slot.get("game"), dict):
                games.append(slot["game"])
    for game in games:
        if not isinstance(game, dict):
            continue
        for side in ("home_team", "visitor_team"):
            if isinstance(game.get(side), dict):
                yield game[side]


def _division_label(division: dict, evidence: dict | None) -> dict:
    official = division.get("name")
    if isinstance(official, str) and official.strip():
        return {"name": official.strip(), "official_name": official, "name_source": "division_name"}
    labels = {
        team["division_name"].strip()
        for team in _evidence_teams(evidence or {})
        if _source_identity(team.get("UDivSeasonID")) == division["id"]
        and _source_identity(team.get("FacilityID", team.get("facility_id"))) == "4"
        and isinstance(team.get("division_name"), str) and team["division_name"].strip()
    }
    return {"name": next(iter(labels)) if len(labels) == 1 else None,
            "official_name": official,
            "name_source": "matching_team_metadata" if len(labels) == 1 else None}


def _discovery_coverage(resources: list[dict], observations: dict[int, list[dict]],
                        documents: dict[int, dict], discoveries: list[dict]) -> dict:
    """Check that visible game participants have evidenced discovery paths.

    Another endpoint's latest valid snapshot can fill missing team metadata,
    but obsolete refresh versions cannot establish a current relationship.
    """
    latest = {r["last_success_snapshot_id"]: r for r in resources
              if r["last_success_snapshot_id"] is not None}
    resource_map = {(r["kind"], r["key"]): r for r in resources}
    recorded = {(r["snapshot_id"], r["division_id"]) for r in discoveries}
    mappings = defaultdict(lambda: defaultdict(list))
    participants = defaultdict(list)
    for snapshot_id, resource in latest.items():
        source = {"kind": resource["kind"], "key": resource["key"], "snapshot_id": snapshot_id}
        for game in observations.get(snapshot_id, []):
            for side in ("home_id", "away_id"):
                participants[(game["id"], side, game.get(side))].append(source)
        if snapshot_id not in documents:
            continue
        for team in _evidence_teams(documents[snapshot_id]):
            team_id = _source_identity(team.get("UTeamID"))
            division_id = _source_identity(team.get("UDivSeasonID"))
            facility_id = _source_identity(team.get("FacilityID", team.get("facility_id")))
            if team_id is None or division_id is None or facility_id != "4":
                continue
            reference = {**source, "division_id": division_id,
                         "discovery_recorded": (snapshot_id, division_id) in recorded}
            if reference not in mappings[team_id][division_id]:
                mappings[team_id][division_id].append(reference)
    gaps = []
    resolved = 0
    for (game_id, side, team_id), sources in sorted(participants.items()):
        candidates = mappings.get(team_id, {})
        reasons = []
        if not candidates:
            reasons.append("team_division_mapping_unavailable")
        elif len(candidates) > 1:
            reasons.append("conflicting_team_division_mappings")
        else:
            division_id, references = next(iter(candidates.items()))
            if not any(reference["discovery_recorded"] for reference in references):
                reasons.append("division_discovery_not_recorded")
            if ("division", division_id) not in resource_map:
                reasons.append("division_not_queued")
        if reasons:
            gaps.append({"game_id": game_id, "side": side, "team_id": team_id,
                         "reasons": reasons, "observed_in": sources,
                         "candidate_division_ids": sorted(candidates),
                         "mapping_sources": [reference for refs in candidates.values() for reference in refs]})
        else:
            resolved += 1
    unresolved_divisions = []
    for resource in resources:
        if resource["kind"] != "division":
            continue
        snapshot_id = resource["last_success_snapshot_id"]
        if resource["status"] != "success" or snapshot_id not in documents:
            unresolved_divisions.append({"division_id": resource["key"], "status": resource["status"],
                                        "last_success_snapshot_id": snapshot_id,
                                        "evidence_verified": snapshot_id in documents})
    return {
        "complete": not gaps and not unresolved_divisions,
        "participant_mappings_complete": not gaps,
        "observed_game_participants": len(participants),
        "participants_with_verified_division_mapping": resolved,
        "unresolved_participant_count": len(gaps),
        "unresolved_gaps": gaps,
        "unresolved_divisions": unresolved_divisions,
        "scope": "Latest successful endpoint observations; explicit facility-4 team-to-division source references only.",
    }


def build_report(root: Path, now: float | None = None) -> dict:
    """Build an inventory without changing the database, queue, or evidence."""
    root = Path(root).resolve()
    database = root / "collector.sqlite3"
    if not database.is_file():
        raise FileNotFoundError(f"Collector database does not exist: {database}")
    now = time.time() if now is None else now
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")

        def rows(table: str) -> list[dict]:
            return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]

        intervals = sorted(rows("intervals"), key=lambda r: (r["start_day"], r["end_day"]))
        resources = sorted(rows("resources"), key=lambda r: (r["kind"], r["key"]))
        attempts = rows("attempts")
        snapshots = rows("snapshots")
        snapshot_map = {s["id"]: s for s in snapshots}
        evidence, documents = _check_evidence(root, snapshots)
        observations = defaultdict(list)
        for row in rows("game_observations"):
            observations[row["snapshot_id"]].append(_json(row["data_json"]))
        official = defaultdict(list)
        for row in rows("standings"):
            official[row["snapshot_id"]].append(row)
        divisions = [_json(row["data_json"]) for row in rows("divisions")]
        teams = [_json(row["data_json"]) for row in rows("teams")]
        games = [_json(row["data_json"]) for row in rows("games")]
        meta = {row["key"]: row["value"] for row in rows("meta")}
        seasons = [_json(row["data_json"]) for row in rows("seasons")]
        definitions = rows("division_definitions")
        leaders = rows("player_leaders")
        discoveries = rows("discoveries")
    finally:
        connection.close()

    resource_map = {(r["kind"], r["key"]): r for r in resources}
    audits = []
    division_inventory = []
    season_groups = {}
    for division in sorted(divisions, key=lambda d: d["id"]):
        resource = resource_map.get(("division", division["id"]))
        if resource is None or resource["last_success_snapshot_id"] not in snapshot_map:
            continue
        snapshot_id = resource["last_success_snapshot_id"]
        division_games = observations[snapshot_id]
        audit = _audit_division(division, resource, division_games, official[snapshot_id], documents.get(snapshot_id))
        audits.append(audit)
        division_years = sorted({_year(g) for g in division_games})
        entry = {
            "division_id": division["id"],
            **_division_label(division, documents.get(snapshot_id)),
            "definition_id": division.get("definition_id"), "season_id": division.get("season_id"),
            "season_number": division.get("season_number"), "game_years": division_years,
            "games": len(division_games), "teams_in_standings": len(official[snapshot_id]),
            "expected_games_per_team": division.get("expected_games"),
            "standings_state": audit["standings_state"], "resource_status": resource["status"],
            "snapshot_id": snapshot_id, "evidence_verified": audit["evidence_verified"],
            "show_all_games": division.get("show_all_games"), "show_first_game": division.get("show_first_game"),
        }
        division_inventory.append(entry)
        season_key = (division.get("season_id"), division.get("season_number"))
        group = season_groups.setdefault(season_key, {"season_id": season_key[0], "season_number": season_key[1], "division_ids": [], "game_ids": set(), "team_ids": set(), "game_years": set()})
        group["division_ids"].append(division["id"])
        group["game_ids"].update(g["id"] for g in division_games)
        group["team_ids"].update(row["team_id"] for row in official[snapshot_id])
        group["game_years"].update(division_years)
    season_inventory = []
    for group in season_groups.values():
        season_inventory.append({"season_id": group["season_id"], "season_number": group["season_number"], "division_ids": group["division_ids"], "divisions": len(group["division_ids"]), "unique_games": len(group["game_ids"]), "teams_in_standings": len(group["team_ids"]), "game_years": sorted(group["game_years"])})
    season_inventory.sort(key=lambda row: (str(row["season_id"] or ""), str(row["season_number"] or "")))
    years = defaultdict(list)
    for game in games:
        years[_year(game)].append(game)
    year_inventory = []
    for year, year_games in sorted(years.items()):
        states = Counter(_game_state(game) for game in year_games)
        year_inventory.append({"year": year, "unique_games": len(year_games), "scored_games": states["scored"], "unscored_games": states["unscored"], "unclassified_games": states["unclassified"], "unique_teams": len({g[side] for g in year_games for side in ("home_id", "away_id") if g.get(side)}), "division_ids": [d["division_id"] for d in division_inventory if year in d["game_years"]]})
    coverage = _coverage(intervals, resources, documents, snapshot_map, observations)
    unresolved = [{k: r[k] for k in ("kind", "key", "status", "attempts", "last_attempt_at", "last_success_at", "last_success_snapshot_id", "next_attempt_at", "last_error", "http_status")} for r in resources if r["status"] != "success"]
    observed_dates = sorted(g["start_local"][:10] for g in games if _year(g) != "unknown")
    conflicts = _conflicts(resources, observations, snapshot_map)
    discovery_coverage = _discovery_coverage(resources, observations, documents, discoveries)
    source_warnings = [{"snapshot_id": s["id"], "kind": s["kind"], "key": s["key"], "warnings": _json(s["warnings_json"], [])} for s in snapshots if _json(s["warnings_json"], [])]
    discrepancies = [{"division_id": audit["division_id"], "team_id": team["team_id"], **difference} for audit in audits for team in audit["teams"] for difference in team["discrepancies"]]
    latest_ids = {r["last_success_snapshot_id"] for r in resources if r["last_success_snapshot_id"] is not None}
    return {
        "report_version": 1, "generated_at": _iso(now), "facility_id": 4,
        "source_base_url": meta.get("base_url"), "all_time_complete": False,
        "retention_boundary": "unresolved", "coverage": coverage,
        "inventory_complete_for_declared_interval": coverage["all_dates_evidenced"] and not unresolved and evidence["complete"] and discovery_coverage["complete"],
        "discovery_coverage": discovery_coverage,
        "ledger": {
            "resource_count": len(resources), "resource_states": dict(sorted(Counter(r["status"] for r in resources).items())),
            "attempt_count": len(attempts), "attempt_outcomes": dict(sorted(Counter(a["outcome"] for a in attempts).items())),
            "unresolved_resources": unresolved, "stale_resources": [r for r in unresolved if r["last_success_snapshot_id"] is not None],
            "next_request_at": float(meta.get("next_request_at", "0")),
            "discovery_references": len(discoveries),
        },
        "inventory": {
            "divisions": len(divisions), "division_definitions": len(definitions), "source_seasons": len(seasons),
            "teams": len(teams), "unique_games": len(games), "snapshots": len(snapshots),
            "latest_player_leader_entries": sum(row["snapshot_id"] in latest_ids for row in leaders),
            "earliest_observed_game_date": observed_dates[0] if observed_dates else None,
            "latest_observed_game_date": observed_dates[-1] if observed_dates else None,
            "by_year": year_inventory, "by_source_season": season_inventory, "by_division": division_inventory,
        },
        "evidence": evidence, "division_audits": audits,
        "discrepancies": discrepancies, "source_conflicts": conflicts,
        "source_warnings": source_warnings, "caveats": list(_CAVEATS),
    }


def _cell(value) -> str:
    if value is None:
        return "unknown"
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _table(headers: list[str], rows: list[list]) -> list[str]:
    return ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |", *["| " + " | ".join(_cell(value) for value in row) + " |" for row in rows], ""]


def _markdown(report: dict) -> str:
    coverage = report["coverage"]
    inventory = report["inventory"]
    ledger = report["ledger"]
    lines = ["# Boise collection coverage", "", f"Generated {report['generated_at']}. Facility 4. Offline report from the local SQLite ledger.", "", "**All-time completeness: unproven. Earliest retention boundary: unresolved.**", "", f"Declared interval union: {coverage['start_day'] or 'none'} through {coverage['end_day'] or 'none'}; {coverage['requested_dates']} distinct dates. Successful date snapshots: {coverage['dates_with_successful_snapshot']}; verified date evidence: {coverage['dates_with_verified_evidence']}; unchecked dates: {coverage['dates_unchecked']}.", "", f"All declared dates checked: **{str(coverage['all_dates_checked']).lower()}**. All declared dates evidenced: **{str(coverage['all_dates_evidenced']).lower()}**. Interval collection and evidence complete: **{str(report['inventory_complete_for_declared_interval']).lower()}**.", "", f"Inventory: {inventory['divisions']} divisions, {inventory['teams']} team registrations, {inventory['unique_games']} unique observed games, {inventory['snapshots']} snapshots. Observed game dates: {inventory['earliest_observed_game_date'] or 'none'} through {inventory['latest_observed_game_date'] or 'none'}.", "", "## Declared intervals", ""]
    lines += _table(["Start", "End", "Dates", "Checked", "Verified", "Empty responses"], [[r["start_day"], r["end_day"], r["requested_dates"], r["dates_with_successful_snapshot"], r["dates_with_verified_evidence"], r["empty_successful_dates"]] for r in coverage["intervals"]])
    lines += ["## Date coverage by year", ""]
    lines += _table(["Year", "Dates", "Checked", "Verified", "Unchecked"], [[r["year"], r["requested_dates"], r["dates_with_successful_snapshot"], r["dates_with_verified_evidence"], r["dates_unchecked"]] for r in coverage["by_year"]])
    lines += ["## Game inventory by year", ""]
    lines += _table(["Year", "Games", "Scored", "Unscored", "Unclassified", "Teams", "Divisions"], [[r["year"], r["unique_games"], r["scored_games"], r["unscored_games"], r["unclassified_games"], r["unique_teams"], len(r["division_ids"])] for r in inventory["by_year"]])
    lines += ["## Inventory by source season", ""]
    lines += _table(["Source season ID", "Season number", "Divisions", "Unique games", "Standing teams", "Game years"], [[r["season_id"], r["season_number"], r["divisions"], r["unique_games"], r["teams_in_standings"], ", ".join(r["game_years"])] for r in inventory["by_source_season"]])
    lines += ["## Inventory by division", ""]
    lines += _table(["Division ID", "Name", "Season ID / number", "Games", "Standing teams", "Standings", "Fetch state"], [[r["division_id"], r["name"], f"{_cell(r['season_id'])} / {_cell(r['season_number'])}", r["games"], r["teams_in_standings"], r["standings_state"], r["resource_status"]] for r in inventory["by_division"]])
    lines += ["## Request ledger", "", f"{ledger['resource_count']} resources; {ledger['attempt_count']} attempts. States: {json.dumps(ledger['resource_states'], sort_keys=True)}. {len(ledger['stale_resources'])} resources retain earlier successful data while their latest attempt or refresh is unresolved.", "", f"Persistent earliest next-request time: {_iso(ledger['next_request_at']) if ledger['next_request_at'] else 'none'}.", ""]
    lines += _table(["Kind", "Key", "State", "Attempts", "HTTP", "Error code", "Retry epoch", "Last good snapshot"], [[r["kind"], r["key"], r["status"], r["attempts"], r["http_status"], r["last_error"], r["next_attempt_at"], r["last_success_snapshot_id"]] for r in ledger["unresolved_resources"]])
    discovery = report["discovery_coverage"]
    lines += ["## Discovery coverage", "", f"Discovery paths complete: **{str(discovery['complete']).lower()}**. {discovery['participants_with_verified_division_mapping']} of {discovery['observed_game_participants']} observed game participants have a verified team-to-division mapping and recorded discovery. {discovery['unresolved_participant_count']} participant mappings and {len(discovery['unresolved_divisions'])} discovered divisions remain unresolved. A successful date response alone does not establish that its divisions were discoverable.", ""]
    lines += _table(["Game", "Side", "Team", "Gap", "Candidate divisions"], [[r["game_id"], r["side"], r["team_id"], ", ".join(r["reasons"]), ", ".join(r["candidate_division_ids"])] for r in discovery["unresolved_gaps"]])
    lines += ["## Evidence integrity", "", f"{report['evidence']['verified_snapshots']} of {report['evidence']['snapshots_checked']} snapshots have present, valid, hash-matching evidence. Complete: **{str(report['evidence']['complete']).lower()}**.", ""]
    lines += _table(["Snapshot", "Source", "Issue"], [[r["snapshot_id"], f"{r['kind']}/{r['key']}", r["problem"]] for r in report["evidence"]["issues"]])
    lines += ["## Reconciliation", "", f"{len(report['discrepancies'])} metric discrepancies across {sum(bool(team['discrepancies']) for audit in report['division_audits'] for team in audit['teams'])} team standings. These are retained differences, not inferred forfeits or automatic corrections. Full team counts and numeric calculations are in coverage.json.", ""]
    lines += _table(["Division", "Standings", "Games", "Scored", "Unscored", "Unclassified", "Undated", "Outside / unlisted teams", "Scoring rules"], [[r["division_id"], r["standings_state"], r["games"], r["scored_games"], r["unscored_games"], r["unclassified_games"], r["undated_games"], len(r["teams_without_standings"]), "supplied" if r["scoring_rules"] else "unknown"] for r in report["division_audits"]])
    lines += ["## Source conflicts", "", f"{len(report['source_conflicts'])} games have differing known fields across the latest successful endpoint snapshots. Different fetch times can explain differences; earlier versions of the same endpoint are excluded. Details and values are retained in coverage.json.", ""]
    lines += _table(["Game", "Conflicting fields"], [[r["game_id"], ", ".join(sorted(r["differences"]))] for r in report["source_conflicts"]])
    lines += ["## Source warnings", ""]
    lines += _table(["Snapshot", "Source", "Warnings"], [[r["snapshot_id"], f"{r['kind']}/{r['key']}", "; ".join(r["warnings"])] for r in report["source_warnings"]])
    lines += ["## Limits", "", *[f"- {caveat}" for caveat in report["caveats"]], "", "## Individual discrepancy appendix", ""]
    lines += _table(["Division", "Team", "Metric", "Observed", "Official / expected", "Difference"], [[r["division_id"], r["team_id"], r["metric"], r["observed"], r["official_or_expected"], r["difference"]] for r in report["discrepancies"]])
    return "\n".join(lines)


def write_report(root: Path, output: Path, now: float | None = None) -> dict:
    """Write coverage.json and coverage.md to an explicit output directory."""
    report = build_report(root, now)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    for name, content in (("coverage.json", json.dumps(report, indent=2, sort_keys=True) + "\n"), ("coverage.md", _markdown(report))):
        destination = output / name
        temporary = output / (name + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(destination)
    return report
