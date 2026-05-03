"""
Create or derive Polymarket CLOB API credentials via a proxy.

Usage:
  1. Set POLY_PROXY_URL in .env (e.g. http://user:pass@host:port)
  2. Run:  uv run python get_api_key.py

  To patch .env automatically with new creds:
    $env:PATCH_ENV="1" ; uv run python get_api_key.py

  To manually paste creds obtained from the Polymarket web UI:
    uv run python get_api_key.py --set KEY SECRET PASSPHRASE

  (Web UI: polymarket.com → Profile → Settings → API Keys → Create New Key)
"""
from __future__ import annotations
import os, sys
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

# ── Manual set mode ──────────────────────────────────────────────────────────
if "--set" in sys.argv:
    idx = sys.argv.index("--set")
    try:
        new_key = sys.argv[idx + 1]
        new_secret = sys.argv[idx + 2]
        new_passphrase = sys.argv[idx + 3]
    except IndexError:
        print("Usage: uv run python get_api_key.py --set KEY SECRET PASSPHRASE")
        sys.exit(1)
    env_path = ROOT / ".env"
    import re
    txt = env_path.read_text()
    def _sub(key, val):
        return re.sub(rf"^{key}=.*$", f"{key}={val}", txt, flags=re.MULTILINE)
    txt = _sub("POLY_API_KEY", new_key)
    txt = _sub("POLY_API_SECRET", new_secret)
    txt = _sub("POLY_API_PASSPHRASE", new_passphrase)
    env_path.write_text(txt)
    print(f".env updated:")
    print(f"  POLY_API_KEY       = {new_key}")
    print(f"  POLY_API_SECRET    = {new_secret[:8]}...")
    print(f"  POLY_API_PASSPHRASE= {new_passphrase[:8]}...")
    sys.exit(0)

# ── Proxy-based create/derive mode ───────────────────────────────────────────
proxy_url = os.environ.get("POLY_PROXY_URL", "").strip()
if not proxy_url:
    print("ERROR: POLY_PROXY_URL not set in .env")
    print("Set it to e.g.  http://user:pass@host:port")
    sys.exit(1)

# Patch httpx client BEFORE importing ClobClient (module-level singleton)
import httpx
import py_clob_client_v2.http_helpers.helpers as _hh
_hh._http_client = httpx.Client(http2=True, proxy=proxy_url)
os.environ["HTTPS_PROXY"] = proxy_url
os.environ["HTTP_PROXY"] = proxy_url
host_port = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
print(f"proxy: ...{host_port}")

from py_clob_client_v2 import ClobClient
from py_clob_client_v2.constants import POLYGON

pk     = os.environ.get("POLY_PRIVATE_KEY", "")
funder = os.environ.get("POLY_FUNDER") or os.environ.get("POLY_WALLET_ADDRESS", "")
sig_t  = int(os.environ.get("POLY_SIGNATURE_TYPE", "1"))

if not pk:
    print("ERROR: POLY_PRIVATE_KEY not set in .env")
    sys.exit(1)

from eth_account import Account
eoa = Account.from_key(pk).address
print(f"private key EOA : {eoa}")
print(f"funder/wallet   : {funder}")
print(f"signature_type  : {sig_t}")
print()

client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=POLYGON,
    key=pk,
    signature_type=sig_t,
    funder=funder or None,
)

# Check geo-block first
import requests as _req
try:
    geo = _req.get("https://polymarket.com/api/geoblock", timeout=8,
                   proxies={"https": proxy_url, "http": proxy_url}).json()
    print(f"geoblock check  : blocked={geo.get('blocked')} country={geo.get('country')} ip={geo.get('ip')}")
    if geo.get("blocked"):
        print("WARNING: still geo-blocked through this proxy — order placement will fail.")
except Exception as e:
    print(f"geoblock check  : could not reach ({e})")
print()

print("Trying create_api_key ...")
creds = None
for nonce in range(5):
    try:
        creds = client.create_api_key(nonce=nonce)
        print(f"  created at nonce={nonce}")
        break
    except Exception as e:
        msg = str(e)
        if "403" in msg:
            print(f"  nonce={nonce}: 403 Cloudflare bot-block — use browser UI to create key")
            print()
            print("  ► Open polymarket.com in browser (with VPN/proxy)")
            print("  ► Profile → Settings → API Keys → Create New Key")
            print("  ► Copy all 3 values, then run:")
            print("    uv run python get_api_key.py --set KEY SECRET PASSPHRASE")
            break
        print(f"  nonce={nonce}: {msg[:80]}")

if not creds:
    print("Falling back to derive_api_key ...")
    for nonce in range(5):
        try:
            creds = client.derive_api_key(nonce=nonce)
            print(f"  derived at nonce={nonce}")
            break
        except Exception as e:
            print(f"  nonce={nonce}: {str(e)[:80]}")

if not creds:
    sys.exit(1)

print()
print("=" * 60)
print(f"POLY_API_KEY       = {creds.api_key}")
print(f"POLY_API_SECRET    = {creds.api_secret}")
print(f"POLY_API_PASSPHRASE= {creds.api_passphrase}")
print("=" * 60)

if os.environ.get("PATCH_ENV") == "1":
    env_path = ROOT / ".env"
    txt = env_path.read_text()
    import re
    def _sub(key, val):
        return re.sub(rf"^{key}=.*$", f"{key}={val}", txt, flags=re.MULTILINE)
    txt = _sub("POLY_API_KEY", creds.api_key)
    txt = _sub("POLY_API_SECRET", creds.api_secret)
    txt = _sub("POLY_API_PASSPHRASE", creds.api_passphrase)
    env_path.write_text(txt)
    print(".env updated in place.")

