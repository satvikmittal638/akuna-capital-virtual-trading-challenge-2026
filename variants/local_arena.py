"""Run our real bot (`bot.py`) against the local arena clones, on synthetic sessions.

Reuses `harness/sim.py`'s generator, contract stream and RFQ/FOK drivers, and `harness/exchange.py`
for matching. The only change from `sim.run_session` is the *field*: instead of two fixed-width
bots, our MarketMaker faces the clones in `arena_opponents.py`. Every maker prices off the same
warmed-up estimator, so this compares quoting policy, exactly as the harness does.

    python3.13 variants/local_arena.py            # 40 sessions, rank + PnL vs the arena field
    python3.13 variants/local_arena.py 120        # more sessions

This never connects anywhere. Live data was gathered once with the throwaway dummy; this is offline.
"""

from __future__ import annotations

import os
import random
import sys
from collections import defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "harness"))
sys.path.insert(0, os.path.join(_ROOT, "variants"))

import statistics

import arena_opponents
from bot import MarketHistory, MarketMaker, OptionLeg, Underlying
from bot import AJARAI_UNDERLYING_ID as AJR, FED_FUNDS_RATE_UNDERLYING_ID as FED, THERIODIC_UNDERLYING_ID as THR
from exchange import Account, Rotation
from sim import (Contract, _run_fok, _run_rfq, random_initial_values, random_parameters,
                 simulate_path, true_price, underlyings)

ALL_IDS = (FED, AJR, THR)
COMPANY_IDS = (AJR, THR)


def _session(seed: int, days: int, cash: float, history_days: int = 220) -> dict[str, float]:
    rng = random.Random(seed)
    parameters = random_parameters(rng)
    random.seed(seed * 7919 + 13)
    warm = simulate_path(parameters, random_initial_values(rng), history_days - 1)
    history = MarketHistory({uid: tuple(day[uid] for day in warm) for uid in ALL_IDS})
    path = simulate_path(parameters, warm[-1], days + 10)

    contracts: list[Contract] = []
    next_id = 1

    def add(day: int, values: dict[int, float], count: int) -> None:
        nonlocal next_id
        for _ in range(count):
            steps = rng.randint(1, 6)
            kind = rng.random()
            if kind < 0.35:
                legs = (OptionLeg(underlying_id=FED, weight=1.0),)
                strike = round(values[FED] + 0.25 * rng.randint(-2, 2), 2)
            elif kind < 0.80:
                uid = rng.choice(COMPANY_IDS)
                legs = (OptionLeg(underlying_id=uid, weight=1.0),)
                strike = round(values[uid] * 2.718281828 ** rng.uniform(-0.12, 0.12), 2)
            else:
                legs = (OptionLeg(underlying_id=AJR, weight=1.0), OptionLeg(underlying_id=THR, weight=-1.0))
                strike = 0.0
            contracts.append(Contract(option_id=next_id, legs=legs, strike=strike, expiry_day=day + steps))
            next_id += 1

    add(0, path[0], 10)
    initial = [c.at(0) for c in contracts]
    bot = MarketMaker(underlyings(path[0]), initial, cash)
    field = arena_opponents.full_field(cash)
    accounts = {m.name: Account(maker=m, cash=cash) for m in [bot, *field]}

    for clone in field:
        model = MarketMaker(underlyings(path[0]), initial, cash)
        model.warm_up(history)
        clone.attach(model)
        # `_Base.on_trade` mutates its own cash; give it the same account the exchange settles into.
        clone._cash = accounts[clone.name].cash
    for maker in [bot, *field]:
        maker.warm_up(history)
        maker.on_step_advance(underlyings(path[0]), initial)

    rotation = Rotation()

    def settlement(contract: Contract) -> float:
        return contract.at(contract.expiry_day).expiry_valuation(path[contract.expiry_day])

    for day in range(days):
        values = path[day]
        live = [c for c in contracts if c.expiry_day >= day]
        if not live:
            add(day, values, 4)
            live = [c for c in contracts if c.expiry_day >= day]
        for _ in range(rng.randint(2, 6)):
            _run_rfq(rng, parameters, accounts, rng.choice(live), day, values, settlement, rotation=rotation)
        for _ in range(rng.randint(1, 4)):
            _run_fok(rng, parameters, accounts, rng.choice(live), day, values, settlement, rotation=rotation)

        for contract in [c for c in contracts if c.expiry_day == day]:
            payoff = settlement(contract)
            for account in accounts.values():
                account.positions.pop(contract.option_id, None)
                account.cash += account.bought.pop(contract.option_id, 0) * payoff
                account.cash += account.sold.pop(contract.option_id, 0) * (1.0 - payoff)
        contracts = [c for c in contracts if c.expiry_day > day]
        for account in accounts.values():
            if not account.bankrupt and account.cash < 0.0:
                account.bankrupt = True
        add(day + 1, path[day + 1], rng.randint(2, 5))
        nxt = [c.at(day + 1) for c in contracts if c.expiry_day >= day + 1]
        for account in accounts.values():
            account.maker.on_step_advance(underlyings(path[day + 1]), nxt)

    final = path[days]
    out: dict[str, float] = {}
    for name, account in accounts.items():
        mark = 0.0
        for contract in contracts:
            fair = true_price(parameters, final, contract.at(days))
            mark += account.bought.get(contract.option_id, 0) * fair
            mark += account.sold.get(contract.option_id, 0) * (1.0 - fair)
        out[name] = (-cash if account.bankrupt else account.cash + mark - cash)
    out["__our_bankrupt__"] = 1.0 if accounts["Telescoping Theo"].bankrupt else 0.0
    return out


def main() -> int:
    sessions = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    cash = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
    pnl: dict[str, list[float]] = defaultdict(list)
    placements: list[int] = []
    bankruptcies = 0
    our = "Telescoping Theo"
    for i in range(sessions):
        result = _session(seed=1000 + i * 7, days=15, cash=cash)
        bankruptcies += int(result.pop("__our_bankrupt__", 0.0))
        for name, value in result.items():
            pnl[name].append(value)
        ours = result[our]
        placements.append(1 + sum(1 for k, v in result.items() if k != our and v > ours))

    field_size = len(pnl)
    print(f"\n{sessions} sessions, {field_size}-maker field, ${cash:g} start, 15 days each\n")
    print(f"  {'maker':<20}{'mean PnL':>10}{'median':>9}{'wins':>7}")
    for name in sorted(pnl, key=lambda n: -statistics.mean(pnl[n])):
        vals = pnl[name]
        wins = sum(1 for i in range(sessions)
                   if vals[i] >= max(pnl[o][i] for o in pnl if o != name))
        tag = "  <-- us" if name == our else ""
        print(f"  {name:<20}{statistics.mean(vals):>+10.2f}{statistics.median(vals):>+9.2f}"
              f"{wins:>5}/{sessions}{tag}")
    rank = statistics.mean(placements)
    hist = {r: placements.count(r) for r in range(1, field_size + 1)}
    print(f"\n  our rank distribution: " + "  ".join(f"#{r}:{hist[r]}" for r in range(1, field_size + 1)))
    print(f"  mean rank {rank:.2f} of {field_size}   first in {hist[1]}/{sessions}"
          f"   bankrupt {bankruptcies}/{sessions}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
