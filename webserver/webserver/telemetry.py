"""Persist screen device telemetry into the shared database dict."""
import time
from datetime import datetime, timedelta

from webserver.screen_headers import battery_percent, parse_battery_header


class ScreenTelemetry:
    def record(
        self,
        database: dict,
        screen_name: str,
        screen_ip: str,
        sleep_seconds,
        *,
        served_name=None,
        battery_mv=None,
        frame_state=None,
    ):
        if "screens" not in database:
            database["screens"] = {}
        screens = database["screens"]
        if screen_name not in screens:
            screens[screen_name] = {"ip": screen_ip, "request_count": 0, "last_seen": None}
        now = datetime.now()
        screens[screen_name]["ip"] = screen_ip
        screens[screen_name]["request_count"] += 1
        screens[screen_name]["last_seen"] = now.isoformat(timespec="seconds")
        screens[screen_name]["last_sleep_seconds"] = int(sleep_seconds)
        screens[screen_name]["next_update_at"] = (
            now + timedelta(seconds=int(sleep_seconds))
        ).isoformat(timespec="seconds")
        if served_name is not None:
            screens[screen_name]["last_served"] = served_name

        if frame_state and isinstance(frame_state.get("bat_mv"), (int, float)):
            fs_mv = parse_battery_header(str(int(frame_state["bat_mv"])))
            if fs_mv is not None:
                battery_mv = fs_mv

        if battery_mv is not None and battery_mv > 0:
            screens[screen_name]["battery_mv"] = int(battery_mv)
            screens[screen_name]["battery_percent"] = battery_percent(battery_mv)
            screens[screen_name]["battery_seen_at"] = now.isoformat(timespec="seconds")

        if frame_state:
            state_with_meta = dict(frame_state)
            clk_now = frame_state.get("clk_now")
            if isinstance(clk_now, (int, float)) and clk_now > 0:
                state_with_meta["clk_drift_s"] = int(clk_now - time.time())
            state_with_meta["seen_at"] = now.isoformat(timespec="seconds")
            screens[screen_name]["state"] = state_with_meta
