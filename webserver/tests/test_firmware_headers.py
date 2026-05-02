"""X-Battery-mV, X-Frame-State parsing and integration with /hokku/screen/."""
from dataclasses import replace
from unittest.mock import patch

import webserver


class TestBatteryReporting:
    def test_parse_valid_mv(self):
        assert webserver._parse_battery_header("4100") == 4100
        assert webserver._parse_battery_header(" 3800 ") == 3800
        assert webserver._parse_battery_header("3400") == 3400

    def test_parse_rejects_garbage(self):
        assert webserver._parse_battery_header(None) is None
        assert webserver._parse_battery_header("") is None
        assert webserver._parse_battery_header("nope") is None

    def test_parse_rejects_out_of_range(self):
        assert webserver._parse_battery_header("0") is None
        assert webserver._parse_battery_header("500") is None
        assert webserver._parse_battery_header("10000") is None

    def test_percent_at_anchors(self):
        assert webserver._battery_percent(webserver.BATTERY_MV_FULL) == 100
        assert webserver._battery_percent(webserver.BATTERY_MV_EMPTY) == 0

    def test_percent_linear_midpoint(self):
        assert webserver._battery_percent(3750) == 50

    def test_percent_clamped_above_full(self):
        assert webserver._battery_percent(4200) == 100

    def test_percent_clamped_below_empty(self):
        assert webserver._battery_percent(3200) == 0

    def test_percent_handles_none(self):
        assert webserver._battery_percent(None) is None
        assert webserver._battery_percent(0) is None

    def test_record_screen_call_stores_battery(self):
        db = {"serve_data": {}}
        with patch.object(webserver.flask_app, "_database", db):
            webserver.flask_app._record_screen_call("Foyer", "1.2.3.4", 3600, battery_mv=3950)
        entry = db["screens"]["Foyer"]
        assert entry["battery_mv"] == 3950
        assert entry["battery_percent"] == webserver._battery_percent(3950)
        assert entry["battery_seen_at"] is not None

    def test_record_screen_call_without_battery_omits_fields(self):
        db = {"serve_data": {}}
        with patch.object(webserver.flask_app, "_database", db):
            webserver.flask_app._record_screen_call("Old", "1.2.3.4", 3600)
        entry = db["screens"]["Old"]
        assert "battery_mv" not in entry
        assert "battery_percent" not in entry

    def test_serve_binary_captures_battery_header(self):
        webserver.app.config["TESTING"] = True
        pool = {"/images/a.jpg": {"hash": "abc"}}
        db = {"serve_data": {"a.jpg": {"show_index": 0, "last_request": None,
              "total_show_count": 0, "total_show_minutes": 0.0}}}
        with webserver.app.test_client() as client, \
             patch.object(webserver.flask_app, "_pool", pool), \
             patch.object(webserver.flask_app, "_database", db), \
             patch.object(webserver.flask_app, "_config", replace(webserver.DEFAULT_CONFIG)), \
             patch.object(webserver.flask_app, "_converting_count", 0), \
             patch.object(webserver.flask_app, "_last_served",
                         {"key": None, "name": None, "served_at": None}), \
             patch("webserver.flask_app._read_cached_binary", return_value=b"x" * 960000), \
             patch("webserver.serve_data._save_database"):
            resp = client.get("/hokku/screen/", headers={
                "X-Screen-Name": "Foyer",
                "X-Battery-mV": "3912",
            })
            assert resp.status_code == 200
            assert db["screens"]["Foyer"]["battery_mv"] == 3912
            assert db["screens"]["Foyer"]["battery_percent"] is not None

    def test_serve_binary_ignores_bogus_battery_header(self):
        webserver.app.config["TESTING"] = True
        with webserver.app.test_client() as client, \
             patch.object(webserver.flask_app, "_pool", {}), \
             patch.object(webserver.flask_app, "_database", {"serve_data": {}}), \
             patch.object(webserver.flask_app, "_config", replace(webserver.DEFAULT_CONFIG)), \
             patch.object(webserver.flask_app, "_converting_count", 0), \
             patch("webserver.serve_data._save_database"):
            resp = client.get("/hokku/screen/", headers={
                "X-Screen-Name": "Foyer",
                "X-Battery-mV": "not a number",
            })
            assert resp.status_code in (404, 503)


