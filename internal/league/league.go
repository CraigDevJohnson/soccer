// Package league talks to Let's Play Soccer's schedule service, which is
// undocumented. It knows Boise only: every Team plays at the one Facility.
package league

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"

	// Embedded so America/Denver loads on Lambda, which has no zoneinfo.
	_ "time/tzdata"
)

var (
	// ErrTeamNotFound means the league has no Team with that Team ID.
	ErrTeamNotFound = errors.New("team not found")
	// ErrUnavailable means the league's service did not answer usefully.
	// Visitors get a plain message and a link to the league; nothing retries.
	ErrUnavailable = errors.New("league schedule service unavailable")
)

// Mountain is the Facility's time zone. The league sends local Boise times
// labelled UTC, so every game time is read as wall-clock time here.
var Mountain = mustLoad("America/Denver")

func mustLoad(name string) *time.Location {
	loc, err := time.LoadLocation(name)
	if err != nil {
		panic(err)
	}
	return loc
}

// Team is one Team for one Session, with every game the league lists for it.
// A Team ID belongs to one season: the same people get a new Team ID when
// they sign up again. Season is the league's own season number.
type Team struct {
	ID       string   `json:"id"`
	Name     string   `json:"name"`
	Division Division `json:"division"`
	Season   string   `json:"season,omitempty"`
	Games    []Game   `json:"games"`
}

// Game is one league game. Start is in Mountain time.
type Game struct {
	ID    string    `json:"id"`
	Start time.Time `json:"start"`
	Field string    `json:"field"`
	Home  string    `json:"home"`
	Away  string    `json:"away"`
}

// TeamSummary is a Team as a search or a division lists it, without games.
// Season is the league's season number, as on Team.
type TeamSummary struct {
	ID       string   `json:"id"`
	Name     string   `json:"name"`
	Division Division `json:"division"`
	Season   string   `json:"season,omitempty"`
}

// Listed is a Team as the league's Team list has it. CoachID is the league's
// id for the Team's coach, kept only to compare one Session's Team with the
// next; it is never served or logged. Dropped means the Team left the
// Session.
type Listed struct {
	TeamSummary
	CoachID string `json:"-"`
	Dropped bool   `json:"dropped,omitempty"`
}

// Division is what Let's Play Soccer calls a league at the Facility, for
// one season: "Coed Over 30 B Sun". ID is the league's UDivSeasonID, Code
// its short form ("CO30BSun") and Season the league's season number.
type Division struct {
	ID     string `json:"id"`
	Name   string `json:"name"`
	Code   string `json:"code,omitempty"`
	Season string `json:"season,omitempty"`
}

// Teams looks a Team up by its Team ID.
type Teams interface {
	Team(ctx context.Context, teamID string) (Team, error)
}

// Directory finds Teams without a Team ID: by name, or by division. An
// unknown division lists no Teams rather than failing.
type Directory interface {
	SearchTeams(ctx context.Context, query string) ([]TeamSummary, error)
	Divisions(ctx context.Context) ([]Division, error)
	DivisionTeams(ctx context.Context, divisionID string) ([]TeamSummary, error)
}

// TeamLists reads every Team the league lists at the Facility.
type TeamLists interface {
	TeamList(ctx context.Context) ([]Listed, error)
}

var teamIDPattern = regexp.MustCompile(`^[0-9]{6}$`)

// ValidTeamID reports whether s, trimmed, is a six-digit Team ID, and returns
// it trimmed. Leading zeros are allowed, as soccer_scraper allowed them;
// all zeros is not.
func ValidTeamID(s string) (string, error) {
	id := strings.TrimSpace(s)
	if id == "" {
		return "", errors.New("team ID cannot be empty")
	}
	if !teamIDPattern.MatchString(id) {
		return "", fmt.Errorf("team ID %q must be exactly 6 digits", id)
	}
	if id == "000000" {
		return "", fmt.Errorf("team ID %q is not a team ID", id)
	}
	return id, nil
}
