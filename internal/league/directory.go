package league

import (
	"cmp"
	"context"
	"errors"
	"log/slog"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	// ListTTL is how long one Team list serves before the next is read.
	ListTTL = time.Hour
	// EmptyListTTL is how long an empty Team list waits before the next
	// read. Reading one can take 37 requests: the wildcard search, then one
	// by each first letter or digit.
	EmptyListTTL = 5 * time.Minute
	// MaxSearchResults caps a name search.
	MaxSearchResults = 50
)

// SearchDirectory is the Directory built from the league's Team list, which
// holds every Team at the Facility in the current Session and the next. One
// list is kept in memory for ListTTL and served to everyone; searches run
// over it, so nothing a person types is sent to the league.
type SearchDirectory struct {
	Lists TeamLists
	// Now is the clock; nil means time.Now.
	Now func() time.Time
	// Log, when set, gets a line when a refresh fails and the last list is
	// served instead.
	Log *slog.Logger

	mu     sync.Mutex
	index  *index
	listed time.Time // when the list on hand was read
	next   time.Time // when it is read again
}

// NewSearchDirectory returns a Directory over lists.
func NewSearchDirectory(lists TeamLists, log *slog.Logger) *SearchDirectory {
	return &SearchDirectory{Lists: lists, Log: log}
}

// index is one Team list, read-only once built.
type index struct {
	listed    []Listed                 // as the league listed them
	divisions []Division               // sorted by Season, then name
	teams     []TeamSummary            // sorted by name, then Season, then Team ID
	byDiv     map[string][]TeamSummary // division ID -> Teams, sorted the same
}

func (d *SearchDirectory) SearchTeams(ctx context.Context, query string) ([]TeamSummary, error) {
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

func (d *SearchDirectory) Divisions(ctx context.Context) ([]Division, error) {
	idx, err := d.current(ctx)
	if err != nil {
		return nil, err
	}
	return idx.divisions, nil
}

func (d *SearchDirectory) DivisionTeams(ctx context.Context, divisionID string) ([]TeamSummary, error) {
	idx, err := d.current(ctx)
	if err != nil {
		return nil, err
	}
	return idx.byDiv[strings.TrimSpace(divisionID)], nil
}

// Listed returns the whole Team list, coach ids included, for finding a
// Team's next Session. It is shared: callers must not change it.
func (d *SearchDirectory) Listed(ctx context.Context) ([]Listed, error) {
	idx, err := d.current(ctx)
	if err != nil {
		return nil, err
	}
	return idx.listed, nil
}

// current returns the list on hand, refreshing it when it is older than
// ListTTL. When a refresh fails, the last list still serves: an hour-old
// listing beats no listing, and the Team lookup itself still tells the
// truth. Concurrent callers wait for one refresh rather than each starting
// their own.
//
// The league lists the current Session's Teams, so an empty list is taken
// as the league failing without saying so: the last list keeps serving, or
// with none on hand the empty one serves for EmptyListTTL only. Either way
// a warning line says so, since every search would otherwise come back
// empty for an hour with nothing in the logs.
func (d *SearchDirectory) current(ctx context.Context) (*index, error) {
	now := time.Now()
	if d.Now != nil {
		now = d.Now()
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.index != nil && now.Before(d.next) {
		return d.index, nil
	}
	listed, err := d.Lists.TeamList(ctx)
	switch {
	case err != nil:
		if d.index != nil && !errors.Is(err, context.Canceled) {
			d.warn("directory_stale", "err", err, "listed", d.listed)
			return d.index, nil
		}
		return nil, err
	case len(listed) == 0:
		d.next = now.Add(EmptyListTTL)
		if d.index != nil && len(d.index.listed) > 0 {
			d.warn("directory_stale", "err", "the league listed no Teams", "listed", d.listed)
			return d.index, nil
		}
		d.warn("team_list_empty")
		d.index, d.listed = build(listed), now
		return d.index, nil
	}
	d.index, d.listed, d.next = build(listed), now, now.Add(ListTTL)
	return d.index, nil
}

func (d *SearchDirectory) warn(msg string, args ...any) {
	if d.Log != nil {
		d.Log.Warn(msg, args...)
	}
}

// build indexes one Team list. A Team that dropped out is left out of the
// search and the divisions, and a division needs an ID and a name to be
// listed.
func build(listed []Listed) *index {
	idx := &index{listed: listed, byDiv: map[string][]TeamSummary{}}
	divs := map[string]Division{}
	for _, l := range listed {
		if l.Dropped {
			continue
		}
		t := l.TeamSummary
		t.Name = strings.TrimSpace(t.Name)
		if dv := t.Division; dv.ID != "" && dv.Name != "" {
			if _, ok := divs[dv.ID]; !ok {
				divs[dv.ID] = dv
				idx.divisions = append(idx.divisions, dv)
			}
		}
		idx.teams = append(idx.teams, t)
		idx.byDiv[t.Division.ID] = append(idx.byDiv[t.Division.ID], t)
	}
	slices.SortFunc(idx.divisions, func(a, b Division) int {
		return cmp.Or(compareSeasons(a.Season, b.Season), strings.Compare(a.Name, b.Name), strings.Compare(a.ID, b.ID))
	})
	byName := func(a, b TeamSummary) int {
		return cmp.Or(strings.Compare(a.Name, b.Name), compareSeasons(a.Season, b.Season), strings.Compare(a.ID, b.ID))
	}
	slices.SortFunc(idx.teams, byName)
	for _, ts := range idx.byDiv {
		slices.SortFunc(ts, byName)
	}
	return idx
}

// compareSeasons orders league season numbers as numbers, so Season 99
// comes before Season 100. Anything that is not a number sorts after them.
func compareSeasons(a, b string) int {
	an, aerr := strconv.Atoi(a)
	bn, berr := strconv.Atoi(b)
	switch {
	case aerr == nil && berr == nil:
		return cmp.Compare(an, bn)
	case aerr == nil:
		return -1
	case berr == nil:
		return 1
	}
	return strings.Compare(a, b)
}
