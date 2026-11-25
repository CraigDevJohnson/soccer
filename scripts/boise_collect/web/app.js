"use strict";

const content = document.getElementById("main-content");
const pageSize = 50;
const state = { overview: null, sessions: null, controller: null, schema: null, sql: "", renderVersion: 0 };
const titles = { sessions: "Sessions", divisions: "Divisions", teams: "Teams", players: "Players", games: "Games", query: "Query", coverage: "Coverage" };
const descriptions = {
  sessions: "Follow a session into its divisions, teams, standings, and games.",
  teams: "Find a team by name or ID. Each record belongs to a particular session.",
  players: "Explore the player IDs and totals present in supplied division leaderboards.",
  games: "Search the collected fixtures and results. All times are Mountain time.",
  divisions: "Review a division’s supplied standings, leaderboard, and schedule."
};
const fmt = value => value === null || value === undefined ? "—" : Number(value).toLocaleString();
const text = value => value === null || value === undefined || value === "" ? "—" : String(value);
const counted = (value, singular, plural = singular + "s") => fmt(value) + " " + (Number(value) === 1 ? singular : plural);

function E(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === "class") node.className = value;
    else if (key === "value") node.value = value;
    else if (key === "checked" || key === "disabled" || key === "hidden") node[key] = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function hash(view, id = null, params = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value !== null && value !== undefined && value !== "") query.set(key, value);
  return "#" + view + (id ? "/" + encodeURIComponent(id) : "") + (query.size ? "?" + query : "");
}
function route() {
  const [path, query] = location.hash.slice(1).split("?");
  const [view, id] = (path || "sessions").split("/");
  let decoded = id || null;
  try { if (id) decoded = decodeURIComponent(id); } catch { /* The API will reject the malformed ID. */ }
  return { view: titles[view] ? view : "sessions", id: decoded, params: Object.fromEntries(new URLSearchParams(query || "")) };
}
function go(view, id = null, params = {}) {
  const target = hash(view, id, params);
  if (location.hash === target) render(); else location.hash = target;
}
function L(label, view, id = null, params = {}, className = "primary-link") {
  return E("a", { href: hash(view, id, params), class: className }, text(label));
}
function pill(label, kind = "muted") { return E("span", { class: "pill pill-" + kind }, label); }
function notice(message, warning = false) { return E("div", { class: "notice" + (warning ? " warning" : "") }, message); }
function loading() { return E("div", { class: "loading", role: "status" }, E("span", { class: "loading-dot" }), "Loading archive…"); }
function toast(message) {
  const node = document.getElementById("toast");
  node.textContent = message; node.hidden = false;
  window.clearTimeout(toast.timer); toast.timer = window.setTimeout(() => { node.hidden = true; }, 4500);
}
async function api(path, params = {}, options = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value !== "" && value !== null && value !== undefined) query.set(key, value);
  const response = await fetch("/api/" + path + (query.size ? "?" + query : ""), { signal: state.controller?.signal, ...options });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "The archive could not be read.");
  return result;
}
function dateLabel(value, short = false) {
  if (!value) return "Date unavailable";
  const raw = String(value).slice(0, 10);
  const parsed = new Date(raw + "T12:00:00Z");
  if (Number.isNaN(parsed.valueOf())) return raw;
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", ...(short ? {} : { year: "numeric" }), timeZone: "UTC" }).format(parsed);
}
function dateRange(start, end) {
  if (!start && !end) return "No dated games";
  if (start === end || !end) return dateLabel(start);
  return dateLabel(start) + " – " + dateLabel(end);
}
function wallTime(value) {
  if (!value || !String(value).includes("T")) return "Time unavailable";
  const [hour, minute] = String(value).slice(11, 16).split(":");
  const h = Number(hour);
  return (h % 12 || 12) + ":" + minute + (h >= 12 ? " PM" : " AM");
}
function fetchedLabel(value) {
  if (value === null || value === undefined || value === "") return "Not collected";
  const parsed = new Date(typeof value === "number" ? value * 1000 : value);
  if (Number.isNaN(parsed.valueOf())) return "Time unavailable";
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit", timeZone: "America/Denver", timeZoneName: "short" }).format(parsed);
}
function sourceTime(value) {
  return value ? dateLabel(value) + " · " + wallTime(value) + " MT" : "Not supplied";
}
function flagLabel(value) { return value === true ? "Yes" : value === false ? "No" : "Not supplied"; }
function infoPanel(title, rows, description = null) {
  return E("section", { class: "info-panel" }, E("h2", {}, title), description ? E("p", {}, description) : null,
    E("dl", { class: "info-list" }, rows.flatMap(([label, value]) => [E("dt", {}, label), E("dd", {}, value === null || value === undefined || value === "" ? "Not supplied" : value)])));
}
function metadataWarnings(item) {
  return [...new Set([...(item.metadata_warnings || []), ...(item.metadata?.date_warnings || [])])];
}
function recordStatus(item) {
  if (item.catalog_only) return pill("Catalog only");
  return item.details_available === false ? pill("Unavailable", "warn") : pill("Supplied", "good");
}
function sourceLabel(source) {
  if (!source) return "Source unavailable";
  if (source.kind === "division") return L("Division " + source.key, "divisions", source.key);
  if (source.kind === "catalog") return "Upcoming catalog";
  return source.kind === "day" ? "Day " + source.key : text(source.kind);
}
const metadataLabels = {
  raw_start: "Source start date", raw_end: "Source end date", open: "Open for registration",
  definition_id: "Definition ID", code: "Division code", div_class: "Class", player_type: "Player type",
  gender: "Gender category", age: "Age category", expected_games: "Expected games", game_minutes: "Match length",
  deadline: "Registration deadline", raw_est_start: "Estimated start", raw_est_end: "Estimated end",
  first_game_date: "Source first game", last_game_date: "Source last game", show_all_games: "Show all games",
  show_first_game: "Show first game", suppress_leaderboard: "Suppress leaderboard",
  girl_goal_double_score: "Double girls’ goals in scores", girl_goal_double_standings: "Double girls’ goals in standings",
  tournament_season: "Tournament season", scoring_rules: "Scoring rules", color: "Team color", dropped: "Dropped registration",
  div_code: "Division code", division_name: "Source division name", fees_active: "Fees active", individual_fee_active: "Individual fee active",
  "fees.published": "Published fee", "fees.team": "Team fee", "fees.referee": "Referee fee", "fees.individual": "Individual fee", "fees.deposit": "Deposit",
  "scoring_rules.win_point": "Win points", "scoring_rules.tie_point": "Tie points", "scoring_rules.loss_point": "Loss points", "scoring_rules.goal_point": "Goal point cap"
};
function metadataProvenance(item) {
  const sources = Object.entries(item.metadata_sources || {}).map(([field, source]) => ({ field, ...source })).sort((a, b) => a.field.localeCompare(b.field));
  if (!sources.length) return null;
  return E("details", { class: "source-details" }, E("summary", {}, "Where this metadata came from"),
    grid([{ title: "Field", render: r => metadataLabels[r.field] || r.field.replaceAll("_", " ") }, { title: "Source", render: sourceLabel }, { title: "Fetched", render: r => fetchedLabel(r.fetched_at) }, { title: "Snapshot", key: "snapshot_id", numeric: true }], sources));
}
function sourceDates(metadata, keys) {
  return keys.map(([key, label]) => [label, metadata[key] ? E("span", { class: "mono" }, metadata[key]) : null]);
}
function metadataPanels(view, item) {
  const m = item.metadata || {}, nodes = [];
  const warnings = metadataWarnings(item);
  if (warnings.length) nodes.push(notice(warnings.join(" "), true));
  if (view === "sessions") {
    nodes.push(E("div", { class: "columns metadata-panels" },
      infoPanel("Source season metadata", [...sourceDates(m, [["raw_start", "Source start date"], ["raw_end", "Source end date"]]), ["Open for registration", flagLabel(m.open)]], "Source dates are shown as supplied. Observed game dates above remain separate."),
      infoPanel("Collection status", [["Records", item.catalog_only ? "Catalog metadata only" : "Observed division records"], ["Latest record fetched", fetchedLabel(item.fetched_at)]], item.catalog_only ? "No validated division details are available for this session yet. No dated games does not mean the session has no games." : "Metadata may come from a different snapshot than the games. Expand the source list to inspect it.")));
  } else if (view === "divisions") {
    const rules = m.scoring_rules || {}, fees = m.fees || {};
    nodes.push(E("div", { class: "columns metadata-panels" },
      infoPanel("Division classification", [["Definition ID", m.definition_id], ["Division code", m.code], ["Class", m.div_class], ["Player type", m.player_type], ["Gender category", m.gender], ["Age category", m.age]]),
      infoPanel("Competition metadata", [["Expected games", m.expected_games], ["Match length", m.game_minutes === null || m.game_minutes === undefined ? null : m.game_minutes + " minutes"], ["Registration deadline", m.deadline], ["Win / tie / loss points", [rules.win_point, rules.tie_point, rules.loss_point].map(text).join(" / ")], ["Goal point cap", rules.goal_point], ["Tournament season", flagLabel(m.tournament_season)]])));
    nodes.push(E("details", { class: "source-details" }, E("summary", {}, "More source metadata: fees, publication flags and dates"), E("div", { class: "columns metadata-panels" },
      infoPanel("Published fees", [["Published fee", fees.published], ["Team fee", fees.team], ["Referee fee", fees.referee], ["Individual fee", fees.individual], ["Deposit", fees.deposit], ["Fees active", flagLabel(m.fees_active)], ["Individual fee active", flagLabel(m.individual_fee_active)]], "Values are shown as supplied, without an assumed currency. These are published fees, not an account’s payment history."),
      infoPanel("Publication & scoring flags", [["Show all games", flagLabel(m.show_all_games)], ["Show first game", flagLabel(m.show_first_game)], ["Suppress leaderboard", flagLabel(m.suppress_leaderboard)], ["Double girls’ goals in scores", flagLabel(m.girl_goal_double_score)], ["Double girls’ goals in standings", flagLabel(m.girl_goal_double_standings)]], "These source settings help explain what was published. They do not establish complete coverage."),
      infoPanel("Source dates & freshness", [...sourceDates(m, [["raw_est_start", "Estimated start"], ["raw_est_end", "Estimated end"], ["first_game_date", "Source first game"], ["last_game_date", "Source last game"]]), ["Latest record fetched", fetchedLabel(item.fetched_at)]], "Source dates remain uncorrected. Missing dates are not inferred from the schedule."))));
  } else if (view === "teams") {
    nodes.push(E("div", { class: "columns metadata-panels" },
      infoPanel("Registration metadata", [["Team color", m.color ?? item.color], ["Dropped registration", flagLabel(m.dropped)], ["Division code", m.div_code], ["Source division name", m.division_name]]),
      infoPanel("Record freshness", [["Latest record fetched", fetchedLabel(item.fetched_at)]], "These attributes describe this registration. Matching team names across sessions do not establish the same club or players.")));
  }
  nodes.push(metadataProvenance(item));
  return nodes.filter(Boolean);
}
function gameTiming(item) {
  const timing = item.timing || {}, sources = timing.sources || [];
  const notes = {
    latest: "The slot end comes from the latest retained game record.",
    earlier_observation: "The latest record does not supply a slot end. A consistent earlier observation supplies the end; any latest supplied slot length is retained.",
    conflict: "Saved timing observations conflict. Review the observations before using a slot end.",
    unknown: "No usable slot end was supplied in the saved observations."
  };
  const winner = item.winner_id ? L(item.winner_id === item.home_id ? item.home_name || "Team " + item.winner_id : item.winner_id === item.away_id ? item.away_name || "Team " + item.winner_id : "Team " + item.winner_id, "teams", item.winner_id) : null;
  const panels = E("div", { class: "columns metadata-panels" },
    infoPanel("Published game metadata", [["Field", item.field], ["Source winner", winner], ["Latest record fetched", fetchedLabel(item.fetched_at)]], "A missing winner is not treated as a draw or a forfeit."),
    infoPanel("Supplied slot timing", [["Latest record slot end", sourceTime(timing.latest_slot_end)], ["Latest slot length", timing.latest_slot_minutes == null ? null : timing.latest_slot_minutes + " minutes"], ["Available slot end", sourceTime(timing.known_slot_end)], ["Available slot length", timing.known_slot_minutes == null ? null : timing.known_slot_minutes + " minutes"], ["Timing source", { latest: "Latest record", earlier_observation: "Earlier observation", conflict: "Conflicting observations", unknown: "Unavailable" }[timing.provenance] || "Unavailable"]], notes[timing.provenance] || notes.unknown));
  const provenance = sources.length ? E("details", { class: "source-details" }, E("summary", {}, "Supporting timing observations (" + fmt(timing.source_count ?? sources.length) + ")"), grid([{ title: "Source", render: sourceLabel }, { title: "Fetched", render: r => fetchedLabel(r.fetched_at) }, { title: "Snapshot", key: "snapshot_id", numeric: true }], sources)) : null;
  return [timing.provenance === "conflict" ? notice(notes.conflict, true) : null, panels, provenance].filter(Boolean);
}
function collectionStatus(data) {
  const catalog = data.catalog || state.overview?.catalog, freshness = data.freshness || state.overview?.freshness;
  if (!catalog && !freshness) return null;
  const job = freshness?.refresh_job;
  const status = catalog?.status || "not collected";
  return section("Catalog & freshness", E("div", { class: "columns metadata-panels" },
    infoPanel("Upcoming catalog", [["Request state", pill(status.replaceAll("_", " "), status === "success" ? "good" : ["failed", "invalid", "blocked"].includes(status) ? "warn" : "muted")], ["Latest successful fetch", fetchedLabel(catalog?.last_success_at)], ["Divisions in snapshot", catalog?.division_count], ["Snapshot", catalog?.snapshot_id]], "Catalog metadata can describe divisions before games or standings are supplied."),
    infoPanel("Source refreshes", [["Day schedules fetched", fetchedLabel(freshness?.latest_day_fetch)], ["Division details fetched", fetchedLabel(freshness?.latest_division_fetch)], ["Catalog fetched", fetchedLabel(freshness?.latest_catalog_fetch)], ["Recorded refresh status", job?.status ? job.status.replaceAll("_", " ") : "No recorded refresh"], ...(job ? [["Refresh dates", dateRange(job.start_date, job.end_date)], ["Completed / total", fmt(job.completed) + " / " + fmt(job.total)], ["Pending", job.pending], ["Skipped source gaps", job.skipped]] : [])], "Fetch times show when saved responses were collected. They do not establish that every score is final.")));
}
function heading(title, description, tools = []) {
  return E("div", { class: "page-heading" }, E("div", {}, E("h1", {}, title), description ? E("p", {}, description) : null), tools.length ? E("div", { class: "heading-tools" }, tools) : null);
}
function statRow(items) {
  return E("div", { class: "stat-row" }, items.map(([value, label, detail]) => E("div", { class: "stat" }, E("span", { class: "stat-value" }, text(value)), E("span", { class: "stat-label" }, label), detail ? E("div", { class: "stat-detail" }, detail) : null)));
}
function section(title, body, action = null) { return E("section", { class: "section-block" }, E("div", { class: "section-heading" }, E("h2", {}, title), action), body); }
function sourceRecord(record) { return E("details", { class: "source-details" }, E("summary", {}, "Inspect normalized record"), E("pre", { class: "code-panel" }, JSON.stringify(record, null, 2))); }

