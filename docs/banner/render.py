"""Render docs/banner/banner.html to docs/banner.png (2x, for sharp text on GitHub)."""

from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 420}, device_scale_factor=2)
    page.goto((HERE / "banner.html").as_uri(), wait_until="networkidle")
    page.evaluate("document.fonts.ready")
    page.screenshot(path=str(HERE.parent / "banner.png"))
    browser.close()
print("Wrote", HERE.parent / "banner.png")
