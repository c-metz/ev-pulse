# EV Charging Monitor — Germany

Real-time monitoring of Germany's public EV charging infrastructure via
the **AFIR-mandated DATEX II** feeds published on
[Mobilithek](https://mobilithek.info).

The system continuously collects status updates from multiple Charge
Point Operators (CPOs), stores every status transition in per-provider
SQLite databases, and serves a live dashboard with geographic
visualisation, power-draw estimates, and network utilisation metrics.

| Metric | Value |
|---|---|
| **Charging points monitored** | ~49 000 |
| **Providers active** | eco-movement, Tesla Supercharger |
| **Polling interval** | 60 s |
| **Static metadata refresh** | 12 h |

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Mobilithek (DATEX II / JSON over mTLS)                      │
│    eco-movement · Tesla · MSU · Eliso · Wirelane             │
└──────────┬──────────────────────────────┬────────────────────┘
           │  dynamic (Δ every 60 s)      │  static (every 12 h)
           ▼                              ▼
┌──────────────────────────────────────────────────────────────┐
│  collector.py                                                │
│    • change-detection — only stores actual transitions       │
│    • SNAPSHOT vs DELTA awareness for drift correction        │
│    • per-provider SQLite databases in data/                  │
└──────────┬──────────────────────────────┬────────────────────┘
           │                              │
           ▼                              ▼
   {slug}_dynamic.sqlite          {slug}_static.sqlite
   (point_status_history)         (charging_points)
           │
           ▼
┌──────────────────────────────────────────────────────────────┐
│  dashboard.py  (Streamlit)                                   │
│    • Live map of all charging points, coloured by status     │
│    • KPI cards: total points, in-use, MW load, usage %       │
│    • Power-draw timeline (nameplate × in-use)                │
│    • Network utilisation over time                           │
└──────────────────────────────────────────────────────────────┘
```

## Quick Start

### 1. Clone & install

```bash
git clone https://github.com/<your-org>/ev-monitor.git
cd ev-monitor
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure credentials

Register at [mobilithek.info](https://mobilithek.info) and download your
PKCS#12 client certificate.  Copy it into the repo root as
`certificate.p12`, then create a `.env` file:

```bash
cp .env.example .env
# edit .env — set MOBILITHEK_CERT_PASSWORD to your certificate password
```

### 3. Run the collector

```bash
# List available providers
python collector.py --list

# Start collecting (runs continuously)
python collector.py eco_movement
python collector.py tesla

# One-shot collection
python collector.py tesla --once
```

### 4. Launch the dashboard

```bash
streamlit run dashboard.py
```

Open [http://localhost:8501](http://localhost:8501).

## Provider System

Adding a new CPO data source requires a single Python file:

```
providers/
├── base.py           # Abstract Provider class + shared helpers
├── eco_movement.py   # eco-movement (~45 k points)
├── tesla.py          # Tesla Supercharger (~3.8 k points)
├── msu.py            # MSU
├── eliso.py          # Eliso
└── wirelane.py       # Wirelane
```

Each provider subclasses `Provider` and implements four methods:
`static_table_ddl()`, `load_static_snapshot()`,
`drain_dynamic_deliveries()`, and `parse_dynamic_points()`.

## Data Model

**Static DB** (`{slug}_static.sqlite`) — full infrastructure snapshot,
refreshed every 12 hours:

| Column | Description |
|---|---|
| `point_id` | Unique charging point identifier |
| `latitude`, `longitude` | WGS 84 coordinates |
| `point_power_w` / `point_power_kw` | Rated power per point |
| `connector_types` | Plug standards (CCS, Type2, …) |
| … | Operator, address, site hierarchy |

**Dynamic DB** (`{slug}_dynamic.sqlite`) — change-detected status
history:

| Table | Purpose |
|---|---|
| `snapshot_runs` | One row per collection cycle with aggregate counts |
| `point_status_history` | One row per *status change* per point |

The collector distinguishes **SNAPSHOT** deliveries (full state dump)
from **DELTA** deliveries (only changed points). Snapshots reset the
internal state machine, preventing drift from accumulating between
polling intervals.

## Server Deployment

The production instance runs on a Hetzner VPS with two systemd services
(`ev-eco.service`, `ev-tesla.service`) collecting data 24/7, and
`ev-dashboard.service` serving the Streamlit dashboard.

Code updates are pulled from GitHub automatically every 5 minutes.

## License

Private — all rights reserved.
