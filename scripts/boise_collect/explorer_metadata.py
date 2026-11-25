"""Read saved, verified metadata without changing the archive's source records."""

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import re


TEAM_FIELDS = {"Color": "color", "Dropped": "dropped", "div_code": "div_code",
               "division_name": "division_name"}
DIVISION_FIELDS = {
    "NumGames": "expected_games", "GameLength": "game_minutes", "DeadlineDate": "deadline",
    "FirstGameDate": "first_game_date", "LastGameDate": "last_game_date",
    "est_start_date": "raw_est_start", "est_end_date": "raw_est_end",
    "show_all_games": "show_all_games", "show_first_game": "show_first_game",
    "suppress_leaderboard": "suppress_leaderboard", "GirlGoalDoubleScore": "girl_goal_double_score",
    "GirlGoalDoubleStandings": "girl_goal_double_standings", "tournament_season": "tournament_season",
}
DEFINITION_FIELDS = {"DivisionCode": "code", "DivClass": "div_class", "PlayerType": "player_type",
                     "Gender": "gender", "Age": "age"}
SESSION_FIELDS = {"StartDate": "raw_start", "EndDate": "raw_end", "Open": "open"}
POINT_FIELDS = ("win_point", "tie_point", "loss_point", "goal_point")


def source(row):
    return {"snapshot_id": row["id"], "kind": row["kind"], "key": row["key"],
            "fetched_at": datetime.fromtimestamp(row["fetched_at"], timezone.utc).isoformat().replace("+00:00", "Z")}


def date_warnings(metadata):
    warnings = []
    for key in ("raw_start", "raw_end", "raw_est_start", "raw_est_end", "deadline"):
        value = metadata.get(key)
        if isinstance(value, str) and re.match(r"\d{4}-", value) and int(value[:4]) < 2000:
            warnings.append(f"Source {key.replace('_', ' ')} predates 2000; retained unchanged.")
    for start, end in (("raw_start", "raw_end"), ("raw_est_start", "raw_est_end")):
        if metadata.get(start) and metadata.get(end) and metadata[start] > metadata[end]:
            warnings.append("Source end date precedes its start date; retained unchanged.")
    return warnings


def load_metadata(root, snapshot_rows, latest_ids):
    """Read the latest successful evidence per resource, with per-field provenance."""
    values = {kind: defaultdict(dict) for kind in ("teams", "divisions", "sessions", "definitions")}
    sources = {kind: defaultdict(dict) for kind in values}
    snapshots = {row["id"]: source(row) for row in snapshot_rows}
    issues = []
    catalog_counts = {}

    def remember(kind, ident, attributes, provenance):
        if ident is None:
            return
        ident = str(ident)
        for key, value in attributes.items():
            if value is not None:
                values[kind][ident][key] = value
                sources[kind][ident][key] = provenance

    def walk(obj, provenance):
        if isinstance(obj, list):
            for child in obj:
                walk(child, provenance)
        elif isinstance(obj, dict):
            if isinstance(obj.get("upcoming_leagues"), list):
                catalog_counts[provenance["snapshot_id"]] = len({str(row["UDivSeasonID"])
                    for row in obj["upcoming_leagues"] if isinstance(row, dict) and row.get("UDivSeasonID") is not None})
            if "UTeamID" in obj:
                remember("teams", obj["UTeamID"], {target: obj.get(key) for key, target in TEAM_FIELDS.items()}, provenance)
            if "UDivSeasonID" in obj and any(key in obj for key in DIVISION_FIELDS):
                remember("divisions", obj["UDivSeasonID"],
                         {target: obj.get(key) for key, target in DIVISION_FIELDS.items()}, provenance)
            if "UDivisionID" in obj and "DivisionCode" in obj:
                remember("definitions", obj["UDivisionID"],
                         {target: obj.get(key) for key, target in DEFINITION_FIELDS.items()}, provenance)
            if "id" in obj and "SeasonNumber" in obj:
                remember("sessions", obj["id"], {target: obj.get(key) for key, target in SESSION_FIELDS.items()}, provenance)
            if isinstance(obj.get("division_season"), dict):
                rules = {key: obj[key] for key in POINT_FIELDS if obj.get(key) is not None}
                if rules:
                    remember("divisions", obj["division_season"].get("UDivSeasonID"), {"scoring_rules": rules}, provenance)
            for child in obj.values():
                if isinstance(child, (dict, list)):
                    walk(child, provenance)

    for row in sorted((row for row in snapshot_rows if row["id"] in latest_ids),
                      key=lambda row: (row["fetched_at"], row["id"])):
        expected = f"evidence/{row['evidence_sha256']}.json"
        path = root / expected
        try:
            if (row["evidence_path"] != expected or path.is_symlink()
                    or not path.resolve().is_relative_to(root / "evidence")
                    or path.stat().st_size > 16 * 1024 * 1024):
                raise ValueError
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != row["evidence_sha256"]:
                raise ValueError
            walk(json.loads(raw), snapshots[row["id"]])
        except (OSError, ValueError, RecursionError):
            issues.append(row["id"])
    return values, sources, snapshots, issues, catalog_counts


