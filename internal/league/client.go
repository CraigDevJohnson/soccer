package league

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"golang.org/x/sync/errgroup"
)

const (
	// DefaultBaseURL is the league's schedule service, as soccer_scraper used it.
	DefaultBaseURL = "https://lps-api-prod.lps-test.com"
	// DefaultFacilityID is Boise in the league's GET /facilities list.
	DefaultFacilityID = "4"
)

// Client reads Teams and the Team list from the league's schedule service.
type Client struct {
	BaseURL    string
	FacilityID string
	HTTP       *http.Client
}

// NewClient returns a Client for Boise with a 10-second timeout per request.
func NewClient(baseURL string) *Client {
	if baseURL == "" {
		baseURL = DefaultBaseURL
	}
	return &Client{
		BaseURL:    strings.TrimRight(baseURL, "/"),
		FacilityID: DefaultFacilityID,
		HTTP:       &http.Client{Timeout: 10 * time.Second},
	}
}

// teamResponse is the league's answer for GET /teams/{id}, as seen live on
// 2026-10-10: a "team" object, its "games", the division "standings" and a
// "roster". The game ID is "UGameID"; soccer_scraper read "game_id", which
// the league no longer sends, so both are accepted. Numbers and strings are
// both accepted where the league might change its mind, and a missing
// "team" object is told apart from an empty one. FacilityID is where the
// Team plays.
type teamResponse struct {
	Team *struct {
		UTeamID      flexString `json:"UTeamID"`
		TeamName     string     `json:"team_name"`
		DivisionName string     `json:"division_name"`
		DivCode      string     `json:"div_code"`
		UDivSeasonID flexString `json:"UDivSeasonID"`
		Season       flexString `json:"Season"`
		FacilityID   flexString `json:"FacilityID"`
	} `json:"team"`
	Games []struct {
		UGameID           flexString `json:"UGameID"`
		GameID            flexString `json:"game_id"`
		SchedGameDateTime string     `json:"SchedGameDateTime"`
		FieldName         string     `json:"field_name"`
		Field             flexString `json:"Field"`
		HomeTeam          struct {
			TeamName string `json:"team_name"`
		} `json:"home_team"`
		VisitorTeam struct {
			TeamName string `json:"team_name"`
		} `json:"visitor_team"`
	} `json:"games"`
}

// flexString reads a JSON string, number or null as a string.
type flexString string

func (f *flexString) UnmarshalJSON(b []byte) error {
	if string(b) == "null" {
		*f = ""
		return nil
	}
	if len(b) > 0 && b[0] == '"' {
		var s string
		if err := json.Unmarshal(b, &s); err != nil {
			return err
		}
		*f = flexString(s)
		return nil
	}
	var n json.Number
	if err := json.Unmarshal(b, &n); err != nil {
		return err
	}
	*f = flexString(n.String())
	return nil
}

