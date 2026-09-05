"""Spatial analysis over remote GeoParquet, served to agents over MCP.

The dataset stays where it is published. DuckDB reads it in place over HTTP
range requests, and only the byte ranges a query needs ever cross the network.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
