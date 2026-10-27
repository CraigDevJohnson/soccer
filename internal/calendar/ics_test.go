package calendar

import (
	"strings"
	"testing"
	"time"
)

func TestWrite(t *testing.T) {
	denver, _ := time.LoadLocation(TimezoneName)
	start := time.Date(2026, 11, 2, 19, 15, 0, 0, denver)
	stamp := time.Date(2026, 10, 10, 0, 0, 0, 0, time.UTC)
	out, err := Write(Calendar{
		Name: "ROVERS + KICKERS",
		Events: []Event{
			{GameID: "g1", Start: start, End: start.Add(45 * time.Minute), Field: "3", Home: "ROVERS", Away: "KICKERS", Team: "ROVERS"},
			// A UTC time is still written as Boise wall-clock time, and the
			// calendar's Team comes first even when it plays away.
			{GameID: "g2", Start: time.Date(2026, 11, 10, 2, 0, 0, 0, time.UTC), End: time.Date(2026, 11, 10, 2, 45, 0, 0, time.UTC), Field: "1", Home: "OWLS", Away: "ROVERS", Team: "ROVERS"},
		},
	}, stamp)
	if err != nil {
		t.Fatal(err)
	}
	unfolded := strings.ReplaceAll(out, "\r\n ", "")
	for _, want := range []string{
		"BEGIN:VCALENDAR",
		"X-WR-CALNAME:ROVERS + KICKERS",
		"BEGIN:VTIMEZONE",
		"TZID:America/Denver",
		"BEGIN:DAYLIGHT",
		"TZNAME:MST",
		"UID:game-g1@soccer.craigdevjohnson.com",
		"DTSTART;TZID=America/Denver:20261102T191500",
		"DTEND;TZID=America/Denver:20261102T200000",
		"DTSTART;TZID=America/Denver:20261109T190000",
		"SUMMARY:ROVERS vs KICKERS - Field 3\r\n",
		"DESCRIPTION:Field 3\\nROVERS vs KICKERS\\nLet's Play Soccer\\, Boise\r\n",
		"SUMMARY:ROVERS vs OWLS - Field 1\r\n",
		"DESCRIPTION:Field 1\\nROVERS vs OWLS\\nLet's Play Soccer\\, Boise\r\n",
		"LOCATION:Let's Play Soccer\\, Boise",
		"TRIGGER:-PT40M",
		"DTSTAMP:20261010T000000Z",
		"REFRESH-INTERVAL;VALUE=DURATION:PT6H",
	} {
		if !strings.Contains(unfolded, want) {
			t.Errorf("missing %q in:\n%s", want, out)
		}
	}
	if n := strings.Count(out, "BEGIN:VEVENT"); n != 2 {
		t.Errorf("%d events", n)
	}
	if strings.Contains(out, "VALUE=DURATION;VALUE=DURATION") {
		t.Error("REFRESH-INTERVAL carries its parameter twice")
	}
	// Every content line ends in CRLF, and none in a bare LF.
	if strings.Count(out, "\r\n") != strings.Count(out, "\n") || !strings.HasSuffix(out, "END:VCALENDAR\r\n") {
		t.Errorf("line endings are not CRLF:\n%q", out[:60])
	}
}

// A title names the calendar's Team, then its opponent, then the field, so
// the calendar says where the game is at a glance. Home vs Away is the
// fallback when the Team is not one side alone.
func TestTitle(t *testing.T) {
	tests := []struct {
		name string
		e    Event
		want string
	}{
		{"team at home", Event{Home: "ROVERS", Away: "KICKERS", Team: "ROVERS", Field: "3"}, "ROVERS vs KICKERS - Field 3"},
		{"team away", Event{Home: "OWLS", Away: "ROVERS", Team: "ROVERS", Field: "1"}, "ROVERS vs OWLS - Field 1"},
		{"name as the league varies it", Event{Home: "OWLS", Away: "ROVERS", Team: " rovers ", Field: "1"}, "ROVERS vs OWLS - Field 1"},
		{"both sides are the calendar's", Event{Home: "OWLS", Away: "ROVERS", Field: "1"}, "OWLS vs ROVERS - Field 1"},
		{"team not in the game", Event{Home: "OWLS", Away: "ROVERS", Team: "BANDITS", Field: "1"}, "OWLS vs ROVERS - Field 1"},
		{"both sides carry the name", Event{Home: "ROVERS", Away: "ROVERS", Team: "ROVERS", Field: "2"}, "ROVERS vs ROVERS - Field 2"},
		{"no field from the league", Event{Home: "OWLS", Away: "ROVERS", Team: "ROVERS"}, "ROVERS vs OWLS"},
		{"blank field", Event{Home: "OWLS", Away: "ROVERS", Team: "ROVERS", Field: " "}, "ROVERS vs OWLS"},
		{"field number with a letter", Event{Home: "OWLS", Away: "ROVERS", Team: "ROVERS", Field: "3A"}, "ROVERS vs OWLS - Field 3A"},
		{"field the league names", Event{Home: "OWLS", Away: "ROVERS", Team: "ROVERS", Field: "North Court"}, "ROVERS vs OWLS - North Court"},
		{"field already labelled", Event{Home: "OWLS", Away: "ROVERS", Team: "ROVERS", Field: "Field 2"}, "ROVERS vs OWLS - Field 2"},
		{"field named after its number", Event{Home: "OWLS", Away: "ROVERS", Team: "ROVERS", Field: "Field 3 North"}, "ROVERS vs OWLS - Field 3 North"},
		{"field named with the word", Event{Home: "OWLS", Away: "ROVERS", Team: "ROVERS", Field: "Field North"}, "ROVERS vs OWLS - Field North"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := Title(tt.e); got != tt.want {
				t.Errorf("Title(%+v) = %q, want %q", tt.e, got, tt.want)
			}
		})
	}
}

