# EV Pulse

Real-time monitoring of Germany's public EV charging infrastructure.

EV Pulse ingests live DATEX II status feeds from
[Mobilithek](https://mobilithek.info) -- the German national mobility
data platform mandated by the EU Alternative Fuels Infrastructure
Regulation (AFIR) -- stores every status transition in a local time-series
database, and serves a live dashboard showing which chargers are active
and how much power the network is drawing.

| Metric | Value |
|---|---|
| Charging points monitored | ~50 000 |
| Providers active | eco-movement, Tesla Supercharger |
| Polling interval | ~60 s (delta-pull) |
| Ground-truth refresh | ~1x / day (SNAPSHOT delivery) |

---

## Architecture

```
Mobilithek (DATEX II / JSON over mTLS)
  |
  |  delta-pull: If-Modified-Since cursor, ~1 req/s
  v
collector.py                 runs continuously on a server
  |
  |  SQLite (WAL mode, per-provider)
  |    {slug}_static.sqlite    infrastructure metadata
  |    {slug}_dynamic.sqlite   status transitions + aggregate runs
  v
dashboard.py                 Streamlit app, reads SQLite
  |
  v
Browser: live map + power-draw timeline with SNAPSHOT markers
```

### Data flow in detail

1. **Static metadata** (locations, rated power, connector types) is
   refreshed every 12 hours from the provider's infrastructure feed.

2. **Dynamic status** (the real-time operating state of every charging
   point) arrives via Mobilithek's delta-pull protocol:
   - **SNAPSHOT** deliveries contain the complete state of all charging
     points. These arrive roughly once per day and serve as
     **ground truth**.
   - **DELTA** deliveries contain only the points whose status changed
     since the last delivery. These arrive every few seconds.

3. The collector applies change-detection and stores only actual status
   transitions, keeping the database compact while preserving the full
   event history.

4. The dashboard replays the stored transitions and renders an interactive
   map and power-draw timeline with SNAPSHOT boundaries clearly marked.

### Delta drift and correction

The delta-pull protocol is inherently lossy: Mobilithek's packet buffer
overwrites previous deliveries, so if a charging point transitions
multiple times between consecutive polls, intermediate states are lost.
Over hours, this causes the accumulated state to **drift** -- typically
overestimating the number of active chargers because "charging ->
available" transitions are missed more often than the reverse.

Each SNAPSHOT delivery resets the state to ground truth. The analysis
notebook (`read_mobilithek.ipynb`) applies a **proportional drift
correction** that linearly distributes the accumulated error between
consecutive SNAPSHOTs, preserving the shape of delta-driven fluctuations
while eliminating discontinuities.

On the dashboard, SNAPSHOT boundaries are marked with **dashed vertical
lines** so the viewer can distinguish ground-truth anchors from
delta-derived estimates at a glance.

---

## Currently supported providers

| Provider | ~Points | Feed type | Notes |
|----------|---------|-----------|-------|
| **eco-movement** | 45 000 | DATEX II JSON, mTLS | Largest open dataset on Mobilithek |
| **Tesla Supercharger** | 5 000 | DATEX II JSON, mTLS | Static data also available via public endpoint |

Additional providers (Eliso, MSU, Wirelane) have implemented parsers in
`providers/` but are not yet wired into the dashboard. Not all German
charge point operators have onboarded their real-time feeds to Mobilithek;
eco-movement and Tesla represent the largest currently available datasets.

Adding a new Mobilithek data source requires subclassing `Provider` from
`providers/base.py` and implementing four methods:

```
providers/
  base.py               Abstract interface + shared DATEX II helpers
  eco_movement.py       eco-movement (~45k points)
  tesla.py              Tesla Supercharger (~5k points)
  eliso.py              Eliso (OCPI-like, experimental)
  msu.py                MSU (DATEX II, experimental)
  wirelane.py           Wirelane (DATEX II, experimental)
```

---

## Setup

### Prerequisites

- Python 3.11+
- A [Mobilithek](https://mobilithek.info) account with an mTLS client
  certificate (PKCS#12 `.p12` file). Register, then subscribe to the
  desired charging infrastructure feeds.

### Install

```bash
git clone https://github.com/c-metz/ev-pulse.git
cd ev-pulse
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
# Edit .env with your certificate path and password
```

```env
MOBILITHEK_CERT_PATH=./certificate.p12
MOBILITHEK_CERT_PASSWORD=your-certificate-password
```

Place your `.p12` certificate in the project root. It is git-ignored.

### Run the collector

```bash
python collector.py --list                    # show available providers
python collector.py eco_movement              # run continuously
python collector.py tesla                     # run continuously
python collector.py eco_movement --once -v    # single cycle, verbose
python collector.py tesla --compact           # deduplicate history
```

Run one collector instance per provider. For production, use `systemd`,
`supervisord`, or `tmux`.

### Run the dashboard

```bash
streamlit run dashboard.py
```

Opens at `http://localhost:8501`. Auto-refresh is available in the
sidebar.

---

## Data model

### Static database (`{slug}_static.sqlite`)

Full infrastructure snapshot, refreshed every 12 h.

| Column | Description |
|---|---|
| `point_id` | Unique charging point identifier |
| `latitude`, `longitude` | WGS 84 coordinates |
| `point_power_w` or `point_power_kw` | Nameplate rated power |
| `connector_types` | Plug standards (CCS, Type 2, CHAdeMO, ...) |
| `operator_name` | Charge point operator |
| `city`, `postcode`, `address_line` | Location |

### Dynamic database (`{slug}_dynamic.sqlite`)

| Table | Purpose |
|---|---|
| `snapshot_runs` | One row per collection cycle: aggregate counts, delivery type, estimated power |
| `point_status_history` | One row per actual status *transition* per point |
| `collector_state` | Cursor for delta-pull resumption |

The `delivery_type` column in `snapshot_runs` distinguishes `SNAPSHOT`
(full ground truth) from `DELTA` (incremental update). This distinction
is critical for interpreting data quality.

---

## Important disclaimers

**This is an experimental research project.** It is provided as-is for
educational and analytical purposes only.

### Data accuracy

- **SNAPSHOT data** (marked with dashed lines on the timeline) represents
  the complete state as reported by the upstream provider at that moment.
  This is the most reliable data available.

- **Between SNAPSHOTs**, the state is reconstructed from incremental
  delta updates. Due to the lossy nature of the Mobilithek delta-pull
  protocol (packet buffer overwrites, second-level timestamp resolution,
  upstream aggregation), some status transitions are missed. Values
  between SNAPSHOTs should be treated as **estimates with an upward
  bias** -- the system tends to overcount active chargers.

- **Power draw estimates** are computed as:

  ```
  estimated_power = SUM(nameplate_rated_power)
                    for all points WHERE status = "in use"
  ```

  This is an **upper bound**, not actual metered consumption. Real power
  draw depends on vehicle state of charge, charging curve, cable
  limitations, load management, and other factors not captured in this
  dataset.

### No warranty

This project is not affiliated with Mobilithek, eco-movement, Tesla, or
any charge point operator. The author accepts no responsibility for
decisions made based on this data.

**Do not use this for safety-critical, financial, or regulatory
purposes.**

### Data source

All data is sourced from [Mobilithek](https://mobilithek.info). Access
requires registration and acceptance of their terms of use.

---

## License

MIT
