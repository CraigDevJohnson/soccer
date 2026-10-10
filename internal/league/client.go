package league

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
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

// teamResponse is the league's answer for GET /teams/{id}.
type teamResponse struct {
	Team struct {
		Season   int    `json:"Season"`
		TeamName string `json:"team_name"`
	} `json:"team"`
	Games []struct {
		GameID            string `json:"game_id"`
		SchedGameDateTime string `json:"SchedGameDateTime"`
		FieldName         string `json:"field_name"`
		Field             int    `json:"Field"`
		HomeTeam          struct {
			TeamName string `json:"team_name"`
		} `json:"home_team"`
		VisitorTeam struct {
			TeamName string `json:"team_name"`
		} `json:"visitor_team"`
	} `json:"games"`
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
	// Only answers that say the Team is absent mean a wrong Team ID; rate
	// limits, refusals and 5xx mean the league itself is in trouble.
	case resp.StatusCode == http.StatusBadRequest, resp.StatusCode == http.StatusNotFound, resp.StatusCode == http.StatusGone:
		return Team{}, ErrTeamNotFound
	case resp.StatusCode != http.StatusOK:
		return Team{}, fmt.Errorf("%w: status %d", ErrUnavailable, resp.StatusCode)
	}

	var body teamResponse
	if err := json.NewDecoder(io.LimitReader(resp.Body, 2<<20)).Decode(&body); err != nil {
		return Team{}, fmt.Errorf("%w: decoding team %s: %v", ErrUnavailable, id, err)
	}
	if body.Team.TeamName == "" {
		return Team{}, ErrTeamNotFound
	}

	team := Team{ID: id, Name: body.Team.TeamName, Games: []Game{}}
	if body.Team.Season != 0 {
		team.Season = strconv.Itoa(body.Team.Season)
	}
	for _, g := range body.Games {
		if g.SchedGameDateTime == "" || g.HomeTeam.TeamName == "" || g.VisitorTeam.TeamName == "" {
			continue
		}
		// Prefer the field's name, falling back to its number.
		field := strings.TrimPrefix(g.FieldName, "Field ")
		if field == "" && g.Field != 0 {
			field = strconv.Itoa(g.Field)
		}
		if field == "" {
			continue
		}
		start, err := ParseLeagueTime(g.SchedGameDateTime)
		if err != nil {
			continue
		}
		team.Games = append(team.Games, Game{
			ID:    g.GameID,
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
