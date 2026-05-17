"""ImageRecord dataclass and serialisation helpers."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from hokku_server.orientation import Orientation


ConvertStatus = Literal["ok", "failed", "pending"]


@dataclass(frozen=True)
class ImageRecord:
    name: str                               # outside-world identifier
    name_hash: str                          # sha1(name) — on-disk identifier
    original_sha1: str                      # sha1 of file contents
    original_size_bytes: int
    original_mtime: float
    added_at: float
    convert_status: ConvertStatus
    convert_error: str | None
    landscape_image_config_slug: str | None = None  # ScreenImageConfig slug for landscape render
    portrait_image_config_slug:  str | None = None  # ScreenImageConfig slug for portrait render
    last_conversion_seconds: float | None = None    # wall-clock time of last successful render
    image_width: int | None = None                  # pixel dimensions of the source image
    image_height: int | None = None


@dataclass(frozen=True)
class ConversionProgress:
    current_name: str | None  # being converted right now (None if idle)
    done: int                 # completed this sync cycle
    total: int                # scheduled this sync cycle


def slug_for(rec: ImageRecord, orientation: Orientation) -> str | None:
    """Return the cached slug for the given orientation, or None if not yet rendered."""
    return (
        rec.landscape_image_config_slug
        if orientation == Orientation.LANDSCAPE
        else rec.portrait_image_config_slug
    )


def record_to_dict(rec: ImageRecord) -> dict:
    return asdict(rec)


def record_from_dict(d: dict) -> ImageRecord:
    raw_t = d.get("last_conversion_seconds")
    raw_w, raw_h = d.get("image_width"), d.get("image_height")
    return ImageRecord(
        name=d["name"],
        name_hash=d["name_hash"],
        original_sha1=d["original_sha1"],
        original_size_bytes=int(d["original_size_bytes"]),
        original_mtime=float(d["original_mtime"]),
        added_at=float(d["added_at"]),
        convert_status=d["convert_status"],
        convert_error=d.get("convert_error"),
        landscape_image_config_slug=d.get("landscape_image_config_slug"),
        portrait_image_config_slug=d.get("portrait_image_config_slug"),
        last_conversion_seconds=float(raw_t) if raw_t is not None else None,
        image_width=int(raw_w) if raw_w is not None else None,
        image_height=int(raw_h) if raw_h is not None else None,
    )
