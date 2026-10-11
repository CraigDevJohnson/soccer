# soccer

The Schedule Downloader's backend: it puts a Let's Play Soccer Team's games into a calendar. It is unofficial and not affiliated with Let's Play Soccer.

This is one Tool behind [The Lobby](https://github.com/CraigDevJohnson/the-lobby), which owns every screen and sign-in (ADR 0001 there). The site calls this backend with signed requests to its Lambda address, and nothing else can reach it (ADR 0006). It is a Go/Chi server that runs on AWS Lambda through an adapter and as an ordinary server on a developer's computer (ADR 0002). The league client and the .ics writer come from the archived [soccer_scraper](https://github.com/CraigDevJohnson/soccer_scraper). Left behind on purpose: its email change alerts and DynamoDB, and its "Special Event" title for one Team, since event wording is deferred in the baseline. The league's schedule service is undocumented. The client reads the shape seen live on 2026-10-10 (`GET /teams/{id}`: a `team` object with its division, `games` with `UGameID`, the division `standings`), still accepts soccer_scraper's older `game_id`, accepts numbers or strings where the league might change its mind, and treats any answer it cannot read as the league being unavailable, never as a Team being gone. A Team ID belongs to one season; the same people get a new one when they sign up again.

## The Visitor flow

Boise only. A person finds a Team by Team ID, by name search, or by browsing the divisions at the Facility; combines several Teams; keeps the remaining games ticked (the default) or changes them; and gets either a file to download or a Session link that calendar apps fetch. Every game is a 45-minute event in Mountain time.

| Method and path | What it does |
|---|---|
| `GET /teams/{teamID}` | One Team and its games, each marked `remaining` or not |
| `GET /schedule?team=…&team=…` | Several Teams combined (up to 10); a game between two of them appears once. Teams that can't be read are listed in `failed` |
| `GET /teams?q=…` | Name search at the Facility (a Team ID matches too) |
| `GET /divisions` | Divisions at the Facility |
| `GET /divisions/{divisionID}/teams` | Teams in one division |
| `POST /calendar` | The chosen games as an .ics download |
| `POST /links` | A Session link for the chosen games: `{token, url, teams, failed}` |
| `GET /links/{token}` (or `{token}.ics`) | What a calendar app fetches: the chosen games, read afresh from the league. `HEAD` works too |
| `GET /healthz` | 204 |

`POST /calendar` and `POST /links` take the Teams and only what the person changed from the default:

```json
{"team_ids": ["123456", "654321"], "excluded": ["<game id unticked>"], "included": ["<played game id ticked>"], "since": "<as_of from the schedule>"}
```

`since` is the `as_of` of the schedule the person ticked from, so a game that starts while the page is open stays ticked; without it the default is judged from the moment of the request.

A Session link's token carries that choice and the moment it was made, so nothing about a Visitor is stored. Games starting after that moment are in unless excluded, so games the league posts later join the link by themselves.

Errors are JSON `{code, message}`: `invalid_team_id` (400), `team_not_found` (404), `invalid_link` (404), `too_large` (413), `not_found` and `method_not_allowed` for unknown routes, `league_unavailable` (503, also logged as a `league_unavailable` line for an alert).

When the league fails for any one Team, a download, a new Session link and a Session link fetch all answer 503 rather than leaving that Team out: a file would look complete, a link would lose the Team for good, and a calendar app keeps its last full copy on a 503 but would delete the missing Team's games on a 200.

## Name search and division browsing

The league's own team search needs a league account, and it has no public division list. What it does have is a public day schedule, `GET /games?facility_id=4&date=YYYY-MM-DD`, listing every game at Boise that day with both Teams' IDs, names and divisions. The Directory reads the coming seven days (every division plays weekly), builds the division and Team lists from them, and keeps that scan in memory for an hour. A Team with no game in the coming week is not listed, and a search answers at most 50 Teams. When a refresh fails, the last scan still serves (with a `directory_stale` log line); with no scan on hand yet, the lookup is `league_unavailable`. Craig chose this over waiting for a real league route on 2026-10-10.

## Logs

One JSON line per request with the route pattern (never the path, so link tokens and searches stay out), status, timing and user agent. The route of each `POST` says which output a person chose; the user agent on `/links/{token}` says which calendar app fetched a link.

## Running it

Needs Go (see `go.mod`) and [Task](https://taskfile.dev).

```sh
task run          # http://127.0.0.1:8081 (SOCCER_ADDR to change)
task ci           # gofmt, vet, tests, and the OpenTofu checks below
task lambda       # dist/soccer.zip for provided.al2023 on arm64
task infra:check  # format, validate and test infra/ offline (needs OpenTofu)
```

| Variable | Meaning |
|---|---|
| `LEAGUE_BASE_URL` | The league's schedule service; defaults to `https://lps-api-prod.lps-test.com` |
| `LEAGUE_FACILITY_ID` | The Facility in the league's `GET /facilities` list; defaults to `4`, Boise |
| `LINK_BASE_URL` | Public address Session link tokens are appended to, such as `https://craigdevjohnson.com/soccer/link/`; when unset, `POST /links` returns only the token |
| `SOCCER_ADDR` | Local listen address |

## Deploying it

`infra/` is the OpenTofu for one deployment: the function, its Function URL that only the site's role may call, its log group and two alarms. The private inputs (account, site role, alerts topic, link address) are gitignored copies of the `.example` files beside it. [docs/operations/deploy.md](docs/operations/deploy.md) has the exact steps; `task infra:plan` and `task infra:apply` run them, and the apply is an approved change (aws-setup's `aws-design.md`). GitHub Actions runs `task ci` on every pull request and never touches AWS.

## Licence

MIT
