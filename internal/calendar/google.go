package calendar

import (
	"encoding/base32"
	"regexp"
	"strings"
	"time"

	"github.com/CraigDevJohnson/soccer/internal/league"
)

// GoogleEvent is one game as a Google Calendar API event body, for the site
// to insert, or update when an event with its ID is already there. It is
// built as Write builds a game: the same title, description, place, times and
// 40-minute reminder.
type GoogleEvent struct {
	ID                 string           `json:"id"`
	Summary            string           `json:"summary"`
	Description        string           `json:"description"`
	Location           string           `json:"location"`
	Start              googleTime       `json:"start"`
	End                googleTime       `json:"end"`
	Status             string           `json:"status"`
	Reminders          googleReminders  `json:"reminders"`
	ExtendedProperties googleProperties `json:"extendedProperties"`
}

type googleTime struct {
	DateTime string `json:"dateTime"`
	TimeZone string `json:"timeZone"`
}

type googleReminders struct {
	UseDefault bool             `json:"useDefault"`
	Overrides  []googleReminder `json:"overrides"`
}

type googleReminder struct {
	Method  string `json:"method"`
	Minutes int    `json:"minutes"`
}

type googleProperties struct {
	Private map[string]string `json:"private"`
}

// AlarmMinutes is AlarmTrigger in minutes, for Google's pop-up reminder.
const AlarmMinutes = 40

// googleID is what Google accepts as an event ID: base32hex characters
// (0-9 and a-v), at least 5 of them (and at most 1024, which no game key
// comes near).
var googleID = regexp.MustCompile(`^[0-9a-v]{5,}$`)

// GoogleEventID is a game's event ID: the league's numeric game ID as it is,
// which is also what craigdevjohnson.com's earlier Let's Plan Soccer used, so
// a game added there is updated rather than added again. Any other key, such
// as a game ID shorter than Google allows or the "20261108T1830-North Court"
// kept for a game the league gave no ID, is "lps" and the key in lowercase,
// unpadded base32hex: always 5 characters or more, and never all digits.
func GoogleEventID(gameID string) string {
	if googleID.MatchString(gameID) {
		return gameID
	}
	return "lps" + strings.ToLower(base32.HexEncoding.WithPadding(base32.NoPadding).EncodeToString([]byte(gameID)))
}

// GoogleEvents builds a Google Calendar event body for each game.
func GoogleEvents(events []Event) []GoogleEvent {
	out := make([]GoogleEvent, len(events))
	for i, e := range events {
		out[i] = GoogleEvent{
			ID:          GoogleEventID(e.GameID),
			Summary:     Title(e),
			Description: description(e),
			Location:    Location,
			Start:       googleTime{DateTime: e.Start.In(league.Mountain).Format(time.RFC3339), TimeZone: TimezoneName},
			End:         googleTime{DateTime: e.End.In(league.Mountain).Format(time.RFC3339), TimeZone: TimezoneName},
			Status:      "confirmed",
			Reminders: googleReminders{
				Overrides: []googleReminder{{Method: "popup", Minutes: AlarmMinutes}},
			},
			ExtendedProperties: googleProperties{Private: map[string]string{"game_id": e.GameID}},
		}
	}
	return out
}
