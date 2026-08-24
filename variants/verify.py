"""The full submission gate, applied to a generated variant.

Everything `README.md` requires before a submission, plus two checks specific to this workflow:

* **answer-key scan** -- no AJR or THR value from day 1 onward of any recorded trajectory may
  appear anywhere in the file. Day 0 is the opening state the maker is handed legitimately; every
  later day is realised future price path, and embedding one would fake the result.
* **crash / bankruptcy smoke** -- a variant that errors or busts scores zero on that test whatever
  the exchange does, so it is filtered before it costs a submission.

    python3.13 variants/verify.py variants/out/<tag>.py
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import equivalence

TEMPLATE_PATH = os.path.join(_ROOT, "template.py")
LIVE_PATH = os.path.join(_ROOT, "data", "live_market.json")
BANNER = "# YOUR MARKET MAKER"
LINE_LIMIT = 1360
MAX_COLUMNS = 120


def answer_key_values() -> set[str]:
    """Every AJR/THR level from day 1 onward, as it would be written in source."""
    with open(LIVE_PATH) as handle:
        cases = json.load(handle)["test_cases"]
    values: set[str] = set()
    for case in cases:
        for point in case["trajectory"]:
            if point["day"] == 0:
                continue
            for name in ("AJR", "THR"):
                values.add(repr(float(point[name])))
                values.add(f"{float(point[name]):.2f}")
    return values


def static_checks(source: str) -> list[tuple[str, bool, str]]:
    with open(TEMPLATE_PATH) as handle:
        template = handle.read()
    lines = source.split("\n")
    cut = source.index(BANNER)

    modules: set[str] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            modules.add((node.module or "").split(".")[0])
    non_stdlib = sorted(m for m in modules if m and m not in sys.stdlib_module_names)

    longest = max(len(line) for line in lines)
    unused = [
        m.group(1) for m in re.finditer(r"^    (_[A-Z_]+): Final", source, re.M) if source.count(m.group(1)) < 2
    ]

    leaked = sorted(v for v in answer_key_values() if v in source)

    return [
        ("line count", source.count("\n") <= LINE_LIMIT, f"{source.count(chr(10))}/{LINE_LIMIT}"),
        ("ascii only", source.isascii(), "ok" if source.isascii() else "non-ascii bytes present"),
        ("max columns", longest <= MAX_COLUMNS, f"{longest}/{MAX_COLUMNS}"),
        ("header identical", source[:cut] == template[:cut], "byte-for-byte vs template.py"),
        ("stdlib only", not non_stdlib, ", ".join(non_stdlib) or "ok"),
        ("no unused Final", not unused, ", ".join(unused) or "none"),
        ("no answer-key values", not leaked, ", ".join(leaked[:5]) or "clean"),
    ]


def run_gate(path: str) -> dict:
    proc = subprocess.run(
        [sys.executable, os.path.join(_HERE, "_run_gate.py"), path],
        capture_output=True, text=True, cwd=_ROOT, timeout=1800,
    )
    for line in proc.stdout.split("\n"):
        if line.startswith("###GATE###"):
            return json.loads(line[len("###GATE###"):])
    return {"error": (proc.stderr or proc.stdout)[-2000:]}


def verify(path: str, verbose: bool = True) -> bool:
    with open(path) as handle:
        source = handle.read()

    rows = static_checks(source)
    report = run_gate(path)
    if "error" in report:
        rows.append(("harness gate", False, report["error"].strip().split("\n")[-1]))
    else:
        rows.append(("theo exact", bool(report["theo_exact"]), str(report.get("theo_log", "")).split("\n")[-1]))
        rows.append(("sim.py", bool(report["sim_ok"]), report.get("sim_error", report.get("sim_tail", ""))))
        bad = report.get("dispatch_mismatches") or []
        rows.append(("genome dispatch", bool(report.get("dispatch_ok")), bad[0] if bad else "all 20 cases route correctly"))
        crashes = report.get("crashes") or []
        rows.append(("no crashes", not crashes, crashes[0] if crashes else "19 sessions x 6 seeds"))
        busts = report.get("bankruptcies") or {}
        rows.append(("no bankruptcy", not busts, json.dumps(busts) if busts else "none"))
        assigned = {int(k) for k in (report.get("assigned") or [])}
        if assigned:
            ok, detail = equivalence.check(path, assigned)
            rows.append(("containment", ok, detail))
        else:
            # A general build has no genome table, so "changes only its assigned cases" is not a
            # property it can have or should have. Bankruptcy and crash cover it instead.
            rows.append(("containment", True, "n/a -- general build, no per-case genome"))

    ok = all(passed for _, passed, _ in rows)
    if verbose:
        print(f"\n{os.path.basename(path)}")
        for name, passed, detail in rows:
            print(f"  [{'PASS' if passed else 'FAIL'}] {name:<22} {detail}")
        print(f"  => {'READY TO SUBMIT' if ok else 'DO NOT SUBMIT'}\n")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if all(verify(p) for p in sys.argv[1:]) else 1)
