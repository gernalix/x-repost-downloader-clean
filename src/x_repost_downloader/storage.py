from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

from .config import Config


@dataclass(frozen=True, slots=True)
class StorageStatus:
    mount: str
    free_bytes: int
    total_bytes: int


def verify_storage(config: Config, require_output_writable: bool = True) -> StorageStatus:
    mount = config.required_mount.resolve()
    output = config.output_dir.resolve()
    if not output.is_relative_to(mount):
        raise RuntimeError(f"output directory {output} is outside required mount {mount}")
    if not mount.is_mount():
        raise RuntimeError(f"required storage is not mounted at {mount}")
    usage = shutil.disk_usage(mount)
    minimum = config.minimum_free_gib * 1024**3
    if usage.free < minimum:
        raise RuntimeError(
            f"insufficient free space on {mount}: {usage.free} bytes available, {minimum} required"
        )
    if require_output_writable and (not output.is_dir() or not os.access(output, os.W_OK)):
        raise RuntimeError(f"output directory is not writable: {output}")
    return StorageStatus(str(mount), usage.free, usage.total)