// Team fetches one Team and all of its games, played and remaining.
func (c *Client) Team(ctx context.Context, teamID string) (Team, error) {
	id, err := ValidTeamID(teamID)
	if err != nil {
		return Team{}, err
	}
	resp, err := c.get(ctx, "/teams/"+id)
	if err != nil {
		return Team{}, err
	}
	defer resp.Body.Close()
	switch {
	// Only answers that say the Team is absent mean a wrong Team ID. A 404
	// for a route the league no longer has, a 400, a rate limit, a refusal
	// or a 5xx means the league itself is in trouble and must not read as
	// "this Team is gone", which would empty every Session link.
	case resp.StatusCode == http.StatusNotFound, resp.StatusCode == http.StatusGone:
		if saysTeamNotFound(resp.Body) {
			return Team{}, ErrTeamNotFound
		}
		return Team{}, fmt.Errorf("%w: team %s: status %d without a team not found message", ErrUnavailable, id, resp.StatusCode)
	case resp.StatusCode != http.StatusOK:
		return Team{}, fmt.Errorf("%w: status %d", ErrUnavailable, resp.StatusCode)
	}

	var body teamResponse
	if err := json.NewDecoder(io.LimitReader(resp.Body, 2<<20)).Decode(&body); err != nil {
		return Team{}, fmt.Errorf("%w: decoding team %s: %v", ErrUnavailable, id, err)
	}
	if body.Team == nil {
		// Not the shape soccer_scraper knew: a maintenance page or a changed
		// API, not an absent Team.
		return Team{}, fmt.Errorf("%w: team %s: no team object in the answer", ErrUnavailable, id)
	}
	if body.Team.TeamName == "" {
		return Team{}, ErrTeamNotFound
	}
	// A Team at another Facility is not one of ours, though its Team ID is
	// real. A missing FacilityID is not proof of another Facility.
	if f := strings.TrimSpace(string(body.Team.FacilityID)); f != "" && f != c.FacilityID {
		return Team{}, fmt.Errorf("%w: team %s plays at facility %s, not %s", ErrTeamNotFound, id, f, c.FacilityID)
	}

	team := Team{
		ID:       id,
		Name:     body.Team.TeamName,
		Division: Division{ID: string(body.Team.UDivSeasonID), Name: body.Team.DivisionName, Code: body.Team.DivCode},
		Games:    []Game{},
	}
	if s := string(body.Team.Season); s != "" && s != "0" {
		team.Season = s
	}
	for _, g := range body.Games {
		// A row with no time, no field or no opponent yet is a game the
		// league has not scheduled; it is left out, as soccer_scraper left it
		// out. A row that has a time this code cannot read is different: the
		// league changed shape, and dropping that game would read as "game
		// cancelled" to every subscribed calendar. That is an outage.
		if g.SchedGameDateTime == "" || g.HomeTeam.TeamName == "" || g.VisitorTeam.TeamName == "" {
			continue
		}
		// Prefer the field's name, falling back to its number.
		field := strings.TrimPrefix(g.FieldName, "Field ")
		if field == "" && g.Field != "0" {
			field = string(g.Field)
		}
		if field == "" {
			continue
		}
		start, err := ParseLeagueTime(g.SchedGameDateTime)
		if err != nil {
			return Team{}, fmt.Errorf("%w: team %s game %q: %v", ErrUnavailable, id, string(g.GameID), err)
		}
		gameID := string(g.UGameID)
		if gameID == "" {
			gameID = string(g.GameID)
		}
		team.Games = append(team.Games, Game{
			ID:    gameID,
			Start: start,
			Field: field,
			Home:  g.HomeTeam.TeamName,
			Away:  g.VisitorTeam.TeamName,
		})
	}
	return team, nil
}

// saysTeamNotFound reports whether a 404 or 410 body is the league's answer
// for an unknown Team ID, {"message":"team not found"}, as seen live on
// 2026-10-12. An unknown route answers 404 too, with {"status":404,
// "error":"Not Found"}, and other lookups have their own messages; none of
// those says this Team is gone.
func saysTeamNotFound(body io.Reader) bool {
	var b struct {
		Message string `json:"message"`
	}
	if err := json.NewDecoder(io.LimitReader(body, 4<<10)).Decode(&b); err != nil {
		return false
	}
	return strings.EqualFold(strings.TrimSpace(b.Message), "team not found")
}

// get makes one league request. A caller that went away is not a league
// outage; anything else that stops the request is.
func (c *Client) get(ctx context.Context, path string) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.BaseURL+path, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")
	resp, err := c.HTTP.Do(req)
	if err != nil {
		if errors.Is(ctx.Err(), context.Canceled) {
			return nil, ctx.Err()
		}
		return nil, fmt.Errorf("%w: %v", ErrUnavailable, err)
	}
	return resp, nil
}

// listedTeam is one row of the league's Team list, GET
// /teams/search_teams, as seen live on 2026-10-13. The row carries more
// (facility_name, Color, UAltID and fields for a signed-in player), which is
// not read.
type listedTeam struct {
	UTeamID      flexString `json:"UTeamID"`
	TeamName     string     `json:"team_name"`
	DivisionName string     `json:"division_name"`
	DivCode      string     `json:"div_code"`
	UDivSeasonID flexString `json:"UDivSeasonID"`
	Season       flexString `json:"Season"`
	UCoachID     flexString `json:"UCoachID"`
	FacilityID   flexString `json:"FacilityID"`
	Dropped      bool       `json:"Dropped"`
}

// sweepPrefixes are the searches TeamList falls back to when the wildcard
// answers nothing, one per first letter or digit of a Team name.
const sweepPrefixes = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

