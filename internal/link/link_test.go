package link

import (
	"encoding/base64"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/CraigDevJohnson/soccer/internal/schedule"
)

func TestRoundTrip(t *testing.T) {
	since := time.Date(2026, 10, 10, 12, 30, 15, 999, time.UTC)
	in := schedule.Selection{
		TeamIDs:  []string{"111111", " 222222", "111111"},
		Excluded: []string{"g3"},
		Included: []string{"g1", "20261021T1800-4"},
		Since:    since,
	}
	token, err := Encode(in)
	if err != nil {
		t.Fatal(err)
	}
	if len(token) > 200 {
		t.Errorf("token is %d characters", len(token))
	}
	out, err := Decode(token)
	if err != nil {
		t.Fatal(err)
	}
	if len(out.TeamIDs) != 2 || out.TeamIDs[0] != "111111" || out.TeamIDs[1] != "222222" {
		t.Fatalf("teams = %v", out.TeamIDs)
	}
	if len(out.Excluded) != 1 || len(out.Included) != 2 || out.Included[1] != "20261021T1800-4" {
		t.Fatalf("games = %v / %v", out.Excluded, out.Included)
	}
	if !out.Since.Equal(since.Truncate(time.Second)) {
		t.Fatalf("since = %v", out.Since)
	}
}

func TestEncodeRefusesBadSelections(t *testing.T) {
	now := time.Now()
	long := make([]string, maxGames+1)
	for i := range long {
		long[i] = "g"
	}
	for name, sel := range map[string]schedule.Selection{
		"no teams":     {Since: now},
		"epoch since":  {TeamIDs: []string{"111111"}, Since: time.Unix(0, 0)},
		"bad team":     {TeamIDs: []string{"12"}, Since: now},
		"empty game":   {TeamIDs: []string{"111111"}, Excluded: []string{""}, Since: now},
		"too many":     {TeamIDs: []string{"111111"}, Excluded: long, Since: now},
		"long game ID": {TeamIDs: []string{"111111"}, Included: []string{string(make([]byte, maxGameID+1))}, Since: now},
	} {
		if _, err := Encode(sel); err == nil {
			t.Errorf("%s: encoded", name)
		}
	}
}

func TestDecodeRejectsGarbage(t *testing.T) {
	enc := func(s string) string { return base64.RawURLEncoding.EncodeToString([]byte(s)) }
	for name, token := range map[string]string{
		"empty":       "",
		"not base64":  "!!!",
		"not json":    enc("hello"),
		"version":     enc(`{"v":2,"t":["111111"],"s":1}`),
		"no since":    enc(`{"v":1,"t":["111111"]}`),
		"bad team":    enc(`{"v":1,"t":["abc"],"s":1}`),
		"no teams":    enc(`{"v":1,"t":[],"s":1}`),
		"bad game ID": enc(`{"v":1,"t":["111111"],"x":[""],"s":1}`),
	} {
		if _, err := Decode(token); err == nil {
			t.Errorf("%s: decoded", name)
		}
	}
}

func TestEncodeRefusesTokensDecodeWouldReject(t *testing.T) {
	ids := make([]string, maxGames)
	for i := range ids {
		ids[i] = strings.Repeat("g", maxGameID-3) + strconv.Itoa(100+i)
	}
	if _, err := Encode(schedule.Selection{TeamIDs: []string{"111111"}, Excluded: ids, Since: time.Now()}); err == nil {
		t.Fatal("encoded a token longer than Decode accepts")
	}
}
