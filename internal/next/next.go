// Package next finds the Team that carries a Team on into the league's next
// Session. Team IDs are new every Session, so a Session link stops getting
// games when its Session ends; this is how the site offers the next one. It
// suggests and never decides: a match that is not clear is the person's
// choice.
package next

import (
	"cmp"
	"context"
	"errors"
	"fmt"
	"maps"
	"slices"
	"strconv"
	"strings"

	"golang.org/x/sync/errgroup"

	"github.com/CraigDevJohnson/soccer/internal/league"
)

// How a Team's next Session was found.
const (
	// Follow: exactly one Team in the next Session has the same name and
	// the same coach.
	Follow = "follow"
	// Ask: Teams in the next Session have the same name, but not one clear
	// match, so the person chooses.
	Ask = "ask"
	// None: no Team in a later Session has the same name yet.
	None = "none"
)

// MaxSimilar is the most Teams offered under a new name. A coach id with
// more Teams than that in one Session is not one person's: the league files
// its own club Teams, from adults to under-8s, under one coach id. None of
// those is offered.
const MaxSimilar = 3

// Lists hands over the league's whole Team list, coach ids included, as the
// SearchDirectory keeps it.
type Lists interface {
	Listed(ctx context.Context) ([]league.Listed, error)
}

// Candidate is a Team in the next Session with the old Team's name.
// SameCoach says whether its coach is the old Team's; the coach is never
// named.
type Candidate struct {
	league.TeamSummary
	SameCoach bool `json:"same_coach"`
}

// Result is what GET /teams/{teamID}/next answers. Teams holds the
// same-name Teams of the earliest later Session; Similar, only when there
// are none, holds that Session's Teams with the same coach under another
// name, when there are no more than MaxSimilar. Neither is ever null, and
// no coach id is in it.
type Result struct {
	Team    league.TeamSummary   `json:"team"`
	Match   string               `json:"match"`
	Teams   []Candidate          `json:"teams"`
	Similar []league.TeamSummary `json:"similar"`
}

// Resolver finds a Team's next Session from the league's Team list, which
// holds the current Session and the next.
type Resolver struct {
	Teams     league.Teams
	Lists     Lists
	Divisions league.DivisionSeasons
}

// Find reads the old Team and looks for it in a later Session: the same
// name, trimmed and ignoring case, in the earliest later Session that has
// one, leaving out Teams that dropped out and one-day events. Measured on
// 2026-10-12, the same name and coach found the Team's next Session 86% of
// the time. An unknown Team ID is league.ErrTeamNotFound; any lookup the
// league fails is league.ErrUnavailable for the whole answer.
func (r *Resolver) Find(ctx context.Context, teamID string) (Result, error) {
	old, err := r.Teams.Team(ctx, teamID)
	if err != nil {
		return Result{}, err
	}
	// The division carries its Session, as the Directory's answers do.
	div := old.Division
	div.Season = old.Season
	res := Result{
		Team:    league.TeamSummary{ID: old.ID, Name: strings.TrimSpace(old.Name), Division: div, Season: old.Season},
		Match:   None,
		Teams:   []Candidate{},
		Similar: []league.TeamSummary{},
	}
	after, err := strconv.Atoi(old.Season)
	if err != nil {
		// Without a Season number there is no "later".
		return res, nil
	}

	listed, err := r.Lists.Listed(ctx)
	if err != nil {
		return Result{}, err
	}
	name := sameName(old.Name)
	coach := usableCoach(old.CoachID)
	var named, coached []league.Listed
	for _, l := range listed {
		if l.Dropped {
			continue
		}
		if s, err := strconv.Atoi(l.Season); err != nil || s <= after {
			continue
		}
		switch {
		case sameName(l.Name) == name:
			named = append(named, l)
		case coach != "" && usableCoach(l.CoachID) == coach:
			coached = append(coached, l)
		}
	}

	found, err := r.earliest(ctx, named)
	if err != nil {
		return Result{}, err
	}
	same := 0
	for _, l := range found {
		c := Candidate{TeamSummary: l.TeamSummary, SameCoach: coach != "" && usableCoach(l.CoachID) == coach}
		if c.SameCoach {
			same++
		}
		res.Teams = append(res.Teams, c)
	}
	// Same coach first, then by division, so the answer is the same each time.
	slices.SortFunc(res.Teams, func(a, b Candidate) int {
		if a.SameCoach != b.SameCoach {
			if a.SameCoach {
				return -1
			}
			return 1
		}
		return cmp.Or(strings.Compare(a.Division.Name, b.Division.Name), strings.Compare(a.ID, b.ID))
	})
	switch {
	case len(res.Teams) == 1 && same == 1:
		res.Match = Follow
		return res, nil
	case len(res.Teams) > 0:
		res.Match = Ask
		return res, nil
	}

	similar, err := r.earliest(ctx, coached)
	if err != nil {
		return Result{}, err
	}
	for _, l := range similar {
		res.Similar = append(res.Similar, l.TeamSummary)
	}
	if len(res.Similar) > MaxSimilar {
		res.Similar = []league.TeamSummary{}
		return res, nil
	}
	slices.SortFunc(res.Similar, func(a, b league.TeamSummary) int {
		return cmp.Or(strings.Compare(a.Name, b.Name), strings.Compare(a.ID, b.ID))
	})
	return res, nil
}

