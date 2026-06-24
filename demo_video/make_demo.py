# -*- coding: utf-8 -*-
"""
make_demo.py — Génère la vidéo diaporama de visite guidée d'English SpeakApp.

Pipeline :
  1. CAPTURE  : Playwright ouvre l'app (serveur local :8000), navigue écran par
                écran (clics réels quand possible, mise en scène JS pour les
                écrans qui exigent une vraie conversation vocale) et capture.
  2. COMPOSE  : Pillow place chaque capture sur un canevas 1920x1080 aux
                couleurs de l'app + bandeau de sous-titre français en bas.
  3. ASSEMBLE : ffmpeg (via imageio-ffmpeg) concatène en MP4 H.264.
                Un fichier .srt est aussi généré (mêmes textes, mêmes timecodes).

NOTE : ce script ne fait QUE des lectures côté serveur (GET) — il ne touche
jamais à data/progress.json.
"""

import math
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
import imageio_ffmpeg
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent
RAW = ROOT / "slides_raw"      # captures brutes (tailles variables)
FINAL = ROOT / "slides_final"  # canevas 1920x1080 sous-titrés
RAW.mkdir(exist_ok=True)
FINAL.mkdir(exist_ok=True)

BASE = os.environ.get("DEMO_BASE", "http://127.0.0.1:8000/")
W, H = 1920, 1080
IMG_ZONE_H = 920          # hauteur réservée à la capture (le reste = sous-titre)
BG = (10, 12, 24)          # #0a0c18, le fond de l'app
TITLE_DUR = 4.0
SCENE_DUR = 5.0
END_DUR = 4.0

FONTS = "C:/Windows/Fonts"
F_TITLE = ImageFont.truetype(f"{FONTS}/georgiab.ttf", 110)
F_SUB = ImageFont.truetype(f"{FONTS}/segoeui.ttf", 46)
F_CAPTION = ImageFont.truetype(f"{FONTS}/seguisb.ttf", 42)

# ---------------------------------------------------------------------------
# Aide JS : activer un écran à la main (réplique de show() côté app.js)
# ---------------------------------------------------------------------------
JS_ACTIVATE = """(name) => {
  document.querySelectorAll('section.screen').forEach(s => s.classList.remove('active'));
  document.getElementById('screen-' + name).classList.add('active');
  window.scrollTo(0, 0);
}"""


def activate(page, name):
    page.evaluate(JS_ACTIVATE, name)


def bubbles_js(container_id, lines):
    """JS qui injecte des bulles de dialogue dans un transcript."""
    items = ",".join(
        "[%r,%r]" % (role, text) for role, text in lines
    )
    return f"""() => {{
      const t = document.getElementById('{container_id}');
      t.innerHTML = '';
      for (const [role, text] of [{items}]) {{
        const b = document.createElement('div');
        b.className = 'bubble ' + role;
        b.textContent = text;
        t.appendChild(b);
      }}
    }}"""


# ---------------------------------------------------------------------------
# LES SCÈNES — (id, sous-titre français, fonction de mise en place)
# ---------------------------------------------------------------------------

def sc_menu(page):
    activate(page, "menu")
    page.wait_for_timeout(800)


def sc_profile(page):
    activate(page, "menu")
    page.click("#gami-banner")
    page.wait_for_timeout(1200)


def sc_free_home(page):
    activate(page, "menu")
    page.click("#mode-free")
    page.wait_for_selector("#character-cards .char-card")
    page.wait_for_timeout(400)


def sc_setup(page):
    page.click("#character-cards .char-card")
    page.wait_for_selector("#decor-cards .decor-card")
    pills = page.locator("#level-pills .pill")
    if pills.count() > 1:
        pills.nth(1).click()
    page.click("#decor-cards .decor-card")
    page.wait_for_timeout(400)


