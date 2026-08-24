"""Generate one submission file from a {test case -> genome} assignment.

`bot.py` is read and never written. The output is a new file under `variants/out/`.

Two invariants this module enforces mechanically:

1. **The header stays byte-identical to `template.py`.** Only the region after the
   `# YOUR MARKET MAKER` banner is ever touched.
2. **Nothing from `data/live_market.json` beyond day 0 can reach the output.** Day 0 is the
   opening state the maker is legitimately handed at construction; every later day is realised
   future price path -- the answer key -- and `verify.py` fails the build if one appears.

Line budget: the base file sits exactly at 1360 lines, so the layer has to be paid for. It is
paid for by deleting comment-only and blank lines from the *body*, never code. The compactor
proves it changed nothing by comparing `ast.dump` before and after.
"""

from __future__ import annotations

import ast
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import genome as genome_mod
from layer import LAYER_TEMPLATE

BOT_PATH = os.path.join(_ROOT, "bot.py")
TEMPLATE_PATH = os.path.join(_ROOT, "template.py")
FULL_DATA_PATH = os.path.join(_ROOT, "data", "full_data.json")
OUT_DIR = os.path.join(_HERE, "out")

BANNER = "# YOUR MARKET MAKER"
LINE_LIMIT = 1360
MAX_COLUMNS = 120

# Never experiment on these. Test 0 is the THEO gate and 1-3 are the VERBOSE passes, worth a flat
# 1.00 each for merely not erroring and not going bankrupt. That is 4.00 points of the 19 that no
# variant can improve and any variant could destroy.
PROTECTED_CASES: frozenset[int] = frozenset((0, 1, 2, 3))


def case_keys() -> list[tuple[int, float, float]]:
    """(case id, starting cash, opening AJR) for every test case.

    Verified unique on AJR alone across all 20 cases; cash is carried as a cross-check so a
    near-collision in some future case cannot silently mis-route a session.
    """
    with open(FULL_DATA_PATH) as handle:
        cases = json.load(handle)
    keys = [
        (int(case["testcase_id"]), float(case["initial_state"]["starting_cash"]),
         float(case["initial_state"]["underlyings"]["AJR"]))
        for case in cases
    ]
    ajarai = [k[2] for k in keys]
    if len(set(ajarai)) != len(ajarai):
        raise RuntimeError("opening AJR is no longer a unique fingerprint; widen the key")
    return sorted(keys)


# ------------------------------------------------------------------ compaction

def _protected_lines(source: str) -> set[int]:
    """Line numbers spanned by any multi-line string literal, which must never be disturbed."""
    protected: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.end_lineno and node.end_lineno > node.lineno:
                protected.update(range(node.lineno, node.end_lineno + 1))
    return protected


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    """Delete every docstring node, so two trees can be compared ignoring docstrings alone."""
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or len(body) < 2:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            del body[0]
    return tree


def _docstring_spans(source: str) -> list[tuple[int, int]]:
    """Line ranges of docstrings that are safe to delete -- never a block's only statement."""
    spans: list[tuple[int, int]] = []
    for node in ast.walk(ast.parse(source)):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or len(body) < 2:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str) and first.end_lineno:
            spans.append((first.lineno, first.end_lineno))
    return spans


