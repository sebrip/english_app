# -*- coding: utf-8 -*-
"""
audit_css.py — Audit visuel responsive : capture CHAQUE écran de l'app dans
chaque format (ordi / tablette / smartphone / paysage) pour vérifier la mise
en page. Réutilise les scènes de make_demo.py + 2 écrans supplémentaires
(fin de partie, révision du carnet). Lecture seule côté serveur.

Sortie : demo_video/audit/<format>/<scene>.png
         + planches contact demo_video/audit/sheet-<format>-N.png
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
from make_demo import SCENES, BASE, activate  # noqa: E402

ROOT = Path(__file__).parent
AUDIT = ROOT / "audit"

VIEWPORTS = {
    # nom            (largeur, hauteur, scale)
    "desktop-1440": (1440, 900, 1),
    "tablet-768": (768, 1024, 1),
    "phone-390": (390, 844, 2),
}
# Écrans "appel" testés EN PLUS en paysage téléphone (media query dédiée).
LANDSCAPE = ("06-conversation", "10-lecon", "13-test-niveau")


# --- Écrans absents de la visite vidéo : fin de partie + révision du carnet ---
def sc_gameover(page):
    activate(page, "game-over")
    page.evaluate("""() => {
      document.getElementById('game-over-title').textContent = 'Bien joué ! 🎉';
      document.getElementById('go-score').textContent = '90';
      document.getElementById('go-best').textContent = '120';
      document.getElementById('go-acc').textContent = '75%';
      const ul = document.getElementById('go-recap');
      ul.innerHTML = '';
      [['✅', '« Break a leg! » → Bonne chance !'],
       ['✅', '« No big deal » → Pas grave'],
       ['❌', '« To hit the road » → Partir (vous aviez choisi « Frapper la route »)'],
       ['✅', '« Piece of cake » → Facile comme tout']].forEach(([ok, txt]) => {
        const li = document.createElement('li');
        li.textContent = ok + ' ' + txt;
        ul.appendChild(li);
      });
    }""")
    page.wait_for_timeout(300)


def sc_review(page):
    activate(page, "review")
    page.evaluate("""() => {
      document.getElementById('rv-qtype').textContent = 'Expression';
      document.getElementById('rv-box').textContent = 'Boîte 2/5';
      document.getElementById('rv-count').textContent = '3 / 5';
      document.getElementById('rv-prompt').textContent = '« To hit the road » — qu\\'est-ce que ça veut dire ?';
      document.getElementById('rv-feedback').innerHTML = '';
      const ce = document.getElementById('rv-choices');
      ce.innerHTML = '';
      ['Partir, se mettre en route', 'Frapper la route', 'Faire du stop', 'Réparer la chaussée'].forEach(c => {
        const b = document.createElement('button');
        b.className = 'game-choice';
        b.textContent = c;
        ce.appendChild(b);
      });
    }""")
    page.wait_for_timeout(300)


ALL_SCENES = [(n, fn) for n, _cap, fn in SCENES] + [
    ("21-fin-de-partie", sc_gameover),
    ("22-revision-carnet", sc_review),
]


def capture_all():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for vp_name, (w, h, scale) in VIEWPORTS.items():
            out = AUDIT / vp_name
            out.mkdir(parents=True, exist_ok=True)
            page = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=scale)
            page.goto(BASE, wait_until="networkidle")
            page.wait_for_selector("#character-cards .char-card", state="attached")
            page.wait_for_timeout(600)
            for name, fn in ALL_SCENES:
                fn(page)
                page.screenshot(path=str(out / f"{name}.png"), full_page=True)
            page.close()
            print("  ok:", vp_name, f"({len(ALL_SCENES)} ecrans)")

        # Paysage téléphone : uniquement les écrans d'appel (media query dédiée).
        out = AUDIT / "phone-paysage-844x390"
        out.mkdir(parents=True, exist_ok=True)
        page = browser.new_page(viewport={"width": 844, "height": 390}, device_scale_factor=2)
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_selector("#character-cards .char-card", state="attached")
        page.wait_for_timeout(600)
        scenes = {n: fn for n, fn in ALL_SCENES}
        for name in LANDSCAPE:
            scenes[name](page)
            # Pour un écran d'appel, c'est la zone VISIBLE qui compte (pas la pleine page).
            page.screenshot(path=str(out / f"{name}.png"), full_page=False)
        page.close()
        print("  ok: phone-paysage-844x390 (%d ecrans)" % len(LANDSCAPE))
        browser.close()


def contact_sheets(vp_name, cols=4, thumb_w=460):
    """Planche contact d'un format : toutes les captures en grille, étiquetées."""
    folder = AUDIT / vp_name
    files = sorted(folder.glob("*.png"))
    font = ImageFont.truetype("C:/Windows/Fonts/seguisb.ttf", 22)
    thumbs = []
    for f in files:
        im = Image.open(f)
        ratio = thumb_w / im.width
        th = im.resize((thumb_w, int(im.height * ratio)), Image.LANCZOS)
        thumbs.append((f.stem, th))
    max_h = 720  # on tronque les pages très longues sur la planche (le détail est dans le PNG)
    cell_h = min(max(t.height for _n, t in thumbs), max_h) + 40
    rows = (len(thumbs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (thumb_w + 16) + 16, rows * cell_h + 16), (18, 20, 34))
    d = ImageDraw.Draw(sheet)
    for i, (name, th) in enumerate(thumbs):
        x = 16 + (i % cols) * (thumb_w + 16)
        y = 16 + (i // cols) * cell_h
        d.text((x, y), name, font=font, fill=(255, 200, 120))
        sheet.paste(th.crop((0, 0, thumb_w, min(th.height, max_h))), (x, y + 32))
    out = AUDIT / f"sheet-{vp_name}.png"
    sheet.save(out)
    print("  planche:", out.name)


if __name__ == "__main__":
    print("[1/2] Captures responsive...")
    capture_all()
    print("[2/2] Planches contact...")
    for vp in VIEWPORTS:
        contact_sheets(vp)
    print("Termine.")
