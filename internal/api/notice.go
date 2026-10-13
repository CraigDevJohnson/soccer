package api

import (
	"cmp"
	"context"
	"fmt"
	"net/url"
	"slices"
	"strings"
	"time"

	"github.com/CraigDevJohnson/soccer/internal/calendar"
	"github.com/CraigDevJohnson/soccer/internal/league"
	"github.com/CraigDevJohnson/soccer/internal/schedule"
)

// quietAfter is how long after a Team's last game, with nothing new posted,
// its Session counts as over even though the league lists fewer games than
// the division plays. Divisions play weekly, so a week and a day without a
// game is the end.
const quietAfter = 8 * 24 * time.Hour

// sessionNotices returns one notice for each Team in a Session link whose
// Session is over, in link order. A Session link is good for one Session:
// the notice says so on the day after the Team's last game and links to the
// site's Let's Plan Soccer page with every Team ID of the link as "after",
// where the person can add next Session's Teams and get a new link. The link
// itself never follows a Team into its next Session.
//
// A division is looked up only once every game a Team has is past. When the
// lookup fails the notice is left out this time and a later fetch adds it;
// the calendar still answers, since its games are all there.
func (h *Handler) sessionNotices(ctx context.Context, s schedule.Schedule, linkTeamIDs []string) []calendar.Notice {
	page := h.soccerPage()
	if page == "" || h.Divisions == nil {
		return nil
	}
	nextURL := page + "?" + url.Values{"after": linkTeamIDs}.Encode()
	now := h.now()
	var notices []calendar.Notice
	for _, t := range s.Teams {
		var games []schedule.Game
		for _, g := range s.Games {
			if slices.Contains(g.TeamIDs, t.ID) {
				games = append(games, g)
			}
		}
		if !allPast(games, now) {
			continue
		}
		ds, err := h.Divisions.DivisionSeason(ctx, t.Division.ID)
		if err != nil {
			h.Log.Warn("session_over_check_failed", "division", t.Division.ID, "err", err)
			continue
		}
		day, over := sessionOver(games, ds, now)
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
// day after its last game. Every game must have ended, by the division's
// game length, and the Team must have played all the division's games or
// gone quietAfter without a game. Ended games alone are not enough: the
// league posts most of a Session's games only after its first one.
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
	quiet := !now.Before(last.Add(length).Add(quietAfter))
	if !played && !quiet {
		return time.Time{}, false
	}
	y, m, d := last.In(league.Mountain).Date()
	return time.Date(y, m, d+1, 0, 0, 0, 0, league.Mountain), true
}
