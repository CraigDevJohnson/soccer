package league

import (
	"context"
	"errors"
	"log/slog"
	"sort"
	"strings"
	"sync"
	"time"

	"golang.org/x/sync/errgroup"
)

const (
	// ScanDays is how many days of the Facility's schedule the Directory
	// reads: a week, since every division plays weekly.
	ScanDays = 7
	// ScanTTL is how long one scan serves before the next.
	ScanTTL = time.Hour
	// MaxSearchResults caps a name search.
	MaxSearchResults = 50
)

// ScanDirectory is the Directory built from the league's public day
// schedule, since its own team search needs a league account and it has no
// public division list. Reading the coming week lists every division
// playing and every Team in it; a Team with no game in that week is not
// listed. One scan is kept in memory for ScanTTL and served to everyone.
type ScanDirectory struct {
	Days Days
	// Now is the clock; nil means time.Now.
	Now func() time.Time
	// Log, when set, gets a line when a refresh fails and the last scan is
	// served instead.
	Log *slog.Logger

	mu      sync.Mutex
	index   *index
	scanned time.Time
}

// NewScanDirectory returns a Directory over days.
func NewScanDirectory(days Days, log *slog.Logger) *ScanDirectory {
	return &ScanDirectory{Days: days, Log: log}
}

// index is one scan, read-only once built.
type index struct {
	divisions []Division               // sorted by name
	teams     []TeamSummary            // sorted by name
	byDiv     map[string][]TeamSummary // division ID -> Teams, sorted by name
}

func (d *ScanDirectory) SearchTeams(ctx context.Context, query string) ([]TeamSummary, error) {
	idx, err := d.current(ctx)
	if err != nil {
		return nil, err
	}
	q := strings.ToLower(strings.TrimSpace(query))
	var out []TeamSummary
	for _, t := range idx.teams {
		if strings.Contains(strings.ToLower(t.Name), q) || t.ID == q {
			out = append(out, t)
			if len(out) == MaxSearchResults {
				break
			}
		}
	}
	return out, nil
}

func (d *ScanDirectory) Divisions(ctx context.Context) ([]Division, error) {
	idx, err := d.current(ctx)
	if err != nil {
		return nil, err
	}
	return idx.divisions, nil
}

func (d *ScanDirectory) DivisionTeams(ctx context.Context, divisionID string) ([]TeamSummary, error) {
	idx, err := d.current(ctx)
	if err != nil {
		return nil, err
	}
	return idx.byDiv[strings.TrimSpace(divisionID)], nil
}

// current returns the scan on hand, refreshing it when it is older than
// ScanTTL. When a refresh fails, the last scan still serves: a week-old
// listing beats no listing, and the Team lookup itself still tells the
// truth. Concurrent callers wait for one refresh rather than each starting
// their own.
func (d *ScanDirectory) current(ctx context.Context) (*index, error) {
	now := time.Now()
	if d.Now != nil {
		now = d.Now()
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.index != nil && now.Sub(d.scanned) < ScanTTL {
		return d.index, nil
	}
	idx, err := d.scan(ctx, now)
	if err != nil {
		if d.index != nil && !errors.Is(err, context.Canceled) {
			if d.Log != nil {
				d.Log.Warn("directory_stale", "err", err, "scanned", d.scanned)
			}
			return d.index, nil
		}
		return nil, err
	}
	d.index, d.scanned = idx, now
	return idx, nil
}

func (d *ScanDirectory) scan(ctx context.Context, now time.Time) (*index, error) {
	days := make([][]DayGame, ScanDays)
	g, gctx := errgroup.WithContext(ctx)
	g.SetLimit(4)
	start := now.In(Mountain)
	for i := range days {
		date := start.AddDate(0, 0, i)
		g.Go(func() error {
			games, err := d.Days.DayGames(gctx, date)
			if err != nil {
				return err
			}
			days[i] = games
			return nil
		})
	}
	if err := g.Wait(); err != nil {
		return nil, err
	}

	idx := &index{byDiv: map[string][]TeamSummary{}}
	divs := map[string]Division{}
	teams := map[string]TeamSummary{}
	for _, games := range days {
		for _, game := range games {
			for _, t := range []TeamSummary{game.Home, game.Away} {
				if t.Division.ID != "" && t.Division.Name != "" {
					divs[t.Division.ID] = t.Division
				}
				teams[t.ID] = t
			}
		}
	}
	for _, dv := range divs {
		idx.divisions = append(idx.divisions, dv)
	}
	sort.Slice(idx.divisions, func(i, j int) bool { return idx.divisions[i].Name < idx.divisions[j].Name })
	for _, t := range teams {
		idx.teams = append(idx.teams, t)
		idx.byDiv[t.Division.ID] = append(idx.byDiv[t.Division.ID], t)
	}
	byName := func(ts []TeamSummary) {
		sort.Slice(ts, func(i, j int) bool {
			if ts[i].Name != ts[j].Name {
				return ts[i].Name < ts[j].Name
			}
			return ts[i].ID < ts[j].ID
		})
	}
	byName(idx.teams)
	for _, ts := range idx.byDiv {
		byName(ts)
	}
	return idx, nil
}
