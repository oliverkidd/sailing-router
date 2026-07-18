"""Wind pipeline: forcing cache + terrain-response downscaling.

Public entry points live on ``WindProvider`` — map display, prepare, and route
all consume the same path. Terrain physics (operator v1) applies when a
precomputed library is present; otherwise the provider is identity.
"""

from .provider import WindProvider, get_provider
from .zoom_tiers import ZoomTier, display_window, tier_for_zoom

__all__ = [
    "WindProvider",
    "ZoomTier",
    "display_window",
    "get_provider",
    "tier_for_zoom",
]
