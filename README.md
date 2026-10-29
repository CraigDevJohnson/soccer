# soccer

**Let's Plan Soccer.** Link. Plan. Score

The Let's Plan Soccer backend: it puts a Let's Play Soccer Team's games into a calendar. It is unofficial and not affiliated with Let's Play Soccer.

This is one Tool behind [The Lobby](https://github.com/CraigDevJohnson/the-lobby), which owns every screen and sign-in (ADR 0001 there). The site calls this backend with signed requests to its Lambda address, and nothing else can reach it (ADR 0006). It is a Go/Chi server that runs on AWS Lambda through an adapter and as an ordinary server on a developer's computer (ADR 0002). The league client and the .ics writer come from the archived [soccer_scraper](https://github.com/CraigDevJohnson/soccer_scraper). Left behind on purpose: its email change alerts and DynamoDB, and its "Special Event" title for one Team; every game gets the title described under The Visitor flow instead. The league's schedule service is undocumented. The client reads the shape seen live on 2026-10-10 (`GET /teams/{id}`: a `team` object with its division, `games` with `UGameID`, the division `standings`), still accepts soccer_scraper's older `game_id`, accepts numbers or strings where the league might change its mind, and treats any answer it cannot read as the league being unavailable, never as a Team being gone. Only a 404 or 410 whose body is the league's own `{"message":"team not found"}`, or a `team` with no name, means a wrong Team ID; any other 404, such as an unknown route's, is the league being unavailable. A Team the league places at another Facility than Boise reads as not found; one with no `FacilityID` is accepted. A Team ID belongs to one season; the same people get a new one when they sign up again.

## The Visitor flow

Boise only. A person finds a Team by Team ID, by name search, or by browsing the divisions at the Facility; combines several Teams; keeps the remaining games ticked (the default) or changes them; and gets either a file to download or a Session link that calendar apps fetch. Every game is a 45-minute event in Mountain time, titled with the person's Team first, then its opponent, then the field: `ROVERS vs KICKERS - Field 3`, so the title alone says where to go. The Team's side is read from the Team IDs the league puts on each game, or from its name when the league leaves those out; a game between two of the chosen Teams keeps the league's Home vs Away order. The field is the number the league gives (a field the league names instead is written as given); a game the league lists with no time, field or opponent yet is left out as unscheduled, so every title carries its field. The description repeats the field and lists the game as the league does, Home vs Away.

| Method and path | What it does |
|---|---|
| `GET /teams/{teamID}` | One Team and its games, each marked `remaining` or not, with each side's Team ID (`home_id`, `away_id`) when the league gives them |
| `GET /teams/{teamID}/next` | The Team that carries this one on into the next Session, or the Teams to choose from |
| `GET /schedule?team=…&team=…` | Several Teams combined (up to 10); a game between two of them appears once. Teams that can't be read are listed in `failed` |
| `GET /teams?q=…` | Name search at the Facility (a Team ID matches too) |
| `GET /divisions` | Divisions at the Facility |
| `GET /divisions/{divisionID}/teams` | Teams in one division |
| `POST /calendar` | The chosen games as an .ics download |
| `POST /links` | A Session link for the chosen games: `{token, url, teams, failed}` |
| `GET /links/{token}` (or `{token}.ics`) | What a calendar app fetches: the chosen games, read afresh from the league, and a notice once a Team's Session is over. `HEAD` works too |
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

The league lists every Team at Boise in the current Session and the next at `GET /teams/search_teams?name=%25&facility_id=4`, with no league account: the name is a prefix match and `%` is its wildcard, so the one request lists them all. Ended Sessions are not listed, so a search or a division finds the Teams of this Session and the next, and each Team and division carries its Session (`season`). The Directory keeps that list in memory for an hour and searches it there: a name matches on any part of it, ignoring case, a Team ID matches whole, and a search answers at most 50 Teams, by name and then Session. Divisions are listed by Session, then name. Teams that dropped out, and any the league places at another Facility, are left out. Nothing a person types is sent to the league, whose search treats `%`, `_` and `[ ]` as wildcards. If the wildcard ever stops working and the list comes back empty, the Directory reads it as 36 searches by first letter or digit instead. When a refresh fails, the last list still serves (with a `directory_stale` log line); with no list on hand yet, the lookup is `league_unavailable`. An empty list counts as a failed refresh, since the league always lists the current Session: the last list keeps serving, or with none on hand the empty one serves for five minutes with a `team_list_empty` log line. Craig chose this on 2026-10-13, replacing the scan of the coming week's day schedules chosen on 2026-10-10, which missed every Team without a game that week.

## The next Session

A Session link is good for one Session. Once a Team in it has no game left to play, and it has played its division's number of games or gone eight days without one, the link adds one all-day notice, "ROVERS: this Session is over", which links to the site's `/soccer` page with every Team ID of the link as `after` (`/soccer?after=123456&after=654321`). The notice goes on the day after the Team's last game, or, when only the eight quiet days say the Session is over, on the day they are up, so it does not first appear a week in the past. A Team whose link also holds a Team by the same name in a later Session gets no notice and is left out of `after`, since the calendar does get next Session's games. That address is `LINK_BASE_URL` without its `link/`; when `LINK_BASE_URL` does not end in `/soccer/link/`, no notice is added. A division's game length and number of games come from the league's `GET /division_seasons/{id}` and are kept for six hours; a link's divisions are looked up together, each once, within three seconds and ending at least two seconds before the request's own eight-second deadline, so a slow league cannot hold the calendar past the site's wait. When that lookup fails or runs out of time, the notice waits for a later fetch (a `session_over_check_failed` log line) and the calendar still answers. A download never gets the notice, and the link itself never follows a Team into its next Session.

`GET /teams/{teamID}/next` is what that page asks. It looks in the Team list for the Team's name, trimmed and ignoring case, in the earliest later Session that has it, leaving out Teams that dropped out and one-day events such as a Saturday tournament. `follow` is exactly one such Team, with the same coach; `ask` is one or more for the person to choose from, each marked `same_coach` or not; `none` is none yet, with the same coach's Teams under other names in `similar` when there are no more than three; a coach id with more is the league's own club Teams, so none of them is offered. The coach is compared, never named: no answer or log line carries a coach id. Measured on 2026-10-12, the same name and coach found a Team's next Session 86% of the time.

## Logs

One JSON line per request with the route pattern (never the path, so link tokens and searches stay out), status, timing and user agent. The route of each `POST` says which output a person chose; the user agent on `/links/{token}` says which calendar app fetched a link.

## Running it

Needs Go (see `go.mod`) and [Task](https://taskfile.dev).

```sh
task run          # http://127.0.0.1:8081 (SOCCER_ADDR to change)
task ci           # gofmt, vet, tests, and the OpenTofu checks below
task lambda       # dist/soccer.zip for provided.al2023 on arm64
task infra:check  # format, validate and test infra/ and infra/bootstrap offline (needs OpenTofu)
```

| Variable | Meaning |
|---|---|
| `LEAGUE_BASE_URL` | The league's schedule service; defaults to `https://lps-api-prod.lps-test.com` |
| `LEAGUE_FACILITY_ID` | The Facility in the league's `GET /facilities` list; defaults to `4`, Boise |
| `LINK_BASE_URL` | Public address Session link tokens are appended to, such as `https://craigdevjohnson.com/soccer/link/`; when unset, `POST /links` returns only the token |
| `SOCCER_ADDR` | Local listen address |

## Deploying it

`infra/` is the OpenTofu for one deployment: the function, its Function URL that only the site's role may call, its log group and two alarms. There are two deployments, dev and production, one per site environment, and their inputs are committed in `infra/environments/`. GitHub Actions deploys them: `CI` runs `task ci` on every pull request and never touches AWS, and `Deploy` builds `main`, applies dev, then applies production once Craig approves it in GitHub. The deploy roles it signs in with are `infra/bootstrap`, applied by hand once. On a computer, `task infra:plan` (`ENV=prod` for production) is for testing. [docs/operations/deploy.md](docs/operations/deploy.md) has the details and the one-time setup.

## Licence

MIT
