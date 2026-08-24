"""Containment proof: a variant may change *only* the tests it was assigned.

This is the property the whole campaign rests on. Thirteen of the nineteen points -- the three
VERBOSE passes plus the ten tests already at 1.00 -- must be untouchable by any experiment, so that
a bad submission costs information and nothing else. The layer is built to guarantee it (an
unassigned session gets an empty genome, and every override is an exact pass-through at its
identity value), but "built to" is not "shown to".

Run the same seeded sessions through `bot.py` and through the variant and require bit-identical
PnL everywhere outside the assignment -- and require that every assigned case *did* move, which
catches a genome that silently no-ops.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

_RUNNER = '''
import importlib.util, io, json, sys
from contextlib import redirect_stdout
spec = importlib.util.spec_from_file_location("bot", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules["bot"] = module
spec.loader.exec_module(module)
sys.path.insert(0, "harness")
import real_sim
out = {}
for case in real_sim.load_cases():
    values = []
    for rep in range(int(sys.argv[2])):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = real_sim.run_case(case, seed=7000 + rep * 13, days=None, use_live=True)
        values.append(round(result["Telescoping Theo"], 6))
    out[int(case["testcase_id"])] = values
print("###" + json.dumps(out))
'''


def _pnl_by_case(path: str, reps: int) -> dict[int, list[float]]:
    runner = os.path.join(_HERE, "_equiv_runner.py")
    with open(runner, "w") as handle:
        handle.write(_RUNNER)
    proc = subprocess.run([sys.executable, runner, path, str(reps)], capture_output=True, text=True, cwd=_ROOT)
    for line in proc.stdout.split("\n"):
        if line.startswith("###"):
            return {int(k): v for k, v in json.loads(line[3:]).items()}
    raise RuntimeError((proc.stderr or proc.stdout)[-1500:])


def check(variant_path: str, assigned: set[int], reps: int = 4) -> tuple[bool, str]:
    base = _pnl_by_case(os.path.join(_ROOT, "bot.py"), reps)
    variant = _pnl_by_case(variant_path, reps)
    moved = {case_id for case_id in base if base[case_id] != variant.get(case_id)}
    leaked = sorted(moved - assigned)
    inert = sorted(assigned - moved)
    if leaked:
        return False, f"changed unassigned tests {leaked}"
    if inert:
        return False, f"genome had no effect on {inert}"
    return True, f"{len(base) - len(moved)} tests bit-identical, {len(moved)} assigned tests moved"


if __name__ == "__main__":
    path = sys.argv[1]
    sys.path.insert(0, _HERE)
    import importlib.util
    spec = importlib.util.spec_from_file_location("_variant_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ok, detail = check(path, set(getattr(module, "_GENOME_TABLE", {})))
    print(f"[{'PASS' if ok else 'FAIL'}] containment  {detail}")
    raise SystemExit(0 if ok else 1)
