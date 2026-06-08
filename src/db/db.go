package db

import (
	"fmt"
	"log"

	"github.com/LeelaChessZero/lczero-server/src/config"
	"github.com/jinzhu/gorm"
	// SQLite backend for the chessckers fleet (single-file DB, no daemon).
	_ "github.com/jinzhu/gorm/dialects/sqlite"
)

var db *gorm.DB
var err error

// Init initializes database.
func Init() {
	// Database.Dbname is the path to the SQLite database file. WAL journaling plus
	// a busy timeout let several self-play clients upload concurrently without
	// tripping "database is locked".
	dsn := fmt.Sprintf("file:%s?_busy_timeout=5000&_journal_mode=WAL&_foreign_keys=on",
		config.Config.Database.Dbname)
	db, err = gorm.Open("sqlite3", dsn)
	if err != nil {
		log.Fatal("Unable to connect to DB", err)
	}
}

// SetupDB setups DB.
func SetupDB() {
	db.AutoMigrate(&User{})
	db.AutoMigrate(&Client{})
	db.AutoMigrate(&TrainingRun{})
	db.AutoMigrate(&Network{})
	db.AutoMigrate(&Match{})
	db.AutoMigrate(&MatchGame{})
	db.AutoMigrate(&TrainingGame{})

	// getTopUsers() in the server reads these as plain tables. The upstream
	// Postgres deployment created them out-of-band; for the SQLite fleet we
	// define them as views so they always reflect current training_games.
	db.Exec(`CREATE VIEW IF NOT EXISTS games_all AS
		SELECT users.username AS username, COUNT(*) AS count
		FROM training_games JOIN users ON users.id = training_games.user_id
		GROUP BY users.username`)
	db.Exec(`CREATE VIEW IF NOT EXISTS games_month AS
		SELECT users.username AS username, COUNT(*) AS count
		FROM training_games JOIN users ON users.id = training_games.user_id
		WHERE training_games.created_at >= datetime('now','-30 day')
		GROUP BY users.username`)
}

// CreateTrainingRun creates training run
func CreateTrainingRun(description string) *TrainingRun {
	trainingRun := TrainingRun{Description: description}
	err := db.Create(&trainingRun).Error
	if err != nil {
		log.Fatal(err)
	}
	return &trainingRun
}

// GetDB returns current database object
func GetDB() *gorm.DB {
	return db
}

// Close closes database
func Close() {
	db.Close()
}
