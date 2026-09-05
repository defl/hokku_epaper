"""Persistent collection metadata and migration coverage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hokku.webserver.collection_store import (
    ALL_COLLECTION_ID,
    CollectionImmutableError,
    CollectionStore,
)


def test_existing_install_migrates_to_all_photos_without_membership_file(tmp_path: Path):
    store = CollectionStore(tmp_path)

    all_photos = store.get(ALL_COLLECTION_ID)
    assert all_photos.name == "All Photos"
    assert store.contains(ALL_COLLECTION_ID, "old-family-photo.jpg")
    assert store.image_names(ALL_COLLECTION_ID) == set()
    assert (tmp_path / "collections.json").exists()


def test_collection_crud_and_membership_persist(tmp_path: Path):
    store = CollectionStore(tmp_path)
    collection = store.create("Family", "People we love")
    assert collection.id != ALL_COLLECTION_ID
    assert collection.created_at == collection.updated_at

    assert store.add_images(collection.id, ["a.jpg", "b.jpg"]) == 2
    assert store.add_images(collection.id, ["a.jpg"]) == 0
    assert store.contains(collection.id, "a.jpg")
    assert store.image_names(collection.id) == {"a.jpg", "b.jpg"}

    renamed = store.update(collection.id, name="Family & Friends")
    assert renamed.id == collection.id
    assert renamed.updated_at >= collection.updated_at

    restored = CollectionStore(tmp_path)
    assert restored.get(collection.id).name == "Family & Friends"
    assert restored.image_names(collection.id) == {"a.jpg", "b.jpg"}

    assert restored.remove_images(collection.id, ["a.jpg"]) == 1
    restored.delete(collection.id)
    assert all(c.id != collection.id for c in restored.list())


def test_same_image_can_belong_to_multiple_collections(tmp_path: Path):
    store = CollectionStore(tmp_path)
    family = store.create("Family")
    favorites = store.create("Favorites")

    store.add_images(family.id, ["shared.jpg"])
    store.add_images(favorites.id, ["shared.jpg"])

    assert store.contains(family.id, "shared.jpg")
    assert store.contains(favorites.id, "shared.jpg")
    store.remove_images(family.id, ["shared.jpg"])
    assert not store.contains(family.id, "shared.jpg")
    assert store.contains(favorites.id, "shared.jpg")


def test_deleting_all_photos_is_protected(tmp_path: Path):
    store = CollectionStore(tmp_path)
    with pytest.raises(CollectionImmutableError):
        store.delete(ALL_COLLECTION_ID)
    with pytest.raises(CollectionImmutableError):
        store.update(ALL_COLLECTION_ID, name="Everything")
    with pytest.raises(CollectionImmutableError):
        store.add_images(ALL_COLLECTION_ID, ["x.jpg"])


def test_malformed_membership_is_ignored_but_valid_collections_survive(tmp_path: Path):
    (tmp_path / "collections.json").write_text(
        json.dumps(
            {
                "version": 1,
                "collections": [
                    {
                        "id": "all",
                        "name": "User renamed this",
                        "description": "bad",
                        "created_at": 1,
                        "updated_at": 1,
                    },
                    {
                        "id": "custom",
                        "name": "Art",
                        "description": "",
                        "created_at": 1,
                        "updated_at": 1,
                    },
                ],
                "memberships": {"custom": ["a.jpg", 7], "broken": "not-a-list"},
            }
        )
    )
    store = CollectionStore(tmp_path)
    assert store.get("custom").name == "Art"
    assert store.image_names("custom") == {"a.jpg"}
    assert store.get(ALL_COLLECTION_ID).name == "All Photos"


def test_malformed_root_starts_with_all_photos(tmp_path: Path):
    (tmp_path / "collections.json").write_text(json.dumps(["not", "a", "mapping"]))

    store = CollectionStore(tmp_path)

    assert [collection.id for collection in store.list()] == [ALL_COLLECTION_ID]
