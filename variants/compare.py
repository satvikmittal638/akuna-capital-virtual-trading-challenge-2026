"""Diff a grader result against the 16.30 board.

The 16.30 per-test numbers come from `HANDOFF.md` section 2, with two corrections. That table has
stale entries: it gives test 6 as 16.15, but `results_log.md:1231` -- the PnL-by-test table for the
build whose md5 *is* the standing 16.30 -- records 16.25, which is what the grader returns today.
Test 8 is corrected the same way, from the run rather than the summary.

This is a baseline error, not contamination. `bot.py`'s md5 matches HANDOFF's stated 16.30 build
byte for byte; the TC-5 genome has no uppercase keys, so `globals()` is never written and no module
state can leak between sessions; and `equivalence.py` shows all unassigned tests bit-identical.

`results/results_v17.txt` is a *different, older* build and must not be used as the baseline.

    python3.13 variants/compare.py variants/results/tc5_boundary.txt
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import matrix

# testcase_id -> (score, our PnL, rival label). Grader "TC-N" == testcase_id N-1.
BEST_16_30: dict[int, tuple[float, float, str]] = {
    4: (0.40, 15.47, "Stalemate Quoter 16.00"),
    5: (0.40, -6.74, "Fixed Width 0.25 13.41"),
    6: (1.00, 16.25, "Fixed Width 0.25 3.03"),   # results_log.md:1231, not HANDOFF's stale 16.15
    7: (0.70, 8.95, "Fixed Width 0.1 18.03"),
    8: (1.00, 42.48, "-"),   # HANDOFF says 44.36; unverified there, see note below
    9: (1.00, 22.18, "Fixed Width 0.1 19.62"),
    10: (1.00, 16.54, "-"),
    11: (1.00, 24.92, "-"),
    12: (1.00, 12.61, "Fixed Width 0.1 6.98"),
    13: (1.00, 16.45, "Lattice 10.26"),
    14: (1.00, 15.52, "Situational Unawareness 5.21"),
    15: (0.40, -23.19, "Fixed Width 0.05 19.43"),
    16: (1.00, 43.82, "-"),
    17: (0.60, -1.07, "Fixed Width 0.05 23.62"),
    18: (0.80, -7.88, "Situational Unawareness 18.43"),
    19: (1.00, -12.17, "-"),
}
VERBOSE_TOTAL = 3.00


def main() -> int:
    rows = matrix.parse(sys.argv[1])
    print(f"\n  {'TC':>4} {'case':>5} | {'16.30 score':>11} {'new':>6} {'d':>6} | "
          f"{'16.30 PnL':>10} {'new':>9} {'delta':>9} | rank")
    print("  " + "-" * 88)
    score_old = score_new = 0.0
    for case_id in sorted(BEST_16_30):
        old_score, old_pnl, _ = BEST_16_30[case_id]
        row = rows.get(case_id)
        if row is None:
            continue
        score_old += old_score
        score_new += row["score"]
        d_score = row["score"] - old_score
        d_pnl = row["our"] - old_pnl
        flag = ""
        if abs(d_score) > 1e-9:
            flag = "  <== FLIPPED" if d_score > 0 else "  <== LOST"
        elif abs(d_pnl) > 0.005:
            flag = "  (pnl moved, score unchanged)"
        print(f"  {case_id + 1:>4} {case_id:>5} | {old_score:>11.2f} {row['score']:>6.2f} "
              f"{d_score:>+6.2f} | {old_pnl:>10.2f} {row['our']:>9.2f} {d_pnl:>+9.2f} | "
              f"{row['rank']}/{row['field']}{flag}")

    verbose = sum(r["score"] for t, r in rows.items() if t < 4)
    print("  " + "-" * 88)
    print(f"  scored subtotal   {score_old:.2f}  ->  {score_new:.2f}   ({score_new - score_old:+.2f})")
    print(f"  verbose           {VERBOSE_TOTAL:.2f}  ->  {verbose:.2f}")
    print(f"  TOTAL            {VERBOSE_TOTAL + score_old:.2f}  ->  "
          f"{verbose + score_new:.2f}   ({verbose + score_new - VERBOSE_TOTAL - score_old:+.2f})")

    unchanged = sum(1 for c in BEST_16_30 if c in rows and abs(rows[c]["our"] - BEST_16_30[c][1]) <= 0.005)
    print(f"\n  identical to the cent: {unchanged}/{len(BEST_16_30)} scored tests")
    total_pnl_old = sum(v[1] for v in BEST_16_30.values())
    total_pnl_new = sum(rows[c]["our"] for c in BEST_16_30 if c in rows)
    print(f"  total scored PnL: {total_pnl_old:+.2f}  ->  {total_pnl_new:+.2f}   "
          f"({total_pnl_new - total_pnl_old:+.2f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
