"""eliso – Mobilithek provider (OCPI-like flat JSON, not DATEX II).

Static data : /container/subscription endpoint (JSON list of locations, mTLS).
Dynamic data: /container/subscription endpoint (JSON evses list, mTLS).

Power values are stored in **kW** (as received from the source).
"""
from __future__ import annotations

import hashlib

import pandas as pd
from requests_pkcs12 import get as pkcs12_get

from providers.base import Provider, CERT_PATH, CERT_PASSWORD

# ── Endpoints (note: /container/subscription, not /subscription) ──────
STATIC_SUBSCRIPTION_ID = "970702710617571328"
STATIC_URL = (
    f"https://mobilithek.info:8443/mobilithek/api/v1.0/container/subscription"
    f"?subscriptionID={STATIC_SUBSCRIPTION_ID}"
)

DYNAMIC_SUBSCRIPTION_ID = "970702682490552320"
DYNAMIC_URL = (
    f"https://mobilithek.info:8443/mobilithek/api/v1.0/container/subscription"
    f"?subscriptionID={DYNAMIC_SUBSCRIPTION_ID}"
)

FETCH_TIMEOUT = 120


def _make_site_id(city: str | None, address: str | None, postcode: str | None) -> str:
    raw = f"{city or ''}|{address or ''}|{postcode or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class ElisoProvider(Provider):
    name = "eliso"
    slug = "eliso"
    tracked_columns = ["status", "operational_status"]
    in_use_status = "In use"

    @property
    def in_use_count_column(self) -> str:
        return "in_use_count"

    # ── Schema ────────────────────────────────────────────────────────
    def static_table_ddl(self) -> str:
        return """
            CREATE TABLE IF NOT EXISTS charging_points (
                point_id            TEXT PRIMARY KEY,
                site_id             TEXT,
                site_name           TEXT,
                latitude            REAL,
                longitude           REAL,
                city                TEXT,
                postcode            TEXT,
                country_code        TEXT,
                address_line        TEXT,
                operator_name       TEXT,
                current_type        TEXT,
                point_power_kw      REAL,
                connector_types     TEXT
            )
        """

    # ── Static data ───────────────────────────────────────────────────
    def load_static_snapshot(self) -> tuple[pd.DataFrame, pd.Timestamp]:
        data = self._fetch_static_json()
        points_df = self._parse_static_points(data)
        pub_time = pd.Timestamp.now(tz="UTC")
        self.logger.info(
            "Static snapshot: %d points from %d locations",
            len(points_df), len(data),
        )
        return points_df, pub_time

    # ── Dynamic data ──────────────────────────────────────────────────
    def drain_dynamic_deliveries(
        self, if_modified_since=None, max_deliveries=500,
    ) -> list[dict]:
        data, last_modified, delivery_type, status = self._fetch_dynamic(if_modified_since)
        if status == 304 or data is None:
            return []
        return [{
            "data": data,
            "last_modified": last_modified,
            "type": delivery_type,
        }]

    def parse_dynamic_points(self, data: dict) -> tuple[pd.DataFrame, str | None]:
        evses = data.get("evses", [])
        rows: list[dict] = []
        for evse in evses:
            rows.append({
                "point_id": evse.get("evseId", pd.NA),
                "status": evse.get("availability_status", pd.NA),
                "operational_status": evse.get("operational_status", pd.NA),
            })
        pub_time = None
        if evses:
            pub_time = evses[0].get("mobilithek_last_updated_dts")
        return pd.DataFrame(rows).convert_dtypes(), pub_time

    # ── Private helpers ───────────────────────────────────────────────
    def _fetch_static_json(self) -> list[dict]:
        resp = pkcs12_get(
            STATIC_URL,
            pkcs12_filename=CERT_PATH,
            pkcs12_password=CERT_PASSWORD,
            headers={"If-Modified-Since": "Thu, 01 Jan 1970 00:00:00 GMT"},
            timeout=FETCH_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def _parse_static_points(self, locations: list[dict]) -> pd.DataFrame:
        rows: list[dict] = []
        for loc in locations:
            city = loc.get("city")
            address = loc.get("address")
            postcode = loc.get("postalCode")
            site_id = _make_site_id(city, address, postcode)
            site_name = f"{city} - {address}" if city and address else (city or address)
            coords = loc.get("coordinates", {})
            country_code = loc.get("country_iso_3166_alpha_2")
            operator_name = loc.get("operator_name")

            for evse in loc.get("evses", []):
                point_id = evse.get("evseId")
                connectors = evse.get("connectors", [])
                powers: list[float] = []
                conn_types: list[str] = []
                current_type = None
                for c in connectors:
                    mp = c.get("maxPower")
                    if mp is not None:
                        powers.append(mp)
                    ct = c.get("type_of_connector")
                    if ct:
                        conn_types.append(ct)
                    pt = c.get("powerType")
                    if pt:
                        current_type = pt.lower()

                point_power_kw = max(powers) if powers else None

                rows.append({
                    "point_id": point_id,
                    "site_id": site_id,
                    "site_name": site_name,
                    "latitude": coords.get("latitude"),
                    "longitude": coords.get("longitude"),
                    "city": city,
                    "postcode": postcode,
                    "country_code": country_code,
                    "address_line": address,
                    "operator_name": operator_name,
                    "current_type": current_type,
                    "point_power_kw": point_power_kw,
                    "connector_types": ", ".join(sorted(set(conn_types))) or None,
                })

        df = pd.DataFrame(rows)
        for col in ["latitude", "longitude", "point_power_kw"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.convert_dtypes()

    def _fetch_dynamic(
        self, if_modified_since: str | None = None,
    ) -> tuple[dict | None, str | None, str | None, int]:
        headers = {}
        if if_modified_since:
            headers["If-Modified-Since"] = if_modified_since

        resp = pkcs12_get(
            DYNAMIC_URL,
            pkcs12_filename=CERT_PATH,
            pkcs12_password=CERT_PASSWORD,
            headers=headers,
            timeout=FETCH_TIMEOUT,
        )

        last_modified = resp.headers.get("Last-Modified")
        raw_type = resp.headers.get("Type")
        delivery_type = raw_type if raw_type else "SNAPSHOT"

        if resp.status_code == 304:
            return None, last_modified, delivery_type, 304

        resp.raise_for_status()
        return resp.json(), last_modified, delivery_type, resp.status_code
