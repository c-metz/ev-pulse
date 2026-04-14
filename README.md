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
| Charging points monitored | ~62 000 |
| Providers active | eco-movement, Tesla, EnBW, ladenetz |
| Data delivery | Push (Mobilithek → push_receiver) |
| Ground-truth refresh | ~1× / day (SNAPSHOT delivery) |

**[▶ Live Dashboard](http://178.104.101.146:8501)** — interactive map
and power-draw timeline, updated every 60 seconds.

---

## Architecture

```
Mobilithek (DATEX II / JSON+XML over mTLS)
  │
  │  Push delivery: Mobilithek POSTs to callback URL
  │  (gzip-compressed, SNAPSHOT + DELTA)
  v
push_receiver.py              FastAPI app on uvicorn (port 8100)
  │                           nginx terminates TLS, reverse-proxies
  │
  │  Reuses collector.py DB logic
  │    SQLite (WAL mode, per-provider)
  │      {slug}_static.sqlite    infrastructure metadata
  │      {slug}_dynamic.sqlite   status transitions + aggregate runs
  v
dashboard.py                  Streamlit app, reads SQLite
  │
  v
Browser: live map + power-draw timeline with SNAPSHOT markers
```

### Data flow in detail

1. **Static metadata** (locations, rated power, connector types) is
   delivered via Mobilithek push whenever the upstream provider publishes
   an update.

2. **Dynamic status** (the real-time operating state of every charging
   point) arrives via Mobilithek's push protocol:
   - **SNAPSHOT** deliveries contain the complete state of all charging
     points. These arrive roughly once per day and serve as
     **ground truth**.
   - **DELTA** deliveries contain only the points whose status changed
     since the last delivery. These arrive every few seconds.
   - No-op DELTAs (0 actual changes) are detected and skipped to reduce
     write volume.

3. The push receiver (`push_receiver.py`) auto-detects gzip compression,
   JSON/XML format, and routes each delivery to the correct provider
   parser and database via subscription ID mapping.

4. The dashboard replays the stored transitions and renders an interactive
   map and power-draw timeline with SNAPSHOT boundaries clearly marked.

### Delta drift and correction

The delta protocol is inherently lossy: if a charging point transitions
multiple times between consecutive deliveries, intermediate states are
lost. Over hours, this causes the accumulated state to **drift** --
typically overestimating the number of active chargers because "charging →
available" transitions are missed more often than the reverse.

Each SNAPSHOT delivery resets the state to ground truth. The dashboard
applies a **proportional drift correction** that linearly distributes the
accumulated error between consecutive SNAPSHOTs, preserving the shape of
delta-driven fluctuations while eliminating discontinuities.

On the dashboard, SNAPSHOT boundaries are marked with **dashed vertical
lines** so the viewer can distinguish ground-truth anchors from
delta-derived estimates at a glance.

---

## Currently supported providers

| Provider | ~Points | Format | Notes |
|----------|---------|--------|-------|
| **eco-movement** | 45 000 | JSON, mTLS | Largest open dataset on Mobilithek |
| **Tesla Supercharger** | 5 000 | JSON, mTLS | Also available via public endpoint |
| **EnBW** | 12 000 | JSON, mTLS | Uses `aegiRefillPointStatus` in dynamic feed |
| **ladenetz** | 2 000 | XML, mTLS | XML DATEX II format |

Additional providers (Eliso, MSU, Wirelane) have implemented parsers in
`providers/` but are not yet wired into the dashboard.

### Adding a new provider

Subclass `Provider` from `providers/base.py`, implement the required
methods, register it in `providers/__init__.py`, and add a subscription
ID mapping in `push_receiver.py`:

```
providers/
  base.py               Abstract interface + shared DATEX II helpers
  eco_movement.py       eco-movement (~45k points, JSON)
  tesla.py              Tesla Supercharger (~5k points, JSON)
  enbw.py               EnBW (~12k points, JSON)
  ladenetz.py           ladenetz (~2k points, XML)
  eliso.py              Eliso (experimental)
  msu.py                MSU (experimental)
  wirelane.py           Wirelane (experimental)
```

---

## Deployment

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

### Run the push receiver

```bash
uvicorn push_receiver:app --host 0.0.0.0 --port 8100
```

In production, use a systemd service and place nginx in front for TLS
termination. Configure each Mobilithek subscription's callback URL as:

```
https://<your-domain>/push/<subscription_id>
```

The push receiver also supports HEAD requests (used by Mobilithek to
probe endpoint availability).

### Run the legacy pull collector

The pull-based collector is still available as a fallback:

```bash
python collector.py --list                    # show available providers
python collector.py eco_movement              # run continuously
python collector.py eco_movement --once -v    # single cycle, verbose
python collector.py tesla --compact           # deduplicate history
```

### Run the dashboard

```bash
streamlit run dashboard.py
```

Opens at `http://localhost:8501`. Auto-refresh is available in the
sidebar.

---

## Production setup (Hetzner)

The reference deployment runs on a Hetzner cloud instance:

- **push_receiver.py** runs as `ev-push.service` (uvicorn on 127.0.0.1:8100)
- **nginx** terminates TLS (Let's Encrypt via sslip.io) and
  reverse-proxies to the push receiver
- **dashboard.py** runs as `ev-dashboard.service` (Streamlit on port 8501)
- Pull collectors (`ev-eco`, `ev-tesla`, `ev-ladenetz`) remain active as
  backup but are no longer the primary ingestion path

---

## Data model

### Static database (`{slug}_static.sqlite`)

Full infrastructure snapshot, updated on each push delivery.

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
| `collector_state` | Cursor for delta resumption |

The `delivery_type` column in `snapshot_runs` distinguishes `SNAPSHOT`
(full ground truth) from `DELTA` (incremental update). This distinction
is critical for interpreting data quality.

---

## Key files

| File | Purpose |
|---|---|
| `push_receiver.py` | FastAPI push receiver — primary data ingestion |
| `collector.py` | Pull-based collector + shared DB logic (store, deduplicate, drift correction) |
| `dashboard.py` | Streamlit dashboard — map, power timeline, heatmaps, renewables correlation |
| `providers/` | Per-provider parsers (static + dynamic DATEX II) |
| `requirements.txt` | Python dependencies |

---

## Important disclaimers

**This is an experimental research project.** It is provided as-is for
educational and analytical purposes only.

### Data accuracy

- **SNAPSHOT data** (marked with dashed lines on the timeline) represents
  the complete state as reported by the upstream provider at that moment.
  This is the most reliable data available.

- **Between SNAPSHOTs**, the state is reconstructed from incremental
  delta updates. Due to the lossy nature of the delta protocol
  (packet buffer overwrites, upstream aggregation), some status
  transitions are missed. Values between SNAPSHOTs should be treated as
  **estimates with an upward bias** -- the system tends to overcount
  active chargers.

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

This project is not affiliated with Mobilithek, eco-movement, Tesla,
EnBW, ladenetz, or any charge point operator. The author accepts no
responsibility for decisions made based on this data.

**Do not use this for safety-critical, financial, or regulatory
purposes.**

### Data source

All data is sourced from [Mobilithek](https://mobilithek.info). Access
requires registration and acceptance of their terms of use.

---

## License

MIT
