"""Bookkeeping across submissions: the (test x archetype) score matrix.

The grader is deterministic, so every cell needs exactly one measurement and a cell once filled is
final. That is what makes the campaign finite rather than a tuning treadmill.

    python3.13 variants/matrix.py record 0 variants/results/sub00.txt
    python3.13 variants/matrix.py report
    python3.13 variants/matrix.py next          # build + verify the next submission

Paste the grader's full 19-test output into `variants/results/sub<NN>.txt` first -- the full
output, never a single test, because one submission measures every target at once.
"""

from __future__ import annotations

import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import plan as plan_mod

MATRIX_PATH = os.path.join(_HERE, "results", "matrix.json")
BASELINE: dict[int, float] = {4: 0.40, 5: 0.40, 7: 0.70, 15: 0.40, 17: 0.60, 18: 0.80}
OUR_NAME = "Telescoping Theo"


def parse(path: str) -> dict[int, dict]:
    """Grader output -> {test number: row}. Block i is test i+1; tests 1-3 are VERBOSE."""
    rows: list[dict] = []
    current: list[tuple[str, float]] | None = None
    for line in open(path).read().split("\n"):
        if line.strip() == "Ranking:":
            current = []
            continue
        entry = re.match(r"\s*(\d+)\. (.+?): \$(-?[\d.]+)\s*$", line)
        if entry and current is not None:
            current.append((entry.group(2), float(entry.group(3))))
            continue
        done = re.search(r"Result: (PASS|FAIL)\s*\(score=([\d.]+)\)", line)
        if done and current:
            rows.append({"entries": current, "score": float(done.group(2))})
            current = None
    out: dict[int, dict] = {}
    for index, row in enumerate(rows):
        names = [name for name, _ in row["entries"]]
        if OUR_NAME not in names:
            continue
        out[index + 1] = {
            "score": row["score"],
            "our": dict(row["entries"])[OUR_NAME],
            "rank": names.index(OUR_NAME) + 1,
            "field": len(names),
            "leader": row["entries"][0][0],
            "leader_pnl": row["entries"][0][1],
        }
    return out


def load() -> dict:
    if os.path.exists(MATRIX_PATH):
        with open(MATRIX_PATH) as handle:
            return json.load(handle)
    return {"cells": {}, "submissions": {}}


def save(state: dict) -> None:
    os.makedirs(os.path.dirname(MATRIX_PATH), exist_ok=True)
    with open(MATRIX_PATH, "w") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)


def record(index: int, results_path: str) -> dict:
    state = load()
    parsed = parse(results_path)
    if not parsed:
        raise RuntimeError(f"no rankings parsed from {results_path}")
    names = plan_mod.submission(index, locked_in=solved(state))
    total = sum(row["score"] for row in parsed.values())
    state["submissions"][str(index)] = {
        "results": results_path, "assignment": names, "total": round(total, 2),
        "per_test": {str(t): parsed[t]["score"] for t in sorted(parsed)},
    }
    for case_id, archetype in names.items():
        if case_id not in parsed:
            continue
        state["cells"].setdefault(str(case_id), {})[archetype] = parsed[case_id]
    # Locked tests re-measure `base` every time; a change there is a regression worth seeing.
    for case_id in plan_mod.LOCKED:
        if case_id in parsed:
            state["cells"].setdefault(str(case_id), {})["base"] = parsed[case_id]
    save(state)
    print(f"recorded submission {index}: total {total:.2f} across {len(parsed)} tests")
    return state


def seed_baseline(results_path: str) -> dict:
    """Record an existing grader run as the `base` cell on every test.

    The 16.30 run is a genuine measurement of the unmodified bot across all 19 tests, so it fills a
    whole column of the matrix for free and gives every later cell something to be compared against.
    """
    state = load()
    parsed = parse(results_path)
    for case_id, row in parsed.items():
        state["cells"].setdefault(str(case_id), {})["base"] = row
    state["submissions"]["baseline"] = {
        "results": results_path, "assignment": {}, "total": round(sum(r["score"] for r in parsed.values()), 2),
        "per_test": {str(t): parsed[t]["score"] for t in sorted(parsed)},
    }
    save(state)
    print(f"seeded `base` on {len(parsed)} tests from {os.path.basename(results_path)}")
    return state


def solved(state: dict | None = None) -> dict[int, str]:
    """Tests where some archetype already reached 1.00, pinned to the cheapest such archetype."""
    state = state if state is not None else load()
    out: dict[int, str] = {}
    for case_id, cells in state.get("cells", {}).items():
        if int(case_id) not in plan_mod.TARGETS:
            continue
        winners = [name for name, row in cells.items() if row["score"] >= 1.0]
        if winners:
            out[int(case_id)] = sorted(winners, key=lambda n: (-cells[n]["our"], n))[0]
    return out


def report() -> None:
    state = load()
    cells = state.get("cells", {})
    if not cells:
        print("no results recorded yet")
        return

    print("\nper-test archetype results (score / our PnL / leader)\n")
    for case_id in sorted(plan_mod.TARGETS):
        row = cells.get(str(case_id), {})
        base = BASELINE[case_id]
        print(f"  test {case_id:>2}   baseline {base:.2f}   flip worth +{plan_mod.TARGETS[case_id]:.2f}")
        if not row:
            print("    (nothing measured)")
            continue
        for name in sorted(row, key=lambda n: (-row[n]["score"], -row[n]["our"])):
            cell = row[name]
            flag = "  <-- WIN" if cell["score"] >= 1.0 else ("  (worse)" if cell["score"] < base else "")
            print(f"    {name:<12} score {cell['score']:.2f}  us {cell['our']:>8.2f}  "
                  f"rank {cell['rank']}/{cell['field']}  vs {cell['leader']} {cell['leader_pnl']:.2f}{flag}")
        print()

    won = solved(state)
    best = {c: max(cells.get(str(c), {}).values(), key=lambda r: r["score"], default={"score": BASELINE[c]})["score"]
            for c in plan_mod.TARGETS}
    projected = sum(max(best[c], BASELINE[c]) for c in plan_mod.TARGETS)
    locked_total = sum(
        max((r["score"] for r in cells.get(str(c), {}).values()), default=1.0) for c in plan_mod.LOCKED
    )
    # The 19 points are 3 VERBOSE + 16 SCORED. THEO is a pass/fail gate and carries no points.
    print(f"  solved: {won or 'none'}")
    print(f"  best-of composite: {3.00 + locked_total + projected:.2f} / 19.00  "
          f"(3.00 verbose, {locked_total:.2f} locked, {projected:.2f} targets)")


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] == "report":
        report()
        return 0
    if sys.argv[1] == "baseline":
        seed_baseline(sys.argv[2])
        report()
        return 0
    if sys.argv[1] == "record":
        record(int(sys.argv[2]), sys.argv[3])
        report()
        return 0
    if sys.argv[1] == "next":
        state = load()
        done = [int(k) for k in state.get("submissions", {}) if k.lstrip("-").isdigit()]
        index = max(done, default=-1) + 1
        plan_mod.emit(index, locked_in=solved(state))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
