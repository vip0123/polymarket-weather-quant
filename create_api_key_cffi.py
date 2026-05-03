"""Create CLOB API key using curl_cffi to bypass Cloudflare."""
import os, sys, time, re
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

pk      = os.environ["POLY_PRIVATE_KEY"]
funder  = os.environ.get("POLY_FUNDER") or os.environ.get("POLY_WALLET_ADDRESS", "")
sig_t   = int(os.environ.get("POLY_SIGNATURE_TYPE", "2"))
proxy   = os.environ["POLY_PROXY_URL"]

from eth_account import Account
eoa = Account.from_key(pk).address
print(f"EOA      : {eoa}")
print(f"funder   : {funder}")
print(f"sig_type : {sig_t}")
print(f"proxy    : ...{proxy.split('@')[-1]}")
print()

# Build L1 auth signature using py_clob_client_v2 signing helpers
import sys; sys.path.insert(0, str(ROOT / ".venv/Lib/site-packages"))
from py_clob_client_v2.signer import Signer
from py_clob_client_v2.signing.eip712 import sign_clob_auth_message
from py_clob_client_v2.constants import POLYGON

signer = Signer(pk, POLYGON)
assert signer.address().lower() == eoa.lower(), f"Signer mismatch: {signer.address()} != {eoa}"

from curl_cffi import requests as cffi_req

proxies = {"http": proxy, "https": proxy}

# Try POST /auth/api-key with nonces 0..4
creds = None
for nonce in range(5):
    ts = int(time.time())
    sig = sign_clob_auth_message(signer, ts, nonce)
    headers = {
        "POLY_ADDRESS":   eoa,
        "POLY_SIGNATURE": sig,
        "POLY_TIMESTAMP": str(ts),
        "POLY_NONCE":     str(nonce),
        "Content-Type":   "application/json",
    }
    try:
        resp = cffi_req.post(
            "https://clob.polymarket.com/auth/api-key",
            headers=headers,
            json={},
            proxies=proxies,
            impersonate="chrome",
            timeout=20,
        )
        print(f"nonce={nonce}: HTTP {resp.status_code} {resp.text[:120]}")
        if resp.status_code == 200:
            data = resp.json()
            creds = data
            break
        elif resp.status_code == 400 and "already" in resp.text.lower():
            # Key already exists — fall through to derive
            print("  => key already exists, will derive instead")
            break
    except Exception as e:
        print(f"nonce={nonce}: error {e}")

if not creds:
    print("\nTrying GET /auth/derive-api-key ...")
    for nonce in range(5):
        ts = int(time.time())
        sig = sign_clob_auth_message(signer, ts, nonce)
        headers = {
            "POLY_ADDRESS":   eoa,
            "POLY_SIGNATURE": sig,
            "POLY_TIMESTAMP": str(ts),
            "POLY_NONCE":     str(nonce),
        }
        try:
            resp = cffi_req.get(
                "https://clob.polymarket.com/auth/derive-api-key",
                headers=headers,
                proxies=proxies,
                impersonate="chrome",
                timeout=20,
            )
            print(f"nonce={nonce}: HTTP {resp.status_code} {resp.text[:120]}")
            if resp.status_code == 200:
                creds = resp.json()
                break
        except Exception as e:
            print(f"nonce={nonce}: error {e}")

if not creds:
    print("\nERROR: Could not create or derive API key.")
    sys.exit(1)

key        = creds.get("apiKey") or creds.get("api_key", "")
secret     = creds.get("secret") or creds.get("api_secret", "")
passphrase = creds.get("passphrase") or creds.get("api_passphrase", "")

print()
print("=" * 60)
print(f"POLY_API_KEY       = {key}")
print(f"POLY_API_SECRET    = {secret}")
print(f"POLY_API_PASSPHRASE= {passphrase}")
print("=" * 60)

# Patch .env
env_path = ROOT / ".env"
txt = env_path.read_text()

def _sub(k, v, t):
    return re.sub(rf"^{k}=.*$", f"{k}={v}", t, flags=re.MULTILINE)

txt = _sub("POLY_API_KEY", key, txt)
txt = _sub("POLY_API_SECRET", secret, txt)
txt = _sub("POLY_API_PASSPHRASE", passphrase, txt)
env_path.write_text(txt)
print(".env updated.")
