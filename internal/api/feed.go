package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"slices"
	"time"

	"golang.org/x/sync/errgroup"

	"github.com/CraigDevJohnson/soccer/internal/league"
	"github.com/CraigDevJohnson/soccer/internal/next"
	"github.com/CraigDevJohnson/soccer/internal/schedule"
)

// MaxRemembered is the most Remembered teams one Member link holds. Each
// keeps at most two Team IDs, so a feed reads at most schedule.MaxTeams.
const MaxRemembered = schedule.MaxTeams / 2

// feedName is the name of every Member link's calendar.
const feedName = "Let's Plan Soccer"

// followBudget bounds a feed's next-Session checks. They come before the
// Teams are read, so a slow league leaves a follow for a later fetch rather
// than making the calendar late. A variable only so tests can shorten it.
var followBudget = 2 * time.Second

// feedRequest is the body of POST /feeds: each Remembered team's Team IDs,
// newest first, one or two of them.
type feedRequest struct {
	Teams [][]string `json:"teams"`
}

// POST /feeds: the calendar behind a Member link. The site keeps a Member's
// Remembered teams and sends them on each fetch; nothing is stored here.
// Each Remembered team moves on to its next Session by itself when the
// next-Session finder answers follow for its newest Team ID, and the answer
// carries the Remembered teams as they now are, for the site to keep:
// {"calendar": "<ics text>", "teams": [["311111", "111111"]]}. The calendar
// holds every game, played and remaining, of every Team ID, and is built and
// failed as a Session link's is.
func (h *Handler) feed(w http.ResponseWriter, r *http.Request) {
	teams, ok := h.readFeed(w, r)
	if !ok {
		return
	}
	teams = h.follow(r.Context(), teams)
	var ids, newest []string
	for _, t := range teams {
		ids = append(ids, t...)
		newest = append(newest, t[0])
	}
	var s schedule.Schedule
	if len(ids) > 0 {
		if s, ok = h.wholeSchedule(w, r, ids); !ok {
			return
		}
	}
	// A zero Since keeps every game, played ones too.
	body, err := h.render(s, schedule.Selection{}, feedName, h.sessionNotices(r.Context(), s, newest, true))
	if err != nil {
		h.Log.Error("render calendar", "err", err)
		writeError(w, http.StatusInternalServerError, "internal", "could not build the calendar")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"calendar": body, "teams": teams})
}

// readFeed reads and checks the Remembered teams, trimming each Team ID.
func (h *Handler) readFeed(w http.ResponseWriter, r *http.Request) ([][]string, bool) {
	var req feedRequest
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<10))
	dec.DisallowUnknownFields()
	err := dec.Decode(&req)
	if err == nil && dec.Decode(&struct{}{}) != io.EOF {
		err = errors.New("trailing data")
	}
	var tooBig *http.MaxBytesError
	switch {
	case errors.As(err, &tooBig):
		writeError(w, http.StatusRequestEntityTooLarge, "too_large", "body is too large")
		return nil, false
	case err != nil:
		writeError(w, http.StatusBadRequest, "bad_request", "body must be JSON with teams")
		return nil, false
	}
	if len(req.Teams) > MaxRemembered {
		h.scheduleError(w, &schedule.InvalidError{Reasons: []string{fmt.Sprintf("at most %d Remembered teams", MaxRemembered)}})
		return nil, false
	}
	teams := make([][]string, len(req.Teams))
	var bad []string
	for i, t := range req.Teams {
		if len(t) < 1 || len(t) > 2 {
			bad = append(bad, "a Remembered team has one or two team IDs")
			continue
		}
		for _, raw := range t {
			id, err := league.ValidTeamID(raw)
			if err != nil {
				bad = append(bad, err.Error())
				continue
			}
			teams[i] = append(teams[i], id)
		}
	}
	if len(bad) > 0 {
		h.scheduleError(w, &schedule.InvalidError{Reasons: bad})
		return nil, false
	}
	return teams, true
}

// follow moves each Remembered team on to its next Session when the
// next-Session finder answers follow for its newest Team ID: the new Team
// ID goes in front, and only the two newest are kept, so the Session before
// stays and anything older drops off. The checks run at most four at a time
// within followBudget. One that fails or runs out of time leaves its team as
// it is until a later fetch, with a follow_check_failed line rather than
// league_unavailable, since the calendar still answers; a Team ID the league
// no longer knows has nowhere to go. Nor does a team whose next Team the
// Member added as a Remembered team of its own already, or that an earlier
// team in the list follows into on this same fetch: the teams stay apart
// rather than showing the same Team twice.
func (h *Handler) follow(ctx context.Context, teams [][]string) [][]string {
	if h.Next == nil || len(teams) == 0 {
		return teams
	}
	ctx, cancel := context.WithTimeout(ctx, followBudget)
	defer cancel()
	found := make([]string, len(teams))
	var g errgroup.Group
	g.SetLimit(4)
	for i, t := range teams {
		g.Go(func() error {
			// A panic here would not reach the request's recoverer and
			// would take the whole process down.
			defer func() {
				if v := recover(); v != nil {
					h.Log.Warn("follow_check_failed", "team", t[0], "err", fmt.Sprintf("panic: %v", v))
				}
			}()
			res, err := h.Next.Find(ctx, t[0])
			switch {
			case errors.Is(err, league.ErrTeamNotFound):
			case err != nil:
				h.Log.Warn("follow_check_failed", "team", t[0], "err", err)
			case res.Match == next.Follow && len(res.Teams) == 1:
				if id, err := league.ValidTeamID(res.Teams[0].ID); err == nil {
					found[i] = id
				}
			}
			return nil
		})
	}
	_ = g.Wait()
	out := make([][]string, len(teams))
	taken := map[string]bool{}
	for i, t := range teams {
		out[i] = t
		id := found[i]
		if id == "" || taken[id] || slices.ContainsFunc(teams, func(u []string) bool { return slices.Contains(u, id) }) {
			continue
		}
		taken[id] = true
		out[i] = []string{id, t[0]}
	}
	return out
}
