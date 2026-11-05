package calendar

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"
)

func TestGoogleEvents(t *testing.T) {
	denver, _ := time.LoadLocation(TimezoneName)
	start := time.Date(2026, 11, 2, 19, 15, 0, 0, denver)
	got := GoogleEvents([]Event{
		{GameID: "123456", Start: start, End: start.Add(45 * time.Minute), Field: "3", Home: "KICKERS", Away: "ROVERS", Team: "ROVERS"},
		{GameID: "20261108T1830-North Court", Start: time.Date(2026, 11, 9, 1, 30, 0, 0, time.UTC), End: time.Date(2026, 11, 9, 2, 15, 0, 0, time.UTC), Field: "North Court", Home: "OWLS", Away: "ROVERS"},
	})
	b, _ := json.Marshal(got[0])
	want := `{"id":"123456","summary":"ROVERS vs KICKERS - Field 3","description":"Field 3\nKICKERS vs ROVERS\nLet's Play Soccer, Boise",` +
		`"location":"` + Location + `","start":{"dateTime":"2026-11-02T19:15:00-07:00","timeZone":"America/Denver"},` +
		`"end":{"dateTime":"2026-11-02T20:00:00-07:00","timeZone":"America/Denver"},"status":"confirmed",` +
		`"reminders":{"useDefault":false,"overrides":[{"method":"popup","minutes":40}]},"extendedProperties":{"private":{"game_id":"123456"}}}`
	if string(b) != strings.ReplaceAll(want, "\n", `\n`) {
		t.Errorf("event\n got %s\nwant %s", b, strings.ReplaceAll(want, "\n", `\n`))
	}
	// A key with characters Google refuses is written in base32hex.
	if id := got[1].ID; id != "lps68o34dhh64o3gl1h70pj0baedtp78q108dnnasjk" || !googleID.MatchString(id) {
		t.Errorf("fallback id = %q", id)
	}
	if got[1].Start.DateTime != "2026-11-08T18:30:00-07:00" || got[1].Summary != "OWLS vs ROVERS - North Court" {
		t.Errorf("second event = %+v", got[1])
	}
	// Short game IDs, which Google refuses as they are, still give an ID it
	// takes.
	for _, key := range []string{"7", "42", "g3", "1234"} {
		if id := GoogleEventID(key); !googleID.MatchString(id) {
			t.Errorf("GoogleEventID(%q) = %q, which Google refuses", key, id)
		}
	}
	if AlarmTrigger != fmt.Sprintf("-PT%dM", AlarmMinutes) {
		t.Errorf("AlarmMinutes %d doesn't match AlarmTrigger %s", AlarmMinutes, AlarmTrigger)
	}
}
