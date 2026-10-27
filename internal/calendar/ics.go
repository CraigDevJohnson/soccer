// Package calendar writes games as an iCalendar (.ics) file in Mountain time,
// carried over from soccer_scraper.
package calendar

import (
	"fmt"
	"strings"
	"time"

	ics "github.com/arran4/golang-ical"

	"github.com/CraigDevJohnson/soccer/internal/league"
)

const (
	// Location is the Facility's address, the same for every Boise game.
	Location = "Let's Play Soccer, Boise, 11448 W President Dr #8967, Boise, ID 83713, USA"
	// TimezoneName is the zone every event is written in; league.Mountain
	// is the same zone loaded.
	TimezoneName = "America/Denver"
	// AlarmTrigger reminds 40 minutes before a game, as soccer_scraper did.
	AlarmTrigger = "-PT40M"
	// RefreshInterval asks subscribing calendar apps to check back every few
	// hours; most ignore it and choose their own.
	RefreshInterval = "PT6H"
	uidDomain       = "soccer.craigdevjohnson.com"
)

// Event is one game to write. Home and Away are the two sides as the league
// lists them. Team is the Team this calendar is for that plays in the game,
// written as the league names that side; the title puts it first. It is
// empty when the calendar is for both sides, or for neither, and the title
// then keeps the league's Home vs Away order.
type Event struct {
	GameID string
	Start  time.Time
	End    time.Time
	Field  string
	Home   string
	Away   string
	Team   string
}

// Title is the event's summary line: the person's Team, its opponent and
// the field, such as "ROVERS vs KICKERS - Field 3", so a glance at the
// calendar says where the game is. The field is left off only when the
// league gave none. A field the league names rather than numbers is written
// as given, "North Court" rather than "Field North Court".
func Title(e Event) string {
	title := matchup(e)
	if field := FieldLabel(e.Field); field != "" {
		title += " - " + field
	}
	return title
}

// matchup is "<Team> vs <opponent>": e.Team first when it is one side and
// not the other, else Home vs Away.
func matchup(e Event) string {
	first, second := e.Home, e.Away
	if sameTeam(e.Team, e.Away) && !sameTeam(e.Team, e.Home) {
		first, second = e.Away, e.Home
	}
	return fmt.Sprintf("%s vs %s", first, second)
}

// sameTeam compares two Team names as the league might vary them: trimmed,
// ignoring case. Two empty names are not the same Team.
func sameTeam(a, b string) bool {
	a, b = strings.TrimSpace(a), strings.TrimSpace(b)
	return a != "" && strings.EqualFold(a, b)
}

// FieldLabel names a field for a person: "Field 3" for a field the league
// numbers, a field it names as it is, "" for none.
func FieldLabel(field string) string {
	if n, ok := league.FieldNumber(field); ok {
		return "Field " + n
	}
	return strings.TrimSpace(field)
}

// Notice is an all-day note on one day, with no reminder and shown as
// free: the end of a Team's Session, written into its Session link. Day's
// date is used as it reads in Day's own location.
type Notice struct {
	TeamID      string
	Day         time.Time
	Summary     string
	Description string
	URL         string
}

// Calendar is a named list of events, and notices written after them.
type Calendar struct {
	Name    string
	Events  []Event
	Notices []Notice
}

