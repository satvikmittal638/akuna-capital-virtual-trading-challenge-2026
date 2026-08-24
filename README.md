# Akuna Capital Virtual Trading Challenge 2026 — Binary Options Market Maker

A market-making bot for binary options on three correlated underlyings, plus the simulation and
analysis tooling built to develop it.

**Final score: 16.30 / 19** on the official grader (16 scored sessions + 3 pass/fail + 1 pricing gate).

The submitted artefact is a single stdlib-only file, [`bot.py`](bot.py) — 1360 lines, no third-party
dependencies, no I/O.

---

## The problem

Market-make binary options ("event contracts") on three daily-evolving underlyings:

| symbol | what it is | process |
|---|---|---|
| `FED` | fed funds rate | mean-reverting jump chain on a 0.25 grid |
| `AJR` | AjarAI valuation | lognormal, driven by a shared sector shock + idiosyncratic noise |
| `THR` | Theriodic valuation | same, with its own betas |

Each contract pays **1.0** if `weighted legs >= strike` at expiry, else **0.0**. In practice you see
single-leg contracts (`THR >= 650`) and spreads (`THR - AJR >= 0`).

Two order pipelines, and they demand different things:

- **RFQ** — the exchange asks every maker for a two-sided quote and routes to best bid / best offer,
  **splitting the order if necessary**. You are *not* told which side the customer wants.
- **FOK** — full terms up front; you answer yes or no. If several makers accept, it is split.

**Margin is the binding constraint.** Every trade immediately debits its *maximum loss* — buy 5 at
0.20 and your balance drops \$1.00; **sell** 5 at 0.20 and it drops \$4.00. Expiries credit back, and
solvency is checked at end of day. Go negative and the session ends.

Scoring is `0.4 + 0.6 * (n - rank) / (n - 1)` per session, where `n` is the number of makers. So a
single rank flip is worth 0.60 in a two-maker session and 0.20 in a four-maker one. **The score is a
step function: PnL that does not cross a rank gap is worth exactly zero.**

---

## The core idea: pricing is exact, not Monte Carlo

This is the part worth reading. A company's step is

```
d log(V) = drift + rate_beta * dR + sector_beta * S + eps
```

where `dR` is the **realised** rate change. Over `n` steps that sum **telescopes**:

```
sum(rate_beta * dR_i)  =  rate_beta * (R_n - R_0)
```

It depends only on the *terminal* rate, not the path — and this holds exactly, including at the zero
floor where a down-draw moves neither the rate nor `dR`.

So conditional on the terminal rate, `(log A_n, log T_n)` is bivariate normal with

- mean `log V_0 + n*drift + rate_beta*(r - R_0)`
- per-step variance `(sector_beta * sigma_S)^2 + idio^2`
- per-step covariance `sector_beta_A * sector_beta_T * sigma_S^2`

which turns pricing into:

1. an **exact Markov-chain DP** over the terminal rate distribution ([`_rate_distribution`](bot.py)), then
2. **one normal CDF per rate state** ([`_single_leg_probability`](bot.py)), or a Simpson quadrature
   for two legs ([`_pair_probability`](bot.py)).

The `THR - AJR >= 0` spread collapses to a log-ratio and is closed-form exact. No simulation, no
sampling error, and it runs in ~2 ms for a single-leg contract.

**Result: the pricing gate passes to 4 decimal places on all six contracts, spreads included.**

---

## Estimating the parameters

The live sessions don't hand you the parameters — you get ~14 days of history and must infer them.

- **Rate process** — least squares on up/down move indicators against the rate level, sharing one
  slope, with a **ridge penalty** because a short flat history makes the raw slope explode, plus a
  Laplace nudge so an unobserved direction isn't frozen out.
- **Company process** — OLS of each log return on `[1, dR]` sharing one design matrix; residual
  variance inflated by `df/(df-2)` for Student-t tails.
- **Shrinkage** — drift, rate beta and correlation are each pulled toward a prior by their own
  standard error. Twenty days of a company up 60% implies 2.5%/day, which is a lucky run, not a drift.