// TeamList lists every Team at the Facility in the current Session and the
// next, from the league's team search. That search's name is a
// case-insensitive prefix match in which "%" is a wildcard, so name=% lists
// them all without a league account. Ended Sessions are not listed. Teams
// that dropped out and Teams at another Facility are left out, and names
// are trimmed.
//
// Only the fixed searches here are ever sent: "%", "_" and "[ ]" are
// wildcards to the league, so text a person typed must never reach it.
func (c *Client) TeamList(ctx context.Context) ([]Listed, error) {
	rows, err := c.searchTeams(ctx, "%")
	if err != nil {
		return nil, err
	}
	if len(rows) == 0 {
		// The wildcard stopped working, or the Facility has no Teams. One
		// search per first letter or digit tells the two apart; a name
		// starting with anything else is missed until the wildcard is back.
		if rows, err = c.sweep(ctx); err != nil {
			return nil, err
		}
	}
	teams := []Listed{}
	for _, r := range rows {
		if r.Dropped {
			continue
		}
		if f := strings.TrimSpace(string(r.FacilityID)); f != "" && f != c.FacilityID {
			continue
		}
		id, name := strings.TrimSpace(string(r.UTeamID)), strings.TrimSpace(r.TeamName)
		if id == "" || name == "" {
			continue
		}
		season := strings.TrimSpace(string(r.Season))
		if season == "0" {
			season = ""
		}
		teams = append(teams, Listed{
			TeamSummary: TeamSummary{
				ID:   id,
				Name: name,
				Division: Division{
					ID:     strings.TrimSpace(string(r.UDivSeasonID)),
					Name:   strings.TrimSpace(r.DivisionName),
					Code:   strings.TrimSpace(r.DivCode),
					Season: season,
				},
				Season: season,
			},
			CoachID: strings.TrimSpace(string(r.UCoachID)),
		})
	}
	return teams, nil
}

// sweep runs one search per sweepPrefixes letter or digit and keeps each
// Team once. Any search failing fails the sweep: a list missing a letter
// would hide those Teams for an hour.
func (c *Client) sweep(ctx context.Context) ([]listedTeam, error) {
	found := make([][]listedTeam, len(sweepPrefixes))
	g, gctx := errgroup.WithContext(ctx)
	g.SetLimit(4)
	for i, p := range sweepPrefixes {
		g.Go(func() error {
			rows, err := c.searchTeams(gctx, string(p))
			found[i] = rows
			return err
		})
	}
	if err := g.Wait(); err != nil {
		return nil, err
	}
	seen := map[string]bool{}
	var rows []listedTeam
	for _, rs := range found {
		for _, r := range rs {
			id := strings.TrimSpace(string(r.UTeamID))
			if seen[id] {
				continue
			}
			seen[id] = true
			rows = append(rows, r)
		}
	}
	return rows, nil
}

// searchTeams makes one Team list search for names starting with prefix.
// An answer this code cannot read is ErrUnavailable; an empty array is not.
func (c *Client) searchTeams(ctx context.Context, prefix string) ([]listedTeam, error) {
	resp, err := c.get(ctx, "/teams/search_teams?name="+url.QueryEscape(prefix)+"&facility_id="+url.QueryEscape(c.FacilityID))
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("%w: team list %q: status %d", ErrUnavailable, prefix, resp.StatusCode)
	}
	var rows []listedTeam
	if err := json.NewDecoder(io.LimitReader(resp.Body, 8<<20)).Decode(&rows); err != nil {
		return nil, fmt.Errorf("%w: decoding team list %q: %v", ErrUnavailable, prefix, err)
	}
	return rows, nil
}

// ParseLeagueTime reads a league timestamp as Mountain wall-clock time. The
// league labels Boise's local times as UTC ("...Z" or "+00:00"), so any zone
// suffix is dropped rather than applied.
func ParseLeagueTime(s string) (time.Time, error) {
	s = strings.TrimSuffix(s, "Z")
	if i := strings.LastIndexAny(s, "+-"); i > 10 {
		s = s[:i]
	}
	if i := strings.IndexByte(s, '.'); i > 0 {
		s = s[:i]
	}
	t, err := time.Parse("2006-01-02T15:04:05", s)
	if err != nil {
		return time.Time{}, fmt.Errorf("unable to parse league time %q", s)
	}
	return time.Date(t.Year(), t.Month(), t.Day(), t.Hour(), t.Minute(), t.Second(), 0, Mountain), nil
}
