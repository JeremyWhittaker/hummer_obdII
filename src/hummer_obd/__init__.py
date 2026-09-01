"""Read-only OBD-II telemetry tooling for the Hummer EV Raspberry Pi node.

Every serial write in this package passes through :mod:`hummer_obd.safety`
first.  The gate is an allowlist: a command that is not explicitly known to be
read-only never reaches the adapter.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