def sc_debutant(page):
    """Sophie, la maîtresse francophone pour grands débutants (carte verte, niveau verrouillé)."""
    activate(page, "menu")
    page.click("#mode-free")
    page.wait_for_selector("#character-cards .char-card")
    page.evaluate("""() => {
      const cards = [...document.querySelectorAll('#character-cards .char-card')];
      const s = cards.find(c => c.querySelector('.char-name')?.textContent.trim() === 'Sophie');
      if (s) s.click();
    }""")
    page.wait_for_selector("#decor-cards .decor-card")
    page.wait_for_timeout(500)


def sc_call(page):
    activate(page, "call")
    page.evaluate("""() => {
      const scr = document.getElementById('screen-call');
      scr.style.backgroundImage =
        'linear-gradient(rgba(8,10,20,0.78), rgba(8,10,20,0.9)), url("decors/restaurant.png")';
      document.getElementById('call-avatar').src = 'avatars/zoe.png';
      document.getElementById('call-charname').textContent = 'Zoe';
      document.getElementById('call-place').textContent = '🍽️ Au restaurant';
      document.getElementById('call-status').textContent = 'En conversation — à vous de parler !';
    }""")
    page.evaluate(bubbles_js("transcript", [
        ("ai", "Hi! Welcome! Here is the menu. What would you like to order today?"),
        ("user", "Hello! I would like the grilled salmon, please."),
        ("ai", "Great choice! And would you like something to drink with that?"),
    ]))
    page.wait_for_timeout(300)


def sc_summary(page):
    activate(page, "summary")
    page.evaluate("""() => {
      document.getElementById('summary-loading').hidden = true;
      document.getElementById('summary-content').hidden = false;
      const C = 2 * Math.PI * 52;
      const arc = document.getElementById('gauge-arc');
      arc.style.strokeDasharray = C;
      arc.style.strokeDashoffset = C * (1 - 8 / 10);
      arc.style.stroke = 'hsl(96, 75%, 55%)';
      document.getElementById('score-num').textContent = '8';
      document.getElementById('score-justif').textContent =
        "Très bon échange : des phrases complètes et une vraie aisance à l'oral.";
      document.getElementById('summary-text').textContent =
        "Vous avez commandé un repas au restaurant, posé des questions sur le menu " +
        "et tenu une conversation naturelle avec Zoe du début à la fin.";
      const fill = (id, items) => {
        const ul = document.getElementById(id);
        ul.innerHTML = '';
        items.forEach(x => { const li = document.createElement('li'); li.textContent = x; ul.appendChild(li); });
      };
      fill('summary-strengths', [
        "Vocabulaire du restaurant bien maîtrisé",
        "Questions naturelles et polies",
        "Bonne réactivité dans l'échange",
      ]);
      fill('summary-improvements', [
        "Préférer « I would like » à « I want »",
        "Les quantités : « a glass of », « a bottle of »",
      ]);
    }""")
    page.wait_for_timeout(600)


def sc_gift(page):
    """Le cadeau 'invité surprise' sur l'accueil (débloqué avec une note >= 9)."""
    activate(page, "menu")
    page.evaluate("() => { document.getElementById('menu-gift').hidden = false; }")
    page.wait_for_timeout(400)


def sc_raj(page):
    """La bannière de Raj, le correspondant indien, sur l'accueil Conversation libre."""
    activate(page, "menu")
    page.click("#mode-free")
    page.wait_for_selector("#character-cards .char-card")
    page.evaluate("""async () => {
      // /api/bonus (GET, lecture seule) renvoie toujours la fiche de Raj.
      const c = (await (await fetch('/api/bonus')).json()).character;
      const banner = document.getElementById('bonus-guest');
      banner.innerHTML = `
        <img src="${c.avatar}" alt="${c.name}">
        <div class="bg-info">
          <div class="bg-tag">✨ Invité surprise débloqué</div>
          <div class="bg-name">${c.name} — ${c.title}</div>
          <div class="bg-desc">${c.tagline}</div>
        </div>
        <button id="bonus-start">Parler avec ${c.name} →</button>`;
      banner.hidden = false;
      window.scrollTo(0, 0);
    }""")
    page.wait_for_timeout(400)


