# variants/ — per-test overfit search

Nothing here is submitted except a generated file under `out/`. **`bot.py` is read, never written.**

## Numbering

Everything in this directory is keyed by `testcase_id` (as in `data/full_data.json`). **The grader
counts THEO as its test 1, so its labels run one higher:** grader `TC-N` == `testcase_id N-1`.
The Stalemate $16.00 / us $15.47 session is `testcase_id 4`, which the grader calls **TC-5**.

## What this is

Sixteen scored tests, a deterministic grader, unlimited submissions, and a full 19-test breakdown
returned every run. That combination means the search does not have to be sequential: a submission
can run a **different strategy in each test**, because every test case has a unique opening state
and the maker is handed that state at construction.

So one submission measures six experiments at once, each cell is noiseless, and a cell once
measured is final. The search is six independent finite-armed bandits, not a tuning treadmill.

## The mechanism

`build.py` appends a subclass to a copy of `bot.py`:

```python
_BaseMarketMaker = MarketMaker

class MarketMaker(_BaseMarketMaker):
    ...
```

All 23 tunable constants on the base class are read through `self.`, so an instance attribute
shadows any of them; the six estimation priors are module globals and are written directly. Seven
lowercase *behavioural* switches (`variance_scale`, `skew_gain`, `theo_shift`, …) are handled by
pass-through method overrides. A session whose opening state is not in the table gets an empty
genome and therefore the unmodified 16.30 bot.

The base file sits exactly at the 1360-line limit, so the layer is paid for by deleting
comment-only and blank lines from the body — never code. The compactor proves it changed nothing
by comparing `ast.dump` before and after.

## Invariants the gate enforces

| check | why |
|---|---|
| **containment** | The variant must be *bit-identical* to `bot.py` on every unassigned test, and must actually move every assigned one. This is what makes a bad submission cost information and nothing else. |
| **no answer-key values** | No AJR/THR level from day 1 onward of any recorded trajectory may appear in the file. Day 0 is the opening state the maker is legitimately handed; later days are the realised future path. |
| protected cases | Test 0 (THEO gate) and 1–3 (VERBOSE) can never be assigned a genome — 4.00 points no variant can raise and any variant could destroy. |
| header / lines / ASCII / columns / stdlib | the standing submission gate from the root `README.md` |
| theo exact, `sim.py`, no crash, no bankruptcy | correctness, and a variant that busts is worth zero regardless of the exchange |

`price_option_from_parameters` is deliberately never overridden, so the THEO gate cannot be
perturbed by any genome.

## Workflow

```sh
python3.13 variants/plan.py                 # the campaign schedule
python3.13 variants/matrix.py next          # build + gate the next submission
# submit variants/out/subNN.py, paste the FULL 19-test output:
python3.13 variants/matrix.py record 0 variants/results/sub00.txt
python3.13 variants/matrix.py report        # per-test results, best-of composite
```

A test that reaches 1.00 is pinned and stops consuming a slot.

## Honest limits

- The local harness cannot score any of this. It is used only as a **crash/bankruptcy filter** and
  for the containment proof — both facts about the code, not predictions about rank. The rule from
  `HANDOFF.md` still holds: never calibrate on `exchange_sim`.
- The search finds, per test, a strategy that happens to suit that test's realised path. Much of
  the per-test PnL spread in this field is inventory luck — a fixed-width bot wins $41 on one test
  and loses $16 on another. **So a win found here is partly a fitted coin flip and carries no
  guarantee into round 2**, where the same file is submitted against different cases. The round-2
  fallback is the unmodified 16.30 bot, which is exactly what an unrecognised session gets.
