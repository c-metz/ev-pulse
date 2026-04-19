"""EV Pulse -- Live Dashboard.

Real-time EV charging infrastructure monitor for Germany.
Map with state borders + per-provider power-draw timeline.

Run:
    streamlit run dashboard.py
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
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
    "enbw": {
        "label": "EnBW",
        "in_use": "charging",
        "in_use_col": "charging_count",
        "power_col": "point_power_w",
        "to_mw": 1e-6,
        "color": "#ff7f0e",
    },
}

BUNDESLAENDER_GEOJSON_URL = (
    "https://raw.githubusercontent.com/isellsoap/deutschlandGeoJSON"
    "/main/2_bundeslaender/4_niedrig.geo.json"
)


def _hex_to_rgba(hex_color: str, alpha: float = 0.18) -> str:
    """Convert '#1f77b4' -> 'rgba(31,119,180,0.18)' for Plotly fill colours."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


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
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "current_point_state" in tables:
            # O(n_points) lookup — maintained incrementally by the push receiver.
            # Returns empty DataFrame if not yet populated; the map will show
            # nothing until the first push delivery arrives (usually seconds).
            return pd.read_sql_query(
                "SELECT point_id, status, updated_at AS collected_at_utc"
                " FROM current_point_state",
                conn,
            )
        # Table not created yet — return empty (avoids expensive GROUP BY scan).
        return pd.DataFrame()


