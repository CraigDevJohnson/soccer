package league

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"
)

// sampleTeam is the league's live shape (2026-10-10), trimmed: game IDs are
// "UGameID" and times carry milliseconds.
const sampleTeam = `{
  "team": {"UTeamID": 123456, "team_name": "ROVERS", "division_name": "Coed Over 30 B Sun", "div_code": "CO30BSun",
           "Dropped": false, "UDivSeasonID": 110046, "Season": 2026, "facility_name": "Boise", "FacilityID": 4},
  "games": [
    {"UGameID": 2988396, "SchedGameDateTime": "2026-10-14T19:15:00.000Z", "schedGameEndTime": null, "field_name": "Field 3", "Field": 3,
     "result": null, "FacilityID": 4, "UTeam1": 123456, "UTeam2": 222222,
     "home_team": {"UTeamID": 123456, "team_name": "ROVERS"}, "visitor_team": {"UTeamID": 222222, "team_name": "KICKERS"}},
    {"UGameID": 2995453, "SchedGameDateTime": "2026-10-21T20:00:00+00:00", "field_name": "", "Field": 2,
     "home_team": {"team_name": "BANDITS"}, "visitor_team": {"team_name": "ROVERS"}},
    {"UGameID": 1, "SchedGameDateTime": "", "Field": 1,
     "home_team": {"team_name": "A"}, "visitor_team": {"team_name": "B"}},
    {"UGameID": 2, "SchedGameDateTime": "2026-10-28T20:00:00Z", "Field": 0,
     "home_team": {"team_name": "A"}, "visitor_team": {"team_name": "B"}},
    {"UGameID": 3, "SchedGameDateTime": "2026-10-28T20:00:00Z", "Field": 1,
     "home_team": {"team_name": ""}, "visitor_team": {"team_name": "B"}}
  ],
  "standings": [{"Season": 2026, "TeamName": "ROVERS", "UTeamID": 123456, "UDivSeasonID": 110046, "position": 1}],
  "roster": null
}`

func serve(t *testing.T, status int, body string) (*Client, *int) {
	t.Helper()
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.URL.Path != "/teams/123456" {
			t.Errorf("path = %q", r.URL.Path)
		}
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	return NewClient(srv.URL), &calls
}

func TestTeamParsesGamesInMountainTime(t *testing.T) {
	c, _ := serve(t, http.StatusOK, sampleTeam)
	team, err := c.Team(context.Background(), " 123456 ")
	if err != nil {
		t.Fatal(err)
	}
	if team.ID != "123456" || team.Name != "ROVERS" || team.Season != "2026" {
		t.Fatalf("team = %+v", team)
	}
	if d := team.Division; d.ID != "110046" || d.Name != "Coed Over 30 B Sun" || d.Code != "CO30BSun" {
		t.Fatalf("division = %+v", d)
	}
	if len(team.Games) != 2 {
		t.Fatalf("got %d games, want 2 (unscheduled rows skipped): %+v", len(team.Games), team.Games)
	}
	g := team.Games[0]
	want := time.Date(2026, 10, 14, 19, 15, 0, 0, Mountain)
	if g.ID != "2988396" {
		t.Fatalf("game ID = %q, want the league's UGameID", g.ID)
	}
	if !g.Start.Equal(want) || g.Field != "3" || g.Home != "ROVERS" || g.Away != "KICKERS" {
		t.Fatalf("game 0 = %+v, want start %v", g, want)
	}
	// "Z" is ignored: 19:15 is Boise wall-clock time, which is 01:15 UTC next day in October.
	if got := g.Start.UTC().Format("2006-01-02T15:04"); got != "2026-10-15T01:15" {
		t.Fatalf("UTC = %s", got)
	}
	if team.Games[1].Field != "2" {
		t.Fatalf("field fallback to number: %+v", team.Games[1])
	}
}

