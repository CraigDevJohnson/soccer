package next

import (
	"context"
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"sync"
	"testing"

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

type fakeLists struct {
	teams []league.Listed
	fail  error
	calls int
}

func (f *fakeLists) Listed(context.Context) ([]league.Listed, error) {
	f.calls++
	return f.teams, f.fail
}

// fakeDivisions knows every division in the test lists; the ones in oneDay
// are one-day events. It records each lookup.
type fakeDivisions struct {
	oneDay []string
	fail   map[string]error
	mu     sync.Mutex
	asked  []string
}

func (f *fakeDivisions) DivisionSeason(_ context.Context, id string) (league.DivisionSeason, error) {
	f.mu.Lock()
	f.asked = append(f.asked, id)
	f.mu.Unlock()
	if err := f.fail[id]; err != nil {
		return league.DivisionSeason{}, err
	}
	return league.DivisionSeason{ID: id, NumGames: 8, OneDay: slices.Contains(f.oneDay, id)}, nil
}

var (
	open173 = league.Division{ID: "110001", Name: "Coed Open Sun", Code: "COSun"}
	cup174  = league.Division{ID: "140001", Name: "Coed Cup Sat", Code: "CCSat", Season: "174"}
	open175 = league.Division{ID: "150001", Name: "Coed Open Sun", Code: "COSun", Season: "175"}
	mens175 = league.Division{ID: "150002", Name: "Mens Open Tues", Code: "MOTues", Season: "175"}
	open177 = league.Division{ID: "170001", Name: "Coed Open Sun", Code: "COSun", Season: "177"}
)

// old is the Team a Session link was made for: ROVERS in Session 173.
var old = league.Team{ID: "111111", Name: "ROVERS", Division: open173, Season: "173", CoachID: "900001", FacilityID: "4"}

func listed(id, name string, div league.Division, coach string) league.Listed {
	return league.Listed{TeamSummary: league.TeamSummary{ID: id, Name: name, Division: div, Season: div.Season}, CoachID: coach}
}

func TestFind(t *testing.T) {
	type team struct {
		id        string
		sameCoach bool
	}
	tests := []struct {
		name    string
		old     league.Team
		teams   []league.Listed
		oneDay  []string
		match   string
		want    []team
		similar []string // only when match is none
		asked   []string // division lookups, when it matters
	}{
		{
			name: "same name and coach",
			teams: []league.Listed{
				listed("211111", "ROVERS", open175, "900001"),
				listed("222222", "KICKERS", open175, "900002"),
			},
			match: Follow, want: []team{{"211111", true}},
		},
		{
			name:  "name compared trimmed and ignoring case",
			teams: []league.Listed{listed("211111", " Rovers ", open175, "900001")},
			match: Follow, want: []team{{"211111", true}},
		},
		{
			name: "a one-day event is not the next Session",
			teams: []league.Listed{
				listed("141111", "ROVERS", cup174, "900001"),
				listed("211111", "ROVERS", open175, "900001"),
			},
			oneDay: []string{cup174.ID},
			match:  Follow, want: []team{{"211111", true}},
		},
		{
			name: "the earliest later Session only",
			teams: []league.Listed{
				listed("271111", "ROVERS", open177, "900001"),
				listed("211111", "ROVERS", open175, "900001"),
			},
			match: Follow, want: []team{{"211111", true}},
			// Session 177's division is never looked up.
			asked: []string{open175.ID},
		},
		{
			name: "this Session and earlier are not next",
			teams: []league.Listed{
				listed("111111", "ROVERS", open173, "900001"),
				listed("100001", "ROVERS", league.Division{ID: "100009", Name: "Coed Open Sun", Season: "99"}, "900001"),
			},
			match: None, want: []team{},
		},
		{
			name:  "same name, different coach",
			teams: []league.Listed{listed("211111", "ROVERS", open175, "900009")},
			match: Ask, want: []team{{"211111", false}},
		},
		{
			name: "two Teams by the name, one with the coach",
			teams: []league.Listed{
				listed("211112", "ROVERS", open175, "900009"),
				listed("211111", "ROVERS", mens175, "900001"),
			},
			match: Ask, want: []team{{"211111", true}, {"211112", false}},
		},
		{
			name: "two Teams by the name and coach",
			teams: []league.Listed{
				listed("211112", "ROVERS", mens175, "900001"),
				listed("211111", "ROVERS", open175, "900001"),
			},
			match: Ask, want: []team{{"211111", true}, {"211112", true}},
		},
		{
			name:  "no coach to compare",
			old:   league.Team{ID: "111111", Name: "ROVERS", Division: open173, Season: "173", CoachID: "0"},
			teams: []league.Listed{listed("211111", "ROVERS", open175, "0")},
			match: Ask, want: []team{{"211111", false}},
		},
		{
			name: "a Team that dropped out is not a match",
			teams: []league.Listed{
				{TeamSummary: league.TeamSummary{ID: "211111", Name: "ROVERS", Division: open175, Season: "175"}, CoachID: "900001", Dropped: true},
			},
			match: None, want: []team{},
		},
		{
			name: "none, with the coach's Teams under new names",
			teams: []league.Listed{
				listed("211114", "ZEBRAS", open175, "900001"),
				listed("211113", "ROVERS FC", mens175, "900001"),
				listed("211112", "MIDDLES", open175, "900001"),
				listed("211111", "ALPHAS", open175, "900001"),
				listed("222222", "KICKERS", open175, "900002"),
				listed("141111", "CUP SIDE", cup174, "900001"),
				listed("271111", "LATER", open177, "900001"),
			},
			oneDay: []string{cup174.ID},
			match:  None, want: []team{}, similar: []string{"211111", "211112", "211113"},
		},
		{
			name: "none, and no similar Teams without a coach",
			old:  league.Team{ID: "111111", Name: "ROVERS", Division: open173, Season: "173"},
			teams: []league.Listed{
				listed("211111", "ALPHAS", open175, ""),
			},
			match: None, want: []team{},
		},
		{
			name:  "no Season number to be later than",
			old:   league.Team{ID: "111111", Name: "ROVERS", Division: open173, CoachID: "900001"},
			teams: []league.Listed{listed("211111", "ROVERS", open175, "900001")},
			match: None, want: []team{},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			o := old
			if tt.old.ID != "" {
				o = tt.old
			}
			divs := &fakeDivisions{oneDay: tt.oneDay}
			r := &Resolver{Teams: fakeTeams{o.ID: o}, Lists: &fakeLists{teams: tt.teams}, Divisions: divs}
			res, err := r.Find(context.Background(), o.ID)
			if err != nil {
				t.Fatal(err)
			}
			if res.Team.ID != o.ID || res.Team.Name != o.Name || res.Team.Season != o.Season || res.Team.Division.ID != o.Division.ID {
				t.Errorf("team = %+v", res.Team)
			}
			if res.Match != tt.match {
				t.Errorf("match = %q, want %q", res.Match, tt.match)
			}
			got := []team{}
			for _, c := range res.Teams {
				got = append(got, team{c.ID, c.SameCoach})
			}
			if !slices.Equal(got, tt.want) {
				t.Errorf("teams = %+v, want %+v", got, tt.want)
			}
			var similar []string
			for _, s := range res.Similar {
				similar = append(similar, s.ID)
			}
			if !slices.Equal(similar, tt.similar) {
				t.Errorf("similar = %v, want %v", similar, tt.similar)
			}
			if res.Teams == nil || res.Similar == nil {
				t.Errorf("null array: %+v", res)
			}
			if tt.asked != nil && !slices.Equal(divs.asked, tt.asked) {
				t.Errorf("divisions looked up = %v, want %v", divs.asked, tt.asked)
			}
		})
	}
}