@st.cache_data(ttl=300)
def load_static(slug: str) -> pd.DataFrame:
    db = DATA_DIR / f"{slug}_static.sqlite"
    if not db.exists():
        return pd.DataFrame()
    with sqlite3.connect(db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(charging_points)").fetchall()}
        if "fetched_at_utc" in cols:
            # Return only the latest fetch batch; older batches are preserved
            # in the DB for history but not loaded into the dashboard.
            return pd.read_sql_query(
                """
                SELECT * FROM charging_points
                WHERE fetched_at_utc = (SELECT MAX(fetched_at_utc) FROM charging_points)
                """,
                conn,
            )
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
    """Load the MW timeline (last 8 days) aggregated to 5-minute buckets,
    returned as a **range band**: upper = raw DELTA-derived estimate
    (overestimates because the DELTA protocol accumulates upward drift),
    lower = SNAPSHOT-anchored floor (DELTA scaled multiplicatively by the
    SNAPSHOT/DELTA ratio k(t), interpolated between SNAPSHOTs).

    The band expresses the inherent uncertainty: on eco-movement SNAPSHOTs
    typically confirm only 30-50% of the DELTA-derived "in-use" count, so
    truth sits somewhere inside the band. Plotting the single "corrected"
    line falsely implied precision we don't have.

    Returns a timeline DataFrame indexed by time with columns
    ``power_mw_upper``, ``power_mw_lower`` and ``power_mw`` (midpoint,
    kept for downstream consumers: heatmap, VRE correlation, CSV export).
    """
    db = DATA_DIR / f"{slug}_dynamic.sqlite"
    empty_cols = ["power_mw", "power_mw_lower", "power_mw_upper"]
    empty = (
        pd.DataFrame(columns=empty_cols),
        pd.DataFrame(columns=["time"]),
    )
    if not db.exists():
        return empty

    # 8-day display window. Extend the query backwards to the most recent
    # SNAPSHOT before cutoff so the earliest visible bucket has an anchor
    # on its left for the k(t) interpolation.
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=8)
    cutoff_str = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")

    with sqlite3.connect(db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(snapshot_runs)").fetchall()}
        if "estimated_power_mw" not in cols:
            return empty

        # Look at most 2 days back for a pre-cutoff SNAPSHOT anchor so
        # a provider with stale SNAPSHOTs (e.g. Tesla, none in weeks)
        # doesn't force us to scan far more than the 8-day window.
        anchor_row = conn.execute(
            """SELECT MAX(collected_at_utc) FROM snapshot_runs
               WHERE delivery_type = 'SNAPSHOT'
                 AND collected_at_utc < ?
                 AND collected_at_utc >= datetime(?, '-2 days')""",
            (cutoff_str, cutoff_str),
        ).fetchone()
        anchor_time = anchor_row[0] if anchor_row else None
        query_from = anchor_time if anchor_time else cutoff_str

        # 5-minute bucketing in SQL. Separate columns for DELTA and
        # SNAPSHOT means so we can build upper (DELTA-only) and the
        # k-ratio (SNAPSHOT/DELTA) independently in Python.
        power_df = pd.read_sql_query(
            """
            SELECT
                datetime(
                    (CAST(strftime('%s', collected_at_utc) AS INTEGER) / 300) * 300,
                    'unixepoch'
                ) AS time,
                AVG(CASE WHEN delivery_type='DELTA'
                         THEN estimated_power_mw END) AS delta_mw,
                AVG(CASE WHEN delivery_type='SNAPSHOT'
                         THEN estimated_power_mw END) AS snap_mw,
                COUNT(CASE WHEN delivery_type='DELTA'    THEN 1 END) AS n_deltas,
                COUNT(CASE WHEN delivery_type='SNAPSHOT' THEN 1 END) AS n_snaps
            FROM snapshot_runs
            WHERE collected_at_utc >= ?
              AND estimated_power_mw IS NOT NULL
            GROUP BY time
            ORDER BY time
            """,
            conn,
            params=(query_from,),
        )

    if power_df.empty:
        return empty

    power_df["time"] = pd.to_datetime(power_df["time"], utc=True)
    power_df = power_df.sort_values("time").reset_index(drop=True)

    # ── Upper edge: DELTA-derived estimate (raw, overestimates) ──────
    # In the rare bucket with SNAPSHOT but no DELTA, fall back to snap_mw.
    upper = power_df["delta_mw"].where(
        power_df["delta_mw"].notna(), power_df["snap_mw"]
    ).astype(float)

    # ── Upstream-throttle detection ──────────────────────────────────
    # Buckets where DELTA count is <20% of median, in runs of >=4
    # consecutive buckets (>=20 min), are marked NaN on both edges.
    # Prevents the band from collapsing / spiking during feed stalls.
    throttle_flag = np.zeros(len(power_df), dtype=bool)
    if len(power_df) >= 12:
        counts = power_df["n_deltas"].values
        baseline = float(np.median(counts))
        if baseline >= 10:
            low = counts < 0.2 * baseline
            i = 0
            while i < len(low):
                if low[i]:
                    j = i
                    while j < len(low) and low[j]:
                        j += 1
                    if j - i >= 4:
                        throttle_flag[i:j] = True
                    i = j
                else:
                    i += 1
    upper = upper.mask(throttle_flag)

    # ── k(t): SNAPSHOT / DELTA ratio, piecewise-linear across time ───
    # For each SNAPSHOT bucket, k_i = snap_mw_i / mean(DELTA estimate in
    # ±1h window). Clipped to [0.05, 1.5] so obviously-broken snapshots
    # can't drag the floor to zero or above the upper. Interpolated
    # linearly between anchors; constant extrapolation at the endpoints.
    snap_mask = (power_df["n_snaps"] > 0) & power_df["snap_mw"].notna()
    snap_indices = np.where(snap_mask.values)[0]

    k_at_snap = np.full(len(power_df), np.nan)
    window = 12  # 12 buckets of 5 min = 1 h half-window
    for i in snap_indices:
        lo = max(0, i - window)
        hi = min(len(power_df), i + window + 1)
        window_vals = upper.iloc[lo:hi].dropna()
        if len(window_vals) == 0:
            continue
        delta_est = float(window_vals.mean())
        snap_val = float(power_df["snap_mw"].iat[i])
        if delta_est <= 0 or snap_val <= 0:
            continue
        k_at_snap[i] = float(np.clip(snap_val / delta_est, 0.05, 1.5))

    k_series = pd.Series(k_at_snap, index=power_df.index)
    if k_series.notna().any():
        k_series = k_series.interpolate(method="linear", limit_direction="both")
    else:
        # No usable SNAPSHOTs in window -- band collapses to single line.
        k_series = pd.Series(1.0, index=power_df.index)

    lower = upper * k_series

    # ── Short-segment filter ─────────────────────────────────────────
    # Drop contiguous non-NaN runs shorter than 24 buckets (2 h). These
    # tiny islands (usually partial feed recovery) clutter the plot and
    # are never long enough to trust.
    def _drop_short_runs(s: pd.Series, min_len: int = 24) -> pd.Series:
        out = s.copy()
        valid = out.notna().values
        i = 0
        while i < len(valid):
            if valid[i]:
                j = i
                while j < len(valid) and valid[j]:
                    j += 1
                if j - i < min_len:
                    out.iloc[i:j] = np.nan
                i = j
            else:
                i += 1
        return out

    upper = _drop_short_runs(upper)
    lower = _drop_short_runs(lower)

    # ── 60-min centred rolling mean (visualisation only) ─────────────
    if len(upper) >= 12:
        upper_s = upper.rolling(12, min_periods=6, center=True).mean()
        lower_s = lower.rolling(12, min_periods=6, center=True).mean()
        # Re-impose gaps so throttle/short-segment zones stay broken.
        mask = upper.isna() | lower.isna()
        upper_s[mask.values] = np.nan
        lower_s[mask.values] = np.nan
        upper = upper_s
        lower = lower_s

    # Physical lower bound: P = SUM(nameplate_rated_kW over in-use) >= 0.
    upper = upper.clip(lower=0)
    lower = lower.clip(lower=0)
    # Numerical guard: lower should never exceed upper.
    lower = pd.concat([lower, upper], axis=1).min(axis=1)

    midpoint = (upper + lower) / 2.0

    timeline = pd.DataFrame({
        "power_mw_upper": upper.values,
        "power_mw_lower": lower.values,
        "power_mw": midpoint.values,
    }, index=power_df["time"])
    timeline.index.name = "time"
    timeline = timeline.sort_index()

    # Trim to 8-day display window (pre-cutoff anchor row was only for k).
    if getattr(timeline.index, "tz", None) is None:
        cutoff_ts = pd.Timestamp(cutoff_dt.replace(tzinfo=None))
    else:
        cutoff_ts = pd.Timestamp(cutoff_dt)
    timeline = timeline[timeline.index >= cutoff_ts]

    snap_df = power_df.loc[snap_mask, ["time"]].copy()
    if not snap_df.empty:
        snap_t = snap_df["time"]
        snap_tz = getattr(snap_t.dt, "tz", None)
        snap_cutoff = (
            pd.Timestamp(cutoff_dt.replace(tzinfo=None))
            if snap_tz is None else pd.Timestamp(cutoff_dt)
        )
        snap_df = snap_df[snap_t >= snap_cutoff]

    return timeline, snap_df


@st.cache_data(ttl=300)
def get_dynamic_time_range(slug: str) -> tuple[datetime, datetime] | None:
    """Earliest and latest collected_at_utc in the last 8 days of data."""
    db = DATA_DIR / f"{slug}_dynamic.sqlite"
    if not db.exists():
        return None
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            """SELECT MIN(collected_at_utc), MAX(collected_at_utc)
               FROM snapshot_runs
               WHERE collected_at_utc >= datetime('now', '-8 days')"""
        ).fetchone()
    if not row or not row[0]:
        return None
    return (
        datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc),
        datetime.fromisoformat(row[1]).replace(tzinfo=timezone.utc),
    )


