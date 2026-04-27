"""SMATRICS – Mobilithek DATEX II provider.

Static data : subscription endpoint (JSON, mTLS).
Dynamic data: subscription endpoint (JSON SNAPSHOT/DELTA, mTLS).

Notes on the SMATRICS feed:

* ``locationReference`` (coordinates + address) is attached at the
  **station** level, not the site level (unlike Tesla/eco-movement).
* The inner key is ``FacilityLocation`` with a capital F (DATEX II v3.5
  style) rather than ``facilityLocation``.
* Coordinates come through ``pointByCoordinates.pointCoordinates`` rather
  than ``coordinatesForDisplay``.
* Power values are native watts.
* The dynamic endpoint can answer ``204 No Content`` when the delivery
  queue is empty — we treat that identically to a 304.
"""
from __future__ import annotations

import pandas as pd
from requests_pkcs12 import get as pkcs12_get

from providers.base import Provider, CERT_PATH, CERT_PASSWORD, get_name, enum_val

# ── Endpoints ─────────────────────────────────────────────────────────
STATIC_SUBSCRIPTION_ID = "981895858521624576"
STATIC_URL = (
    f"https://mobilithek.info:8443/mobilithek/api/v1.0/subscription"
    f"?subscriptionID={STATIC_SUBSCRIPTION_ID}"
)

DYNAMIC_SUBSCRIPTION_ID = "970701736117174272"
DYNAMIC_URL = (
    f"https://mobilithek.info:8443/mobilithek/api/v1.0/subscription"
    f"?subscriptionID={DYNAMIC_SUBSCRIPTION_ID}"
)

FETCH_TIMEOUT = 120


def _extract_location(loc: dict) -> tuple[float | None, float | None, dict]:
    """Return (lat, lon, address_dict) from a DATEX II locationReference.

    Handles both the classic ``coordinatesForDisplay`` layout and the
    ``pointByCoordinates.pointCoordinates`` layout used by SMATRICS.
    Also handles both ``facilityLocation`` and ``FacilityLocation`` keys.
    """
    if not isinstance(loc, dict):
        return None, None, {}

    area_loc = loc.get("locAreaLocation", {}) or {}
    point_loc = loc.get("locPointLocation", {}) or {}

    # Coordinates — try all three patterns.
    latitude = longitude = None
    for container in (area_loc, point_loc):
        cfd = container.get("coordinatesForDisplay") or {}
        if cfd.get("latitude") is not None:
            latitude = cfd.get("latitude")
            longitude = cfd.get("longitude")
            break
        pbc = (
            container.get("pointByCoordinates", {})
            .get("pointCoordinates", {})
        )
        if pbc.get("latitude") is not None:
            latitude = pbc.get("latitude")
            longitude = pbc.get("longitude")
            break

    # Address — try both casings.
    loc_ext = (
        area_loc.get("locLocationExtensionG")
        or point_loc.get("locLocationExtensionG")
        or {}
    )
    fac = (
        loc_ext.get("facilityLocation")
        or loc_ext.get("FacilityLocation")
        or {}
    )
    addr = fac.get("address", {}) or {}
    return latitude, longitude, addr


class SmatricsProvider(Provider):
    name = "SMATRICS"
    slug = "smatrics"
    tracked_columns = ["status"]
    in_use_status = "occupied"
    power_column = "point_power_w"
    power_to_mw = 1e-6

    # ── Schema ────────────────────────────────────────────────────────
    def static_table_ddl(self) -> str:
        return """
            CREATE TABLE IF NOT EXISTS charging_points (
                point_id            TEXT PRIMARY KEY,
                evse_id             TEXT,
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
                connector_types     TEXT,
                num_connectors      INTEGER
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

            if status in (204, 304):
                self.logger.debug(
                    "%d after %d deliveries — queue drained.", status, i,
                )
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
        # Also accept the non-container layout (payload directly under root).
        if not payloads and "payload" in data:
            single = data.get("payload")
            payloads = single if isinstance(single, list) else [single]

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

                # Site-level location (if present — SMATRICS usually omits it).
                site_lat, site_lon, site_addr = _extract_location(
                    site.get("locationReference", {}),
                )

                operator_obj = site.get("operator", {})
                org = operator_obj.get("afacAnOrganisation", {})
                operator_name = get_name(org.get("name"))

                for station in site.get("energyInfrastructureStation", []):
                    station_id = station.get("idG")
                    total_max_power_w = station.get("totalMaximumPower")

                    # Station-level location (SMATRICS attaches it here).
                    st_lat, st_lon, st_addr = _extract_location(
                        station.get("locationReference", {}),
                    )
                    latitude = st_lat if st_lat is not None else site_lat
                    longitude = st_lon if st_lon is not None else site_lon
                    addr = st_addr or site_addr

                    city = get_name(addr.get("city"))
                    postcode = addr.get("postcode")
                    country_code = addr.get("countryCode")
                    address_lines = addr.get("addressLine", [])
                    address_line = None
                    if address_lines and isinstance(address_lines, list):
                        first = address_lines[0]
                        if isinstance(first, dict):
                            address_line = get_name(first.get("text"))

                    for rp in station.get("refillPoint", []):
                        ecp = rp.get("aegiElectricChargingPoint", {})
                        point_id = ecp.get("idG")
                        current_type = enum_val(ecp.get("currentType"))
                        num_connectors = ecp.get("numberOfConnectors")

                        # EVSE ID — the European standard identifier.
                        evse_id = None
                        for ext_id in ecp.get("externalIdentifier", []):
                            toi = ext_id.get("typeOfIdentifier", {})
                            if (isinstance(toi, dict) and
                                    toi.get("extendedValueG") == "evseId"):
                                evse_id = ext_id.get("identifier")
                                break

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

                        max_socket_w = (
                            max(socket_powers_w) if socket_powers_w else None
                        )
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
                            "evse_id": evse_id,
                            "current_type": current_type,
                            "available_power_w": avail_power_w,
                            "max_socket_power_w": max_socket_w,
                            "point_power_w": point_power_w,
                            "connector_types": ", ".join(
                                sorted(set(connector_types))
                            ) or None,
                            "num_connectors": num_connectors,
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

        # 304 Not Modified — queue quiet since cursor.
        if resp.status_code == 304:
            return None, last_modified, delivery_type, 304
        # 204 No Content — queue empty. Treated identically by the drainer.
        if resp.status_code == 204 or not resp.content:
            return None, last_modified, delivery_type, 204

        resp.raise_for_status()
        return resp.json(), last_modified, delivery_type, resp.status_code
