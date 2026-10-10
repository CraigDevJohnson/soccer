# soccer

The Schedule Downloader's backend: it puts a Let's Play Soccer Team's games into a calendar. It is unofficial and not affiliated with Let's Play Soccer.

This is one Tool behind [The Lobby](https://github.com/CraigDevJohnson/the-lobby), which owns every screen and sign-in (ADR 0001 there). The site calls this backend with signed requests to its Lambda address, and nothing else can reach it (ADR 0006). It is a Go/Chi server that runs on AWS Lambda through an adapter and as an ordinary server on a developer's computer (ADR 0002). The league client and the .ics writer come from the archived [soccer_scraper](https://github.com/CraigDevJohnson/soccer_scraper); its email change alerts were left behind.

## The Visitor flow

Boise only. A person finds a Team by Team ID, by name search, or by browsing the divisions at the Facility; combines several Teams; keeps the remaining games ticked (the default) or changes them; and gets either a file to download or a Session link that calendar apps fetch. Every game is a 45-minute event in Mountain time.

| Method and path | What it does |
|---|---|
| `GET /teams/{teamID}` | One Team and its games, each marked `remaining` or not |
| `GET /schedule?team=…&team=…` | Several Teams combined (up to 10); a game between two of them appears once. Teams that can't be read are listed in `failed` |
| `GET /teams?q=…` | Name search at the Facility |
| `GET /divisions` | Divisions at the Facility |
| `GET /divisions/{divisionID}/teams` | Teams in one division |
| `POST /calendar` | The chosen games as an .ics download |
| `POST /links` | A Session link for the chosen games: `{token, url, teams, failed}` |
| `GET /links/{token}` (or `{token}.ics`) | What a calendar app fetches: the chosen games, read afresh from the league |
| `GET /healthz` | 204 |

`POST /calendar` and `POST /links` take the Teams and only what the person changed from the default:

```json
{"team_ids": ["123456", "654321"], "excluded": ["<game id unticked>"], "included": ["<played game id ticked>"]}
```

A Session link's token carries that choice and the moment it was made, so nothing about a Visitor is stored. Games starting after that moment are in unless excluded, so games the league posts later join the link by themselves.

Errors are JSON `{code, message}`: `invalid_team_id` (400), `team_not_found` (404), `invalid_link` (404), `league_unavailable` (503, also logged as a `league_unavailable` line for an alert), and `not_connected` (501) for the name search and division browsing until the league addresses behind them are wired in.

## Logs

One JSON line per request with the route pattern (never the path, so link tokens and searches stay out), status, timing and user agent. The route of each `POST` says which output a person chose; the user agent on `/links/{token}` says which calendar app fetched a link.

## Running it

Needs Go (see `go.mod`) and [Task](https://taskfile.dev).

```sh
task run        # http://127.0.0.1:8081 (SOCCER_ADDR to change)
task ci         # gofmt, vet, tests
task lambda     # dist/soccer.zip for provided.al2023 on arm64
```

| Variable | Meaning |
|---|---|
| `LEAGUE_BASE_URL` | The league's schedule service; defaults to `https://lps-api-prod.lps-test.com` |
| `LINK_BASE_URL` | Public address Session link tokens are appended to, such as `https://craigdevjohnson.com/soccer/link/`; when unset, `POST /links` returns only the token |
| `SOCCER_ADDR` | Local listen address |

## Licence

MIT
