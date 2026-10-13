package api

import (
	"cmp"
	"context"
	"fmt"
	"net/url"
	"slices"
	"strconv"
	"strings"
	"time"

	"golang.org/x/sync/errgroup"

	"github.com/CraigDevJohnson/soccer/internal/calendar"
	"github.com/CraigDevJohnson/soccer/internal/league"
	"github.com/CraigDevJohnson/soccer/internal/schedule"
)

// quietAfter is how long after a Team's last game, with nothing new posted,
// its Session counts as over even though the league lists fewer games than
// the division plays. Divisions play weekly, so a week and a day without a
// game is the end.
const quietAfter = 8 * 24 * time.Hour

// noticeBudget bounds the division lookups for the notices. The site gives
// up on the backend after 7 seconds, so a slow league leaves notices out
// this time rather than making the whole calendar late.
const noticeBudget = 3 * time.Second

// sessionNotices returns one notice for each Team in a Session link whose
// Session is over, in link order. A Session link is good for one Session:
// the notice says so and links to the site's Let's Plan Soccer page with the
// link's Team IDs as "after", where the person can add next Session's Teams
// and get a new link. The link itself never follows a Team into its next
// Session.
//
// A Team whose link also holds a Team of a later Session gets no notice and
// is not passed on as "after": the calendar does get next Session's games,
// from that Team, and the person chose it already.
//
// A division is looked up only once every game a Team has is past, each
// division once, at most four at a time and within noticeBudget. When the
// lookup fails the notice is left out this time and a later fetch adds it;
// the calendar still answers, since its games are all there.
func (h *Handler) sessionNotices(ctx context.Context, s schedule.Schedule, linkTeamIDs []string) []calendar.Notice {
	page := h.soccerPage()
	if page == "" || h.Divisions == nil {
		return nil
	}
	now := h.now()
	var after []string
	for _, id := range linkTeamIDs {
		i := slices.IndexFunc(s.Teams, func(t schedule.Team) bool { return t.ID == id })
		if i < 0 || !laterInLink(s.Teams[i], s.Teams) {
			after = append(after, id)
		}
	}
	nextURL := page + "?" + url.Values{"after": after}.Encode()

	var ended []schedule.Team
	games := map[string][]schedule.Game{}
	var divisionIDs []string
	for _, t := range s.Teams {
		if laterInLink(t, s.Teams) {
			continue
		}
		for _, g := range s.Games {
			if slices.Contains(g.TeamIDs, t.ID) {
				games[t.ID] = append(games[t.ID], g)
			}
		}
		if !allPast(games[t.ID], now) {
			continue
		}
		ended = append(ended, t)
		if !slices.Contains(divisionIDs, t.Division.ID) {
			divisionIDs = append(divisionIDs, t.Division.ID)
		}
	}
	divisions := h.lookUpDivisions(ctx, divisionIDs)

	var notices []calendar.Notice
	for _, t := range ended {
		ds, ok := divisions[t.Division.ID]
		if !ok {
			continue
		}
		day, over := sessionOver(games[t.ID], ds, now)
		if !over {
			continue
		}
		name := strings.TrimSpace(t.Name)
		notices = append(notices, calendar.Notice{
			TeamID:  t.ID,
			Day:     day,
			Summary: name + ": this Session is over",
			Description: fmt.Sprintf("%s has played its last game of this Session, so this calendar won't get next Session's games. "+
				"Get next Session's calendar from Let's Plan Soccer: %s", name, nextURL),
			URL: nextURL,
		})
	}
	return notices
}

// lookUpDivisions returns the divisions the league answered for, by ID,
// leaving out (with a warning line) any it failed for.
func (h *Handler) lookUpDivisions(ctx context.Context, ids []string) map[string]league.DivisionSeason {
	ctx, cancel := context.WithTimeout(ctx, noticeBudget)
	defer cancel()
	found := make([]league.DivisionSeason, len(ids))
	errs := make([]error, len(ids))
	var g errgroup.Group
	g.SetLimit(4)
	for i, id := range ids {
		g.Go(func() error {
			// A panic here would not reach the request's recoverer and
			// would take the whole process down.
			defer func() {
				if v := recover(); v != nil {
					errs[i] = fmt.Errorf("%w: division season %s: panic: %v", league.ErrUnavailable, id, v)
				}
			}()
			found[i], errs[i] = h.Divisions.DivisionSeason(ctx, id)
			return nil
		})
	}
	_ = g.Wait()
	divisions := map[string]league.DivisionSeason{}
	for i, id := range ids {
		if errs[i] != nil {
			h.Log.Warn("session_over_check_failed", "division", id, "err", errs[i])
			continue
		}
		divisions[id] = found[i]
	}
	return divisions
}

// laterInLink reports whether the link already holds t in a later Session:
// a Team with the same name, trimmed and ignoring case, and a later season
// number. Another Team of a later Session is someone else's Team, so it
// does not stand in for t's.
func laterInLink(t schedule.Team, teams []schedule.Team) bool {
	season, err := strconv.Atoi(t.Season)
	if err != nil {
		return false
	}
	name := strings.ToUpper(strings.TrimSpace(t.Name))
	return slices.ContainsFunc(teams, func(u schedule.Team) bool {
		s, err := strconv.Atoi(u.Season)
		return err == nil && s > season && strings.ToUpper(strings.TrimSpace(u.Name)) == name
	})
}

// soccerPage is the site's Let's Plan Soccer page, read from LinkBaseURL
// ("https://craigdevjohnson.com/soccer/link/" gives
// "https://craigdevjohnson.com/soccer"), or "" when LinkBaseURL is not that
// shape.
func (h *Handler) soccerPage() string {
	if !strings.HasSuffix(h.LinkBaseURL, "/soccer/link/") {
		return ""
	}
	return strings.TrimSuffix(h.LinkBaseURL, "/link/")
}

// allPast reports whether a Team has games and every one has ended, by the
// usual game length.
func allPast(games []schedule.Game, now time.Time) bool {
	for _, g := range games {
		if g.End.After(now) {
			return false
		}
	}
	return len(games) > 0
}

// sessionOver reports whether a Team's Session is over, and the Mountain
// day its notice goes on. Every game must have ended, by the division's
// game length, and the Team must have played all the division's games or
// gone quietAfter without a game. Ended games alone are not enough: the
// league posts most of a Session's games only after its first one.
//
// The notice goes on the day after the last game, or, when only the quiet
// rule says the Session is over, on the day that rule first holds: the day
// after the last game would by then be a week back, where nobody looks.
// Both come from the games alone, so every fetch writes the same day.
func sessionOver(games []schedule.Game, ds league.DivisionSeason, now time.Time) (time.Time, bool) {
	if len(games) == 0 {
		return time.Time{}, false
	}
	length := cmp.Or(ds.GameLength, schedule.GameLength)
	var last time.Time
	for _, g := range games {
		if g.Start.Add(length).After(now) {
			return time.Time{}, false
		}
		if g.Start.After(last) {
			last = g.Start
		}
	}
	played := ds.NumGames > 0 && len(games) >= ds.NumGames
	quietFrom := last.Add(length).Add(quietAfter)
	quiet := !now.Before(quietFrom)
	switch {
	case played:
		y, m, d := last.In(league.Mountain).Date()
		return time.Date(y, m, d+1, 0, 0, 0, 0, league.Mountain), true
	case quiet:
		y, m, d := quietFrom.In(league.Mountain).Date()
		return time.Date(y, m, d, 0, 0, 0, 0, league.Mountain), true
	}
	return time.Time{}, false
}
