package api

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-lambda-go/events"
	"github.com/awslabs/aws-lambda-go-api-proxy/httpadapter"

	"github.com/CraigDevJohnson/soccer/internal/league"
)

func at(day, hour int) time.Time {
	return time.Date(2026, 10, day, hour, 0, 0, 0, league.Mountain)
}

var now = at(10, 12)

type fakeTeams struct{ down bool }

func (f fakeTeams) Team(_ context.Context, id string) (league.Team, error) {
	if f.down {
		return league.Team{}, league.ErrUnavailable
	}
	switch id {
	case "111111":
		return league.Team{ID: id, Name: "ROVERS", Season: "2026", Games: []league.Game{
			{ID: "g1", Start: at(7, 19), Field: "1", Home: "ROVERS", Away: "BANDITS"},
			{ID: "g3", Start: at(14, 19), Field: "2", Home: "ROVERS", Away: "KICKERS"},
		}}, nil
	case "222222":
		return league.Team{ID: id, Name: "KICKERS", Season: "2026", Games: []league.Game{
			{ID: "g3", Start: at(14, 19), Field: "2", Home: "ROVERS", Away: "KICKERS"},
			{ID: "g2", Start: at(14, 18), Field: "4", Home: "KICKERS", Away: "OWLS"},
		}}, nil
	}
	return league.Team{}, league.ErrTeamNotFound
}

type fakeDirectory struct{}

func (fakeDirectory) SearchTeams(_ context.Context, q string) ([]league.TeamSummary, error) {
	if strings.EqualFold(q, "rov") {
		return []league.TeamSummary{{ID: "111111", Name: "ROVERS", Division: "Coed Open"}}, nil
	}
	return nil, nil
}

func (fakeDirectory) Divisions(context.Context) ([]league.Division, error) {
	return []league.Division{{ID: "d1", Name: "Coed Open"}}, nil
}

func (fakeDirectory) DivisionTeams(_ context.Context, id string) ([]league.TeamSummary, error) {
	return []league.TeamSummary{{ID: "111111", Name: "ROVERS", Division: id}}, nil
}

func newServer(t *testing.T, teams league.Teams, dir league.Directory) (http.Handler, *bytes.Buffer) {
	t.Helper()
	logs := &bytes.Buffer{}
	h := &Handler{
		Teams:       teams,
		Directory:   dir,
		LinkBaseURL: "https://example.test/soccer/link/",
		Log:         slog.New(slog.NewJSONHandler(logs, nil)),
		Now:         func() time.Time { return now },
	}
	return h.Router(), logs
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

func TestSearchNotConnectedYet(t *testing.T) {
	h, _ := newServer(t, fakeTeams{}, league.Unconnected{})
	for _, path := range []string{"/teams?q=rovers", "/divisions", "/divisions/d1/teams"} {
		if rec := do(t, h, "GET", path, ""); rec.Code != http.StatusNotImplemented || !strings.Contains(rec.Body.String(), "not_connected") {
			t.Errorf("%s: %d %s", path, rec.Code, rec.Body)
		}
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

	for _, bad := range []string{`not json`, `{"team_ids":[]}`, `{"team_ids":["111111"],"extra":1}`} {
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
