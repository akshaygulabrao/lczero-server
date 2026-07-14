package main

import (
	"math"
	"testing"
)

// pfspProbs is pure; these tests pin the sampling-weight semantics:
// f_hard(wr) = (1-wr)^2 on Laplace-smoothed win rates, mixed with an
// epsilon-uniform floor, rounded to 3 decimals.

func almostEqual(a, b, tol float64) bool { return math.Abs(a-b) <= tol }

func TestPfspProbsNoDataIsUniform(t *testing.T) {
	ids := []uint{1, 2, 3, 4}
	probs := pfspProbs(ids, map[uint]pfspStat{})
	if len(probs) != 4 {
		t.Fatalf("want 4 probs, got %d", len(probs))
	}
	for i, p := range probs {
		if !almostEqual(p, 0.25, 0.001) {
			t.Errorf("probs[%d] = %v, want 0.25 (no data => uniform)", i, p)
		}
	}
}

func TestPfspProbsFocusesOnHardOpponent(t *testing.T) {
	// Opponent 1 crushes the learner (2/20), opponent 2 is crushed (18/20).
	ids := []uint{1, 2}
	stats := map[uint]pfspStat{
		1: {games: 20, points: 2},
		2: {games: 20, points: 18},
	}
	probs := pfspProbs(ids, stats)
	if probs[0] < 0.8 {
		t.Errorf("hard opponent prob = %v, want > 0.8", probs[0])
	}
	if probs[1] > 0.2 {
		t.Errorf("easy opponent prob = %v, want < 0.2", probs[1])
	}
	// Epsilon floor: no opponent below eps * uniform.
	floor := pfspEpsilon / float64(len(ids))
	for i, p := range probs {
		if p < floor-0.001 {
			t.Errorf("probs[%d] = %v below epsilon floor %v", i, p, floor)
		}
	}
	if s := probs[0] + probs[1]; !almostEqual(s, 1.0, 0.002) {
		t.Errorf("probs sum = %v, want ~1", s)
	}
}

func TestPfspProbsSumAndStability(t *testing.T) {
	ids := []uint{7, 8, 9}
	stats := map[uint]pfspStat{
		7: {games: 5, points: 4.5},
		8: {games: 0, points: 0}, // fresh pool entrant: smoothed to 0.5
		9: {games: 40, points: 15},
	}
	a := pfspProbs(ids, stats)
	b := pfspProbs(ids, stats)
	var sum float64
	for i := range a {
		if a[i] != b[i] {
			t.Fatalf("pfspProbs not deterministic at %d: %v vs %v", i, a[i], b[i])
		}
		sum += a[i]
	}
	if !almostEqual(sum, 1.0, 0.003) {
		t.Errorf("probs sum = %v, want ~1 (3-decimal rounding tolerance)", sum)
	}
	// 9 is the (mildly) hardest measured opponent; 8 is unknown (0.5).
	// wr: 7 -> 5.5/7 ~ 0.786, 8 -> 0.5, 9 -> 16/42 ~ 0.381.
	if !(a[2] > a[1] && a[1] > a[0]) {
		t.Errorf("expected ordering p(9) > p(8) > p(7), got %v", a)
	}
}
