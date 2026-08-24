"""Turn a grader result into a list of priced, reachable rank flips.

The score is a step function: `0.4 + 0.6*(n-rank)/(n-1)` per scored test. A change that raises PnL
without crossing a gap is worth exactly nothing, which is how v8 earned +$10.01 and scored 0.00.
Reading a result therefore means asking two questions the raw score does not answer -- which gap is
nearest, and which win has become thin enough to lose -- and this does both mechanically so that
neither is noticed a version too late.

    python3.13 ledger.py results.txt              # priced flips and at-risk wins
    python3.13 ledger.py new.txt --against results.txt   # what the last submission actually moved
"""

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)

import sys

from exchange_sim import load_truth


def flip_value(field_size: int) -> float:
    """What one place is worth. Two-maker tests pay triple a four-maker test."""
    return 0.6 / (field_size - 1) if field_size > 1 else 0.0


def rows(path: str) -> list[dict]:
    out: list[dict] = []
    for row in load_truth(path):
        entries = row["entries"]
        ordered = sorted(entries, key=lambda item: -item[1])
        names = [name for name, _ in ordered]
        rank = names.index("Telescoping Theo") + 1
        ours = dict(entries)["Telescoping Theo"]
        above = ordered[rank - 2] if rank >= 2 else None
        below = ordered[rank] if rank < len(ordered) else None
        out.append({
            "test": row["test"], "verbose": row["verbose"], "score": row["score"],
            "rank": rank, "field_size": len(ordered), "ours": ours,
            "chaser": below[0] if below else None,
            "margin": ours - below[1] if below else None,
            "target": above[0] if above else None,
            "gap": above[1] - ours if above else None,
            "worth": flip_value(len(ordered)) if above else 0.0,
        })
    return out


def report(path: str) -> list[dict]:
    data = rows(path)
    scored = [row for row in data if not row["verbose"]]
    total = sum(row["score"] for row in data)
    print(f"{path}: {total:.2f} / {len(data)}   ({len(scored)} scored, "
          f"{len(data) - len(scored)} verbose)\n")

    flips = sorted((r for r in scored if r["gap"] is not None),
                   key=lambda r: r["gap"] / r["worth"] if r["worth"] else 1e9)
    print("REACHABLE FLIPS -- cheapest first")
    print(f"  {'test':>5}{'rank':>7}{'behind':>26}{'gap':>9}{'worth':>8}{'$/point':>10}")
    for row in flips:
        cost = row["gap"] / row["worth"] if row["worth"] else float("inf")
        print(f"  {row['test']:>5}{row['rank']:>4}/{row['field_size']:<2}"
              f"{row['target'][:24]:>26}{row['gap']:>9.2f}{row['worth']:>+8.2f}{cost:>10.0f}")
    print(f"\n  total available: {sum(r['worth'] for r in flips):+.2f}")

    thin = sorted((r for r in scored if r["margin"] is not None and r["rank"] == 1),
                  key=lambda r: r["margin"])
    print("\nWINS TO PROTECT -- thinnest first")
    print(f"  {'test':>5}{'over':>26}{'margin':>9}{'at risk':>9}")
    for row in thin:
        mark = "  <-- thin" if row["margin"] < 5.0 else ""
        print(f"  {row['test']:>5}{row['chaser'][:24]:>26}{row['margin']:>9.2f}"
              f"{flip_value(row['field_size']):>9.2f}{mark}")
    return data


def compare(new: str, old: str) -> None:
    before = {r["test"]: r for r in rows(old)}
    after = {r["test"]: r for r in rows(new)}
    moved = [t for t in after if t in before and after[t]["score"] != before[t]["score"]]
    delta = sum(r["score"] for r in after.values()) - sum(r["score"] for r in before.values())
    print(f"\n\nWHAT MOVED  ({old} -> {new}, {delta:+.2f})\n")
    if not moved:
        print("  no test changed score -- every PnL change landed inside a gap")
    for test in sorted(moved):
        was, now = before[test], after[test]
        print(f"  test {test:>2}  {was['score']:.2f} -> {now['score']:.2f}  "
              f"({now['score'] - was['score']:+.2f};  rank {was['rank']} -> {now['rank']},  "
              f"PnL {was['ours']:+.2f} -> {now['ours']:+.2f})")
    unmoved = [t for t in after if t in before and after[t]["score"] == before[t]["score"]
               and abs(after[t]["ours"] - before[t]["ours"]) > 1.0]
    if unmoved:
        print(f"\n  PnL moved but score did not, on {len(unmoved)} tests: "
              + ", ".join(f"{t}({after[t]['ours'] - before[t]['ours']:+.1f})" for t in sorted(unmoved)))


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = args[0] if args else _os.path.join(_ROOT, "results", "results.txt")
    report(path)
    if "--against" in sys.argv:
        compare(path, sys.argv[sys.argv.index("--against") + 1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
