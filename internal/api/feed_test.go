package api

import (
	"context"
	"net/http"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/CraigDevJohnson/soccer/internal/league"
	"github.com/CraigDevJohnson/soccer/internal/next"
)

// listOf is the league's Team list, as given.
type listOf []league.Listed

func (l listOf) Listed(context.Context) ([]league.Listed, error) { return l, nil }

// finderFunc is a NextFinder made of one function.
type finderFunc func(ctx context.Context, teamID string) (next.Result, error)

func (f finderFunc) Find(ctx context.Context, teamID string) (next.Result, error) {
	return f(ctx, teamID)
}

type feedBody struct {
	Calendar string
	Teams    [][]string
}

func uids(calendar string) []string {
	var out []string
	for _, line := range strings.Split(strings.ReplaceAll(calendar, "\r\n ", ""), "\r\n") {
		if uid, ok := strings.CutPrefix(line, "UID:"); ok {
			out = append(out, strings.TrimSuffix(uid, "@soccer.craigdevjohnson.com"))
		}
	}
	return out
}

// A Remembered team whose newest Team answers follow takes the next
// Session's Team on by itself: it goes in front, the Session before stays,
// and the one before that drops off.
func TestFeedFollowsATeamIntoItsNextSession(t *testing.T) {
	teams := sessionTeams{
		"100001": {Name: "ROVERS", Season: "171", CoachID: "900001", Division: league.Division{ID: "d0"}, Games: weekly("p", at(1, 19), 2)},
		"111111": {Name: "ROVERS", Season: "173", CoachID: "900001", Division: league.Division{ID: "d1"}, Games: weekly("r", at(11, 19), 3)},
		"311111": {Name: "ROVERS", Season: "175", CoachID: "900001", Division: league.Division{ID: "d2"}, Games: weekly("n", at(25, 19), 2)},
	}
	lists := listOf{{TeamSummary: league.TeamSummary{ID: "311111", Name: "ROVERS", Division: league.Division{ID: "d2"}, Season: "175"}, CoachID: "900001"}}
	h, logs := newHandler(teams, fakeDirectory{})
	h.Next = &next.Resolver{Teams: teams, Lists: lists, Divisions: fakeDivisions{}}
	r := h.Router()

	rec := do(t, r, "POST", "/feeds", `{"teams":[["111111","100001"]]}`)
	if rec.Code != http.StatusOK || rec.Header().Get("Content-Type") != "application/json" {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
	got := decode[feedBody](t, rec)
	if want := [][]string{{"311111", "111111"}}; !slices.EqualFunc(got.Teams, want, slices.Equal) {
		t.Fatalf("teams = %v, want %v", got.Teams, want)
	}
	// Every game of both, played ones too; none of the Session dropped.
	if want := []string{"game-r1", "game-r2", "game-r3", "game-n1", "game-n2"}; !slices.Equal(uids(got.Calendar), want) {
		t.Fatalf("events = %v, want %v", uids(got.Calendar), want)
	}
	if !strings.Contains(got.Calendar, "X-WR-CALNAME:Let's Plan Soccer\r\n") {
		t.Fatalf("calendar name:\n%s", got.Calendar)
	}
	if !strings.Contains(logs.String(), `"route":"/feeds"`) || strings.Contains(logs.String(), "follow_check_failed") {
		t.Fatalf("logs: %s", logs)
	}

	// Next fetch: the new Team has no later Session yet, so nothing moves.
	got = decode[feedBody](t, do(t, r, "POST", "/feeds", `{"teams":[["311111","111111"]]}`))
	if want := [][]string{{"311111", "111111"}}; !slices.EqualFunc(got.Teams, want, slices.Equal) {
		t.Fatalf("second fetch teams = %v, want %v", got.Teams, want)
	}
}

// A next-Session check that fails or runs out of time leaves the team where
// it is this fetch. The calendar still answers, and the line it writes is
// not the one the league alarm counts.
func TestFeedServesWhenAFollowCheckFails(t *testing.T) {
	defer func(d time.Duration) { followBudget = d }(followBudget)
	followBudget = 50 * time.Millisecond
	var mu sync.Mutex
	var left time.Duration
	failing := finderFunc(func(ctx context.Context, _ string) (next.Result, error) {
		mu.Lock()
		if deadline, ok := ctx.Deadline(); ok {
			left = time.Until(deadline)
		}
		mu.Unlock()
		return next.Result{}, league.ErrUnavailable
	})
	blocking := finderFunc(func(ctx context.Context, _ string) (next.Result, error) {
		<-ctx.Done()
		return next.Result{}, ctx.Err()
	})
	for name, finder := range map[string]NextFinder{"fails": failing, "runs out of time": blocking} {
		t.Run(name, func(t *testing.T) {
			h, logs := newHandler(fakeTeams{}, fakeDirectory{})
			h.Next = finder
			start := time.Now()
			rec := do(t, h.Router(), "POST", "/feeds", `{"teams":[["111111"],["222222"]]}`)
			if rec.Code != http.StatusOK {
				t.Fatalf("%d %s", rec.Code, rec.Body)
			}
			if took := time.Since(start); took > time.Second {
				t.Fatalf("took %v, want the checks to give up after %v", took, followBudget)
			}
			got := decode[feedBody](t, rec)
			if want := [][]string{{"111111"}, {"222222"}}; !slices.EqualFunc(got.Teams, want, slices.Equal) {
				t.Fatalf("teams = %v, want %v", got.Teams, want)
			}
			// ROVERS and KICKERS share g3.
			if n := strings.Count(got.Calendar, "BEGIN:VEVENT"); n != 3 {
				t.Fatalf("%d events, want all 3 games:\n%s", n, got.Calendar)
			}
			if strings.Count(logs.String(), `"msg":"follow_check_failed"`) != 2 || strings.Contains(logs.String(), "league_unavailable") {
				t.Fatalf("logs: %s", logs)
			}
		})
	}
	if left <= 0 || left > followBudget {
		t.Fatalf("checks had %v left, want at most %v", left, followBudget)
	}
}

// A Member who added next Session's Team as a Remembered team of its own
// keeps the two apart: the earlier one doesn't follow into the same Team.
func TestFeedDoesNotFollowIntoAnotherRememberedTeam(t *testing.T) {
	h, _ := newServer(t, fakeTeams{}, fakeDirectory{})
	// 111111 answers follow to 311111 (fakeLists).
	got := decode[feedBody](t, do(t, h, "POST", "/feeds", `{"teams":[["111111"],["311111"]]}`))
	if want := [][]string{{"111111"}, {"311111"}}; !slices.EqualFunc(got.Teams, want, slices.Equal) {
		t.Fatalf("teams = %v, want %v", got.Teams, want)
	}
}

// Two Remembered teams that both follow into the same Team: only the first
// moves on, so the Team shows once.
func TestFeedFollowsIntoATeamOnce(t *testing.T) {
	h, _ := newHandler(fakeTeams{}, fakeDirectory{})
	h.Next = finderFunc(func(context.Context, string) (next.Result, error) {
		return next.Result{Match: next.Follow, Teams: []next.Candidate{{TeamSummary: league.TeamSummary{ID: "311111", Name: "ROVERS"}, SameCoach: true}}}, nil
	})
	got := decode[feedBody](t, do(t, h.Router(), "POST", "/feeds", `{"teams":[["111111"],["100001"]]}`))
	if want := [][]string{{"311111", "111111"}, {"100001"}}; !slices.EqualFunc(got.Teams, want, slices.Equal) {
		t.Fatalf("teams = %v, want %v", got.Teams, want)
	}
}

// With no Remembered teams, or only Teams the league no longer knows, the
// calendar is empty, not an error: a calendar app then shows nothing. The
// league being down is different: the app keeps its last copy.
func TestFeedEmptyCalendar(t *testing.T) {
	h, _ := newServer(t, fakeTeams{}, fakeDirectory{})
	for body, teams := range map[string][][]string{
		`{"teams":[]}`:           {},
		`{"teams":[["333333"]]}`: {{"333333"}},
	} {
		rec := do(t, h, "POST", "/feeds", body)
		if rec.Code != http.StatusOK {
			t.Fatalf("%s: %d %s", body, rec.Code, rec.Body)
		}
		got := decode[feedBody](t, rec)
		if !strings.HasPrefix(got.Calendar, "BEGIN:VCALENDAR") || strings.Contains(got.Calendar, "BEGIN:VEVENT") || got.Teams == nil ||
			!slices.EqualFunc(got.Teams, teams, slices.Equal) {
			t.Fatalf("%s: %+v", body, got)
		}
	}

	down, logs := newServer(t, fakeTeams{down: true}, fakeDirectory{})
	if rec := do(t, down, "POST", "/feeds", `{"teams":[["111111"]]}`); rec.Code != http.StatusServiceUnavailable || !strings.Contains(logs.String(), `"msg":"league_unavailable"`) {
		t.Fatalf("league down: %d %s", rec.Code, rec.Body)
	}
	// One Team's games missing would make the app delete them.
	flaky, _ := newServer(t, fakeTeams{downOnly: "222222"}, fakeDirectory{})
	if rec := do(t, flaky, "POST", "/feeds", `{"teams":[["111111"],["222222"]]}`); rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("one Team down: %d %s", rec.Code, rec.Body)
	}
}

