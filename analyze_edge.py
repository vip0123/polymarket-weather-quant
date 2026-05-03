import csv

rows = list(csv.DictReader(open("weather/edge_table.csv")))
pos = [r for r in rows if float(r["edge"]) > 0.08]
pos.sort(key=lambda r: float(r["edge"]), reverse=True)
print(f"Positive-edge opportunities (edge>0.08): {len(pos)}")
print(f"{'target_date':<12} {'city':<15} {'metric':<10} {'our_p':>6} {'mkt_p':>6} {'edge':>7} {'fcst_f':>7}  question")
for r in pos:
    print(
        f"{r['target_date']:<12} {r['city']:<15} {r['metric']:<10}"
        f" {float(r['our_p']):>6.3f} {float(r['market_p']):>6.4f}"
        f" {float(r['edge']):>7.3f} {float(r['forecast_f']):>7.1f}"
        f"  {r['question'][:80]}"
    )

print()
all_cols = list(rows[0].keys())
print("CSV columns:", all_cols)
