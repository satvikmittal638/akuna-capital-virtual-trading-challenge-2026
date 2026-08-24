# Live arena: opponent reconnaissance and local clones

The arena at `wss://akuna.server.strixthekiet.me/ws` broadcasts every maker's live order book to
all connected clients. So a **spectator dummy** — a maker that quotes the riskless 0.00/1.00 and
declines every FOK (`arena_dummy_maker.py`, run via `arena_client.py`) — sees all competitors'
quotes while revealing nothing of our own. That is the only thing ever connected; `bot.py` and its
strategy never touch the wire.

## Toolchain

| file | role |
|---|---|
| `arena_dummy_maker.py` | the throwaway maker (copy to the client dir as `main.py` to spectate) |
| `parse_arena.py` | reduces a capture to per-maker quoting stats + match standings |
| `arena_opponents.py` | local clones of the observed bots (subclass `harness/opponents._Base`) |
| `local_arena.py` | runs our real `bot.py` against the clone field on synthetic sessions |

## The field, mapped (4 matches, ~980 book rows)

| bot | nature | half-spread (med / range) | size | FOK | result |
|---|---|---|---|---|---|
| **3 Rings** | tight + big size, **adaptive** width | 0.05 / 0.005–0.48 | to 400 | yes | **dominant: +23 PnL, 0.94 pts** |
| **Ar4yu** | size-1000 **boundary flooder** (Stalemate-like) | 0.5 / 0.015–0.5 | ~1000 | some | middling, +1 |
| **HelloWorld** | very wide, **adaptive**, high variance | 0.125 / 0.025–0.5 | ~8 | rarely | won 1 match big, else low |
| **quantifyer** | medium width, medium size | 0.09 / 0.05–0.5 | 4–50 | some | middle |
| **Lattice** | very tight, size 1, trusts model | 0.025 / flat | 1 | yes | high volume, slightly −ve |
| **Fixed Width 0.05** | stated policy, tight, size 1 | 0.025 / 0.015–0.03 | 1 | yes | picked off, ~last |

**Key finding:** the three strongest bots (3 Rings, Ar4yu, HelloWorld) all show enormous
width *ranges* — they are **adaptive**, widening and tightening with the flow. That is the same
competitive-width search generalized into `variants/out/general.py`. The tight fixed-width makers
(Lattice, Fixed Width) get picked off by the arena's ~35% informed flow and finish last.

## Caveat on the local runner

`local_arena.py` reuses `harness/sim.py`'s synthetic session generator, whose counterparty flow is
**not** the arena's "35% informed" model. So it reproduces each bot's *quoting policy* faithfully
but not the arena's placement — in the live arena 3 Rings wins by +23; in these synthetic sessions
it lands mid-pack. Use the local arena to (a) confirm our bot never blows up against these policies,
and (b) A/B our own variants against a fixed field — **not** to predict live standings.
