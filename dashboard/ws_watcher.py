"""Polygon WebSocket watcher. Subscribes to OrderFilled events on the
Polymarket CTF Exchange contracts, filtered by the target wallet as maker.
Emits normalized events into a thread-safe queue that engine.py drains.

Falls back gracefully if POLYGON_WSS_URL isn't set — the REST poller keeps
working without it.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from queue import Queue
from typing import Optional

import websockets.sync.client as wsclient
from web3 import Web3

log = logging.getLogger("ws_watcher")

# Polymarket exchanges on Polygon
CTF_EXCHANGE = Web3.to_checksum_address("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E")
NEG_RISK_EXCHANGE = Web3.to_checksum_address("0xC5d563A36AE78145C45a50134d48A1215220f80a")

# keccak256("OrderFilled(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)")
ORDER_FILLED_TOPIC = Web3.keccak(
    text="OrderFilled(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)"
).hex()
if not ORDER_FILLED_TOPIC.startswith("0x"):
    ORDER_FILLED_TOPIC = "0x" + ORDER_FILLED_TOPIC

USDC_ASSET_ID = "0"


def _pad_address(addr: str) -> str:
    a = addr.lower().replace("0x", "")
    return "0x" + a.rjust(64, "0")


def _decode_log(log_dict: dict) -> Optional[dict]:
    """Parse an OrderFilled log into the same shape as data-api activity items
    so engine.py can mirror it without special-casing."""
    try:
        topics = log_dict["topics"]
        data_hex = log_dict["data"][2:]  # strip 0x
        if len(topics) < 4:
            return None

        maker = "0x" + topics[2][-40:]
        taker = "0x" + topics[3][-40:]
        # non-indexed data: makerAssetId, takerAssetId, makerFilled, takerFilled, fee
        words = [data_hex[i*64:(i+1)*64] for i in range(5)]
        maker_asset = int(words[0], 16)
        taker_asset = int(words[1], 16)
        maker_filled = int(words[2], 16)
        taker_filled = int(words[3], 16)

        usdc_is_maker = maker_asset == 0
        if usdc_is_maker:
            # maker gave USDC, taker gave token → maker BOUGHT token (but our target is maker)
            side = "BUY"
            token_asset = taker_asset
            token_size = taker_filled / 1_000_000  # shares (6 decimals on CTF)
            usdc_size = maker_filled / 1_000_000
        else:
            # maker gave token, taker gave USDC → maker SOLD token
            side = "SELL"
            token_asset = maker_asset
            token_size = maker_filled / 1_000_000
            usdc_size = taker_filled / 1_000_000

        price = usdc_size / token_size if token_size else 0.0
        tx_hash = log_dict.get("transactionHash", "")
        block_number = int(log_dict.get("blockNumber", "0x0"), 16)

        return {
            "transactionHash": tx_hash,
            "timestamp": int(time.time()),  # approximate; block ts is better but adds a call
            "maker": maker, "taker": taker,
            "side": side, "price": round(price, 6),
            "size": token_size, "usdcSize": usdc_size,
            "asset": str(token_asset),
            "conditionId": "",   # not in OrderFilled; engine falls back fine
            "source": "ws",
            "blockNumber": block_number,
        }
    except Exception as e:
        log.warning("decode failed: %s", e)
        return None


def _subscribe_loop(ws_url: str, target: str, out_queue: Queue, stop_event: threading.Event):
    target_padded = _pad_address(target)
    sub_params = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_subscribe",
        "params": ["logs", {
            "address": [CTF_EXCHANGE, NEG_RISK_EXCHANGE],
            "topics": [ORDER_FILLED_TOPIC, None, target_padded, None],
        }],
    }

    backoff = 1
    while not stop_event.is_set():
        try:
            log.info("ws connecting %s (maker=%s)", ws_url.split("/v2/")[0] + "/…", target[:10])
            with wsclient.connect(ws_url, max_size=4_000_000, open_timeout=15) as ws:
                ws.send(json.dumps(sub_params))
                ack = json.loads(ws.recv(timeout=15))
                if "error" in ack:
                    log.error("ws subscribe error: %s", ack["error"])
                    time.sleep(10); continue
                log.info("ws subscribed sid=%s", ack.get("result"))
                backoff = 1
                while not stop_event.is_set():
                    raw = ws.recv(timeout=120)
                    msg = json.loads(raw)
                    params = msg.get("params", {})
                    log_entry = params.get("result")
                    if not log_entry:
                        continue
                    decoded = _decode_log(log_entry)
                    if decoded:
                        log.info("ws fill tx=%s side=%s @%.3f size=%.2f",
                                 decoded["transactionHash"][:10],
                                 decoded["side"], decoded["price"], decoded["size"])
                        out_queue.put(decoded)
        except Exception as e:
            log.warning("ws loop error: %s — reconnecting in %ds", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


def start_ws_watcher(ws_url: str, target: str) -> Queue:
    """Spawn a daemon thread subscribing to OrderFilled events for `target`.
    Returns a Queue that engine.py can drain. Pass ws_url=None to disable."""
    q: Queue = Queue(maxsize=10_000)
    if not ws_url:
        log.info("POLYGON_WSS_URL not set — ws watcher disabled, REST poller only")
        return q
    stop = threading.Event()
    t = threading.Thread(
        target=_subscribe_loop, args=(ws_url, target, q, stop),
        daemon=True, name="polygon-ws",
    )
    t.start()
    return q
