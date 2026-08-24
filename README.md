# Akuna Capital Virtual Trading Challenge 2026 — Binary Options Market Maker

A market-making bot for binary options on three correlated underlyings, together with the
simulation and analysis tooling built to develop it.

**Final score: 16.30 / 19** on the official grader — first place in 11 of the 16 scored sessions.

The submitted artefact is a single stdlib-only file, [`bot.py`](bot.py): 1360 lines, no third-party
dependencies, no file or network I/O, no Monte Carlo.

---

## Contents

- [The problem](#the-problem)
- [Strategy overview](#strategy-overview)
- [Part 1 — Exact pricing](#part-1--exact-pricing)
- [Part 2 — Estimating the parameters](#part-2--estimating-the-parameters)
- [Part 3 — Knowing what you don't know](#part-3--knowing-what-you-dont-know)
- [Part 4 — Quoting](#part-4--quoting)
- [Part 5 — Sizing and capital](#part-5--sizing-and-capital)
- [Part 6 — Fill-or-kill](#part-6--fill-or-kill)
- [Part 7 — Reading the opposition](#part-7--reading-the-opposition)
- [Part 8 — Robustness](#part-8--robustness)
- [Constants](#constants)
- [Results](#results)
- [What the engineering taught](#what-the-engineering-taught)
- [Verification](#verification)
- [Repository layout](#repository-layout)
- [Honest notes](#honest-notes)

---

## The problem

Market-make binary options ("event contracts") on three daily-evolving underlyings:

| symbol | what it is | process |
|---|---|---|
| `FED` | fed funds rate | mean-reverting jump chain on a 0.25 grid, floored at zero |
| `AJR` | AjarAI valuation | lognormal; shared sector shock + idiosyncratic noise + rate sensitivity |
| `THR` | Theriodic valuation | same form, its own betas |

Formally, each company evolves as

```
d log(V) = drift + rate_beta * dR + sector_beta * S + eps
```

with `S` a **sector shock common to both companies** (which is what makes them correlated), `eps`
idiosyncratic, and `dR` the realised change in the rate that day.

Each contract pays **1.0** if `weighted legs >= strike` at expiry and **0.0** otherwise. In practice
you see single-leg contracts (`THR >= 650`) and spreads (`THR - AJR >= 0`).

**Two order pipelines, demanding opposite things:**

- **RFQ** — the exchange asks every maker for a two-sided quote, then routes to best bid / best
  offer, **splitting the order across makers if it has to**. Critically, *you are not told which side
  the customer wants*, so both sides of your quote must stand on their own.
- **FOK** — full terms up front (side, price, quantity); you answer yes or no. If several makers
  accept, the order is split between them.

**Margin is the binding constraint, and it is brutal.** Every trade immediately debits its *maximum
loss*: buy 5 at 0.20 and your balance drops \$1.00; **sell** 5 at 0.20 and it drops \$4.00. Expiries
credit back, and solvency is checked at end of day. Go negative and the session ends early with a
bankruptcy.

**Scoring** is `0.4 + 0.6 * (n - rank) / (n - 1)` per session, `n` = number of makers. A single rank
flip is worth 0.60 in a two-maker session, 0.30 in a three-maker, 0.20 in a four-maker. **The score
is a step function — PnL that does not cross a rank boundary is worth exactly zero.**

---

## Strategy overview

The bot is built in layers, each of which feeds the next:

```
 warm-up history
        │
        ▼
 ┌──────────────────┐   estimate the generating process, with shrinkage
 │  PARAMETERS      │   → drift, rate_beta, variance, covariance, rate chain
 └────────┬─────────┘
          │
          ▼
 ┌──────────────────┐   exact Markov-DP over the terminal rate + normal CDFs
 │  THEO (price)    │   → no Monte Carlo, no sampling error
 └────────┬─────────┘
          │
          ▼
 ┌──────────────────┐   push each parameter by one standard error, combine in
 │  UNCERTAINTY     │   quadrature → how wrong could this price be?
 └────────┬─────────┘
          │
    ┌─────┴─────┐
    ▼           ▼
 QUOTE       FILL-OR-KILL         width, skew, size / accept-or-decline
    │           │
    └─────┬─────┘
          ▼
 ┌──────────────────┐   margin shadow, inventory, per-counterparty markouts
 │  RISK & FEEDBACK │   → feeds back into width and size next time
 └──────────────────┘
```

The guiding principle throughout: **quote a price you believe, then charge for how much you might be
wrong about it.** Everything else is bookkeeping around that.

---

## Part 1 — Exact pricing

This is the heart of the bot and the reason it prices to four decimal places rather than
approximately.

### The telescoping identity

The naive view is that a company's path depends on the whole *sequence* of rate moves, which would
force a joint simulation over rate paths and company paths. It doesn't. Summing the log-return over
`n` steps:

```
Σ rate_beta * dR_i  =  rate_beta * Σ dR_i  =  rate_beta * (R_n − R_0)
```

The sum **telescopes**. Only the *terminal* rate matters, not the path taken to reach it. And this
holds exactly at the zero floor too: when a down-draw is clamped by the floor, the rate doesn't move
and neither does `dR`, so the identity survives the non-linearity.

### What that buys

Conditional on the terminal rate `r`, the pair `(log A_n, log T_n)` is **bivariate normal**:

| | value |
|---|---|
| mean | `log V_0 + n * drift + rate_beta * (r − R_0)` |
| per-step variance | `(sector_beta * sigma_S)^2 + idio^2` |
| per-step covariance | `sector_beta_A * sector_beta_T * sigma_S^2` |

So pricing decomposes into two exact steps:

**1. The rate distribution** — [`_rate_distribution`](bot.py) runs an exact forward DP over the rate
chain. Each step, every reachable rate level spawns up / down / stay children with probabilities from
`transition()`, rounded onto the same 0.25 grid the exchange uses and floored at zero. After `n`
steps you hold the exact terminal distribution, typically 2n+1 states.

The transition mirrors the generator in intercept form, which is all that is identifiable:

```python
up   = clamp(alpha_up   − lam * R, 0, 1)
down = clamp(alpha_down + lam * R, 0, 1 − up)
```

**2. The company legs, conditional on each rate state:**

- **Single leg** — [`_single_leg_probability`](bot.py) is one normal CDF. It handles negative leg
  weights and the case where the implied threshold is non-positive (the leg then settles outright,
  certain if long and impossible if short).
- **Two legs** — [`_pair_probability`](bot.py). The spread actually traded, `THR − AJR >= 0`, has
  `strike ≈ 0` with opposite-signed weights, so it reduces to a **log ratio** and is closed-form
  exact — one CDF on the difference of two normals. For the general case it integrates over one leg
  with a 112-node Simpson quadrature and uses the fact that the condition on the second leg is
  monotone, so the inner probability is again a single CDF.

Total cost is one CDF per rate state (a quadrature for two legs) — about **2 ms** for a single-leg
contract and **13 ms** for a spread at ten days. No sampling, therefore no sampling error.

**Result: the grader's pricing gate passes to four decimals on all six contracts, spreads included.**

### The zero-step ambiguity

Whether a contract with `steps_until_expiry == 0` settles on today's values or after one more move
is unspecified. Rather than guess, [`_reference_prices`](bot.py) prices it **both ways** and returns
the bracket `(min, max)`. The bid must stay below the low end and the offer above the high end. The
bot gives up the ambiguous middle rather than hand away what only *looks* like free money.

---

## Part 2 — Estimating the parameters

In the live sessions the parameters are hidden; you get roughly 14 days of history and must infer
them. Fourteen points is very little, so every estimate is shrunk toward a prior by its own standard
error.

### The rate chain — [`_estimate_rate_process`](bot.py)

The true up/down probabilities are linear in the rate level before clamping, so the intercept form
comes from least squares on the **move indicators**, sharing a single slope `lam`:

```
up(R)   = alpha_up   − lam * R
down(R) = alpha_down + lam * R
```

Two details matter:

- **A ridge penalty.** A ten-day warm-up that sits on two neighbouring grid points has almost no
  variation in `R`, and the raw slope explodes. The denominator carries `+ _RATE_REVERSION_RIDGE`,
  which dominates when the history is flat and fades once it genuinely spans levels. Reversion is
  then clamped to `[0, 0.35]`.
- **A Laplace-style nudge.** A run that happens to contain no up-move would otherwise freeze that
  direction out entirely, assigning it probability zero. The nudge keeps both directions alive.

A down-move at a zero rate is indistinguishable from no move, so those rows are excluded from the
down regression only.

### The companies — [`_estimate_company_process`](bot.py)

Both companies are regressed on the same design matrix `[1, dR]`, giving drift and rate beta per
company, plus residuals for variance and covariance.

- **Student-t variance inflation.** With `df = n − 2` residuals the variance estimate is itself
  noisy, so it is inflated by `df / (df − 2)` to match the predictive t variance.
- **Correlation shrinkage toward 0.75.** This prior is not arbitrary: both companies load on one
  sector shock at beta ≈ 1, which implies ≈ 0.767. The error lands squarely in spread pricing, where
  `v_A + v_T − 2·cov` is acute, so it is shrunk hard.
- **Drift and rate-beta shrinkage.** Each is pulled toward its prior with Bayesian weight
  `prior_var / (prior_var + estimation_error)`. Twenty days of a company up 60% implies 2.5% a day —
  that is a lucky run, and without shrinkage it gets baked into every subsequent price.

### Folding estimation error into the price itself

This is the subtle part. [`effective_steps`](bot.py) exploits the fact that **integrating a normal
CDF over a normally-distributed mean gives another normal CDF**. So uncertainty in `(drift,
rate_beta)` enters *exactly* as extra variance — expressed as additional diffusion steps:

```
extra = n²·m00 + 2·n·dR·m01 + dR²·m11        (m = inv(X'X), the OLS mean covariance)
effective_steps = n + clamp(extra, 0, 4n)
```

The price is therefore the true **posterior mean**, not a plug-in estimate. A short or uninformative
history automatically produces a price closer to 0.5.

Against the one parameter set the grader reveals, two priors check out: `_CORRELATION_PRIOR_MEAN`
0.75 vs a true 0.767, and `_RATE_BETA_PRIOR_MEAN` −0.020 vs −0.020 / −0.015.

---

## Part 3 — Knowing what you don't know

[`_uncertainty`](bot.py) answers a different question from `effective_steps`: not "what is the
posterior mean?" but **"how far could that mean sit from the truth?"** That number sets the spread.

Each error source is pushed **one standard error in both directions**, the option is repriced, the
larger move is taken, and the sources are combined in quadrature (they are largely independent):

| source | perturbation |
|---|---|
| volatility level | scale variance by `(1 ± vol_relative_error)²` |
| rate up-probability | shift `alpha_up` by ± its standard error |
| rate down-probability | shift `alpha_down` by ± its standard error |
| each company's drift | ± 1 standard error |
| each company's rate beta | ± 1 standard error |

Drift and beta are pushed **separately per company** — moving both adversely at once assumes the
errors conspire, which overstates the risk badly on short-dated rate contracts.

The mechanism is measurable and nobody tuned it:

| | uncertainty | quoted width |
|---|---|---|
| never warmed up (maximum ignorance) | 0.1895 | **0.20** |
| warmed up on full history | 0.1131 | **0.14** |

Less knowledge ⇒ wider quote, automatically, straight out of the estimator's own standard errors.

---

## Part 4 — Quoting

[`_build_quote`](bot.py) assembles a two-sided market in five stages.

### 1. Width

```python
half_spread = clamp(base + multiplier * uncertainty, MIN, MAX)
              + toxicity_edge(counterparty)
              + adverse_selection_edge()
```

The uncertainty multiplier is deliberately **below 1.0** (0.40). Winning is bad news: a counterparty
lifts your offer precisely when your error made it cheap, so the charge must exceed zero but need not
cover the full error — you only pay it on the trades you actually lose.

Crucially, the quote path and the fill-or-kill path have **separate base constants**
(`_QUOTE_BASE_HALF_SPREAD` 0.02 vs `_FOK_BASE_HALF_SPREAD` 0.05). They were once a single constant,
and that coupling is discussed in [What the engineering taught](#what-the-engineering-taught).

### 2. Inventory skew

[`_inventory_skew`](bot.py) leans the *whole market* rather than refusing one side, so the bot stays
two-sided while becoming keener to trade out of risk.

Exposure is tracked per underlying, weighted by `4p(1−p)` — a cheap proxy for how at-the-money a
contract is, and hence how much it actually moves with the underlying. A deep in-the-money contract
carries little live risk no matter its size.

```python
raw  = 0.8 * aggregate_underlying_exposure * sensitivity / exposure_scale
     + 0.6 * position_in_this_contract     / contract_position_cap
skew = −half_spread * clamp(raw, −1.5, 1.5)
```

Long inventory pushes both bid and offer down — cheaper to buy from us, dearer to sell to us.

### 3. The safety rail

```python
bid_price   = min(low_reference  − half_spread + skew, low_reference)
offer_price = max(high_reference + half_spread + skew, high_reference)
```

The outer `min`/`max` are the invariant that everything else rests on: **the skew modulates keenness
but can never push the bid above fair or the offer below it.** Every fill is priced to make money in
expectation. Verified across 198,450 adversarial quotes — see [Verification](#verification).

### 4. Penny rounding and the boundary

Prices round outward — bid floors, offer ceilings — so rounding never eats the edge. The offer is
forced at least one penny above the bid.

A market quoted at `0.00 / 1.00` does not *beat* a rival quoting the boundary, it **ties** it, and
the exchange then splits the order. When the bot has won every auction so far, it steps one penny
inside to take the whole order instead of half — but only where fair value permits, so it still
never trades through theo.

### 5. Fallbacks

If margin leaves no room on a side, that side is quoted at the riskless boundary (bid `0.00` or offer
`1.00`) for one lot rather than being withdrawn. Bidding zero costs nothing and offering one cannot
lose, so the bot always shows a legal two-sided market.

---

## Part 5 — Sizing and capital

Because each trade debits its **maximum loss**, size is a capital-allocation problem, not a
confidence expression.

### Capacity

```python
capacity              = initial_cash / 0.5          # contracts of typical margin the balance supports
max_quote_size        = 0.60 * capacity
contract_position_cap = 0.75 * capacity
```

A "typical" contract near fair costs 0.5 of margin, so capacity is the number of such contracts the
account can carry. All limits are expressed as fractions of it, which makes the bot scale correctly
across the \$10 / \$20 / \$40 sessions without per-session tuning.

### Per-side quantity — [`_side_quantity`](bot.py)

```python
quantity = budget / margin_per_contract      # what margin affords at THIS price
quantity = min(quantity, lot_cap) * size_factor
quantity = min(quantity, position_room)
```

Three independent limiters, each doing a different job:

| limiter | protects against |
|---|---|
| `budget / margin_per_contract` | insolvency — dollars actually at risk |
| `lot_cap` | concentration in a single request |
| `position_room` | accumulating one-way exposure in one contract |

`budget` is half the available margin (`_QUOTE_MARGIN_FRACTION`), where available margin already
holds back a 5% cash buffer. Note that margin per contract is **price-dependent**: a bid at 0.05
risks a twentieth of a bid at fair, so the same dollar budget buys twenty times the lots near a
boundary.

`size_factor` scales down for uncertainty (`1 / (1 + 6·uncertainty)`) and again for counterparties
that have been beating us.

---

## Part 6 — Fill-or-kill

[`_evaluate_fok`](bot.py) faces a different problem from quoting: the terms are known, so it is a
pure accept/decline on **edge versus capital consumed**. It runs four gates in order.

**1. Edge over a hurdle.** Edge is measured against the conservative end of the reference bracket:

```python
buying:  edge = low_reference − price
selling: edge = price − high_reference

required = MIN_FOK_EDGE + FOK_UNCERTAINTY_MULTIPLIER * uncertainty + toxicity_edge
required = max(0.5 * MIN_FOK_EDGE, required − direction * skew)
```

The fill-or-kill multiplier (0.75) is nearly twice the quote multiplier (0.40), because a fill-or-kill
is **strictly adversely selected** — the counterparty chose the contract, the side, the price and the
size, and only sends it if they like it. The inventory skew relaxes the hurdle in the direction that
reduces risk.

**2. Return on margin.** Positive edge is not enough when margin is scarce:

```python
utilisation = clamp(1 − available_margin / initial_cash, 0, 1)
hurdle      = half_spread * (0.5 + 1.5 * utilisation)
reject if edge / margin_per_contract < hurdle
```

Eight contracts at 0.99 for one cent of edge is a penny of profit against 1% of half the balance —
fine when idle, terrible when capital is working. The hurdle rises as the account fills up.

**3. Affordability in full.** An acceptance can be allocated the *entire* order, so the full margin
must be available (capped at `_FOK_MARGIN_FRACTION`), not just the expected share.

**4. Position limits**, in the direction that would increase exposure.

### Self-correcting side convention

`FokOrder.order_type` is documented as the **counterparty's** side, so accepting a `BUY` means *we
sell*. Getting that backwards would be catastrophic — every order bought that should have been sold.

Rather than trust the reading, the bot **verifies it against reality**. On acceptance it records the
expected direction in `_pending_fok`; when the resulting fill arrives, `_match_pending_fok` compares
the actual signed quantity to what was expected. Three disagreements with zero agreements flips the
convention. One agreement settles it permanently — an inverted convention is far more dangerous than
a mistaken flip, so the bar for flipping is high and the bar for stopping is low.

The accepted quantity joins the matching key, because contract and price alone cannot distinguish a
fill-or-kill fill from an ordinary quote fill on the same contract at the same penny.

---

## Part 7 — Reading the opposition

Three feedback loops, all built on **markouts** rather than settled payoffs.

Why markouts: a binary payoff is a coin flip worth about 0.5 per contract in noise, which drowns an
edge of a few pennies. The next day's theo is a *conditional expectation* and far less noisy, so it
detects a systematic loser in a handful of trades rather than hundreds.

### Per-counterparty toxicity

[`_counterparty_pnl_per_contract`](bot.py) does **not** use the mean. It uses

```python
mean + confidence_multiple * standard_error
```

— the **optimistic** end of the interval for our own profit. A counterparty is only called toxic when
the losses are too consistent to be luck. The mean alone misjudges ordinary counterparties often
enough to cost more than it saves.

Once a name is flagged it is charged more than the observed loss (`1.5 ×`, capped at 0.25) on the
theory that whoever has been beating you by that much will continue to, and simultaneously quoted
smaller via `1 / (1 + 25 · loss)`.

### Session-wide adverse selection

Per-counterparty statistics need a dozen trades against *one* name. Two situations don't fit that
shape, so [`_adverse_selection_edge`](bot.py) handles them at session level:

- **Our own fills are losing money** across everybody — widen proportionally, capped.
- **Nobody is competing** — if every decided auction has been won, no rival has ever priced better,
  and the spread is ours to set. The bar is *every* auction, not most; a single loss ends it for the
  rest of the session.

---

## Part 8 — Robustness

The bot must never crash the grader and must never overdraw.

- **Every public method is exception-wrapped** with a safe fallback: `quote` → `0.00/1.00` for one
  lot (riskless on both sides), `respond_to_fok` → decline, `price_option` → 0.5.
- **The margin shadow mirrors the exchange exactly.** `_record_trade` debits maximum loss on every
  fill; `_settle_expired` credits back per trade leg, deliberately taking `min(payoff,
  previous_payoff)`. Trades are released **individually rather than netted**, because netting a
  profitable round trip would destroy cash the exchange has not actually released. Measured across
  48 full-session replays: the shadow matched the exchange ledger **exactly**, zero drift, zero
  bankruptcies.
- **Caching.** Prices and uncertainties are memoised on `(contract, underlying-state)` and cleared
  every step; exposure is cached on `(state, trade counter)`. This is what keeps a spread at 13 ms.

---

## Constants

Roughly 46 distinct numeric values, in three tiers:

**Structural** — forced by the rules or the mathematics. The 0.25 rate grid, 100 pennies to the
dollar, 0.5 as the margin of a fair contract, the quadrature node count.

**Theory-derived** — checkable against evidence. `_CORRELATION_PRIOR_MEAN` 0.75 (implied ≈ 0.767 by
two unit sector betas), `_RATE_BETA_PRIOR_MEAN` −0.020.

**Tuned** — chosen by measurement.

| constant | value | role |
|---|---|---|
| `_QUOTE_BASE_HALF_SPREAD` | 0.02 | floor width on the quote path |
| `_FOK_BASE_HALF_SPREAD` | 0.05 | floor width on the fill-or-kill path |
| `_QUOTE_UNCERTAINTY_MULTIPLIER` | 0.40 | how much of our own error to charge when quoting |
| `_FOK_UNCERTAINTY_MULTIPLIER` | 0.75 | ditto for a strictly adverse-selected order |
| `_MAX_HALF_SPREAD` / `_MAX_TOTAL_HALF_SPREAD` | 0.15 / 0.50 | model cap, and the cap once toxicity is added |
| `_QUOTE_SIZE_FRACTION` / `_POSITION_CAP_FRACTION` | 0.60 / 0.75 | fractions of capacity |
| `_QUOTE_MARGIN_FRACTION` / `_FOK_MARGIN_FRACTION` | 0.50 / 0.30 | share of free margin per decision |
| `_CASH_BUFFER_FRACTION` | 0.05 | never spend the last 5% |
| `_MIN_TOXICITY_TRADES` / `_TOXICITY_CONFIDENCE` | 12 / 2.0 | evidence bar before a name is called toxic |
| `_MIN_QUOTE_SAMPLE` / `_UNOPPOSED_EDGE` | 6 / 0.45 | unopposed detection and the width it triggers |

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

Scored subtotal **13.30** + 3.00 (pass/fail sessions) = **16.30 / 19**.

Progression: 15.10 → 15.30 → **16.20** → **16.30**.

---

## What the engineering taught

Recorded in full in [`results_log.md`](results_log.md) — roughly 1700 lines covering every version
and every graded result. The findings that generalise beyond this competition:

**One constant feeding two mechanisms hides every cause.** `_half_spread` originally set *both* the
quote width and the fill-or-kill hurdle. Every experiment moved both at once and measured their sum,
so five of the first six experiments were uninterpretable. Splitting it into two constants was worth
**+0.90** — the single largest gain in the project, and it was a decoupling, not a better number.

**A step-function score punishes optimising the wrong quantity.** One version raised PnL by **\$10.01
and scored 0.00**, because none of the gains crossed a rank boundary. [`harness/ledger.py`](harness/ledger.py)
exists to price the distance to the next rank *before* any PnL change is valued.

**Pricing was not the bottleneck — decisions were.** Scored against realised settlement on 189
decisions whose contract terms are known:

| predictor | Brier score |
|---|---|
| our theo | **0.1017** |
| the base rate | 0.2382 |
| always 0.5 | 0.2500 |

A skill score of 0.57, roughly at par with the counterparties' own prices (0.1025 vs 0.1119). And
decisively: Brier in sessions we **lose** is **0.0911** versus **0.1078** in sessions we **win** — we
price *better* where we lose. One session had the worst pricing measured (Brier 0.6968) and was won
outright; another was near-perfect (0.0003) and lost.

**Calibration exposed a real modelling gap.** Both tails are too narrow — the model says 0.984 and it
happens 0.873 (n=63, 2.6σ); says 0.006 and it happens 0.026. The structural cause is identifiable:
`effective_steps` integrates over uncertainty in the *mean*, but nothing integrates over uncertainty
in the *variance*.

**A simulator that cannot resolve the question must not be allowed to answer it.** The replay harness
carries \$14–19 of PnL error and reproduces rank at roughly chance. Two changes tuned against it cost
**−0.80** and **−1.50** on the real grader. The rule adopted afterwards, and kept: *a change ships
only if its mechanism is justified without harness calibration.*

---

## Verification

```sh
python3.13 harness/sim.py                                        # must print ALL CHECKS PASSED
python3.13 -c "import sys; sys.path.insert(0,'harness'); \
  import real_sim; print(real_sim.check_theo_case())"            # must print True
wc -l bot.py                                                     # <= 1360 (HackerRank limit)
```

Plus a static audit: ASCII-only, max line length 120, header byte-identical to `template.py`,
stdlib imports only, no unused constants.

**Adversarial testing.** 198,450 quotes across the cross-product of rates {0 … 100}, values
{1e-3 … 1e9}, tenors 0–30, capital 1–1e6, nine leg shapes (single, spread, short, non-unit weights,
mixed rate+company, unknown underlying) and six strikes including negative and 1e12 —
**zero invariant failures**:

- theo always in `[0, 1]`, never NaN
- `bid < offer` always; quantities always positive
- **never bid above fair, never offer below fair**

Across 48 full-session replays the internal cash shadow matched the exchange ledger exactly, with
zero bankruptcies.

---

## Repository layout

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
analysis/               plotting and exploratory scripts
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
| [`opponents.py`](harness/opponents.py) | reconstructions of the rival bots, inferred from names and results |
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

- **Session 4 was never solved.** Seven structurally different changes — width, size, penny
  placement, sizing base, fill-or-kill gate — and the rival returned **exactly \$16.00 every single
  time**, with our \$15.47 unmoved to the cent. Its PnL is an integer count of free lots: it bids
  0.00 and offers 1.00, so it can never lose, only fail to win. Nothing in our quoting touches it.
- **Known defect, documented rather than fixed:** `_quotes_won` counts *fills* rather than auctions
  won, so fill-or-kill fills can make the "nobody is competing" detector fire while auctions are
  being lost. One accepted fill-or-kill masks two lost auctions. The measured 16.30 was produced *by
  this code*, defect included, so changing it would require its own graded re-measurement.
- **The unopposed-widening mechanism is the most field-specific part of the bot.** It is worth
  roughly +1.00 here largely because one rival never competes on price. Against makers that do, a
  run of six wins is ordinary luck rather than evidence, and it can trigger on noise.
- **`_DRIFT_PRIOR_MEAN` is 4× the one revealed truth** (0.005 vs 0.001–0.0015). That is
  mis-specification rather than overfitting, and it is worth only ~0.0015 of Brier because fourteen
  days of data carry most of the posterior weight.

## Licence

MIT.