function csvDownload(columns, rows, name) {
  const cell = value => {
    let result = value === null || value === undefined ? "" : typeof value === "object" ? JSON.stringify(value) : String(value);
    if (typeof value === "string" && /^(?:\s*[=+@-]|[\t\r])/.test(result)) result = "'" + result;
    return '"' + result.replaceAll('"', '""') + '"';
  };
  const csv = [columns, ...rows].map(row => row.map(cell).join(",")).join("\r\n");
  const url = URL.createObjectURL(new Blob(["\uFEFF", csv], { type: "text/csv;charset=utf-8" }));
  const a = E("a", { href: url, download: "boise-archive-" + name + ".csv" });
  document.body.append(a); a.click(); a.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function exportRows(items, name) {
  if (!items.length) return toast("There are no displayed rows to export.");
  const scalar = value => value === null || typeof value !== "object";
  const rows = items.map(item => {
    const row = Object.fromEntries(Object.entries(item).filter(([, value]) => scalar(value)));
    for (const group of ["metadata", "timing"]) {
      for (const [key, value] of Object.entries(item[group] || {})) {
        if (scalar(value)) row[group + "." + key] = value;
        else if (group === "metadata" && ["scoring_rules", "fees"].includes(key)) for (const [field, supplied] of Object.entries(value)) row[group + "." + key + "." + field] = supplied;
      }
    }
    if (metadataWarnings(item).length) row.metadata_warnings = metadataWarnings(item).join(" | ");
    if (Object.keys(item.metadata_sources || {}).length) row.metadata_sources = JSON.stringify(item.metadata_sources);
    return row;
  });
  const keys = [...new Set(rows.flatMap(row => Object.keys(row)))];
  csvDownload(keys, rows.map(row => keys.map(key => row[key])), name);
}
function grid(columns, items, { caption = "", result = null, onPage = null, exportName = null, emptyTitle = "No matching records", emptyText = "Try a different search or clear the filters." } = {}) {
  const table = E("table", {}, E("thead", {}, E("tr", {}, columns.map(c => E("th", { scope: "col", class: c.numeric ? "numeric" : "" }, c.title)))), E("tbody", {}, items.map(row => E("tr", {}, columns.map(c => E("td", { class: c.numeric ? "numeric" : "" }, c.render ? c.render(row) : text(row[c.key])))))));
  const panel = E("div", { class: "table-panel" });
  panel.append(E("div", { class: "table-caption" }, E("strong", {}, caption || counted(result?.total ?? items.length, "record")), exportName ? E("button", { class: "button button-small", type: "button", disabled: !items.length, onClick: () => exportRows(items, exportName) }, "Export displayed rows") : E("span", { class: "record-count" }, fmt(items.length) + " shown")));
  if (items.length) panel.append(E("div", { class: "table-wrap" }, table));
  else panel.append(E("div", { class: "empty" }, E("h3", {}, emptyTitle), E("p", {}, emptyText)));
  if (result && onPage) {
    const offset = Number(result.offset || 0), limit = Number(result.limit || pageSize), total = Number(result.total || 0);
    panel.append(E("div", { class: "pager" }, E("span", {}, total ? `${fmt(offset + 1)}–${fmt(Math.min(offset + items.length, total))} of ${fmt(total)}` : "0 results"), E("div", { class: "pager-actions" }, E("button", { class: "button button-small", disabled: offset === 0, onClick: () => onPage(Math.max(0, offset - limit)) }, "← Previous"), E("button", { class: "button button-small", disabled: offset + limit >= total, onClick: () => onPage(offset + limit) }, "Next →"))));
  }
  return panel;
}

const columns = {
  sessions: [
    { title: "Session", render: r => E("div", { class: "session-number" }, E("span", { class: "session-marker" }, r.number), E("div", {}, L("Session " + r.number, "sessions", r.id), E("span", { class: "cell-secondary" }, "ID " + r.id), r.catalog_only || metadataWarnings(r).length ? E("div", { class: "cell-status" }, r.catalog_only ? pill("Catalog only") : null, metadataWarnings(r).length ? pill("Source warning", "warn") : null) : null)) },
    { title: "Observed games", render: r => dateRange(r.start_date, r.end_date) },
    { title: "Divisions", numeric: true, render: r => fmt(r.division_count) },
    { title: "Teams", numeric: true, render: r => fmt(r.team_count) },
    { title: "Games", numeric: true, render: r => fmt(r.game_count) }
  ],
  divisions: [
    { title: "Division", render: r => E("div", {}, L(r.name || "Division " + r.id, "divisions", r.id), E("span", { class: "cell-secondary" }, "ID " + r.id + (r.metadata?.code ? " · " + r.metadata.code : ""))) },
    { title: "Session", render: r => r.session_id ? L(r.session_number, "sessions", r.session_id) : "—" },
    { title: "Teams", numeric: true, render: r => fmt(r.team_count) },
    { title: "Games", numeric: true, render: r => fmt(r.game_count) },
    { title: "Division details", render: recordStatus }
  ],
  teams: [
    { title: "Team registration", render: r => E("div", {}, L(r.name || "Team " + r.id, "teams", r.id), E("span", { class: "cell-secondary" }, "ID " + r.id), r.metadata?.dropped === true ? pill("Dropped registration", "warn") : null) },
    { title: "Session", render: r => r.session_id ? L("Session " + r.session_number, "sessions", r.session_id) : text(r.session_number) },
    { title: "Division", render: r => r.division_id ? L(r.division_name || "Division " + r.division_id, "divisions", r.division_id) : "—" },
    { title: "Games", numeric: true, render: r => fmt(r.game_count) },
    { title: "Observed range", render: r => dateRange(r.start_date, r.end_date) }
  ],
  players: [
    { title: "Player ID", render: r => L("Player " + r.id, "players", r.id) },
    { title: "Leaderboard entries", numeric: true, render: r => fmt(r.entry_count) },
    { title: "Sessions", numeric: true, render: r => fmt(r.session_count) },
    { title: "Teams", numeric: true, render: r => fmt(r.team_count) },
    { title: "Recorded goals", numeric: true, render: r => fmt(r.recorded_goals) },
    { title: "Recorded assists", numeric: true, render: r => fmt(r.recorded_assists) }
  ],
  games: [
    { title: "Date / time", render: r => E("div", {}, L(dateLabel(r.start_local), "games", r.id), E("span", { class: "cell-secondary" }, wallTime(r.start_local))) },
    { title: "Home", render: r => E("div", {}, L(r.home_name || "Team " + r.home_id, "teams", r.home_id), E("span", { class: "cell-secondary" }, "#" + r.home_id)) },
    { title: "Result", render: r => E("a", { class: "score" + (r.scored ? "" : " score-unplayed"), href: hash("games", r.id), "aria-label": "Game " + r.id + ": " + (r.scored ? `${r.home_score} to ${r.away_score}` : "unscored") }, r.scored ? `${r.home_score} – ${r.away_score}` : "Unscored") },
    { title: "Away", render: r => E("div", {}, L(r.away_name || "Team " + r.away_id, "teams", r.away_id), E("span", { class: "cell-secondary" }, "#" + r.away_id)) },
    { title: "Field", render: r => text(r.field) },
    { title: "Game ID", render: r => L(r.id, "games", r.id, {}, "mono") }
  ]
};
function standingColumns() {
  return [{ title: "Pos", key: "position", numeric: true }, { title: "Team", render: r => L(r.team_name || "Team " + r.team_id, "teams", r.team_id) }, ...[["GP", "games_played"], ["W", "wins"], ["L", "losses"], ["T", "ties"], ["GF", "goals_for"], ["GA", "goals_against"], ["Pts", "points"]].map(([title, key]) => ({ title, key, numeric: true, render: r => fmt(r[key]) }))];
}
function leaderColumns(withSession = false) {
  return [{ title: "Player", render: r => L("Player " + r.player_id, "players", r.player_id) }, ...(withSession ? [{ title: "Session", render: r => r.session_id ? L(r.session_number, "sessions", r.session_id) : text(r.session_number) }, { title: "Division", render: r => L(r.division_name || r.division_id, "divisions", r.division_id) }] : []), { title: "Team", render: r => L(r.team_name || "Team " + r.team_id, "teams", r.team_id) }, ...[["Goals", "goals"], ["Assists", "assists"], ["Points", "points"]].map(([title, key]) => ({ title, key, numeric: true, render: r => fmt(r[key]) }))];
}

async function sessions() {
  if (!state.sessions) state.sessions = (await api("sessions", { limit: 200 })).items;
  return state.sessions;
}
function inputFilter(name, label, value, placeholder = "", type = "text", extraClass = "") {
  const id = "filter-" + name;
  return E("div", { class: "filter " + extraClass }, E("label", { for: id }, label), E("input", { id, name, class: "field", type, value: value || "", placeholder, autocomplete: "off", maxlength: name === "search" ? 160 : 30 }));
}
function selectFilter(name, label, value, options) {
  const id = "filter-" + name;
  const select = E("select", { id, name, class: "field" }, options.map(([key, caption]) => E("option", { value: key }, caption)));
  select.value = value || "";
  return E("div", { class: "filter" }, E("label", { for: id }, label), select);
}
async function filters(view, params) {
  const form = E("form", { class: "filters", "aria-label": "Filter " + view });
  form.append(inputFilter("search", view === "players" ? "Player ID" : "Search", params.search, { sessions: "Session number or ID", teams: "Team name or registration ID", players: "Source player ID", games: "Game ID or team name", divisions: "Division name or ID" }[view], "search", "search"));
  if (view !== "sessions") {
    const list = await sessions();
    form.append(selectFilter("session", "Session", params.session, [["", "All sessions"], ...list.map(s => [s.id, "Session " + s.number + (s.start_date ? " · " + s.start_date.slice(0, 4) : "")])]));
  }
  if (["teams", "games", "players"].includes(view)) form.append(inputFilter("division", "Division ID", params.division, "Any division"));
  if (["games", "players"].includes(view)) form.append(inputFilter("team", "Team ID", params.team, "Any registration"));
  if (view === "games") {
    form.append(inputFilter("from", "From date", params.from, "", "date"), inputFilter("to", "Through date", params.to, "", "date"), inputFilter("field", "Field", params.field, "Any field", "number"), selectFilter("status", "Result", params.status, [["", "All games"], ["scored", "Scored"], ["unscored", "Unscored"]]));
    form.querySelector("[name=field]").min = "1";
    form.querySelector("[name=field]").step = "1";
  }
  if (params.year) form.append(E("input", { type: "hidden", name: "year", value: params.year }));
  if (params.player && view === "teams") form.append(E("input", { type: "hidden", name: "player", value: params.player }));
  form.append(E("button", { type: "submit", class: "button button-primary" }, "Apply"));
  if (Object.keys(params).some(k => !["limit", "offset"].includes(k))) form.append(E("button", { type: "button", class: "button button-quiet", onClick: () => go(view) }, "Clear"));
  form.addEventListener("submit", event => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(form));
    for (const key of ["team", "division", "field"]) if (values[key] && !/^[1-9][0-9]*$/.test(values[key])) return toast(key === "field" ? "Use a positive field number, or leave the field blank." : "Use a numeric " + key + " ID, or follow a link from its record.");
    if (values.from && values.to && values.from > values.to) return toast("The start date must be on or before the end date.");
    go(view, null, values);
  });
  return form;
}
function yearStrip(params) {
  const years = state.overview?.years || [];
  return E("div", { class: "year-strip", "aria-label": "Filter sessions by observed year" }, [["", "All years"], ...years.map(year => [String(year), String(year)])].map(([value, label]) => E("button", { type: "button", class: (params.year || "") === value ? "active" : "", "aria-pressed": (params.year || "") === value ? "true" : "false", onClick: () => go("sessions", null, { ...params, year: value, offset: "" }) }, label)));
}
async function listView(current) {
  const { view, params } = current;
  const [result, form] = await Promise.all([api(view, { ...params, limit: pageSize }), filters(view, params)]);
  const nodes = [heading(titles[view], descriptions[view])];
  if (view === "sessions") nodes.push(yearStrip(params));
  if (view === "players") nodes.push(notice("Player names were not retained. Search by source ID or browse a team’s supplied leaderboard. Totals cover listed leaderboard entries only; match appearances and complete career totals are unavailable."));
  if (view === "teams" && params.player) nodes.push(notice("These are team registrations linked to the player’s supplied leaderboard entries. They do not establish individual match appearances."));
  const noun = view === "teams" ? "team registration" : view === "players" ? "player ID" : view.slice(0, -1);
  nodes.push(form, grid(columns[view], result.items, { result, exportName: view, caption: counted(result.total, noun), onPage: offset => go(view, null, { ...params, offset }) }));
  if (view === "sessions") nodes.push(E("p", { class: "small-print" }, "Dates in this table reflect observed games. Catalog-only sessions remain visible under All years even before dated games are available. Source dates and their warnings are shown in session details."));
  if (view === "games") nodes.push(E("p", { class: "small-print" }, "Displayed scores are the source’s records. A score alone does not establish a forfeit or administrative result. Division expansion also supplied games after the discovery interval."));
  return nodes;
}