def sc_course_home(page):
    activate(page, "menu")
    page.click("#mode-course")
    page.wait_for_selector("#course-character-cards .char-card")
    page.wait_for_timeout(600)


def sc_course_setup(page):
    page.click("#course-character-cards .char-card")
    page.wait_for_selector("#course-level-pills .pill")
    page.wait_for_timeout(600)


def sc_lesson(page):
    activate(page, "lesson")
    page.evaluate("""() => {
      document.getElementById('screen-lesson').style.backgroundImage =
        'linear-gradient(rgba(8,10,20,0.78), rgba(8,10,20,0.9)), url("decors/classroom.png")';
      document.getElementById('lesson-avatar').src = 'avatars/john.png';
      document.getElementById('lesson-charname').textContent = 'John';
      document.getElementById('lesson-level').textContent = 'B2 · Avancé';
      document.getElementById('lesson-timer').textContent = '08:10 / 10:00';
      document.getElementById('lesson-status').textContent = 'Leçon en cours : les nuances du hedging';
      document.getElementById('lesson-finish-btn').hidden = false;
    }""")
    page.evaluate(bubbles_js("lesson-transcript", [
        ("ai", "Instead of \"You are wrong\", try softening it: \"I'm not sure that's quite right.\""),
        ("user", "I see. So I could say: \"It seems to me that the answer might be different.\""),
        ("ai", "Exactly! That sounds much more natural. Let's try one more."),
    ]))
    page.wait_for_timeout(300)


def sc_lesson_summary(page):
    activate(page, "lesson-summary")
    page.evaluate("""() => {
      document.getElementById('lesson-summary-loading').hidden = true;
      document.getElementById('lesson-summary-content').hidden = false;
      document.getElementById('lesson-verdict-title').textContent = 'Leçon validée ! 🎉';
      const badge = document.getElementById('lesson-verdict-badge');
      badge.className = 'verdict-badge pass';
      badge.textContent = '✅ Validée';
      const C = 2 * Math.PI * 52;
      const arc = document.getElementById('lesson-gauge-arc');
      arc.style.strokeDasharray = C;
      arc.style.strokeDashoffset = C * (1 - 8 / 10);
      arc.style.stroke = 'hsl(96, 75%, 55%)';
      document.getElementById('lesson-score-num').textContent = '8';
      document.getElementById('lesson-score-justif').textContent =
        "Objectif atteint : vous adoucissez vos phrases comme un vrai anglophone.";
      document.getElementById('lesson-summary-text').textContent =
        "Leçon sur le hedging : exprimer un désaccord poliment avec « it seems », " +
        "« might » et « I'm not sure that... ». Exercices réussis avec John.";
      const fill = (id, items) => {
        const ul = document.getElementById(id);
        ul.innerHTML = '';
        items.forEach(x => { const li = document.createElement('li'); li.textContent = x; ul.appendChild(li); });
      };
      fill('lesson-acquired', [
        "« It seems to me that... » utilisé à bon escient",
        "Modaux de prudence : might, could",
      ]);
      fill('lesson-toreview', [
        "« I'm afraid » en début de désaccord",
      ]);
      const nick = document.getElementById('lesson-nickname');
      nick.hidden = false;
      document.getElementById('lesson-nick-name').textContent = 'Chef Bricoleur';
      document.getElementById('lesson-nick-reason').textContent =
        "Parce que tu bricoles tes phrases avec ce que tu as sous la main — et ça marche !";
      const vc = document.getElementById('lesson-vocab');
      vc.hidden = false;
      const list = document.getElementById('lesson-vocab-list');
      list.innerHTML = '';
      [['to hedge', 'nuancer ses propos'], ['I am afraid...', 'je crains que...'],
       ['it seems', 'il semble'], ['slightly', 'légèrement']].forEach(([en, fr]) => {
        const chip = document.createElement('div');
        chip.className = 'vocab-chip';
        chip.textContent = en + ' — ' + fr;
        list.appendChild(chip);
      });
    }""")
    page.wait_for_timeout(600)


