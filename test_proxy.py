"""Quick test: geoblock check + live order post through proxy."""
import os, httpx, json, csv
from dotenv import load_dotenv

load_dotenv(".env")
proxy = os.environ["POLY_PROXY_URL"]
print(f"Proxy: {proxy.split('@')[-1]}")

# 1. Geoblock check via httpx (avoids requests/urllib3 CONNECT tunnel issues)
with httpx.Client(http2=False, proxy=proxy, timeout=15) as hc:
    geo = hc.get("https://polymarket.com/api/geoblock").json()
print("Geoblock:", geo)
if geo.get("blocked"):
    print("BLOCKED — need a different proxy country")
    raise SystemExit(1)

# 2. Inject proxy into ClobClient httpx singleton
import py_clob_client_v2.http_helpers.helpers as _hh
_hh._http_client = httpx.Client(http2=False, proxy=proxy, timeout=15)

from py_clob_client_v2 import ClobClient, ApiCreds, OrderArgs, Side
from py_clob_client_v2.constants import POLYGON

creds = ApiCreds(
    api_key=os.environ["POLY_API_KEY"],
    api_secret=os.environ["POLY_API_SECRET"],
    api_passphrase=os.environ["POLY_API_PASSPHRASE"],
)
client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=POLYGON,
    key=os.environ["POLY_PRIVATE_KEY"],
    creds=creds,
    signature_type=int(os.environ.get("POLY_SIGNATURE_TYPE", "0")),
    funder=os.environ.get("POLY_FUNDER"),
)

with open("weather/edge_table.csv") as f:
    rows = list(csv.DictReader(f))
test_token = json.loads(rows[0]["tokens"])[0]
print(f"Test token: {test_token[:24]}...")

order_args = OrderArgs(token_id=test_token, price=0.01, size=5.0, side=Side.BUY)
order = client.create_order(order_args)
print("Order signed OK")
res = client.post_order(order)
print("POST /order result:", res)
print("\n✅ SUCCESS — proxy + L2 auth working!")