async function detailView(current) {
  const { view, id, params } = current;
  const detail = await api(view + "/" + encodeURIComponent(id));
  const item = detail.item;
  const nodes = [E("a", { class: "detail-back", href: hash(view === "divisions" ? "sessions" : view) }, "← Back to " + (view === "divisions" ? "sessions" : view))];
  if (view === "sessions") {
    nodes.push(heading("Session " + item.number, dateRange(item.start_date, item.end_date) + " · Observed games · Source season ID " + item.id), statRow([[fmt(item.game_count), "Archived games"], [fmt(item.team_count), "Team registrations"], [fmt(item.division_count), "Divisions"], [item.number, "Source session number"]]));
    nodes.push(E("div", { class: "detail-tabs" }, L("Browse session games", "games", null, { session: id }, ""), L("Browse session teams", "teams", null, { session: id }, ""), L("Supplied player leaders", "players", null, { session: id }, "")));
    nodes.push(...metadataPanels(view, item));
    nodes.push(section("Divisions in this session", grid(columns.divisions, detail.divisions || [], { exportName: "session-" + item.number + "-divisions" })));
  } else if (view === "divisions") {
    nodes.push(heading(item.name || "Division " + id, "Division " + id + " · " + dateRange(item.start_date, item.end_date)), E("div", { class: "detail-tabs" }, item.session_id ? L("Session " + item.session_number, "sessions", item.session_id, {}, "") : null, L("Division games", "games", null, { division: id }, ""), L("Division teams", "teams", null, { division: id }, "")));
    nodes.push(statRow([[fmt(item.game_count), "Archived games"], [fmt(item.team_count), "Team registrations"], [item.details_available === false ? "—" : fmt(detail.standings?.length), "Standing rows supplied"], [item.details_available === false ? "—" : fmt(detail.leaders?.length), "Leaderboard entries"]]));
    if (item.details_available === false) nodes.push(notice(item.catalog_only ? "Catalog metadata is available, but no validated division detail is available yet. Standings and leaderboard entries remain unknown." : "Validated division details are unavailable. Known games and registrations from other source records remain available; official standings and leaderboard details are unavailable.", !item.catalog_only));
    nodes.push(...metadataPanels(view, item));
    nodes.push(section("Published standings", grid(standingColumns(), detail.standings || [], { exportName: "division-" + id + "-standings", emptyTitle: "No supplied standings", emptyText: "No official standings were supplied for this division. Missing values do not mean zero." })));
    nodes.push(section("Supplied leaderboard", E("div", {}, E("p", { class: "small-print" }, "These are the supplied leaderboard entries, not a complete roster. Player names were not retained."), grid(leaderColumns(), detail.leaders || [], { exportName: "division-" + id + "-leaders", emptyText: "No leaderboard entries were supplied. This does not establish a complete player roster." }))));
    nodes.push(E("p", { class: "small-print" }, "Standings are the league’s published table when fetched. They are kept separately from calculations based on displayed game scores."));
  } else if (view === "teams") {
    const standing = detail.standing;
    nodes.push(heading(item.name || "Team " + id, "Team registration " + id + " · " + dateRange(item.start_date, item.end_date)), E("div", { class: "detail-tabs" }, item.session_id ? L("Session " + item.session_number, "sessions", item.session_id, {}, "") : null, item.division_id ? L(item.division_name || "Division " + item.division_id, "divisions", item.division_id, {}, "") : null, L("Filter this team’s games", "games", null, { team: id }, "")));
    nodes.push(statRow([[fmt(item.game_count), "Archived games"], [fmt(standing?.games_played), "Official games played"], [standing ? [standing.wins, standing.losses, standing.ties].map(text).join(" / ") : "—", "Official W / L / T"], [fmt(standing?.points), "Official points"]]));
    if (!standing) nodes.push(notice("An official standing is unavailable for this registration. Schedule records are still shown when another verified source supplied them.", true));
    nodes.push(...metadataPanels(view, item));
    const games = await api("games", { team: id, limit: pageSize, offset: params.offset || 0 });
    nodes.push(section("Games", grid(columns.games, games.items, { result: games, exportName: "team-" + id + "-games", onPage: offset => go("teams", id, { offset }) })));
    nodes.push(section("Supplied player leaders", E("div", {}, E("p", { class: "small-print" }, "Only players present in the supplied leaderboard are listed. This is not a complete roster."), grid(leaderColumns(), detail.leaders || [], { exportName: "team-" + id + "-leaders", emptyText: "This registration has no supplied leaderboard entries. No roster or player match appearances can be inferred." }))));
    if (detail.other_registrations?.length) nodes.push(section("Other registrations with this name", E("div", {}, E("p", { class: "small-print" }, "Matching names are shown separately. They do not prove these registrations are the same club or players."), grid(columns.teams, detail.other_registrations, { exportName: "same-name-registrations" }))));
  } else if (view === "players") {
    nodes.push(heading("Player " + id, "Source player ID · Supplied leaderboard entries"), notice("The archive contains IDs rather than player names. These entries are partial leaderboard records, not a roster, match appearances, or complete career statistics."), statRow([[fmt(item.entry_count), "Leaderboard entries"], [fmt(item.session_count), "Sessions represented"], [fmt(item.recorded_goals), "Goals in supplied entries"], [fmt(item.recorded_assists), "Assists in supplied entries"]]));
    nodes.push(E("div", { class: "detail-tabs" }, L("Linked team registrations", "teams", null, { player: id }, "")));
    nodes.push(section("Recorded entries", grid(leaderColumns(true), detail.entries || [], { exportName: "player-" + id + "-entries" })));
  } else if (view === "games") {
    nodes.push(heading("Game " + id, "Archived fixture and source observations"));
    const side = key => E("div", { class: "match-team" }, E("span", { class: "side" }, key === "home" ? "Home" : "Away"), L(item[key + "_name"] || "Team " + item[key + "_id"], "teams", item[key + "_id"]), E("span", { class: "cell-secondary" }, "Registration " + item[key + "_id"]), item[key + "_session_id"] ? L("Session " + item[key + "_session_number"], "sessions", item[key + "_session_id"], {}, "small-print") : null);
    nodes.push(E("section", { class: "game-hero" }, E("div", { class: "match-date" }, dateLabel(item.start_local) + " · " + wallTime(item.start_local) + " MT · Field " + text(item.field)), E("div", { class: "matchup" }, side("home"), E("span", { class: "score" + (item.scored ? "" : " score-unplayed") }, item.scored ? item.home_score + " – " + item.away_score : "Unscored"), side("away")), E("div", { class: "match-note" }, "The published score is preserved without inferring an administrative outcome.")));
    if (item.home_session_number !== item.away_session_number) nodes.push(notice("The participants belong to different source sessions. This game appears under either participant’s session and is counted once globally.", true));
    if (metadataWarnings(item).length) nodes.push(notice(metadataWarnings(item).join(" "), true));
    nodes.push(...gameTiming(item));
    nodes.push(section("Source observations", grid([{ title: "Source", render: sourceLabel }, { title: "Fetched", render: r => fetchedLabel(r.fetched_at) }, { title: "Snapshot", key: "snapshot_id", numeric: true }], detail.observations || [])));
    nodes.push(sourceRecord(detail.record || item));
  }
  if (detail.notes?.length) nodes.push(notice(detail.notes.join(" ")));
  return nodes;
}