// earliest keeps the Teams of the earliest Season among them that has any
// Team outside a one-day event, leaving the one-day ones out. Seasons are
// tried in order, so a later Session's divisions are only looked up when
// the earlier ones had nothing. A Team without a division ID is kept: that
// is not proof of a one-day event.
func (r *Resolver) earliest(ctx context.Context, teams []league.Listed) ([]league.Listed, error) {
	bySeason := map[int][]league.Listed{}
	for _, l := range teams {
		s, _ := strconv.Atoi(l.Season)
		bySeason[s] = append(bySeason[s], l)
	}
	for _, s := range slices.Sorted(maps.Keys(bySeason)) {
		oneDay, err := r.oneDay(ctx, bySeason[s])
		if err != nil {
			return nil, err
		}
		var kept []league.Listed
		for _, l := range bySeason[s] {
			if !oneDay[l.Division.ID] {
				kept = append(kept, l)
			}
		}
		if len(kept) > 0 {
			return kept, nil
		}
	}
	return nil, nil
}

// oneDay looks up each division the Teams play in, at most four at a time,
// and reports which are one-day events.
func (r *Resolver) oneDay(ctx context.Context, teams []league.Listed) (map[string]bool, error) {
	var ids []string
	for _, l := range teams {
		if id := l.Division.ID; id != "" && !slices.Contains(ids, id) {
			ids = append(ids, id)
		}
	}
	found := make([]league.DivisionSeason, len(ids))
	g, gctx := errgroup.WithContext(ctx)
	g.SetLimit(4)
	for i, id := range ids {
		g.Go(func() error {
			ds, err := r.Divisions.DivisionSeason(gctx, id)
			switch {
			case err == nil:
				found[i] = ds
				return nil
			case errors.Is(err, context.Canceled), errors.Is(err, league.ErrUnavailable):
				return err
			}
			// A division the league lists but cannot find is the league
			// contradicting itself, not an answer.
			return fmt.Errorf("%w: division season %s: %v", league.ErrUnavailable, id, err)
		})
	}
	if err := g.Wait(); err != nil {
		return nil, err
	}
	oneDay := map[string]bool{}
	for i, id := range ids {
		oneDay[id] = found[i].OneDay
	}
	return oneDay, nil
}

// sameName is a Team name as names are compared: trimmed, ignoring case.
func sameName(s string) string {
	return strings.ToUpper(strings.TrimSpace(s))
}

// usableCoach returns a coach id, or "" when there is none to compare.
func usableCoach(id string) string {
	id = strings.TrimSpace(id)
	if id == "0" {
		return ""
	}
	return id
}
