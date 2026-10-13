"""CSV-backed biological sample and lab inventory database with an MCP interface."""

__version__ = "0.1.0"

SCHEMA_VERSION = 3  # v3: containers.capacity_override became capacity, and every
# container gained one -- occupancy counts nested containers as slots taken.
