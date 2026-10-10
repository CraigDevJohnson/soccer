package league

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// DefaultBaseURL is the league's Team address, as soccer_scraper used it.
const DefaultBaseURL = "https://lps-api-prod.lps-test.com"

// Client reads Teams from the league's schedule service.
type Client struct {
	BaseURL string
	HTTP    *http.Client
}

// NewClient returns a Client with a 10-second timeout per request.
func NewClient(baseURL string) *Client {
	if baseURL == "" {
		baseURL = DefaultBaseURL
	}
	return &Client{
		BaseURL: strings.TrimRight(baseURL, "/"),
		HTTP:    &http.Client{Timeout: 10 * time.Second},
	}
}

// teamResponse is the league's answer for GET /teams/{id}, as seen live on
// 2026-10-10: a "team" object, its "games", the division "standings" and a
// "roster". The game ID is "UGameID"; soccer_scraper read "game_id", which
// the league no longer sends, so both are accepted. Numbers and strings are
// both accepted where the league might change its mind, and a missing
// "team" object is told apart from an empty one.
type teamResponse struct {
	Team *struct {
		UTeamID      flexString `json:"UTeamID"`
		TeamName     string     `json:"team_name"`
		DivisionName string     `json:"division_name"`
		DivCode      string     `json:"div_code"`
		UDivSeasonID flexString `json:"UDivSeasonID"`
		Season       flexString `json:"Season"`
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
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.BaseURL+"/teams/"+id, nil)
	if err != nil {
		return Team{}, err
	}
	req.Header.Set("Accept", "application/json")
	resp, err := c.HTTP.Do(req)
	if err != nil {
		// A caller that went away is not a league outage.
		if errors.Is(ctx.Err(), context.Canceled) {
			return Team{}, ctx.Err()
		}
		return Team{}, fmt.Errorf("%w: %v", ErrUnavailable, err)
	}
	defer resp.Body.Close()
	switch {
	// Only answers that say the Team is absent mean a wrong Team ID. A 400,
	// a rate limit, a refusal or a 5xx means the league itself is in trouble
	// and must not read as "this Team is gone", which would empty every
	// Session link.
	case resp.StatusCode == http.StatusNotFound, resp.StatusCode == http.StatusGone:
		return Team{}, ErrTeamNotFound
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
