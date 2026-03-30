"""Abstract base class and shared utilities for Mobilithek data providers."""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

# ── Shared Mobilithek mTLS certificate ────────────────────────────────
CERT_PATH = os.environ.get("MOBILITHEK_CERT_PATH", "./certificate.p12")
CERT_PASSWORD = os.environ.get("MOBILITHEK_CERT_PASSWORD", "")


# ── DATEX II JSON helpers ─────────────────────────────────────────────
def get_name(name_obj) -> str | None:
    """Extract text from a multilingual name object like {"values": [{"lang":"de","value":"…"}]}."""
    if isinstance(name_obj, dict):
        for v in name_obj.get("values", []):
            val = v.get("value")
            if val:
                return val
    return None


def enum_val(obj) -> str | None:
    """Extract value from an enum wrapper like {"value": "dc"}."""
    if isinstance(obj, dict):
        return obj.get("value")
    return None


class Provider(ABC):
    """Abstract base for a Mobilithek DATEX II charging-data provider.

    To add a new data source:
      1. Create ``providers/my_source.py``
      2. Subclass ``Provider``, set the four class attributes, implement
         the four abstract methods.
      3. Register the class in ``providers/__init__.py`` → ``PROVIDERS``.

    Class attributes to set:
        name             Human-readable label, e.g. "Tesla Supercharger".
        slug             Short identifier for DB filenames, e.g. "tesla".
        tracked_columns  Dynamic columns tracked for change-detection,
                         e.g. ``["status"]`` or ``["status", "opening_status"]``.
        in_use_status    The status string meaning "point is in use",
                         e.g. "occupied" (Tesla) or "charging" (eco-movement).
    """

    # ── Required class attributes (subclass must set) ─────────────────
    name: str
    slug: str
    tracked_columns: list[str]
    in_use_status: str

    # ── Derived helpers ───────────────────────────────────────────────
    @property
    def static_db_path(self) -> Path:
        return Path("data") / f"{self.slug}_static.sqlite"

    @property
    def dynamic_db_path(self) -> Path:
        return Path("data") / f"{self.slug}_dynamic.sqlite"

    @property
    def logger(self) -> logging.Logger:
        return logging.getLogger(f"provider.{self.slug}")

    @property
    def in_use_count_column(self) -> str:
        """Column name for the 'in use' count in snapshot_runs.

        Derived from *in_use_status*: "occupied" → "occupied_count",
        "charging" → "charging_count", etc.
        """
        return f"{self.in_use_status}_count"

    # ── Abstract interface ────────────────────────────────────────────
    @abstractmethod
    def static_table_ddl(self) -> str:
        """Return the full ``CREATE TABLE IF NOT EXISTS charging_points (…);`` DDL."""
        ...

    @abstractmethod
    def load_static_snapshot(self) -> tuple[pd.DataFrame, pd.Timestamp]:
        """Fetch and parse static infrastructure data.

        Returns ``(points_df, publication_time)``.
        """
        ...

    @abstractmethod
    def drain_dynamic_deliveries(
        self,
        if_modified_since: str | None = None,
        max_deliveries: int = 500,
    ) -> list[dict]:
        """Fetch all pending dynamic deliveries.

        Each item is ``{"data": dict, "last_modified": str, "type": str}``.
        """
        ...

    @abstractmethod
    def parse_dynamic_points(self, data: dict) -> tuple[pd.DataFrame, str | None]:
        """Parse one dynamic delivery into ``(status_df, publication_time)``."""
        ...
