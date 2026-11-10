"""Render docs/banner/thumbnail.html to docs/thumbnail.png (1200x800, 3:2, for Devpost)."""

from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1200, "height": 800}, device_scale_factor=1)
    page.goto((HERE / "thumbnail.html").as_uri(), wait_until="networkidle")
    page.evaluate("document.fonts.ready")
    page.screenshot(path=str(HERE.parent / "thumbnail.png"))
    browser.close()
print("Wrote", HERE.parent / "thumbnail.png")
