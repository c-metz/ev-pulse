"""EV Pulse -- Live Dashboard (eco-movement focus).

Real-time view of EV charger load on the eco-movement network in
Germany, with raw SQLite from all collected providers downloadable
at the bottom of the page.

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
import streamlit as st

# ── Page config ───────────────────────────────────────────────────────
st.set_page_config(
    page_title="EV Pulse -- Germany",
    page_icon="⚡",
    layout="wide",
)

DATA_DIR = Path("data")

# Public download base URL (served by nginx /data/ alias).
DOWNLOAD_BASE = "https://178-104-101-146.sslip.io/data"

# Featured providers (plotted on the chart). All others appear only
# in the download section at the bottom.
FEATURED = [
    {
        "slug": "eco",
        "label": "eco-movement",
        "in_use": "charging",
        "color": "#1f77b4",
    },
    {
        "slug": "tesla",
        "label": "Tesla",
        "in_use": "occupied",
        "color": "#d62728",
    },
]

# Days of history shown on the chart.
WINDOW_DAYS = 7


def _hex_to_rgba(hex_color: str, alpha: float = 0.18) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def _human_size(n_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n_bytes < 1024:
            return f"{n_bytes:.1f} {unit}" if unit != "B" else f"{n_bytes} {unit}"
        n_bytes /= 1024
    return f"{n_bytes:.1f} PB"


def _human_age(ts: datetime) -> str:
    delta = datetime.now(timezone.utc) - ts
    secs = delta.total_seconds()
    if secs < 60:
        return f"{int(secs)}s ago"
    if secs < 3600:
        return f"{int(secs/60)}m ago"
    if secs < 86400:
        return f"{secs/3600:.1f}h ago"
    return f"{secs/86400:.1f}d ago"


# ═══════════════════════════════════════════════════════════════════════
#  DATA LOADING
# ═══════════════════════════════════════════════════════════════════════
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
            return pd.read_sql_query(
                "SELECT point_id, status, updated_at AS collected_at_utc"
                " FROM current_point_state",
                conn,
            )
        return pd.DataFrame()


@st.cache_data(ttl=60)
def load_latest_snapshot(slug: str) -> dict:
    """Most recent snapshot_run row for headline KPIs.

    The in-use count column is provider-specific (``charging_count`` for
    eco-movement / EnBW / Qwello, ``occupied_count`` for Tesla / SMATRICS).
    We pick whichever exists in the schema to stay agnostic.
    """
    db = DATA_DIR / f"{slug}_dynamic.sqlite"
    out: dict = {
        "collected_at": None, "delivery_type": None,
        "in_use_count": None, "estimated_power_mw": None,
        "total_runs": 0,
    }
    if not db.exists():
        return out
    with sqlite3.connect(db) as conn:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(snapshot_runs)"
        ).fetchall()}
        # Pick the in-use column that exists for this provider.
        in_use_col = next(
            (c for c in ("charging_count", "occupied_count") if c in cols),
            None,
        )
        select_in_use = in_use_col if in_use_col else "NULL"
        row = conn.execute(
            f"SELECT collected_at_utc, delivery_type, {select_in_use},"
            f" estimated_power_mw FROM snapshot_runs"
            f" ORDER BY snapshot_id DESC LIMIT 1"
        ).fetchone()
        if row:
            out["collected_at"] = datetime.fromisoformat(row[0])
            out["delivery_type"] = row[1]
            out["in_use_count"] = row[2]
            out["estimated_power_mw"] = row[3]
        out["total_runs"] = conn.execute(
            "SELECT COUNT(*) FROM snapshot_runs"
        ).fetchone()[0]
    return out


@st.cache_data(ttl=60)
def load_power_band(slug: str) -> pd.DataFrame:
    """Load the 8-day MW timeline as a SNAPSHOT-anchored uncertainty band.

    Upper edge = raw DELTA-derived estimate (overestimates because the
    DELTA stream drifts upward between SNAPSHOTs).
    Lower edge = DELTA shape scaled by the SNAPSHOT/DELTA ratio k(t),
    interpolated linearly between SNAPSHOT anchors.
    Midpoint  = the visual line between the two.

    Returns a 5-min-bucket DataFrame indexed by time with columns
    ``power_mw_lower``, ``power_mw``, ``power_mw_upper``.
    """
    db = DATA_DIR / f"{slug}_dynamic.sqlite"
    empty = pd.DataFrame(columns=["power_mw", "power_mw_lower", "power_mw_upper"])
    if not db.exists():
        return empty

    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    cutoff_str = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")

    with sqlite3.connect(db) as conn:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(snapshot_runs)"
        ).fetchall()}
        if "estimated_power_mw" not in cols:
            return empty

        anchor_row = conn.execute(
            """SELECT MAX(collected_at_utc) FROM snapshot_runs
               WHERE delivery_type='SNAPSHOT'
                 AND collected_at_utc < ?
                 AND collected_at_utc >= datetime(?, '-2 days')""",
            (cutoff_str, cutoff_str),
        ).fetchone()
        anchor_time = anchor_row[0] if anchor_row else None
        query_from = anchor_time if anchor_time else cutoff_str

        df = pd.read_sql_query(
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

    if df.empty:
        return empty

    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.sort_values("time").reset_index(drop=True)

    # ── Upper: DELTA-derived estimate (raw, overestimates) ──────────
    upper = df["delta_mw"].where(df["delta_mw"].notna(), df["snap_mw"]).astype(float)

    # ── Upstream-throttle detection ────────────────────────────────
    throttle_flag = np.zeros(len(df), dtype=bool)
    if len(df) >= 12:
        counts = df["n_deltas"].values
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

    # ── k(t): SNAPSHOT/DELTA ratio piecewise-linear across time ────
    snap_mask = (df["n_snaps"] > 0) & df["snap_mw"].notna()
    snap_indices = np.where(snap_mask.values)[0]

    k_at_snap = np.full(len(df), np.nan)
    window = 12  # ±1h half-window
    for i in snap_indices:
        lo = max(0, i - window)
        hi = min(len(df), i + window + 1)
        win_vals = upper.iloc[lo:hi].dropna()
        if len(win_vals) == 0:
            continue
        d_est = float(win_vals.mean())
        s_val = float(df["snap_mw"].iat[i])
        if d_est <= 0 or s_val <= 0:
            continue
        k_at_snap[i] = float(np.clip(s_val / d_est, 0.10, 1.0))

    k_series = pd.Series(k_at_snap, index=df.index)
    if k_series.notna().any():
        k_series = k_series.interpolate(method="linear", limit_direction="both")
    else:
        k_series = pd.Series(1.0, index=df.index)

    lower = upper * k_series

    # ── Drop short runs (<2h) ──────────────────────────────────────
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

    # ── 60-min centred rolling mean (cosmetic) ─────────────────────
    if len(upper) >= 12:
        upper_s = upper.rolling(12, min_periods=6, center=True).mean()
        lower_s = lower.rolling(12, min_periods=6, center=True).mean()
        gap = upper.isna() | lower.isna()
        upper_s[gap.values] = np.nan
        lower_s[gap.values] = np.nan
        upper, lower = upper_s, lower_s

    upper = upper.clip(lower=0)
    lower = lower.clip(lower=0)
    lower = pd.concat([lower, upper], axis=1).min(axis=1)
    midpoint = (upper + lower) / 2.0

    timeline = pd.DataFrame({
        "power_mw_upper": upper.values,
        "power_mw_lower": lower.values,
        "power_mw": midpoint.values,
    }, index=df["time"])
    timeline.index.name = "time"
    timeline = timeline.sort_index()

    cutoff_ts = pd.Timestamp(cutoff_dt)
    timeline = timeline[timeline.index >= cutoff_ts]
    return timeline


def list_data_files() -> list[dict]:
    """Inventory of every .sqlite in data/ for the download section."""
    out = []
    if not DATA_DIR.exists():
        return out
    for p in sorted(DATA_DIR.glob("*.sqlite")):
        try:
            stat = p.stat()
            out.append({
                "name": p.name,
                "size_bytes": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            })
        except OSError:
            continue
    return out


# ═══════════════════════════════════════════════════════════════════════
#  PAGE
# ═══════════════════════════════════════════════════════════════════════

# ── CSS ──────────────────────────────────────────────────────────────
st.markdown("""
<style>
  .block-container { padding-top: 2rem; padding-bottom: 2rem; max-width: 1200px; }
  [data-testid="stMetricValue"] { font-size: 1.7rem; }
  [data-testid="stMetricLabel"]  { color: #888; font-size: 0.85rem; }
  .download-row {
    display: flex; align-items: center; justify-content: space-between;
    padding: 0.4rem 0; border-bottom: 1px solid rgba(128,128,128,0.15);
  }
  .download-row:last-child { border-bottom: none; }
  .download-row a { text-decoration: none; }
  .download-meta { color: #888; font-size: 0.85rem; }
</style>
""", unsafe_allow_html=True)

# ── Header ───────────────────────────────────────────────────────────
featured_labels = " & ".join(p["label"] for p in FEATURED)
st.markdown(
    "<h1 style='margin-bottom:0'>EV Pulse &mdash; Germany</h1>"
    "<p style='color:gray;margin-top:0;font-size:0.9em'>"
    f"Live EV charger load on {featured_labels} ({WINDOW_DAYS}-day trend) "
    "&ensp;|&ensp; AFIR / DATEX II via "
    "<a href='https://mobilithek.info' style='color:gray'>Mobilithek</a>"
    f"&ensp;|&ensp;{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC"
    "</p>",
    unsafe_allow_html=True,
)

# ── Load data once for both providers ────────────────────────────────
provider_data: list[dict] = []
for p in FEATURED:
    band = load_power_band(p["slug"])
    latest = load_latest_snapshot(p["slug"])
    state = load_current_state(p["slug"])

    if not band.empty and band["power_mw"].notna().any():
        cur_mw = float(band["power_mw"].dropna().iloc[-1])
        peak_mw = float(band["power_mw"].max())
    else:
        cur_mw = peak_mw = None

    charging_now = (
        int((state["status"] == p["in_use"]).sum())
        if not state.empty and "status" in state.columns
        else None
    )

    provider_data.append({
        **p,
        "band": band,
        "latest": latest,
        "cur_mw": cur_mw,
        "peak_mw": peak_mw,
        "charging_now": charging_now,
    })

# ── KPI strip — 2 rows of 4 (one row per provider) ───────────────────
for d in provider_data:
    st.markdown(
        f"<p style='margin-top:0.6rem;margin-bottom:0.2rem;font-weight:600;"
        f"color:{d['color']}'>{d['label']}</p>",
        unsafe_allow_html=True,
    )
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        "Current load",
        f"{d['cur_mw']:.0f} MW" if d["cur_mw"] is not None else "—",
        help="Midpoint of the SNAPSHOT-anchored uncertainty band, latest 5-min bucket.",
    )
    c2.metric(
        f"Peak last {WINDOW_DAYS} d",
        f"{d['peak_mw']:.0f} MW" if d["peak_mw"] is not None else "—",
    )
    c3.metric(
        "In-use now",
        f"{d['charging_now']:,}" if d["charging_now"] is not None else "—",
        help=f"Points with status='{d['in_use']}' in the latest state.",
    )
    c4.metric(
        "Last update",
        _human_age(d["latest"]["collected_at"]) if d["latest"]["collected_at"] else "—",
        help=(
            f"Last delivery: {d['latest']['delivery_type']}"
            if d["latest"]["delivery_type"] else "No deliveries yet."
        ),
    )

# ── Combined power band chart ────────────────────────────────────────
st.markdown("&nbsp;", unsafe_allow_html=True)

any_data = any(
    not d["band"].empty and d["band"]["power_mw"].notna().any()
    for d in provider_data
)

if any_data:
    fig = go.Figure()

    for d in provider_data:
        band = d["band"]
        if band.empty or band["power_mw"].isna().all():
            continue
        color = d["color"]
        fill_rgba = _hex_to_rgba(color, 0.10)

        # Invisible upper edge — fill anchor.
        fig.add_trace(go.Scatter(
            x=band.index, y=band["power_mw_upper"],
            mode="lines", line=dict(width=0, color=color),
            showlegend=False, hoverinfo="skip", connectgaps=False,
        ))
        # Invisible lower edge — fills upward to the upper.
        fig.add_trace(go.Scatter(
            x=band.index, y=band["power_mw_lower"],
            mode="lines", line=dict(width=0, color=color),
            fill="tonexty", fillcolor=fill_rgba,
            showlegend=False, hoverinfo="skip", connectgaps=False,
        ))
        # Midpoint — primary visual.
        mid_cd = np.stack([
            band["power_mw_lower"].values,
            band["power_mw_upper"].values,
        ], axis=-1)
        fig.add_trace(go.Scatter(
            x=band.index, y=band["power_mw"],
            name=d["label"], mode="lines",
            line=dict(width=1.8, color=color),
            connectgaps=False, customdata=mid_cd,
            hovertemplate=(
                "%{y:.0f} MW  (range %{customdata[0]:.0f}–%{customdata[1]:.0f})"
                f"<extra>{d['label']}</extra>"
            ),
        ))

    fig.update_layout(
        height=380,
        margin=dict(l=0, r=0, t=10, b=0),
        yaxis_title="MW", xaxis_title="Time (UTC)",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
    )
    fig.update_xaxes(gridcolor="rgba(128,128,128,0.12)")
    fig.update_yaxes(gridcolor="rgba(128,128,128,0.12)", rangemode="tozero")

    st.plotly_chart(fig, use_container_width=True)

    st.caption(
        "**Methodology** — each network publishes a full **SNAPSHOT** "
        "roughly once per day with incremental **DELTA** updates in "
        "between. The DELTA stream drifts upward (Mobilithek's packet "
        "buffer overwrites unread deliveries). Each shaded band shows "
        "uncertainty: upper edge is the raw DELTA-derived estimate, "
        "lower edge is anchored to the nearest SNAPSHOT ground truth "
        "(typically 30–50% of DELTA on eco-movement). True load sits "
        "inside the band. "
        "P = SUM(nameplate-rated kW) for in-use points — an upper bound; "
        "actual grid draw depends on vehicle SOC, charging curve and "
        "load management."
    )

    # ── CSV download for the combined timeline ──────────────────────
    csv_frames = []
    for d in provider_data:
        if d["band"].empty:
            continue
        resampled = d["band"].resample("1min").last().ffill()
        resampled = resampled.rename(columns={
            "power_mw":       f"{d['label']} MW (mid)",
            "power_mw_lower": f"{d['label']} MW (low)",
            "power_mw_upper": f"{d['label']} MW (high)",
        })
        csv_frames.append(resampled)
    if csv_frames:
        csv_df = pd.concat(csv_frames, axis=1).sort_index()
        csv_df.index.name = "time_utc"
        st.download_button(
            label="📥 Download timeline (.csv)",
            data=csv_df.to_csv(),
            file_name=f"ev_pulse_load_{datetime.now(timezone.utc):%Y%m%d_%H%M}.csv",
            mime="text/csv",
        )
else:
    st.info("No power-draw data available yet.")

# ── Load shape — hour × day-of-week heatmap ──────────────────────────
st.divider()
st.markdown(f"#### Load shape — hour of day × day of week (last {WINDOW_DAYS} days)")
st.caption(
    "Average MW load by hour-of-day (UTC) and day-of-week, computed "
    "from the band midpoint. Reveals weekday/weekend split, daily "
    "commute peaks, and overnight troughs. Use this to spot whether "
    "charging tracks a residential, commercial, or mixed pattern."
)

DOW_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
heatmap_cols = st.columns(len(provider_data))

for col, d in zip(heatmap_cols, provider_data):
    band = d["band"]
    if band.empty or band["power_mw"].isna().all():
        col.info(f"{d['label']}: not enough data.")
        continue

    hourly = band.resample("1h").mean(numeric_only=True)
    if hourly.empty or hourly["power_mw"].isna().all():
        col.info(f"{d['label']}: not enough data.")
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
        title=dict(
            text=d["label"],
            font=dict(size=13, color=d["color"]),
            x=0.02,
        ),
        height=280,
        margin=dict(l=0, r=0, t=30, b=0),
        xaxis_title="Hour (UTC)",
        yaxis_title="",
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    fig_h.update_xaxes(dtick=2)
    col.plotly_chart(fig_h, use_container_width=True)

# ── Raw data section ─────────────────────────────────────────────────
st.divider()
st.markdown("### Raw SQLite — all collected providers")
st.markdown(
    "Data from **EnBW, ladenetz, Qwello and SMATRICS** is also being "
    "collected, alongside the eco-movement and Tesla feeds shown above. "
    "Their schemas and delivery cadences vary considerably — feeds have "
    "idiosyncrasies (delayed SNAPSHOTs, intra-delivery duplicates, "
    "incomplete metadata, missing fields) which are interesting to "
    "analyse but make a single consolidated dashboard view misleading. "
    "All raw SQLite databases are available below for offline analysis."
)

files = list_data_files()
if files:
    for f in files:
        url = f"{DOWNLOAD_BASE}/{f['name']}"
        size_str = _human_size(f["size_bytes"])
        age_str = _human_age(f["mtime"])
        st.markdown(
            f"<div class='download-row'>"
            f"<div><a href='{url}' download><strong>{f['name']}</strong></a></div>"
            f"<div class='download-meta'>{size_str} &nbsp;·&nbsp; updated {age_str}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )
    st.caption(
        "Each `*_static.sqlite` contains charging-point metadata "
        "(location, operator, connector types, rated power). Each "
        "`*_dynamic.sqlite` contains the live status feed: a "
        "`snapshot_runs` table with one row per delivery (counts and "
        "estimated MW), `point_status_history` with change-only "
        "transitions, and `current_point_state` with the latest status "
        "per point. See the "
        "[GitHub repo](https://github.com/c-metz/ev-pulse) for schema "
        "details and the `eco_movement_pipeline.ipynb` notebook for an "
        "end-to-end walk-through of how the data is parsed and stored."
    )
else:
    st.info("No data files available.")

# ── Footer ───────────────────────────────────────────────────────────
st.divider()
st.caption(
    "EV Pulse · charles.metz@yahoo.de · "
    "[GitHub](https://github.com/c-metz/ev-pulse) · "
    "Auto-refresh every 60 s."
)

# ── Auto-refresh (60s) ───────────────────────────────────────────────
st.markdown(
    "<meta http-equiv='refresh' content='60'>",
    unsafe_allow_html=True,
)
