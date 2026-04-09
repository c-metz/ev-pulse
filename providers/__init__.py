"""Data source providers for Mobilithek DATEX II feeds.

Each provider implements fetching and parsing of static infrastructure
metadata and dynamic status updates for one charging network.

Usage::

    from providers import get_provider, list_providers

    provider = get_provider("tesla")
    df, pub_time = provider.load_static_snapshot()

To add a new provider, create a module in this package, subclass
``Provider``, and register the class in the ``PROVIDERS`` dict below.
"""
from __future__ import annotations

from providers.base import Provider
from providers.tesla import TeslaProvider
from providers.eco_movement import EcoMovementProvider
from providers.eliso import ElisoProvider
from providers.ladenetz import LadenetzProvider
from providers.msu import MsuProvider
from providers.wirelane import WirelaneProvider

PROVIDERS: dict[str, type[Provider]] = {
    "tesla": TeslaProvider,
    "eco_movement": EcoMovementProvider,
    "eliso": ElisoProvider,
    "ladenetz": LadenetzProvider,
    "msu": MsuProvider,
    "wirelane": WirelaneProvider,
}


def list_providers() -> list[str]:
    """Return sorted list of registered provider names."""
    return sorted(PROVIDERS)


def get_provider(name: str) -> Provider:
    """Instantiate a provider by name. Raises ValueError if unknown."""
    cls = PROVIDERS.get(name)
    if cls is None:
        available = ", ".join(sorted(PROVIDERS))
        raise ValueError(f"Unknown provider {name!r}. Available: {available}")
    return cls()
