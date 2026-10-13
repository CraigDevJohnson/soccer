package league

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// sampleDivision is the league's live shape for a division season
// (2026-10-13), trimmed and made up: estimated dates 100 years early, game
// length in minutes, and games, Teams and standings that are not read.
const sampleDivision = `{
  "games": [{"UGameID": 1, "SchedGameDateTime": "2026-09-27T12:50:00.000Z"}],
  "player_standings": [], "teams": [], "division_name": "Coed Open Sun",
  "division_season": {"DivSeasonID": null, "DivisionID": 20001, "Season": 173, "GameLength": 45, "FacilityID": 4,
    "UDivSeasonID": 110001, "NumGames": 8, "LastGameDate": null, "FirstGameDate": null,
    "est_start_date": "1926-09-21T00:00:00.000Z", "est_end_date": "1926-11-08T00:00:00.000Z", "season_id": 6001},
  "win_point": 3, "tie_point": 1, "loss_point": 0, "goal_point": 0
}`

func serveDivision(t *testing.T, status int, body string) (*Client, *int) {
	t.Helper()
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.Method != http.MethodGet || r.URL.Path != "/division_seasons/110001" {
			t.Errorf("%s %s", r.Method, r.URL.Path)
		}
		if r.Header.Get("Cookie") != "" || r.Header.Get("Authorization") != "" {
			t.Errorf("credentials sent to the league")
		}
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	return NewClient(srv.URL), &calls
}

func TestDivisionSeasonReadsTheLeaguesFacts(t *testing.T) {
	c, _ := serveDivision(t, http.StatusOK, sampleDivision)
	ds, err := c.DivisionSeason(context.Background(), " 110001 ")
	if err != nil {
		t.Fatal(err)
	}
	want := DivisionSeason{ID: "110001", Season: "173", GameLength: 45 * time.Minute, NumGames: 8}
	if ds != want {
		t.Fatalf("got %+v, want %+v", ds, want)
	}
}

func TestDivisionSeasonOneDay(t *testing.T) {
	tests := []struct {
		name       string
		start, end string
		want       bool
	}{
		{"weekly", `"1926-09-21T00:00:00.000Z"`, `"1926-11-08T00:00:00.000Z"`, false},
		// A Saturday tournament: the same day, whatever the time.
		{"one day", `"1926-09-26T00:00:00.000Z"`, `"1926-09-26T18:00:00.000Z"`, true},
		{"no dates", `null`, `null`, false},
		{"no end", `"1926-09-26T00:00:00.000Z"`, `null`, false},
		{"unreadable", `"soon"`, `"soon"`, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			c, _ := serveDivision(t, http.StatusOK, `{"division_season": {"UDivSeasonID": 110001, "Season": 174, "GameLength": 35, "NumGames": 3,
			  "est_start_date": `+tt.start+`, "est_end_date": `+tt.end+`}}`)
			ds, err := c.DivisionSeason(context.Background(), "110001")
			if err != nil {
				t.Fatal(err)
			}
			if ds.OneDay != tt.want || ds.GameLength != 35*time.Minute {
				t.Fatalf("got %+v, want OneDay %v", ds, tt.want)
			}
		})
	}
}

// A division season without a game length or a game count leaves them
// zero, for the caller to treat as unknown.
func TestDivisionSeasonWithoutCounts(t *testing.T) {
	c, _ := serveDivision(t, http.StatusOK, `{"division_season": {"UDivSeasonID": "110001", "Season": "173", "GameLength": null, "NumGames": 0}}`)
	ds, err := c.DivisionSeason(context.Background(), "110001")
	if err != nil {
		t.Fatal(err)
	}
	if ds != (DivisionSeason{ID: "110001", Season: "173"}) {
		t.Fatalf("got %+v", ds)
	}
}