func TestTeamWithAnUnreadableGameTimeIsUnavailable(t *testing.T) {
	body := `{"team": {"team_name": "ROVERS"}, "games": [
	  {"game_id": "g1", "SchedGameDateTime": "2026-10-14T19:15:00Z", "Field": 3,
	   "home_team": {"team_name": "ROVERS"}, "visitor_team": {"team_name": "KICKERS"}},
	  {"game_id": "g2", "SchedGameDateTime": "10/21/2026 8:00 PM", "Field": 2,
	   "home_team": {"team_name": "BANDITS"}, "visitor_team": {"team_name": "ROVERS"}}]}`
	c, _ := serve(t, http.StatusOK, body)
	_, err := c.Team(context.Background(), "123456")
	if !errors.Is(err, ErrUnavailable) {
		t.Fatalf("err = %v, want ErrUnavailable: a game the code cannot read must not vanish from calendars", err)
	}
}

func TestTeamErrors(t *testing.T) {
	tests := []struct {
		name   string
		status int
		body   string
		want   error
	}{
		{"not found", http.StatusNotFound, `{"message":"team not found"}`, ErrTeamNotFound},
		{"not found, other spelling", http.StatusNotFound, `{"message": " Team Not Found "}`, ErrTeamNotFound},
		{"gone", http.StatusGone, `{"message":"team not found"}`, ErrTeamNotFound},
		// A 404 or 410 that does not say the Team is absent is the league in
		// trouble: an unknown route must never empty every calendar.
		{"unknown route", http.StatusNotFound, `{"status":404,"error":"Not Found"}`, ErrUnavailable},
		{"other lookup not found", http.StatusNotFound, `{"message":"Division Season not found"}`, ErrUnavailable},
		{"not found, no body", http.StatusNotFound, ``, ErrUnavailable},
		{"not found page", http.StatusNotFound, `<html>team not found</html>`, ErrUnavailable},
		{"gone, no body", http.StatusGone, ``, ErrUnavailable},
		{"bad request", http.StatusBadRequest, ``, ErrUnavailable},
		{"no team object", http.StatusOK, `{"message": "maintenance"}`, ErrUnavailable},
		{"rate limited", http.StatusTooManyRequests, ``, ErrUnavailable},
		{"refused", http.StatusForbidden, ``, ErrUnavailable},
		{"empty team", http.StatusOK, `{"team": {"team_name": ""}, "games": []}`, ErrTeamNotFound},
		{"server error", http.StatusBadGateway, ``, ErrUnavailable},
		{"garbage", http.StatusOK, `<html>`, ErrUnavailable},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			c, _ := serve(t, tt.status, tt.body)
			if _, err := c.Team(context.Background(), "123456"); !errors.Is(err, tt.want) {
				t.Fatalf("err = %v, want %v", err, tt.want)
			}
		})
	}
}

// The Tool knows Boise only: a Team that plays at another Facility is not
// one of ours, but a Team object without a FacilityID is accepted.
func TestTeamFacility(t *testing.T) {
	tests := []struct {
		name     string
		facility string
		want     error
	}{
		{"ours", `"FacilityID": 4,`, nil},
		{"ours as a string", `"FacilityID": "4",`, nil},
		{"absent", ``, nil},
		{"null", `"FacilityID": null,`, nil},
		{"another facility", `"FacilityID": 7,`, ErrTeamNotFound},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			c, _ := serve(t, http.StatusOK, `{"team": {`+tt.facility+` "team_name": "ROVERS", "Season": 2026}, "games": []}`)
			team, err := c.Team(context.Background(), "123456")
			if !errors.Is(err, tt.want) {
				t.Fatalf("err = %v, want %v", err, tt.want)
			}
			if tt.want == nil && team.Name != "ROVERS" {
				t.Fatalf("team = %+v", team)
			}
		})
	}

	c, _ := serve(t, http.StatusOK, sampleTeam)
	c.FacilityID = "7"
	if _, err := c.Team(context.Background(), "123456"); !errors.Is(err, ErrTeamNotFound) {
		t.Fatalf("Boise Team read by a client for facility 7: err = %v, want ErrTeamNotFound", err)
	}
}

func TestTeamUnreachable(t *testing.T) {
	c := NewClient("http://127.0.0.1:1")
	if _, err := c.Team(context.Background(), "123456"); !errors.Is(err, ErrUnavailable) {
		t.Fatalf("err = %v", err)
	}
}

