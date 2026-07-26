package config

import (
	"encoding/json"
	"io/ioutil"
)

// Config is a Server config.
var Config struct {
	Database struct {
		Host     string
		User     string
		Dbname   string
		Password string
	}
	Clients struct {
		MinClientVersion  uint64
		NextClientVersion uint64
		MinEngineVersion  string
		NextEngineVersion string
	}
	URLs struct {
		OnNewNetwork          []string
		NetworkLocation       string
		BackupNetworkLocation string
	}
	Matches struct {
		// Disabled turns the promotion gate OFF entirely: every uploaded
		// candidate is auto-promoted to best (pre-2026-06-13 behavior), no
		// candidate-vs-best match or panel legs are created. Side effect:
		// championPoolIDs walks promotion MATCHES, so with gating disabled the
		// champion pool is empty — the regression panel and league self-play
		// degrade to plain self-play. Default false = gating on (an absent key
		// keeps the historical behavior).
		Disabled   bool
		Games      int
		Parameters []interface{}
		Threshold  float64
		// EveryNNetworks throttles the gate: a candidate-vs-best match is
		// created only every Nth uploaded network (0 or 1 = every network, the
		// historical behavior). Gating is SERIAL with self-play — the fleet
		// pauses while a match runs — so this is the dial that sets what
		// fraction of total fleet compute goes to gating. Rough sizing: a gate
		// costs Games*5 (+ panel legs) games at Matches.Parameters visits;
		// divide that by the visits the fleet self-plays between gates.
		// Skipped candidates are NOT promoted — best_network_id stays on the
		// last gate-approved net — so pair this with SelfplayUsesLatest or the
		// data-generating net freezes between promotions.
		// Side effect: promotions become ~N times rarer, so championPoolIDs
		// (and hence the league pool and the regression panel) fills ~N times
		// slower.
		EveryNNetworks int
		// SelfplayUsesLatest decouples the data-generating net from the gate:
		// when true, /next_game hands TRAINING games the most recently
		// uploaded network for the run instead of best_network_id. Match games
		// are unaffected — they carry their own candidate/best pair. This is
		// what makes a low-frequency gate safe: without it, throttling the gate
		// also freezes self-play on a stale net, trading compute for worse
		// data. best_network_id keeps its meaning as the last gate-approved
		// checkpoint (gate opponent, league anchor, champion-pool exclusion),
		// so the gate degrades from a blocking promotion test to a periodic
		// regression tripwire.
		SelfplayUsesLatest bool
		// Panel is the promotion-gate regression panel: each candidate also
		// plays up to Opponents log-spaced past champions, and promotion
		// additionally requires calcElo > Threshold on every leg (anti
		// rock-paper-scissors: beating the current best is not enough if the
		// candidate regresses vs older champions).
		// NOTE the x5 semantics: panel legs run at target_slice 0 and
		// createMatch multiplies the slice-0 game cap by 5, so Games=4 means
		// 20 real games per leg (just like Matches.Games=8 means a 40-game
		// main match).
		Panel struct {
			Enabled   bool
			Opponents int
			Games     int
			Threshold float64
		}
	}
	League struct {
		Enabled  bool
		Fraction float64
		PoolSize int
		// Pfsp weights league opponent sampling by live per-opponent win
		// rates (AlphaStar-style prioritized fictitious self-play) instead
		// of uniform: /next_game gains leagueProbs, which the client hands
		// to the engine as --league-probs. Old clients ignore the field;
		// old ENGINES fatal on the flag — deploy engine before client.
		Pfsp bool
	}
	WebServer struct {
		Address string
	}
}

func init() {
	content, err := ioutil.ReadFile("serverconfig.json")
	if err != nil {
		panic(err)
	}
	err = json.Unmarshal(content, &Config)
	if err != nil {
		panic(err)
	}
}