@st.cache_data(ttl=60, max_entries=300)
def load_state_at(slug: str, ts_iso: str) -> pd.DataFrame:
    """Reconstruct each point's last-known status as of *ts_iso*.

    Fast path: if ts_iso is within the last 5 minutes, return
    current_point_state directly (O(n_points), avoids full history scan).
    Historical path: bounded GROUP BY over the 8-day window.
    """
    db = DATA_DIR / f"{slug}_dynamic.sqlite"
    if not db.exists():
        return pd.DataFrame()

    # Fast path: requested time ≈ "now" → current_point_state is exact
    try:
        ts_dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        age_minutes = (datetime.now(timezone.utc) - ts_dt).total_seconds() / 60
    except Exception:
        age_minutes = 999

    with sqlite3.connect(db) as conn:
        if age_minutes < 5:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            if "current_point_state" in tables:
                df = pd.read_sql_query(
                    "SELECT point_id, status FROM current_point_state", conn
                )
                if not df.empty:
                    return df

        # Historical path: bounded to 8-day window so idx_psh_time can
        # pre-filter rows before the GROUP BY.
        return pd.read_sql_query(
            """
            SELECT h.point_id, h.status
            FROM point_status_history h
            INNER JOIN (
                SELECT point_id, MAX(collected_at_utc) AS max_time
                FROM point_status_history
                WHERE collected_at_utc >= datetime('now', '-8 days')
                  AND collected_at_utc <= ?
                GROUP BY point_id
            ) latest ON h.point_id = latest.point_id
                    AND h.collected_at_utc = latest.max_time
            """,
            conn,
            params=(ts_iso,),
        )