func TestTeamRejectsBadIDWithoutCalling(t *testing.T) {
	c, calls := serve(t, http.StatusOK, sampleTeam)
	for _, id := range []string{"", "12345", "1234567", "abcdef", "000000", "12/456"} {
		if _, err := c.Team(context.Background(), id); err == nil {
			t.Errorf("%q accepted", id)
		}
	}
	if *calls != 0 {
		t.Fatalf("league called %d times", *calls)
	}
}

func TestParseLeagueTime(t *testing.T) {
	want := time.Date(2026, 3, 8, 9, 30, 0, 0, Mountain)
	for _, s := range []string{"2026-03-08T09:30:00Z", "2026-03-08T09:30:00", "2026-03-08T09:30:00+00:00", "2026-03-08T09:30:00.000Z", "2026-03-08T09:30:00-07:00"} {
		got, err := ParseLeagueTime(s)
		if err != nil || !got.Equal(want) {
			t.Errorf("%s: got %v, %v", s, got, err)
		}
	}
	if _, err := ParseLeagueTime("2026-03-08"); err == nil {
		t.Error("date without time accepted")
	}
}

func TestTeamCanceledIsNotAnOutage(t *testing.T) {
	c, _ := serve(t, http.StatusOK, sampleTeam)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err := c.Team(ctx, "123456")
	if !errors.Is(err, context.Canceled) || errors.Is(err, ErrUnavailable) {
		t.Fatalf("err = %v", err)
	}
}

func TestLeadingZeroTeamID(t *testing.T) {
	if id, err := ValidTeamID("012345"); err != nil || id != "012345" {
		t.Fatalf("%q %v", id, err)
	}
}

// The league may change number/string choices again, and soccer_scraper's
// "game_id" is still read when "UGameID" is absent.
func TestTeamAcceptsNumbersOrStrings(t *testing.T) {
	c, _ := serve(t, http.StatusOK, `{
	  "team": {"Season": "2026 Fall", "team_name": "ROVERS"},
	  "games": [
	    {"game_id": 42, "SchedGameDateTime": "2026-10-14T19:15:00Z", "field_name": "", "Field": "3",
	     "home_team": {"team_name": "ROVERS"}, "visitor_team": {"team_name": "KICKERS"}},
	    {"game_id": null, "SchedGameDateTime": "2026-10-21T19:15:00Z", "Field": 2,
	     "home_team": {"team_name": "ROVERS"}, "visitor_team": {"team_name": "OWLS"}}
	  ]}`)
	team, err := c.Team(context.Background(), "123456")
	if err != nil {
		t.Fatal(err)
	}
	if team.Season != "2026 Fall" || len(team.Games) != 2 || team.Games[0].ID != "42" || team.Games[0].Field != "3" || team.Games[1].ID != "" || team.Games[1].Field != "2" {
		t.Fatalf("%+v", team)
	}
}

// sampleList is the league's Team list in its live shape (2026-10-13), with
// made-up Teams: an array of rows, ids mostly numbers, a name padded with
// spaces, a Team that dropped out and one at another Facility.
const sampleList = `[
  {"UTeamID": 111111, "team_name": "ROVERS", "division_name": "Coed Open Sun", "div_code": "COSun", "Dropped": false,
   "UCoachID": 900001, "UAltID": null, "UDivSeasonID": 110001, "Season": 173, "role_result": null, "facility_name": "Boise",
   "team_action": null, "player_that_belongs": null, "FacilityID": 4, "Color": "RED"},
  {"UTeamID": "222222", "team_name": " Kickers  ", "division_name": "Coed Open Sun", "div_code": "COSun", "Dropped": false,
   "UCoachID": "900002", "UDivSeasonID": "110001", "Season": "173", "FacilityID": "4", "Color": null},
  {"UTeamID": 333333, "team_name": "ROVERS", "division_name": "Coed Open Sun", "div_code": "COSun",
   "UCoachID": 900001, "UDivSeasonID": 110002, "Season": 175},
  {"UTeamID": 444444, "team_name": "QUITTERS", "division_name": "Coed Open Sun", "div_code": "COSun", "Dropped": true,
   "UCoachID": 900003, "UDivSeasonID": 110001, "Season": 173, "FacilityID": 4},
  {"UTeamID": 555555, "team_name": "VISITORS", "division_name": "Mens Open Tues", "div_code": "MOTues", "Dropped": false,
   "UCoachID": 900004, "UDivSeasonID": 120001, "Season": 173, "FacilityID": 7},
  {"UTeamID": 666666, "team_name": "  ", "UDivSeasonID": 110001, "Season": 173, "FacilityID": 4}
]`