def sc_progress(page):
    activate(page, "course-home")
    page.click("#course-progress-btn")
    page.wait_for_timeout(1500)


def sc_assessment(page):
    activate(page, "assessment")
    page.evaluate("""() => {
      document.getElementById('screen-assessment').style.backgroundImage =
        'linear-gradient(rgba(8,10,20,0.78), rgba(8,10,20,0.9)), url("decors/interview.png")';
      document.getElementById('assess-avatar').src = 'avatars/david.png';
      document.getElementById('assess-charname').textContent = 'David';
      document.getElementById('assess-timer').textContent = '03:45 / 10:00';
      document.getElementById('assess-status').textContent = "L'examen suit son cours — répondez naturellement";
    }""")
    page.evaluate(bubbles_js("assess-transcript", [
        ("ai", "Tell me about your last holiday. Where did you go and what did you do?"),
        ("user", "Last summer I went to Spain with my family. We visited Barcelona and we ate a lot of tapas!"),
        ("ai", "Lovely! And if you could travel anywhere next year, where would you go?"),
    ]))
    page.wait_for_timeout(300)


def sc_assessment_summary(page):
    activate(page, "assessment-summary")
    page.evaluate("""() => {
      document.getElementById('assess-summary-loading').hidden = true;
      document.getElementById('assess-summary-content').hidden = false;
      const order = ['beginner', 'A1', 'A2', 'B1', 'B2', 'C1', 'C2'];
      const labels = { beginner: 'Déb.', A1: 'A1', A2: 'A2', B1: 'B1', B2: 'B2', C1: 'C1', C2: 'C2' };
      const scale = document.getElementById('level-scale');
      scale.innerHTML = '';
      const idx = order.indexOf('B1');
      order.forEach((lvl, i) => {
        const step = document.createElement('div');
        step.className = 'ls-step' + (i === idx ? ' current' : '') + (i <= idx ? ' reached' : '');
        step.innerHTML = '<span class="ls-dot"></span><span class="ls-name">' + labels[lvl] + '</span>';
        scale.appendChild(step);
      });
      document.getElementById('assess-level-label').textContent = 'B1 · Intermédiaire';
      document.getElementById('assess-summary-text').textContent =
        "Vous comprenez les questions du quotidien et tenez une conversation sur des sujets familiers : " +
        "les voyages, la famille, le travail.";
      document.getElementById('assess-justif').textContent =
        "Le passé est bien maîtrisé ; le conditionnel reste à consolider.";
      const fill = (id, items) => {
        const ul = document.getElementById(id);
        ul.innerHTML = '';
        items.forEach(x => { const li = document.createElement('li'); li.textContent = x; ul.appendChild(li); });
      };
      fill('assess-strengths', ["Fluidité sur les sujets du quotidien", "Bon réflexe de reformulation"]);
      fill('assess-improvements', ["Le conditionnel (would, could)", "Les questions indirectes"]);
      const reco = document.getElementById('assess-reco-box');
      reco.innerHTML = '🎓 Pour démarrer tes cours, je te conseille le niveau <b>B1 · Intermédiaire</b> — mais tu restes libre de choisir.';
      reco.hidden = false;
    }""")
    page.wait_for_timeout(400)


def sc_games_hub(page):
    activate(page, "menu")
    page.click("#mode-game")
    page.wait_for_timeout(500)


def sc_game_home(page):
    page.click("#hub-quiz")
    page.wait_for_selector("#game-level-pills .pill")
    page.wait_for_timeout(500)