// The description keeps the field on its own line and the matchup in the
// title's order; a game with no field has no empty line.
func TestDescription(t *testing.T) {
	if got := description(Event{Home: "OWLS", Away: "ROVERS", Team: "ROVERS", Field: "1"}); got != "Field 1\nROVERS vs OWLS\nLet's Play Soccer, Boise" {
		t.Errorf("description = %q", got)
	}
	if got := description(Event{Home: "OWLS", Away: "ROVERS"}); got != "OWLS vs ROVERS\nLet's Play Soccer, Boise" {
		t.Errorf("description without a field = %q", got)
	}
}

func TestWriteNotice(t *testing.T) {
	denver, _ := time.LoadLocation(TimezoneName)
	start := time.Date(2026, 11, 8, 19, 15, 0, 0, denver)
	stamp := time.Date(2026, 11, 20, 0, 0, 0, 0, time.UTC)
	c := Calendar{
		Name:   "ROVERS",
		Events: []Event{{GameID: "g1", Start: start, End: start.Add(45 * time.Minute), Field: "3", Home: "ROVERS", Away: "KICKERS"}},
		Notices: []Notice{{
			TeamID:      "111111",
			Day:         time.Date(2026, 11, 9, 0, 0, 0, 0, denver),
			Summary:     "ROVERS: this Session is over",
			Description: "ROVERS has played its last game of this Session, so this calendar won't get next Session's games.",
			URL:         "https://example.test/soccer?after=111111&after=222222",
		}},
	}
	out, err := Write(c, stamp)
	if err != nil {
		t.Fatal(err)
	}
	unfolded := strings.ReplaceAll(out, "\r\n ", "")
	notice := unfolded[strings.LastIndex(unfolded, "BEGIN:VEVENT"):]
	for _, want := range []string{
		"UID:session-over-111111@soccer.craigdevjohnson.com\r\n",
		"DTSTART;VALUE=DATE:20261109\r\n",
		"DTEND;VALUE=DATE:20261110\r\n",
		"TRANSP:TRANSPARENT\r\n",
		"SUMMARY:ROVERS: this Session is over\r\n",
		"DESCRIPTION:ROVERS has played its last game of this Session\\, so this calendar won't get next Session's games.\r\n",
		"URL:https://example.test/soccer?after=111111&after=222222\r\n",
		"DTSTAMP:20261120T000000Z\r\n",
	} {
		if !strings.Contains(notice, want) {
			t.Errorf("missing %q in:\n%s", want, notice)
		}
	}
	// A notice is not a game: no reminder, no place, no time zone.
	for _, not := range []string{"VALARM", "LOCATION", "TZID"} {
		if strings.Contains(notice, not) {
			t.Errorf("notice has %s:\n%s", not, notice)
		}
	}
	if n := strings.Count(out, "BEGIN:VEVENT"); n != 2 {
		t.Errorf("%d events, want the game and the notice", n)
	}
	// The same input gives the same bytes, so calendar apps update the
	// notice rather than add another.
	again, _ := Write(c, stamp)
	if again != out {
		t.Error("output differs between runs")
	}
}

func TestWriteEmpty(t *testing.T) {
	out, err := Write(Calendar{Name: "Soccer"}, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(out, "BEGIN:VEVENT") || !strings.Contains(out, "END:VCALENDAR") {
		t.Fatalf("unexpected:\n%s", out)
	}
}

func TestFilename(t *testing.T) {
	tests := map[string][]string{
		"ROVERS_FC_KICKERS.ics": {"ROVERS FC", "KICKERS"},
		"NO_BUENO_O30.ics":      {"NO BUENO O30!"},
		"soccer.ics":            {"\"/\\"},
	}
	for want, names := range tests {
		if got := Filename(names); got != want {
			t.Errorf("Filename(%q) = %q, want %q", names, got, want)
		}
	}
	if got := Filename([]string{strings.Repeat("A", 200)}); len(got) != 84 {
		t.Errorf("long name not cut: %d", len(got))
	}
}
