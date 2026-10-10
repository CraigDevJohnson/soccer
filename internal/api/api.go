// Package api is the Schedule Downloader's HTTP interface. Only the site calls
// it, with signed requests (ADR 0006); the site turns its answers into screens
// and serves Session links to calendar apps.
package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"

	"github.com/CraigDevJohnson/soccer/internal/calendar"
	"github.com/CraigDevJohnson/soccer/internal/league"
	"github.com/CraigDevJohnson/soccer/internal/link"
	"github.com/CraigDevJohnson/soccer/internal/schedule"
)

// Handler serves the Visitor flow.
type Handler struct {
	Teams     league.Teams
	Directory league.Directory
	// LinkBaseURL, when set, is the public address Session link tokens are
	// appended to, such as "https://craigdevjohnson.com/soccer/link/".
	LinkBaseURL string
	Log         *slog.Logger
	Now         func() time.Time
}

// Router returns the routes with logging, recovery and a request timeout.
func (h *Handler) Router() http.Handler {
	r := chi.NewRouter()
	r.Use(requestLogger(h.Log))
	r.Use(h.recoverer)
	r.Use(middleware.Timeout(8 * time.Second))

	r.NotFound(func(w http.ResponseWriter, _ *http.Request) {
		writeError(w, http.StatusNotFound, "not_found", "no such route")
	})
	r.MethodNotAllowed(func(w http.ResponseWriter, _ *http.Request) {
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed", "method not allowed on this route")
	})

	r.Get("/healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) })
	r.Get("/teams", h.searchTeams)
	r.Get("/teams/{teamID}", h.team)
	r.Get("/divisions", h.divisions)
	r.Get("/divisions/{divisionID}/teams", h.divisionTeams)
	r.Get("/schedule", h.schedule)
	r.Post("/calendar", h.download)
	r.Post("/links", h.createLink)
	r.Get("/links/{token}", h.serveLink)
	return r
}

func (h *Handler) now() time.Time {
	if h.Now != nil {
		return h.Now()
	}
	return time.Now()
}

// GET /teams?q=rovers
func (h *Handler) searchTeams(w http.ResponseWriter, r *http.Request) {
	q := strings.TrimSpace(r.URL.Query().Get("q"))
	if len(q) < 2 {
		writeError(w, http.StatusBadRequest, "bad_request", "search needs at least 2 characters")
		return
	}
	if len(q) > 100 {
		writeError(w, http.StatusBadRequest, "bad_request", "search is too long")
		return
	}
	teams, err := h.Directory.SearchTeams(r.Context(), q)
	if err != nil {
		h.leagueError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"teams": nonNil(teams)})
}

// GET /teams/{teamID}: one Team and its games, remaining ones marked.
func (h *Handler) team(w http.ResponseWriter, r *http.Request) {
	h.writeSchedule(w, r, []string{chi.URLParam(r, "teamID")})
}

// GET /schedule?team=123456&team=654321: several Teams combined.
func (h *Handler) schedule(w http.ResponseWriter, r *http.Request) {
	h.writeSchedule(w, r, r.URL.Query()["team"])
}

func (h *Handler) writeSchedule(w http.ResponseWriter, r *http.Request, ids []string) {
	s, err := schedule.Combine(r.Context(), h.Teams, ids, h.now())
	if err != nil {
		h.scheduleError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, s)
}

// GET /divisions: the divisions at the Facility.
func (h *Handler) divisions(w http.ResponseWriter, r *http.Request) {
	ds, err := h.Directory.Divisions(r.Context())
	if err != nil {
		h.leagueError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"divisions": nonNil(ds)})
}

// GET /divisions/{divisionID}/teams
func (h *Handler) divisionTeams(w http.ResponseWriter, r *http.Request) {
	teams, err := h.Directory.DivisionTeams(r.Context(), chi.URLParam(r, "divisionID"))
	if err != nil {
		h.leagueError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"teams": nonNil(teams)})
}