@st.cache_data(ttl=3600)
def fetch_de_renewables(start_iso: str, end_iso: str) -> pd.DataFrame:
    """Fetch German VRE generation (Solar + Wind on/offshore) from energy-charts.info.

    Returns hourly DataFrame indexed by UTC time with column 'vre_mw'.
    """
    url = "https://api.energy-charts.info/public_power"
    params = {"country": "de", "start": start_iso[:10], "end": end_iso[:10]}
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return pd.DataFrame()

    times_raw = data.get("unix_seconds") or []
    types = data.get("production_types") or []
    if not times_raw or not types:
        return pd.DataFrame()

    times = pd.to_datetime(times_raw, unit="s", utc=True)
    vre_names = {"Solar", "Wind onshore", "Wind offshore"}
    parts = {}
    for pt in types:
        name = pt.get("name", "")
        if name in vre_names:
            arr = pd.Series(pt.get("data") or [], index=times)
            arr = pd.to_numeric(arr, errors="coerce").fillna(0.0)
            parts[name] = arr
    if not parts:
        return pd.DataFrame()
    df = pd.DataFrame(parts)
    df["vre_mw"] = df.sum(axis=1)
    # Resample to 1h means (energy-charts native is 15-min)
    return df.resample("1h").mean()


@st.cache_data(ttl=600)
def load_eco_price_timeseries() -> pd.DataFrame:
    """Hourly mean of observed price_per_kwh in eco-movement dynamic DB."""
    db = DATA_DIR / "eco_dynamic.sqlite"
    if not db.exists():
        return pd.DataFrame()
    with sqlite3.connect(db) as conn:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(point_status_history)"
        ).fetchall()}
        if "price_per_kwh" not in cols:
            return pd.DataFrame()
        df = pd.read_sql_query(
            """
            SELECT collected_at_utc AS time, price_per_kwh
            FROM point_status_history
            WHERE price_per_kwh IS NOT NULL AND price_per_kwh != ''
              AND collected_at_utc >= datetime('now', '-8 days')
            """,
            conn,
        )
    if df.empty:
        return pd.DataFrame()
    df["price_per_kwh"] = pd.to_numeric(df["price_per_kwh"], errors="coerce")
    df = df.dropna(subset=["price_per_kwh"])
    if df.empty:
        return df
    df["time"] = pd.to_datetime(df["time"], format="ISO8601", utc=True)
    return (
        df.set_index("time")
          .sort_index()["price_per_kwh"]
          .resample("1h").mean()
          .to_frame("avg_price_eur_kwh")
    )


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
        "**Methodology** -- Each provider delivers a full-state "
        "**SNAPSHOT** roughly once per day, with incremental **DELTA** "
        "updates in between. The DELTA protocol is lossy (the Mobilithek "
        "packet buffer overwrites unread deliveries), so the DELTA-only "
        "state drifts upward over the day. Each line is drawn as a "
        "**range band**: the upper edge is the raw DELTA-derived "
        "estimate (overstates active chargers), the lower edge scales "
        "the DELTA shape by the SNAPSHOT/DELTA ratio observed at the "
        "nearest ground-truth anchor. **True load sits inside the "
        "band**; the band's width is the uncertainty. Upstream-throttle "
        "periods are shown as gaps. A 60-minute centred rolling mean is "
        "applied for visualisation only (DB rows are untouched)."
    )
    st.caption(
        "**Power estimation** -- "
        "P = SUM(nameplate_rated_kW) for all points with status = in_use. "
        "This is an upper bound. Actual grid draw depends on vehicle SOC, "
        "charging curve, cable limits, and load management."
    )
    st.caption(
        "**Granularity** -- The timeline is aggregated to 5-minute "
        "buckets for a responsive interface. The underlying SQLite "
        "databases retain every status transition at native resolution; "
        "for the raw data, check out the "
        "[GitHub repository](https://github.com/c-metz/ev-pulse)."
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

    # ── Per-provider range band (upper=raw DELTA, lower=SNAPSHOT-scaled)
    # Truth sits inside the band. Width expresses the DELTA-drift
    # uncertainty: narrow near a SNAPSHOT anchor, wider further away.
    for slug, ts in power_traces.items():
        cfg = PROVIDERS[slug]
        if ts["power_mw_upper"].isna().all():
            continue
        label = cfg["label"]
        color = cfg["color"]
        fill_rgba = _hex_to_rgba(color, 0.18)

        # Upper edge: invisible line that the fill anchors against.
        fig.add_trace(go.Scatter(
            x=ts.index, y=ts["power_mw_upper"],
            name=f"{label} upper",
            mode="lines",
            line=dict(width=0, color=color),
            showlegend=False,
            hoverinfo="skip",
            connectgaps=False,
        ))
        # Lower edge: visible line, fills up to the previous trace.
        upper_cd = ts["power_mw_upper"].values.reshape(-1, 1)
        fig.add_trace(go.Scatter(
            x=ts.index, y=ts["power_mw_lower"],
            name=label,
            mode="lines",
            line=dict(width=1.4, color=color),
            fill="tonexty",
            fillcolor=fill_rgba,
            connectgaps=False,
            customdata=upper_cd,
            hovertemplate=(
                "%{y:.0f}–%{customdata[0]:.0f} MW"
                "<extra>" + label + "</extra>"
            ),
        ))

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

    st.caption(
        "Each shaded band shows the uncertainty range for that provider. "
        "The lower edge is anchored to upstream SNAPSHOT ground truth "
        "(typically 30-50% of the raw DELTA estimate on eco-movement -- "
        "the DELTA stream has accumulated upward drift since the last "
        "SNAPSHOT). The upper edge is the uncorrected DELTA-derived "
        "estimate. See [GitHub](https://github.com/c-metz/ev-pulse) for "
        "methodology."
    )

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
        "CSV export contains the midpoint of each provider's uncertainty "
        "band (1-minute resolution, forward-filled from 5-minute buckets). "
        "Power = SUM(nameplate-rated kW) for all in-use points."
    )
