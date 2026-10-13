package api

import (
	"testing"
	"time"

	"github.com/CraigDevJohnson/soccer/internal/league"
	"github.com/CraigDevJohnson/soccer/internal/schedule"
)

func TestSessionOver(t *testing.T) {
	game := func(start time.Time) schedule.Game {
		return schedule.Game{Start: start, End: start.Add(schedule.GameLength)}
	}
	eight := func(last time.Time) []schedule.Game {
		var games []schedule.Game
		for i := range 8 {
			games = append(games, game(last.AddDate(0, 0, -7*(7-i))))
		}
		return games
	}
	division := league.DivisionSeason{GameLength: 45 * time.Minute, NumGames: 8}
	tests := []struct {
		name     string
		games    []schedule.Game
		division league.DivisionSeason
		now      time.Time
		day      string // "" for not over
	}{
		{"no games", nil, division, at(10, 12), ""},
		{"every game played", eight(at(4, 19)), division, at(10, 12), "2026-10-05"},
		{"a game still to come", append(eight(at(4, 19))[1:], game(at(11, 19))), division, at(10, 12), ""},
		// The division's own game length decides when the last one ended.
		{"last game not over by the division's length", eight(at(10, 11)), league.DivisionSeason{GameLength: 90 * time.Minute, NumGames: 8}, at(10, 12), ""},
		{"no game length: 45 minutes", eight(at(10, 11)), league.DivisionSeason{NumGames: 8}, at(10, 12), "2026-10-11"},
		// An unknown game count is not "every game played"; eight quiet days are.
		{"no game count", eight(at(4, 19)), league.DivisionSeason{GameLength: 45 * time.Minute}, at(10, 12), ""},
		// Ended by the quiet rule alone, the notice goes on the day the rule
		// first holds, not a week back on the day after the last game.
		{"no game count, eight quiet days", eight(at(1, 19)), league.DivisionSeason{GameLength: 45 * time.Minute}, at(10, 12), "2026-10-09"},
		{"fewer games than the count, eight quiet days", eight(at(1, 19))[5:], division, at(10, 12), "2026-10-09"},
		{"fewer games than the count, not yet eight days", eight(at(2, 19))[5:], division, at(10, 12), ""},
		// The day is the Mountain day, whatever zone the time is in.
		{"late game read in UTC", []schedule.Game{game(time.Date(2026, 10, 2, 5, 30, 0, 0, time.UTC))}, league.DivisionSeason{GameLength: 45 * time.Minute, NumGames: 1}, at(10, 12), "2026-10-02"},
		{"quiet from late at night", []schedule.Game{game(time.Date(2026, 10, 2, 4, 30, 0, 0, time.UTC))}, division, at(10, 12), "2026-10-09"},
		{"the day clocks go back", eight(time.Date(2026, 10, 31, 22, 0, 0, 0, league.Mountain)), division, time.Date(2026, 11, 2, 9, 0, 0, 0, league.Mountain), "2026-11-01"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			day, over := sessionOver(tt.games, tt.division, tt.now)
			if over != (tt.day != "") {
				t.Fatalf("over = %v, want %v", over, tt.day != "")
			}
			if !over {
				return
			}
			if got := day.Format("2006-01-02"); got != tt.day || day.Location() != league.Mountain || day.Hour() != 0 {
				t.Fatalf("day = %v, want %s at midnight in Mountain time", day, tt.day)
			}
		})
	}
}

func TestSoccerPage(t *testing.T) {
	for base, want := range map[string]string{
		"https://craigdevjohnson.com/soccer/link/":     "https://craigdevjohnson.com/soccer",
		"https://dev.craigdevjohnson.com/soccer/link/": "https://dev.craigdevjohnson.com/soccer",
		"": "",
		"https://craigdevjohnson.com/soccer/link": "",
		"https://craigdevjohnson.com/links/":      "",
	} {
		if got := (&Handler{LinkBaseURL: base}).soccerPage(); got != want {
			t.Errorf("%q: got %q, want %q", base, got, want)
		}
	}
}