// serveList answers every Team list search with answer(name) and records
// each request's query. The league must never get a cookie or a sign-in.
func serveList(t *testing.T, answer func(name string) (int, string)) (*Client, func() []string) {
	t.Helper()
	var mu sync.Mutex
	var queries []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/teams/search_teams" {
			t.Errorf("%s %s", r.Method, r.URL.Path)
		}
		if r.Header.Get("Cookie") != "" || r.Header.Get("Authorization") != "" {
			t.Errorf("credentials sent to the league")
		}
		mu.Lock()
		queries = append(queries, r.URL.RawQuery)
		mu.Unlock()
		status, body := answer(r.URL.Query().Get("name"))
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	return NewClient(srv.URL), func() []string {
		mu.Lock()
		defer mu.Unlock()
		return slices.Clone(queries)
	}
}

func TestTeamListReadsTheLeaguesList(t *testing.T) {
	c, queries := serveList(t, func(string) (int, string) { return http.StatusOK, sampleList })
	teams, err := c.TeamList(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	// The wildcard, escaped as the league's own site sends it, in one request.
	if got := queries(); len(got) != 1 || got[0] != "name=%25&facility_id=4" {
		t.Fatalf("queries = %q", got)
	}
	open := Division{ID: "110001", Name: "Coed Open Sun", Code: "COSun", Season: "173"}
	want := []Listed{
		{TeamSummary: TeamSummary{ID: "111111", Name: "ROVERS", Division: open, Season: "173"}, CoachID: "900001"},
		{TeamSummary: TeamSummary{ID: "222222", Name: "Kickers", Division: open, Season: "173"}, CoachID: "900002"},
		// No FacilityID is not proof of another Facility.
		{TeamSummary: TeamSummary{ID: "333333", Name: "ROVERS", Division: Division{ID: "110002", Name: "Coed Open Sun", Code: "COSun", Season: "175"}, Season: "175"}, CoachID: "900001"},
	}
	if !slices.Equal(teams, want) {
		t.Fatalf("teams =\n%+v\nwant\n%+v", teams, want)
	}
	// The coach id is for comparing Teams, never for an answer.
	b, err := json.Marshal(teams)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(b), "900001") || strings.Contains(strings.ToLower(string(b)), "coach") {
		t.Fatalf("coach id serialised: %s", b)
	}
}