else:
    st.info("No power-draw data available.")


# ═══════════════════════════════════════════════════════════════════════
#  DENSITY MAP  (time-scrubbable heatmap of in-use chargers)
# ═══════════════════════════════════════════════════════════════════════

st.divider()
st.markdown("#### Where charging is happening")

geojson_data = load_bundeslaender()

# Find a global time range across all selected providers
from datetime import timedelta as _td

ranges = [r for r in (get_dynamic_time_range(s) for s in selected_providers) if r]
if ranges:
    t_min_raw = min(r[0] for r in ranges)
    t_max_raw = max(r[1] for r in ranges)
    t_min = t_min_raw.replace(minute=0, second=0, microsecond=0)
    t_max = t_max_raw.replace(minute=0, second=0, microsecond=0)
    if t_max < t_max_raw:
        t_max = t_max + _td(hours=1)

    # Hourly slider; default = "now" (latest)
    chosen_time = st.slider(
        "Snapshot time (UTC)",
        min_value=t_min,
        max_value=t_max,
        value=t_max,
        step=_td(hours=1),
        format="MMM DD, HH:mm",
    )
    chosen_iso = chosen_time.replace(tzinfo=None).isoformat() + "+00:00"

    active_rows = []
    for slug in selected_providers:
        static = all_statics[slug]
        cfg = PROVIDERS[slug]
        if static.empty:
            continue
        state = load_state_at(slug, chosen_iso)
        if state.empty:
            continue
        active = state.loc[state["status"].eq(cfg["in_use"])].merge(
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
            f"Density of **{len(density_df):,}** in-use chargers at "
            f"**{chosen_time:%a %b %d, %H:%M UTC}** "
            f"({', '.join(PROVIDERS[s]['label'] for s in selected_providers)}). "
            "Drag the slider to see how the geographic load profile evolves — "
            "compare commuter cities at 08:00 with highway corridors at 02:00."
        )
    else:
        st.info("No active chargers at this timestamp.")
else:
    st.info("No data available.")



