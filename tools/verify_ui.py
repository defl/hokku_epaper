"""Kill any server on :8080, start a fresh one from current source, open Playwright."""
import asyncio
import socket
import subprocess
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent
WEBSERVER_DIR = ROOT / "webserver"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
CONFIG = ROOT / "test_server" / "config.json"


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def kill_port_8080() -> None:
    """Kill whatever owns :8080 using netstat + taskkill."""
    out = subprocess.run(
        ["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True
    ).stdout
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(":8080") and parts[3] == "LISTENING":
            pid = parts[4]
            print(f"  Killing PID {pid} on :8080 ...")
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
            return


async def main():
    if port_in_use(8080):
        print("Port 8080 is busy, killing the existing server...")
        kill_port_8080()
        for _ in range(20):
            if not port_in_use(8080):
                break
            time.sleep(0.2)
        else:
            sys.exit("Could not free port 8080.")

    print("Starting fresh hokku_server with latest source...")
    server = subprocess.Popen(
        [str(PYTHON), "-m", "hokku_server", str(CONFIG)],
        cwd=str(WEBSERVER_DIR),
    )

    # Wait for server to accept connections
    for _ in range(50):
        if port_in_use(8080):
            break
        time.sleep(0.2)
    else:
        server.terminate()
        sys.exit("Server did not start within 10 s.")
    time.sleep(1)  # let Flask finish initialising

    print("Opening Playwright browser to http://localhost:8080/hokku/ui ...")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context(viewport={"width": 1280, "height": 1400})
            page = await context.new_page()
            # Cache-busting query string just in case
            await page.goto(
                f"http://localhost:8080/hokku/ui?t={int(time.time())}",
                wait_until="networkidle",
            )

            # Expand the Advanced section so the detect toggles + pipeline panels
            # are visible.
            await page.click("#cfg-advanced-toggle")
            await page.wait_for_timeout(400)

            # Enable both detect toggles so the pipeline panels are visible too.
            await page.check("#bw-detect-enabled")
            await page.check("#face-detect-enabled")
            await page.wait_for_timeout(300)

            # Scroll to the Detect B&W photos row so the user sees the layout.
            await page.locator("#bw-detect-enabled").scroll_into_view_if_needed()

            # Save a full-page screenshot for verification.
            screenshot_path = ROOT / "hokku_ui_verified.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            print(f"Screenshot saved: {screenshot_path}")

            print()
            print("=" * 60)
            print("Browser is OPEN at http://localhost:8080/hokku/ui")
            print("Advanced is expanded and both detect toggles are ON.")
            print("This script will keep the browser alive for 1 hour.")
            print("Kill the python process when you are done.")
            print("=" * 60)
            print()
            await asyncio.sleep(3600)
            await browser.close()
    finally:
        print("Stopping server...")
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    asyncio.run(main())
