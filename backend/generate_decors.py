"""
Génère les décors de fond (paysage) des lieux via l'API image d'OpenAI,
et les enregistre dans frontend/decors/. À relancer seulement pour régénérer.

    python backend/generate_decors.py
"""
from __future__ import annotations

import base64
import sys
import tomllib
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = BASE_DIR / "frontend" / "decors"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def load_key() -> str:
    secrets = BASE_DIR / ".streamlit" / "secrets.toml"
    with open(secrets, "rb") as fh:
        return tomllib.load(fh)["OPENAI_API_KEY"]


KEY = load_key()

# Décors en paysage. On insiste sur "no text" : les modèles ajoutent souvent du
# texte illisible. Le décor est de toute façon assombri par un calque sombre.
DECORS = {
    "airport": (
        "Wide photorealistic interior of a modern bright airport departure terminal: "
        "large floor-to-ceiling windows with airplanes on the tarmac outside, sleek "
        "check-in desks, glossy floor, soft natural daylight, a few softly blurred "
        "travelers in the distance. Cinematic, calm, inviting. No text, no signs with "
        "letters, no watermark."
    ),
    "restaurant": (
        "Wide photorealistic cozy upscale restaurant interior in the evening: elegant "
        "set tables with small candles and glassware, warm ambient golden lighting, "
        "soft background bokeh, plants, inviting romantic atmosphere. Cinematic depth. "
        "No text, no menu lettering, no watermark."
    ),
    "interview": (
        "Wide photorealistic modern corporate meeting room prepared for a job "
        "interview: a clean wooden table with two chairs facing each other, a laptop "
        "and a notebook, large windows with a bright blurred city skyline, plants, "
        "professional confident atmosphere, soft daylight. No people, no text, no "
        "watermark."
    ),
    "classroom": (
        "Wide photorealistic cozy modern language-learning study room: a warm "
        "well-lit space with a comfortable desk, soft chairs, bookshelves with books, "
        "a few green plants, a blurred chalkboard or whiteboard in the background, "
        "warm inviting daylight, calm and friendly atmosphere. No people, no readable "
        "text, no watermark."
    ),
    "school": (
        "Wide photorealistic bright and friendly primary-school classroom: small wooden "
        "desks and colorful little chairs, a large green chalkboard, cheerful educational "
        "posters on the walls, shelves with books and supplies, big windows with warm "
        "daylight, a few green plants, welcoming and cozy atmosphere. No people, no "
        "readable text, no watermark."
    ),
    "supermarket": (
        "Wide photorealistic interior of a modern bright supermarket: long well-stocked "
        "aisles with colorful shelves of groceries, a glossy clean floor, bright overhead "
        "lighting, a softly blurred fresh-produce section in the distance, inviting "
        "everyday atmosphere. No people, no readable text, no signs with letters, no "
        "watermark."
    ),
}


def generate(prompt: str) -> bytes:
    headers = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

    r = requests.post(
        "https://api.openai.com/v1/images/generations",
        headers=headers,
        json={"model": "gpt-image-1", "prompt": prompt, "size": "1536x1024", "quality": "medium"},
        timeout=180,
    )
    if r.status_code == 200:
        return base64.b64decode(r.json()["data"][0]["b64_json"])
    print(f"  gpt-image-1 KO ({r.status_code}): {r.text[:200]}")

    # Repli dall-e-3 (paysage 1792x1024).
    r = requests.post(
        "https://api.openai.com/v1/images/generations",
        headers=headers,
        json={"model": "dall-e-3", "prompt": prompt, "size": "1792x1024", "response_format": "b64_json"},
        timeout=180,
    )
    if r.status_code == 200:
        return base64.b64decode(r.json()["data"][0]["b64_json"])
    raise RuntimeError(f"dall-e-3 KO ({r.status_code}): {r.text[:300]}")


def main() -> int:
    force = "--force" in sys.argv  # par défaut on ne régénère PAS les décors existants
    for cid, prompt in DECORS.items():
        if (OUT_DIR / f"{cid}.png").exists() and not force:
            print(f"Déjà présent, on garde : {cid}")
            continue
        print(f"Génération du décor : {cid} …")
        try:
            png = generate(prompt)
        except Exception as exc:
            print(f"  ECHEC {cid}: {exc}")
            return 1
        out = OUT_DIR / f"{cid}.png"
        out.write_bytes(png)
        print(f"  -> {out} ({len(png)} octets)")
    print("Terminé.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
