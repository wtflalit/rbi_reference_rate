"""Rate source registry.

Sources are tried in the order given by RATE_SOURCES / --sources. The first
one that returns rates wins; the rest are never called.
"""

from __future__ import annotations

from .base import RateSource
from .fbil import FBILSource
from .frankfurter import FrankfurterSource
from .rbi import RBISource

REGISTRY: dict[str, type[RateSource]] = {
    FBILSource.name: FBILSource,
    RBISource.name: RBISource,
    FrankfurterSource.name: FrankfurterSource,
}

DEFAULT_CHAIN = ("fbil", "rbi", "frankfurter")


def build_chain(names: list[str] | tuple[str, ...]) -> list[RateSource]:
    """Instantiate sources by name, preserving order."""
    chain: list[RateSource] = []
    for raw in names:
        key = raw.strip().lower()
        if not key:
            continue
        if key not in REGISTRY:
            raise KeyError(
                f"Unknown source {key!r}. Available: {', '.join(sorted(REGISTRY))}"
            )
        chain.append(REGISTRY[key]())
    if not chain:
        raise ValueError("Source chain is empty")
    return chain


__all__ = [
    "RateSource",
    "FBILSource",
    "RBISource",
    "FrankfurterSource",
    "REGISTRY",
    "DEFAULT_CHAIN",
    "build_chain",
]
