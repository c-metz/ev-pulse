"""eco-movement – Mobilithek DATEX II provider.

Static data : subscription endpoint (JSON, mTLS).
Dynamic data: subscription endpoint (JSON deltaPull, mTLS).

Power values are stored in **watts** (as received from the source).
"""
from __future__ import annotations

import pandas as pd
from requests_pkcs12 import get as pkcs12_get

from providers.base import Provider, CERT_PATH, CERT_PASSWORD, get_name, enum_val

# ── Endpoints ─────────────────────────────────────────────────────────
STATIC_SUBSCRIPTION_ID = "970702152460550144"
STATIC_URL = (
    f"https://mobilithek.info:8443/mobilithek/api/v1.0/subscription"
    f"?subscriptionID={STATIC_SUBSCRIPTION_ID}"
)

DYNAMIC_SUBSCRIPTION_ID = "970702182768590848"
DYNAMIC_URL = (
    f"https://mobilithek.info:8443/mobilithek/api/v1.0/subscription"
    f"?subscriptionID={DYNAMIC_SUBSCRIPTION_ID}"
)

FETCH_TIMEOUT = 180


class EcoMovementProvider(Provider):
    name = "eco-movement"
    slug = "eco"
    tracked_columns = ["status"]
    in_use_status = "charging"
    power_column = "point_power_w"
    power_to_mw = 1e-6

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
                operator_name       TEXT,
                station_max_power_w REAL,
                current_type        TEXT,
                available_power_w   REAL,
                max_socket_power_w  REAL,
                point_power_w       REAL,
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
                        rows.append({
                            "point_id": point_ref.get("idG", pd.NA),
                            "status": status,
                        })

        return pd.DataFrame(rows).convert_dtypes(), pub_time

    # ── Private fetch / parse helpers ─────────────────────────────────
    def _fetch_static_json(self) -> dict:
        resp = pkcs12_get(
            STATIC_URL,
            pkcs12_filename=CERT_PATH,
            pkcs12_password=CERT_PASSWORD,
            headers={"If-Modified-Since": "Thu, 01 Jan 1970 00:00:00 GMT"},
            timeout=FETCH_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def _parse_static_points(self, data: dict) -> pd.DataFrame:
        rows: list[dict] = []

        payload = data.get("payload", {})
        pub = payload.get("aegiEnergyInfrastructureTablePublication", {})

        for table in pub.get("energyInfrastructureTable", []):
            for site in table.get("energyInfrastructureSite", []):
                site_id = site.get("idG")
                site_name = get_name(site.get("name"))

                loc = site.get("locationReference", {})
                area_loc = loc.get("locAreaLocation", {})
                point_loc = loc.get("locPointLocation", {})
                coords = (
                    area_loc.get("coordinatesForDisplay", {})
                    or point_loc.get("coordinatesForDisplay", {})
                )
                latitude = coords.get("latitude")
                longitude = coords.get("longitude")

                loc_ext = (
                    area_loc.get("locLocationExtensionG", {})
                    or point_loc.get("locLocationExtensionG", {})
                )
                addr = loc_ext.get("facilityLocation", {}).get("address", {})
                city = get_name(addr.get("city"))
                postcode = addr.get("postcode")
                country_code = addr.get("countryCode")
                address_lines = addr.get("addressLine", [])
                address_line = None
                if address_lines and isinstance(address_lines, list):
                    first = address_lines[0]
                    if isinstance(first, dict):
                        address_line = get_name(first.get("text"))

                operator_obj = site.get("operator", {})
                org = operator_obj.get("afacAnOrganisation", {})
                operator_name = get_name(org.get("name"))

                for station in site.get("energyInfrastructureStation", []):
                    station_id = station.get("idG")
                    total_max_power_w = station.get("totalMaximumPower")

                    for rp in station.get("refillPoint", []):
                        ecp = rp.get("aegiElectricChargingPoint", {})
                        point_id = ecp.get("idG")
                        current_type = enum_val(ecp.get("currentType"))

                        avail_powers = ecp.get("availableChargingPower", [])
                        avail_power_w = None
                        if isinstance(avail_powers, list) and avail_powers:
                            avail_power_w = max(avail_powers)
                        elif isinstance(avail_powers, (int, float)):
                            avail_power_w = avail_powers

                        connectors = ecp.get("connector", [])
                        socket_powers_w: list[float] = []
                        connector_types: list[str] = []
                        for c in connectors:
                            mp = c.get("maxPowerAtSocket")
                            if mp is not None:
                                socket_powers_w.append(mp)
                            ct = enum_val(c.get("connectorType"))
                            if ct:
                                connector_types.append(ct)

                        max_socket_w = max(socket_powers_w) if socket_powers_w else None
                        point_power_w = avail_power_w or max_socket_w

                        rows.append({
                            "site_id": site_id,
                            "site_name": site_name,
                            "latitude": latitude,
                            "longitude": longitude,
                            "city": city,
                            "postcode": postcode,
                            "country_code": country_code,
                            "address_line": address_line,
                            "operator_name": operator_name,
                            "station_id": station_id,
                            "station_max_power_w": total_max_power_w,
                            "point_id": point_id,
                            "current_type": current_type,
                            "available_power_w": avail_power_w,
                            "max_socket_power_w": max_socket_w,
                            "point_power_w": point_power_w,
                            "connector_types": ", ".join(sorted(set(connector_types))) or None,
                        })

        df = pd.DataFrame(rows)
        for col in ["latitude", "longitude", "station_max_power_w",
                     "available_power_w", "max_socket_power_w", "point_power_w"]:
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
