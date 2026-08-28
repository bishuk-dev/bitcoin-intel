from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from bitcoin_intel.graph.constants import NEO4J_VERSION

DEFAULT_IMAGE = f"sih26146-neo4j:{NEO4J_VERSION}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export the pinned, plugin-complete Neo4j image for offline transfer"
    )
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("../../offline/neo4j") / f"sih26146-neo4j-{NEO4J_VERSION}.tar",
    )
    args = parser.parse_args()
    export_image(args.image, args.output)
    return 0


def export_image(image: str, output: Path) -> Path:
    if not image.strip() or any(character.isspace() for character in image):
        raise ValueError("image reference must be non-empty and contain no whitespace")
    destination = output.expanduser().resolve(strict=False)
    metadata_path = destination.with_suffix(".json")
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary_metadata = metadata_path.with_name(f".{metadata_path.name}.tmp")
    existing = next(
        (
            path
            for path in (destination, metadata_path, temporary, temporary_metadata)
            if path.exists()
        ),
        None,
    )
    if existing is not None:
        raise FileExistsError(f"offline image export path already exists: {existing}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(("docker", "image", "inspect", image), "inspect the Neo4j image")
    try:
        _run(("docker", "save", "--output", str(temporary), image), "export the Neo4j image")
        metadata = {
            "image": image,
            "archive": destination.name,
            "bytes": temporary.stat().st_size,
            "sha256": _sha256_file(temporary),
            "exported_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        temporary_metadata.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(destination)
        temporary_metadata.replace(metadata_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        temporary_metadata.unlink(missing_ok=True)
        raise
    print(destination)
    return destination


def _run(command: tuple[str, ...], action: str) -> None:
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"failed to {action}: {detail}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
