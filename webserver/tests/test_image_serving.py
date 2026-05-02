"""Thumbnail, original, and dithered image HTTP endpoints."""
from dataclasses import replace
from io import BytesIO
from unittest.mock import patch

import pytest
from PIL import Image

import webserver


class TestImageServingEndpoints:
    @pytest.fixture
    def client(self, tmp_path):
        webserver.app.config["TESTING"] = True
        upload = tmp_path / "upload"; upload.mkdir()
        cache = tmp_path / "cache"; cache.mkdir()
        cfg = replace(
            webserver.DEFAULT_CONFIG,
            upload_dir=str(upload),
            cache_dir=str(cache),
        )
        with patch.object(webserver.flask_app, "_config", cfg), \
             webserver.app.test_client() as client:
            yield client, upload, cache

    def test_thumbnail_404_for_missing_file(self, client):
        client_, _, _ = client
        resp = client_.get("/hokku/api/thumbnail/nope.jpg")
        assert resp.status_code == 404

    def test_original_404_for_missing_file(self, client):
        client_, _, _ = client
        resp = client_.get("/hokku/api/original/nope.jpg")
        assert resp.status_code == 404

    def test_dithered_404_for_missing_file(self, client):
        client_, _, _ = client
        with patch.object(webserver.flask_app, "_pool", {}):
            resp = client_.get("/hokku/api/dithered/nope.jpg")
        assert resp.status_code == 404

    def test_original_jpeg_served_directly(self, client):
        client_, upload, _ = client
        p = upload / "pic.jpg"
        Image.new("RGB", (100, 100), (200, 100, 50)).save(p, "JPEG")
        resp = client_.get("/hokku/api/original/pic.jpg")
        assert resp.status_code == 200
        assert resp.headers["Content-Type"].startswith("image/jpeg")

    def test_original_converts_non_browser_formats_to_jpeg(self, client):
        client_, upload, _ = client
        p = upload / "scan.tiff"
        Image.new("RGB", (100, 100), (200, 100, 50)).save(p, "TIFF")
        resp = client_.get("/hokku/api/original/scan.tiff")
        assert resp.status_code == 200
        assert resp.headers["Content-Type"] == "image/jpeg"

    def test_thumbnail_generated_on_demand(self, client):
        client_, upload, _ = client
        p = upload / "big.jpg"
        Image.new("RGB", (2000, 1500), (10, 20, 30)).save(p, "JPEG")
        resp = client_.get("/hokku/api/thumbnail/big.jpg")
        assert resp.status_code == 200
        img = Image.open(BytesIO(resp.data))
        assert max(img.size) <= 300
