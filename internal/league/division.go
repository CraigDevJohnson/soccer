package league

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

// ErrDivisionNotFound means the league has no division season with that ID.
var ErrDivisionNotFound = errors.New("division season not found")

// DivisionTTL is how long DivisionCache keeps a division's facts. They are
// fixed for a Session, so a few hours only bounds how stale a correction
// can be.
const DivisionTTL = 6 * time.Hour

// DivisionSeason is what the league says about one division for one
// Session. GameLength is zero and NumGames zero when the league leaves them
// out. OneDay marks a one-day event, such as a Saturday tournament, whose
// Teams are not anyone's next Session.
type DivisionSeason struct {
	ID         string
	Season     string
	GameLength time.Duration
	NumGames   int
	OneDay     bool
}

// DivisionSeasons looks a division season up by its UDivSeasonID.
type DivisionSeasons interface {
	DivisionSeason(ctx context.Context, id string) (DivisionSeason, error)
}

// divisionResponse is the league's answer for GET /division_seasons/{id},
// as seen live on 2026-10-13. The answer also lists the division's games,
// Teams and standings, which are not read. GameLength is in minutes and
// NumGames is the games each Team plays. The estimated dates are stored 100
// years early ("1926-09-21T00:00:00.000Z"), so only their date parts are
// compared, and only with each other.
type divisionResponse struct {
	DivisionSeason *struct {
		UDivSeasonID flexString `json:"UDivSeasonID"`
		Season       flexString `json:"Season"`
		GameLength   flexString `json:"GameLength"`
		NumGames     flexString `json:"NumGames"`
		EstStartDate string     `json:"est_start_date"`
		EstEndDate   string     `json:"est_end_date"`
	} `json:"division_season"`
}

var divisionIDPattern = regexp.MustCompile(`^[0-9]{1,12}$`)

// DivisionSeason reads one division season's facts. An unknown ID is
// ErrDivisionNotFound only when the league says so in its own words; any
// other failure is ErrUnavailable.
func (c *Client) DivisionSeason(ctx context.Context, id string) (DivisionSeason, error) {
	id = strings.TrimSpace(id)
	if !divisionIDPattern.MatchString(id) {
		return DivisionSeason{}, fmt.Errorf("%w: %q is not a division season ID", ErrDivisionNotFound, id)
	}
	resp, err := c.get(ctx, "/division_seasons/"+id)
	if err != nil {
		return DivisionSeason{}, err
	}
	defer resp.Body.Close()
	switch {
	case resp.StatusCode == http.StatusNotFound, resp.StatusCode == http.StatusGone:
		if saysMessage(resp.Body, "division season not found") {
			return DivisionSeason{}, fmt.Errorf("%w: %s", ErrDivisionNotFound, id)
		}
		return DivisionSeason{}, fmt.Errorf("%w: division season %s: status %d without a not found message", ErrUnavailable, id, resp.StatusCode)
	case resp.StatusCode != http.StatusOK:
		return DivisionSeason{}, fmt.Errorf("%w: division season %s: status %d", ErrUnavailable, id, resp.StatusCode)
	}

	var body divisionResponse
	if err := json.NewDecoder(io.LimitReader(resp.Body, 8<<20)).Decode(&body); err != nil {
		return DivisionSeason{}, fmt.Errorf("%w: decoding division season %s: %v", ErrUnavailable, id, err)
	}
	if body.DivisionSeason == nil {
		return DivisionSeason{}, fmt.Errorf("%w: division season %s: no division_season object in the answer", ErrUnavailable, id)
	}
	b := body.DivisionSeason
	ds := DivisionSeason{ID: strings.TrimSpace(string(b.UDivSeasonID))}
	if ds.ID == "" {
		ds.ID = id
	}
	if s := strings.TrimSpace(string(b.Season)); s != "0" {
		ds.Season = s
	}
	if m, err := strconv.Atoi(strings.TrimSpace(string(b.GameLength))); err == nil && m > 0 {
		ds.GameLength = time.Duration(m) * time.Minute
	}
	if n, err := strconv.Atoi(strings.TrimSpace(string(b.NumGames))); err == nil && n > 0 {
		ds.NumGames = n
	}
	start, end := datePart(b.EstStartDate), datePart(b.EstEndDate)
	ds.OneDay = start != "" && start == end
	return ds, nil
}

// datePart returns the YYYY-MM-DD at the start of a league date, or "" when
// there is none.
func datePart(s string) string {
	s = strings.TrimSpace(s)
	if len(s) < 10 {
		return ""
	}
	if _, err := time.Parse("2006-01-02", s[:10]); err != nil {
		return ""
	}
	return s[:10]
}

// DivisionCache keeps each division season's facts for DivisionTTL, so a
// calendar app fetching a Session link, or a person finding a next Session,
// does not ask the league again. Failures are not kept. It is safe for
// concurrent use.
type DivisionCache struct {
	Source DivisionSeasons
	// Now is the clock; nil means time.Now.
	Now func() time.Time

	mu      sync.Mutex
	entries map[string]cachedDivision
}

type cachedDivision struct {
	ds DivisionSeason
	at time.Time
}

// NewDivisionCache returns a DivisionCache over src.
func NewDivisionCache(src DivisionSeasons) *DivisionCache {
	return &DivisionCache{Source: src}
}

func (d *DivisionCache) DivisionSeason(ctx context.Context, id string) (DivisionSeason, error) {
	id = strings.TrimSpace(id)
	now := time.Now()
	if d.Now != nil {
		now = d.Now()
	}
	d.mu.Lock()
	e, ok := d.entries[id]
	d.mu.Unlock()
	if ok && now.Sub(e.at) < DivisionTTL {
		return e.ds, nil
	}

	// Two callers may look the same division up at once; both answers are
	// the same, so neither waits for the other.
	ds, err := d.Source.DivisionSeason(ctx, id)
	if err != nil {
		return DivisionSeason{}, err
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.entries == nil {
		d.entries = map[string]cachedDivision{}
	}
	for k, e := range d.entries {
		if now.Sub(e.at) >= DivisionTTL {
			delete(d.entries, k)
		}
	}
	d.entries[id] = cachedDivision{ds: ds, at: now}
	return ds, nil
}