async function queryView() {
  if (!state.schema) state.schema = await api("schema");
  const schema = state.schema;
  const examples = schema.examples || [{ name: "Games by year", sql: "SELECT substr(start_local, 1, 4) AS year, count(*) AS games\nFROM games\nGROUP BY year\nORDER BY year;" }];
  if (!state.sql) state.sql = examples[0]?.sql || "SELECT count(*) AS games FROM games;";
  const textarea = E("textarea", { class: "field query-editor", "aria-label": "Read-only SQL query", spellcheck: "false", maxlength: 18000, value: state.sql });
  const resultNode = E("div", { class: "query-result", "aria-live": "polite" });
  const runButton = E("button", { class: "button button-primary", type: "button" }, "Run query");
  const run = async () => {
    if (runButton.disabled) return;
    state.sql = textarea.value;
    runButton.disabled = true; runButton.textContent = "Running…"; resultNode.replaceChildren(loading());
    try {
      const result = await api("query", {}, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sql: state.sql }) });
      const rows = result.rows.map((values, i) => ({ values, index: i }));
      const resultColumns = result.columns.map((title, index) => ({ title, render: row => E("div", { class: "query-cell" }, typeof row.values[index] === "object" && row.values[index] !== null ? JSON.stringify(row.values[index]) : text(row.values[index])) }));
      const actions = E("div", { class: "query-tools" }, E("span", { class: "record-count" }, `${counted(result.rows.length, "row")} · ${Math.round(result.elapsed_ms)} ms`), result.truncated ? pill("Limited to " + fmt(result.row_limit) + " rows", "warn") : null, E("button", { class: "button button-small", type: "button", disabled: !rows.length, onClick: () => csvDownload(result.columns, result.rows, "query") }, "Export results"));
      resultNode.replaceChildren(actions, grid(resultColumns, rows, { caption: "Query results", emptyText: "The query returned no rows." }));
    } catch (error) {
      if (error.name !== "AbortError") resultNode.replaceChildren(E("div", { class: "error-box", role: "alert" }, error.message));
    } finally { runButton.disabled = false; runButton.textContent = "Run query"; }
  };
  runButton.addEventListener("click", run);
  textarea.addEventListener("input", () => { state.sql = textarea.value; });
  textarea.addEventListener("keydown", event => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter") { event.preventDefault(); run(); } });
  const aside = E("aside", { class: "query-aside" }, E("h3", {}, "Start with a query"), E("div", { class: "preset-list" }, examples.map(example => E("button", { type: "button", onClick: () => { textarea.value = example.sql; state.sql = example.sql; textarea.focus(); } }, example.name))), E("div", { class: "schema-list" }, E("h3", {}, "Archive schema"), (schema.tables || []).map(table => E("details", {}, E("summary", {}, table.name), E("div", { class: "schema-columns" }, table.columns.map(column => E("div", {}, column.name, " ", E("span", { class: "muted" }, column.type || ""))))))));
  return [heading("Query the archive", "Read-only SQL for questions that go beyond the filters."), notice("Queries cannot change the archive or attach other databases. Results and execution time are bounded. Player totals represent supplied leaderboard entries only."), E("div", { class: "query-layout" }, E("div", { class: "query-main" }, textarea, E("div", { class: "query-tools" }, runButton, E("span", { class: "query-help" }, "⌘ / Ctrl + Enter to run · Up to 500 rows")), resultNode), aside)];
}

