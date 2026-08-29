from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import maxminddb
from maxminddb.errors import InvalidDatabaseError

PROVIDER = "DB-IP Lite"
LICENSE_IDENTIFIER = "CC-BY-4.0"
ATTRIBUTION = "IP Geolocation by DB-IP"
PROVIDER_URL = "https://db-ip.com"


class GeoIPResourceError(RuntimeError):
    """Raised when an offline MMDB resource is missing, invalid, or unsuitable."""


@dataclass(frozen=True, slots=True)
class ResourceDescriptor:
    role: str
    path: Path
    database_type: str
    build_epoch: int
    ip_version: int
    size: int
    sha256: str

    def manifest_value(self) -> dict[str, object]:
        return {
            "provider": PROVIDER,
            "role": self.role,
            "database_type": self.database_type,
            "release": None,
            "build_epoch": self.build_epoch,
            "ip_version": self.ip_version,
            "file_name": self.path.name,
            "bytes": self.size,
            "sha256": self.sha256,
            "license": LICENSE_IDENTIFIER,
            "attribution": ATTRIBUTION,
            "provider_url": PROVIDER_URL,
        }


class OfflineGeoIPReaders:
    """Opens each immutable database once and reuses both readers for the full build."""

    def __init__(self, country_path: Path, asn_path: Path) -> None:
        self.country_descriptor, self.country = _open(country_path, "country")
        try:
            self.asn_descriptor, self.asn = _open(asn_path, "asn")
        except Exception:
            self.country.close()
            raise

    def close(self) -> None:
        self.country.close()
        self.asn.close()

    def __enter__(self) -> OfflineGeoIPReaders:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _open(path: Path, role: str) -> tuple[ResourceDescriptor, Any]:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as error:
        raise GeoIPResourceError(f"{role} MMDB does not exist: {path}") from error
    if not resolved.is_file() or resolved.stat().st_size == 0:
        raise GeoIPResourceError(f"{role} MMDB must be a non-empty file: {resolved}")
    try:
        reader = maxminddb.open_database(resolved)
        metadata = reader.metadata()
    except (OSError, InvalidDatabaseError, ValueError) as error:
        raise GeoIPResourceError(f"{role} MMDB is unreadable or corrupt: {error}") from error
    database_type = metadata.database_type
    expected_token = "country" if role == "country" else "asn"
    normalized_type = database_type.lower()
    if "dbip" not in normalized_type or expected_token not in normalized_type:
        reader.close()
        raise GeoIPResourceError(f"{role} MMDB has incompatible database type {database_type!r}")
    return (
        ResourceDescriptor(
            role=role,
            path=resolved,
            database_type=database_type,
            build_epoch=int(metadata.build_epoch),
            ip_version=int(metadata.ip_version),
            size=resolved.stat().st_size,
            sha256=_sha256_file(resolved),
        ),
        reader,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
