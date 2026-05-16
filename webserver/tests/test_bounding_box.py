"""Tests for BoundingBox dataclass."""
import pytest
from hokku_server.bounding_box import BoundingBox


class TestBoundingBoxCreation:
    """Test valid BoundingBox creation."""

    def test_valid_bbox_creation(self):
        """Create a valid bounding box."""
        bbox = BoundingBox(x=0.1, y=0.2, w=0.3, h=0.4)
        assert bbox.x == 0.1
        assert bbox.y == 0.2
        assert bbox.w == 0.3
        assert bbox.h == 0.4

    def test_bbox_all_small_positive_values(self):
        """Create bbox with very small but positive values."""
        bbox = BoundingBox(x=0.001, y=0.001, w=0.001, h=0.001)
        assert bbox.x > 0
        assert bbox.y > 0
        assert bbox.w > 0
        assert bbox.h > 0

    def test_bbox_normalized_range(self):
        """Create bbox with normalized [0, 1] values."""
        bbox = BoundingBox(x=0.5, y=0.5, w=0.5, h=0.5)
        assert bbox.x == 0.5
        assert bbox.y == 0.5


class TestBoundingBoxValidation:
    """Test BoundingBox validation."""

    def test_bbox_accepts_zero_x(self):
        """X coordinate can be 0 (top-left corner)."""
        bbox = BoundingBox(x=0.0, y=0.1, w=0.1, h=0.1)
        assert bbox.x == 0.0

    def test_bbox_accepts_zero_y(self):
        """Y coordinate can be 0 (top-left corner)."""
        bbox = BoundingBox(x=0.1, y=0.0, w=0.1, h=0.1)
        assert bbox.y == 0.0

    def test_bbox_rejects_zero_w(self):
        """Width must be > 0."""
        with pytest.raises(ValueError, match="dimensions must be > 0"):
            BoundingBox(x=0.1, y=0.1, w=0.0, h=0.1)

    def test_bbox_rejects_zero_h(self):
        """Height must be > 0."""
        with pytest.raises(ValueError, match="dimensions must be > 0"):
            BoundingBox(x=0.1, y=0.1, w=0.1, h=0.0)

    def test_bbox_rejects_negative_x(self):
        """X coordinate must be >= 0 (not negative)."""
        with pytest.raises(ValueError, match="position must be >= 0"):
            BoundingBox(x=-0.1, y=0.1, w=0.1, h=0.1)

    def test_bbox_rejects_negative_y(self):
        """Y coordinate must be >= 0 (not negative)."""
        with pytest.raises(ValueError, match="position must be >= 0"):
            BoundingBox(x=0.1, y=-0.1, w=0.1, h=0.1)

    def test_bbox_rejects_negative_w(self):
        """Width must be > 0 (not negative)."""
        with pytest.raises(ValueError, match="dimensions must be > 0"):
            BoundingBox(x=0.1, y=0.1, w=-0.1, h=0.1)

    def test_bbox_rejects_negative_h(self):
        """Height must be > 0 (not negative)."""
        with pytest.raises(ValueError, match="dimensions must be > 0"):
            BoundingBox(x=0.1, y=0.1, w=0.1, h=-0.1)

    def test_bbox_accepts_zero_coords_with_positive_size(self):
        """Zero coordinates with positive size is valid (top-left corner)."""
        bbox = BoundingBox(x=0.0, y=0.0, w=0.5, h=0.5)
        assert bbox.x == 0.0
        assert bbox.y == 0.0
        assert bbox.w == 0.5
        assert bbox.h == 0.5


class TestBoundingBoxImmutability:
    """Test that BoundingBox is frozen (immutable)."""

    def test_bbox_is_frozen(self):
        """Frozen dataclass prevents field modification."""
        bbox = BoundingBox(x=0.1, y=0.2, w=0.3, h=0.4)
        with pytest.raises(AttributeError):
            bbox.x = 0.5  # type: ignore

    def test_bbox_hashable(self):
        """Frozen dataclass is hashable."""
        bbox1 = BoundingBox(x=0.1, y=0.2, w=0.3, h=0.4)
        bbox2 = BoundingBox(x=0.1, y=0.2, w=0.3, h=0.4)
        # Should not raise
        hash(bbox1)
        hash(bbox2)
        # Equal boxes should have equal hashes
        assert bbox1 == bbox2
        assert hash(bbox1) == hash(bbox2)


class TestBoundingBoxComparison:
    """Test BoundingBox equality and comparison."""

    def test_bbox_equality(self):
        """Two boxes with same values are equal."""
        bbox1 = BoundingBox(x=0.1, y=0.2, w=0.3, h=0.4)
        bbox2 = BoundingBox(x=0.1, y=0.2, w=0.3, h=0.4)
        assert bbox1 == bbox2

    def test_bbox_inequality(self):
        """Two boxes with different values are not equal."""
        bbox1 = BoundingBox(x=0.1, y=0.2, w=0.3, h=0.4)
        bbox2 = BoundingBox(x=0.1, y=0.2, w=0.3, h=0.5)
        assert bbox1 != bbox2

    def test_bbox_in_set(self):
        """BoundingBox can be used in sets."""
        bbox1 = BoundingBox(x=0.1, y=0.2, w=0.3, h=0.4)
        bbox2 = BoundingBox(x=0.1, y=0.2, w=0.3, h=0.4)
        bbox3 = BoundingBox(x=0.5, y=0.5, w=0.5, h=0.5)
        bbox_set = {bbox1, bbox2, bbox3}
        # bbox1 and bbox2 are equal, so set should have 2 items
        assert len(bbox_set) == 2

    def test_bbox_as_dict_key(self):
        """BoundingBox can be used as dictionary key."""
        bbox1 = BoundingBox(x=0.1, y=0.2, w=0.3, h=0.4)
        bbox2 = BoundingBox(x=0.5, y=0.5, w=0.5, h=0.5)
        bbox_dict = {bbox1: "small", bbox2: "large"}
        assert bbox_dict[bbox1] == "small"
        assert bbox_dict[bbox2] == "large"