async function coverageView() {
  const data = await api("coverage");
  const coverage = data.coverage || data;
  const yearly = data.yearly_dates || data.by_year || coverage.by_year || [];
  const gaps = data.resource_gaps || data.gaps || data.unresolved_resources || data.ledger?.unresolved_resources || [];
  const requested = coverage.requested_dates ?? state.overview?.coverage?.requested_dates;
  const validated = coverage.dates_with_verified_evidence ?? coverage.validated_dates ?? state.overview?.coverage?.validated_dates;
  const failed = gaps.filter(row => row.kind === "division").length || state.overview?.coverage?.failed_divisions || 0;
  const invalid = gaps.filter(row => row.kind === "day").length || state.overview?.coverage?.invalid_dates || 0;
  const nodes = [heading("Coverage & source quality", "See what was collected, what could not be validated, and what remains uncertain."), notice("The backfill attempted the declared dates. Available records do not establish all-time completeness or an authoritative start of retention.", true), statRow([[fmt(validated) + " / " + fmt(requested), "Validated dates"], [fmt(invalid), "Invalid day responses"], [fmt(failed), "Unavailable divisions"], [fmt(data.scoring_discrepancies ?? data.discrepancy_count ?? state.overview?.coverage?.scoring_discrepancies), "Metric discrepancies"]])];
  if (data.source_changed) nodes.push(notice("The archive changed after this view was loaded. Use Reload archive to read the newer snapshots."));
  if (data.report_fresh === false) nodes.push(notice("The saved coverage report predates the loaded archive. Request states come from the loaded records; detailed source-quality metrics need a new coverage report.", true));
  if (data.metadata_evidence_issues > 0) nodes.push(notice("Metadata evidence could not be fully verified (" + counted(data.metadata_evidence_issues, "issue") + "). Treat missing attributes as unknown and review the source warnings.", true));
  const statusPanel = collectionStatus(data);
  if (statusPanel) nodes.push(statusPanel);
  const yearPanel = E("div", { class: "info-panel" }, E("h2", {}, "Date coverage by year"), yearly.map(row => {
    const valid = row.dates_with_verified_evidence ?? row.validated ?? row.success ?? 0;
    const total = row.requested_dates ?? row.requested ?? 0;
    return E("div", { class: "coverage-year" }, E("strong", { class: "mono" }, row.year), E("progress", { class: "coverage-bar", value: valid, max: total || 1, "aria-label": `${row.year}: ${valid} of ${total} dates validated` }), E("span", { class: "number" }, fmt(valid) + " / " + fmt(total)));
  }));
  const quality = E("div", { class: "info-panel" }, E("h2", {}, "How to read this archive"), E("ul", { class: "plain-list" }, E("li", {}, "Invalid responses stay visible as gaps; unknown records are not empty games."), E("li", {}, "Some games are recovered through division records even when a daily response is invalid."), E("li", {}, "Published standings and score-derived comparisons can disagree. Neither is silently rewritten."), E("li", {}, "Division expansion includes some games after the discovery interval."), E("li", {}, "Player names, complete rosters and individual match appearances are unavailable."), E("li", {}, "Team registrations with matching names are not automatically merged.")), E("p", { class: "small-print" }, "Evidence integrity: " + ((data.evidence?.complete ?? coverage.evidence_complete ?? state.overview?.coverage?.evidence_complete) ? "verified by the saved coverage report" : "see saved report for details")));
  nodes.push(E("div", { class: "columns" }, yearPanel, quality));
  nodes.push(section("Unresolved source records", grid([{ title: "Source", render: sourceLabel }, { title: "State", render: r => pill(r.status, "warn") }, { title: "HTTP", key: "http_status", numeric: true }, { title: "Reason", render: r => r.last_error === "invalid_source_response" ? "Source response could not be validated" : r.last_error === "http_500" ? "Upstream server error" : text(r.last_error) }, { title: "Attempts", key: "attempts", numeric: true }], gaps, { exportName: "coverage-gaps", emptyText: "No unresolved request records were reported." })));
  return nodes;
}

