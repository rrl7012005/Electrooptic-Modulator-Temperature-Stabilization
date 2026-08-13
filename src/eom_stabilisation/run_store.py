"""Atomic run artifacts, configuration snapshots, and SHA-256 provenance."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

import yaml


def sha256_bytes(data: bytes) -> str:
    """Return the lowercase SHA-256 digest of bytes."""

    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    """Hash a file without loading a large file into memory."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    """Hash a JSON-compatible value using canonical key ordering."""

    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return sha256_bytes(payload)


def atomic_write_bytes(path: str | Path, data: bytes) -> Path:
    """Replace one file from a same-directory temporary file."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        with temporary.open("wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def atomic_write_text(
    path: str | Path,
    text: str,
    *,
    encoding: str = "utf-8",
) -> Path:
    """Atomically replace a text file."""

    return atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(path: str | Path, value: Any) -> Path:
    """Atomically write indented strict JSON."""

    return atomic_write_text(
        path,
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
    )


@dataclass(frozen=True)
class RunStore:
    """Filesystem owner for one experiment run directory."""

    root: Path

    @classmethod
    def create(cls, root: str | Path, *, exist_ok: bool = False) -> "RunStore":
        path = Path(root).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=exist_ok)
        return cls(path)

    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    def write_effective_experiment(self, effective: Mapping[str, Any]) -> Path:
        """Write the expanded effective configuration as deterministic YAML."""

        text = yaml.safe_dump(
            dict(effective),
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        )
        return atomic_write_text(self.path("effective_experiment.yaml"), text)

    def snapshot_configuration(
        self,
        *,
        sources: Mapping[str, Path],
        source_hashes: Mapping[str, str],
        effective: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Copy immutable source bytes and record requested/effective hashes."""

        if set(sources) != set(source_hashes):
            raise ValueError("sources and source_hashes must have identical roles.")
        originals_directory = self.path("configuration", "originals")
        originals_directory.mkdir(parents=True, exist_ok=True)
        copied: dict[str, dict[str, str]] = {}
        for role, raw_source in sorted(sources.items()):
            source = Path(raw_source).resolve()
            if not source.is_file():
                raise FileNotFoundError(f"Configuration source is missing: {source}")
            actual_hash = sha256_file(source)
            expected_hash = source_hashes[role]
            if actual_hash != expected_hash:
                raise ValueError(
                    f"Configuration source {role!r} changed before snapshotting."
                )
            safe_role = "".join(
                character if character.isalnum() or character in "_-" else "_"
                for character in role
            )
            snapshot = originals_directory / f"{safe_role}_{source.name}"
            if snapshot.exists():
                raise FileExistsError(
                    f"Configuration snapshot already exists: {snapshot}"
                )
            atomic_write_bytes(snapshot, source.read_bytes())
            copied[role] = {
                "source_path": str(source),
                "snapshot_path": str(snapshot.relative_to(self.root)),
                "sha256": actual_hash,
            }

        effective_path = self.write_effective_experiment(effective)
        effective_hash = sha256_file(effective_path)
        manifest = {
            "algorithm": "sha256",
            "sources": copied,
            "effective_experiment": {
                "path": str(effective_path.relative_to(self.root)),
                "sha256": effective_hash,
                "canonical_sha256": canonical_sha256(effective),
            },
        }
        atomic_write_json(self.path("configuration_hashes.json"), manifest)
        return manifest

    def reserve_raw_path(self, *parts: str) -> Path:
        """Return a raw-output path only when it does not already exist."""

        path = self.path(*parts)
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite raw output: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
