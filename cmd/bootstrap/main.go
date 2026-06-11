// Bootstrap initializes a fresh chessckers training database: it creates the
// schema and seeds training run #1 with the self-play and match parameters that
// the dispatcher hands to clients. Run it once from the repo root (so it reads
// the same serverconfig.json / chessckers.db as the server) before starting the
// server and the trainer bridge.
package main

import (
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
	trainParams := `["--noise-epsilon=0.25","--noise-alpha=0.3","--temperature=1.0","--tempdecay-moves=15","--visits=800"]`
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
