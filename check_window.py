"""Check which markets are within/near the 30-hour firing window."""
from datetime import datetime, date
from zoneinfo import ZoneInfo
import csv

rows = list(csv.DictReader(open("weather/edge_table.csv")))
from weather.cities import CITIES, get_offset_c

now_utc = datetime.now(ZoneInfo("UTC"))
print(f"Now UTC: {now_utc.strftime('%Y-%m-%d %H:%M')}\n")

print("=== DIRECTIONAL (>=/<= op) positive-edge, sorted by hours_left ===")
print(f"{'date':<12} {'city':<14} {'edge':>6} {'hrs_left':>9} {'cushion_F':>10}  question")
candidates = []
for r in rows:
    if float(r["edge"]) < 0.08:
        continue
    if r["op"] not in (">=", "<="):
        continue
    td = date.fromisoformat(r["target_date"])
    city = r["city"].lower()
    tz_str = CITIES[city][2] if city in CITIES else "UTC"
    res = datetime(td.year, td.month, td.day, 23, 59, tzinfo=ZoneInfo(tz_str))
    hours = (res - now_utc).total_seconds() / 3600
    offset_c = get_offset_c(city)
    offset_f = offset_c * 9 / 5
    eff = float(r["forecast_f"]) + offset_f
    thr = float(r["threshold"])
    cushion = eff - thr if r["op"] == ">=" else thr - eff
    candidates.append((hours, r, cushion))

candidates.sort(key=lambda x: x[0])
for hours, r, cushion in candidates:
    window = "IN-WINDOW" if hours <= 30 else f"opens in {hours-30:.0f}h"
    print(f"{r['target_date']:<12} {r['city']:<14} {float(r['edge']):>6.3f} {hours:>8.1f}h  {cushion:>9.1f}F  [{window}] {r['question'][:55]}")

print()
print("=== TODAY bucket NO opportunities (op=in, our_p < market_p, today) ===")
today = date.today().isoformat()
print(f"{'city':<14} {'edge':>7} {'cushion_F':>10} {'mkt':>6} {'our':>6}  question")
for r in rows:
    if r["target_date"] != today:
        continue
    if r["op"] != "in":
        continue
    edge = float(r["edge"])
    if edge >= 0:  # we only want NO bets (negative edge = market overprices YES)
        continue
    city = r["city"].lower()
    offset_c = get_offset_c(city)
    offset_f = offset_c * 9 / 5
    eff = float(r["forecast_f"]) + offset_f
    thr_str = r["threshold"]
    lo, hi = [float(x) for x in thr_str.split("-")]
    if lo <= eff <= hi:
        cushion = 0.0  # forecast inside bucket — risky
    else:
        cushion = min(abs(eff - lo), abs(eff - hi))
    print(f"{r['city']:<14} {edge:>7.3f} {cushion:>10.2f}F {float(r['market_p']):>6.3f} {float(r['our_p']):>6.3f}  {r['question'][:60]}")