// Write renders c as an iCalendar document. stamp is the DTSTAMP for every
// event. Each event's UID is built from its game ID alone, and each notice's
// from its Team ID, so a calendar app that fetches a Session link again
// updates them instead of duplicating them.
func Write(c Calendar, stamp time.Time) (string, error) {
	loc := league.Mountain
	cal := ics.NewCalendarFor("craigdevjohnson.com Let's Plan Soccer")
	cal.SetMethod(ics.MethodPublish)
	cal.SetCalscale("GREGORIAN")
	if c.Name != "" {
		cal.SetXWRCalName(c.Name)
	}
	cal.SetXWRTimezone(TimezoneName)
	cal.SetRefreshInterval(RefreshInterval) // the property already carries VALUE=DURATION
	cal.SetXPublishedTTL(RefreshInterval)
	addTimezone(cal)

	for _, e := range c.Events {
		ev := cal.AddEvent(fmt.Sprintf("game-%s@%s", e.GameID, uidDomain))
		ev.SetSummary(Title(e))
		ev.AddProperty(ics.ComponentPropertyDtStart, e.Start.In(loc).Format("20060102T150405"), ics.WithTZID(TimezoneName))
		ev.AddProperty(ics.ComponentPropertyDtEnd, e.End.In(loc).Format("20060102T150405"), ics.WithTZID(TimezoneName))
		ev.SetLocation(Location)
		ev.SetDescription(description(e))
		ev.SetDtStampTime(stamp.UTC())

		alarm := ev.AddAlarm()
		alarm.SetAction(ics.ActionDisplay)
		alarm.SetTrigger(AlarmTrigger)
		alarm.AddProperty(ics.ComponentPropertyDescription, "Soccer game starting soon")
	}
	for _, n := range c.Notices {
		ev := cal.AddEvent(fmt.Sprintf("session-over-%s@%s", n.TeamID, uidDomain))
		ev.SetSummary(n.Summary)
		ev.SetAllDayStartAt(n.Day)
		ev.SetAllDayEndAt(n.Day.AddDate(0, 0, 1))
		ev.SetTimeTransparency(ics.TransparencyTransparent)
		ev.SetDescription(n.Description)
		ev.SetURL(n.URL)
		ev.SetDtStampTime(stamp.UTC())
	}
	// iCalendar content lines end in CRLF (RFC 5545 §3.1).
	return cal.Serialize(ics.WithNewLineWindows), nil
}

// description is the event's longer text: the field, the matchup in the
// title's order, and the place.
func description(e Event) string {
	lines := []string{}
	if field := FieldLabel(e.Field); field != "" {
		lines = append(lines, field)
	}
	lines = append(lines, matchup(e), "Let's Play Soccer, Boise")
	return strings.Join(lines, "\n")
}

// addTimezone writes America/Denver's rules so calendar apps read each local
// time correctly: MST (UTC-7) from the first Sunday of November, MDT (UTC-6)
// from the second Sunday of March.
func addTimezone(cal *ics.Calendar) {
	tz := cal.AddTimezone(TimezoneName)

	standard := tz.AddStandard()
	standard.AddProperty(ics.ComponentProperty(ics.PropertyDtstart), "19701101T020000")
	standard.AddProperty(ics.ComponentProperty(ics.PropertyTzoffsetfrom), "-0600")
	standard.AddProperty(ics.ComponentProperty(ics.PropertyTzoffsetto), "-0700")
	standard.AddProperty(ics.ComponentProperty(ics.PropertyRrule), "FREQ=YEARLY;BYMONTH=11;BYDAY=1SU")
	standard.AddProperty(ics.ComponentProperty(ics.PropertyTzname), "MST")

	daylight := &ics.Daylight{}
	daylight.AddProperty(ics.ComponentProperty(ics.PropertyDtstart), "19700308T020000")
	daylight.AddProperty(ics.ComponentProperty(ics.PropertyTzoffsetfrom), "-0700")
	daylight.AddProperty(ics.ComponentProperty(ics.PropertyTzoffsetto), "-0600")
	daylight.AddProperty(ics.ComponentProperty(ics.PropertyRrule), "FREQ=YEARLY;BYMONTH=3;BYDAY=2SU")
	daylight.AddProperty(ics.ComponentProperty(ics.PropertyTzname), "MDT")
	tz.Components = append(tz.Components, daylight)
}

// Name joins Team names into a calendar name, such as "Rovers + Kickers".
func Name(teamNames []string) string {
	return strings.Join(teamNames, " + ")
}

// Filename suggests a download name built from the Team names.
func Filename(teamNames []string) string {
	var b strings.Builder
	for _, r := range strings.Join(teamNames, "_") {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '-', r == '_':
			b.WriteRune(r)
		case r == ' ':
			b.WriteRune('_')
		}
	}
	name := b.String()
	if name == "" {
		name = "soccer"
	}
	if len(name) > 80 {
		name = name[:80]
	}
	return name + ".ics"
}