class TestFrameState:
    def test_parse_valid_json(self):
        state = webserver._parse_frame_state(
            '{"fw":"20260418","boot":3,"wake":"timer","bat_mv":4050,"chg":"charging"}')
        assert state["fw"] == "20260418"
        assert state["boot"] == 3
        assert state["wake"] == "timer"
        assert state["bat_mv"] == 4050
        assert state["chg"] == "charging"

    def test_parse_empty_returns_none(self):
        assert webserver._parse_frame_state(None) is None
        assert webserver._parse_frame_state("") is None

    def test_parse_malformed_json_returns_none(self):
        assert webserver._parse_frame_state("{broken") is None
        assert webserver._parse_frame_state("not json at all") is None

    def test_parse_non_object_returns_none(self):
        assert webserver._parse_frame_state("[1,2,3]") is None
        assert webserver._parse_frame_state("42") is None
        assert webserver._parse_frame_state('"hello"') is None

    def test_record_stores_whole_state_dict(self):
        db = {"serve_data": {}}
        state = {"fw": "abc", "boot": 7, "wake": "button", "new_future_key": "v2"}
        with patch.object(webserver.flask_app, "_database", db):
            webserver.flask_app._record_screen_call("Foo", "1.2.3.4", 60, frame_state=state)
        stored = db["screens"]["Foo"]["state"]
        assert stored["fw"] == "abc"
        assert stored["boot"] == 7
        assert stored["new_future_key"] == "v2"
        assert "seen_at" in stored

    def test_record_pulls_battery_from_state(self):
        db = {"serve_data": {}}
        state = {"fw": "abc", "bat_mv": 3950}
        with patch.object(webserver.flask_app, "_database", db):
            webserver.flask_app._record_screen_call("Foo", "1.2.3.4", 60,
                                          battery_mv=None, frame_state=state)
        entry = db["screens"]["Foo"]
        assert entry["battery_mv"] == 3950
        assert entry["battery_percent"] == webserver._battery_percent(3950)

    def test_record_state_includes_clk_drift(self):
        db = {"serve_data": {}}
        import time as _time
        frame_clk = int(_time.time()) + 15
        state = {"fw": "abc", "clk_now": frame_clk}
        with patch.object(webserver.flask_app, "_database", db):
            webserver.flask_app._record_screen_call("Foo", "1.2.3.4", 60, frame_state=state)
        drift = db["screens"]["Foo"]["state"]["clk_drift_s"]
        assert 10 <= drift <= 20

    def test_record_state_omits_drift_without_clk_now(self):
        db = {"serve_data": {}}
        state = {"fw": "abc"}
        with patch.object(webserver.flask_app, "_database", db):
            webserver.flask_app._record_screen_call("Foo", "1.2.3.4", 60, frame_state=state)
        assert "clk_drift_s" not in db["screens"]["Foo"]["state"]

    def test_serve_binary_captures_frame_state(self):
        webserver.app.config["TESTING"] = True
        pool = {"/images/a.jpg": {"hash": "abc"}}
        db = {"serve_data": {"a.jpg": {"show_index": 0, "last_request": None,
              "total_show_count": 0, "total_show_minutes": 0.0}}}
        header_json = ('{"fw":"20260418012020Z","boot":3,"wake":"timer",'
                       '"caller":"wake","bat_mv":4100,"chg":"charging",'
                       '"last_sleep":"deep_sleep","rssi":-57,"heap_kb":240,'
                       '"spurious":0,"cfg_ver":1,"clk_now":1776500000}')
        with webserver.app.test_client() as client, \
             patch.object(webserver.flask_app, "_pool", pool), \
             patch.object(webserver.flask_app, "_database", db), \
             patch.object(webserver.flask_app, "_config", replace(webserver.DEFAULT_CONFIG)), \
             patch.object(webserver.flask_app, "_converting_count", 0), \
             patch.object(webserver.flask_app, "_last_served",
                         {"key": None, "name": None, "served_at": None}), \
             patch("webserver.flask_app._read_cached_binary", return_value=b"x" * 960000), \
             patch("webserver.serve_data._save_database"):
            resp = client.get("/hokku/screen/", headers={
                "X-Screen-Name": "Foyer",
                "X-Frame-State": header_json,
            })
            assert resp.status_code == 200
            state = db["screens"]["Foyer"]["state"]
            assert state["fw"] == "20260418012020Z"
            assert state["wake"] == "timer"
            assert state["rssi"] == -57
            assert db["screens"]["Foyer"]["battery_mv"] == 4100

    def test_serve_binary_tolerates_bogus_frame_state(self):
        webserver.app.config["TESTING"] = True
        with webserver.app.test_client() as client, \
             patch.object(webserver.flask_app, "_pool", {}), \
             patch.object(webserver.flask_app, "_database", {"serve_data": {}}), \
             patch.object(webserver.flask_app, "_config", replace(webserver.DEFAULT_CONFIG)), \
             patch.object(webserver.flask_app, "_converting_count", 0), \
             patch("webserver.serve_data._save_database"):
            resp = client.get("/hokku/screen/", headers={
                "X-Screen-Name": "Foyer",
                "X-Frame-State": "not json {{{",
            })
            assert resp.status_code in (404, 503)
