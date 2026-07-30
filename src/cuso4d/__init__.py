"""CPI: a typed access layer over cuso4d's personal data exports.

Archives are the source of truth. Every module here reads immutable, timestamped
export files and assembles the full view at runtime; nothing is ever normalized
into a persistent database.
"""