- **Uncertainty feeds back into the quote.** Each parameter is pushed by one standard error and the
  price moves combined in quadrature. Less knowledge ⇒ wider quote, automatically:

  | | uncertainty | quoted width |
  |---|---|---|
  | never warmed up | 0.1895 | 0.20 |
  | warmed up on full history | 0.1131 | 0.14 |

  Nobody tuned that. It falls out of the estimator's own standard errors.

Two priors were checked against the one parameter set the grader reveals:
`_CORRELATION_PRIOR_MEAN` 0.75 vs a true 0.767, and `_RATE_BETA_PRIOR_MEAN` −0.020 vs −0.020/−0.015.

---

## Results

| session | score | our PnL | nearest rival |
|---|---|---|---|
| 4 | 0.40 | 15.47 | Stalemate Quoter **16.00** |
| 5 | 0.40 | −6.74 | Fixed Width 0.25 13.41 |
| 6 | **1.00** | 16.15 | Fixed Width 0.25 3.03 |
| 7 | 0.70 | 8.95 | Fixed Width 0.1 18.03 |
| 8 | **1.00** | 44.36 | — |
| 9 | **1.00** | 22.18 | Fixed Width 0.1 19.62 |
| 10 | **1.00** | 16.54 | Fixed Width 0.1 0.70 |
| 11 | **1.00** | 24.92 | Fixed Width 0.05 −34.43 |
| 12 | **1.00** | 12.61 | Fixed Width 0.1 6.98 |
| 13 | **1.00** | 16.45 | Lattice 10.26 |
| 14 | **1.00** | 15.52 | Situational Unawareness 5.21 |
| 15 | 0.40 | −23.19 | Fixed Width 0.05 19.43 |
| 16 | **1.00** | 43.82 | Lattice 8.59 |
| 17 | 0.60 | −1.07 | Lattice 3.63 |
| 18 | 0.80 | −7.88 | Situational Unawareness 18.43 |
| 19 | **1.00** | −12.17 | Lattice −17.01 |

Scored subtotal **13.30** + 3.00 (pass/fail sessions) = **16.30 / 19**. First place in 11 of 16.

Progression: 15.10 → 15.30 → **16.20** → **16.30**. The single largest structural gain was +0.90,
and it was not tuning — see below.

---

## What the engineering actually taught

Written up in full in [`results_log.md`](results_log.md) (~1700 lines, every version and every
graded result). The findings that generalise:

**One constant feeding two mechanisms hides every cause.** `_half_spread` set *both* the quote width
and the fill-or-kill acceptance hurdle. Every experiment moved both at once and measured their sum.
Splitting it into `_QUOTE_BASE_HALF_SPREAD` and `_FOK_BASE_HALF_SPREAD` was worth **+0.90** — the
biggest single gain in the project, and it was a decoupling, not a better number.

**A step-function score punishes optimising the wrong thing.** One version raised PnL by **\$10.01
and scored 0.00**, because none of the gains crossed a rank boundary. [`harness/ledger.py`](harness/ledger.py)
was written to price the gap to the next rank *before* valuing any PnL change.

**Pricing was not the bottleneck — decisions were.** Scored against realised settlement on 189
decisions with known contract terms:

| predictor | Brier score |
|---|---|
| our theo | **0.1017** |
| the base rate | 0.2382 |
| always 0.5 | 0.2500 |

A skill score of 0.57, and roughly at par with the counterparties' own prices (0.1025 vs 0.1119).
Decisively: Brier in sessions we **lose** is **0.0911**, versus **0.1078** in sessions we **win** —
we price *better* where we lose. One session had the worst pricing measured (Brier 0.6968) and was
won outright; another was near-perfect (0.0003) and lost.

**Calibration revealed a real defect.** Both tails are too narrow — we say 0.984 and it happens 0.873
(n=63, 2.6σ); we say 0.006 and it happens 0.026. The structural cause: the model integrates over
uncertainty in the *mean* but nothing integrates over uncertainty in the *variance*.

