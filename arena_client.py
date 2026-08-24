import asyncio
import importlib.util
import json
import os
import pathlib
import sys
import threading

import websockets

ARENA_URL = os.environ.get("ARENA_URL", "wss://akuna.server.strixthekiet.me/ws")
MAKER_FILE = "main.py"
MODEL = ("BinaryOption", "OptionLeg", "FokOrder", "OrderType", "MarketHistory", "Underlying")


class Client:
    def __init__(self, namespace, name):
        self.maker_class = namespace["MarketMaker"]
        self.name = name
        for attribute in MODEL:
            setattr(self, attribute, namespace[attribute])
        self.maker = self.socket = None
        self.lock = asyncio.Lock()
        self.quitting = False

    def option(self, payload):
        return self.BinaryOption(**{**payload,
                                    "legs": tuple(self.OptionLeg(**leg)
                                                  for leg in payload["legs"])})

    def underlying(self, payload):
        return self.Underlying(**payload)

    def fok(self, payload):
        return self.FokOrder(**{**payload, "order_type": self.OrderType(payload["order_type"])})

    def dispatch(self, method, args):
        if method == "init":
            self.maker = self.maker_class([self.underlying(u) for u in args["underlyings"]],
                                          [self.option(o) for o in args["options"]], args["cash"])
        elif self.maker is None:
            raise RuntimeError("the exchange called before the match was set up")
        elif method == "warm_up":
            self.maker.warm_up(self.MarketHistory(
                {int(key): tuple(values) for key, values in args["history"].items()}))
        elif method == "quote_batch":
            return [self.attempt(self.quote, request, None) for request in args["requests"]]
        elif method == "fok_batch":
            return [self.attempt(self.decide, request, False) for request in args["requests"]]
        elif method == "trade_batch":
            for trade in args["trades"]:
                self.maker.on_trade(self.option(trade["option"]), trade["price"],
                                    trade["quantity"], trade["counterparty_id"])
        elif method == "step_advance":
            self.maker.on_step_advance([self.underlying(u) for u in args["underlyings"]],
                                       [self.option(o) for o in args["options"]])
        else:
            raise RuntimeError(f"unknown method {method!r}")

    def attempt(self, call, request, fallback):
        try:
            return call(request)
        except Exception as error:
            self.say(f"  your maker raised on option {request['option']['option_id']}: "
                     f"{type(error).__name__}: {error}")
            return fallback

    def quote(self, request):
        quote = self.maker.quote(self.option(request["option"]), request["counterparty_id"])
        return quote and {"bid_price": float(quote.bid_price),
                          "bid_quantity": int(quote.bid_quantity),
                          "offer_price": float(quote.offer_price),
                          "offer_quantity": int(quote.offer_quantity)}

    def decide(self, request):
        return bool(self.maker.respond_to_fok(self.option(request["option"]),
                                              self.fok(request["fok"])))

    @staticmethod
    def say(text):
        print(text, flush=True)

    async def send(self, message):
        if self.socket is not None:
            await self.socket.send(json.dumps(message))

    async def answer(self, message):
        try:
            async with self.lock:
                value = await asyncio.to_thread(self.dispatch, message["method"],
                                                message.get("args") or {})
            reply = {"ok": True, "value": value}
        except Exception as error:
            self.say(f"  your {message['method']} raised {type(error).__name__}: {error}")
            reply = {"ok": False, "error": f"{type(error).__name__}: {error}"}
        await self.send({"type": "result", "id": message["id"], **reply})

    async def handle(self, message):
        if message.get("type") == "call":
            asyncio.create_task(self.answer(message))
        elif message.get("lines"):
            self.say("\n".join(message["lines"]))
        elif message.get("message"):
            self.say(f"  {message['message']}")

    async def run(self):
        hello = {"type": "hello", "name": self.name, "maker": self.maker_class.__name__,
                 "client": "terminal", "colour": sys.stdout.isatty()}
        self.say(f"Akuna arena -- connecting to {ARENA_URL} as {self.name}")
        while not self.quitting:
            try:
                async with websockets.connect(ARENA_URL, ping_interval=20,
                                              ping_timeout=60, max_size=32 << 20) as socket:
                    self.socket = socket
                    await self.send(hello)
                    async for raw in socket:
                        await self.handle(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if self.quitting:
                    return
                self.say(f"  connection lost ({error}); retrying in 5s")
                await asyncio.sleep(5)

    async def keyboard(self):
        queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def pump():
            for line in sys.stdin:
                loop.call_soon_threadsafe(queue.put_nowait, line)

        threading.Thread(target=pump, daemon=True).start()
        keys = {"": {"type": "join"}, "y": {"type": "join"}, "yes": {"type": "join"},
                "a": {"type": "join", "always": True},
                "always": {"type": "join", "always": True},
                "n": {"type": "leave"}, "no": {"type": "leave"}}
        while not self.quitting:
            key = (await queue.get()).strip().lower()
            if key in keys:
                await self.send(keys[key])
            elif key in ("q", "quit", "exit"):
                self.quitting = True
                self.say("  leaving the arena")
                if self.socket is not None:
                    await self.socket.close()
                return
            else:
                self.say("  y = join, n = spectate, a = always join, q = quit")


def load_maker():
    path = pathlib.Path(__file__).resolve().with_name(MAKER_FILE)
    if not path.is_file():
        raise SystemExit(f"expected your market maker at {path}")

    spec = importlib.util.spec_from_file_location("arena_maker", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["arena_maker"] = module
    spec.loader.exec_module(module)

    missing = [name for name in (*MODEL, "MarketMaker") if not hasattr(module, name)]
    if missing:
        raise SystemExit(f"{path} is missing {', '.join(missing)}. It must hold the exchange "
                         f"data model and your MarketMaker in one file.")
    return module.__dict__


def maker_name(maker_class):
    name = getattr(maker_class, "name", None)
    if isinstance(name, property):
        try:
            name = name.fget(maker_class.__new__(maker_class))
        except Exception:
            name = None
    if not isinstance(name, str) or not name.strip():
        raise SystemExit('Your MarketMaker needs a name for the arena. Add one to the class:\n\n'
                         '    class MarketMaker:\n'
                         '        name = "yourname"\n')
    return name.strip()


async def play(client):
    await asyncio.gather(client.run(), client.keyboard())


if __name__ == "__main__":
    namespace = load_maker()
    try:
        asyncio.run(play(Client(namespace, maker_name(namespace["MarketMaker"]))))
    except KeyboardInterrupt:
        print("\nleft the arena", flush=True)
