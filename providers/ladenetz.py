"""ladenetz – Mobilithek DATEX II provider (XML format).

Static data : subscription endpoint (XML, mTLS).
Dynamic data: subscription endpoint (XML deltaPull, mTLS).

Power values are stored in **watts** (as received from the source).
Point IDs use the EVSE-style external identifier (e.g. "DE1ESE001101").
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

import pandas as pd
from requests_pkcs12 import get as pkcs12_get

from providers.base import Provider, CERT_PATH, CERT_PASSWORD

# ── Endpoints ─────────────────────────────────────────────────────────
STATIC_SUBSCRIPTION_ID = "970702370149138432"
STATIC_URL = (
    f"https://mobilithek.info:8443/mobilithek/api/v1.0/subscription"
    f"?subscriptionID={STATIC_SUBSCRIPTION_ID}"
)

DYNAMIC_SUBSCRIPTION_ID = "970702352017145856"
DYNAMIC_URL = (
    f"https://mobilithek.info:8443/mobilithek/api/v1.0/subscription"
    f"?subscriptionID={DYNAMIC_SUBSCRIPTION_ID}"
)

FETCH_TIMEOUT = 300  # 29 MB static payload → generous timeout

# ── XML namespace shortcuts ───────────────────────────────────────────
NS = {
    "mc": "http://datex2.eu/schema/3/messageContainer",
    "c":  "http://datex2.eu/schema/3/common",
    "ei": "http://datex2.eu/schema/3/energyInfrastructure",
    "f":  "http://datex2.eu/schema/3/facilities",
    "lr": "http://datex2.eu/schema/3/locationReferencing",
    "le": "http://datex2.eu/schema/3/locationExtension",
}


def _find_text(el: ET.Element, path: str) -> str | None:
    """Find child element by namespace-prefixed path, return .text or None."""
    node = el.find(path, NS)
    return node.text.strip() if node is not None and node.text else None


def _find_name(el: ET.Element) -> str | None:
    """Extract the German/first name value from a facilities:name element."""
    name_el = el.find("f:name", NS)
    if name_el is None:
        return None
    val = name_el.find("c:values/c:value", NS)
    return val.text.strip() if val is not None and val.text else None


class LadenetzProvider(Provider):
    name = "ladenetz"
    slug = "ladenetz"
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
                available_power_w   REAL,
                max_socket_power_w  REAL,
                point_power_w       REAL,
                connector_types     TEXT
            )
        """

    # ── Static data ───────────────────────────────────────────────────
    def load_static_snapshot(self) -> tuple[pd.DataFrame, pd.Timestamp]:
        resp = pkcs12_get(
            STATIC_URL,
            pkcs12_filename=CERT_PATH,
            pkcs12_password=CERT_PASSWORD,
            headers={"If-Modified-Since": "Thu, 01 Jan 1970 00:00:00 GMT"},
            timeout=FETCH_TIMEOUT,
        )
        resp.raise_for_status()

        root = ET.fromstring(resp.content)
        rows = self._parse_static_xml(root)
        points_df = pd.DataFrame(rows)
        for col in ["latitude", "longitude", "available_power_w",
                     "max_socket_power_w", "point_power_w"]:
            if col in points_df.columns:
                points_df[col] = pd.to_numeric(points_df[col], errors="coerce")
        points_df = points_df.convert_dtypes()

        pub_time_el = root.find(".//ei:publicationTime", NS)
        if pub_time_el is None:
            # Try under the payload wrapper
            pub_time_el = root.find(".//{http://datex2.eu/schema/3/energyInfrastructure}publicationTime")
        pub_time_str = pub_time_el.text if pub_time_el is not None else None
        pub_time = pd.to_datetime(pub_time_str, utc=True) if pub_time_str else pd.Timestamp.now("UTC")

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
            resp, last_modified, delivery_type, status = self._fetch_dynamic(cursor)
            if status == 304:
                self.logger.debug("304 after %d deliveries — queue drained.", i)
                break
            if resp is None:
                break

            deliveries.append({
                "data": resp,         # raw bytes (XML)
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

    def parse_dynamic_points(self, data) -> tuple[pd.DataFrame, str | None]:
        """Parse one dynamic delivery (XML bytes) into (status_df, pub_time)."""
        if isinstance(data, bytes):
            root = ET.fromstring(data)
        elif isinstance(data, ET.Element):
            root = data
        else:
            # Already parsed dict — shouldn't happen but be defensive
            return pd.DataFrame(columns=["point_id", "status"]), None

        rows: list[dict] = []
        pub_time = None
        for pub_el in root.iter():
            if pub_el.tag.endswith("}publicationTime") and pub_time is None:
                pub_time = pub_el.text

        for rps in root.iter():
            if not rps.tag.endswith("}refillPointStatus"):
                continue
            # reference element carries point_id in its 'id' attribute
            ref = rps.find("f:reference", NS)
            point_id = ref.get("id") if ref is not None else None
            if not point_id:
                continue

            status_el = rps.find("ei:status", NS)
            status = status_el.text.strip() if status_el is not None and status_el.text else pd.NA

            rows.append({"point_id": point_id, "status": status})

        return pd.DataFrame(rows).convert_dtypes(), pub_time

    def parse_static_points(self, data) -> tuple[pd.DataFrame, pd.Timestamp]:
        """Parse pre-fetched static XML into (points_df, pub_time).

        Used by the push receiver when the payload is already available.
        """
        if isinstance(data, bytes):
            root = ET.fromstring(data)
        elif isinstance(data, ET.Element):
            root = data
        else:
            raise TypeError(f"Expected bytes or Element, got {type(data)}")

        rows = self._parse_static_xml(root)
        points_df = pd.DataFrame(rows)
        for c in ["latitude", "longitude", "available_power_w",
                   "max_socket_power_w", "point_power_w"]:
            if c in points_df.columns:
                points_df[c] = pd.to_numeric(points_df[c], errors="coerce")
        points_df = points_df.convert_dtypes()

        pub_time_el = root.find(".//ei:publicationTime", NS)
        if pub_time_el is None:
            pub_time_el = root.find(
                ".//{http://datex2.eu/schema/3/energyInfrastructure}publicationTime"
            )
        pub_time_str = pub_time_el.text if pub_time_el is not None else None
        pub_time = (
            pd.to_datetime(pub_time_str, utc=True)
            if pub_time_str
            else pd.Timestamp.now("UTC")
        )
        return points_df, pub_time

    # ── Private helpers ───────────────────────────────────────────────
    def _parse_static_xml(self, root: ET.Element) -> list[dict]:
        rows: list[dict] = []

        for site in root.iter():
            if not site.tag.endswith("}energyInfrastructureSite"):
                continue

            site_id = site.get("id")
            site_name = _find_name(site)

            # Coordinates
            coords = site.find("f:locationReference/lr:coordinatesForDisplay", NS)
            latitude = _find_text(coords, "lr:latitude") if coords is not None else None
            longitude = _find_text(coords, "lr:longitude") if coords is not None else None

            # Address — inside _locationReferenceExtension > facilityLocation > address
            addr = site.find(
                "f:locationReference/lr:_locationReferenceExtension"
                "/lr:facilityLocation/le:address", NS
            )
            city = postcode = country_code = address_line = None
            if addr is not None:
                postcode = _find_text(addr, "le:postcode")
                country_code = _find_text(addr, "le:countryCode")
                city_el = addr.find("le:city/c:values/c:value", NS)
                city = city_el.text.strip() if city_el is not None and city_el.text else None
                # First addressLine text
                al_el = addr.find("le:addressLine/le:text/c:values/c:value", NS)
                address_line = al_el.text.strip() if al_el is not None and al_el.text else None

            # Operator name
            operator_name = _find_name(site.find("f:operator", NS)) if site.find("f:operator", NS) is not None else None

            # Stations
            for station in site:
                if not station.tag.endswith("}energyInfrastructureStation"):
                    continue
                station_id = station.get("id")

                for rp in station:
                    if not rp.tag.endswith("}refillPoint"):
                        continue

                    point_id = _find_text(rp, "f:externalIdentifier")
                    if not point_id:
                        continue

                    avail_power_text = _find_text(rp, "ei:availableChargingPower")
                    avail_power_w = float(avail_power_text) if avail_power_text else None

                    # Parse connectors
                    socket_powers: list[float] = []
                    connector_types: list[str] = []
                    for conn in rp:
                        if not conn.tag.endswith("}connector"):
                            continue
                        ct = _find_text(conn, "ei:connectorType")
                        if ct:
                            connector_types.append(ct)
                        mp = _find_text(conn, "ei:maxPowerAtSocket")
                        if mp:
                            try:
                                socket_powers.append(float(mp))
                            except ValueError:
                                pass

                    max_socket_w = max(socket_powers) if socket_powers else None
                    point_power_w = avail_power_w or max_socket_w

                    rows.append({
                        "point_id": point_id,
                        "site_id": site_id,
                        "station_id": station_id,
                        "site_name": site_name,
                        "latitude": latitude,
                        "longitude": longitude,
                        "city": city,
                        "postcode": postcode,
                        "country_code": country_code,
                        "address_line": address_line,
                        "operator_name": operator_name,
                        "available_power_w": avail_power_w,
                        "max_socket_power_w": max_socket_w,
                        "point_power_w": point_power_w,
                        "connector_types": ", ".join(sorted(set(connector_types))) or None,
                    })

        return rows

    def _fetch_dynamic(
        self, if_modified_since: str | None = None,
    ) -> tuple[bytes | None, str | None, str | None, int]:
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
        # Return raw bytes — parse_dynamic_points handles XML parsing
        return resp.content, last_modified, delivery_type, resp.status_code