def sc_game(page):
    activate(page, "game")
    page.evaluate("""() => {
      document.getElementById('game-score').textContent = '70';
      document.getElementById('game-combo').textContent = 'x3 🔥';
      document.getElementById('game-lives').textContent = '❤️❤️🤍';
      document.getElementById('game-qtype').textContent = 'Expression';
      document.getElementById('game-progress').textContent = 'Question 5/10';
      document.getElementById('game-prompt').textContent = '« Break a leg! » — qu\\'est-ce que ça veut dire ?';
      document.getElementById('game-timer-bar').style.width = '62%';
      document.getElementById('game-feedback').innerHTML = '';
      const ce = document.getElementById('game-choices');
      ce.innerHTML = '';
      ['Bonne chance !', 'Casse-toi !', 'Attention à la marche !', 'Quel malheur !'].forEach(c => {
        const b = document.createElement('button');
        b.className = 'game-choice';
        b.textContent = c;
        ce.appendChild(b);
      });
    }""")
    page.wait_for_timeout(300)


def sc_wordrush_home(page):
    activate(page, "games-hub")
    page.click("#hub-rush")
    page.wait_for_selector("#wr-theme-grid > *")
    page.click("#wr-theme-grid > *:first-child")
    page.wait_for_timeout(400)


def sc_wordrush(page):
    activate(page, "wordrush")
    page.evaluate("""() => {
      document.getElementById('wr-score').textContent = '120';
      document.getElementById('wr-combo').textContent = 'x4 🔥';
      document.getElementById('wr-time').textContent = '0:42';
      document.getElementById('wr-timer-bar').style.width = '70%';
      document.getElementById('wr-theme-label').textContent = '🍳 Cuisine';
      document.getElementById('wr-count').textContent = 'Mot 9';
      document.getElementById('wr-prompt').textContent = 'une casserole';
      document.getElementById('wr-feedback').innerHTML = '';
      const ce = document.getElementById('wr-choices');
      ce.innerHTML = '';
      ['a saucepan', 'a frying pan', 'a kettle', 'a ladle'].forEach(c => {
        const b = document.createElement('button');
        b.className = 'game-choice';
        b.textContent = c;
        ce.appendChild(b);
      });
    }""")
    page.wait_for_timeout(300)


def sc_errorbook(page):
    activate(page, "games-hub")
    page.click("#hub-errors")
    page.wait_for_timeout(1200)


SCENES = [
    ("02-menu", "L'écran d'accueil propose trois modes : conversation libre, cours d'anglais et mini-jeux.", sc_menu),
    ("03-profil", "Le profil : niveau, points d'expérience, série de jours et badges à débloquer.", sc_profile),
    ("04-partenaires", "En conversation libre, choisissez votre partenaire : six personnages, chacun sa voix et son caractère — du professeur de Boston au jeune gamer californien.", sc_free_home),
    ("05-reglages", "Réglez votre niveau, puis choisissez le décor : restaurant, aéroport, entretien... et désormais l'école ou le supermarché.", sc_setup),
    ("05b-debutant", "Nouveau : Sophie, la maîtresse d'école francophone pour les grands débutants — elle explique en français et ne propose que le niveau « Débutant ».", sc_debutant),
    ("06-conversation", "Parlez en temps réel, à la voix, avec sous-titres en direct — comme un vrai appel.", sc_call),
    ("07-bilan", "À la fin, un bilan complet : note sur 10, points forts et axes d'amélioration.", sc_summary),
    ("07b-cadeau", "Décrochez 9/10 ou plus, et un cadeau apparaît sur l'accueil : un invité surprise vous attend !", sc_gift),
    ("07c-raj", "C'est Raj, le correspondant indien ! Une conversation bonus au pub — il disparaît après l'avoir rencontré.", sc_raj),
    ("08-cours", "Le mode Cours : des professeurs spécialisés, repérables à leur couleur — débutants, vocabulaire, examinateur — qui suivent votre progression.", sc_course_home),
    ("09-cours-reglages", "Choisissez le niveau et la durée : l'application reprend là où vous vous étiez arrêté.", sc_course_setup),
    ("10-lecon", "Pendant la leçon, un chrono guide la séance ; vous validez quand l'objectif est atteint.", sc_lesson),
    ("11-lecon-bilan", "Chaque leçon est évaluée : note, acquis, vocabulaire du jour... et parfois un nouveau surnom.", sc_lesson_summary),
    ("12-progression", "Le tableau de bord résume tout : leçons validées, note moyenne et progression par cours.", sc_progress),
    ("13-test-niveau", "Le test de niveau : 10 minutes avec David l'examinateur pour situer votre anglais.", sc_assessment),
    ("14-test-resultat", "Votre niveau s'affiche sur l'échelle officielle, du débutant au C2, avec un conseil pour la suite.", sc_assessment_summary),
    ("15-jeux", "Côté détente : trois mini-jeux pour réviser en s'amusant.", sc_games_hub),
    ("16-quiz", "Le quiz d'expressions américaines : combos, vies et explication à chaque question.", sc_game_home),
    ("17-quiz-jeu", "Répondez avant la fin du chrono : chaque bonne réponse fait grimper le combo !", sc_game),
    ("18-wordrush", "Word Rush : 60 secondes de vocabulaire par thème — les combos rajoutent du temps.", sc_wordrush_home),
    ("19-wordrush-jeu", "Trouvez la bonne traduction le plus vite possible !", sc_wordrush),
    ("20-carnet", "Le carnet d'erreurs fait revenir vos fautes au bon moment : c'est la répétition espacée.", sc_errorbook),
]