**A local simulator that can't resolve the question must not answer it.** The replay harness carries
\$14–19 of PnL error and reproduces rank at roughly chance. Two changes tuned against it cost
**−0.80** and **−1.50** on the real grader. The rule adopted afterwards: *a change ships only if its
mechanism is justified without harness calibration.*

---

## Verification

`bot.py` is gated before every submission:

```sh
python3.13 harness/sim.py                                        # must print ALL CHECKS PASSED
python3.13 -c "import sys; sys.path.insert(0,'harness'); \
  import real_sim; print(real_sim.check_theo_case())"            # must print True
wc -l bot.py                                                     # <= 1360 (HackerRank limit)
```

Plus a static audit: ASCII-only, max line length 120, header byte-identical to `template.py`,
stdlib imports only, no unused constants.

**Adversarial testing.** 198,450 quotes across the cross-product of rates {0 … 100}, values
{1e-3 … 1e9}, tenors 0–30, capital 1–1e6, nine leg shapes and six strikes including negative and
1e12 — **zero invariant failures**. theo always in [0,1]; `bid < offer` always; quantities positive;
and never bid above fair or offer below fair. Across 48 full-session replays the bot's internal cash
shadow matched the exchange ledger **exactly**, with zero bankruptcies.

---

## Layout

```
bot.py                  the submitted market maker (stdlib only, <= 1360 lines)
template.py             provided skeleton; bot.py's header must match it byte for byte
ps.md                   problem statement
results_log.md          full version-by-version development log
HANDOFF.md              technical handoff: architecture, findings, open leads
snapshots/              every scored build, including the final 16.30
harness/                simulation and analysis tooling
data/                   test-case inputs and recorded market data (offline evaluation)
results/                grader output, verbatim
analysis/               plotting and exploratory notebooks/scripts
variants/               experimental per-session parameter search (not part of the submission)
scrapers/               one-off scripts used to collect the datasets in data/
```

### The harness

| file | purpose |
|---|---|
| [`exchange.py`](harness/exchange.py) | the matching rules in one place — quote collection, tie splitting, allocation |
| [`sim.py`](harness/sim.py) | synthetic sessions; the correctness gate |
| [`real_sim.py`](harness/real_sim.py) | sessions seeded from the real test cases |
| [`exchange_sim.py`](harness/exchange_sim.py) | replay: real cases, real flow, real opponents |
| [`opponents.py`](harness/opponents.py) | reconstructions of the rival bots, inferred from their names and results |
| [`arena.py`](harness/arena.py) | every saved version competing on one exchange |
| [`profile.py`](harness/profile.py) | what the bot actually traded, not what it earned |
| [`ledger.py`](harness/ledger.py) | turns a grader result into priced, reachable rank flips |

Harness modules put the repo root on `sys.path` themselves, so they run from any directory.

```sh
python3.13 harness/ledger.py results/results.txt    # priced, reachable rank flips
python3.13 harness/profile.py                       # fill composition
python3.13 harness/arena.py --seeds 40              # all versions head-to-head
```

---

## Honest notes

- **Test case 4 was never solved.** Seven structurally different changes — width, size, penny
  placement, sizing base, fill-or-kill gate — and the rival returned **exactly \$16.00 every single
  time**, with our \$15.47 unmoved to the cent. Its PnL is an integer count of free lots: it bids
  0.00 and offers 1.00, so it cannot lose. Nothing in our quoting touches it.
- **Known defect, documented not fixed:** `_quotes_won` counts *fills* rather than auctions won, so
  fill-or-kill fills can make the "nobody is competing" detector fire while auctions are being lost.
  The measured 16.30 was produced *by this code*, defect included; changing it would need its own
  graded re-measurement. Details in `HANDOFF.md`.
- **The unopposed-widening mechanism is the most field-specific thing in the bot.** It is worth
  roughly +1.00 here largely because one rival never competes on price. Against makers that do, it
  can trigger on an ordinary run of luck.

## Licence

MIT.
