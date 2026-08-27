"""Canonical ingestion pipeline for SIH Bitcoin metadata."""

from bitcoin_intel.ingestion.pipeline import IngestionSummary, ingest_file

__all__ = ["IngestionSummary", "ingest_file"]
