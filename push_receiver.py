"""EV Pulse -- Push Receiver.

Receives DATEX II deliveries pushed by Mobilithek via HTTP POST and stores
them using the same DB logic as the pull-based collector.

Mobilithek push: each subscription is configured with a callback URL.
When new data arrives, Mobilithek POSTs the payload to that URL.  The
delivery type (SNAPSHOT vs DELTA) comes in the ``Type`` header, and
``Last-Modified`` tracks ordering.

Usage:
    uvicorn push_receiver:app --host 0.0.0.0 --port 8100

In production, nginx terminates TLS and reverse-proxies to this.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pandas as pd
from fastapi import FastAPI, Request, Response

from providers import get_provider
from providers.base import Provider
import collector as col

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s UTC | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOGGER = logging.getLogger("push_receiver")

# ── Subscription ID → (provider_name, feed_type) ────────────────────
# provider_name must match keys in providers.PROVIDERS (e.g. "eco_movement").
SUBSCRIPTION_MAP: dict[str, tuple[str, str]] = {
    # eco-movement
    "970702152460550144": ("eco_movement", "static"),
    "970702182768590848": ("eco_movement", "dynamic"),
    # tesla
    "970702208026705920": ("tesla", "dynamic"),
    # ladenetz
    "970702370149138432": ("ladenetz", "static"),
    "970702352017145856": ("ladenetz", "dynamic"),
}

# ── Providers whose static data must be pulled (no push subscription) ─
STATIC_PULL_PROVIDERS = ["tesla"]
STATIC_PULL_INTERVAL = timedelta(hours=12)

# ── Per-provider state (DB connections + last known dynamic state) ────
_lock = threading.Lock()
_providers: dict[str, Provider] = {}
_static_conns: dict[str, sqlite3.Connection] = {}
_dynamic_conns: dict[str, sqlite3.Connection] = {}
_previous_states: dict[str, pd.DataFrame | None] = {}
_static_pull_stop = threading.Event()


def _init_provider(name: str) -> None:
    """Initialise DB connections and load last-known state for a provider."""
    provider = get_provider(name)
    _providers[name] = provider
    _static_conns[name] = col.ensure_static_db(provider)
    _dynamic_conns[name] = col.ensure_dynamic_db(provider)
    _previous_states[name] = col.load_last_known_state(
        _dynamic_conns[name], provider,
    )
    n = len(_previous_states[name]) if _previous_states[name] is not None else 0
    LOGGER.info("%s: initialised (%d known point states)", provider.name, n)


def _static_pull_loop() -> None:
    """Background thread: periodically pull static data for providers
    that don't have a push subscription for their static feed
    (e.g. Tesla, whose static endpoint is public/noauth)."""
    last_refresh: dict[str, datetime] = {}
    while not _static_pull_stop.is_set():
        now = datetime.now(timezone.utc)
        for name in STATIC_PULL_PROVIDERS:
            if name not in _providers:
                continue
            last = last_refresh.get(name)
            if last and now - last < STATIC_PULL_INTERVAL:
                continue
            provider = _providers[name]
            try:
                LOGGER.info("Pulling static data for %s …", provider.name)
                points_df, pub_time = provider.load_static_snapshot()
                with _lock:
                    col.store_static_snapshot(
                        _static_conns[name], points_df, pub_time,
                    )
                last_refresh[name] = now
            except Exception:
                LOGGER.exception(
                    "Failed to pull static data for %s", provider.name,
                )
                last_refresh[name] = now  # back off, retry next interval
        _static_pull_stop.wait(timeout=300)  # check every 5 min


# ── FastAPI lifecycle ────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    seen: set[str] = set()
    for prov_name, _ in SUBSCRIPTION_MAP.values():
        if prov_name not in seen:
            _init_provider(prov_name)
            seen.add(prov_name)
    # Also init providers that only need static pulls
    for name in STATIC_PULL_PROVIDERS:
        if name not in seen:
            _init_provider(name)
            seen.add(name)

    # Start background static puller
    pull_thread = threading.Thread(
        target=_static_pull_loop, daemon=True, name="static-puller",
    )
    pull_thread.start()

    LOGGER.info(
        "Push receiver ready — %d providers, %d push subscriptions, "
        "%d static-pull providers",
        len(seen), len(SUBSCRIPTION_MAP), len(STATIC_PULL_PROVIDERS),
    )
    yield
    _static_pull_stop.set()
    pull_thread.join(timeout=10)
    for conn in _static_conns.values():
        conn.close()
    for conn in _dynamic_conns.values():
        conn.close()


app = FastAPI(title="EV Pulse Push Receiver", lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "providers": {
            name: {
                "points": len(st) if st is not None else 0,
            }
            for name, st in _previous_states.items()
        },
    }


@app.post("/push/{subscription_id}")
async def receive_push(subscription_id: str, request: Request):
    """Receive a DATEX II delivery from Mobilithek.

    Configure Mobilithek push callback URL as:
        https://<your-domain>/push/<subscription_id>
    """
    mapping = SUBSCRIPTION_MAP.get(subscription_id)
    if mapping is None:
        LOGGER.warning("Unknown subscription ID: %s", subscription_id)
        return Response(status_code=404, content="Unknown subscription")

    prov_name, feed_type = mapping
    provider = _providers[prov_name]
    delivery_type = request.headers.get("Type", "SNAPSHOT")
    last_modified = request.headers.get("Last-Modified")
    body = await request.body()

    LOGGER.info(
        "Received %s %s for %s (%d bytes)",
        delivery_type, feed_type, provider.name, len(body),
    )

    try:
        data = _parse_body(body)
    except Exception:
        # Log the raw payload for debugging, then accept gracefully.
        LOGGER.warning(
            "Unparseable payload for %s/%s (%d bytes), first 500 chars: %s",
            prov_name, feed_type, len(body),
            body[:500].decode("utf-8", errors="replace"),
        )
        return Response(status_code=200, content="OK (accepted)")

    try:
        if feed_type == "static":
            _handle_static_push(prov_name, provider, data)
        else:
            _handle_dynamic_push(
                prov_name, provider, data, delivery_type, last_modified,
            )
    except Exception:
        LOGGER.exception("Error processing push for %s/%s", prov_name, feed_type)
        return Response(status_code=500, content="Processing error")

    return Response(status_code=200, content="OK")


# ── Static push handling ─────────────────────────────────────────────

def _parse_body(body: bytes):
    """Auto-detect JSON vs XML and return parsed object."""
    stripped = body.lstrip()
    if stripped[:1] in (b"{", b"["):
        return json.loads(body)
    return ET.fromstring(body)


def _handle_static_push(
    prov_name: str, provider: Provider, data,
) -> None:
    if hasattr(provider, "parse_static_points"):
        # XML providers (ladenetz): dedicated method for pre-fetched data
        points_df, pub_time = provider.parse_static_points(data)
    elif hasattr(provider, "_parse_static_points") and isinstance(data, dict):
        # JSON providers (eco-movement, tesla): internal parse method
        points_df = provider._parse_static_points(data)
        pub_time_str = (
            data.get("payload", {})
            .get("aegiEnergyInfrastructureTablePublication", {})
            .get("publicationTime")
        )
        pub_time = pd.to_datetime(pub_time_str, utc=True)
    else:
        LOGGER.error("No static parser for %s", provider.name)
        return

    with _lock:
        col.store_static_snapshot(_static_conns[prov_name], points_df, pub_time)


# ── Dynamic push handling ────────────────────────────────────────────

def _handle_dynamic_push(
    prov_name: str,
    provider: Provider,
    data,
    delivery_type: str,
    last_modified: str | None,
) -> None:
    status_df, pub_time = provider.parse_dynamic_points(data)

    if status_df.empty:
        LOGGER.debug("Empty dynamic delivery for %s, skipping.", provider.name)
        return

    with _lock:
        dynamic_conn = _dynamic_conns[prov_name]
        static_conn = _static_conns.get(prov_name)
        previous = _previous_states.get(prov_name)

        if delivery_type == "SNAPSHOT":
            _, updated = col.store_dynamic_snapshot(
                dynamic_conn, provider, status_df, pub_time,
                delivery_type, previous_state=None,
                static_conn=static_conn,
            )
        else:
            _, updated = col.store_dynamic_snapshot(
                dynamic_conn, provider, status_df, pub_time,
                delivery_type, previous_state=previous,
                static_conn=static_conn,
            )

        _previous_states[prov_name] = updated

        if last_modified:
            col.set_cursor(dynamic_conn, last_modified)