// choice is the body of POST /calendar and POST /links: the Teams, and the
// games the person changed from the default (remaining games ticked).
type choice struct {
	TeamIDs  []string `json:"team_ids"`
	Excluded []string `json:"excluded"`
	Included []string `json:"included"`
}

func (h *Handler) readChoice(w http.ResponseWriter, r *http.Request) (schedule.Selection, bool) {
	var c choice
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<10))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&c); err != nil || dec.Decode(&struct{}{}) != io.EOF {
		writeError(w, http.StatusBadRequest, "bad_request", "body must be JSON with team_ids, excluded and included")
		return schedule.Selection{}, false
	}
	return schedule.Selection{
		TeamIDs:  c.TeamIDs,
		Excluded: c.Excluded,
		Included: c.Included,
		Since:    h.now().Truncate(time.Second),
	}, true
}

// POST /calendar: the chosen games as a file to download.
func (h *Handler) download(w http.ResponseWriter, r *http.Request) {
	sel, ok := h.readChoice(w, r)
	if !ok {
		return
	}
	s, err := schedule.Combine(r.Context(), h.Teams, sel.TeamIDs, h.now())
	if err != nil {
		h.scheduleError(w, err)
		return
	}
	// A file missing one Team's games would look complete to the person.
	if s.Unavailable() {
		h.leagueError(w, league.ErrUnavailable)
		return
	}
	body, err := h.render(s, sel)
	if err != nil {
		h.Log.Error("render calendar", "err", err)
		writeError(w, http.StatusInternalServerError, "internal", "could not build the calendar")
		return
	}
	w.Header().Set("Content-Type", "text/calendar; charset=utf-8")
	w.Header().Set("Content-Disposition", fmt.Sprintf(`attachment; filename=%q`, calendar.Filename(teamNames(s))))
	_, _ = w.Write([]byte(body))
}

// POST /links: a Session link for the chosen games. The Teams are read first
// so that a link is never handed out for Team IDs that do not exist.
func (h *Handler) createLink(w http.ResponseWriter, r *http.Request) {
	sel, ok := h.readChoice(w, r)
	if !ok {
		return
	}
	s, err := schedule.Combine(r.Context(), h.Teams, sel.TeamIDs, h.now())
	if err != nil {
		h.scheduleError(w, err)
		return
	}
	// A Team the league failed to answer for would be dropped from the link
	// for good, so the link waits until the league answers for every Team.
	if s.Unavailable() {
		h.leagueError(w, league.ErrUnavailable)
		return
	}
	sel.TeamIDs = s.TeamIDs()
	token, err := link.Encode(sel)
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_request", err.Error())
		return
	}
	resp := map[string]any{"token": token, "teams": s.Teams, "failed": s.Failed}
	if h.LinkBaseURL != "" {
		resp["url"] = h.LinkBaseURL + token + ".ics"
	}
	writeJSON(w, http.StatusCreated, resp)
}

// GET /links/{token}: what a calendar app fetches. It reads the Teams afresh
// each time, so changed times and newly posted games come through.
func (h *Handler) serveLink(w http.ResponseWriter, r *http.Request) {
	sel, err := link.Decode(strings.TrimSuffix(chi.URLParam(r, "token"), ".ics"))
	if err != nil {
		writeError(w, http.StatusNotFound, "invalid_link", "this calendar link is not valid")
		return
	}
	s, err := schedule.Combine(r.Context(), h.Teams, sel.TeamIDs, h.now())
	switch {
	case errors.Is(err, schedule.ErrNoTeams):
		// The Session is over and its Team IDs are gone: an empty calendar
		// rather than an error, so the app quietly shows nothing.
	case err != nil:
		h.scheduleError(w, err)
		return
	case s.Unavailable():
		// A 200 missing one Team's games would make the calendar app drop
		// them; a 503 makes it keep its last full copy.
		h.leagueError(w, league.ErrUnavailable)
		return
	}
	body, err := h.render(s, sel)
	if err != nil {
		h.Log.Error("render calendar", "err", err)
		writeError(w, http.StatusInternalServerError, "internal", "could not build the calendar")
		return
	}
	w.Header().Set("Content-Type", "text/calendar; charset=utf-8")
	w.Header().Set("Cache-Control", "private, max-age=900")
	_, _ = w.Write([]byte(body))
}

