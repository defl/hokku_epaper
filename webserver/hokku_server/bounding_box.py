"""Shared bounding-box type used across detection and rendering."""
from dataclasses import dataclass


@dataclass(frozen=True)
class BoundingBox:
    """Normalized bounding box (x, y, w, h) in [0, 1] range.

    All coordinates and dimensions must be > 0.
    Typically used to represent face bounding boxes or other regions of interest
    that need special handling (e.g., CLAHE keep-out regions).
    """
    x: float
    y: float
    w: float
    h: float

    def __post_init__(self):
        if not (self.x >= 0 and self.y >= 0 and self.w > 0 and self.h > 0):
            raise ValueError(
                f"BoundingBox position must be >= 0, dimensions must be > 0, "
                f"got x={self.x}, y={self.y}, w={self.w}, h={self.h}"
            )
