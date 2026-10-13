package api

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/aws/aws-lambda-go/events"
	"github.com/awslabs/aws-lambda-go-api-proxy/httpadapter"

	"github.com/CraigDevJohnson/soccer/internal/league"
	"github.com/CraigDevJohnson/soccer/internal/next"
)

func at(day, hour int) time.Time {
	return time.Date(2026, 10, day, hour, 0, 0, 0, league.Mountain)
}

var now = at(10, 12)

type fakeTeams struct {
	down     bool
	downOnly string // one Team ID the league fails for
}

func (f fakeTeams) Team(_ context.Context, id string) (league.Team, error) {
	if f.down || id == f.downOnly {
		return league.Team{}, league.ErrUnavailable
	}
	switch id {
	case "111111":
		return league.Team{ID: id, Name: "ROVERS", Season: "173", CoachID: "900001", Games: []league.Game{
			{ID: "g1", Start: at(7, 19), Field: "1", Home: "ROVERS", Away: "BANDITS"},
			{ID: "g3", Start: at(14, 19), Field: "2", Home: "ROVERS", Away: "KICKERS"},
		}}, nil
	case "222222":
		return league.Team{ID: id, Name: "KICKERS", Season: "173", Games: []league.Game{
			{ID: "g3", Start: at(14, 19), Field: "2", Home: "ROVERS", Away: "KICKERS"},
			{ID: "g2", Start: at(14, 18), Field: "4", Home: "KICKERS", Away: "OWLS"},
		}}, nil
	}
	return league.Team{}, league.ErrTeamNotFound
}

type fakeDirectory struct{}

func (fakeDirectory) SearchTeams(_ context.Context, q string) ([]league.TeamSummary, error) {
	if strings.EqualFold(q, "rov") {
		return []league.TeamSummary{{ID: "111111", Name: "ROVERS", Division: league.Division{ID: "d1", Name: "Coed Open"}}}, nil
	}
	return nil, nil
}

func (fakeDirectory) Divisions(context.Context) ([]league.Division, error) {
	return []league.Division{{ID: "d1", Name: "Coed Open"}}, nil
}

func (fakeDirectory) DivisionTeams(_ context.Context, id string) ([]league.TeamSummary, error) {
	return []league.TeamSummary{{ID: "111111", Name: "ROVERS", Division: league.Division{ID: id, Name: "Coed Open"}}}, nil
}

// fakeLists is the league's Team list for the next Session: ROVERS again
// under the same coach, and a Team KICKERS's coach does not have.
type fakeLists struct{}

func (fakeLists) Listed(context.Context) ([]league.Listed, error) {
	d := league.Division{ID: "d2", Name: "Coed Open", Season: "2027"}
	return []league.Listed{
		{TeamSummary: league.TeamSummary{ID: "311111", Name: "ROVERS", Division: d, Season: "2027"}, CoachID: "900001"},
		{TeamSummary: league.TeamSummary{ID: "322222", Name: "OWLS", Division: d, Season: "2027"}, CoachID: "900003"},
	}, nil
}

// fakeDivisions says every division plays 8 games of 45 minutes, or fails.
type fakeDivisions struct{ down bool }

func (f fakeDivisions) DivisionSeason(_ context.Context, id string) (league.DivisionSeason, error) {
	if f.down {
		return league.DivisionSeason{}, league.ErrUnavailable
	}
	return league.DivisionSeason{ID: id, GameLength: 45 * time.Minute, NumGames: 8}, nil
}

func newServer(t *testing.T, teams league.Teams, dir league.Directory) (http.Handler, *bytes.Buffer) {
	t.Helper()
	h, logs := newHandler(teams, dir)
	return h.Router(), logs
}

func newHandler(teams league.Teams, dir league.Directory) (*Handler, *bytes.Buffer) {
	logs := &bytes.Buffer{}
	return &Handler{
		Teams:       teams,
		Directory:   dir,
		Divisions:   fakeDivisions{},
		Next:        &next.Resolver{Teams: teams, Lists: fakeLists{}, Divisions: fakeDivisions{}},
		LinkBaseURL: "https://example.test/soccer/link/",
		Log:         slog.New(slog.NewJSONHandler(logs, nil)),
		Now:         func() time.Time { return now },
	}, logs
}