function updateOverview(overview) {
  state.overview = overview;
  const c = overview.counts || {};
  document.getElementById("archive-line").replaceChildren(...[[c.games, "games"], [c.teams, "team registrations"], [c.sessions, "sessions"]].flatMap(([count, label], index) => [index ? E("span", { class: "separator", "aria-hidden": "true" }) : null, E("span", {}, E("strong", {}, fmt(count)), " " + label)]).filter(Boolean));
  document.getElementById("session-count").textContent = fmt(c.sessions);
  const gaps = (overview.coverage?.invalid_dates || 0) + (overview.coverage?.failed_divisions || 0);
  document.getElementById("gap-count").textContent = gaps ? fmt(gaps) : "";
  if (overview.catalog) document.getElementById("archive-line").append(E("span", {}, "Catalog ", E("strong", {}, overview.catalog.status === "success" ? counted(overview.catalog.division_count, "division") : String(overview.catalog.status || "not collected").replaceAll("_", " "))));
  document.getElementById("archive-date").textContent = "Latest fetch " + fetchedLabel(overview.archive?.latest_fetch) + " · Local, read-only archive";
}
async function render() {
  const version = ++state.renderVersion;
  state.controller?.abort(); state.controller = new AbortController();
  const current = route();
  document.querySelectorAll("nav a[data-view]").forEach(a => {
    const selected = a.dataset.view === current.view || (current.view === "divisions" && a.dataset.view === "sessions");
    if (selected) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  document.title = (titles[current.view] || "Archive") + " · Boise Soccer Archive";
  content.replaceChildren(loading());
  try {
    if (!state.overview) updateOverview(await api("overview"));
    const nodes = current.view === "query" ? await queryView() : current.view === "coverage" ? await coverageView() : current.id ? await detailView(current) : await listView(current);
    if (version !== state.renderVersion) return;
    content.replaceChildren(...nodes);
  } catch (error) {
    if (error.name === "AbortError" || version !== state.renderVersion) return;
    content.replaceChildren(heading("This view could not be loaded", "The archive remains unchanged."), E("div", { class: "error-box", role: "alert" }, error.message), E("button", { class: "button", onClick: render }, "Try again"), E("a", { class: "button button-quiet", href: "#sessions" }, "Browse sessions"));
  }
}
document.getElementById("reload").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const overview = await api("reload", {}, { method: "POST", signal: undefined });
    state.sessions = null; state.schema = null; updateOverview(overview); await render(); toast("Archive reloaded. No data was changed.");
  } catch (error) { if (error.name !== "AbortError") toast(error.message); }
  finally { button.disabled = false; }
});
window.addEventListener("hashchange", render);
document.querySelector(".skip-link").addEventListener("click", event => {
  event.preventDefault();
  content.focus();
  content.scrollIntoView({ block: "start" });
});
render();
