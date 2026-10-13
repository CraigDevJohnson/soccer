package league

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"
)

var (
	open173   = Division{ID: "110001", Name: "Coed Open Sun", Code: "COSun", Season: "173"}
	mens173   = Division{ID: "120001", Name: "Mens Open Tues", Code: "MOTues", Season: "173"}
	open175   = Division{ID: "110002", Name: "Coed Open Sun", Code: "COSun", Season: "175"}
	rovers    = TeamSummary{ID: "111111", Name: "ROVERS", Division: open173, Season: "173"}
	kickers   = TeamSummary{ID: "222222", Name: "KICKERS", Division: open173, Season: "173"}
	bandits   = TeamSummary{ID: "333333", Name: "BANDITS", Division: mens173, Season: "173"}
	rovers175 = TeamSummary{ID: "100000", Name: "ROVERS", Division: open175, Season: "175"} // a lower Team ID, a later Session
	quitters  = TeamSummary{ID: "444444", Name: "QUITTERS", Division: mens173, Season: "173"}
)

// fakeLists serves a Team list of this Session and the next, in no order.
type fakeLists struct {
	mu    sync.Mutex
	calls int
	fail  error
	delay time.Duration
	teams []Listed
}

func (f *fakeLists) TeamList(context.Context) ([]Listed, error) {
	time.Sleep(f.delay)
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	if f.fail != nil {
		return nil, f.fail
	}
	if f.teams != nil {
		return f.teams, nil
	}
	return []Listed{
		{TeamSummary: rovers175, CoachID: "900001"},
		{TeamSummary: kickers, CoachID: "900002"},
		{TeamSummary: rovers, CoachID: "900001"},
		{TeamSummary: bandits, CoachID: "900003"},
		{TeamSummary: quitters, CoachID: "900004", Dropped: true},
	}, nil
}

func (f *fakeLists) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.calls
}

func newDir(lists *fakeLists) (*SearchDirectory, *time.Time) {
	now := time.Date(2026, 10, 13, 9, 0, 0, 0, Mountain)
	d := NewSearchDirectory(lists, nil)
	d.Now = func() time.Time { return now }
	return d, &now
}