func TestDivisionSeasonErrors(t *testing.T) {
	tests := []struct {
		name   string
		status int
		body   string
		want   error
	}{
		{"not found", http.StatusNotFound, `{"message":"Division Season not found"}`, ErrDivisionNotFound},
		{"unknown route", http.StatusNotFound, `{"status":404,"error":"Not Found"}`, ErrUnavailable},
		{"other lookup not found", http.StatusNotFound, `{"message":"team not found"}`, ErrUnavailable},
		{"not found, no body", http.StatusNotFound, ``, ErrUnavailable},
		{"server error", http.StatusBadGateway, ``, ErrUnavailable},
		{"garbage", http.StatusOK, `<html>`, ErrUnavailable},
		{"no division_season object", http.StatusOK, `{"message":"maintenance"}`, ErrUnavailable},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			c, _ := serveDivision(t, tt.status, tt.body)
			if _, err := c.DivisionSeason(context.Background(), "110001"); !errors.Is(err, tt.want) {
				t.Fatalf("err = %v, want %v", err, tt.want)
			}
		})
	}
}

func TestDivisionSeasonRejectsBadIDWithoutCalling(t *testing.T) {
	c, calls := serveDivision(t, http.StatusOK, sampleDivision)
	for _, id := range []string{"", "abc", "110001/teams", "1%2F2", strings.Repeat("1", 13)} {
		if _, err := c.DivisionSeason(context.Background(), id); !errors.Is(err, ErrDivisionNotFound) {
			t.Errorf("%q: err = %v", id, err)
		}
	}
	if *calls != 0 {
		t.Fatalf("league called %d times", *calls)
	}
}

// fakeDivisions counts lookups and fails while fail is set.
type fakeDivisions struct {
	mu    sync.Mutex
	calls map[string]int
	fail  error
}

func (f *fakeDivisions) DivisionSeason(_ context.Context, id string) (DivisionSeason, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.calls == nil {
		f.calls = map[string]int{}
	}
	f.calls[id]++
	if f.fail != nil {
		return DivisionSeason{}, f.fail
	}
	return DivisionSeason{ID: id, Season: "173", GameLength: 45 * time.Minute, NumGames: 8}, nil
}

func (f *fakeDivisions) count(id string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.calls[id]
}

func TestDivisionCacheKeepsFactsForTheTTL(t *testing.T) {
	src := &fakeDivisions{}
	d := NewDivisionCache(src)
	now := time.Date(2026, 10, 13, 9, 0, 0, 0, Mountain)
	d.Now = func() time.Time { return now }
	ctx := context.Background()

	for range 3 {
		if ds, err := d.DivisionSeason(ctx, "110001"); err != nil || ds.NumGames != 8 {
			t.Fatalf("%+v %v", ds, err)
		}
	}
	if _, err := d.DivisionSeason(ctx, "120001"); err != nil {
		t.Fatal(err)
	}
	if src.count("110001") != 1 || src.count("120001") != 1 {
		t.Fatalf("lookups = %v, want one each", src.calls)
	}

	now = now.Add(DivisionTTL)
	if _, err := d.DivisionSeason(ctx, "110001"); err != nil || src.count("110001") != 2 {
		t.Fatalf("not looked up again after the TTL: %v, %d", err, src.count("110001"))
	}

	// A failure is not kept: the next caller asks again.
	src.fail = ErrUnavailable
	if _, err := d.DivisionSeason(ctx, "130001"); !errors.Is(err, ErrUnavailable) {
		t.Fatalf("err = %v", err)
	}
	src.fail = nil
	if _, err := d.DivisionSeason(ctx, "130001"); err != nil || src.count("130001") != 2 {
		t.Fatalf("failure was kept: %v, %d", err, src.count("130001"))
	}
}

func TestDivisionCacheIsSafeForConcurrentUse(t *testing.T) {
	d := NewDivisionCache(&fakeDivisions{})
	var wg sync.WaitGroup
	for i := range 16 {
		wg.Go(func() {
			id := []string{"110001", "120001", "130001"}[i%3]
			if ds, err := d.DivisionSeason(context.Background(), id); err != nil || ds.ID != id {
				t.Errorf("%s: %+v %v", id, ds, err)
			}
		})
	}
	wg.Wait()
}
