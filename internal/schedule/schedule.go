// Package schedule combines Teams into one list of games and applies a
// person's choice of games to it.
package schedule

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"time"

	"golang.org/x/sync/errgroup"

	"github.com/CraigDevJohnson/soccer/internal/league"
)

// GameLength is how long every Boise game slot lasts.
const GameLength = 45 * time.Minute

// MaxTeams caps how many Teams one calendar combines.
const MaxTeams = 10

// ErrNoTeams means no Team could be read at all.
var ErrNoTeams = errors.New("no team could be read")

// InvalidError lists Team IDs that are not Team IDs.
type InvalidError struct{ Reasons []string }

func (e *InvalidError) Error() string { return fmt.Sprintf("invalid team IDs: %v", e.Reasons) }

// Team is a Team that was read, without its games.
type Team struct {
	ID       string          `json:"id"`
	Name     string          `json:"name"`
	Division league.Division `json:"division"`
	Season   string          `json:"season,omitempty"`
}

// Failure is a Team ID that could not be read: "not_found" or "unavailable".
type Failure struct {
	TeamID string `json:"team_id"`
	Reason string `json:"reason"`
}

// Unavailable reports whether the league failed for any Team, as opposed to
// Team IDs it simply does not know.
func (s Schedule) Unavailable() bool {
	for _, f := range s.Failed {
		if f.Reason == "unavailable" {
			return true
		}
	}
	return false
}

// Game is a game in a combined schedule. TeamIDs names the chosen Teams that
// play in it, so a game between two of them appears once.
type Game struct {
	ID        string    `json:"id"`
	Start     time.Time `json:"start"`
	End       time.Time `json:"end"`
	Field     string    `json:"field"`
	Home      string    `json:"home"`
	Away      string    `json:"away"`
	TeamIDs   []string  `json:"team_ids"`
	Remaining bool      `json:"remaining"`
}

// Schedule is several Teams' games combined, in start order.
type Schedule struct {
	Teams  []Team    `json:"teams"`
	Games  []Game    `json:"games"`
	Failed []Failure `json:"failed"`
	// AsOf is the moment Remaining was judged from. A screen sends it back
	// as "since" so its ticks mean the same thing however long it was open.
	AsOf time.Time `json:"as_of"`
}

// TeamIDs returns the Team IDs that were read.
func (s Schedule) TeamIDs() []string {
	ids := make([]string, len(s.Teams))
	for i, t := range s.Teams {
		ids[i] = t.ID
	}
	return ids
}

// NormalizeTeamIDs validates and de-duplicates Team IDs, keeping their order.
func NormalizeTeamIDs(raw []string) ([]string, error) {
	var ids, bad []string
	for _, r := range raw {
		id, err := league.ValidTeamID(r)
		if err != nil {
			bad = append(bad, err.Error())
			continue
		}
		if !slices.Contains(ids, id) {
			ids = append(ids, id)
		}
	}
	if len(bad) > 0 {
		return nil, &InvalidError{Reasons: bad}
	}
	if len(ids) == 0 {
		return nil, &InvalidError{Reasons: []string{"at least one team ID is needed"}}
	}
	if len(ids) > MaxTeams {
		return nil, &InvalidError{Reasons: []string{fmt.Sprintf("at most %d teams can be combined", MaxTeams)}}
	}
	return ids, nil
}

// Combine reads each Team and merges their games. A Team that cannot be read
// is reported in Failed while the rest still combine. When none can be read,
// it returns league.ErrUnavailable if the league failed for any of them, or
// ErrNoTeams if every Team ID was simply unknown. Games starting at or after
// now are Remaining.
func Combine(ctx context.Context, src league.Teams, teamIDs []string, now time.Time) (Schedule, error) {
	ids, err := NormalizeTeamIDs(teamIDs)
	if err != nil {
		return Schedule{}, err
	}
	// The same second a screen gets back as as_of, so Remaining and a later
	// since never disagree about a game starting right now.
	now = now.Truncate(time.Second)

	teams := make([]league.Team, len(ids))
	errs := make([]error, len(ids))
	g, gctx := errgroup.WithContext(ctx)
	g.SetLimit(4)
	for i, id := range ids {
		g.Go(func() (err error) {
			// A panic here would not reach the request's recoverer and
			// would take the whole process down.
			defer func() {
				if v := recover(); v != nil {
					errs[i] = fmt.Errorf("%w: reading team %s: panic: %v", league.ErrUnavailable, id, v)
				}
			}()
			teams[i], errs[i] = src.Team(gctx, id)
			return nil
		})
	}
	_ = g.Wait()
	if err := ctx.Err(); errors.Is(err, context.Canceled) {
		return Schedule{}, err
	}

	s := Schedule{Teams: []Team{}, Games: []Game{}, Failed: []Failure{}, AsOf: now}
	byKey := map[string]int{}
	unavailable := false
	for i, t := range teams {
		if errs[i] != nil {
			reason := "unavailable"
			if errors.Is(errs[i], league.ErrTeamNotFound) {
				reason = "not_found"
			} else {
				unavailable = true
			}
			s.Failed = append(s.Failed, Failure{TeamID: ids[i], Reason: reason})
			continue
		}
		s.Teams = append(s.Teams, Team{ID: t.ID, Name: t.Name, Division: t.Division, Season: t.Season})
		for _, lg := range t.Games {
			key := gameKey(lg)
			if j, ok := byKey[key]; ok {
				s.Games[j].TeamIDs = append(s.Games[j].TeamIDs, t.ID)
				continue
			}
			byKey[key] = len(s.Games)
			s.Games = append(s.Games, Game{
				ID:        key,
				Start:     lg.Start,
				End:       lg.Start.Add(GameLength),
				Field:     lg.Field,
				Home:      lg.Home,
				Away:      lg.Away,
				TeamIDs:   []string{t.ID},
				Remaining: !lg.Start.Before(now),
			})
		}
	}
	if len(s.Teams) == 0 {
		if unavailable {
			return s, league.ErrUnavailable
		}
		return s, ErrNoTeams
	}
	slices.SortStableFunc(s.Games, func(a, b Game) int {
		if c := a.Start.Compare(b.Start); c != 0 {
			return c
		}
		if a.Field < b.Field {
			return -1
		}
		if a.Field > b.Field {
			return 1
		}
		return 0
	})
	return s, nil
}

// gameKey identifies a game across Teams: the league's game ID, or, when the
// league leaves it out, the slot it is played in. soccer_scraper always had a
// game ID; the slot is a last resort, and a game without one that moves is
// treated as a new game.
func gameKey(g league.Game) string {
	if g.ID != "" {
		return g.ID
	}
	return fmt.Sprintf("%s-%s", g.Start.Format("20060102T1504"), g.Field)
}

// Selection is a person's choice of games across some Teams. Games starting
// at or after Since are in unless Excluded; earlier ones are out unless
// Included. Games the league adds later therefore join a Session link on
// their own, which matters because the league posts most of a Session's games
// only after its first one.
type Selection struct {
	TeamIDs  []string  `json:"team_ids"`
	Excluded []string  `json:"excluded,omitempty"`
	Included []string  `json:"included,omitempty"`
	Since    time.Time `json:"since"`
}

// Chosen returns the games the selection keeps, in schedule order.
func (sel Selection) Chosen(games []Game) []Game {
	out := []Game{}
	for _, g := range games {
		in := !g.Start.Before(sel.Since)
		if in && slices.Contains(sel.Excluded, g.ID) {
			in = false
		}
		if !in && slices.Contains(sel.Included, g.ID) {
			in = true
		}
		if in {
			out = append(out, g)
		}
	}
	return out
}