// A Remembered team whose Session is over, with no Team to follow, gets
// the notice in a Member link's words. Only each team's newest Team ID can
// get it or go in "after": its other one is the Session before.
func TestFeedNoticeForAMember(t *testing.T) {
	teams := sessionTeams{
		"100002": {Name: "KICKERS FC", Season: "171", Division: league.Division{ID: "d0"}, Games: weekly("o", at(1, 18), 8)},
		"222222": {Name: "KICKERS", Season: "173", Division: league.Division{ID: "d1"}, Games: weekly("k", at(4, 18), 8)},
		"111111": {Name: "ROVERS", Season: "173", Division: league.Division{ID: "d1"}, Games: append(weekly("r", at(4, 19), 2), weekly("s", at(11, 19), 1)...)},
	}
	h, _ := newHandler(teams, fakeDirectory{})
	h.Next = &next.Resolver{Teams: teams, Lists: listOf{}, Divisions: fakeDivisions{}}
	rec := do(t, h.Router(), "POST", "/feeds", `{"teams":[["222222","100002"],["111111"]]}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
	body := strings.ReplaceAll(decode[feedBody](t, rec).Calendar, "\r\n ", "")
	for _, want := range []string{
		"UID:session-over-222222@soccer.craigdevjohnson.com\r\n",
		"SUMMARY:KICKERS: this Session is over\r\n",
		"DESCRIPTION:KICKERS has played its last game of this Session. Pick next Session's Team on Let's Plan Soccer\\, " +
			"and its games join this same calendar: https://example.test/soccer?after=222222&after=111111\r\n",
		"URL:https://example.test/soccer?after=222222&after=111111\r\n",
	} {
		if !strings.Contains(body, want) {
			t.Errorf("missing %q in:\n%s", want, body)
		}
	}
	if n := strings.Count(body, "session-over-"); n != 1 {
		t.Errorf("%d notices, want only KICKERS's", n)
	}
}

func TestFeedRefusesBadBodies(t *testing.T) {
	h, _ := newServer(t, fakeTeams{}, fakeDirectory{})
	for body, code := range map[string]string{
		`not json`:                                        "bad_request",
		`{"teams":[["111111"]],"extra":1}`:                "bad_request",
		`{"teams":[["111111"]]} {"teams":[]}`:             "bad_request",
		`{"teams":[[]]}`:                                  "invalid_team_id",
		`{"teams":[["111111","222222","444444"]]}`:        "invalid_team_id",
		`{"teams":[["12"]]}`:                              "invalid_team_id",
		`{"teams":[["1"],["2"],["3"],["4"],["5"],["6"]]}`: "invalid_team_id",
	} {
		if rec := do(t, h, "POST", "/feeds", body); rec.Code != http.StatusBadRequest || !strings.Contains(rec.Body.String(), code) {
			t.Errorf("%s: %d %s", body, rec.Code, rec.Body)
		}
	}
	// A Team ID is trimmed, as everywhere else.
	got := decode[feedBody](t, do(t, h, "POST", "/feeds", `{"teams":[[" 222222 "]]}`))
	if want := [][]string{{"222222"}}; !slices.EqualFunc(got.Teams, want, slices.Equal) {
		t.Fatalf("teams = %v, want %v", got.Teams, want)
	}
}
