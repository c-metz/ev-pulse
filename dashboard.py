"""EV Pulse -- Live Dashboard.

Real-time EV charging infrastructure monitor for Germany.
Map with state borders + per-provider power-draw timeline.

Run:
    streamlit run dashboard.py
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import pydeck as pdk
import requests
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
        "label": "Tesla",
        "in_use": "occupied",
        "in_use_col": "occupied_count",
        "power_col": "point_power_kw",
        "to_mw": 1e-3,
        "color": "#d62728",
    },
}

BUNDESLAENDER_GEOJSON_URL = (
    "https://raw.githubusercontent.com/isellsoap/deutschlandGeoJSON"
    "/main/2_bundeslaender/4_niedrig.geo.json"
)


# ═══════════════════════════════════════════════════════════════════════
#  DATA LOADING
# ═══════════════════════════════════════════════════════════════════════
@st.cache_data(ttl=3600)
def load_bundeslaender() -> dict | None:
    try:
        resp = requests.get(BUNDESLAENDER_GEOJSON_URL, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


@st.cache_data(ttl=60)
def load_current_state(slug: str) -> pd.DataFrame:
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
    db = DATA_DIR / f"{slug}_static.sqlite"
    if not db.exists():
        return pd.DataFrame()
    with sqlite3.connect(db) as conn:
        return pd.read_sql_query("SELECT * FROM charging_points", conn)


@st.cache_data(ttl=60)
def load_snapshot_meta(slug: str) -> dict:
    """Load SNAPSHOT metadata for data quality reporting."""
    db = DATA_DIR / f"{slug}_dynamic.sqlite"
    meta: dict = {"snapshots": 0, "last_snapshot": None, "last_snapshot_age_h": None,
                  "total_runs": 0, "first_data": None}
    if not db.exists():
        return meta
    with sqlite3.connect(db) as conn:
        meta["total_runs"] = conn.execute("SELECT COUNT(*) FROM snapshot_runs").fetchone()[0]
        snaps = conn.execute(
            "SELECT collected_at_utc FROM snapshot_runs "
            "WHERE delivery_type = 'SNAPSHOT' ORDER BY snapshot_id"
        ).fetchall()
        meta["snapshots"] = len(snaps)
        if snaps:
            last = datetime.fromisoformat(snaps[-1][0])
            meta["last_snapshot"] = last
            meta["last_snapshot_age_h"] = (
                datetime.now(timezone.utc) - last
            ).total_seconds() / 3600
        first = conn.execute(
            "SELECT collected_at_utc FROM snapshot_runs ORDER BY snapshot_id LIMIT 1"
        ).fetchone()
        if first:
            meta["first_data"] = datetime.fromisoformat(first[0])
    return meta


@st.cache_data(ttl=60)
def load_power_and_snapshots(slug: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load the full MW timeline with drift correction, plus SNAPSHOT timestamps."""
    db = DATA_DIR / f"{slug}_dynamic.sqlite"
    empty = pd.DataFrame(columns=["time", "power_mw"]), pd.DataFrame(columns=["time"])
    if not db.exists():
        return empty

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
            ORDER BY snapshot_id
            """,
            conn,
        )

    if power_df.empty:
        return empty

    power_df["time"] = pd.to_datetime(power_df["time"], format="ISO8601")

    # ── Fill NULL power on SNAPSHOT rows ─────────────────────────────
    snap_mask = power_df["delivery_type"] == "SNAPSHOT"
    for idx in power_df.index[snap_mask & power_df["power_mw"].isna()]:
        after = power_df.loc[idx + 1:, "power_mw"].dropna()
        if not after.empty:
            power_df.at[idx, "power_mw"] = after.iloc[0]

    power_df = power_df.dropna(subset=["power_mw"]).reset_index(drop=True)

    # ── Proportional drift correction (vectorised) ─────────────────
    snap_mask = power_df["delivery_type"] == "SNAPSHOT"
    snap_indices = power_df.index[snap_mask].tolist()
    power_vals = power_df["power_mw"].values.copy()
    times_i64 = power_df["time"].values.astype("int64")

    for i in range(len(snap_indices) - 1):
        idx_a = snap_indices[i]
        idx_b = snap_indices[i + 1]
        if idx_b <= idx_a + 1:
            continue
        power_at_b = power_vals[idx_b]
        power_before_b = power_vals[idx_b - 1]
        jump = power_before_b - power_at_b
        if abs(jump) < 0.01:
            continue
        t_a = times_i64[idx_a]
        t_b = times_i64[idx_b]
        span = t_b - t_a
        if span <= 0:
            continue
        sl = slice(idx_a + 1, idx_b)
        frac = (times_i64[sl] - t_a) / span
        power_vals[sl] -= jump * frac

    power_df["power_mw"] = power_vals

    snap_df = power_df.loc[snap_mask, ["time"]].copy()
    timeline = power_df[["time", "power_mw"]].set_index("time").sort_index()

    # Return the last SNAPSHOT time for uncorrected-region shading
    return timeline, snap_df



# ═══════════════════════════════════════════════════════════════════════
#  DASHBOARD
# ═══════════════════════════════════════════════════════════════════════

# ── Mobile-friendly CSS ───────────────────────────────────────────────
st.markdown(
    """
    <style>
      /* Tighten default page padding so plots breathe on phones */
      .block-container {
          padding-top: 1rem;
          padding-bottom: 1rem;
          padding-left: 0.6rem;
          padding-right: 0.6rem;
          max-width: 100% !important;
      }
      /* Smaller H1 on small screens */
      @media (max-width: 640px) {
          h1 { font-size: 1.4rem !important; }
          .block-container { padding-left: 0.3rem; padding-right: 0.3rem; }
          [data-testid="stPlotlyChart"] { min-height: 260px; }
      }
      /* Make pydeck and plotly fill the column on phones */
      [data-testid="stDeckGlJsonChart"] > div { width: 100% !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── Header ────────────────────────────────────────────────────────────
st.markdown(
    "<h1 style='margin-bottom:0'>EV Pulse &mdash; Germany</h1>"
    "<p style='color:gray;margin-top:0;font-size:0.9em'>"
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
    auto_refresh = st.toggle("Auto-refresh (60 s)", value=False)

    # ── Data quality panel ────────────────────────────────────────────
    st.divider()
    st.subheader("Data Quality")

    for slug in selected_providers:
        cfg = PROVIDERS[slug]
        meta = load_snapshot_meta(slug)
        age_str = (
            f"{meta['last_snapshot_age_h']:.0f} h ago"
            if meta["last_snapshot_age_h"] is not None
            else "never"
        )
        st.markdown(f"**{cfg['label']}**")
        col1, col2 = st.columns(2)
        col1.metric("SNAPSHOTs", meta["snapshots"])
        col2.metric("Last SNAPSHOT", age_str)

    st.divider()
    st.caption(
        "**Methodology** -- Dashed vertical lines on the timeline mark "
        "**SNAPSHOT** deliveries (complete ground-truth state resets from "
        "the upstream data provider). Between snapshots, the state is "
        "reconstructed from incremental **DELTA** updates via Mobilithek's "
        "delta-pull protocol. "
        "Because some status transitions are missed between polls "
        "(the Mobilithek packet buffer overwrites unread deliveries), "
        "the accumulated state drifts -- typically overestimating active "
        "chargers. A **proportional drift correction** is applied: the "
        "error is linearly distributed between consecutive SNAPSHOT "
        "anchors, preserving the shape of intraday fluctuations. "
        "The shaded region after the last SNAPSHOT has not been corrected."
    )
    st.caption(
        "**Power estimation** -- "
        "P = SUM(nameplate_rated_kW) for all points with status = in_use. "
        "This is an upper bound. Actual grid draw depends on vehicle SOC, "
        "charging curve, cable limits, and load management."
    )

# ── Load data ─────────────────────────────────────────────────────────
all_states: dict[str, pd.DataFrame] = {}
all_statics: dict[str, pd.DataFrame] = {}

for slug in selected_providers:
    all_states[slug] = load_current_state(slug)
    all_statics[slug] = load_static(slug)


# ═══════════════════════════════════════════════════════════════════════
#  POWER DRAW TIMELINE  (top of page — drift-corrected, ALL data)
# ═══════════════════════════════════════════════════════════════════════

power_traces: dict[str, pd.DataFrame] = {}
snapshot_markers: dict[str, pd.DataFrame] = {}

for slug in selected_providers:
    timeline, snapshots = load_power_and_snapshots(slug)
    if not timeline.empty:
        power_traces[slug] = timeline
        snapshot_markers[slug] = snapshots

if power_traces:
    fig = go.Figure()

    for slug, ts in power_traces.items():
        cfg = PROVIDERS[slug]
        resampled = ts.resample("1min").last().ffill()
        fig.add_trace(go.Scatter(
            x=resampled.index,
            y=resampled["power_mw"],
            name=cfg["label"],
            mode="lines",
            line=dict(width=1.2, color=cfg["color"]),
            hovertemplate="%{y:.0f} MW<extra>" + cfg["label"] + "</extra>",
        ))

    # ── SNAPSHOT markers ──────────────────────────────────────────────
    snap_times = set()
    for snaps in snapshot_markers.values():
        for t in snaps["time"]:
            snap_times.add(t)

    last_snap_time = max(snap_times) if snap_times else None

    for snap_t in sorted(snap_times):
        x_val = pd.Timestamp(snap_t).to_pydatetime()
        fig.add_shape(
            type="line", x0=x_val, x1=x_val,
            y0=0, y1=1, yref="paper",
            line=dict(color="rgba(255,255,255,0.3)", width=1, dash="dash"),
        )
        fig.add_annotation(
            x=x_val, y=1.0, yref="paper",
            text="SNAPSHOT",
            font=dict(size=9, color="rgba(255,255,255,0.45)"),
            showarrow=False, yanchor="bottom",
        )

    # ── Shaded uncorrected region after last SNAPSHOT ─────────────────
    if last_snap_time is not None:
        x_end = max(ts.index.max() for ts in power_traces.values())
        last_snap_dt = pd.Timestamp(last_snap_time).to_pydatetime()
        x_end_dt = pd.Timestamp(x_end).to_pydatetime()
        if x_end_dt > last_snap_dt:
            fig.add_shape(
                type="rect",
                x0=last_snap_dt, x1=x_end_dt,
                y0=0, y1=1, yref="paper",
                fillcolor="rgba(255, 165, 0, 0.07)",
                line=dict(width=0),
                layer="below",
            )
            mid_x = last_snap_dt + (x_end_dt - last_snap_dt) / 2
            fig.add_annotation(
                x=mid_x, y=0.97, yref="paper",
                text="not yet drift-corrected",
                font=dict(size=10, color="rgba(255, 180, 80, 0.7)"),
                showarrow=False,
            )

    fig.update_layout(
        height=350,
        margin=dict(l=0, r=0, t=10, b=0),
        yaxis_title="MW",
        xaxis_title="Time (UTC)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        hovermode="x unified",
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    fig.update_xaxes(gridcolor="rgba(128,128,128,0.12)")
    fig.update_yaxes(gridcolor="rgba(128,128,128,0.12)", rangemode="tozero")

    st.plotly_chart(fig, use_container_width=True)

    # ── Download: timeline data as CSV ────────────────────────────────
    csv_frames = []
    for slug, ts in power_traces.items():
        col_name = PROVIDERS[slug]["label"]
        resampled = ts.resample("1min").last().ffill()
        resampled = resampled.rename(columns={"power_mw": f"{col_name} (MW)"})
        csv_frames.append(resampled)
    if csv_frames:
        csv_df = pd.concat(csv_frames, axis=1).sort_index()
        csv_df.index.name = "time_utc"
        st.download_button(
            label="📥 Download data (.csv)",
            data=csv_df.to_csv(),
            file_name=f"ev_pulse_data_{datetime.now(timezone.utc):%Y%m%d_%H%M}.csv",
            mime="text/csv",
        )

    st.caption(
        "Dashed lines = **SNAPSHOT** ground truth. "
        "Between snapshots, a proportional drift correction is applied "
        "to compensate for missed delta transitions. "
        "The orange-shaded region after the last SNAPSHOT is raw (uncorrected). "
        "Power = SUM(nameplate-rated kW) for all in-use points."
    )
else:
    st.info("No power-draw data available.")


# ═══════════════════════════════════════════════════════════════════════
#  DENSITY MAP  (heatmap of currently in-use chargers, bottom of page)
# ═══════════════════════════════════════════════════════════════════════

st.divider()
st.markdown("#### Where charging is happening right now")

geojson_data = load_bundeslaender()

active_rows = []
for slug in selected_providers:
    state = all_states[slug]
    static = all_statics[slug]
    cfg = PROVIDERS[slug]
    if state.empty or static.empty:
        continue
    in_use_mask = state["status"].eq(cfg["in_use"])
    active = state.loc[in_use_mask].merge(
        static[["point_id", "latitude", "longitude"]],
        on="point_id",
        how="inner",
    ).dropna(subset=["latitude", "longitude"])
    if not active.empty:
        active_rows.append(active[["latitude", "longitude"]])

if active_rows:
    density_df = pd.concat(active_rows, ignore_index=True)
    density_df["weight"] = 1

    layers = []
    if geojson_data is not None:
        layers.append(
            pdk.Layer(
                "GeoJsonLayer",
                data=geojson_data,
                stroked=True,
                filled=False,
                get_line_color=[255, 255, 255, 70],
                line_width_min_pixels=1,
                pickable=False,
            )
        )
    layers.append(
        pdk.Layer(
            "HeatmapLayer",
            data=density_df,
            get_position=["longitude", "latitude"],
            get_weight="weight",
            radius_pixels=40,
            intensity=1.0,
            threshold=0.03,
            aggregation="SUM",
        )
    )

    view_state = pdk.ViewState(latitude=51.1, longitude=10.4, zoom=5.2, pitch=0)
    st.pydeck_chart(
        pdk.Deck(
            layers=layers,
            initial_view_state=view_state,
            map_style="mapbox://styles/mapbox/dark-v11",
        ),
        use_container_width=True,
        height=520,
    )
    st.caption(
        f"Density heatmap of **{len(density_df):,}** chargers currently in use "
        f"({', '.join(PROVIDERS[s]['label'] for s in selected_providers)}). "
        "Hotspots concentrate around metropolitan corridors and highway junctions."
    )
else:
    st.info("No active chargers to display.")


# ── Footer ────────────────────────────────────────────────────────────
st.divider()
st.caption(
    "Data: [Mobilithek](https://mobilithek.info) AFIR / DATEX II feeds "
    "&ensp;|&ensp; "
    "[Source code](https://github.com/c-metz/ev-pulse) "
    "&ensp;|&ensp; "
    "Experimental research tool -- no guarantees on accuracy or completeness."
)

# ── Auto-refresh ──────────────────────────────────────────────────────
if auto_refresh:
    import time

    time.sleep(60)
    st.rerun()