def metadata_for(kind, ident, record, snapshot_id, saved, saved_sources, snapshots, fields):
    metadata = dict(saved[kind].get(ident, {}))
    provenance = dict(saved_sources[kind].get(ident, {}))
    for key, value in metadata.items():
        if isinstance(value, dict):
            metadata[key] = dict(value)
            for child in value:
                if key in provenance:
                    provenance[key + "." + child] = provenance[key]
            provenance.pop(key, None)
    field_sources = record.get("_field_sources", {})
    for key in fields:
        if record.get(key) is None:
            continue
        entries = [(key + "." + child, value, child) for child, value in record[key].items()] \
            if isinstance(record[key], dict) else [(key, record[key], None)]
        for field, value, child in entries:
            if value is None:
                continue
            observed = snapshots.get(field_sources.get(field, field_sources.get(key, snapshot_id)))
            previous = provenance.get(field)
            if previous and (observed is None or (previous["fetched_at"], previous["snapshot_id"]) >
                             (observed["fetched_at"], observed["snapshot_id"])):
                continue
            if child is None:
                metadata[key] = value
            else:
                metadata.setdefault(key, {})[child] = value
            if observed:
                provenance[field] = observed
    return {"metadata": metadata, "metadata_sources": provenance,
            "metadata_warnings": date_warnings(metadata),
            "fetched_at": snapshots.get(snapshot_id, {}).get("fetched_at")}


def timing_summaries(db, records, snapshots):
    """Fill only absent timing from consistent observations of the same fixture."""
    candidates = defaultdict(lambda: defaultdict(list))
    for row in db.execute(
        "SELECT o.game_id,o.snapshot_id,json_extract(o.data_json,'$.slot_end') AS slot_end,"
        "json_extract(o.data_json,'$.slot_minutes') AS slot_minutes "
        "FROM game_observations o JOIN games g ON g.id=o.game_id "
        "WHERE json_extract(g.data_json,'$.slot_end') IS NULL "
        "AND json_extract(o.data_json,'$.slot_end') IS NOT NULL "
        "AND json_extract(o.data_json,'$.start_local') IS json_extract(g.data_json,'$.start_local') "
        "AND json_extract(o.data_json,'$.home_id') IS json_extract(g.data_json,'$.home_id') "
        "AND json_extract(o.data_json,'$.away_id') IS json_extract(g.data_json,'$.away_id') "
        "AND json_extract(o.data_json,'$.field') IS json_extract(g.data_json,'$.field')"
    ):
        latest = records.get(row["game_id"])
        if latest is None or latest.get("slot_end") is not None:
            continue
        candidates[row["game_id"]][row["slot_end"]].append((row["snapshot_id"], row["slot_minutes"]))
    result = {}
    for ident, latest in records.items():
        end, minutes = latest.get("slot_end"), latest.get("slot_minutes")
        provenance = "latest" if end is not None else "unknown"
        matches = []
        if end is not None:
            matches = [(latest["snapshot_id"], minutes)]
        elif len(candidates[ident]) == 1:
            candidate_end, matches = next(iter(candidates[ident].items()))
            known_minutes = {value for _, value in matches if value is not None}
            compatible = True
            if minutes is not None:
                try:
                    start = datetime.fromisoformat(latest["start_local"]).replace(tzinfo=None)
                    finish = datetime.fromisoformat(candidate_end.replace("Z", "+00:00")).replace(tzinfo=None)
                    compatible = abs((finish - start).total_seconds() / 60 - minutes) <= 0.001
                except (TypeError, ValueError):
                    compatible = False
                matches = [*matches, (latest["snapshot_id"], minutes)]
            if compatible:
                end = candidate_end
                if minutes is None:
                    minutes = next(iter(known_minutes)) if len(known_minutes) == 1 else None
                provenance = "earlier_observation"
            else:
                provenance = "conflict"
        elif candidates[ident]:
            provenance = "conflict"
            matches = [pair for group in candidates[ident].values() for pair in group]
        snapshot_ids = sorted({sid for sid, _ in matches}, reverse=True)
        result[ident] = {"latest_slot_end": latest.get("slot_end"), "latest_slot_minutes": latest.get("slot_minutes"),
                         "known_slot_end": end, "known_slot_minutes": minutes, "provenance": provenance,
                         "sources": [snapshots[sid] for sid in snapshot_ids[:5] if sid in snapshots],
                         "source_count": len(snapshot_ids)}
    return result
