"""Tesla Supercharger – Mobilithek DATEX II provider.

Static data : public noauth endpoint (gzip JSON).
Dynamic data: subscription endpoint (JSON deltaPull, mTLS).
"""
from __future__ import annotations

import gzip
import json
import time
from urllib.request import urlopen, Request

import pandas as pd
from requests_pkcs12 import get as pkcs12_get

from providers.base import Provider, CERT_PATH, CERT_PASSWORD, get_name, enum_val

# ── Endpoints ─────────────────────────────────────────────────────────
STATIC_PUBLICATION_ID = "953828817873125376"
STATIC_URL = (
    f"https://mobilithek.info/mdp-api/mdp-conn-server/v1/publication/"
    f"{STATIC_PUBLICATION_ID}/file/noauth"
)

DYNAMIC_SUBSCRIPTION_ID = "970702208026705920"
DYNAMIC_URL = (
    f"https://mobilithek.info:8443/mobilithek/api/v1.0/subscription"
    f"?subscriptionID={DYNAMIC_SUBSCRIPTION_ID}"
)

WATTS_PER_KILOWATT = 1000.0
FETCH_TIMEOUT = 120
FETCH_MAX_RETRIES = 3


class TeslaProvider(Provider):
    name = "Tesla Supercharger"
    slug = "tesla"
    tracked_columns = ["status", "opening_status", "operation_status"]
    in_use_status = "occupied"
    power_column = "point_power_kw"
    power_to_mw = 1e-3

    # ── Schema ────────────────────────────────────────────────────────
    def static_table_ddl(self) -> str:
        return """
            CREATE TABLE IF NOT EXISTS charging_points (
                point_id            TEXT PRIMARY KEY,
                site_id             TEXT,
                station_id          TEXT,
                site_name           TEXT,
                latitude            REAL,
                longitude           REAL,
                city                TEXT,
                postcode            TEXT,
                country_code        TEXT,
                address_line        TEXT,
                station_max_power_kw REAL,
                external_id         TEXT,
                current_type        TEXT,
                available_power_kw  REAL,
                max_socket_power_kw REAL,
                point_power_kw      REAL,
                connector_types     TEXT
            )
        """

    # ── Static data ───────────────────────────────────────────────────
    def load_static_snapshot(self) -> tuple[pd.DataFrame, pd.Timestamp]:
        data = self._fetch_static_json()
        points_df = self._parse_static_points(data)
        pub_time_str = (
            data.get("payload", {})
            .get("aegiEnergyInfrastructureTablePublication", {})
            .get("publicationTime")
        )
        pub_time = pd.to_datetime(pub_time_str, utc=True)
        self.logger.info(
            "Static snapshot: %d points, published %s",
            len(points_df), pub_time.isoformat(),
        )
        return points_df, pub_time

    # ── Dynamic data ──────────────────────────────────────────────────
    def drain_dynamic_deliveries(
        self, if_modified_since=None, max_deliveries=500,
    ) -> list[dict]:
        deliveries: list[dict] = []
        cursor = if_modified_since

        for i in range(max_deliveries):
            data, last_modified, delivery_type, status = self._fetch_dynamic(cursor)

            if status == 304:
                self.logger.debug("304 after %d deliveries — queue drained.", i)
                break
            if data is None:
                break

            deliveries.append({
                "data": data,
                "last_modified": last_modified,
                "type": delivery_type,
            })
            self.logger.debug(
                "Delivery #%d: %s  Last-Modified=%s",
                i + 1, delivery_type, last_modified,
            )

            if not last_modified or last_modified == cursor:
                break
            cursor = last_modified

        return deliveries

    def parse_dynamic_points(self, data: dict) -> tuple[pd.DataFrame, str | None]:
        rows: list[dict] = []
        mc = data.get("messageContainer", {})
        payloads = mc.get("payload", [])

        pub_time = None
        for p in payloads:
            pub = p.get("aegiEnergyInfrastructureStatusPublication", {})
            pub_time = pub.get("publicationTime", pub_time)

            for site_st in pub.get("energyInfrastructureSiteStatus", []):
                for station_st in site_st.get("energyInfrastructureStationStatus", []):
                    for rp in station_st.get("refillPointStatus", []):
                        ecp = rp.get("aegiElectricChargingPointStatus", {})
                        point_ref = ecp.get("reference", {})
                        status = ecp.get("status", {})
                        if isinstance(status, dict):
                            status = status.get("value", pd.NA)
                        opening = ecp.get("openingStatus", {})
                        if isinstance(opening, dict):
                            opening = opening.get("value", pd.NA)
                        operation = ecp.get("operationStatus", {})
                        if isinstance(operation, dict):
                            operation = operation.get("value", pd.NA)
                        rows.append({
                            "point_id": point_ref.get("idG", pd.NA),
                            "status": status,
                            "opening_status": opening,
                            "operation_status": operation,
                        })

        return pd.DataFrame(rows).convert_dtypes(), pub_time

    # ── Private fetch / parse helpers ─────────────────────────────────
    def _fetch_static_json(self) -> dict:
        for attempt in range(1, FETCH_MAX_RETRIES + 1):
            try:
                req = Request(STATIC_URL, headers={"User-Agent": "Mozilla/5.0"})
                with urlopen(req, timeout=FETCH_TIMEOUT) as resp:
                    raw = resp.read()
                text = gzip.decompress(raw).decode("utf-8")
                return json.loads(text)
            except Exception:
                if attempt == FETCH_MAX_RETRIES:
                    raise
                self.logger.warning(
                    "Static fetch attempt %d/%d failed, retrying…",
                    attempt, FETCH_MAX_RETRIES,
                )
                time.sleep(5 * attempt)

    def _parse_static_points(self, data: dict) -> pd.DataFrame:
        rows: list[dict] = []
        payload = data.get("payload", {})
        pub = payload.get("aegiEnergyInfrastructureTablePublication", {})

        for table in pub.get("energyInfrastructureTable", []):
            for site in table.get("energyInfrastructureSite", []):
                site_id = site.get("idG")
                site_name = get_name(site.get("name"))

                loc = site.get("locationReference", {})
                coords = loc.get("locPointLocation", {}).get("coordinatesForDisplay", {})
                latitude = coords.get("latitude")
                longitude = coords.get("longitude")

                addr = (
                    loc.get("locPointLocation", {})
                    .get("locLocationExtensionG", {})
                    .get("facilityLocation", {})
                    .get("address", {})
                )
                city = get_name(addr.get("city"))
                postcode = addr.get("postcode")
                country_code = addr.get("countryCode")
                address_lines = addr.get("addressLine", [])
                address_line = None
                if address_lines:
                    address_line = get_name(address_lines[0].get("text"))

                for station in site.get("energyInfrastructureStation", []):
                    station_id = station.get("idG")
                    total_max_power_w = station.get("totalMaximumPower")
                    station_max_power_kw = (
                        total_max_power_w / WATTS_PER_KILOWATT
                        if total_max_power_w is not None else None
                    )
                    station_external_id = station.get("externalIdentifier")

                    for rp in station.get("refillPoint", []):
                        ecp = rp.get("aegiElectricChargingPoint", {})
                        point_id = ecp.get("idG")
                        external_id = ecp.get("externalIdentifier") or station_external_id
                        current_type = enum_val(ecp.get("currentType"))

                        avail_powers = ecp.get("availableChargingPower", [])
                        avail_power_kw = None
                        if isinstance(avail_powers, list) and avail_powers:
                            avail_power_kw = max(avail_powers) / WATTS_PER_KILOWATT
                        elif isinstance(avail_powers, (int, float)):
                            avail_power_kw = avail_powers / WATTS_PER_KILOWATT

                        connectors = ecp.get("connector", [])
                        socket_powers_kw: list[float] = []
                        connector_types: list[str] = []
                        for c in connectors:
                            mp = c.get("maxPowerAtSocket")
                            if mp is not None:
                                socket_powers_kw.append(mp / WATTS_PER_KILOWATT)
                            ct = enum_val(c.get("connectorType"))
                            if ct:
                                connector_types.append(ct)

                        max_socket_kw = max(socket_powers_kw) if socket_powers_kw else None
                        point_power_kw = avail_power_kw or max_socket_kw

                        rows.append({
                            "site_id": site_id,
                            "site_name": site_name,
                            "latitude": latitude,
                            "longitude": longitude,
                            "city": city,
                            "postcode": postcode,
                            "country_code": country_code,
                            "address_line": address_line,
                            "station_id": station_id,
                            "station_max_power_kw": station_max_power_kw,
                            "external_id": external_id,
                            "point_id": point_id,
                            "current_type": current_type,
                            "available_power_kw": avail_power_kw,
                            "max_socket_power_kw": max_socket_kw,
                            "point_power_kw": point_power_kw,
                            "connector_types": ", ".join(sorted(set(connector_types))) or None,
                        })

        df = pd.DataFrame(rows)
        for col in ["latitude", "longitude", "station_max_power_kw",
                     "available_power_kw", "max_socket_power_kw", "point_power_kw"]:
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
