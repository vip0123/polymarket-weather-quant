"""Discover weather-category Polymarket markets + get live orderbook."""
from __future__ import annotations

import json
from typing import Optional

import requests

GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"


def fetch_weather_events(limit: int = 100) -> list[dict]:
    """Pull weather-tagged events. Gamma's tag field is 'Weather' on Polymarket."""
    out: list[dict] = []
    # Tag-filtered endpoint: tag_slug=weather
    for endpoint, params in [
        (GAMMA_EVENTS, {"tag_slug": "weather", "limit": limit,
                        "closed": "false", "active": "true"}),
        (GAMMA_EVENTS, {"tag": "Weather", "limit": limit,
                        "closed": "false", "active": "true"}),
    ]:
        try:
            r = requests.get(endpoint, params=params,
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            data = r.json() or []
            if data:
                out.extend(data)
                break
        except Exception:
            continue
    # Dedupe by id
    seen = set(); uniq = []
    for e in out:
        if e.get("id") in seen: continue
        seen.add(e.get("id")); uniq.append(e)
    return uniq


def extract_markets(event: dict) -> list[dict]:
    """Flatten an event into [{question, outcomes, tokens, asks}]."""
    mkts = event.get("markets", []) or []
    out = []
    for m in mkts:
        try:
            outcomes = json.loads(m.get("outcomes", "[]"))
            tokens = json.loads(m.get("clobTokenIds", "[]"))
            prices = json.loads(m.get("outcomePrices", "[]"))
        except Exception:
            continue
        if not tokens: continue
        out.append({
            "event_id": event.get("id"),
            "event_title": event.get("title", ""),
            "conditionId": m.get("conditionId"),
            "question": m.get("question", "") or event.get("title", ""),
            "slug": m.get("slug", ""),
            "outcomes": outcomes,
            "tokens": tokens,
            "prices": [float(p) for p in prices] if prices else [],
            "end_date": m.get("endDate") or event.get("endDate"),
        })
    return out


def fetch_market_book(token_id: str) -> dict:
    """Fetch L2 book for a token via Polymarket CLOB public endpoint."""
    try:
        r = requests.get(f"https://clob.polymarket.com/book",
                         params={"token_id": token_id}, timeout=8)
        return r.json()
    except Exception as e:
        return {"error": str(e)}
