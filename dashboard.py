"""EV Charging Monitor — Live Dashboard.

Reads the collector's SQLite databases and presents real-time KPIs,
a geographic map, and power-draw timelines for Germany's public
EV charging infrastructure.

Run:
    streamlit run dashboard.py
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ── Page config ───────────────────────────────────────────────────────
st.set_page_config(
    page_title="EV Charging Monitor — Germany",
    page_icon="⚡",
    layout="wide",
)

DATA_DIR = Path("data")

PROVIDERS = {
    "eco": {
        "label": "eco-movement",
        "in_use": "charging",
        "in_use_col": "charging_count",
        "power_col": "point_power_w",
        "to_mw": 1e-6,
        "color": "#1f77b4",
    },
    "tesla": {
        "label": "Tesla Supercharger",
        "in_use": "occupied",
        "in_use_col": "occupied_count",
        "power_col": "point_power_kw",
        "to_mw": 1e-3,
        "color": "#d62728",
    },
}

STATUS_COLORS = {
    "available": "#2ecc71",
    "charging": "#e74c3c",
    "occupied": "#e74c3c",
    "outOfService": "#95a5a6",
    "unknown": "#bdc3c7",
    "removed": "#7f8c8d",
    "reserved": "#f39c12",
    "inoperative": "#95a5a6",
    "blocked": "#e67e22",
}
DEFAULT_COLOR = "#bdc3c7"


# ═══════════════════════════════════════════════════════════════════════
#  DATA LOADING  (cached 60 s)
# ═══════════════════════════════════════════════════════════════════════
@st.cache_data(ttl=60)
def load_current_state(slug: str) -> pd.DataFrame:
    """Most recent status per charging point (last snapshot + deltas)."""
    db = DATA_DIR / f"{slug}_dynamic.sqlite"
    if not db.exists():
        return pd.DataFrame()
    with sqlite3.connect(db) as conn:
        return pd.read_sql_query(
            """
            SELECT h.point_id, h.status, h.collected_at_utc
            FROM point_status_history h
            INNER JOIN (
                SELECT point_id, MAX(id) AS max_id
                FROM point_status_history
                GROUP BY point_id
            ) latest ON h.id = latest.max_id
            """,
            conn,
        )


@st.cache_data(ttl=300)
def load_static(slug: str) -> pd.DataFrame:
    """Static metadata — coordinates, power ratings, etc."""
    db = DATA_DIR / f"{slug}_static.sqlite"
    if not db.exists():
        return pd.DataFrame()
    with sqlite3.connect(db) as conn:
        return pd.read_sql_query("SELECT * FROM charging_points", conn)


@st.cache_data(ttl=60)
def load_snapshot_runs(slug: str, hours: int = 48) -> pd.DataFrame:
    """Aggregated snapshot runs for the timeline charts."""
    db = DATA_DIR / f"{slug}_dynamic.sqlite"
    if not db.exists():
        return pd.DataFrame()
    cutoff = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .__sub__(pd.Timedelta(hours=hours))
        .isoformat()
    )
    with sqlite3.connect(db) as conn:
        cfg = PROVIDERS[slug]
        return pd.read_sql_query(
            f"""
            SELECT collected_at_utc, delivery_type, point_count,
                   {cfg['in_use_col']}, available_count, unknown_count
            FROM snapshot_runs
            WHERE collected_at_utc >= ?
            ORDER BY snapshot_id
            """,
            conn,
            params=(cutoff,),
        )


@st.cache_data(ttl=60)
def replay_power(slug: str, hours: int = 48) -> pd.DataFrame:
    """Replay dynamic history and compute estimated MW from rated power."""
    cfg = PROVIDERS[slug]
    static = load_static(slug)
    if static.empty:
        return pd.DataFrame(columns=["time", "power_mw"])

    power_col = cfg["power_col"]
    if power_col not in static.columns:
        return pd.DataFrame(columns=["time", "power_mw"])

    power_map = dict(
        zip(
            static["point_id"],
            pd.to_numeric(static[power_col], errors="coerce").fillna(0) * cfg["to_mw"],
        )
    )

    db = DATA_DIR / f"{slug}_dynamic.sqlite"
    if not db.exists():
        return pd.DataFrame(columns=["time", "power_mw"])

    cutoff = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .__sub__(pd.Timedelta(hours=hours))
        .isoformat()
    )

    with sqlite3.connect(db) as conn:
        df = pd.read_sql_query(
            """
            SELECT h.point_id, h.status, h.collected_at_utc,
                   h.snapshot_id, s.delivery_type
            FROM point_status_history h
            JOIN snapshot_runs s ON h.snapshot_id = s.snapshot_id
            WHERE h.collected_at_utc >= ?
            ORDER BY h.id
            """,
            conn,
            params=(cutoff,),
        )

    if df.empty:
        return pd.DataFrame(columns=["time", "power_mw"])

    df["collected_at_utc"] = pd.to_datetime(df["collected_at_utc"])
    snapshot_ids = set(
        df.loc[df["delivery_type"] == "SNAPSHOT", "snapshot_id"].unique()
    )

    in_use_status = cfg["in_use"]
    state: dict[str, str] = {}
    records: list[tuple] = []

    for (sid, ts), group in df.groupby(
        ["snapshot_id", "collected_at_utc"], sort=False
    ):
        if sid in snapshot_ids:
            state = dict(zip(group["point_id"], group["status"]))
        else:
            for pid, status in zip(group["point_id"], group["status"]):
                state[pid] = status

        total_mw = sum(
            power_map.get(pid, 0.0)
            for pid, s in state.items()
            if s == in_use_status
        )
        records.append((ts, total_mw))

    result = pd.DataFrame(records, columns=["time", "power_mw"]).set_index("time")
    return result.sort_index()


# ═══════════════════════════════════════════════════════════════════════
#  DASHBOARD
# ═══════════════════════════════════════════════════════════════════════

# ── Header ────────────────────────────────────────────────────────────
st.markdown(
    "<h1 style='margin-bottom:0'>⚡ EV Charging Monitor — Germany</h1>"
    "<p style='color:gray;margin-top:0'>"
    "Real-time AFIR / DATEX II data from Mobilithek&ensp;·&ensp;"
    f"Last refresh: {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC"
    "</p>",
    unsafe_allow_html=True,
)

# ── Sidebar ───────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Settings")
    selected_providers = st.multiselect(
        "Providers",
        options=list(PROVIDERS.keys()),
        default=list(PROVIDERS.keys()),
        format_func=lambda s: PROVIDERS[s]["label"],
    )
    timeline_hours = st.slider("Timeline window (hours)", 6, 168, 48, step=6)
    auto_refresh = st.toggle("Auto-refresh (60 s)", value=False)
    if auto_refresh:
        st.markdown("_Page will reload automatically._")

# ── Load data ─────────────────────────────────────────────────────────
all_states: dict[str, pd.DataFrame] = {}
all_statics: dict[str, pd.DataFrame] = {}

for slug in selected_providers:
    all_states[slug] = load_current_state(slug)
    all_statics[slug] = load_static(slug)

# ── KPI metrics ───────────────────────────────────────────────────────
total_points = 0
total_in_use = 0
total_available = 0
total_mw = 0.0
latest_ts = None

for slug in selected_providers:
    state = all_states[slug]
    static = all_statics[slug]
    cfg = PROVIDERS[slug]

    if state.empty:
        continue

    n_in_use = int((state["status"] == cfg["in_use"]).sum())
    n_available = int((state["status"] == "available").sum())
    total_points += len(state)
    total_in_use += n_in_use
    total_available += n_available

    # Estimated MW from rated power
    power_col = cfg["power_col"]
    if not static.empty and power_col in static.columns:
        in_use_ids = set(state.loc[state["status"] == cfg["in_use"], "point_id"])
        pw = static.loc[
            static["point_id"].isin(in_use_ids), power_col
        ]
        total_mw += pd.to_numeric(pw, errors="coerce").fillna(0).sum() * cfg["to_mw"]

    ts = pd.to_datetime(state["collected_at_utc"]).max()
    if latest_ts is None or ts > latest_ts:
        latest_ts = ts

usage_pct = (total_in_use / total_points * 100) if total_points else 0

cols = st.columns(5)
cols[0].metric("Charging Points", f"{total_points:,}")
cols[1].metric("In Use", f"{total_in_use:,}")
cols[2].metric("Available", f"{total_available:,}")
cols[3].metric("Est. Load", f"{total_mw:,.1f} MW")
cols[4].metric("Usage Rate", f"{usage_pct:.1f}%")

# ── Map + Provider breakdown ─────────────────────────────────────────
map_col, stats_col = st.columns([2, 1])

with map_col:
    st.subheader("Charging Points — Current Status")

    map_rows = []
    for slug in selected_providers:
        state = all_states[slug]
        static = all_statics[slug]
        cfg = PROVIDERS[slug]
        if state.empty or static.empty:
            continue
        merged = state.merge(
            static[["point_id", "latitude", "longitude"]],
            on="point_id",
            how="inner",
        )
        merged = merged.dropna(subset=["latitude", "longitude"])
        merged["provider"] = cfg["label"]
        merged["color"] = merged["status"].map(
            lambda s: STATUS_COLORS.get(s, DEFAULT_COLOR)
        )
        map_rows.append(merged)

    if map_rows:
        map_df = pd.concat(map_rows, ignore_index=True)

        # Convert hex color to RGBA list for pydeck
        def hex_to_rgba(hex_str: str) -> list[int]:
            h = hex_str.lstrip("#")
            return [int(h[i : i + 2], 16) for i in (0, 2, 4)] + [180]

        map_df["color_rgba"] = map_df["color"].apply(hex_to_rgba)

        layer = {
            "@@type": "ScatterplotLayer",
            "data": map_df[
                ["latitude", "longitude", "status", "provider", "color_rgba"]
            ].to_dict("records"),
            "getPosition": ["longitude", "latitude"],
            "getFillColor": "@@=color_rgba",
            "getRadius": 800,
            "pickable": True,
            "opacity": 0.7,
            "radiusMinPixels": 2,
            "radiusMaxPixels": 8,
        }

        st.pydeck_chart(
            {
                "@@type": "Deck",
                "initialViewState": {
                    "latitude": 51.1,
                    "longitude": 10.4,
                    "zoom": 5.5,
                    "pitch": 0,
                },
                "layers": [layer],
                "mapStyle": "mapbox://styles/mapbox/dark-v11",
            },
            use_container_width=True,
            height=520,
        )
    else:
        st.info("No geolocation data available.")

with stats_col:
    st.subheader("Provider Breakdown")
    for slug in selected_providers:
        state = all_states[slug]
        cfg = PROVIDERS[slug]
        if state.empty:
            continue

        counts = state["status"].value_counts()
        n = len(state)
        n_in = int(counts.get(cfg["in_use"], 0))

        st.markdown(f"**{cfg['label']}** — {n:,} points")

        fig = go.Figure(
            go.Bar(
                x=counts.values,
                y=counts.index,
                orientation="h",
                marker_color=[
                    STATUS_COLORS.get(s, DEFAULT_COLOR) for s in counts.index
                ],
                text=[f"{v:,}" for v in counts.values],
                textposition="auto",
            )
        )
        fig.update_layout(
            height=180,
            margin=dict(l=0, r=0, t=5, b=5),
            xaxis=dict(visible=False),
            yaxis=dict(autorange="reversed"),
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig, use_container_width=True)

# ── Power draw timeline ──────────────────────────────────────────────
st.subheader("Estimated Power Draw (MW)")

power_traces = {}
for slug in selected_providers:
    ts = replay_power(slug, hours=timeline_hours)
    if not ts.empty:
        power_traces[slug] = ts

if power_traces:
    fig_power = go.Figure()

    # Resample all to 1-min and align
    resampled = {}
    for slug, ts in power_traces.items():
        r = ts.resample("1min").last().ffill()
        resampled[slug] = r

    # Union of indices
    if len(resampled) == 2:
        slugs = list(resampled.keys())
        idx = resampled[slugs[0]].index.union(resampled[slugs[1]].index)
        vals = {}
        for s in slugs:
            vals[s] = resampled[s].reindex(idx).ffill().fillna(0)["power_mw"]

        # Stacked area
        fig_power.add_trace(
            go.Scatter(
                x=idx,
                y=vals[slugs[0]],
                name=PROVIDERS[slugs[0]]["label"],
                fill="tozeroy",
                line=dict(width=0.5, color=PROVIDERS[slugs[0]]["color"]),
                fillcolor=PROVIDERS[slugs[0]]["color"].replace(")", ",0.3)").replace(
                    "rgb", "rgba"
                )
                if "rgb" in PROVIDERS[slugs[0]]["color"]
                else PROVIDERS[slugs[0]]["color"] + "4D",
            )
        )
        fig_power.add_trace(
            go.Scatter(
                x=idx,
                y=vals[slugs[0]] + vals[slugs[1]],
                name=PROVIDERS[slugs[1]]["label"],
                fill="tonexty",
                line=dict(width=0.5, color=PROVIDERS[slugs[1]]["color"]),
                fillcolor=PROVIDERS[slugs[1]]["color"].replace(")", ",0.3)").replace(
                    "rgb", "rgba"
                )
                if "rgb" in PROVIDERS[slugs[1]]["color"]
                else PROVIDERS[slugs[1]]["color"] + "4D",
            )
        )
    else:
        for slug, ts in resampled.items():
            fig_power.add_trace(
                go.Scatter(
                    x=ts.index,
                    y=ts["power_mw"],
                    name=PROVIDERS[slug]["label"],
                    fill="tozeroy",
                    line=dict(width=1, color=PROVIDERS[slug]["color"]),
                )
            )

    fig_power.update_layout(
        height=350,
        margin=dict(l=0, r=0, t=10, b=0),
        yaxis_title="MW (nameplate)",
        xaxis_title="",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        hovermode="x unified",
        plot_bgcolor="rgba(0,0,0,0)",
    )
    fig_power.update_xaxes(gridcolor="rgba(128,128,128,0.15)")
    fig_power.update_yaxes(gridcolor="rgba(128,128,128,0.15)", rangemode="tozero")
    st.plotly_chart(fig_power, use_container_width=True)
else:
    st.info("No power-draw data available for the selected window.")

# ── Usage timeline (snapshot_runs aggregation) ────────────────────────
st.subheader("Network Utilisation Over Time")

usage_fig = go.Figure()
for slug in selected_providers:
    runs = load_snapshot_runs(slug, hours=timeline_hours)
    cfg = PROVIDERS[slug]
    if runs.empty:
        continue
    runs["collected_at_utc"] = pd.to_datetime(runs["collected_at_utc"])
    # Only SNAPSHOT rows give full-network state
    snap = runs[runs["delivery_type"] == "SNAPSHOT"].copy()
    if snap.empty:
        continue
    in_use_col = cfg["in_use_col"]
    snap["usage_pct"] = snap[in_use_col] / snap["point_count"] * 100

    usage_fig.add_trace(
        go.Scatter(
            x=snap["collected_at_utc"],
            y=snap["usage_pct"],
            name=cfg["label"],
            mode="lines",
            line=dict(width=1.5, color=cfg["color"]),
        )
    )

usage_fig.update_layout(
    height=300,
    margin=dict(l=0, r=0, t=10, b=0),
    yaxis_title="% points in use",
    xaxis_title="",
    legend=dict(orientation="h", yanchor="bottom", y=1.02),
    hovermode="x unified",
    plot_bgcolor="rgba(0,0,0,0)",
)
usage_fig.update_xaxes(gridcolor="rgba(128,128,128,0.15)")
usage_fig.update_yaxes(gridcolor="rgba(128,128,128,0.15)", rangemode="tozero")
st.plotly_chart(usage_fig, use_container_width=True)

# ── Footer ────────────────────────────────────────────────────────────
st.divider()
st.caption(
    "Data source: [Mobilithek](https://mobilithek.info) — "
    "AFIR-mandated DATEX II feeds from German CPOs.  "
    "Power estimates use nameplate ratings; actual draw depends on "
    "vehicle SOC, cable limits, and load management."
)

# ── Auto-refresh ──────────────────────────────────────────────────────
if auto_refresh:
    import time

    time.sleep(60)
    st.rerun()
