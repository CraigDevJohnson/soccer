// Package link turns a Selection into a Session link token and back. The
// token carries the whole choice, so nothing about a Visitor is stored. A
// Session link is not secret: anyone who has it can use it.
package link

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/CraigDevJohnson/soccer/internal/schedule"
)

const (
	version     = 1
	maxTokenLen = 4096
	maxGames    = 200
	maxGameID   = 64
)

// ErrInvalid means a token is not one this package wrote.
var ErrInvalid = errors.New("invalid session link")

type payload struct {
	V int      `json:"v"`
	T []string `json:"t"`
	X []string `json:"x,omitempty"`
	I []string `json:"i,omitempty"`
	S int64    `json:"s"`
}

// Encode returns the token for sel. Since is kept to the second.
func Encode(sel schedule.Selection) (string, error) {
	if err := check(sel.Excluded, sel.Included); err != nil {
		return "", err
	}
	ids, err := schedule.NormalizeTeamIDs(sel.TeamIDs)
	if err != nil {
		return "", err
	}
	b, err := json.Marshal(payload{V: version, T: ids, X: sel.Excluded, I: sel.Included, S: sel.Since.Unix()})
	if err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(b), nil
}

// Decode reads a token back into a Selection.
func Decode(token string) (schedule.Selection, error) {
	if token == "" || len(token) > maxTokenLen {
		return schedule.Selection{}, ErrInvalid
	}
	b, err := base64.RawURLEncoding.DecodeString(token)
	if err != nil {
		return schedule.Selection{}, ErrInvalid
	}
	var p payload
	if err := json.Unmarshal(b, &p); err != nil || p.V != version || p.S <= 0 {
		return schedule.Selection{}, ErrInvalid
	}
	ids, err := schedule.NormalizeTeamIDs(p.T)
	if err != nil {
		return schedule.Selection{}, fmt.Errorf("%w: %v", ErrInvalid, err)
	}
	if err := check(p.X, p.I); err != nil {
		return schedule.Selection{}, fmt.Errorf("%w: %v", ErrInvalid, err)
	}
	return schedule.Selection{TeamIDs: ids, Excluded: p.X, Included: p.I, Since: time.Unix(p.S, 0)}, nil
}

func check(lists ...[]string) error {
	n := 0
	for _, l := range lists {
		n += len(l)
		for _, id := range l {
			if id == "" || len(id) > maxGameID {
				return fmt.Errorf("game ID %q is not usable", id)
			}
		}
	}
	if n > maxGames {
		return fmt.Errorf("at most %d games can be ticked or unticked", maxGames)
	}
	return nil
}