TITLE_CAPTION = "Bienvenue dans English SpeakApp : l'application pour parler anglais... pour de vrai !"
END_CAPTION = "À vous de jouer ! English SpeakApp — parlez anglais, pour de vrai."


# ---------------------------------------------------------------------------
# 1) CAPTURE
# ---------------------------------------------------------------------------
def capture(only=None):
    """only : liste de sous-chaînes pour ne recapturer que certaines scènes."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 720}, device_scale_factor=1.5)
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_selector("#character-cards .char-card", state="attached")
        page.wait_for_timeout(800)
        for name, _caption, fn in SCENES:
            if only and not any(s in name for s in only):
                continue
            print("  capture:", name)
            fn(page)
            page.screenshot(path=str(RAW / f"{name}.png"), full_page=True)
        browser.close()


# ---------------------------------------------------------------------------
# 2) COMPOSE (canevas 1920x1080 + bandeau sous-titre)
# ---------------------------------------------------------------------------
def orbs_background():
    """Fond aux couleurs de l'app avec halos flous (comme les .bg-orbs)."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.ellipse((-300, -300, 700, 700), fill=(46, 36, 110))
    d.ellipse((1400, 500, 2400, 1500), fill=(110, 30, 80))
    d.ellipse((700, 800, 1500, 1600), fill=(20, 60, 90))
    img = img.filter(ImageFilter.GaussianBlur(180))
    return img


