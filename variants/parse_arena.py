import re, sys, statistics
from collections import defaultdict

BOOK = re.compile(
    r"^\s+(?P<name>.+?)\s{2,}"
    r"(?P<bid>\d\.\d\d)\s*x(?P<bq>\d+)\s*/\s*"
    r"(?P<off>\d\.\d\d)\s*x(?P<oq>\d+)\s*\((?P<w>\d\.\d\d) wide\)")
RESULT = re.compile(r"^\s*\d+\s+(?P<name>.+?)\s{2,}(?P<pl>[+-]?\d+\.\d+)\s+(?P<cash>\d+\.\d+)\s+(?P<tr>\d+)\s+(?P<vol>\d+)\s+(?P<pts>\d+\.\d+)")

obs = defaultdict(lambda: {"w": [], "bq": [], "oq": [], "bid0": 0, "off1": 0, "n": 0})
results = defaultdict(lambda: {"pl": [], "pts": [], "trades": [], "vol": [], "dq": 0})

for path in sys.argv[1:]:
    for ln in open(path, errors="replace"):
        m = BOOK.match(ln)
        if m:
            d = obs[m["name"].strip()]
            d["n"] += 1
            d["w"].append(float(m["w"]))
            d["bq"].append(int(m["bq"]))
            d["oq"].append(int(m["oq"]))
            d["bid0"] += float(m["bid"]) <= 0.0
            d["off1"] += float(m["off"]) >= 1.0
            continue
        r = RESULT.match(ln)
        if r:
            d = results[r["name"].strip()]
            d["pl"].append(float(r["pl"])); d["pts"].append(float(r["pts"]))
            d["trades"].append(int(r["tr"])); d["vol"].append(int(r["vol"]))
            if "DISQUALIFIED" in ln: d["dq"] += 1

def med(x): return round(statistics.median(x), 3) if x else None
def rng(x): return (min(x), max(x)) if x else None

print(f"\n=== quoting behaviour ({sum(v['n'] for v in obs.values())} book rows) ===")
print(f"{'maker':<20}{'n':>5}{'half_spread med(range)':>26}{'bid_sz med(rng)':>20}{'off_sz med(rng)':>20}{'bid=0':>7}{'off=1':>7}")
for name in sorted(obs, key=lambda n: -obs[n]["n"]):
    d = obs[name]
    hs = [round(w/2, 3) for w in d["w"]]
    print(f"{name:<20}{d['n']:>5}   {med(hs)!s:>6} {str(rng(hs)):>16}"
          f"{med(d['bq'])!s:>8} {str(rng(d['bq'])):>10}"
          f"{med(d['oq'])!s:>8} {str(rng(d['oq'])):>10}"
          f"{100*d['bid0']//max(d['n'],1):>5}%{100*d['off1']//max(d['n'],1):>6}%")

if results:
    print(f"\n=== match results ({sum(len(v['pl']) for v in results.values())} standings rows) ===")
    print(f"{'maker':<20}{'avg PL':>9}{'avg pts':>9}{'avg trades':>12}{'DQ':>5}")
    for name in sorted(results, key=lambda n: -statistics.mean(results[n]['pts'] or [0])):
        d = results[name]
        print(f"{name:<20}{statistics.mean(d['pl']):>+9.2f}{statistics.mean(d['pts']):>9.2f}"
              f"{statistics.mean(d['trades']):>12.1f}{d['dq']:>5}")
