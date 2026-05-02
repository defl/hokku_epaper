"""Screen registry updates from _record_screen_call."""
from unittest.mock import patch

import webserver


class TestRecordScreenCall:
    def test_first_call_creates_entry(self):
        db = {"serve_data": {}}
        with patch.object(webserver.flask_app, "_database", db):
            webserver.flask_app._record_screen_call("Foyer", "192.168.1.5", 3600,
                                          served_name="a.jpg")
        entry = db["screens"]["Foyer"]
        assert entry["ip"] == "192.168.1.5"
        assert entry["request_count"] == 1
        assert entry["last_seen"] is not None
        assert entry["last_sleep_seconds"] == 3600
        assert entry["next_update_at"] is not None
        assert entry["last_served"] == "a.jpg"

    def test_repeat_call_increments_count_and_updates_next_update(self):
        db = {"serve_data": {}}
        with patch.object(webserver.flask_app, "_database", db):
            webserver.flask_app._record_screen_call("Foyer", "192.168.1.5", 60)
            first_next = db["screens"]["Foyer"]["next_update_at"]
            import time as _time
            _time.sleep(1.1)
            webserver.flask_app._record_screen_call("Foyer", "192.168.1.5", 120)
        entry = db["screens"]["Foyer"]
        assert entry["request_count"] == 2
        assert entry["last_sleep_seconds"] == 120
        assert entry["next_update_at"] != first_next

    def test_ip_updated_on_new_network(self):
        db = {"serve_data": {}}
        with patch.object(webserver.flask_app, "_database", db):
            webserver.flask_app._record_screen_call("Bed", "10.0.0.5", 600)
            webserver.flask_app._record_screen_call("Bed", "10.0.0.77", 600)
        assert db["screens"]["Bed"]["ip"] == "10.0.0.77"

    def test_served_name_optional(self):
        db = {"serve_data": {}}
        with patch.object(webserver.flask_app, "_database", db):
            webserver.flask_app._record_screen_call("A", "1.2.3.4", 60)
        assert "last_served" not in db["screens"]["A"]
