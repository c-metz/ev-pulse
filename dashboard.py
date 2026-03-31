"""EV Pulse -- Live Dashboard.

Real-time EV charging infrastructure monitor for Germany.
Reads the collector's SQLite databases and presents a geographic map
and estimated power-draw timeline with SNAPSHOT ground-truth markers.

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
import pydeck as pdk
import streamlit as st

# ── Page config ───────────────────────────────────────────────────────
st.set_page_config(
    page_title="EV Pulse -- Germany",
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
    "outOfOrder": "#95a5a6",
    "unknown": "#bdc3c7",
    "removed": "#7f8c8d",
    "reserved": "#f39c12",
    "inoperative": "#95a5a6",
    "blocked": "#e67e22",
}
DEFAULT_COLOR = "#bdc3c7"


def hex_to_rgba(hex_str: str, alpha: int = 180) -> list[int]:
    h = hex_str.lstrip("#")
    return [int(h[i : i + 2], 16) for i in (0, 2, 4)] + [alpha]


def hex_to_rgba_str(hex_color: str, opacity: float = 0.3) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{opacity})"


# ═══════════════════════════════════════════════════════════════════════
#  DATA LOADING  (cached 60 s)
# ═══════════════════════════════════════════════════════════════════════
@st.cache_data(ttl=60)
def load_current_state(slug: str) -> pd.DataFrame:
    """Most recent status per charging point (last SNAPSHOT + deltas)."""
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
    """Static metadata -- coordinates, power ratings, etc."""
    db = DATA_DIR / f"{slug}_static.sqlite"
    if not db.exists():
        return pd.DataFrame()
    with sqlite3.connect(db) as conn:
        return pd.read_sql_query("SELECT * FROM charging_points", conn)


@st.cache_data(ttl=60)
def load_power_and_snapshots(slug: str, hours: int = 48) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load pre-computed MW timeline and SNAPSHOT timestamps."""
    db = DATA_DIR / f"{slug}_dynamic.sqlite"
    empty = pd.DataFrame(columns=["time", "power_mw"]), pd.DataFrame()
    if not db.exists():
        return empty

    cutoff = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .__sub__(pd.Timedelta(hours=hours))
        .isoformat()
    )

    with sqlite3.connect(db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(snapshot_runs)").fetchall()}
        if "estimated_power_mw" not in cols:
            return empty

        power_df = pd.read_sql_query(
            """
            SELECT collected_at_utc AS time,
                   estimated_power_mw AS power_mw,
                   delivery_type
            FROM snapshot_runs
            WHERE collected_at_utc >= ?
              AND estimated_power_mw IS NOT NULL
            ORDER BY snapshot_id
            """,
            conn,
            params=(cutoff,),
        )

    if power_df.empty:
        return empty

    power_df["time"] = pd.to_datetime(power_df["time"])

    snapshots = power_df[power_df["delivery_type"] == "SNAPSHOT"][["time", "power_mw"]].copy()
    timeline = power_df[["time", "power_mw"]].set_index("time").sort_index()

    return timeline, snapshots


# ═══════════════════════════════════════════════════════════════════════
#  DASHBOARD
# ═══════════════════════════════════════════════════════════════════════

# ── Header ────────────────────────────────────────────────────────────
st.markdown(
    "<h1 style='margin-bottom:0'>EV Pulse &mdash; Germany</h1>"
    "<p style='color:gray;margin-top:0;font-size:0.95em'>"
    "Real-time EV charging infrastructure &ensp;|&ensp;"
    "AFIR / DATEX II via "
    "<a href='https://mobilithek.info' style='color:gray'>Mobilithek</a>"
    f"&ensp;|&ensp;{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC"
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

    st.divider()
    st.caption(
        "**Data quality note** -- Between SNAPSHOT deliveries "
        "(vertical dashed lines on the timeline), values are derived "
        "from incremental delta updates and may drift from reality. "
        "SNAPSHOTs are ground truth."
    )
    st.caption(
        "Power estimates use nameplate ratings (max rated power "
        "x 1 if in use, x 0 otherwise). Actual grid draw depends "
        "on vehicle SOC, cable limits, and load management."
    )

# ── Load data ─────────────────────────────────────────────────────────
all_states: dict[str, pd.DataFrame] = {}
all_statics: dict[str, pd.DataFrame] = {}

for slug in selected_providers:
    all_states[slug] = load_current_state(slug)
    all_statics[slug] = load_static(slug)

# ── KPI row ───────────────────────────────────────────────────────────
total_points = 0
total_in_use = 0
total_available = 0
total_mw = 0.0

for slug in selected_providers:
    state = all_states[slug]
    static = all_statics[slug]
    cfg = PROVIDERS[slug]
    if state.empty:
        continue

    total_points += len(state)
    total_in_use += int((state["status"] == cfg["in_use"]).sum())
    total_available += int((state["status"] == "available").sum())

    power_col = cfg["power_col"]
    if not static.empty and power_col in static.columns:
        in_use_ids = set(state.loc[state["status"] == cfg["in_use"], "point_id"])
        pw = static.loc[static["point_id"].isin(in_use_ids), power_col]
        total_mw += pd.to_numeric(pw, errors="coerce").fillna(0).sum() * cfg["to_mw"]

usage_pct = (total_in_use / total_points * 100) if total_points else 0

cols = st.columns(5)
cols[0].metric("Charging Points", f"{total_points:,}")
cols[1].metric("In Use", f"{total_in_use:,}")
cols[2].metric("Available", f"{total_available:,}")
cols[3].metric("Est. Load", f"{total_mw:,.0f} MW")
cols[4].metric("Usage Rate", f"{usage_pct:.1f} %")

st.markdown("")  # spacer

# ═══════════════════════════════════════════════════════════════════════
#  MAP
# ═══════════════════════════════════════════════════════════════════════
st.subheader("Live Charging Point Status")

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
    merged["color_rgba"] = merged["status"].map(
        lambda s: hex_to_rgba(STATUS_COLORS.get(s, DEFAULT_COLOR))
    )
    map_rows.append(merged)

if map_rows:
    map_df = pd.concat(map_rows, ignore_index=True)

    layer = pdk.Layer(
        "ScatterplotLayer",
        data=map_df[["latitude", "longitude", "status", "provider", "color_rgba", "point_id"]],
        get_position=["longitude", "latitude"],
        get_fill_color="color_rgba",
        get_radius=800,
        pickable=True,
        opacity=0.7,
        radius_min_pixels=2,
        radius_max_pixels=8,
    )

    view_state = pdk.ViewState(latitude=51.1, longitude=10.4, zoom=5.5, pitch=0)

    st.pydeck_chart(
        pdk.Deck(
            layers=[layer],
            initial_view_state=view_state,
            map_style="mapbox://styles/mapbox/dark-v11",
            tooltip={
                "html": "<b>{provider}</b><br/>ID: {point_id}<br/>Status: {status}",
                "style": {"backgroundColor": "#1a1a2e", "color": "white", "fontSize": "12px"},
            },
        ),
        use_container_width=True,
        height=520,
    )

    # Legend
    legend_items = []
    for status, color in STATUS_COLORS.items():
        if status in map_df["status"].values:
            n = int((map_df["status"] == status).sum())
            legend_items.append(
                f'<span style="color:{color}">&#9679;</span> {status} ({n:,})'
            )
    if legend_items:
        st.markdown(
            "&emsp;".join(legend_items),
            unsafe_allow_html=True,
        )
else:
    st.info("No geolocation data available.")


# ═══════════════════════════════════════════════════════════════════════
#  ESTIMATED POWER DRAW TIMELINE
# ═══════════════════════════════════════════════════════════════════════
st.subheader("Estimated Power Draw")

power_traces: dict[str, pd.DataFrame] = {}
snapshot_markers: dict[str, pd.DataFrame] = {}

for slug in selected_providers:
    timeline, snapshots = load_power_and_snapshots(slug, hours=timeline_hours)
    if not timeline.empty:
        power_traces[slug] = timeline
        snapshot_markers[slug] = snapshots

if power_traces:
    fig = go.Figure()

    # Resample all to 1-min and align
    resampled = {}
    for slug, ts in power_traces.items():
        resampled[slug] = ts.resample("1min").last().ffill()

    if len(resampled) == 2:
        slugs = list(resampled.keys())
        idx = resampled[slugs[0]].index.union(resampled[slugs[1]].index)
        vals = {}
        for s in slugs:
            vals[s] = resampled[s].reindex(idx).ffill().fillna(0)["power_mw"]

        # Stacked area
        fig.add_trace(go.Scatter(
            x=idx, y=vals[slugs[0]],
            name=PROVIDERS[slugs[0]]["label"],
            fill="tozeroy",
            line=dict(width=0.5, color=PROVIDERS[slugs[0]]["color"]),
            fillcolor=hex_to_rgba_str(PROVIDERS[slugs[0]]["color"]),
            hovertemplate="%{y:.0f} MW<extra>" + PROVIDERS[slugs[0]]["label"] + "</extra>",
        ))
        fig.add_trace(go.Scatter(
            x=idx, y=vals[slugs[0]] + vals[slugs[1]],
            name=PROVIDERS[slugs[1]]["label"],
            fill="tonexty",
            line=dict(width=0.5, color=PROVIDERS[slugs[1]]["color"]),
            fillcolor=hex_to_rgba_str(PROVIDERS[slugs[1]]["color"]),
            hovertemplate="%{y:.0f} MW<extra>Total</extra>",
        ))
    else:
        for slug, ts in resampled.items():
            fig.add_trace(go.Scatter(
                x=ts.index, y=ts["power_mw"],
                name=PROVIDERS[slug]["label"],
                fill="tozeroy",
                line=dict(width=1, color=PROVIDERS[slug]["color"]),
                hovertemplate="%{y:.0f} MW<extra>" + PROVIDERS[slug]["label"] + "</extra>",
            ))

    # ── SNAPSHOT markers ──────────────────────────────────────────────
    # Collect all unique SNAPSHOT timestamps across providers
    snap_times = set()
    for slug, snaps in snapshot_markers.items():
        for t in snaps["time"]:
            snap_times.add(t)

    for snap_t in sorted(snap_times):
        fig.add_vline(
            x=snap_t,
            line=dict(color="rgba(255,255,255,0.35)", width=1, dash="dash"),
            annotation=dict(
                text="SNAPSHOT",
                font=dict(size=9, color="rgba(255,255,255,0.5)"),
                yref="paper", y=1.0,
                showarrow=False,
            ),
        )

    fig.update_layout(
        height=400,
        margin=dict(l=0, r=0, t=30, b=0),
        yaxis_title="MW (nameplate rating)",
        xaxis_title="",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        hovermode="x unified",
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    fig.update_xaxes(gridcolor="rgba(128,128,128,0.12)")
    fig.update_yaxes(gridcolor="rgba(128,128,128,0.12)", rangemode="tozero")
    st.plotly_chart(fig, use_container_width=True)

    st.caption(
        "Dashed vertical lines mark **SNAPSHOT** deliveries -- full ground-truth state "
        "resets from the upstream data provider. Between snapshots, values are derived "
        "from incremental delta updates and may overestimate actual usage due to missed "
        "status transitions. "
        "Power is estimated as: sum of nameplate-rated power across all in-use charging points."
    )
else:
    st.info("No power-draw data available for the selected window.")


# ── Footer ────────────────────────────────────────────────────────────
st.divider()
st.caption(
    "Data: [Mobilithek](https://mobilithek.info) AFIR / DATEX II feeds "
    "&ensp;|&ensp; "
    "This is an experimental research tool. No guarantees are made regarding "
    "accuracy or completeness. See README for details."
)

# ── Auto-refresh ──────────────────────────────────────────────────────
if auto_refresh:
    import time

    time.sleep(60)
    st.rerun()