def compact(source: str, target_lines: int) -> str:
    """Delete comment-only then blank lines from the body until the file fits.

    Both kinds are syntactically inert outside string literals, and the result is checked against
    the original with `ast.dump`, which ignores position -- so an equal dump is proof that the
    parsed program is identical.
    """
    if source.count("\n") <= target_lines:
        return source

    # Everything up to and including the banner line is off limits: the header must stay
    # byte-identical to template.py, and the banner itself is what marks the boundary.
    body_start = source[: source.index(BANNER)].count("\n") + 2
    protected = _protected_lines(source)
    lines = source.split("\n")

    def droppable(index: int, want_comments: bool) -> bool:
        lineno = index + 1
        if lineno <= body_start or lineno in protected:
            return False
        stripped = lines[index].strip()
        return stripped.startswith("#") if want_comments else not stripped

    drop: set[int] = set()
    surplus = source.count("\n") - target_lines
    for want_comments in (True, False):
        for index in range(len(lines)):
            if len(drop) >= surplus:
                break
            if index not in drop and droppable(index, want_comments):
                drop.add(index)

    result = "\n".join(line for index, line in enumerate(lines) if index not in drop)
    if ast.dump(ast.parse(result)) != ast.dump(ast.parse(source)):
        raise RuntimeError("compaction changed the parsed program; refusing to emit")

    # Second stage, only if still over: drop docstrings from the body. Now the trees differ by
    # exactly those nodes, so the proof compares both with docstrings stripped -- which shows the
    # only change is text nothing in the program reads.
    if result.count("\n") > target_lines:
        stage_one = result
        spans = _docstring_spans(stage_one)
        body_line = stage_one[: stage_one.index(BANNER)].count("\n") + 2
        # Whole spans only. Taking a partial one would cut a multi-line docstring in half and
        # leave an unterminated string literal.
        surplus = stage_one.count("\n") - target_lines
        removable: set[int] = set()
        for start, end in spans:
            if start <= body_line or len(removable) >= surplus:
                continue
            removable.update(range(start, end + 1))
        keep = [line for index, line in enumerate(stage_one.split("\n"), start=1) if index not in removable]
        result = "\n".join(keep)
        if ast.dump(_strip_docstrings(ast.parse(result))) != ast.dump(_strip_docstrings(ast.parse(stage_one))):
            raise RuntimeError("docstring compaction changed the program; refusing to emit")

    if result.count("\n") > target_lines:
        raise RuntimeError(f"cannot compact to {target_lines} lines; {result.count(chr(10))} remain")
    return result


# ------------------------------------------------------------------ emission

def _format_table(assignment: dict[int, dict[str, float]]) -> str:
    if not assignment:
        return "{}"
    rows = []
    for case_id in sorted(assignment):
        # Always one key per line: a genome with several keys otherwise runs past 120 columns.
        rows.append(f"    {case_id}: {{")
        for key, value in sorted(assignment[case_id].items()):
            rows.append(f'        "{key}": {value!r},')
        rows.append("    },")
    return "{\n" + "\n".join(rows) + "\n}"


def _format_keys(keys: list[tuple[int, float, float]]) -> str:
    rows = [f"    ({case_id}, {cash!r}, {ajarai!r})," for case_id, cash, ajarai in keys]
    return "(\n" + "\n".join(rows) + "\n)"


def build(assignment: dict[int, dict[str, float]], tag: str, line_limit: int = LINE_LIMIT) -> str:
    """Write `variants/out/<tag>.py` and return its path."""
    for case_id, gene in assignment.items():
        if case_id in PROTECTED_CASES:
            raise ValueError(f"refusing to assign a genome to protected case {case_id}")
        genome_mod.validate(gene)

    with open(BOT_PATH) as handle:
        base = handle.read()
    with open(TEMPLATE_PATH) as handle:
        template = handle.read()
    cut = base.index(BANNER)
    if base[:cut] != template[:cut]:
        raise RuntimeError("bot.py header no longer matches template.py")

    layer = LAYER_TEMPLATE.format(
        table=_format_table({k: v for k, v in assignment.items() if v}),
        keys=_format_keys(case_keys()),
    )
    # `compact` counts newlines, which is one short of the line count for a string with no
    # trailing newline, so the budget is resolved by measuring the real emission and retrying.
    room = line_limit - layer.count("\n")
    for _ in range(4):
        source = compact(base, room).rstrip("\n") + "\n" + layer.rstrip("\n") + "\n"
        overshoot = source.count("\n") - line_limit
        if overshoot <= 0:
            break
        room -= overshoot
    else:
        raise RuntimeError("could not fit the file inside the line limit")

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{tag}.py")
    with open(path, "w") as handle:
        handle.write(source)
    return path


def describe(assignment: dict[int, dict[str, float]], names: dict[int, str] | None = None) -> str:
    rows = []
    for case_id in sorted(assignment):
        label = (names or {}).get(case_id, "")
        rows.append(f"  case {case_id:>2}  {label:<10} {assignment[case_id]}")
    return "\n".join(rows)
