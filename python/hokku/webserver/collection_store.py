"""Persistent photo collection metadata.

Collections are deliberately kept separate from the image cache. A collection
contains image *names*, not copies of image files, so membership changes do not
touch conversion artifacts or serve statistics. ``all`` is a protected virtual
view of every image in the library, not a persisted collection or membership.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from hokku.webserver.filesystem import atomic_write_json

logger = logging.getLogger(__name__)


_DB_FILENAME = "collections.json"
_DB_VERSION = 2
ALL_COLLECTION_ID = "all"
ALL_COLLECTION_NAME = "All Photos"


class CollectionNotFoundError(KeyError):
    """Raised when an API or scheduler references an unknown collection."""


class CollectionImmutableError(ValueError):
    """Raised when a caller tries to mutate the virtual All Photos view."""


@dataclass(frozen=True)
class Collection:
    id: str
    name: str
    description: str
    created_at: float
    updated_at: float

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Collection:
        collection_id = str(data["id"])
        name = str(data["name"]).strip()
        if not collection_id or not name:
            raise ValueError("collection id and name are required")
        return cls(
            id=collection_id,
            name=name,
            description=str(data.get("description", "")),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
        )


class CollectionStore:
    """Thread-safe, atomic JSON metadata store for collections and membership."""

    def __init__(self, cache_dir: str | Path) -> None:
        self._path = Path(cache_dir) / _DB_FILENAME
        self._lock = threading.RLock()
        self._collections: dict[str, Collection] = {}
        self._members: dict[str, set[str]] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def list(self, *, include_all: bool = False) -> list[Collection]:
        with self._lock:
            collections = sorted(self._collections.values(), key=lambda c: c.name.casefold())
            if include_all:
                return [self._all_view(), *collections]
            return collections

    def get(self, collection_id: str) -> Collection:
        with self._lock:
            return self._require(collection_id)

    def contains(self, collection_id: str, image_name: str) -> bool:
        with self._lock:
            self._require(collection_id)
            return collection_id == ALL_COLLECTION_ID or image_name in self._members.get(
                collection_id, set()
            )

    def image_names(self, collection_id: str) -> set[str]:
        """Return explicit membership names; All Photos is resolved by its caller."""
        with self._lock:
            self._require(collection_id)
            if collection_id == ALL_COLLECTION_ID:
                return set()
            return set(self._members.get(collection_id, set()))

    def create(self, name: str, description: str = "") -> Collection:
        clean_name = self._validate_name(name)
        clean_description = self._validate_description(description)
        with self._lock:
            if clean_name.casefold() == ALL_COLLECTION_NAME.casefold():
                raise ValueError("All Photos is the unfiltered view, not a collection")
            if any(c.name.casefold() == clean_name.casefold() for c in self._collections.values()):
                raise ValueError(f"A collection named {clean_name!r} already exists")
            now = time.time()
            collection = Collection(
                id=uuid.uuid4().hex,
                name=clean_name,
                description=clean_description,
                created_at=now,
                updated_at=now,
            )
            self._collections[collection.id] = collection
            self._members[collection.id] = set()
            self._save_locked()
            return collection

    def update(
        self,
        collection_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Collection:
        with self._lock:
            current = self._require(collection_id)
            if collection_id == ALL_COLLECTION_ID:
                raise CollectionImmutableError("All Photos cannot be renamed or edited")
            clean_name = current.name if name is None else self._validate_name(name)
            clean_description = (
                current.description
                if description is None
                else self._validate_description(description)
            )
            if any(
                c.id != collection_id and c.name.casefold() == clean_name.casefold()
                for c in self._collections.values()
            ):
                raise ValueError(f"A collection named {clean_name!r} already exists")
            updated = replace(
                current,
                name=clean_name,
                description=clean_description,
                updated_at=time.time(),
            )
            self._collections[collection_id] = updated
            self._save_locked()
            return updated

    def delete(self, collection_id: str) -> None:
        with self._lock:
            self._require(collection_id)
            if collection_id == ALL_COLLECTION_ID:
                raise CollectionImmutableError("All Photos cannot be deleted")
            del self._collections[collection_id]
            self._members.pop(collection_id, None)
            self._save_locked()

    def add_images(self, collection_id: str, image_names: list[str] | set[str]) -> int:
        names = self._validate_image_names(image_names)
        with self._lock:
            self._require_mutable(collection_id)
            members = self._members.setdefault(collection_id, set())
            before = len(members)
            members.update(names)
            if len(members) != before:
                self._touch_locked(collection_id)
                self._save_locked()
            return len(members) - before

    def remove_images(self, collection_id: str, image_names: list[str] | set[str]) -> int:
        names = self._validate_image_names(image_names)
        with self._lock:
            self._require_mutable(collection_id)
            members = self._members.setdefault(collection_id, set())
            before = len(members)
            members.difference_update(names)
            if len(members) != before:
                self._touch_locked(collection_id)
                self._save_locked()
            return before - len(members)

    def image_collections(self, image_name: str) -> set[str]:
        """Return the explicit collection IDs containing ``image_name``."""
        with self._lock:
            return {
                collection_id
                for collection_id, members in self._members.items()
                if collection_id in self._collections and image_name in members
            }

    def set_image_collections(
        self, image_name: str, collection_ids: list[str] | set[str]
    ) -> set[str]:
        """Replace an image's explicit memberships and return the resulting IDs."""
        if not isinstance(collection_ids, (list, set)):
            raise ValueError("collection_ids must be a list of collection IDs")
        ids = set()
        with self._lock:
            for collection_id in collection_ids:
                if not isinstance(collection_id, str) or not collection_id:
                    raise ValueError("collection IDs must be non-empty strings")
                self._require_mutable(collection_id)
                ids.add(collection_id)
            current = self.image_collections(image_name)
            changed = current.symmetric_difference(ids)
            for collection_id in changed:
                members = self._members.setdefault(collection_id, set())
                if collection_id in ids:
                    members.add(image_name)
                else:
                    members.discard(image_name)
                self._touch_locked(collection_id)
            if changed:
                self._save_locked()
            return ids

    def remove_image(self, image_name: str) -> None:
        """Remove a deleted library image from every explicit collection."""
        with self._lock:
            changed = False
            for collection_id, members in self._members.items():
                if image_name in members:
                    members.remove(image_name)
                    self._touch_locked(collection_id)
                    changed = True
            if changed:
                self._save_locked()

    def _require(self, collection_id: str) -> Collection:
        if collection_id == ALL_COLLECTION_ID:
            return self._all_view()
        try:
            return self._collections[collection_id]
        except KeyError as e:
            raise CollectionNotFoundError(collection_id) from e

    def _require_mutable(self, collection_id: str) -> Collection:
        collection = self._require(collection_id)
        if collection_id == ALL_COLLECTION_ID:
            raise CollectionImmutableError("All Photos membership is automatic")
        return collection

    def _touch_locked(self, collection_id: str) -> None:
        current = self._collections[collection_id]
        self._collections[collection_id] = replace(current, updated_at=time.time())

    @staticmethod
    def _validate_name(name: str) -> str:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("collection name must not be empty")
        clean = name.strip()
        if len(clean) > 120:
            raise ValueError("collection name must be 120 characters or fewer")
        return clean

    @staticmethod
    def _validate_description(description: str) -> str:
        if not isinstance(description, str):
            raise ValueError("collection description must be a string")
        if len(description) > 500:
            raise ValueError("collection description must be 500 characters or fewer")
        return description.strip()

    @staticmethod
    def _validate_image_names(image_names: list[str] | set[str]) -> set[str]:
        if not isinstance(image_names, (list, set)):
            raise ValueError("images must be a list of image names")
        names = set()
        for name in image_names:
            if not isinstance(name, str) or not name or "/" in name or "\\" in name:
                raise ValueError("image names must be non-empty filenames")
            names.add(name)
        return names

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with open(self._path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Failed to load %s: %s (starting with All Photos)", _DB_FILENAME, e)
            return
        if not isinstance(data, dict):
            logger.warning("Invalid %s root (starting with All Photos)", _DB_FILENAME)
            return
        version = data.get("version")
        if version not in (1, _DB_VERSION):
            logger.warning(
                "Unsupported %s version %r (starting with empty collections)",
                _DB_FILENAME,
                version,
            )
            return
        needs_migration = version != _DB_VERSION
        raw_collections = data.get("collections", [])
        if not isinstance(raw_collections, list):
            logger.warning("Invalid collections in %s (starting with All Photos)", _DB_FILENAME)
            raw_collections = []
        for blob in raw_collections:
            try:
                collection = Collection.from_dict(blob)
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("Skipping malformed collection: %s", e)
                continue
            if collection.id == ALL_COLLECTION_ID:
                needs_migration = True
                continue
            self._collections[collection.id] = collection
        raw_members = data.get("memberships", {})
        if isinstance(raw_members, dict):
            for collection_id, names in raw_members.items():
                if not isinstance(collection_id, str) or not isinstance(names, list):
                    needs_migration = True
                    continue
                if collection_id == ALL_COLLECTION_ID or collection_id not in self._collections:
                    needs_migration = True
                    continue
                self._members[collection_id] = {name for name in names if isinstance(name, str)}
        if needs_migration:
            self._save_locked()

    @staticmethod
    def _all_view() -> Collection:
        return Collection(
            id=ALL_COLLECTION_ID,
            name=ALL_COLLECTION_NAME,
            description="Every image in the library.",
            created_at=0.0,
            updated_at=0.0,
        )

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            self._path,
            {
                "version": _DB_VERSION,
                "collections": [c.to_dict() for c in self.list()],
                "memberships": {
                    collection_id: sorted(names)
                    for collection_id, names in self._members.items()
                    if collection_id in self._collections
                },
            },
        )
