// Bootstrap initializes a fresh chessckers training database: it creates the
// schema and seeds training run #1 with the self-play and match parameters that
// the dispatcher hands to clients. Run it once from the repo root (so it reads
// the same serverconfig.json / chessckers.db as the server) before starting the
// server and the trainer bridge.
package main

import (
	"encoding/json"
	"log"
	"os"

	"github.com/LeelaChessZero/lczero-server/src/db"
)

func main() {
	db.Init()
	defer db.Close()
	db.SetupDB()

	// If run #1 already exists, do nothing (idempotent bootstrap).
	var count int
	db.GetDB().Model(&db.TrainingRun{}).Count(&count)
	if count > 0 {
		log.Println("Training run already exists; bootstrap is a no-op.")
		return
	}

	// Run name (TrainingRun.Description) is the human handle for the experiment,
	// shown in the dashboard and on every Network row's run. Overridable via
	// RUN_NAME so a reset-then-relaunch can name the run per experiment without
	// editing this seeder (launch_server.sh sets it).
	runName := os.Getenv("RUN_NAME")
	if runName == "" {
		runName = "Chessckers run 1"
	}
	run := db.CreateTrainingRun(runName)

	// Self-play (training) parameters: Dirichlet root noise + move-selection
	// temperature give the games enough diversity for AlphaZero-style learning.
	// Match parameters keep some opening temperature (the start position is fixed,
	// so without it every game between the same two nets would be identical) but
	// converge to near-greedy play after the opening.
	//
	// PCR flags (probabilistic chain reduction): opt-in via env so runs that don't
	// set them get exactly the same trainParams as before (no silent inheritance).
	//   PCR_FULL_PROB=0.25  -> appends --pcr-full-prob=0.25 --pcr-fast-visits=<PCR_FAST_VISITS|100>
	//   PCR_FULL_PROB unset -> trainParams is the baseline literal below, unchanged.
	//
	// Gumbel S2 (run 26+): GUMBEL_SH=true emits ONLY the S2 flag set —
	// --visits=<VISITS|64> --gumbel-sh=true --gumbel-m=<GUMBEL_M|16>. No Dirichlet
	// or temperature flags: under S2 the Gumbel root perturbation is the
	// exploration mechanism and the Sequential Halving winner is the played move.
	// Requires an engine with fork commit 03e524e+ deployed FIRST (older engines
	// die on the unknown flag — the league deploy-order lesson).
	var trainParamsJSON string
	if os.Getenv("GUMBEL_SH") == "true" {
		visits := os.Getenv("VISITS")
		if visits == "" {
			visits = "64"
		}
		gumbelM := os.Getenv("GUMBEL_M")
		if gumbelM == "" {
			gumbelM = "16"
		}
		flags := []string{
			"--visits=" + visits,
			"--gumbel-sh=true",
			"--gumbel-m=" + gumbelM,
		}
		b, err := json.Marshal(flags)
		if err != nil {
			log.Fatal(err)
		}
		trainParamsJSON = string(b)
	} else if pcrProb := os.Getenv("PCR_FULL_PROB"); pcrProb != "" {
		pcrVisits := os.Getenv("PCR_FAST_VISITS")
		if pcrVisits == "" {
			pcrVisits = "100"
		}
		flags := []string{
			"--noise-epsilon=0.25",
			"--noise-alpha=0.3",
			"--temperature=1.0",
			"--tempdecay-moves=15",
			"--visits=800",
			"--pcr-full-prob=" + pcrProb,
			"--pcr-fast-visits=" + pcrVisits,
		}
		b, err := json.Marshal(flags)
		if err != nil {
			log.Fatal(err)
		}
		trainParamsJSON = string(b)
	} else {
		// Baseline (PUCT + Dirichlet + temperature). VISITS overrides the 800
		// default so the run-27 ablation can run this EXACT flag set at 64
		// visits — the only way to separate "low visits are enough" from
		// "Sequential Halving is what makes low visits work" (run 26 changed
		// both at once). Unset VISITS => byte-identical to the old literal.
		visits := os.Getenv("VISITS")
		if visits == "" {
			visits = "800"
		}
		flags := []string{
			"--noise-epsilon=0.25",
			"--noise-alpha=0.3",
			"--temperature=1.0",
			"--tempdecay-moves=15",
			"--visits=" + visits,
		}
		b, err := json.Marshal(flags)
		if err != nil {
			log.Fatal(err)
		}
		trainParamsJSON = string(b)
	}
	trainParams := trainParamsJSON
	matchParams := `["--visits=128","--temperature=1.0","--tempdecay-moves=10","--temp-visit-offset=-0.8"]`
	err := db.GetDB().Model(run).Updates(map[string]interface{}{
		"train_parameters": trainParams,
		"match_parameters": matchParams,
		"active":           true,
	}).Error
	if err != nil {
		log.Fatal(err)
	}
	log.Printf("Bootstrapped training run #%d (%q).", run.ID, runName)
}
