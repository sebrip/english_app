"""
Génère une fois pour toutes les portraits réalistes des personnages via l'API
image d'OpenAI, et les enregistre dans frontend/avatars/.
À relancer seulement si on veut régénérer les visages.

    python backend/generate_avatars.py
"""
from __future__ import annotations

import base64
import sys
import tomllib
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = BASE_DIR / "frontend" / "avatars"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def load_key() -> str:
    secrets = BASE_DIR / ".streamlit" / "secrets.toml"
    with open(secrets, "rb") as fh:
        return tomllib.load(fh)["OPENAI_API_KEY"]


KEY = load_key()

PORTRAITS = {
    "john": (
        "Photorealistic professional headshot portrait of a warm, friendly "
        "55-year-old Caucasian male English professor from Boston. Salt-and-pepper "
        "grey hair, neat short beard stubble, wearing thin modern eyeglasses, gentle "
        "encouraging smile, smart casual collared shirt and blazer. Soft studio "
        "lighting, clean softly-blurred neutral blue background, centered, looking at "
        "camera, high detail, natural skin texture."
    ),
    "marcus": (
        "Photorealistic professional headshot portrait of a relaxed, confident "
        "35-year-old African-American man from Brooklyn. Short black hair, short "
        "beard, big friendly smile, casual streetwear hoodie. Soft studio lighting, "
        "clean softly-blurred warm neutral background, centered, looking at camera, "
        "high detail, natural skin texture."
    ),
    "brenda": (
        "Photorealistic professional headshot portrait of a warm, cheerful "
        "55-year-old Caucasian woman from Texas. Blonde shoulder-length wavy hair, "
        "bright red lipstick, big joyful smile, elegant blouse and tasteful jewelry. "
        "Soft studio lighting, clean softly-blurred warm neutral background, centered, "
        "looking at camera, high detail, natural skin texture."
    ),
    "zoe": (
        "Photorealistic professional headshot portrait of a dynamic, serious "
        "24-year-old Caucasian woman from Nevada. Dark brown hair, stylish modern "
        "eyeglasses, confident focused expression, smart casual blazer. Soft studio "
        "lighting, clean softly-blurred cool neutral background, centered, looking at "
        "camera, high detail, natural skin texture."
    ),
    "lucy": (
        "Photorealistic professional headshot portrait of a warm, friendly 42-year-old Caucasian woman, "
        "an English vocabulary teacher. Shoulder-length light-brown hair, subtle natural makeup, gentle "
        "intelligent smile, smart casual blouse and a light cardigan. She looks patient, clear and approachable. "
        "Soft studio lighting, clean softly-blurred warm neutral background, centered, looking at camera, "
        "high detail, natural skin texture."
    ),
    "david": (
        "Photorealistic professional headshot portrait of a calm, distinguished 52-year-old Black man, "
        "an English language examiner. Short greying hair, neat short grey-flecked beard, wearing elegant "
        "rectangular glasses, a composed and kind but authoritative expression, smart navy blazer over a "
        "light shirt. Soft studio lighting, clean softly-blurred neutral background, centered, looking at "
        "camera, high detail, natural deep-brown skin texture."
    ),
    "raj": (
        "Photorealistic warm friendly headshot portrait of a cheerful 40-year-old "
        "Indian man from Mumbai with a warm DEEP BROWN skin tone. Short black hair, neat "
        "short beard, big welcoming smile, smart casual shirt. Soft studio lighting, clean "
        "softly-blurred warm neutral background, centered, looking at camera, high detail, "
        "natural skin texture."
    ),
}


def generate(prompt: str) -> bytes:
    """Essaie gpt-image-1, puis dall-e-3 en repli. Renvoie les octets PNG."""
    headers = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

    # 1) gpt-image-1 (renvoie du base64)
    r = requests.post(
        "https://api.openai.com/v1/images/generations",
        headers=headers,
        json={"model": "gpt-image-1", "prompt": prompt, "size": "1024x1024", "quality": "medium"},
        timeout=120,
    )
    if r.status_code == 200:
        return base64.b64decode(r.json()["data"][0]["b64_json"])
    print(f"  gpt-image-1 KO ({r.status_code}): {r.text[:200]}")

    # 2) dall-e-3 en repli (on demande du base64 aussi)
    r = requests.post(
        "https://api.openai.com/v1/images/generations",
        headers=headers,
        json={"model": "dall-e-3", "prompt": prompt, "size": "1024x1024", "response_format": "b64_json"},
        timeout=120,
    )
    if r.status_code == 200:
        return base64.b64decode(r.json()["data"][0]["b64_json"])
    raise RuntimeError(f"dall-e-3 KO ({r.status_code}): {r.text[:300]}")


def main() -> int:
    force = "--force" in sys.argv  # par défaut on ne régénère PAS les portraits existants
    for cid, prompt in PORTRAITS.items():
        out_existing = OUT_DIR / f"{cid}.png"
        if out_existing.exists() and not force:
            print(f"Déjà présent, on garde : {cid}")
            continue
        print(f"Génération du portrait : {cid} …")
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
