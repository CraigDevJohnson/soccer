package league

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
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
		{"not found", http.StatusNotFound, ``, ErrTeamNotFound},
		{"bad request", http.StatusBadRequest, ``, ErrUnavailable},
		{"gone", http.StatusGone, ``, ErrTeamNotFound},
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

func TestUnconnected(t *testing.T) {
	var d Directory = Unconnected{}
	if _, err := d.SearchTeams(context.Background(), "rovers"); !errors.Is(err, ErrNotConnected) {
		t.Fatal(err)
	}
	if _, err := d.Divisions(context.Background()); !errors.Is(err, ErrNotConnected) {
		t.Fatal(err)
	}
	if _, err := d.DivisionTeams(context.Background(), "1"); !errors.Is(err, ErrNotConnected) {
		t.Fatal(err)
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
