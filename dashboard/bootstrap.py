"""One-time bootstrap — derive CLOB API credentials from the private key
and write API_KEY / SECRET / PASSPHRASE back to .env. Idempotent.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"


def update_env(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    seen = set()
    out = []
    for line in lines:
        if "=" in line and not line.startswith("#"):
            k = line.split("=", 1)[0]
            if k in updates:
                out.append(f"{k}={updates[k]}")
                seen.add(k)
                continue
        out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    path.write_text("\n".join(out) + "\n")


def main() -> int:
    load_dotenv(ENV_FILE)
    priv = os.environ.get("POLY_PRIVATE_KEY", "").strip()
    funder = os.environ.get("POLY_FUNDER", "").strip() or None
    sig_type = int(os.environ.get("POLY_SIGNATURE_TYPE", "0"))
    host = "https://clob.polymarket.com"

    if not priv:
        print("POLY_PRIVATE_KEY not set in .env", file=sys.stderr)
        return 1

    client = ClobClient(host=host, key=priv, chain_id=POLYGON,
                        signature_type=sig_type, funder=funder)
    print(f"signer address: {client.get_address()}")
    print(f"funder:          {funder}")
    print(f"signature_type:  {sig_type}")

    creds = client.create_or_derive_api_creds()
    print("API creds derived.")
    update_env(ENV_FILE, {
        "POLY_API_KEY": creds.api_key,
        "POLY_API_SECRET": creds.api_secret,
        "POLY_API_PASSPHRASE": creds.api_passphrase,
    })
    print(f"Wrote API_KEY / SECRET / PASSPHRASE to {ENV_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