func TestDirectoryListsDivisionsAndTeams(t *testing.T) {
	lists := &fakeLists{}
	d, _ := newDir(lists)
	ctx := context.Background()

	// One entry per division season, by Session and then name.
	divs, err := d.Divisions(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if want := []Division{open173, mens173, open175}; !slices.Equal(divs, want) {
		t.Fatalf("divisions = %+v, want %+v", divs, want)
	}

	for id, want := range map[string][]TeamSummary{
		open173.ID:         {kickers, rovers},
		" " + open175.ID:   {rovers175},
		mens173.ID:         {bandits}, // not the Team that dropped out
		"nope":             nil,
		"%":                nil,
		open173.ID + "%25": nil,
	} {
		got, err := d.DivisionTeams(ctx, id)
		if err != nil {
			t.Fatal(err)
		}
		if !slices.Equal(got, want) {
			t.Errorf("division %q = %+v, want %+v", id, got, want)
		}
	}

	for q, want := range map[string][]TeamSummary{
		"rovers":  {rovers, rovers175}, // the same name: earlier Session first
		" Kick ":  {kickers},
		"ERS":     {kickers, rovers, rovers175},
		"222222":  {kickers},
		"22222":   nil, // a Team ID matches whole only
		"quitter": nil,
		"xyz":     nil,
	} {
		got, err := d.SearchTeams(ctx, q)
		if err != nil {
			t.Fatal(err)
		}
		if !slices.Equal(got, want) {
			t.Errorf("search %q = %+v, want %+v", q, got, want)
		}
	}

	// The resolver gets the whole list, coach ids and all.
	listed, err := d.Listed(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(listed) != 5 || listed[0].CoachID != "900001" || !listed[4].Dropped {
		t.Fatalf("listed = %+v", listed)
	}
	if n := lists.count(); n != 1 {
		t.Fatalf("list read %d times, want once", n)
	}
}

func TestDirectorySearchIsCapped(t *testing.T) {
	var teams []Listed
	for i := range MaxSearchResults + 10 {
		teams = append(teams, Listed{TeamSummary: TeamSummary{ID: fmt.Sprint(500000 + i), Name: fmt.Sprintf("TEAM %02d", i), Division: open173, Season: "173"}})
	}
	d, _ := newDir(&fakeLists{teams: teams})
	got, err := d.SearchTeams(context.Background(), "team")
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != MaxSearchResults || got[0].Name != "TEAM 00" || got[MaxSearchResults-1].Name != "TEAM 49" {
		t.Fatalf("got %d Teams, %+v first", len(got), got[0])
	}
}

func TestDirectoryRefreshesAfterTTLAndServesStaleOnFailure(t *testing.T) {
	lists := &fakeLists{}
	d, now := newDir(lists)
	var logs bytes.Buffer
	d.Log = slog.New(slog.NewJSONHandler(&logs, nil))
	ctx := context.Background()
	if _, err := d.Divisions(ctx); err != nil {
		t.Fatal(err)
	}
	*now = now.Add(ListTTL - time.Minute)
	if _, err := d.Divisions(ctx); err != nil || lists.count() != 1 {
		t.Fatalf("refreshed early: %v, %d calls", err, lists.count())
	}

	*now = now.Add(2 * time.Minute)
	lists.fail = ErrUnavailable
	divs, err := d.Divisions(ctx)
	if err != nil || len(divs) != 3 {
		t.Fatalf("stale list not served: %v %v", divs, err)
	}
	if lists.count() != 2 {
		t.Fatalf("no refresh tried: %d calls", lists.count())
	}
	if !strings.Contains(logs.String(), `"msg":"directory_stale"`) {
		t.Fatalf("no directory_stale line: %s", logs.String())
	}

	lists.fail = nil
	if _, err := d.Divisions(ctx); err != nil || lists.count() != 3 {
		t.Fatalf("failed refresh was not retried: %v, %d calls", err, lists.count())
	}
}

// An empty Team list is the league failing without saying so: the last list
// keeps serving, and the league is asked again after EmptyListTTL, not on
// every call and not after a whole hour.
func TestDirectoryKeepsItsListWhenTheLeagueListsNoTeams(t *testing.T) {
	lists := &fakeLists{}
	d, now := newDir(lists)
	var logs bytes.Buffer
	d.Log = slog.New(slog.NewJSONHandler(&logs, nil))
	ctx := context.Background()
	if _, err := d.Divisions(ctx); err != nil {
		t.Fatal(err)
	}

	*now = now.Add(ListTTL)
	lists.teams = []Listed{}
	for range 2 {
		got, err := d.SearchTeams(ctx, "rovers")
		if err != nil || len(got) != 2 {
			t.Fatalf("last list not served: %+v %v", got, err)
		}
	}
	if lists.count() != 2 || !strings.Contains(logs.String(), `"msg":"directory_stale"`) {
		t.Fatalf("%d calls, logs: %s", lists.count(), logs.String())
	}

	*now = now.Add(EmptyListTTL)
	lists.teams = nil
	if _, err := d.SearchTeams(ctx, "rovers"); err != nil || lists.count() != 3 {
		t.Fatalf("not asked again after EmptyListTTL: %v, %d calls", err, lists.count())
	}
}

// With no list on hand, an empty one serves, but only for EmptyListTTL and
// with a warning line.
func TestDirectoryHoldsAnEmptyListBriefly(t *testing.T) {
	lists := &fakeLists{teams: []Listed{}}
	d, now := newDir(lists)
	var logs bytes.Buffer
	d.Log = slog.New(slog.NewJSONHandler(&logs, nil))
	ctx := context.Background()
	for range 2 {
		got, err := d.SearchTeams(ctx, "rovers")
		if err != nil || len(got) != 0 {
			t.Fatalf("%+v %v", got, err)
		}
	}
	if lists.count() != 1 || !strings.Contains(logs.String(), `"msg":"team_list_empty"`) {
		t.Fatalf("%d calls, logs: %s", lists.count(), logs.String())
	}

	*now = now.Add(EmptyListTTL)
	lists.teams = nil
	got, err := d.SearchTeams(ctx, "rovers")
	if err != nil || len(got) != 2 || lists.count() != 2 {
		t.Fatalf("empty list kept too long: %+v %v, %d calls", got, err, lists.count())
	}
}

func TestDirectoryWithNothingOnHandIsUnavailable(t *testing.T) {
	d, _ := newDir(&fakeLists{fail: ErrUnavailable})
	if _, err := d.SearchTeams(context.Background(), "rovers"); !errors.Is(err, ErrUnavailable) {
		t.Fatalf("err = %v", err)
	}
	if _, err := d.Listed(context.Background()); !errors.Is(err, ErrUnavailable) {
		t.Fatalf("err = %v", err)
	}
	d, _ = newDir(&fakeLists{fail: context.Canceled})
	if _, err := d.Divisions(context.Background()); !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v", err)
	}
}

func TestDirectoryCallersShareOneRefresh(t *testing.T) {
	lists := &fakeLists{delay: 20 * time.Millisecond}
	d, _ := newDir(lists)
	var wg sync.WaitGroup
	for range 8 {
		wg.Go(func() {
			if _, err := d.SearchTeams(context.Background(), "rovers"); err != nil {
				t.Error(err)
			}
		})
	}
	wg.Wait()
	if n := lists.count(); n != 1 {
		t.Fatalf("list read %d times, want once", n)
	}
}

func TestCompareSeasons(t *testing.T) {
	for _, tt := range []struct {
		a, b string
		want int
	}{
		{"173", "175", -1},
		{"99", "100", -1},
		{"175", "175", 0},
		{"173", "", -1},
		{"", "173", 1},
		{"2026 Fall", "2026 Spring", -1},
	} {
		if got := compareSeasons(tt.a, tt.b); got != tt.want {
			t.Errorf("compareSeasons(%q, %q) = %d, want %d", tt.a, tt.b, got, tt.want)
		}
	}
}
