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
	// ErrNotConnected means a lookup this package cannot make yet because
	// the league address behind it is not known.
	ErrNotConnected = errors.New("not connected to the league yet")
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
type Team struct {
	ID     string `json:"id"`
	Name   string `json:"name"`
	Season string `json:"season,omitempty"`
	Games  []Game `json:"games"`
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
type TeamSummary struct {
	ID       string `json:"id"`
	Name     string `json:"name"`
	Division string `json:"division,omitempty"`
}

// Division is what Let's Play Soccer calls a league at the Facility.
type Division struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}

// Teams looks a Team up by its Team ID.
type Teams interface {
	Team(ctx context.Context, teamID string) (Team, error)
}

// Directory finds Teams without a Team ID: by name, or by division.
type Directory interface {
	SearchTeams(ctx context.Context, query string) ([]TeamSummary, error)
	Divisions(ctx context.Context) ([]Division, error)
	DivisionTeams(ctx context.Context, divisionID string) ([]TeamSummary, error)
}

// Unconnected is the Directory until the league's search and division
// addresses are known; every call answers ErrNotConnected.
type Unconnected struct{}

func (Unconnected) SearchTeams(context.Context, string) ([]TeamSummary, error) {
	return nil, ErrNotConnected
}

func (Unconnected) Divisions(context.Context) ([]Division, error) {
	return nil, ErrNotConnected
}

func (Unconnected) DivisionTeams(context.Context, string) ([]TeamSummary, error) {
	return nil, ErrNotConnected
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
