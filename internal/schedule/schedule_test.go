package schedule

import (
	"context"
	"errors"
	"strconv"
	"testing"
	"time"

	"github.com/CraigDevJohnson/soccer/internal/league"
)

type fakeTeams map[string]league.Team

func (f fakeTeams) Team(_ context.Context, id string) (league.Team, error) {
	if id == "999999" {
		return league.Team{}, league.ErrUnavailable
	}
	t, ok := f[id]
	if !ok {
		return league.Team{}, league.ErrTeamNotFound
	}
	return t, nil
}

func at(day, hour int) time.Time {
	return time.Date(2026, 10, day, hour, 0, 0, 0, league.Mountain)
}

var teams = fakeTeams{
	"111111": {ID: "111111", Name: "ROVERS", Season: "173", Games: []league.Game{
		{ID: "g1", Start: at(7, 19), Field: "1", Home: "ROVERS", Away: "BANDITS"},
		{ID: "g3", Start: at(14, 19), Field: "2", Home: "ROVERS", Away: "KICKERS"},
	}},
	"222222": {ID: "222222", Name: "KICKERS", Season: "173", Games: []league.Game{
		{ID: "g3", Start: at(14, 19), Field: "2", Home: "ROVERS", Away: "KICKERS"},
		{ID: "g2", Start: at(14, 18), Field: "4", Home: "KICKERS", Away: "OWLS", HomeID: "222222", AwayID: "444444"},
		{ID: "", Start: at(21, 18), Field: "4", Home: "OWLS", Away: "KICKERS"},
	}},
}

var now = at(10, 12)

func TestCombineMergesSortsAndMarksRemaining(t *testing.T) {
	s, err := Combine(context.Background(), teams, []string{"111111", "222222", "111111"}, now)
	if err != nil {
		t.Fatal(err)
	}
	if len(s.Teams) != 2 || len(s.Failed) != 0 {
		t.Fatalf("teams %+v failed %+v", s.Teams, s.Failed)
	}
	var ids []string
	for _, g := range s.Games {
		ids = append(ids, g.ID)
	}
	want := []string{"g1", "g2", "g3", "20261021T1800-4"}
	if len(ids) != len(want) {
		t.Fatalf("games = %v, want %v", ids, want)
	}
	for i := range want {
		if ids[i] != want[i] {
			t.Fatalf("games = %v, want %v", ids, want)
		}
	}
	if s.Games[0].Remaining || !s.Games[1].Remaining {
		t.Fatalf("remaining flags wrong: %+v", s.Games[:2])
	}
	if g := s.Games[1]; g.HomeID != "222222" || g.AwayID != "444444" {
		t.Fatalf("side IDs lost: %+v", g)
	}
	shared := s.Games[2]
	if len(shared.TeamIDs) != 2 {
		t.Fatalf("shared game should name both Teams: %+v", shared)
	}
	if shared.End.Sub(shared.Start) != 45*time.Minute {
		t.Fatalf("game length = %v", shared.End.Sub(shared.Start))
	}
}

func TestCombinePartialFailure(t *testing.T) {
	s, err := Combine(context.Background(), teams, []string{"111111", "333333", "999999"}, now)
	if err != nil {
		t.Fatal(err)
	}
	if len(s.Teams) != 1 || len(s.Failed) != 2 {
		t.Fatalf("teams %+v failed %+v", s.Teams, s.Failed)
	}
	if s.Failed[0] != (Failure{"333333", "not_found"}) || s.Failed[1] != (Failure{"999999", "unavailable"}) {
		t.Fatalf("failed = %+v", s.Failed)
	}
}

func TestCombineNothingRead(t *testing.T) {
	if _, err := Combine(context.Background(), teams, []string{"333333"}, now); !errors.Is(err, ErrNoTeams) {
		t.Fatalf("unknown only: %v", err)
	}
	if _, err := Combine(context.Background(), teams, []string{"333333", "999999"}, now); !errors.Is(err, league.ErrUnavailable) {
		t.Fatalf("league down: %v", err)
	}
}

func TestNormalizeTeamIDs(t *testing.T) {
	var invalid *InvalidError
	for _, in := range [][]string{nil, {"12"}, {"111111", "abc"}} {
		if _, err := NormalizeTeamIDs(in); !errors.As(err, &invalid) {
			t.Errorf("%v: err = %v", in, err)
		}
	}
	many := make([]string, MaxTeams+1)
	for i := range many {
		many[i] = strconv.Itoa(100000 + i)
	}
	if _, err := NormalizeTeamIDs(many); !errors.As(err, &invalid) {
		t.Errorf("%d teams accepted", len(many))
	}
}

func TestSelectionChosen(t *testing.T) {
	s, err := Combine(context.Background(), teams, []string{"111111", "222222"}, now)
	if err != nil {
		t.Fatal(err)
	}
	ids := func(gs []Game) []string {
		out := []string{}
		for _, g := range gs {
			out = append(out, g.ID)
		}
		return out
	}

	// The default: remaining games only.
	got := ids(Selection{Since: now}.Chosen(s.Games))
	if len(got) != 3 || got[0] != "g2" {
		t.Fatalf("default = %v", got)
	}
	// Untick a remaining game and tick a played one.
	got = ids(Selection{Since: now, Excluded: []string{"g3"}, Included: []string{"g1"}}.Chosen(s.Games))
	if len(got) != 3 || got[0] != "g1" || got[1] != "g2" || got[2] != "20261021T1800-4" {
		t.Fatalf("changed = %v", got)
	}
}