def wrap_text(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w_ in words:
        test = (cur + " " + w_).strip()
        if draw.textbbox((0, 0), test, font=font)[2] <= max_w:
            cur = test
        else:
            lines.append(cur)
            cur = w_
    if cur:
        lines.append(cur)
    return lines


def draw_caption(img, text):
    """Bandeau de sous-titre centré dans la zone basse (y >= IMG_ZONE_H)."""
    d = ImageDraw.Draw(img, "RGBA")
    lines = wrap_text(d, text, F_CAPTION, 1560)
    line_h = 58
    block_h = len(lines) * line_h
    y0 = IMG_ZONE_H + (H - IMG_ZONE_H - block_h) // 2
    for i, line in enumerate(lines):
        bbox = d.textbbox((0, 0), line, font=F_CAPTION)
        x = (W - (bbox[2] - bbox[0])) // 2
        y = y0 + i * line_h
        d.text((x + 2, y + 2), line, font=F_CAPTION, fill=(0, 0, 0, 180))
        d.text((x, y), line, font=F_CAPTION, fill=(240, 240, 250, 255))


def compose_slide(raw_path, caption, out_path, bg):
    img = bg.copy()
    shot = Image.open(raw_path)
    # Échelle pour tenir dans 1920 x IMG_ZONE_H (zone image)
    scale = min(W / shot.width, IMG_ZONE_H / shot.height)
    nw, nh = int(shot.width * scale), int(shot.height * scale)
    shot = shot.resize((nw, nh), Image.LANCZOS)
    img.paste(shot, ((W - nw) // 2, (IMG_ZONE_H - nh) // 2))
    draw_caption(img, caption)
    img.save(out_path)


def make_card(title, subtitle, caption, out_path, bg):
    img = bg.copy()
    d = ImageDraw.Draw(img)
    bb = d.textbbox((0, 0), title, font=F_TITLE)
    d.text(((W - bb[2]) // 2, 360), title, font=F_TITLE, fill=(245, 245, 252))
    bb2 = d.textbbox((0, 0), subtitle, font=F_SUB)
    d.text(((W - bb2[2]) // 2, 530), subtitle, font=F_SUB, fill=(255, 122, 89))
    draw_caption(img, caption)
    img.save(out_path)


def compose():
    bg = orbs_background()
    make_card("English SpeakApp", "Visite guidée de l'application", TITLE_CAPTION, FINAL / "01-titre.png", bg)
    for name, caption, _fn in SCENES:
        print("  compose:", name)
        compose_slide(RAW / f"{name}.png", caption, FINAL / f"{name}.png", bg)
    make_card("Merci !", "Parlez anglais. Pour de vrai.", END_CAPTION, FINAL / "99-fin.png", bg)


# ---------------------------------------------------------------------------
# 2b) MUSIQUE — petite boucle douce générée en numpy (pads + arpèges)
# ---------------------------------------------------------------------------
SR = 44100


def _note(freq, dur, amp=1.0, attack=0.04, release=0.25):
    """Une note sinus + harmoniques douces, avec enveloppe attaque/relâche."""
    import numpy as np
    n = int(SR * dur)
    t = np.arange(n) / SR
    wave = (np.sin(2 * np.pi * freq * t)
            + 0.35 * np.sin(2 * np.pi * 2 * freq * t)
            + 0.12 * np.sin(2 * np.pi * 3 * freq * t))
    env = np.ones(n)
    a, r = int(SR * attack), int(SR * release)
    a, r = min(a, n), min(r, n)
    if a > 0:
        env[:a] = np.linspace(0.0, 1.0, a)
    if r > 0:
        env[-r:] *= np.linspace(1.0, 0.0, r)
    return (amp * wave * env).astype(np.float64)


def make_music(total_dur):
    """Boucle calme et optimiste : Cmaj7 → Am7 → Fmaj7 → G6, pads + arpèges."""
    import numpy as np

    def hz(midi):
        return 440.0 * 2 ** ((midi - 69) / 12)

    BAR = 3.0  # secondes par accord (~80 BPM)
    # Accords (notes MIDI) : C4 E4 G4 B4 / A3 C4 E4 G4 / F3 A3 C4 E4 / G3 B3 D4 E4
    chords = [
        [60, 64, 67, 71],
        [57, 60, 64, 67],
        [53, 57, 60, 64],
        [55, 59, 62, 64],
    ]
    loop_dur = BAR * len(chords)
    n_loops = math.ceil((total_dur + 2) / loop_dur)
    buf = np.zeros(int(SR * (n_loops * loop_dur + 2)))

    def mix(start_s, samples):
        i = int(start_s * SR)
        buf[i:i + len(samples)] += samples

    for loop in range(n_loops):
        t0 = loop * loop_dur
        for ci, chord in enumerate(chords):
            tc = t0 + ci * BAR
            # Pad : l'accord tenu, très doux
            for m in chord:
                mix(tc, _note(hz(m), BAR, amp=0.05, attack=0.6, release=1.2))
            # Basse : la fondamentale une octave plus bas
            mix(tc, _note(hz(chord[0] - 12), BAR, amp=0.07, attack=0.05, release=0.9))
            # Arpège : croches plumées sur les notes de l'accord
            seq = chord + [chord[2], chord[1]] if loop % 2 == 0 else [m + 12 for m in chord] + [chord[3], chord[2]]
            step = BAR / 6
            for k, m in enumerate(seq):
                mix(tc + k * step, _note(hz(m), step * 1.8, amp=0.045, attack=0.005, release=step * 1.2))

    buf = buf[:int(SR * total_dur)]
    buf /= max(1e-9, np.max(np.abs(buf)))  # normalise
    pcm = (buf * 0.45 * 32767).astype(np.int16)  # volume final modéré
    import wave as wavemod
    with wavemod.open(str(ROOT / "music.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    print("OK: music.wav (%.0f s)" % total_dur)


# ---------------------------------------------------------------------------
# 3) ASSEMBLE (mp4 + srt)
# ---------------------------------------------------------------------------
def fmt_ts(sec):
    ms = int(round((sec - int(sec)) * 1000))
    s = int(sec)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d},{ms:03d}"


def total_duration():
    return TITLE_DUR + len(SCENES) * SCENE_DUR + END_DUR


def assemble():
    slides = [("01-titre.png", TITLE_DUR, TITLE_CAPTION)]
    slides += [(f"{name}.png", SCENE_DUR, caption) for name, caption, _fn in SCENES]
    slides += [("99-fin.png", END_DUR, END_CAPTION)]
    total = total_duration()

    # concat list pour ffmpeg
    lst = ROOT / "slides.txt"
    with open(lst, "w", encoding="utf-8") as f:
        for fname, dur, _ in slides:
            f.write(f"file 'slides_final/{fname}'\nduration {dur}\n")
        f.write(f"file 'slides_final/{slides[-1][0]}'\n")  # dernier frame requis par concat

    out = ROOT / "english_speakapp_visite_guidee.mp4"
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(lst)]
    music = ROOT / "music.wav"
    if music.exists():
        # Musique de fond : fondu d'entrée 2 s, fondu de sortie sur la carte de fin.
        cmd += ["-i", str(music),
                "-af", f"afade=t=in:d=2,afade=t=out:st={total - END_DUR:.1f}:d={END_DUR:.1f}",
                "-c:a", "aac", "-b:a", "160k"]
    cmd += [
        "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-crf", "18",
        "-preset", "medium", "-t", f"{total:.2f}", "-movflags", "+faststart", str(out),
    ]
    subprocess.run(cmd, check=True, cwd=str(ROOT), capture_output=True)

    # .srt (sous-titres également incrustés dans l'image)
    srt = ROOT / "english_speakapp_visite_guidee.srt"
    with open(srt, "w", encoding="utf-8") as f:
        t = 0.0
        for i, (_fname, dur, caption) in enumerate(slides, 1):
            f.write(f"{i}\n{fmt_ts(t)} --> {fmt_ts(t + dur)}\n{caption}\n\n")
            t += dur
    print("OK:", out.name, "+", srt.name)


if __name__ == "__main__":
    args = sys.argv[1:]
    steps = [a for a in args if a in ("capture", "compose", "music", "assemble")] \
        or ["capture", "compose", "music", "assemble"]
    only = [a for a in args if a not in ("capture", "compose", "music", "assemble")] or None
    if "capture" in steps:
        print("[1/4] Captures...")
        capture(only)
    if "compose" in steps:
        print("[2/4] Composition...")
        compose()
    if "music" in steps:
        print("[3/4] Musique...")
        make_music(total_duration())
    if "assemble" in steps:
        print("[4/4] Assemblage video...")
        assemble()
    print("Termine.")