# ═══════════════════════════════════════════════════════════════════════
#  DIURNAL × WEEKDAY LOAD SHAPE
# ═══════════════════════════════════════════════════════════════════════

st.divider()
st.markdown("#### Load shape — hour of day × day of week")
st.caption(
    "Average MW in each (weekday, hour) bucket across the full history. "
    "The shape a load forecaster needs in one glance: where the morning "
    "ramp starts, where the evening peak lands, weekend vs weekday."
)

DOW_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

heatmap_cols = st.columns(max(1, len(power_traces)))
for col, (slug, ts) in zip(heatmap_cols, power_traces.items()):
    cfg = PROVIDERS[slug]
    hourly = ts.resample("1h").mean()
    if hourly.empty or hourly["power_mw"].isna().all():
        col.info(f"{cfg['label']}: not enough data.")
        continue
    hourly = hourly.copy()
    hourly["dow"] = hourly.index.dayofweek
    hourly["hour"] = hourly.index.hour
    pivot = hourly.pivot_table(
        index="dow", columns="hour", values="power_mw", aggfunc="mean"
    ).reindex(index=range(7), columns=range(24))

    fig_h = go.Figure(go.Heatmap(
        z=pivot.values,
        x=list(range(24)),
        y=DOW_LABELS,
        colorscale="Viridis",
        colorbar=dict(title="MW", thickness=10),
        hovertemplate="%{y} %{x}:00 — %{z:.0f} MW<extra></extra>",
    ))
    fig_h.update_layout(
        title=dict(text=cfg["label"], font=dict(size=13), x=0.02),
        height=260,
        margin=dict(l=0, r=0, t=30, b=0),
        xaxis_title="Hour (UTC)",
        yaxis_title="",
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    fig_h.update_xaxes(dtick=2)
    col.plotly_chart(fig_h, use_container_width=True)


# ═══════════════════════════════════════════════════════════════════════
#  RENEWABLES CORRELATION  (EV charging MW vs Solar+Wind generation)
# ═══════════════════════════════════════════════════════════════════════

st.divider()
st.markdown("#### EV charging vs German renewables (Solar + Wind)")
st.caption(
    "EV charging load overlaid on national VRE generation (Solar + Wind on/offshore, "
    "via [energy-charts.info](https://energy-charts.info)). "
    "A negative correlation would suggest charging happens *outside* of high-VRE "
    "windows; a positive correlation hints at smart charging soaking up cheap "
    "renewable surplus. Zero ≈ EV load is renewables-agnostic and follows pure "
    "human routine."
)

if power_traces:
    t_start = min(ts.index.min() for ts in power_traces.values())
    t_end = max(ts.index.max() for ts in power_traces.values())
    vre_df = fetch_de_renewables(
        pd.Timestamp(t_start).isoformat(),
        (pd.Timestamp(t_end) + pd.Timedelta(days=1)).isoformat(),
    )

    if vre_df.empty:
        st.info("Could not fetch renewables data from energy-charts.info.")
    else:
        # Build a unified hourly frame: each provider's MW + vre_mw
        ev_hourly = pd.DataFrame()
        for slug, ts in power_traces.items():
            label = PROVIDERS[slug]["label"]
            h = ts["power_mw"].resample("1h").mean()
            ev_hourly[label] = h
        ev_hourly = ev_hourly.dropna(how="all")

        merged = ev_hourly.join(vre_df["vre_mw"], how="inner").dropna(how="any")

        if merged.empty:
            st.info("No overlapping time range with VRE data yet.")
        else:
            fig_v = go.Figure()
            for slug in power_traces:
                label = PROVIDERS[slug]["label"]
                if label in merged.columns:
                    fig_v.add_trace(go.Scatter(
                        x=merged.index, y=merged[label],
                        name=f"{label} (MW, left)",
                        line=dict(width=1.4, color=PROVIDERS[slug]["color"]),
                        yaxis="y1",
                        hovertemplate="%{y:.0f} MW<extra>" + label + "</extra>",
                    ))
            fig_v.add_trace(go.Scatter(
                x=merged.index, y=merged["vre_mw"] / 1000.0,
                name="DE Solar + Wind (GW, right)",
                line=dict(width=1.2, color="#2ca02c", dash="dot"),
                yaxis="y2",
                hovertemplate="%{y:.1f} GW<extra>VRE</extra>",
            ))
            fig_v.update_layout(
                height=320,
                margin=dict(l=0, r=0, t=10, b=0),
                yaxis=dict(title="EV MW", rangemode="tozero",
                           gridcolor="rgba(128,128,128,0.12)"),
                yaxis2=dict(title="VRE (GW)", overlaying="y", side="right",
                            rangemode="tozero", showgrid=False),
                xaxis=dict(title="Time (UTC)",
                           gridcolor="rgba(128,128,128,0.12)"),
                legend=dict(orientation="h", yanchor="bottom", y=1.02),
                hovermode="x unified",
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
            )
            st.plotly_chart(fig_v, use_container_width=True)

            # Correlation block
            metric_cols = st.columns(len(power_traces))
            for col, slug in zip(metric_cols, power_traces):
                label = PROVIDERS[slug]["label"]
                if label not in merged.columns:
                    continue
                r = merged[[label, "vre_mw"]].corr().iloc[0, 1]
                col.metric(
                    f"corr({label}, VRE)",
                    f"{r:+.2f}",
                    help="Pearson r over hourly samples. "
                         "Range −1 (anti-correlated) to +1 (in lockstep).",
                )


# ═══════════════════════════════════════════════════════════════════════
#  CONSUMER PRICE vs RENEWABLES  (eco-movement EUR/kWh)
# ═══════════════════════════════════════════════════════════════════════

st.divider()
st.markdown("#### Consumer charging price vs renewables")
st.caption(
    "Hourly mean of all observed eco-movement EUR/kWh tariffs against German "
    "VRE generation."
)

price_df = load_eco_price_timeseries()
if price_df.empty:
    st.info("No price observations in the database yet.")
else:
    # Use the same vre_df from above if available; else refetch
    try:
        vre_df_p = vre_df  # noqa: F821
    except NameError:
        vre_df_p = fetch_de_renewables(
            price_df.index.min().isoformat(),
            (price_df.index.max() + pd.Timedelta(days=1)).isoformat(),
        )

    fig_p = go.Figure()
    fig_p.add_trace(go.Scatter(
        x=price_df.index,
        y=price_df["avg_price_eur_kwh"],
        name="Avg price (EUR/kWh)",
        line=dict(width=1.4, color="#9467bd"),
        yaxis="y1",
        hovertemplate="€%{y:.3f}/kWh<extra></extra>",
    ))
    if isinstance(vre_df_p, pd.DataFrame) and not vre_df_p.empty:
        fig_p.add_trace(go.Scatter(
            x=vre_df_p.index, y=vre_df_p["vre_mw"] / 1000.0,
            name="DE VRE (GW)",
            line=dict(width=1.0, color="#2ca02c", dash="dot"),
            yaxis="y2",
            hovertemplate="%{y:.1f} GW<extra>VRE</extra>",
        ))
    fig_p.update_layout(
        height=300,
        margin=dict(l=0, r=0, t=10, b=0),
        yaxis=dict(title="EUR / kWh",
                   gridcolor="rgba(128,128,128,0.12)"),
        yaxis2=dict(title="VRE (GW)", overlaying="y", side="right",
                    rangemode="tozero", showgrid=False),
        xaxis=dict(title="Time (UTC)",
                   gridcolor="rgba(128,128,128,0.12)"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        hovermode="x unified",
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(fig_p, use_container_width=True)

    # Quick stats
    stat_cols = st.columns(3)
    stat_cols[0].metric(
        "Median tariff",
        f"€{price_df['avg_price_eur_kwh'].median():.3f}/kWh",
    )
    stat_cols[1].metric(
        "IQR",
        f"€{price_df['avg_price_eur_kwh'].quantile(0.25):.3f} – "
        f"€{price_df['avg_price_eur_kwh'].quantile(0.75):.3f}",
    )
    if isinstance(vre_df_p, pd.DataFrame) and not vre_df_p.empty:
        joined = price_df.join(vre_df_p["vre_mw"], how="inner").dropna()
        if len(joined) >= 5:
            r_pv = joined[["avg_price_eur_kwh", "vre_mw"]].corr().iloc[0, 1]
            stat_cols[2].metric(
                "corr(price, VRE)", f"{r_pv:+.2f}",
                help="Pearson r over hourly samples.",
            )


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
