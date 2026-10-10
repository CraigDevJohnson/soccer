package league

import (
	"context"
	"errors"
	"sort"
	"sync"
	"testing"
	"time"
)

var (
	coed8 = Division{ID: "114434", Name: "Coed 8 Sun", Code: "C8Sun"}
	mens3 = Division{ID: "114527", Name: "Mens 3 Tues", Code: "M3Tues"}
	huff  = TeamSummary{ID: "489605", Name: "HUFF N PUFF", Division: coed8}
	ballz = TeamSummary{ID: "490345", Name: "NO BALLZ ALL BALLERZ", Division: coed8}
	squad = TeamSummary{ID: "490100", Name: "SQUAD", Division: mens3}
	ez    = TeamSummary{ID: "490101", Name: "ITS TOO EZ", Division: mens3}
)

// fakeDays serves a week where Sunday and Tuesday have games.
type fakeDays struct {
	mu    sync.Mutex
	calls []string
	fail  error
}

func (f *fakeDays) DayGames(ctx context.Context, date time.Time) ([]DayGame, error) {
	f.mu.Lock()
	f.calls = append(f.calls, date.Format("2006-01-02"))
	f.mu.Unlock()
	if f.fail != nil {
		return nil, f.fail
	}
	switch date.Weekday() {
	case time.Sunday:
		return []DayGame{{ID: "1", Start: date, Field: "1", Home: huff, Away: ballz}}, nil
	case time.Tuesday:
		return []DayGame{
			{ID: "2", Start: date, Field: "1", Home: squad, Away: ez},
			{ID: "3", Start: date, Field: "2", Home: ez, Away: squad}, // the same Teams again
		}, nil
	}
	return []DayGame{}, nil
}

func newDir(days *fakeDays) (*ScanDirectory, *time.Time) {
	now := time.Date(2026, 10, 10, 22, 0, 0, 0, Mountain) // a Saturday evening
	d := NewScanDirectory(days, nil)
	d.Now = func() time.Time { return now }
	return d, &now
}

func TestScanListsDivisionsAndTeams(t *testing.T) {
	days := &fakeDays{}
	d, _ := newDir(days)
	ctx := context.Background()

	divs, err := d.Divisions(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(divs) != 2 || divs[0] != coed8 || divs[1] != mens3 {
		t.Fatalf("divisions = %+v", divs)
	}
	scanned := append([]string(nil), days.calls...)
	sort.Strings(scanned)
	if len(scanned) != ScanDays || scanned[0] != "2026-10-10" || scanned[ScanDays-1] != "2026-10-16" {
		t.Fatalf("scanned %v", scanned)
	}

	teams, err := d.DivisionTeams(ctx, mens3.ID)
	if err != nil {
		t.Fatal(err)
	}
	if len(teams) != 2 || teams[0] != ez || teams[1] != squad {
		t.Fatalf("Mens 3 = %+v", teams)
	}
	if teams, _ := d.DivisionTeams(ctx, "nope"); len(teams) != 0 {
		t.Fatalf("unknown division listed %+v", teams)
	}

	for q, want := range map[string][]TeamSummary{
		"ballz":  {ballz},
		" Squad": {squad},
		"490101": {ez},
		"xyz":    nil,
	} {
		got, err := d.SearchTeams(ctx, q)
		if err != nil {
			t.Fatal(err)
		}
		if len(got) != len(want) || (len(want) > 0 && got[0] != want[0]) {
			t.Errorf("search %q = %+v, want %+v", q, got, want)
		}
	}
	if len(days.calls) != ScanDays {
		t.Fatalf("rescanned: %d calls", len(days.calls))
	}
}

func TestScanRefreshesAfterTTLAndServesStaleOnFailure(t *testing.T) {
	days := &fakeDays{}
	d, now := newDir(days)
	ctx := context.Background()
	if _, err := d.Divisions(ctx); err != nil {
		t.Fatal(err)
	}
	*now = now.Add(ScanTTL + time.Minute)
	days.fail = ErrUnavailable
	divs, err := d.Divisions(ctx)
	if err != nil || len(divs) != 2 {
		t.Fatalf("stale scan not served: %v %v", divs, err)
	}
	if len(days.calls) <= ScanDays {
		t.Fatalf("no refresh tried: %d calls", len(days.calls))
	}
	days.fail = nil
	before := len(days.calls)
	if _, err := d.Divisions(ctx); err != nil || len(days.calls) == before {
		t.Fatalf("failed refresh was not retried: %v, %d calls", err, len(days.calls))
	}
}

func TestScanWithNothingOnHandIsUnavailable(t *testing.T) {
	d, _ := newDir(&fakeDays{fail: ErrUnavailable})
	if _, err := d.SearchTeams(context.Background(), "huff"); !errors.Is(err, ErrUnavailable) {
		t.Fatalf("err = %v", err)
	}
	d, _ = newDir(&fakeDays{fail: context.Canceled})
	if _, err := d.Divisions(context.Background()); !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v", err)
	}
}
