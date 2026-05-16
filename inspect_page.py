#!/usr/bin/env python3
import asyncio
from playwright.async_api import async_playwright
import json

async def inspect_page():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()

        try:
            print("Navigating to http://127.0.0.1:8080/hokku/ui...")
            await page.goto("http://127.0.0.1:8080/hokku/ui", timeout=10000)
            await page.wait_for_load_state("networkidle", timeout=5000)

            # Get status
            print("\n=== Checking API Status ===")
            status_response = await page.request.get("http://127.0.0.1:8080/hokku/api/status")
            status_data = await status_response.json()

            print(f"Images ready: {status_data.get('images_ready', 'N/A')}")
            print(f"Dithering in progress: {status_data.get('dithering_in_progress', 'N/A')}")
            print(f"Failed count: {status_data.get('failed_count', 'N/A')}")

            pending = status_data.get('pending_conversions', [])
            print(f"\nPending conversions ({len(pending)}):")
            for img in pending[:3]:
                print(f"  - {img['name']}: status={img.get('convert_status')}, error={img.get('convert_error')}")

            print("\n=== Page Content ===")
            # Get the main status text from the page
            status_text = await page.locator("text=Images:").text_content()
            print(f"Status text: {status_text}")

            # Check if any images are showing dithering badge
            dithering_badges = await page.locator("text=Dithering").count()
            print(f"Images with 'Dithering' badge: {dithering_badges}")

            # Get the dithering progress indicator
            progress = await page.locator("text=Dithering").first.text_content()
            print(f"Dithering progress: {progress}")

        except Exception as e:
            print(f"Error: {e}")
        finally:
            await browser.close()

asyncio.run(inspect_page())
