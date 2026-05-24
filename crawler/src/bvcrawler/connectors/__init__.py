"""Source connector registry.

Each connector is a module under this package that exposes a single
`Connector` instance assigned to `CONNECTOR`. Add it to `CONNECTORS`
below once implemented.
"""

from bvcrawler.connectors.base import Connector

# Registry populated as connectors are implemented:
# from bvcrawler.connectors import tcia, openneuro
# CONNECTORS: dict[str, Connector] = {"tcia": tcia.CONNECTOR, "openneuro": openneuro.CONNECTOR}
CONNECTORS: dict[str, Connector] = {}

__all__ = ["CONNECTORS", "Connector"]
