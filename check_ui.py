#!/usr/bin/env python3
import asyncio
from playwright.async_api import async_playwright

async def check_ui():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()

        try:
            print("Opening page...")
            await page.goto("http://127.0.0.1:8080/hokku/ui", timeout=15000)
            await page.wait_for_load_state("networkidle", timeout=10000)

            # Get header status
            header = await page.locator("text=/Images:/").text_content()
            print(f"Header status: {header}")

            # Get dithering badge status
            dithering_text = await page.locator("button, span").filter(has_text="Dithering").first.text_content(timeout=1000)
            print(f"Dithering badge: {dithering_text}")

            # Count image cards
            image_count = await page.locator("div[class*='card'], article").count()
            print(f"Image cards on page: {image_count}")

            # Get status of first few images
            images = await page.locator("h3, h4").all()
            print(f"\nFirst 5 images on page:")
            for i, img in enumerate(images[:5]):
                name = await img.text_content()
                print(f"  {i+1}. {name}")

            print("\nWaiting 5 seconds to see if page updates...")
            await page.wait_for_timeout(5000)

            # Check again after waiting
            header_after = await page.locator("text=/Images:/").text_content()
            print(f"\nHeader after 5s: {header_after}")

        except Exception as e:
            print(f"Error: {e}")
        finally:
            await browser.close()

asyncio.run(check_ui())
