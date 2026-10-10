package league

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

const sampleTeam = `{
  "team": {"Season": 2026, "team_name": "ROVERS"},
  "games": [
    {"game_id": "g1", "SchedGameDateTime": "2026-10-14T19:15:00Z", "field_name": "Field 3", "Field": 3,
     "home_team": {"team_name": "ROVERS"}, "visitor_team": {"team_name": "KICKERS"}},
    {"game_id": "g2", "SchedGameDateTime": "2026-10-21T20:00:00+00:00", "field_name": "", "Field": 2,
     "home_team": {"team_name": "BANDITS"}, "visitor_team": {"team_name": "ROVERS"}},
    {"game_id": "bad-time", "SchedGameDateTime": "not a time", "Field": 1,
     "home_team": {"team_name": "A"}, "visitor_team": {"team_name": "B"}},
    {"game_id": "no-field", "SchedGameDateTime": "2026-10-28T20:00:00Z", "Field": 0,
     "home_team": {"team_name": "A"}, "visitor_team": {"team_name": "B"}},
    {"game_id": "no-team", "SchedGameDateTime": "2026-10-28T20:00:00Z", "Field": 1,
     "home_team": {"team_name": ""}, "visitor_team": {"team_name": "B"}}
  ]
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
	if len(team.Games) != 2 {
		t.Fatalf("got %d games, want 2 (bad rows skipped): %+v", len(team.Games), team.Games)
	}
	g := team.Games[0]
	want := time.Date(2026, 10, 14, 19, 15, 0, 0, Mountain)
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

func TestTeamErrors(t *testing.T) {
	tests := []struct {
		name   string
		status int
		body   string
		want   error
	}{
		{"not found", http.StatusNotFound, ``, ErrTeamNotFound},
		{"bad request", http.StatusBadRequest, ``, ErrTeamNotFound},
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
	for _, id := range []string{"", "12345", "1234567", "abcdef", "012345", "12/456"} {
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