// The answer's shape is what the site reads, and it never names a coach.
func TestResultJSON(t *testing.T) {
	r := &Resolver{
		Teams:     fakeTeams{old.ID: old},
		Lists:     &fakeLists{teams: []league.Listed{listed("211111", "ROVERS", open175, "900001")}},
		Divisions: &fakeDivisions{},
	}
	res, err := r.Find(context.Background(), old.ID)
	if err != nil {
		t.Fatal(err)
	}
	b, err := json.Marshal(res)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"team":{"id":"111111","name":"ROVERS","division":{"id":"110001","name":"Coed Open Sun","code":"COSun","season":"173"},"season":"173"},` +
		`"match":"follow",` +
		`"teams":[{"id":"211111","name":"ROVERS","division":{"id":"150001","name":"Coed Open Sun","code":"COSun","season":"175"},"season":"175","same_coach":true}],` +
		`"similar":[]}`
	if string(b) != want {
		t.Fatalf("got\n%s\nwant\n%s", b, want)
	}
	if strings.Contains(string(b), "900001") {
		t.Fatal("coach id in the answer")
	}
}

func TestFindErrors(t *testing.T) {
	lists := []league.Listed{
		listed("211111", "ROVERS", open175, "900001"),
		listed("222222", "KICKERS", mens175, "900001"),
	}
	tests := []struct {
		name   string
		teamID string
		lists  *fakeLists
		fail   map[string]error
		want   error
	}{
		{"unknown Team", "333333", &fakeLists{teams: lists}, nil, league.ErrTeamNotFound},
		{"league down for the Team", "999999", &fakeLists{teams: lists}, nil, league.ErrUnavailable},
		{"no Team list", old.ID, &fakeLists{fail: league.ErrUnavailable}, nil, league.ErrUnavailable},
		{"division lookup fails", old.ID, &fakeLists{teams: lists}, map[string]error{open175.ID: league.ErrUnavailable}, league.ErrUnavailable},
		{"division the league cannot find", old.ID, &fakeLists{teams: lists}, map[string]error{open175.ID: league.ErrDivisionNotFound}, league.ErrUnavailable},
		{"canceled", old.ID, &fakeLists{teams: lists}, map[string]error{open175.ID: context.Canceled}, context.Canceled},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			r := &Resolver{Teams: fakeTeams{old.ID: old}, Lists: tt.lists, Divisions: &fakeDivisions{fail: tt.fail}}
			if _, err := r.Find(context.Background(), tt.teamID); !errors.Is(err, tt.want) {
				t.Fatalf("err = %v, want %v", err, tt.want)
			}
		})
	}

	// A similar Team's division failing fails the answer too.
	r := &Resolver{
		Teams:     fakeTeams{old.ID: old},
		Lists:     &fakeLists{teams: lists[1:]},
		Divisions: &fakeDivisions{fail: map[string]error{mens175.ID: league.ErrUnavailable}},
	}
	if _, err := r.Find(context.Background(), old.ID); !errors.Is(err, league.ErrUnavailable) {
		t.Fatalf("similar: err = %v", err)
	}
}

// Without a Season number the Team list is not needed at all.
func TestFindWithoutSeasonReadsNoList(t *testing.T) {
	lists := &fakeLists{fail: league.ErrUnavailable}
	o := league.Team{ID: "111111", Name: "ROVERS"}
	r := &Resolver{Teams: fakeTeams{o.ID: o}, Lists: lists, Divisions: &fakeDivisions{}}
	res, err := r.Find(context.Background(), o.ID)
	if err != nil || res.Match != None || lists.calls != 0 {
		t.Fatalf("%+v %v, %d list reads", res, err, lists.calls)
	}
}