func do(t *testing.T, h http.Handler, method, target, body string) *httptest.ResponseRecorder {
	t.Helper()
	var r io.Reader
	if body != "" {
		r = strings.NewReader(body)
	}
	req := httptest.NewRequest(method, target, r)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func decode[T any](t *testing.T, rec *httptest.ResponseRecorder) T {
	t.Helper()
	var v T
	if err := json.Unmarshal(rec.Body.Bytes(), &v); err != nil {
		t.Fatalf("decode %q: %v", rec.Body.String(), err)
	}
	return v
}

type scheduleBody struct {
	Teams []struct{ ID, Name string }
	Games []struct {
		ID        string
		Remaining bool
		TeamIDs   []string `json:"team_ids"`
	}
	Failed []struct {
		TeamID string `json:"team_id"`
		Reason string
	}
}

func TestTeamByID(t *testing.T) {
	h, _ := newServer(t, fakeTeams{}, fakeDirectory{})
	rec := do(t, h, "GET", "/teams/111111", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
	s := decode[scheduleBody](t, rec)
	if len(s.Teams) != 1 || s.Teams[0].Name != "ROVERS" || len(s.Games) != 2 {
		t.Fatalf("%+v", s)
	}
	if s.Games[0].Remaining || !s.Games[1].Remaining {
		t.Fatalf("remaining: %+v", s.Games)
	}

	if rec := do(t, h, "GET", "/teams/333333", ""); rec.Code != http.StatusNotFound || !strings.Contains(rec.Body.String(), "team_not_found") {
		t.Fatalf("unknown team: %d %s", rec.Code, rec.Body)
	}
	if rec := do(t, h, "GET", "/teams/12", ""); rec.Code != http.StatusBadRequest || !strings.Contains(rec.Body.String(), "invalid_team_id") {
		t.Fatalf("bad id: %d %s", rec.Code, rec.Body)
	}
}

func TestScheduleCombinesTeams(t *testing.T) {
	h, _ := newServer(t, fakeTeams{}, fakeDirectory{})
	rec := do(t, h, "GET", "/schedule?team=111111&team=222222&team=333333", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
	s := decode[scheduleBody](t, rec)
	if len(s.Teams) != 2 || len(s.Games) != 3 || len(s.Failed) != 1 || s.Failed[0].Reason != "not_found" {
		t.Fatalf("%+v", s)
	}
	if s.Games[2].ID != "g3" || len(s.Games[2].TeamIDs) != 2 {
		t.Fatalf("shared game: %+v", s.Games[2])
	}
}

func TestLeagueDown(t *testing.T) {
	h, logs := newServer(t, fakeTeams{down: true}, fakeDirectory{})
	rec := do(t, h, "GET", "/teams/111111", "")
	if rec.Code != http.StatusServiceUnavailable || !strings.Contains(rec.Body.String(), "league_unavailable") {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
	if !strings.Contains(logs.String(), `"msg":"league_unavailable"`) {
		t.Fatalf("no alert line in logs: %s", logs)
	}
}

func TestSearchAndBrowse(t *testing.T) {
	h, _ := newServer(t, fakeTeams{}, fakeDirectory{})
	rec := do(t, h, "GET", "/teams?q=rov", "")
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"name":"ROVERS"`) {
		t.Fatalf("search: %d %s", rec.Code, rec.Body)
	}
	if rec := do(t, h, "GET", "/teams?q=zz", ""); rec.Code != http.StatusOK || strings.TrimSpace(rec.Body.String()) != `{"teams":[]}` {
		t.Fatalf("empty search: %d %s", rec.Code, rec.Body)
	}
	if rec := do(t, h, "GET", "/teams?q=r", ""); rec.Code != http.StatusBadRequest {
		t.Fatalf("short search: %d", rec.Code)
	}
	if rec := do(t, h, "GET", "/divisions", ""); rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), "Coed Open") {
		t.Fatalf("divisions: %d %s", rec.Code, rec.Body)
	}
	if rec := do(t, h, "GET", "/divisions/d1/teams", ""); rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), "111111") {
		t.Fatalf("division teams: %d %s", rec.Code, rec.Body)
	}
}

func TestDownload(t *testing.T) {
	h, logs := newServer(t, fakeTeams{}, fakeDirectory{})
	rec := do(t, h, "POST", "/calendar", `{"team_ids":["111111","222222"],"excluded":["g2"]}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
	if ct := rec.Header().Get("Content-Type"); !strings.HasPrefix(ct, "text/calendar") {
		t.Fatalf("content type %q", ct)
	}
	if cd := rec.Header().Get("Content-Disposition"); cd != `attachment; filename="ROVERS_KICKERS.ics"` {
		t.Fatalf("disposition %q", cd)
	}
	body := rec.Body.String()
	// Remaining games by default, minus the unticked one; the played game is out.
	if strings.Count(body, "BEGIN:VEVENT") != 1 || !strings.Contains(body, "UID:game-g3@") {
		t.Fatalf("events:\n%s", body)
	}
	if !strings.Contains(logs.String(), `"route":"/calendar"`) {
		t.Fatalf("output not logged: %s", logs)
	}

	for _, bad := range []string{`not json`, `{"team_ids":[]}`, `{"team_ids":["111111"],"extra":1}`, `{"team_ids":["111111"]} {"team_ids":["222222"]}`} {
		if rec := do(t, h, "POST", "/calendar", bad); rec.Code != http.StatusBadRequest {
			t.Errorf("%s: %d", bad, rec.Code)
		}
	}
}

func TestSessionLink(t *testing.T) {
	h, logs := newServer(t, fakeTeams{}, fakeDirectory{})
	rec := do(t, h, "POST", "/links", `{"team_ids":["111111","222222","333333"],"included":["g1"]}`)
	if rec.Code != http.StatusCreated {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
	created := decode[struct {
		Token  string
		URL    string
		Failed []struct {
			TeamID string `json:"team_id"`
		}
	}](t, rec)
	if created.Token == "" || created.URL != "https://example.test/soccer/link/"+created.Token+".ics" {
		t.Fatalf("%+v", created)
	}
	if len(created.Failed) != 1 || created.Failed[0].TeamID != "333333" {
		t.Fatalf("failed: %+v", created.Failed)
	}

	for _, path := range []string{"/links/" + created.Token, "/links/" + created.Token + ".ics"} {
		rec := do(t, h, "GET", path, "")
		if rec.Code != http.StatusOK || !strings.HasPrefix(rec.Header().Get("Content-Type"), "text/calendar") {
			t.Fatalf("%s: %d %s", path, rec.Code, rec.Body)
		}
		if n := strings.Count(rec.Body.String(), "BEGIN:VEVENT"); n != 3 {
			t.Fatalf("%s: %d events, want played g1 plus 2 remaining", path, n)
		}
	}
	if strings.Contains(logs.String(), created.Token) {
		t.Fatal("link token reached the logs")
	}
	if !strings.Contains(logs.String(), `"route":"/links/{token}"`) {
		t.Fatalf("link fetch not logged: %s", logs)
	}

	if rec := do(t, h, "GET", "/links/nonsense", ""); rec.Code != http.StatusNotFound {
		t.Fatalf("bad token: %d", rec.Code)
	}
	if rec := do(t, h, "POST", "/links", `{"team_ids":["333333"]}`); rec.Code != http.StatusNotFound {
		t.Fatalf("link for unknown team: %d %s", rec.Code, rec.Body)
	}
}

func TestSessionLinkAfterSessionEnds(t *testing.T) {
	h, _ := newServer(t, fakeTeams{}, fakeDirectory{})
	created := decode[struct{ Token string }](t, do(t, h, "POST", "/links", `{"team_ids":["111111"]}`))

	// Next Session: the league no longer knows the Team ID.
	h2, _ := newServer(t, onlyUnknown{}, fakeDirectory{})
	rec := do(t, h2, "GET", "/links/"+created.Token+".ics", "")
	if rec.Code != http.StatusOK || strings.Contains(rec.Body.String(), "BEGIN:VEVENT") {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
	// The league being down is not the same: the app keeps its last copy.
	h3, _ := newServer(t, fakeTeams{down: true}, fakeDirectory{})
	if rec := do(t, h3, "GET", "/links/"+created.Token, ""); rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("league down: %d", rec.Code)
	}
}

type onlyUnknown struct{}

func (onlyUnknown) Team(context.Context, string) (league.Team, error) {
	return league.Team{}, league.ErrTeamNotFound
}

// The same router answers through the Lambda adapter main.go uses, given a
// Function URL event (payload format 2.0).
func TestLambdaAdapter(t *testing.T) {
	h, _ := newServer(t, fakeTeams{}, fakeDirectory{})
	adapter := httpadapter.NewV2(h)
	resp, err := adapter.ProxyWithContext(context.Background(), events.APIGatewayV2HTTPRequest{
		Version:        "2.0",
		RawPath:        "/schedule",
		RawQueryString: "team=111111&team=222222",
		Headers:        map[string]string{"accept": "application/json"},
		RequestContext: events.APIGatewayV2HTTPRequestContext{
			HTTP: events.APIGatewayV2HTTPRequestContextHTTPDescription{Method: "GET", Path: "/schedule"},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != http.StatusOK || !strings.Contains(resp.Body, `"name":"KICKERS"`) {
		t.Fatalf("%d %s", resp.StatusCode, resp.Body)
	}
}

func TestPartialOutageNeverDropsATeam(t *testing.T) {
	h, _ := newServer(t, fakeTeams{}, fakeDirectory{})
	created := decode[struct{ Token string }](t, do(t, h, "POST", "/links", `{"team_ids":["111111","222222"]}`))

	flaky, _ := newServer(t, fakeTeams{downOnly: "222222"}, fakeDirectory{})
	// A download without KICKERS would look complete.
	if rec := do(t, flaky, "POST", "/calendar", `{"team_ids":["111111","222222"]}`); rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("download: %d %s", rec.Code, rec.Body)
	}
	// Creating a link would lose KICKERS for good.
	if rec := do(t, flaky, "POST", "/links", `{"team_ids":["111111","222222"]}`); rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("create: %d %s", rec.Code, rec.Body)
	}
	// Serving a link without KICKERS would make the app delete its games.
	if rec := do(t, flaky, "GET", "/links/"+created.Token, ""); rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("serve: %d %s", rec.Code, rec.Body)
	}
}

func TestRouterErrorsAreJSON(t *testing.T) {
	h, _ := newServer(t, fakeTeams{}, fakeDirectory{})
	for _, tc := range []struct {
		method, path string
		status       int
		code         string
	}{
		{"GET", "/nowhere", http.StatusNotFound, "not_found"},
		{"DELETE", "/links/abc", http.StatusMethodNotAllowed, "method_not_allowed"},
	} {
		rec := do(t, h, tc.method, tc.path, "")
		if rec.Code != tc.status || rec.Header().Get("Content-Type") != "application/json" || !strings.Contains(rec.Body.String(), tc.code) {
			t.Errorf("%s %s: %d %q %s", tc.method, tc.path, rec.Code, rec.Header().Get("Content-Type"), rec.Body)
		}
	}
}

type canceledTeams struct{}

func (canceledTeams) Team(ctx context.Context, _ string) (league.Team, error) {
	return league.Team{}, ctx.Err()
}

func TestCanceledRequestIsNotAnOutage(t *testing.T) {
	h, logs := newServer(t, canceledTeams{}, fakeDirectory{})
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	req := httptest.NewRequest("GET", "/teams/111111", nil).WithContext(ctx)
	h.ServeHTTP(httptest.NewRecorder(), req)
	if strings.Contains(logs.String(), "league_unavailable") {
		t.Fatalf("cancellation raised an outage alert: %s", logs)
	}
}

type panickyDirectory struct{ fakeDirectory }

func (panickyDirectory) Divisions(context.Context) ([]league.Division, error) { panic("boom") }

func TestPanicIsJSON(t *testing.T) {
	h, _ := newServer(t, fakeTeams{}, panickyDirectory{})
	rec := do(t, h, "GET", "/divisions", "")
	if rec.Code != http.StatusInternalServerError || !strings.Contains(rec.Body.String(), `"code":"internal"`) {
		t.Fatalf("%d %q", rec.Code, rec.Body)
	}
}

func TestChoiceKeepsTheScreensCutoff(t *testing.T) {
	h, _ := newServer(t, fakeTeams{}, fakeDirectory{})
	asOf := decode[struct {
		AsOf string `json:"as_of"`
	}](t, do(t, h, "GET", "/schedule?team=111111", "")).AsOf
	if asOf == "" {
		t.Fatal("schedule has no as_of")
	}

	// g3 starts Oct 14 19:00; the person submits after it has started.
	later := at(14, 20)
	h2 := &Handler{Teams: fakeTeams{}, Directory: fakeDirectory{}, Log: slog.New(slog.NewJSONHandler(io.Discard, nil)), Now: func() time.Time { return later }}
	r := h2.Router()
	rec := do(t, r, "POST", "/calendar", `{"team_ids":["111111"],"since":"`+asOf+`"}`)
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), "UID:game-g3@") {
		t.Fatalf("game ticked on screen was dropped: %d\n%s", rec.Code, rec.Body)
	}
	// Without since, the default is judged from now and g3 is out.
	rec = do(t, r, "POST", "/calendar", `{"team_ids":["111111"]}`)
	if strings.Contains(rec.Body.String(), "UID:game-g3@") {
		t.Fatalf("g3 kept without since:\n%s", rec.Body)
	}
	// A since in the future is clamped to now.
	rec = do(t, r, "POST", "/calendar", `{"team_ids":["111111"],"since":"2030-01-01T00:00:00Z"}`)
	if rec.Code != http.StatusOK || strings.Contains(rec.Body.String(), "UID:game-g3@") {
		t.Fatalf("future since: %d\n%s", rec.Code, rec.Body)
	}
}

func TestLinkRefusesSinceDecodeWouldReject(t *testing.T) {
	h, _ := newServer(t, fakeTeams{}, fakeDirectory{})
	if rec := do(t, h, "POST", "/links", `{"team_ids":["111111"],"since":"1970-01-01T00:00:00Z"}`); rec.Code != http.StatusBadRequest {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
}

func TestHeadOnLinks(t *testing.T) {
	h, _ := newServer(t, fakeTeams{}, fakeDirectory{})
	created := decode[struct{ Token string }](t, do(t, h, "POST", "/links", `{"team_ids":["111111"]}`))
	rec := do(t, h, "HEAD", "/links/"+created.Token+".ics", "")
	if rec.Code != http.StatusOK || !strings.HasPrefix(rec.Header().Get("Content-Type"), "text/calendar") {
		t.Fatalf("%d %q", rec.Code, rec.Header().Get("Content-Type"))
	}
}

func TestOversizedBodyIs413(t *testing.T) {
	h, _ := newServer(t, fakeTeams{}, fakeDirectory{})
	body := `{"team_ids":["111111"],"excluded":["` + strings.Repeat("g", 70<<10) + `"]}`
	if rec := do(t, h, "POST", "/calendar", body); rec.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
}

type slowTeams struct{}

func (slowTeams) Team(ctx context.Context, _ string) (league.Team, error) {
	<-ctx.Done()
	return league.Team{}, league.ErrUnavailable
}

// The request deadline is answered by the handler as a 503, never rewritten.
func TestDeadlineAnswers503(t *testing.T) {
	h, _ := newServer(t, slowTeams{}, fakeDirectory{})
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	req := httptest.NewRequest("GET", "/teams/111111", nil).WithContext(ctx)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusServiceUnavailable || !strings.Contains(rec.Body.String(), "league_unavailable") {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
}

type panicInFetch struct{}

func (panicInFetch) Team(context.Context, string) (league.Team, error) { panic("league body shape") }

func TestPanicWhileReadingATeamIs503(t *testing.T) {
	h, _ := newServer(t, panicInFetch{}, fakeDirectory{})
	rec := do(t, h, "GET", "/teams/111111", "")
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
}

func TestNextSession(t *testing.T) {
	h, logs := newServer(t, fakeTeams{}, fakeDirectory{})
	rec := do(t, h, "GET", "/teams/111111/next", "")
	if rec.Code != http.StatusOK || rec.Header().Get("Content-Type") != "application/json" {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
	// The shape the site reads. No coach id is in it.
	want := `{"team":{"id":"111111","name":"ROVERS","division":{"id":"","name":"","season":"173"},"season":"173"},` +
		`"match":"follow",` +
		`"teams":[{"id":"311111","name":"ROVERS","division":{"id":"d2","name":"Coed Open","season":"2027"},"season":"2027","same_coach":true}],` +
		`"similar":[]}`
	if got := strings.TrimSpace(rec.Body.String()); got != want {
		t.Fatalf("got\n%s\nwant\n%s", got, want)
	}

	// No Team by the name in the next Session, and no coach to compare.
	none := decode[struct {
		Match   string
		Teams   []any
		Similar []any
	}](t, do(t, h, "GET", "/teams/222222/next", ""))
	if none.Match != "none" || none.Teams == nil || len(none.Teams) != 0 || none.Similar == nil {
		t.Fatalf("%+v", none)
	}

	if rec := do(t, h, "GET", "/teams/333333/next", ""); rec.Code != http.StatusNotFound || !strings.Contains(rec.Body.String(), "team_not_found") {
		t.Fatalf("unknown team: %d %s", rec.Code, rec.Body)
	}
	if rec := do(t, h, "GET", "/teams/12/next", ""); rec.Code != http.StatusBadRequest || !strings.Contains(rec.Body.String(), "invalid_team_id") {
		t.Fatalf("bad id: %d %s", rec.Code, rec.Body)
	}
	if !strings.Contains(logs.String(), `"route":"/teams/{teamID}/next"`) || strings.Contains(logs.String(), "/teams/111111/next") {
		t.Fatalf("route not logged, or the path was: %s", logs)
	}
	if strings.Contains(logs.String(), "900001") {
		t.Fatal("coach id reached the logs")
	}

	down, logs := newServer(t, fakeTeams{down: true}, fakeDirectory{})
	if rec := do(t, down, "GET", "/teams/111111/next", ""); rec.Code != http.StatusServiceUnavailable || !strings.Contains(rec.Body.String(), "league_unavailable") {
		t.Fatalf("league down: %d %s", rec.Code, rec.Body)
	}
	if !strings.Contains(logs.String(), `"msg":"league_unavailable"`) {
		t.Fatalf("no alert line in logs: %s", logs)
	}
}

// seasonTeams are Teams near the end of a Session, in division d1.
type seasonTeams map[string][]league.Game

func (s seasonTeams) Team(_ context.Context, id string) (league.Team, error) {
	games, ok := s[id]
	if !ok {
		return league.Team{}, league.ErrTeamNotFound
	}
	name := map[string]string{"111111": "ROVERS", "222222": "KICKERS"}[id]
	return league.Team{ID: id, Name: name, Season: "173", Division: league.Division{ID: "d1", Name: "Coed Open"}, Games: games}, nil
}

// weekly is n games a week apart, the last starting at last.
func weekly(prefix string, last time.Time, n int) []league.Game {
	var games []league.Game
	for i := range n {
		start := last.AddDate(0, 0, -7*(n-1-i))
		games = append(games, league.Game{ID: fmt.Sprintf("%s%d", prefix, i+1), Start: start, Field: "1", Home: "ROVERS", Away: "OWLS"})
	}
	return games
}

// A Session link adds one notice for each Team whose Session is over, so a
// calendar that will get no more games says where next Session's are.
func TestSessionLinkNoticesTheEndOfASession(t *testing.T) {
	// now is Oct 10, 12:00.
	midSession := append(weekly("m", at(4, 19), 2), weekly("n", at(11, 19), 1)...)
	tests := []struct {
		name   string
		rovers []league.Game
		day    string // the notice's date, or "" for none
	}{
		{"mid-Session", midSession, ""},
		// The league posts most games after the first: one game played is
		// not the end.
		{"only the first game posted and played", weekly("r", at(4, 19), 1), ""},
		{"every game of the division played", weekly("r", at(4, 19), 8), "20261005"},
		// The day the eight days are up, not the day after the last game.
		{"eight days since the last game", weekly("r", at(1, 19), 3), "20261009"},
		{"not yet eight days", weekly("r", at(2, 19), 3), ""},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			teams := seasonTeams{"111111": tt.rovers, "222222": weekly("k", at(14, 18), 6)}
			h, _ := newServer(t, teams, fakeDirectory{})
			created := decode[struct{ Token string }](t, do(t, h, "POST", "/links", `{"team_ids":["111111","222222"],"since":"2026-08-01T00:00:00Z"}`))
			rec := do(t, h, "GET", "/links/"+created.Token+".ics", "")
			if rec.Code != http.StatusOK {
				t.Fatalf("%d %s", rec.Code, rec.Body)
			}
			body := strings.ReplaceAll(rec.Body.String(), "\r\n ", "")
			if tt.day == "" {
				if strings.Contains(body, "session-over-") {
					t.Fatalf("notice mid-Session:\n%s", body)
				}
				return
			}
			for _, want := range []string{
				"UID:session-over-111111@soccer.craigdevjohnson.com\r\n",
				"DTSTART;VALUE=DATE:" + tt.day + "\r\n",
				"SUMMARY:ROVERS: this Session is over\r\n",
				"DESCRIPTION:ROVERS has played its last game of this Session\\, so this calendar won't get next Session's games. " +
					"Get next Session's calendar from Let's Plan Soccer: https://example.test/soccer?after=111111&after=222222\r\n",
				"URL:https://example.test/soccer?after=111111&after=222222\r\n",
				"TRANSP:TRANSPARENT\r\n",
			} {
				if !strings.Contains(body, want) {
					t.Errorf("missing %q in:\n%s", want, body)
				}
			}
			// KICKERS still has games to play.
			if n := strings.Count(body, "session-over-"); n != 1 {
				t.Errorf("%d notices, want one", n)
			}
		})
	}
}

func TestSessionNoticeIsTheSameEachFetch(t *testing.T) {
	teams := seasonTeams{"111111": weekly("r", at(4, 19), 8)}
	h, _ := newHandler(teams, fakeDirectory{})
	created := decode[struct{ Token string }](t, do(t, h.Router(), "POST", "/links", `{"team_ids":["111111"]}`))
	first := do(t, h.Router(), "GET", "/links/"+created.Token, "").Body.String()
	h.Now = func() time.Time { return now.Add(36 * time.Hour) }
	second := do(t, h.Router(), "GET", "/links/"+created.Token, "").Body.String()
	stamp := regexp.MustCompile(`DTSTAMP:[0-9TZ]+`)
	if !strings.Contains(first, "session-over-111111") || stamp.ReplaceAllString(first, "") != stamp.ReplaceAllString(second, "") {
		t.Fatalf("fetches differ:\n%s\n---\n%s", first, second)
	}
}

func TestNoSessionNoticeWithoutTheSitesAddress(t *testing.T) {
	teams := seasonTeams{"111111": weekly("r", at(4, 19), 8)}
	for _, base := range []string{"", "https://example.test/calendars/"} {
		h, _ := newHandler(teams, fakeDirectory{})
		h.LinkBaseURL = base
		r := h.Router()
		created := decode[struct{ Token string }](t, do(t, r, "POST", "/links", `{"team_ids":["111111"]}`))
		rec := do(t, r, "GET", "/links/"+created.Token, "")
		if rec.Code != http.StatusOK || strings.Contains(rec.Body.String(), "session-over-") {
			t.Errorf("LinkBaseURL %q: %d\n%s", base, rec.Code, rec.Body)
		}
	}
}

// The notice is for a Session link: a downloaded file is not fetched again.
func TestNoSessionNoticeInADownload(t *testing.T) {
	h, _ := newServer(t, seasonTeams{"111111": weekly("r", at(4, 19), 8)}, fakeDirectory{})
	rec := do(t, h, "POST", "/calendar", `{"team_ids":["111111"],"since":"2026-08-01T00:00:00Z"}`)
	if rec.Code != http.StatusOK || strings.Contains(rec.Body.String(), "session-over-") || !strings.Contains(rec.Body.String(), "UID:game-r8@") {
		t.Fatalf("%d\n%s", rec.Code, rec.Body)
	}
}

// A division the league fails to answer for leaves the notice out this time;
// the calendar's games are all there, so it still answers.
func TestSessionNoticeLeftOutWhenTheDivisionFails(t *testing.T) {
	h, logs := newHandler(seasonTeams{"111111": weekly("r", at(4, 19), 8)}, fakeDirectory{})
	h.Divisions = fakeDivisions{down: true}
	r := h.Router()
	created := decode[struct{ Token string }](t, do(t, r, "POST", "/links", `{"team_ids":["111111"],"since":"2026-08-01T00:00:00Z"}`))
	rec := do(t, r, "GET", "/links/"+created.Token, "")
	if rec.Code != http.StatusOK || strings.Contains(rec.Body.String(), "session-over-") || strings.Count(rec.Body.String(), "BEGIN:VEVENT") != 8 {
		t.Fatalf("%d\n%s", rec.Code, rec.Body)
	}
	if !strings.Contains(logs.String(), `"msg":"session_over_check_failed"`) || strings.Contains(logs.String(), `"msg":"league_unavailable"`) {
		t.Fatalf("logs: %s", logs)
	}

	// While a game is still to come, the division is not looked up at all.
	h, logs = newHandler(seasonTeams{"111111": append(weekly("r", at(4, 19), 7), weekly("s", at(11, 19), 1)...)}, fakeDirectory{})
	h.Divisions = fakeDivisions{down: true}
	r = h.Router()
	created = decode[struct{ Token string }](t, do(t, r, "POST", "/links", `{"team_ids":["111111"]}`))
	if rec := do(t, r, "GET", "/links/"+created.Token, ""); rec.Code != http.StatusOK || strings.Contains(logs.String(), "session_over_check_failed") {
		t.Fatalf("%d, logs: %s", rec.Code, logs)
	}
}

// sessionTeams are Teams with their Session, division and games.
type sessionTeams map[string]league.Team

func (s sessionTeams) Team(_ context.Context, id string) (league.Team, error) {
	t, ok := s[id]
	if !ok {
		return league.Team{}, league.ErrTeamNotFound
	}
	t.ID = id
	return t, nil
}

// A link that already holds next Session's Team gets no notice when this
// Session's Team ends: the calendar does get next Session's games. When the
// later Team's Session ends too, its notice passes on only that Team.
func TestNoSessionNoticeWhenTheLinkHoldsALaterSession(t *testing.T) {
	teams := sessionTeams{
		"111111": {Name: "ROVERS", Season: "173", Division: league.Division{ID: "d1"}, Games: weekly("r", at(4, 19), 8)},
		"211111": {Name: "ROVERS", Season: "175", Division: league.Division{ID: "d2"}, Games: weekly("n", at(25, 19), 8)},
	}
	h, _ := newHandler(teams, fakeDirectory{})
	r := h.Router()
	created := decode[struct{ Token string }](t, do(t, r, "POST", "/links", `{"team_ids":["111111","211111"],"since":"2026-08-01T00:00:00Z"}`))
	body := do(t, r, "GET", "/links/"+created.Token, "").Body.String()
	if strings.Contains(body, "session-over-") || !strings.Contains(body, "UID:game-n8@") {
		t.Fatalf("notice beside next Session's Team:\n%s", body)
	}

	h.Now = func() time.Time { return at(26, 12) }
	body = strings.ReplaceAll(do(t, r, "GET", "/links/"+created.Token, "").Body.String(), "\r\n ", "")
	if !strings.Contains(body, "UID:session-over-211111@") || strings.Count(body, "session-over-") != 1 ||
		!strings.Contains(body, "URL:https://example.test/soccer?after=211111\r\n") {
		t.Fatalf("want one notice passing on 211111 alone:\n%s", body)
	}
}

// A later Session's Team under another name is someone else's Team, so the
// ended Team still gets its notice, and both stay in after.
func TestSessionNoticeBesideAnotherTeamsLaterSession(t *testing.T) {
	teams := sessionTeams{
		"111111": {Name: "ROVERS", Season: "173", Division: league.Division{ID: "d1"}, Games: weekly("r", at(4, 19), 8)},
		"211112": {Name: "KICKERS", Season: "175", Division: league.Division{ID: "d2"}, Games: weekly("n", at(25, 19), 8)},
	}
	h, _ := newHandler(teams, fakeDirectory{})
	r := h.Router()
	created := decode[struct{ Token string }](t, do(t, r, "POST", "/links", `{"team_ids":["111111","211112"],"since":"2026-08-01T00:00:00Z"}`))
	body := strings.ReplaceAll(do(t, r, "GET", "/links/"+created.Token, "").Body.String(), "\r\n ", "")
	if !strings.Contains(body, "UID:session-over-111111@") || strings.Count(body, "session-over-") != 1 ||
		!strings.Contains(body, "URL:https://example.test/soccer?after=111111&after=211112\r\n") {
		t.Fatalf("want one notice for 111111 passing on both Teams:\n%s", body)
	}
}

// gatedDivisions answers only once n lookups are waiting at the same time,
// so lookups made one after another fail. It counts lookups by division and
// keeps the deadline they were given.
type gatedDivisions struct {
	n        int
	mu       sync.Mutex
	calls    map[string]int
	waiting  int
	deadline time.Time
	open     chan struct{}
}

func (g *gatedDivisions) DivisionSeason(ctx context.Context, id string) (league.DivisionSeason, error) {
	g.mu.Lock()
	g.calls[id]++
	g.deadline, _ = ctx.Deadline()
	if g.waiting++; g.waiting == g.n {
		close(g.open)
	}
	g.mu.Unlock()
	select {
	case <-g.open:
		return league.DivisionSeason{ID: id, GameLength: 45 * time.Minute, NumGames: 8}, nil
	case <-time.After(time.Second):
		return league.DivisionSeason{}, league.ErrUnavailable
	}
}

// A link's divisions are looked up together, each once, and within
// noticeBudget, so a slow league cannot hold a calendar past the site's
// wait: one lookup after another took 8 s for 10 Teams.
func TestSessionNoticesLookDivisionsUpTogether(t *testing.T) {
	teams := sessionTeams{
		"111111": {Name: "ROVERS", Season: "173", Division: league.Division{ID: "d1"}, Games: weekly("r", at(4, 19), 8)},
		"222222": {Name: "KICKERS", Season: "173", Division: league.Division{ID: "d2"}, Games: weekly("k", at(4, 18), 8)},
		"333333": {Name: "OWLS", Season: "173", Division: league.Division{ID: "d2"}, Games: weekly("o", at(4, 20), 8)},
	}
	divisions := &gatedDivisions{n: 2, calls: map[string]int{}, open: make(chan struct{})}
	h, _ := newHandler(teams, fakeDirectory{})
	h.Divisions = divisions
	r := h.Router()
	created := decode[struct{ Token string }](t, do(t, r, "POST", "/links", `{"team_ids":["111111","222222","333333"]}`))
	rec := do(t, r, "GET", "/links/"+created.Token, "")
	if n := strings.Count(rec.Body.String(), "UID:session-over-"); rec.Code != http.StatusOK || n != 3 {
		t.Fatalf("%d, %d notices, want 3:\n%s", rec.Code, n, rec.Body)
	}
	if divisions.calls["d1"] != 1 || divisions.calls["d2"] != 1 {
		t.Fatalf("lookups = %v, want each division once", divisions.calls)
	}
	if divisions.deadline.IsZero() || divisions.deadline.After(time.Now().Add(noticeBudget)) {
		t.Fatalf("lookups had until %v, want at most %v", divisions.deadline, noticeBudget)
	}
}

// divisionFunc is a DivisionSeasons made of one function.
type divisionFunc func(ctx context.Context, id string) (league.DivisionSeason, error)

func (f divisionFunc) DivisionSeason(ctx context.Context, id string) (league.DivisionSeason, error) {
	return f(ctx, id)
}

// The notice lookups end noticeHeadroom before the request's own deadline
// when that comes first, so a slow Team read cannot push the calendar past
// the site's wait. With no time left, a cached division still answers.
func TestNoticeLookupsStopBeforeTheRequestDeadline(t *testing.T) {
	var got time.Time
	h := &Handler{
		Divisions: divisionFunc(func(ctx context.Context, id string) (league.DivisionSeason, error) {
			got, _ = ctx.Deadline()
			return league.DivisionSeason{ID: id, NumGames: 8}, nil
		}),
		Log: slog.New(slog.NewJSONHandler(io.Discard, nil)),
	}
	end := time.Now().Add(4 * time.Second)
	ctx, cancel := context.WithDeadline(context.Background(), end)
	defer cancel()
	if found := h.lookUpDivisions(ctx, []string{"d1"}); len(found) != 1 {
		t.Fatalf("found = %v", found)
	}
	if want := end.Add(-noticeHeadroom); !got.Equal(want) {
		t.Fatalf("lookups had until %v, want %v", got, want)
	}

	cache := league.NewDivisionCache(divisionFunc(func(ctx context.Context, id string) (league.DivisionSeason, error) {
		if err := ctx.Err(); err != nil {
			return league.DivisionSeason{}, err
		}
		return league.DivisionSeason{ID: id, NumGames: 8}, nil
	}))
	if _, err := cache.DivisionSeason(context.Background(), "d1"); err != nil {
		t.Fatal(err)
	}
	h.Divisions = cache
	late, cancel := context.WithDeadline(context.Background(), time.Now().Add(time.Second))
	defer cancel()
	found := h.lookUpDivisions(late, []string{"d1", "d2"})
	if _, ok := found["d1"]; !ok || len(found) != 1 {
		t.Fatalf("with no time left, found = %v, want only the cached d1", found)
	}
}
