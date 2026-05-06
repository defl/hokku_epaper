"""POST /hokku/api/upload and DELETE /hokku/api/image/."""
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

import webserver


class TestUpload:
    @pytest.fixture
    def client_with_upload_dir(self, tmp_path):
        webserver.app.config["TESTING"] = True
        upload_dir = tmp_path / "upload"; upload_dir.mkdir()
        src_dir = tmp_path / "src"; src_dir.mkdir()
        cfg = replace(
            webserver.DEFAULT_CONFIG,
            upload_dir=str(upload_dir),
            cache_dir=str(tmp_path / "cache"),
        )
        with patch.object(webserver.flask_app, "_config", cfg), \
             patch("webserver.flask_app._sync_pool"), \
             webserver.app.test_client() as client:
            yield client, upload_dir, src_dir

    def _make_jpeg(self, path):
        Image.new("RGB", (32, 32), (10, 20, 30)).save(path, "JPEG")
        return path.read_bytes()

    def test_upload_single_jpeg_success(self, client_with_upload_dir):
        client, upload_dir, src_dir = client_with_upload_dir
        src = src_dir / "src.jpg"
        data = self._make_jpeg(src)
        resp = client.post("/hokku/api/upload",
                           data={"files": (src.open("rb"), "holiday.jpg")},
                           content_type="multipart/form-data")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["saved"] == ["holiday.jpg"]
        assert (upload_dir / "holiday.jpg").read_bytes() == data

    def test_upload_rejects_unsupported_extension(self, client_with_upload_dir):
        client, upload_dir, _ = client_with_upload_dir
        resp = client.post("/hokku/api/upload",
                           data={"files": (BytesIO(b"not an image"), "virus.exe")},
                           content_type="multipart/form-data")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["saved"] == []
        assert len(body["skipped"]) == 1
        assert "unsupported type" in body["skipped"][0]["reason"]
        assert not (upload_dir / "virus.exe").exists()

    def test_upload_collision_suffix(self, client_with_upload_dir):
        client, upload_dir, src_dir = client_with_upload_dir
        existing = upload_dir / "clash.jpg"
        self._make_jpeg(existing)
        src = src_dir / "src.jpg"
        self._make_jpeg(src)
        resp = client.post("/hokku/api/upload",
                           data={"files": (src.open("rb"), "clash.jpg")},
                           content_type="multipart/form-data")
        assert resp.status_code == 200
        saved = resp.get_json()["saved"]
        assert saved == ["clash_1.jpg"]
        assert (upload_dir / "clash.jpg").exists()
        assert (upload_dir / "clash_1.jpg").exists()

    def test_upload_multiple_files(self, client_with_upload_dir):
        client, upload_dir, src_dir = client_with_upload_dir
        a = src_dir / "a.jpg"; self._make_jpeg(a)
        b = src_dir / "b.png"
        Image.new("RGB", (16, 16), (1, 2, 3)).save(b, "PNG")
        resp = client.post("/hokku/api/upload", content_type="multipart/form-data",
                           data={"files": [(a.open("rb"), "a.jpg"), (b.open("rb"), "b.png")]})
        assert resp.status_code == 200
        assert set(resp.get_json()["saved"]) == {"a.jpg", "b.png"}

    def test_upload_no_files_returns_400(self, client_with_upload_dir):
        client, _, _ = client_with_upload_dir
        resp = client.post("/hokku/api/upload", content_type="multipart/form-data", data={})
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_upload_path_traversal_rejected(self, client_with_upload_dir):
        client, upload_dir, src_dir = client_with_upload_dir
        src = src_dir / "src.jpg"
        self._make_jpeg(src)
        resp = client.post("/hokku/api/upload", content_type="multipart/form-data",
                           data={"files": (src.open("rb"), "../../../etc/passwd.jpg")})
        assert resp.status_code == 200
        escaped = (upload_dir / ".." / ".." / ".." / "etc" / "passwd.jpg").resolve()
        assert not escaped.exists()

    def test_upload_filesystem_readonly_returns_json_error(self, client_with_upload_dir):
        client, _, src_dir = client_with_upload_dir
        src = src_dir / "src.jpg"; self._make_jpeg(src)
        from pathlib import Path as _Path
        with patch.object(_Path, "mkdir", side_effect=OSError(30, "Read-only file system")):
            resp = client.post("/hokku/api/upload", content_type="multipart/form-data",
                               data={"files": (src.open("rb"), "x.jpg")})
        assert resp.status_code == 500
        assert "error" in resp.get_json()
        assert resp.is_json


class TestDeleteImage:
    @pytest.fixture
    def client_with_image(self, tmp_path):
        webserver.app.config["TESTING"] = True
        upload_dir = tmp_path / "upload"; upload_dir.mkdir()
        cache_dir = tmp_path / "cache"; cache_dir.mkdir()
        (upload_dir / "victim.jpg").write_bytes(b"fake-image")
        (cache_dir / "victim_thumb.jpg").write_bytes(b"fake-thumb")
        cfg = replace(
            webserver.DEFAULT_CONFIG,
            upload_dir=str(upload_dir),
            cache_dir=str(cache_dir),
        )
        from webserver.image_manager import ImageManager
        with patch.object(webserver.flask_app, "_config", cfg), \
             patch.object(webserver.flask_app, "_image_manager", ImageManager(cfg)), \
             patch("webserver.flask_app._sync_pool"), \
             webserver.app.test_client() as client:
            yield client, upload_dir, cache_dir

    def test_delete_removes_original_and_thumbnail(self, client_with_image):
        client, upload_dir, cache_dir = client_with_image
        resp = client.delete("/hokku/api/image/victim.jpg")
        assert resp.status_code == 200
        assert resp.get_json()["deleted"] == "victim.jpg"
        assert not (upload_dir / "victim.jpg").exists()
        assert not (cache_dir / "victim_thumb.jpg").exists()

    def test_delete_missing_file_returns_404(self, client_with_image):
        client, _, _ = client_with_image
        resp = client.delete("/hokku/api/image/does-not-exist.jpg")
        assert resp.status_code == 404
        assert "error" in resp.get_json()

    def test_delete_triggers_sync(self, client_with_image):
        client, _, _ = client_with_image
        with patch("webserver.flask_app.threading.Thread") as mock_thread:
            resp = client.delete("/hokku/api/image/victim.jpg")
            assert resp.status_code == 200
            assert mock_thread.called
            kwargs = mock_thread.call_args.kwargs
            assert kwargs.get("target") is webserver.flask_app._sync_pool

    def test_delete_preserves_other_files(self, client_with_image):
        client, upload_dir, _ = client_with_image
        (upload_dir / "keep.jpg").write_bytes(b"other")
        resp = client.delete("/hokku/api/image/victim.jpg")
        assert resp.status_code == 200
        assert (upload_dir / "keep.jpg").exists()
