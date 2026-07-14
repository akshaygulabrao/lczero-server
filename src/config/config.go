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
		Games      int
		Parameters []interface{}
		Threshold  float64
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
