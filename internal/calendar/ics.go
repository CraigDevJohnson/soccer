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

// Event is one game to write.
type Event struct {
	GameID string
	Start  time.Time
	End    time.Time
	Field  string
	Home   string
	Away   string
}

// Calendar is a named list of events.
type Calendar struct {
	Name   string
	Events []Event
}

// Write renders c as an iCalendar document. stamp is the DTSTAMP for every
// event. Each event's UID is built from its game ID alone, so a calendar app
// that fetches a Session link again updates games instead of duplicating them.
func Write(c Calendar, stamp time.Time) (string, error) {
	loc := league.Mountain
	cal := ics.NewCalendarFor("craigdevjohnson.com Schedule Downloader")
	cal.SetMethod(ics.MethodPublish)
	cal.SetCalscale("GREGORIAN")
	if c.Name != "" {
		cal.SetXWRCalName(c.Name)
	}
	cal.SetXWRTimezone(TimezoneName)
	cal.SetRefreshInterval(RefreshInterval, ics.WithValue("DURATION"))
	cal.SetXPublishedTTL(RefreshInterval)
	addTimezone(cal)

	for _, e := range c.Events {
		ev := cal.AddEvent(fmt.Sprintf("game-%s@%s", e.GameID, uidDomain))
		ev.SetSummary(fmt.Sprintf("%s vs %s", e.Home, e.Away))
		ev.AddProperty(ics.ComponentPropertyDtStart, e.Start.In(loc).Format("20060102T150405"), ics.WithTZID(TimezoneName))
		ev.AddProperty(ics.ComponentPropertyDtEnd, e.End.In(loc).Format("20060102T150405"), ics.WithTZID(TimezoneName))
		ev.SetLocation(Location)
		ev.SetDescription(fmt.Sprintf("Field %s\n%s vs %s\nLet's Play Soccer, Boise", e.Field, e.Home, e.Away))
		ev.SetDtStampTime(stamp.UTC())

		alarm := ev.AddAlarm()
		alarm.SetAction(ics.ActionDisplay)
		alarm.SetTrigger(AlarmTrigger)
		alarm.AddProperty(ics.ComponentPropertyDescription, "Soccer game starting soon")
	}
	return cal.Serialize(), nil
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
