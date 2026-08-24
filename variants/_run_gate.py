"""Subprocess half of the gate: load a variant *as if it were* `bot`, then run the harness on it.

Invoked as `python3.13 _run_gate.py <variant.py>`. The harness modules all do `from bot import ...`,
so installing the variant in `sys.modules` under that name points every one of them at the variant
without copying a file over `bot.py`.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from contextlib import redirect_stdout

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


def load_as_bot(path: str):
    spec = importlib.util.spec_from_file_location("bot", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["bot"] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    path = sys.argv[1]
    report: dict[str, object] = {"path": path}

    load_as_bot(path)
    sys.path.insert(0, os.path.join(_ROOT, "harness"))

    import real_sim
    import sim

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        report["theo_exact"] = bool(real_sim.check_theo_case())
    report["theo_log"] = buffer.getvalue().strip()

    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer):
            sim.main()
        report["sim_ok"] = "ALL CHECKS PASSED" in buffer.getvalue()
    except Exception as error:  # a crash in the correctness sim is itself the finding
        report["sim_ok"] = False
        report["sim_error"] = f"{type(error).__name__}: {error}"
    report["sim_tail"] = buffer.getvalue().strip().split("\n")[-1] if buffer.getvalue() else ""

    # Dispatch check: every genome in the table must actually land, and no case outside the table
    # may pick one up. Without this a fingerprint typo would silently ship the unmodified bot.
    module = sys.modules["bot"]
    with open(os.path.join(_ROOT, "data", "full_data.json")) as handle:
        all_cases = {int(c["testcase_id"]): c for c in json.load(handle)}
    table = dict(getattr(module, "_GENOME_TABLE", {}))
    mismatches: list[str] = []
    for case_id, case in sorted(all_cases.items()):
        values = {real_sim.NAME_TO_ID[k]: v for k, v in case["initial_state"]["underlyings"].items()}
        options = [real_sim.parse_option(text) for text in case["initial_active_options"]]
        maker = module.MarketMaker(
            real_sim.underlyings(values), options, case["initial_state"]["starting_cash"]
        )
        want = table.get(case_id, {})
        if dict(getattr(maker, "_genome", {})) != want:
            mismatches.append(f"case {case_id}: got {getattr(maker, '_genome', None)} want {want}")
    report["assigned"] = sorted(table)
    report["dispatch_ok"] = not mismatches
    report["dispatch_mismatches"] = mismatches

    # Crash / bankruptcy smoke test. Not a score estimate -- the harness cannot produce one -- but
    # a variant that errors or busts is worth zero on that test no matter how the exchange behaves.
    crashes: list[str] = []
    bankruptcies: dict[str, int] = {}
    cases = real_sim.load_cases()
    for case in cases:
        case_id = str(case["testcase_id"])
        busts = 0
        for replication in range(6):
            try:
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    result = real_sim.run_case(case, seed=7000 + replication * 13, days=None, use_live=True)
                busts += int(bool(result.get("__bankrupt__")))
            except Exception as error:
                crashes.append(f"case {case_id} rep {replication}: {type(error).__name__}: {error}")
                break
        if busts:
            bankruptcies[case_id] = busts
    report["crashes"] = crashes
    report["bankruptcies"] = bankruptcies

    print("###GATE###" + json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
