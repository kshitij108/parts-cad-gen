"""Private file storage for customer uploads.

Files live under <data>/jobs/<job_id>/originals/, are never served statically,
and are never sent anywhere for training. Deleting a job removes its folder.
Swap this module for an encrypted object store (private bucket, per-tenant
prefix) in production.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from pathlib import Path

MAX_UPLOAD_BYTES = 100 * 1024 * 1024


def data_root() -> Path:
    return Path(os.environ.get("PARTS_CAD_DATA", "data")).resolve()


def job_dir(job_id: str) -> Path:
    return data_root() / "jobs" / job_id


def save_original(job_id: str, original_name: str, content: bytes) -> tuple[str, Path, str]:
    """Store bytes under a random name (the original name is metadata only)."""
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(f"{original_name} exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    file_id = uuid.uuid4().hex
    suffix = Path(original_name).suffix.lower()[:10]
    dest = job_dir(job_id) / "originals" / f"{file_id}{suffix}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    os.chmod(dest, 0o600)
    return file_id, dest, hashlib.sha256(content).hexdigest()


def delete_job_files(job_id: str) -> None:
    d = job_dir(job_id)
    if d.exists() and d.is_relative_to(data_root()):
        shutil.rmtree(d)
