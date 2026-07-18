"""Legacy geo helpers.

Routing land/depth now come exclusively from the chart pipeline in
``app.geo.fetch`` (OSM land + ``region_depth.tif``). This module no longer
fetches WCS bathymetry or GSHHG for the router.
"""

from __future__ import annotations

# Intentionally empty of routing data sources — see app.geo.fetch.