func TestTeamListAtAnotherFacility(t *testing.T) {
	c, queries := serveList(t, func(string) (int, string) { return http.StatusOK, sampleList })
	c.FacilityID = "7"
	teams, err := c.TeamList(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if got := queries(); len(got) != 1 || got[0] != "name=%25&facility_id=7" {
		t.Fatalf("queries = %q", got)
	}
	var ids []string
	for _, l := range teams {
		ids = append(ids, l.ID)
	}
	if !slices.Equal(ids, []string{"333333", "555555"}) {
		t.Fatalf("Team IDs = %v, want the Facility's Team and the one with no FacilityID", ids)
	}
}

// If the wildcard ever stops working, name=% answers [] and the list is
// read as one search per first letter or digit instead.
func TestTeamListFallsBackToPrefixSearches(t *testing.T) {
	var mu sync.Mutex
	inFlight, most := 0, 0
	c, queries := serveList(t, func(name string) (int, string) {
		mu.Lock()
		inFlight++
		most = max(most, inFlight)
		mu.Unlock()
		time.Sleep(2 * time.Millisecond)
		mu.Lock()
		inFlight--
		mu.Unlock()
		switch name {
		case "K":
			return http.StatusOK, `[{"UTeamID": 222222, "team_name": "KICKERS", "UDivSeasonID": 110001, "Season": 173, "FacilityID": 4}]`
		case "R":
			return http.StatusOK, `[{"UTeamID": 111111, "team_name": "ROVERS", "UDivSeasonID": 110001, "Season": 173, "FacilityID": 4},
			  {"UTeamID": 333333, "team_name": "ROVERS", "UDivSeasonID": 110002, "Season": 175, "FacilityID": 4}]`
		case "7":
			// A Team listed twice is kept once.
			return http.StatusOK, `[{"UTeamID": 111111, "team_name": "ROVERS", "UDivSeasonID": 110001, "Season": 173, "FacilityID": 4}]`
		}
		return http.StatusOK, `[]`
	})
	teams, err := c.TeamList(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	var ids []string
	for _, l := range teams {
		ids = append(ids, l.ID)
	}
	if !slices.Equal(ids, []string{"222222", "111111", "333333"}) {
		t.Fatalf("Team IDs = %v", ids)
	}

	got := queries()
	if len(got) != 1+len(sweepPrefixes) || got[0] != "name=%25&facility_id=4" {
		t.Fatalf("queries = %q", got)
	}
	var want []string
	for _, p := range sweepPrefixes {
		want = append(want, "name="+string(p)+"&facility_id=4")
	}
	sweep := slices.Sorted(slices.Values(got[1:]))
	if !slices.Equal(sweep, slices.Sorted(slices.Values(want))) {
		t.Fatalf("sweep queries = %q", sweep)
	}
	if most > 4 {
		t.Fatalf("%d searches at once, want at most 4", most)
	}
}

func TestTeamListWithNoTeamsAnywhereIsEmpty(t *testing.T) {
	c, queries := serveList(t, func(string) (int, string) { return http.StatusOK, `[]` })
	teams, err := c.TeamList(context.Background())
	if err != nil || teams == nil || len(teams) != 0 {
		t.Fatalf("teams = %#v, err = %v, want an empty list", teams, err)
	}
	if n := len(queries()); n != 1+len(sweepPrefixes) {
		t.Fatalf("%d queries, want the wildcard and the sweep", n)
	}
}

func TestTeamListErrors(t *testing.T) {
	tests := []struct {
		name   string
		answer func(name string) (int, string)
	}{
		{"server error", func(string) (int, string) { return http.StatusBadGateway, `` }},
		{"unknown route", func(string) (int, string) { return http.StatusNotFound, `{"status":404,"error":"Not Found"}` }},
		{"rate limited", func(string) (int, string) { return http.StatusTooManyRequests, `` }},
		{"maintenance page", func(string) (int, string) { return http.StatusOK, `<html>` }},
		{"not a list", func(string) (int, string) { return http.StatusOK, `{"message":"maintenance"}` }},
		{"unreadable row", func(string) (int, string) { return http.StatusOK, `[{"UTeamID": 111111, "Dropped": "no"}]` }},
		// A sweep missing a letter would hide those Teams for an hour.
		{"sweep fails for one letter", func(name string) (int, string) {
			if name == "Q" {
				return http.StatusServiceUnavailable, ``
			}
			return http.StatusOK, `[]`
		}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			c, _ := serveList(t, tt.answer)
			if _, err := c.TeamList(context.Background()); !errors.Is(err, ErrUnavailable) {
				t.Fatalf("err = %v, want ErrUnavailable", err)
			}
		})
	}

	c, _ := serveList(t, func(string) (int, string) { return http.StatusOK, sampleList })
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := c.TeamList(ctx); !errors.Is(err, context.Canceled) || errors.Is(err, ErrUnavailable) {
		t.Fatalf("canceled: err = %v", err)
	}
}

// Whatever a person types is searched in memory. "%", "_" and "[ ]" are
// wildcards to the league, so only the fixed Team list search reaches it.
func TestSearchNeverSendsTypedText(t *testing.T) {
	c, queries := serveList(t, func(string) (int, string) { return http.StatusOK, sampleList })
	d := NewSearchDirectory(c, nil)
	for _, q := range []string{"%", "_", "[a-z]", "ro%", "k_ckers"} {
		got, err := d.SearchTeams(context.Background(), q)
		if err != nil {
			t.Fatal(err)
		}
		if len(got) != 0 {
			t.Errorf("search %q = %+v, want no Teams: no name holds it", q, got)
		}
	}
	if got := queries(); len(got) != 1 || got[0] != "name=%25&facility_id=4" {
		t.Fatalf("queries = %q", got)
	}
}
