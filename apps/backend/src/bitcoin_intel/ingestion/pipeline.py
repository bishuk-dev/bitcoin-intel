from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from bitcoin_intel.ingestion.adapters import detect_source_format, iter_source_records
from bitcoin_intel.ingestion.errors import IngestionFileError
from bitcoin_intel.ingestion.models import ValidatedInputRecord, validation_error_to_issue
from bitcoin_intel.ingestion.normalization import CanonicalAccumulator
from bitcoin_intel.ingestion.parquet import SCHEMA_VERSION, write_canonical_dataset
from bitcoin_intel.ingestion.validation import build_source_record_id

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IngestionSummary:
    records_read: int
    records_accepted: int
    records_rejected: int
    unique_transactions: int
    network_observations: int
    output_path: Path


def ingest_file(input_path: Path, output_path: Path) -> IngestionSummary:
    source = input_path.expanduser().resolve(strict=True)
    if not source.is_file():
        raise IngestionFileError(f"input is not a regular file: {source}")
    source_format = detect_source_format(source)

    destination = output_path.expanduser().resolve(strict=False)
    if destination.exists():
        raise IngestionFileError(
            f"output already exists and will not be overwritten: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)

    initial_stat = source.stat()
    source_sha256 = _sha256_file(source)
    source_name = source.name
    logger.info("source opened: file=%s format=%s", source_name, source_format.value)

    staging_path = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=destination.parent)
    )
    try:
        accumulator = CanonicalAccumulator()
        records_read = 0
        records_accepted = 0
        records_rejected = 0

        for raw_record in iter_source_records(source, source_format):
            records_read += 1
            source_record_id = build_source_record_id(source_sha256, raw_record.record_index)
            accumulator.add_source_record(
                source_record_id=source_record_id,
                source_file=source_name,
                source_format=source_format,
                source_file_sha256=source_sha256,
                record_index=raw_record.record_index,
            )

            issue = raw_record.issue
            validated: ValidatedInputRecord | None = None
            if issue is None:
                if raw_record.data is None:
                    raise AssertionError("adapter returned neither record data nor an issue")
                try:
                    validated = ValidatedInputRecord.model_validate(raw_record.data)
                except ValidationError as error:
                    issue = validation_error_to_issue(error)

            if issue is None and validated is not None:
                issue = accumulator.accept(validated, source_record_id=source_record_id)

            if issue is not None:
                records_rejected += 1
                accumulator.reject(
                    source_record_id=source_record_id,
                    source_file=source_name,
                    record_index=raw_record.record_index,
                    issue=issue,
                )
                continue
            records_accepted += 1

        final_stat = source.stat()
        if (initial_stat.st_size, initial_stat.st_mtime_ns) != (
            final_stat.st_size,
            final_stat.st_mtime_ns,
        ):
            raise IngestionFileError("input file changed while ingestion was running")

        dataset = accumulator.build()
        manifest_fields = {
            "source": {
                "file": source_name,
                "format": source_format.value,
                "sha256": source_sha256,
                "size_bytes": initial_stat.st_size,
            },
            "records_read": records_read,
            "records_accepted": records_accepted,
            "records_rejected": records_rejected,
            "unique_transactions": len(dataset.transactions),
            "network_observations": len(dataset.network_observations),
        }
        write_canonical_dataset(staging_path, dataset, manifest_fields)
        if destination.exists():
            raise IngestionFileError(
                f"output was created concurrently and will not be overwritten: {destination}"
            )
        staging_path.replace(destination)
        logger.info(
            "dataset written: output=%s read=%d accepted=%d rejected=%d schema=%s",
            destination,
            records_read,
            records_accepted,
            records_rejected,
            SCHEMA_VERSION,
        )
        return IngestionSummary(
            records_read=records_read,
            records_accepted=records_accepted,
            records_rejected=records_rejected,
            unique_transactions=len(dataset.transactions),
            network_observations=len(dataset.network_observations),
            output_path=destination,
        )
    except Exception:
        logger.exception("ingestion failed: source=%s", source_name)
        if staging_path.exists():
            shutil.rmtree(staging_path)
        raise


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as file_handle:
            while chunk := file_handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise IngestionFileError(f"failed to read input file: {error}") from error
    return digest.hexdigest()