func (h *Handler) render(s schedule.Schedule, sel schedule.Selection) (string, error) {
	chosen := sel.Chosen(s.Games)
	events := make([]calendar.Event, len(chosen))
	for i, g := range chosen {
		events[i] = calendar.Event{GameID: g.ID, Start: g.Start, End: g.End, Field: g.Field, Home: g.Home, Away: g.Away}
	}
	name := calendar.Name(teamNames(s))
	if name == "" {
		name = "Soccer"
	}
	return calendar.Write(calendar.Calendar{Name: name, Events: events}, h.now())
}

func teamNames(s schedule.Schedule) []string {
	names := make([]string, len(s.Teams))
	for i, t := range s.Teams {
		names[i] = t.Name
	}
	return names
}

func (h *Handler) scheduleError(w http.ResponseWriter, err error) {
	var invalid *schedule.InvalidError
	switch {
	case errors.As(err, &invalid):
		writeJSON(w, http.StatusBadRequest, map[string]any{
			"code": "invalid_team_id", "message": "check the team IDs", "details": invalid.Reasons,
		})
	case errors.Is(err, schedule.ErrNoTeams):
		writeError(w, http.StatusNotFound, "team_not_found", "no team has that team ID")
	default:
		h.leagueError(w, err)
	}
}

func (h *Handler) leagueError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, context.Canceled):
		// The caller went away; not an outage, so no alert line.
		writeError(w, http.StatusServiceUnavailable, "canceled", "request canceled")
	case errors.Is(err, league.ErrNotConnected):
		writeError(w, http.StatusNotImplemented, "not_connected", "this lookup is not connected to the league yet")
	case errors.Is(err, league.ErrTeamNotFound):
		writeError(w, http.StatusNotFound, "team_not_found", "no team has that team ID")
	case errors.Is(err, league.ErrUnavailable):
		// A distinct line, so an alarm can email Craig when the league is down.
		h.Log.Warn("league_unavailable", "err", err)
		writeError(w, http.StatusServiceUnavailable, "league_unavailable", "the league's schedule service is not answering")
	default:
		h.Log.Error("unexpected", "err", err)
		writeError(w, http.StatusInternalServerError, "internal", "something went wrong")
	}
}

func writeError(w http.ResponseWriter, status int, code, message string) {
	writeJSON(w, status, map[string]string{"code": code, "message": message})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func nonNil[T any](s []T) []T {
	if s == nil {
		return []T{}
	}
	return s
}

// requestLogger writes one line per request. It logs the route pattern, not
// the path, so link tokens and search terms stay out of the logs; the user
// agent on /links/{token} says which calendar app fetched a link, and the
// route of each POST says which output a person chose.
func requestLogger(log *slog.Logger) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			start := time.Now()
			ww := middleware.NewWrapResponseWriter(w, r.ProtoMajor)
			next.ServeHTTP(ww, r)
			route := ""
			if rc := chi.RouteContext(r.Context()); rc != nil {
				route = rc.RoutePattern()
			}
			log.Info("request",
				"method", r.Method,
				"route", route,
				"status", ww.Status(),
				"bytes", ww.BytesWritten(),
				"ms", time.Since(start).Milliseconds(),
				"ua", r.UserAgent(),
			)
		})
	}
}

// recoverer turns a panic into the usual JSON 500.
func (h *Handler) recoverer(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if v := recover(); v != nil {
				if v == http.ErrAbortHandler {
					panic(v)
				}
				h.Log.Error("panic", "err", v, "route", chi.RouteContext(r.Context()).RoutePattern())
				writeError(w, http.StatusInternalServerError, "internal", "something went wrong")
			}
		}()
		next.ServeHTTP(w, r)
	})
}
