# -*- coding: utf-8 -*-
"""Capture rapide de l'écran course-setup en 390x844 (iPhone 12 Pro) pour
vérifier les ajustements CSS mobile. Lecture seule côté serveur."""
from pathlib import Path
from playwright.sync_api import sync_playwright

OUT = Path(__file__).parent / "check_mobile.png"

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
    page.goto("http://127.0.0.1:8000/", wait_until="networkidle")
    page.click("#mode-course")
    page.wait_for_selector("#course-character-cards .char-card")
    page.click("#course-character-cards .char-card")
    page.wait_for_selector("#course-level-pills .pill")
    page.wait_for_timeout(600)
    page.screenshot(path=str(OUT), full_page=True)
    browser.close()
print("OK:", OUT)
