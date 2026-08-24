"""Play our candidate bots against each other and against the arena field.

`bot.py` is read, never modified. Each candidate is a self-contained maker file (data model +
MarketMaker). They all self-report the name "Telescoping Theo", but the exchange keys accounts by
the label we assign, so several can share one field. Options and underlyings flow as `bot`'s
classes and every maker reads them by attribute -- structural typing, so a candidate's own Quote
class comes back out fine.

Reuses `harness/sim.py`'s generator + RFQ/FOK drivers and `harness/exchange.py`'s matcher, exactly
as `local_arena.py` does. Every maker prices off its own warm-up; the comparison is of policy.

    python3.13 variants/tournament.py ours          # our candidates head-to-head
    python3.13 variants/tournament.py arena         # each candidate vs the 6 arena clones
    python3.13 variants/tournament.py ours 300 20   # sessions, cash
"""

from __future__ import annotations

import importlib.util
import os
import random
import statistics
import sys
from collections import defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "harness"))
sys.path.insert(0, os.path.join(_ROOT, "variants"))

import arena_opponents
import bot as canon
from bot import MarketHistory, OptionLeg
from bot import AJARAI_UNDERLYING_ID as AJR, FED_FUNDS_RATE_UNDERLYING_ID as FED, THERIODIC_UNDERLYING_ID as THR
from exchange import Account, Rotation
from sim import (Contract, _run_fok, _run_rfq, random_initial_values, random_parameters,
                 simulate_path, true_price, underlyings)

ALL_IDS = (FED, AJR, THR)
COMPANY_IDS = (AJR, THR)

CANDIDATES = {
    "base-16.30":  "bot.py",
    "BEST_17.20":  "variants/out/BEST_17.20.py",
    "general_v2":  "variants/out/general_v2.py",
    "general_v3":  "variants/out/general_v3.py",
    "fok_take":    "variants/out/fok_take.py",
}


def _load(path: str):
    name = "cand_" + os.path.basename(path).replace(".", "_")
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolves KW_ONLY via sys.modules[cls.__module__]
    spec.loader.exec_module(module)
    return module


_MODULES = {label: _load(path) for label, path in CANDIDATES.items()}


class _Bot:
    """A candidate maker file, wrapped as a field participant."""
    def __init__(self, label: str, module) -> None:
        self.label, self._module = label, module

    def build(self, u0, o0, cash, history):
        maker = self._module.MarketMaker(u0, o0, cash)
        maker.warm_up(history)
        return maker


class _Clone:
    """An arena clone, wrapped so it prices off a warmed canonical model."""
    def __init__(self, label: str, factory) -> None:
        self.label, self._factory = label, factory

    def build(self, u0, o0, cash, history):
        clone = self._factory(cash)
        model = canon.MarketMaker(u0, o0, cash)
        model.warm_up(history)
        clone.attach(model)
        clone.warm_up(history)
        clone._cash = cash
        return clone


def _session(specs: list, seed: int, days: int, cash: float, history_days: int = 220) -> dict:
    rng = random.Random(seed)
    parameters = random_parameters(rng)
    random.seed(seed * 7919 + 13)
    warm = simulate_path(parameters, random_initial_values(rng), history_days - 1)
    history = MarketHistory({uid: tuple(day[uid] for day in warm) for uid in ALL_IDS})
    path = simulate_path(parameters, warm[-1], days + 10)

    contracts: list[Contract] = []
    next_id = 1

    def add(day, values, count):
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
    u0 = underlyings(path[0])
    makers = {spec.label: spec.build(u0, initial, cash, history) for spec in specs}
    accounts = {label: Account(maker=maker, cash=cash) for label, maker in makers.items()}
    for maker in makers.values():
        maker.on_step_advance(u0, initial)

    rotation = Rotation()

    def settlement(contract):
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
            for label, account in accounts.items():
                bought = account.bought.pop(contract.option_id, 0)
                sold = account.sold.pop(contract.option_id, 0)
                account.positions.pop(contract.option_id, None)
                account.cash += bought * payoff + sold * (1.0 - payoff)
                maker = makers[label]
                if hasattr(maker, "credit"):
                    maker.credit(contract.option_id, payoff, bought, sold)
        contracts = [c for c in contracts if c.expiry_day > day]
        for account in accounts.values():
            if not account.bankrupt and account.cash < 0.0:
                account.bankrupt = True
        add(day + 1, path[day + 1], rng.randint(2, 5))
        nxt = [c.at(day + 1) for c in contracts if c.expiry_day >= day + 1]
        un = underlyings(path[day + 1])
        for maker in makers.values():
            maker.on_step_advance(un, nxt)

    final = path[days]
    out = {}
    for label, account in accounts.items():
        mark = 0.0
        for contract in contracts:
            fair = true_price(parameters, final, contract.at(days))
            mark += account.bought.get(contract.option_id, 0) * fair
            mark += account.sold.get(contract.option_id, 0) * (1.0 - fair)
        out[label] = (-cash, True) if account.bankrupt else (account.cash + mark - cash, False)
    return out


def _run(specs: list, sessions: int, cash: float, title: str) -> None:
    labels = [s.label for s in specs]
    pnl = defaultdict(list)
    bankrupt = defaultdict(int)
    placements = defaultdict(list)
    for i in range(sessions):
        result = _session(specs, seed=4000 + i * 7, days=15, cash=cash)
        for label, (value, bust) in result.items():
            pnl[label].append(value)
            bankrupt[label] += int(bust)
        for label in labels:
            mine = result[label][0]
            placements[label].append(1 + sum(1 for o in labels if o != label and result[o][0] > mine))

    n = len(labels)
    print(f"\n=== {title}: {sessions} sessions, {n}-maker field, ${cash:g} start ===\n")
    print(f"  {'maker':<14}{'mean PnL':>10}{'median':>9}{'wins':>8}{'mean rank':>11}{'bankrupt':>10}")
    for label in sorted(labels, key=lambda x: -statistics.mean(pnl[x])):
        vals = pnl[label]
        wins = sum(1 for i in range(sessions)
                   if vals[i] >= max(pnl[o][i] for o in labels if o != label))
        star = "  <" if label in ("base-16.30",) else ""
        print(f"  {label:<14}{statistics.mean(vals):>+10.2f}{statistics.median(vals):>+9.2f}"
              f"{wins:>6}/{sessions}{statistics.mean(placements[label]):>11.2f}{bankrupt[label]:>7}/{sessions}{star}")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "ours"
    sessions = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    cash = float(sys.argv[3]) if len(sys.argv) > 3 else 20.0

    if mode == "ours":
        specs = [_Bot(label, _MODULES[label]) for label in CANDIDATES]
        _run(specs, sessions, cash, "our candidates head-to-head")
    elif mode == "arena":
        clones = [_Clone(name, (lambda c, n=name: arena_opponents.build(n, c)))
                  for name in arena_opponents.ROSTER]
        for label in CANDIDATES:
            specs = [_Bot(label, _MODULES[label]), *clones]
            _run(specs, sessions, cash, f"{label} vs the arena field")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
