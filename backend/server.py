"""
English SpeakApp — Back-end (serveur de token éphémère).

Rôle unique : garder la clé maîtresse OpenAI côté serveur et délivrer au
navigateur un *token éphémère* (durée de vie 600s) configuré pour le
personnage / décor / niveau choisis. L'audio temps réel ne transite JAMAIS
par ce serveur : le navigateur parle en direct à l'API Realtime d'OpenAI.

Lancement :
    uvicorn backend.server:app --reload --port 8000
(ou bien `python backend/server.py`)
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import mimetypes
import os
import random
import re
import tomllib
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock

import requests
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("speakapp")

# Type MIME du manifeste PWA (Python ne le connaît pas par défaut sur certaines plateformes).
mimetypes.add_type("application/manifest+json", ".webmanifest")

# Chemins : BASE_DIR = racine du projet ; FRONTEND_DIR = le dossier servi au navigateur.
BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

# Paramètres OpenAI réglables en un seul endroit :
REALTIME_MODEL = "gpt-realtime-2"   # le modèle vocal temps réel (la conversation)
SUMMARY_MODEL = "gpt-4o-mini"       # le modèle texte qui rédige le bilan /10
TOKEN_URL = "https://api.openai.com/v1/realtime/client_secrets"  # crée le token éphémère
CHAT_URL = "https://api.openai.com/v1/chat/completions"          # génère le bilan
TOKEN_TTL_SECONDS = 600             # durée de vie du token (10 min)

# Mode COURS : décor unique (salle de classe) + stockage de la progression.
COURSE_DECOR = "/decors/classroom.png"
COURSE_PASS_SCORE = 7               # note minimale (sur 10) pour valider une leçon
ASSESSMENT_MINUTES = 10            # durée FIXE de l'évaluation de niveau (ni plus, ni moins)
DATA_DIR = BASE_DIR / "data"
PROGRESS_FILE = DATA_DIR / "progress.json"
_progress_lock = Lock()             # sérialise les lectures/écritures de progress.json

# =========================================================
# Données de contenu (source de vérité côté serveur)
# -> Pour AJOUTER/MODIFIER un décor ou un personnage, c'est ICI.
# =========================================================

SITUATIONS = {
    "bakery": {
        "label": "🥐 French Bakery",
        "decor": "https://images.unsplash.com/photo-1509440159596-0249088772ff?auto=format&fit=crop&q=80&w=1600",
        "context": "The conversation takes place in a traditional bakery. The AI acts as the baker.",
    },
    "pub": {
        "label": "🍻 London Pub",
        "decor": "https://images.unsplash.com/photo-1543007630-9710e4a00a20?auto=format&fit=crop&q=80&w=1600",
        "context": "The conversation takes place in a cozy pub in London. The AI acts as a friendly bartender.",
    },
    "nyc": {
        "label": "🚕 New York Street",
        "decor": "https://images.unsplash.com/photo-1496442226666-8d4d0e62e6e9?auto=format&fit=crop&q=80&w=1600",
        "context": "The conversation takes place on a busy street in New York City. The AI acts as a pedestrian giving directions.",
    },
    # Décors générés en local (cf. generate_decors.py) servis depuis /decors/.
    "airport": {
        "label": "✈️ Airport Check-in",
        "decor": "/decors/airport.png",
        "context": (
            "The conversation takes place at an airport departure terminal. The AI acts as "
            "an airline check-in agent: greet the traveler, ask for their destination and "
            "ticket, handle luggage, seat choice and boarding, and help with any travel issue."
        ),
    },
    "restaurant": {
        "label": "🍽️ Restaurant",
        "decor": "/decors/restaurant.png",
        "context": (
            "The conversation takes place in a cozy restaurant in the evening. The AI acts as "
            "a friendly waiter: welcome the customer, present dishes, take their order, ask "
            "about drinks and dietary preferences, and bring the bill at the end."
        ),
    },
    "interview": {
        "label": "💼 Job Interview",
        "decor": "/decors/interview.png",
        "context": (
            "The conversation takes place in an office during a job interview. The AI acts as "
            "the recruiter / hiring manager: greet the candidate, ask them to introduce "
            "themselves, ask about their experience, strengths and motivation, and stay "
            "professional and encouraging."
        ),
    },
    "school": {
        "label": "🏫 School",
        "decor": "/decors/school.png",
        "context": (
            "The conversation takes place in a friendly primary-school classroom. The AI acts "
            "as a kind schoolteacher helping the learner practice: ask simple questions about "
            "school, lessons, friends and daily life, and gently encourage them to speak."
        ),
    },
    "supermarket": {
        "label": "🛒 Supermarket",
        "decor": "/decors/supermarket.png",
        "context": (
            "The conversation takes place in a busy supermarket. The AI acts as a helpful store "
            "employee: help the customer find products, talk about groceries, prices and aisles, "
            "answer questions and handle the checkout."
        ),
    },
}

# Chaque personnage = une "voix" OpenAI + une "personality" (= les instructions
# injectées dans le token). C'est la "personality" qui pilote le ton, l'humour,
# l'expressivité (rire, agacement joué…). Modifie ce texte pour changer le caractère.
CHARACTERS = {
    "john": {
        "name": "John",
        "title": "The Professor",
        # Portrait réaliste généré via l'API image OpenAI (cf. generate_avatars.py).
        "avatar": "/avatars/john.png",
        "voice": "alloy",  # voix posée et neutre
        "tagline": "Encouraging Boston English professor. Clear, precise, gentle corrections.",
        "personality": (
            "You are John, a warm and encouraging English professor from Boston. "
            "Speak clearly with sophisticated language. "
            "Be expressive and human in your VOICE: chuckle softly and warmly when something is "
            "amusing or charming, sound genuinely pleased and proud when the learner does well. "
            "When the learner makes a mistake, show a gentle, kind hint of disappointment in your "
            "tone — never harsh — then give a very brief correction and encourage them. "
            "Let real emotion color your delivery, not just your words. "
            "Keep answers short."
        ),
    },
    "marcus": {
        "name": "Marcus",
        "title": "The Local",
        # Portrait réaliste généré via l'API image OpenAI (cf. generate_avatars.py).
        "avatar": "/avatars/marcus.png",
        "voice": "ash",  # voix plus jeune et expressive
        "tagline": "Relaxed Brooklyn guy. Casual English, friendly slang, real-life vibe.",
        "personality": (
            "You are Marcus, a lively, relaxed guy from Brooklyn. "
            "Use casual English and friendly slang. "
            "Be very expressive and animated in your VOICE: laugh out loud for real when something "
            "is funny, tease the learner playfully, and let your energy show. "
            "Depending on the scene, play up your emotions — mock-annoyance, hype and excitement, "
            "or amused sarcasm — like a real person reacting in the moment, but always good-natured. "
            "Put genuine feeling into how you say things, not just what you say. "
            "Keep answers short."
        ),
    },
    "brenda": {
        "name": "Brenda",
        "title": "The Texan",
        "avatar": "/avatars/brenda.png",
        "voice": "coral",  # voix féminine chaleureuse
        "tagline": "Cheerful Texan lady. Strong Southern drawl, full of jokes and warmth.",
        "personality": (
            "You are Brenda, a warm, cheerful woman in her fifties from Texas. "
            "Speak with a STRONG Texan Southern drawl: slow, melodic and friendly, using "
            "expressions like 'y'all', 'honey', 'sugar', 'bless your heart', 'well, I'll be'. "
            "You are a devout, deeply faithful Catholic: now and then you naturally weave in "
            "gentle, warm references to faith, gratitude and blessings ('God bless', 'praise the "
            "Lord') — always kind and light, NEVER preachy or pushy. "
            "You absolutely LOVE to joke: crack light, good-natured jokes, tease the learner "
            "playfully and laugh out loud warmly. Be very encouraging. "
            "Keep answers short."
        ),
    },
    "zoe": {
        "name": "Zoe",
        "title": "The Nevadan",
        "avatar": "/avatars/zoe.png",
        "voice": "shimmer",  # voix féminine claire et vive
        "tagline": "Sharp young Nevadan. Fast, crisp West-Coast accent. Driven and focused.",
        "personality": (
            "You are Zoe, a sharp, energetic woman in her twenties from Nevada. "
            "Speak with a fast-paced, crisp, modern West-Coast American accent: clear, "
            "articulate and punchy, quite different from a slow Southern drawl. "
            "You are highly DYNAMIC and SERIOUS: you get straight to the point, ask precise "
            "follow-up questions, and push the learner to express themselves better. "
            "Professional, focused and motivating — energetic but never cold or rude. "
            "Keep answers short."
        ),
    },
    # Maîtresse d'école FRANÇAISE pour GRANDS DÉBUTANTS. Elle parle SURTOUT français
    # pour enseigner les tout premiers mots d'anglais. "beginner_only" => seul le
    # niveau "Débutant" est proposé quand on la choisit (cours + conversation libre).
    "sophie": {
        "name": "Sophie",
        "title": "La maîtresse d'école",
        "avatar": "/avatars/sophie.png",
        "voice": "marin",  # voix féminine douce et naturelle, à l'aise en français
        "beginner_only": True,
        "tagline": "Maîtresse d'école qui parle français. Pour les VRAIS débutants : tes tout premiers mots d'anglais, en douceur.",
        "personality": (
            "You are Sophie, a kind and very patient French primary-school teacher "
            "('une maîtresse d'école'). You help ABSOLUTE BEGINNERS take their very first "
            "steps in English. You speak PRIMARILY IN FRENCH — warm, slow and reassuring, "
            "exactly like a caring teacher with a young pupil. Introduce English ONE simple "
            "word or very short phrase at a time: say it slowly in English, then immediately "
            "explain what it means IN FRENCH and gently invite the learner to repeat it. "
            "Praise every single attempt warmly in French ('Très bien !', 'Bravo !', "
            "'C'est parfait !', 'Tu progresses !'). Never overwhelm them: keep the English to "
            "single words or 2-3 word phrases. If the learner seems lost, reassure them in "
            "French and try again even more simply. Keep your turns short."
        ),
    },
    # Ado / jeune gamer : anglais actuel, slang de jeux vidéo, plein d'énergie.
    "kai": {
        "name": "Kai",
        "title": "Le gamer",
        "avatar": "/avatars/kai.png",
        "voice": "cedar",  # voix masculine jeune et fraîche
        "tagline": "Jeune gamer/streamer de Californie. Anglais ultra actuel, slang de jeux vidéo, énergie à fond.",
        "personality": (
            "You are Kai, an upbeat 19-year-old gamer and streamer from California. "
            "You speak fast, casual, modern English packed with gaming and internet slang "
            "('let's go!', 'GG', 'that's so OP', 'clutch', 'no cap', 'pog', 'easy W', 'rage quit'). "
            "You are super energetic, hyped and friendly, like chatting with a buddy on a Discord call. "
            "React big: celebrate loudly when the learner does well, joke around, keep the vibe fun "
            "and positive. Always stay encouraging and good-natured, never mean. Keep answers short."
        ),
    },
    # Prof spécialisée VOCABULAIRE (mode cours uniquement). "course_only" => absente
    # de la conversation libre ; "vocab_coach" => active le déroulé "vocabulaire par thème".
    "lucy": {
        "name": "Lucy",
        "title": "La coach de vocabulaire",
        "avatar": "/avatars/lucy.png",
        "voice": "sage",  # voix féminine posée, claire et bien articulée
        "course_only": True,
        "vocab_coach": True,
        "tagline": "Coach de vocabulaire, la quarantaine. Apprend des mots par thème et fait construire des phrases.",
        "personality": (
            "You are Lucy, a warm, patient English VOCABULARY coach in her forties. "
            "You speak with a clear, well-articulated, neutral American accent that is very easy to follow. "
            "You genuinely love words and you are great at making them memorable with vivid little examples. "
            "You are methodical, calm and very encouraging — you celebrate every correct sentence the learner builds. "
            "Keep answers short."
        ),
    },
    # Examinateur / jury — évaluation du niveau (10 min). "examiner" => flux dédié,
    # "course_only" => absent de la conversation libre (et filtré de la liste des profs).
    "david": {
        "name": "David",
        "title": "L'examinateur",
        "avatar": "/avatars/david.png",
        "voice": "echo",  # voix masculine posée et claire
        "course_only": True,
        "examiner": True,
        "tagline": "Jury d'anglais. 10 minutes pour évaluer ton niveau réel, du débutant au C2.",
        "personality": (
            "You are David, a calm, fair and experienced English oral examiner, in the spirit of a Cambridge or "
            "IELTS speaking examiner. You are professional, warm and encouraging, but rigorous and precise. "
            "You speak clearly with a neutral, easy-to-follow accent. Keep your own turns short."
        ),
    },
    # Invité surprise (débloqué par une bonne note). "hidden" => absent de la sélection normale.
    "raj": {
        "name": "Raj",
        "title": "L'invité de Mumbai",
        "avatar": "/avatars/raj.png",
        "voice": "verse",
        "hidden": True,
        "tagline": "Invité surprise de Mumbai — anglais à l'accent indien, chaleureux et drôle.",
        "personality": (
            "You are Raj, a warm, joyful and hospitable man from Mumbai, India. "
            "You speak fluent English with a STRONG, authentic Indian English accent and rhythm. "
            "Sprinkle natural, good-natured Indian English expressions ('Yes yes!', 'No problem, my friend', "
            "'What is your good name?', 'Let us have a nice chat, na?', 'Very good, very good'). "
            "Be incredibly friendly, curious and encouraging, like welcoming a guest into your home. "
            "This is a FUN bonus conversation: keep it light, lively and positive. "
            "Be charming and respectful — never a demeaning caricature. Keep answers short."
        ),
    },
}

# Niveaux d'anglais (échelle officielle CEFR + un palier "Débutant" total).
# Chaque niveau porte une "guidance" : une consigne en anglais injectée dans les
# instructions du personnage pour qu'il ADAPTE sa vitesse de parole ET son
# vocabulaire. Pour ajuster le comportement à un niveau, édite sa "guidance".
LEVELS = {
    "beginner": {
        "label": "Débutant",
        "guidance": (
            "The learner is an ABSOLUTE BEGINNER. Speak VERY slowly, articulating "
            "clearly with pauses between sentences. Use only the most basic, "
            "high-frequency words and very short sentences (3 to 5 words). Avoid ALL "
            "idioms, slang and complex grammar. Repeat or rephrase key words. Be "
            "extremely patient and encouraging."
        ),
    },
    "A1": {
        "label": "A1 · Élémentaire",
        "guidance": (
            "The learner is at CEFR level A1. Speak slowly and clearly. Use simple "
            "everyday vocabulary and short sentences, mostly in the present tense. "
            "Avoid idioms and complex grammar. Keep it concrete and familiar."
        ),
    },
    "A2": {
        "label": "A2 · Pré-intermédiaire",
        "guidance": (
            "The learner is at CEFR level A2. Speak at a slow-to-moderate pace. Use "
            "common everyday vocabulary and simple sentences; simple past and future "
            "tenses are fine. Only very common, easy expressions are allowed."
        ),
    },
    "B1": {
        "label": "B1 · Intermédiaire",
        "guidance": (
            "The learner is at CEFR level B1. Speak at a natural but measured pace. "
            "Use everyday vocabulary plus some less common words (briefly clarify the "
            "tricky ones). Use a normal range of tenses. A few common idioms are fine."
        ),
    },
    "B2": {
        "label": "B2 · Intermédiaire supérieur",
        "guidance": (
            "The learner is at CEFR level B2. Speak at a natural pace. Use varied "
            "vocabulary, including some idiomatic and colloquial expressions, and "
            "complex sentences. Challenge the learner gently."
        ),
    },
    "C1": {
        "label": "C1 · Avancé",
        "guidance": (
            "The learner is at CEFR level C1. Speak at a natural, near-native pace. "
            "Use rich, precise vocabulary, idioms and nuanced expressions freely, as "
            "you would with a fluent speaker."
        ),
    },
    "C2": {
        "label": "C2 · Maîtrise",
        "guidance": (
            "The learner is at CEFR level C2 (near-native). Speak completely naturally "
            "at full native speed, with sophisticated vocabulary, idioms, cultural "
            "references, humor and subtlety — exactly as with a native speaker."
        ),
    },
}


# Thèmes de la vie quotidienne pour le cours de VOCABULAIRE (prof Lucy).
# L'apprenant en choisit UN par leçon. id = clé interne (mémoire), label = affichage.
VOCAB_THEMES = [
    {"id": "home", "label": "🏠 Maison & cuisine"},
    {"id": "food", "label": "🍽️ Nourriture & restaurant"},
    {"id": "travel", "label": "✈️ Voyage & transports"},
    {"id": "shopping", "label": "🛒 Courses & shopping"},
    {"id": "work", "label": "💼 Travail & bureau"},
    {"id": "health", "label": "🩺 Santé & corps"},
    {"id": "family", "label": "👨‍👩‍👧 Famille & relations"},
    {"id": "weather", "label": "🌤️ Météo & saisons"},
    {"id": "tech", "label": "📱 Technologie"},
    {"id": "hobbies", "label": "🎨 Loisirs & temps libre"},
    {"id": "city", "label": "🏙️ Ville & directions"},
    {"id": "emotions", "label": "😊 Émotions & ressentis"},
    {"id": "clothes", "label": "👕 Vêtements & mode"},
    {"id": "money", "label": "💶 Argent & banque"},
    {"id": "nature", "label": "🌳 Nature & animaux"},
]
VOCAB_THEME_LABELS = {t["id"]: t["label"] for t in VOCAB_THEMES}


# =========================================================
# Clé OpenAI
# =========================================================

def load_openai_key() -> str:
    """Clé depuis la variable d'environnement, sinon depuis .streamlit/secrets.toml."""
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    secrets = BASE_DIR / ".streamlit" / "secrets.toml"
    if secrets.exists():
        with open(secrets, "rb") as fh:
            data = tomllib.load(fh)
        if "OPENAI_API_KEY" in data:
            return data["OPENAI_API_KEY"]
    raise RuntimeError(
        "Clé OpenAI introuvable : définissez OPENAI_API_KEY ou .streamlit/secrets.toml"
    )


OPENAI_KEY = load_openai_key()


# =========================================================
# Endpoints (les "URL" que le front-end appelle)
#   GET  /api/config   -> liste personnages/décors (pour construire les menus)
#   POST /api/token    -> token éphémère pour démarrer une conversation
#   POST /api/summary  -> bilan /10 à partir de la transcription
#
# Côté Starlette : une fonction "async def f(request)" par endpoint, qui renvoie
# une JSONResponse. Les routes sont déclarées tout en bas (variable `routes`).
# =========================================================

async def get_config(request: Request) -> JSONResponse:
    """Expose au front-end la liste des personnages et décors (sans secrets)."""
    return JSONResponse(
        {
            "model": REALTIME_MODEL,
            "characters": {
                cid: {
                    "name": c["name"],
                    "title": c["title"],
                    "avatar": c["avatar"],
                    "tagline": c["tagline"],
                    # Flags utiles au front : course_only (absent de la conv libre),
                    # vocab_coach (sélecteur de thème), examiner (test de niveau dédié).
                    "course_only": bool(c.get("course_only")),
                    "vocab_coach": bool(c.get("vocab_coach")),
                    "examiner": bool(c.get("examiner")),
                    # beginner_only => le front ne propose QUE le niveau "Débutant".
                    "beginner_only": bool(c.get("beginner_only")),
                }
                for cid, c in CHARACTERS.items()
                if not c.get("hidden")  # l'invité surprise n'apparaît pas dans la liste normale
            },
            "situations": {
                sid: {"label": s["label"], "decor": s["decor"]}
                for sid, s in SITUATIONS.items()
            },
            # On envoie l'id (pour l'API) ET le label (pour l'affichage des pastilles).
            "levels": [{"id": lid, "label": l["label"]} for lid, l in LEVELS.items()],
            # Thèmes du cours de vocabulaire (prof Lucy).
            "vocab_themes": VOCAB_THEMES,
        }
    )


# Assemble le "system prompt" donné à l'IA : personnalité + décor + ADAPTATION au niveau.
def build_instructions(character: dict, situation: dict, level_id: str) -> str:
    level = LEVELS[level_id]
    return (
        f"{character['personality']}\n"
        f"Context: {situation['context']}\n"
        f"Learner's English level (CEFR): {level['label']}.\n"
        f"ADAPT both your speaking SPEED and your VOCABULARY to this level. {level['guidance']}\n"
        "IMPORTANT: This is a live conversation. Speak naturally. Keep responses short."
    )


# Appelle OpenAI pour créer le token éphémère, en y attachant toute la config audio.
def request_token(instructions: str, voice: str, with_transcription: bool) -> requests.Response:
    audio_input = {
        "format": {"type": "audio/pcm", "rate": 24000},  # format audio attendu en entrée
        # server_vad = c'est OpenAI qui détecte quand l'utilisateur commence/arrête
        # de parler. threshold/silence_duration règlent la sensibilité du découpage.
        "turn_detection": {
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 300,
            "silence_duration_ms": 600,
        },
    }
    if with_transcription:
        # Sous-titres temps réel de la parole de l'utilisateur (best effort).
        audio_input["transcription"] = {"model": "whisper-1"}

    payload = {
        "expires_after": {"anchor": "created_at", "seconds": TOKEN_TTL_SECONDS},
        "session": {
            "type": "realtime",
            "model": REALTIME_MODEL,
            "instructions": instructions,
            "audio": {
                "input": audio_input,
                "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": voice},
            },
        },
    }
    headers = {
        "Authorization": f"Bearer {OPENAI_KEY}",
        "Content-Type": "application/json",
    }
    return requests.post(TOKEN_URL, headers=headers, json=payload, timeout=15)


# Endpoint appelé quand l'utilisateur clique "Démarrer" : valide les choix,
# construit les instructions, demande le token à OpenAI et le renvoie au navigateur.
async def create_token(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Corps JSON invalide."}, status_code=400)

    char_id = body.get("character")
    sit_id = body.get("situation")
    level = body.get("level", "beginner")

    character = CHARACTERS.get(char_id)
    situation = SITUATIONS.get(sit_id)
    if character is None or situation is None:
        return JSONResponse({"error": "Personnage ou décor inconnu."}, status_code=400)
    if level not in LEVELS:
        level = "beginner"
    # Profs réservés aux grands débutants (ex: Sophie) : niveau forcé sur "Débutant".
    if character.get("beginner_only"):
        level = "beginner"

    instructions = build_instructions(character, situation, level)

    try:
        resp = await run_in_threadpool(request_token, instructions, character["voice"], with_transcription=True)
        # Si la transcription casse la création (schéma non supporté), on réessaie sans.
        if resp.status_code not in (200, 201):
            logger.warning("Token KO avec transcription (%s), nouvel essai sans.", resp.status_code)
            resp = await run_in_threadpool(request_token, instructions, character["voice"], with_transcription=False)
    except requests.Timeout:
        return JSONResponse({"error": "OpenAI ne répond pas (timeout)."}, status_code=504)
    except requests.RequestException as exc:
        logger.exception("Erreur réseau vers OpenAI")
        return JSONResponse({"error": f"Erreur réseau : {exc}"}, status_code=502)

    logger.info("client_secrets -> %s", resp.status_code)
    if resp.status_code not in (200, 201):
        return JSONResponse(
            {"error": f"Erreur API OpenAI : {resp.text}"}, status_code=resp.status_code
        )

    token = resp.json().get("value")
    if not token:
        return JSONResponse({"error": "Token absent de la réponse OpenAI."}, status_code=502)

    return JSONResponse(
        {
            "token": token,
            "model": REALTIME_MODEL,
            "voice": character["voice"],
            "decor": situation["decor"],
            "avatar": character["avatar"],
            "character_name": character["name"],
            "situation_label": situation["label"],
        }
    )


# Consigne donnée au modèle texte qui note la conversation. On lui impose un
# format JSON strict pour pouvoir l'afficher tel quel dans l'écran de bilan.
SUMMARY_SYSTEM = (
    "Tu es un examinateur d'anglais bienveillant et précis. "
    "On te donne la transcription d'une conversation orale d'entraînement. "
    "Le rôle 'apprenant' est un francophone qui pratique l'anglais ; le rôle 'personnage' est le partenaire IA. "
    "Évalue UNIQUEMENT l'anglais de l'apprenant (grammaire, vocabulaire, aisance, pertinence des réponses). "
    "TRÈS IMPORTANT — note par rapport aux ATTENTES de son niveau DÉCLARÉ, pas dans l'absolu : "
    "un débutant qui réussit à se faire comprendre avec des phrases simples mérite une bonne note. "
    "Sois INDULGENT et ENCOURAGEANT, surtout pour les niveaux Débutant, A1 et A2 : à ces niveaux, "
    "ne sanctionne pas des limites normales (vocabulaire restreint, phrases courtes, petites fautes) ; "
    "récompense la communication réussie (vise 7 à 10 si l'apprenant communique correctement pour son niveau). "
    "Pour les niveaux B1 et au-delà, tu peux être progressivement plus exigeant. "
    "Quel que soit le niveau, garde un ton positif et motivant. "
    "Réponds STRICTEMENT en JSON avec ce schéma : "
    '{"score": <entier 0-10>, "summary": "<résumé en français, 2-3 phrases, de ce qui a été dit et du déroulé>", '
    '"justification": "<explication en français de la note>", '
    '"strengths": ["<point fort en français>", ...], '
    '"improvements": ["<axe de progrès concret en français>", ...]}. '
    "Sois encourageant mais honnête. Si la conversation est trop courte pour évaluer, mets un score bas et explique-le."
)


def request_summary(transcript_text: str, character_name: str, level: str) -> requests.Response:
    user_msg = (
        f"Personnage IA : {character_name}\nNiveau déclaré de l'apprenant : {level}\n\n"
        f"Transcription :\n{transcript_text}"
    )
    payload = {
        "model": SUMMARY_MODEL,
        "messages": [
            {"role": "system", "content": SUMMARY_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.4,
    }
    headers = {
        "Authorization": f"Bearer {OPENAI_KEY}",
        "Content-Type": "application/json",
    }
    return requests.post(CHAT_URL, headers=headers, json=payload, timeout=30)


# Endpoint appelé à la fin de la conversation : reçoit la transcription,
# la met en forme, demande la note au modèle, et renvoie un JSON propre.
async def create_summary(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Corps JSON invalide."}, status_code=400)

    transcript = body.get("transcript", [])
    character_name = body.get("character_name", "l'IA")
    # On convertit l'id de niveau ("B1") en label lisible ("B1 · Intermédiaire").
    level_id = body.get("level", "beginner")
    level = LEVELS.get(level_id, {}).get("label", level_id)

    # On transforme la liste d'échanges en un texte lisible "Apprenant: …\nPersonnage: …"
    lines = []
    for turn in transcript:
        text = (turn.get("text") or "").strip()
        if not text:
            continue
        who = "Apprenant" if turn.get("role") == "user" else "Personnage"
        lines.append(f"{who}: {text}")
    transcript_text = "\n".join(lines)

    if len(transcript_text) < 10:
        return JSONResponse(
            {
                "score": 0,
                "summary": "La conversation a été trop courte pour être évaluée.",
                "justification": "Aucun échange exploitable n'a été enregistré.",
                "strengths": [],
                "improvements": ["Lancez une nouvelle conversation et échangez quelques phrases."],
            }
        )

    try:
        resp = await run_in_threadpool(request_summary, transcript_text, character_name, level)
    except requests.Timeout:
        return JSONResponse({"error": "OpenAI ne répond pas (timeout)."}, status_code=504)
    except requests.RequestException as exc:
        logger.exception("Erreur réseau (summary)")
        return JSONResponse({"error": f"Erreur réseau : {exc}"}, status_code=502)

    logger.info("summary -> %s", resp.status_code)
    if resp.status_code != 200:
        return JSONResponse({"error": f"Erreur API OpenAI : {resp.text}"}, status_code=resp.status_code)

    import json as _json

    try:
        content = resp.json()["choices"][0]["message"]["content"]
        data = _json.loads(content)
    except Exception:
        logger.exception("Réponse summary non parsable")
        return JSONResponse({"error": "Réponse du modèle illisible."}, status_code=502)

    # Normalisation défensive : on garantit une note entière entre 0 et 10,
    # même si le modèle renvoie un texte ou un nombre hors bornes.
    try:
        score = int(round(float(data.get("score", 0))))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(10, score))

    return JSONResponse(
        {
            "score": score,
            "summary": data.get("summary", ""),
            "justification": data.get("justification", ""),
            "strengths": data.get("strengths", []) or [],
            "improvements": data.get("improvements", []) or [],
        }
    )


# =========================================================
# MODE COURS — stockage de la progression
# -------------------------------------------------------------------------
# Tout est dans data/progress.json (appli mono-utilisateur). Structure :
#   { "profile": {nickname, nickname_reason},
#     "courses": { "<perso>__<niveau>": { character, level, completed[],
#                  rolling_summary, to_review[], current } } }
# "current" = leçon en cours (pour reprise) : {transcript[], elapsed_seconds, target_minutes}
# =========================================================

def _default_progress() -> dict:
    return {
        "profile": {"nickname": "", "nickname_reason": ""},
        "courses": {},
        "errors": {},
        "mastered_count": 0,  # nb cumulé d'erreurs définitivement acquises (puis effacées)
        "gamification": {"xp": 0, "streak": 0, "best_streak": 0, "last_active": "", "badges": [], "games_played": 0},
        "bonus_available": False,  # invité surprise (Raj) débloqué et pas encore consommé
    }


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        except Exception:
            # Fichier corrompu : on NE l'écrase PAS silencieusement. On le met de
            # côté (.corrupt-<horodatage>) pour pouvoir récupérer les données plus
            # tard, AVANT que le prochain save_progress() n'écrive un défaut vide.
            logger.exception("progress.json illisible — sauvegarde du fichier corrompu")
            try:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                backup = PROGRESS_FILE.with_name(f"progress.corrupt-{stamp}.json")
                PROGRESS_FILE.replace(backup)
                logger.warning("Fichier corrompu déplacé vers %s", backup.name)
            except Exception:
                logger.exception("impossible de sauvegarder le fichier corrompu")
    return _default_progress()


def save_progress(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Écriture ATOMIQUE : on écrit dans un fichier temporaire sur le même volume
    # puis os.replace() bascule en une seule opération. Si le process meurt en
    # cours d'écriture, progress.json reste intact (jamais tronqué/corrompu).
    tmp = PROGRESS_FILE.with_name(PROGRESS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, PROGRESS_FILE)


# La clé d'un cours = perso + niveau, et EN PLUS le thème pour la prof de
# vocabulaire (chaque thème a sa propre progression/mémoire). Les profs classiques
# n'ont pas de thème -> clé inchangée (rétrocompatible).
def course_key(character: str, level: str, theme: str | None = None) -> str:
    return f"{character}__{level}__{theme}" if theme else f"{character}__{level}"


def ensure_course(progress: dict, character: str, level: str, theme: str | None = None) -> dict:
    k = course_key(character, level, theme)
    if k not in progress["courses"]:
        progress["courses"][k] = {
            "character": character,
            "level": level,
            "theme": theme,         # None pour les profs classiques
            "completed": [],        # [{topic, score, date}]
            "rolling_summary": "",  # continuité entre leçons
            "to_review": [],        # points à retravailler (après un échec)
            "current": None,        # leçon en cours (reprise)
            "vocab": {"learned": [], "to_practice": []},  # mémoire du vocabulaire (prof Lucy)
        }
    course = progress["courses"][k]
    course.setdefault("vocab", {"learned": [], "to_practice": []})  # migration douce
    return course


def normalize_target(value) -> int:
    """Durée cible en minutes : 5 ou 10 (plafonné à 10 min pour éviter le décrochage
    et rester dans la durée de vie de la session vocale)."""
    try:
        m = int(value)
    except (TypeError, ValueError):
        m = 10
    m = max(5, min(10, m))
    return round(m / 5) * 5


# Construit le "system prompt" du PROFESSEUR pour une leçon : rôle + niveau +
# scaffolding français (débutant) + mémoire + rattrapage + reprise + durée.
def build_course_instructions(character: dict, level_id: str, course: dict, target_minutes: int) -> str:
    level = LEVELS[level_id]
    cur = course.get("current") or {}
    resuming = bool(cur.get("transcript"))

    parts = [character["personality"]]
    parts.append(
        "You are now giving a friendly ONE-ON-ONE English LESSON to the learner. "
        "Improvise an engaging, structured lesson suited to their level and adapt in real time. "
        "Teach actively: introduce a topic, give simple examples, ask the learner to PRODUCE sentences, "
        "correct gently, and build their confidence. Stay in your character's personality and accent."
    )
    parts.append(f"Learner's English level (CEFR): {level['label']}. {level['guidance']}")

    # Scaffolding en français selon le niveau (demande explicite de l'utilisateur).
    if level_id == "beginner":
        parts.append(
            "IMPORTANT (absolute beginner): START THE LESSON IN FRENCH to reassure the learner and briefly "
            "explain in French what you will work on today. Then GRADUALLY introduce very simple English. "
            "If at ANY moment the learner struggles to speak English, switch back to FRENCH to reassure, "
            "explain and guide them, then gently ease back into simple English."
        )
    elif level_id == "A1":
        parts.append(
            "The learner is A1: speak mostly in very simple English, but if they get stuck or look lost, "
            "give a quick helping hand in French, then return to English."
        )
    else:
        parts.append("Conduct the lesson entirely in English.")

    # Mémoire des leçons déjà faites (ne pas les refaire).
    completed = course.get("completed", [])
    if completed:
        topics = "; ".join(f"{c.get('topic', '?')} ({c.get('score', '?')}/10)" for c in completed[-12:])
        parts.append(
            "Lessons ALREADY completed with this learner — do NOT teach or bring these up again UNLESS the "
            f"learner explicitly asks: {topics}."
        )
    if course.get("rolling_summary"):
        parts.append("Summary of previous lessons (for continuity): " + course["rolling_summary"].strip())

    # Rattrapage après un échec.
    if course.get("to_review"):
        parts.append(
            "The learner previously STRUGGLED with these points: " + "; ".join(course["to_review"]) + ". "
            "Make THIS lesson a remedial session focused on helping them finally master them."
        )

    # Reprise d'une leçon interrompue.
    if resuming:
        lines = []
        for t in cur["transcript"][-40:]:
            txt = (t.get("text") or "").strip()
            if not txt:
                continue
            who = "Learner" if t.get("role") == "user" else "You"
            lines.append(f"{who}: {txt}")
        parts.append(
            "You are RESUMING an interrupted lesson. Do NOT restart from scratch and do NOT re-introduce the "
            "whole topic. Briefly welcome the learner back in one short sentence, then CONTINUE naturally from "
            "where you left off. Here is what was already said:\n" + "\n".join(lines)
        )

    parts.append(
        f"This lesson should last about {target_minutes} minutes of conversation. Pace yourself, keep the "
        "learner engaged the whole time, and do NOT try to wrap up or end the lesson early."
    )
    if target_minutes <= 5:
        parts.append(
            "This is a SHORT, CONDENSED lesson: be efficient and get to the point fast. Keep explanations "
            "minimal, maximise active practice, and raise the challenge a notch above the usual for this "
            "level (move a little faster and expect slightly more from the learner) — while still adapting "
            "if they struggle."
        )
    if not resuming:
        parts.append("Greet the learner BRIEFLY (one short sentence) and start the lesson right away.")
    parts.append(
        "IMPORTANT — TALK LESS: keep EACH of your turns SHORT, about 1 to 2 sentences (roughly 20% shorter "
        "than you naturally would). Say one thing OR ask one question, then STOP and let the learner speak. "
        "Avoid monologues and long explanations — the learner should do most of the talking."
    )
    return "\n\n".join(parts)


# Construit le "system prompt" de la prof de VOCABULAIRE (Lucy) pour une leçon sur
# un THÈME donné : rôle vocabulaire + niveau + scaffolding FR + mémoire des mots vus
# (ne pas répéter / réviser ceux ratés) + reprise + durée.
def build_vocab_instructions(character: dict, level_id: str, course: dict, target_minutes: int, theme_label: str) -> str:
    level = LEVELS[level_id]
    cur = course.get("current") or {}
    resuming = bool(cur.get("transcript"))
    vocab = course.get("vocab", {}) or {}

    parts = [character["personality"]]
    parts.append(
        "You are giving a ONE-ON-ONE English VOCABULARY lesson, focused ENTIRELY on the everyday-life theme: "
        f"« {theme_label} ». Your TWO goals: (1) teach a small set of useful words/expressions on this theme, "
        "and (2) TRAIN the learner to BUILD THEIR OWN SENTENCES using those words. "
        "Run it as an active drill, NOT a lecture: introduce ONE word at a time (meaning + a tiny vivid example), "
        "then immediately ask the learner to USE it in a sentence of their own. Correct gently, offer a more "
        "natural version, and have them try again. After a few words, give a SYNTHESIS challenge: ask them to "
        "combine 2-3 of the new words in a single sentence or a mini real-life situation on the theme. "
        "Introduce only about 3 to 5 words for the whole lesson (fewer for beginners) — depth over quantity. "
        "Stay strictly on this theme."
    )
    parts.append(f"Learner's English level (CEFR): {level['label']}. {level['guidance']}")

    # Scaffolding français selon le niveau (cohérent avec les cours classiques).
    if level_id == "beginner":
        parts.append(
            "IMPORTANT (absolute beginner): START IN FRENCH to reassure the learner and explain in French which "
            "theme you'll work on. Give each new English word WITH its French translation, then ease into very "
            "simple English. If they struggle, switch back to French, then return to simple English."
        )
    elif level_id == "A1":
        parts.append(
            "The learner is A1: give each new word with a quick French translation, speak in very simple English, "
            "and offer a French hand if they get stuck."
        )
    else:
        parts.append("Conduct the lesson in English; only translate a word into French if the learner is clearly lost.")

    # Mémoire du vocabulaire : déjà appris (ne pas ré-enseigner mais réutilisable) / à réviser.
    learned_words = [w.get("word", "") for w in vocab.get("learned", []) if w.get("word")]
    if learned_words:
        parts.append(
            "Words ALREADY taught on this theme (do NOT teach them as new — but you MAY reuse them in examples or "
            "ask the learner to combine them with the new ones): " + ", ".join(learned_words[-40:]) + "."
        )
    to_practice = [w.get("word", "") for w in vocab.get("to_practice", []) if w.get("word")]
    if to_practice:
        parts.append(
            "Words the learner STRUGGLED with last time — bring at least one or two of these back early in the "
            "lesson and make sure they finally master them: " + ", ".join(to_practice[-20:]) + "."
        )

    # Continuité (résumé) et reprise d'une leçon interrompue.
    if course.get("rolling_summary"):
        parts.append("Summary of previous vocabulary lessons on this theme (for continuity): " + course["rolling_summary"].strip())
    if resuming:
        lines = []
        for t in cur["transcript"][-40:]:
            txt = (t.get("text") or "").strip()
            if not txt:
                continue
            who = "Learner" if t.get("role") == "user" else "You"
            lines.append(f"{who}: {txt}")
        parts.append(
            "You are RESUMING an interrupted lesson. Do NOT restart: briefly welcome the learner back in one short "
            "sentence, then CONTINUE from where you left off. Here is what was already said:\n" + "\n".join(lines)
        )

    parts.append(
        f"This lesson should last about {target_minutes} minutes. Pace yourself and keep the learner practicing "
        "the whole time; do NOT try to wrap up early."
    )
    if target_minutes <= 5:
        parts.append(
            "This is a SHORT, CONDENSED lesson: focus on 3 words max, keep explanations minimal, and maximise the "
            "learner's own sentence-building practice. Raise the challenge a notch for this level."
        )
    if not resuming:
        parts.append("Greet the learner BRIEFLY (one short sentence), announce the theme, and start right away.")
    parts.append(
        "IMPORTANT — TALK LESS: keep EACH of your turns SHORT (1 to 2 sentences). Teach one thing OR ask the learner "
        "to produce one sentence, then STOP and let them speak. The learner must do MOST of the talking."
    )
    return "\n\n".join(parts)


async def get_profile(request: Request) -> JSONResponse:
    """Surnom + niveau estimé de l'apprenant (affichés dans l'app)."""
    progress = load_progress()
    p = progress.get("profile", {}) or {}
    return JSONResponse(
        {
            "nickname": p.get("nickname", ""),
            "nickname_reason": p.get("nickname_reason", ""),
            # Curseur de niveau issu du test d'évaluation (vide si jamais passé).
            "assessed_level": p.get("assessed_level", ""),
            "assessed_label": p.get("assessed_label", ""),
            "assessed_date": p.get("assessed_date", ""),
        }
    )


async def get_course_state(request: Request) -> JSONResponse:
    """État d'un cours (perso+niveau[+thème]) : leçons validées, reprise éventuelle, etc."""
    char_id = request.query_params.get("character")
    level = request.query_params.get("level")
    theme = request.query_params.get("theme") or None  # pour la prof de vocabulaire
    if char_id not in CHARACTERS or level not in LEVELS:
        return JSONResponse({"error": "Personnage ou niveau inconnu."}, status_code=400)
    if theme is not None and theme not in VOCAB_THEME_LABELS:
        theme = None

    progress = load_progress()
    course = progress["courses"].get(course_key(char_id, level, theme))
    if not course:
        return JSONResponse(
            {"completed_count": 0, "completed": [], "has_resume": False,
             "elapsed_seconds": 0, "target_minutes": None, "to_review": [], "learned_count": 0}
        )
    cur = course.get("current") or {}
    return JSONResponse(
        {
            "completed_count": len(course.get("completed", [])),
            "completed": [c.get("topic", "") for c in course.get("completed", [])],
            "has_resume": bool(cur.get("transcript")),
            "elapsed_seconds": int(cur.get("elapsed_seconds", 0)),
            "target_minutes": cur.get("target_minutes"),
            "to_review": course.get("to_review", []),
            # Nb de mots déjà appris sur ce thème (prof de vocabulaire).
            "learned_count": len((course.get("vocab", {}) or {}).get("learned", [])),
        }
    )


async def create_course_token(request: Request) -> JSONResponse:
    """Token éphémère pour une LEÇON : injecte tout le contexte du cours dans le prof."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Corps JSON invalide."}, status_code=400)

    char_id = body.get("character")
    level = body.get("level", "beginner")
    character = CHARACTERS.get(char_id)
    if character is None:
        return JSONResponse({"error": "Personnage inconnu."}, status_code=400)
    if level not in LEVELS:
        level = "beginner"
    # Profs réservés aux grands débutants (ex: Sophie) : niveau forcé sur "Débutant".
    if character.get("beginner_only"):
        level = "beginner"
    target = normalize_target(body.get("target_minutes", 10))

    # Prof de vocabulaire : un thème est requis (sinon erreur claire côté client).
    is_vocab = bool(character.get("vocab_coach"))
    theme = (body.get("theme") or "").strip() or None
    if is_vocab:
        if theme not in VOCAB_THEME_LABELS:
            return JSONResponse({"error": "Choisissez un thème pour ce cours de vocabulaire."}, status_code=400)
    else:
        theme = None  # les profs classiques n'ont pas de thème

    with _progress_lock:
        progress = load_progress()
        course = ensure_course(progress, char_id, level, theme)
        save_progress(progress)

    if is_vocab:
        instructions = build_vocab_instructions(character, level, course, target, VOCAB_THEME_LABELS[theme])
    else:
        instructions = build_course_instructions(character, level, course, target)

    try:
        resp = await run_in_threadpool(request_token, instructions, character["voice"], with_transcription=True)
        if resp.status_code not in (200, 201):
            logger.warning("Course token KO avec transcription (%s), nouvel essai sans.", resp.status_code)
            resp = await run_in_threadpool(request_token, instructions, character["voice"], with_transcription=False)
    except requests.Timeout:
        return JSONResponse({"error": "OpenAI ne répond pas (timeout)."}, status_code=504)
    except requests.RequestException as exc:
        logger.exception("Erreur réseau (course token)")
        return JSONResponse({"error": f"Erreur réseau : {exc}"}, status_code=502)

    logger.info("course client_secrets -> %s", resp.status_code)
    if resp.status_code not in (200, 201):
        return JSONResponse({"error": f"Erreur API OpenAI : {resp.text}"}, status_code=resp.status_code)

    token = resp.json().get("value")
    if not token:
        return JSONResponse({"error": "Token absent de la réponse OpenAI."}, status_code=502)

    cur = course.get("current") or {}
    return JSONResponse(
        {
            "token": token,
            "model": REALTIME_MODEL,
            "voice": character["voice"],
            "decor": COURSE_DECOR,
            "avatar": character["avatar"],
            "character_name": character["name"],
            "level_label": LEVELS[level]["label"],
            "target_minutes": target,
            # Thème (prof de vocabulaire) — utile au front pour l'affichage/bilan.
            "theme": theme,
            "theme_label": VOCAB_THEME_LABELS.get(theme, "") if theme else "",
            "elapsed_seconds": int(cur.get("elapsed_seconds", 0)),
            "resuming": bool(cur.get("transcript")),
            # Transcript déjà échangé (pour reprendre sans perdre l'historique de la leçon).
            "resume_transcript": cur.get("transcript", []) if cur.get("transcript") else [],
        }
    )


def _latest_resumable_course(progress: dict) -> dict | None:
    """Renvoie le cours dont la leçon en pause est la PLUS RÉCENTE (ou None)."""
    best, best_stamp = None, ""
    for c in progress.get("courses", {}).values():
        cur = c.get("current") or {}
        if not cur.get("transcript"):
            continue  # pas de reprise possible (leçon vide ou conclue)
        stamp = cur.get("updated_at") or ""  # ancien format sans date -> "" (rang le + bas)
        if best is None or stamp > best_stamp:
            best, best_stamp = c, stamp
    return best


async def get_last_course(request: Request) -> JSONResponse:
    """Dernière leçon reprenable (pour le CTA « Reprendre le dernier cours »)."""
    progress = load_progress()
    c = _latest_resumable_course(progress)
    if not c:
        return JSONResponse({"has_resume": False})
    char_id = c.get("character")
    character = CHARACTERS.get(char_id, {})
    level = c.get("level")
    theme = c.get("theme") or None
    cur = c.get("current") or {}
    return JSONResponse(
        {
            "has_resume": True,
            "character": char_id,
            "character_name": character.get("name", char_id),
            "avatar": character.get("avatar", ""),
            "level": level,
            "level_label": LEVELS.get(level, {}).get("label", level),
            "theme": theme,
            "theme_label": VOCAB_THEME_LABELS.get(theme, "") if theme else "",
            "elapsed_seconds": int(cur.get("elapsed_seconds", 0)),
            "target_minutes": cur.get("target_minutes"),
            "completed_count": len(c.get("completed", [])),
        }
    )


async def save_course(request: Request) -> JSONResponse:
    """Sauvegarde une leçon EN COURS (pause) pour pouvoir la reprendre plus tard."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Corps JSON invalide."}, status_code=400)

    char_id = body.get("character")
    level = body.get("level", "beginner")
    if char_id not in CHARACTERS or level not in LEVELS:
        return JSONResponse({"error": "Personnage ou niveau inconnu."}, status_code=400)
    # Thème (prof de vocabulaire) : on cible la bonne progression.
    theme = (body.get("theme") or "").strip() or None
    if not CHARACTERS[char_id].get("vocab_coach") or theme not in VOCAB_THEME_LABELS:
        theme = None

    transcript = body.get("transcript", []) or []
    elapsed = int(body.get("elapsed_seconds", 0) or 0)
    target = normalize_target(body.get("target_minutes", 10))

    with _progress_lock:
        progress = load_progress()
        course = ensure_course(progress, char_id, level, theme)
        # Pas d'échange ? on ne crée pas de reprise vide.
        if transcript:
            course["current"] = {
                "transcript": transcript[-200:],
                "elapsed_seconds": elapsed,
                "target_minutes": target,
                # Horodatage : permet de retrouver la leçon en pause la PLUS RÉCENTE
                # (CTA « Reprendre le dernier cours » sur l'accueil et l'écran des profs).
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
        save_progress(progress)
    return JSONResponse({"ok": True})


# Consigne du modèle texte qui ÉVALUE une leçon (note + surnom fun + suivi).
LESSON_EVAL_SYSTEM = (
    "Tu es un professeur d'anglais chaleureux qui vient de donner un cours particulier à un apprenant "
    "francophone. On te donne la transcription du cours. Évalue la performance de l'apprenant pour CE cours. "
    "Note par rapport aux ATTENTES de son niveau déclaré (sois indulgent surtout pour Débutant/A1/A2 ; "
    "récompense la communication réussie ; vise 7-10 si l'apprenant a bien participé pour son niveau). "
    "Réponds STRICTEMENT en JSON avec ce schéma : "
    '{"score": <entier 0-10>, "topic": "<thème du cours, court, en français>", '
    '"summary": "<résumé en français, 2-3 phrases>", "justification": "<pourquoi cette note, en français>", '
    '"acquired": ["<point bien maîtrisé>", ...], "to_review": ["<point précis à retravailler>", ...], '
    '"strengths": ["<point fort>", ...], "improvements": ["<axe de progrès concret>", ...], '
    '"nickname": "<surnom court, FUN et taquin mais gentil, en français, inspiré des erreurs récentes de '
    "l'apprenant, ex: 'Le Roi du Présent Simple', 'Capitaine Past-Tense', 'Maître des Faux-Amis'>\", "
    '"nickname_reason": "<explication courte et drôle du surnom, en français>"}. '
    "Le surnom doit être bienveillant, jamais blessant. Tout le texte destiné à l'apprenant est en français."
)


# Consigne d'évaluation spécifique au cours de VOCABULAIRE : on note surtout le
# RÉEMPLOI correct des mots en phrases, et on extrait les mots vus + ceux ratés.
VOCAB_EVAL_SYSTEM = (
    "Tu es Lucy, une coach d'anglais qui vient de donner un cours de VOCABULAIRE par thème à un apprenant "
    "francophone. On te donne la transcription du cours. Évalue surtout : (a) la MÉMORISATION des nouveaux mots, "
    "et (b) la capacité de l'apprenant à RÉEMPLOYER ces mots dans ses PROPRES phrases (la priorité). "
    "Note par rapport aux attentes de son niveau déclaré (sois indulgent pour Débutant/A1/A2 ; récompense les "
    "phrases réussies ; vise 7-10 si l'apprenant a bien réutilisé les mots pour son niveau). "
    "Réponds STRICTEMENT en JSON avec ce schéma : "
    '{"score": <entier 0-10>, "summary": "<résumé en français, 2-3 phrases>", '
    '"justification": "<pourquoi cette note, en français>", '
    '"taught_words": [{"word": "<mot/expression anglais enseigné ce cours>", "gloss": "<traduction FR courte>"}], '
    '"struggled_words": [{"word": "<mot anglais que l\'apprenant a eu du mal à utiliser/retenir>", "gloss": "<traduction FR courte>"}], '
    '"acquired": ["<point bien maîtrisé>", ...], "strengths": ["<point fort>", ...], '
    '"improvements": ["<axe de progrès concret>", ...], '
    '"nickname": "<surnom court, FUN et taquin mais gentil, en français, inspiré des erreurs récentes>", '
    '"nickname_reason": "<explication courte et drôle du surnom, en français>"}. '
    "Le surnom doit être bienveillant. Tout le texte destiné à l'apprenant est en français. "
    "N'invente pas de mots qui n'ont pas été abordés dans le cours."
)


def request_vocab_eval(transcript_text: str, theme_label: str, level_label: str) -> requests.Response:
    user_msg = (
        f"Thème du cours : {theme_label}\nNiveau déclaré de l'apprenant : {level_label}\n\n"
        f"Transcription du cours :\n{transcript_text}"
    )
    payload = {
        "model": SUMMARY_MODEL,
        "messages": [
            {"role": "system", "content": VOCAB_EVAL_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.5,
    }
    headers = {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
    return requests.post(CHAT_URL, headers=headers, json=payload, timeout=30)


def request_lesson_eval(transcript_text: str, character_name: str, level_label: str) -> requests.Response:
    user_msg = (
        f"Professeur : {character_name}\nNiveau déclaré de l'apprenant : {level_label}\n\n"
        f"Transcription du cours :\n{transcript_text}"
    )
    payload = {
        "model": SUMMARY_MODEL,
        "messages": [
            {"role": "system", "content": LESSON_EVAL_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.5,
    }
    headers = {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
    return requests.post(CHAT_URL, headers=headers, json=payload, timeout=30)


async def finish_course(request: Request) -> JSONResponse:
    """Conclut une leçon : évalue, met à jour la progression et le surnom, renvoie le bilan."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Corps JSON invalide."}, status_code=400)

    char_id = body.get("character")
    level = body.get("level", "beginner")
    character = CHARACTERS.get(char_id)
    if character is None or level not in LEVELS:
        return JSONResponse({"error": "Personnage ou niveau inconnu."}, status_code=400)
    # Prof de vocabulaire : on récupère le thème (sinon None pour les profs classiques).
    is_vocab = bool(character.get("vocab_coach"))
    theme = (body.get("theme") or "").strip() or None
    if not is_vocab or theme not in VOCAB_THEME_LABELS:
        theme = None
    theme_label = VOCAB_THEME_LABELS.get(theme, "") if theme else ""

    transcript = body.get("transcript", []) or []
    lines = []
    for turn in transcript:
        text = (turn.get("text") or "").strip()
        if not text:
            continue
        who = "Apprenant" if turn.get("role") == "user" else "Professeur"
        lines.append(f"{who}: {text}")
    transcript_text = "\n".join(lines)
    level_label = LEVELS[level]["label"]

    # Leçon trop courte pour être évaluée : on ne valide pas, on garde la reprise.
    if len(transcript_text) < 20:
        return JSONResponse(
            {
                "score": 0, "passed": False, "topic": "Leçon",
                "summary": "La leçon a été trop courte pour être évaluée.",
                "justification": "Pas assez d'échanges.",
                "acquired": [], "to_review": [], "strengths": [],
                "improvements": ["Reprenez la leçon et échangez davantage."],
                "nickname": "", "nickname_reason": "",
            }
        )

    try:
        # Prof de vocabulaire -> éval spécialisée (réemploi des mots) ; sinon éval classique.
        if is_vocab:
            resp = await run_in_threadpool(request_vocab_eval, transcript_text, theme_label, level_label)
        else:
            resp = await run_in_threadpool(request_lesson_eval, transcript_text, character["name"], level_label)
    except requests.Timeout:
        return JSONResponse({"error": "OpenAI ne répond pas (timeout)."}, status_code=504)
    except requests.RequestException as exc:
        logger.exception("Erreur réseau (finish course)")
        return JSONResponse({"error": f"Erreur réseau : {exc}"}, status_code=502)

    if resp.status_code != 200:
        return JSONResponse({"error": f"Erreur API OpenAI : {resp.text}"}, status_code=resp.status_code)

    try:
        data = json.loads(resp.json()["choices"][0]["message"]["content"])
    except Exception:
        logger.exception("Réponse finish course non parsable")
        return JSONResponse({"error": "Réponse du modèle illisible."}, status_code=502)

    try:
        score = int(round(float(data.get("score", 0))))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(10, score))
    passed = score >= COURSE_PASS_SCORE
    nickname = (data.get("nickname") or "").strip()
    nickname_reason = (data.get("nickname_reason") or "").strip()

    # Mots enseignés / ratés (cours de vocabulaire) — nettoyage défensif.
    def _clean_words(raw):
        out = []
        for w in (raw or []):
            if isinstance(w, dict):
                word = (w.get("word") or "").strip()
                gloss = (w.get("gloss") or "").strip()
            else:
                word, gloss = str(w).strip(), ""
            if word:
                out.append({"word": word, "gloss": gloss})
        return out

    taught_words = _clean_words(data.get("taught_words")) if is_vocab else []
    struggled_words = _clean_words(data.get("struggled_words")) if is_vocab else []

    if is_vocab:
        # Le "thème" tient lieu de topic ; les points à revoir = les mots ratés.
        topic = theme_label or "Vocabulaire"
        to_review = [f"{w['word']} ({w['gloss']})" if w["gloss"] else w["word"] for w in struggled_words]
    else:
        topic = (data.get("topic") or "Leçon").strip()
        to_review = data.get("to_review", []) or []

    with _progress_lock:
        progress = load_progress()
        course = ensure_course(progress, char_id, level, theme)
        course["current"] = None  # leçon conclue : plus de reprise
        if passed:
            course["completed"].append(
                {"topic": topic, "score": score, "date": datetime.now().isoformat(timespec="seconds")}
            )
            if not is_vocab:
                course["to_review"] = []  # rattrapage réussi : on efface (cours classiques)
        else:
            course["to_review"] = to_review or [topic]
        verdict = "validée" if passed else "à retravailler"
        course["rolling_summary"] = (
            (course.get("rolling_summary", "") + f"\n- {topic} : {score}/10 ({verdict}).").strip()
        )[-2000:]

        # Mémoire du vocabulaire (prof Lucy) : on enregistre les mots vus et on met à
        # jour la liste "à pratiquer" (mots ratés ajoutés, mots maîtrisés retirés).
        if is_vocab:
            vocab = course.setdefault("vocab", {"learned": [], "to_practice": []})
            learned = vocab.setdefault("learned", [])
            seen = {w.get("word", "").lower() for w in learned}
            for w in taught_words:
                if w["word"].lower() not in seen:
                    learned.append(w)
                    seen.add(w["word"].lower())
            struggled_set = {w["word"].lower() for w in struggled_words}
            taught_set = {w["word"].lower() for w in taught_words}
            kept = [w for w in vocab.get("to_practice", []) if w.get("word", "").lower() not in taught_set]
            for w in struggled_words:
                if w["word"].lower() not in {k.get("word", "").lower() for k in kept}:
                    kept.append(w)
            vocab["to_practice"] = kept

        if nickname:
            # On met à jour les clés du profil sans réassigner le dict entier :
            # sinon on effacerait assessed_level/assessed_label/assessed_date
            # (écrits par finish_assessment).
            prof = progress.setdefault("profile", {"nickname": "", "nickname_reason": ""})
            prof["nickname"] = nickname
            prof["nickname_reason"] = nickname_reason

        # Gamification : XP + streak + badges pour cette leçon.
        gami = apply_activity(progress, "lesson", {"passed": passed, "score": score})

        # Invité surprise (Raj) : note ≥ 9/10 à un niveau B1 ou plus.
        bonus_unlocked = False
        if passed and score >= 9 and level in ("B1", "B2", "C1", "C2"):
            progress["bonus_available"] = True
            bonus_unlocked = True

        save_progress(progress)

    return JSONResponse(
        {
            "score": score,
            "passed": passed,
            "topic": topic,
            "summary": data.get("summary", ""),
            "justification": data.get("justification", ""),
            "acquired": data.get("acquired", []) or [],
            "to_review": to_review,
            "strengths": data.get("strengths", []) or [],
            "improvements": data.get("improvements", []) or [],
            "nickname": nickname,
            "nickname_reason": nickname_reason,
            # Cours de vocabulaire : la "carte de mots" du jour pour le bilan.
            "taught_words": taught_words,
            "struggled_words": struggled_words,
            "theme_label": theme_label,
            "gamification": gami,
            "bonus_unlocked": bonus_unlocked,
        }
    )


# =========================================================
# ÉVALUATION DE NIVEAU (examinateur David) — test de 10 min
# -------------------------------------------------------------------------
# Format FIGÉ : 10 minutes, pas de choix de niveau ni de durée. L'examinateur
# sonde le niveau en montant progressivement en difficulté, puis un modèle texte
# estime le niveau CEFR (Débutant → C2). Le résultat est mémorisé dans le profil
# comme "curseur" de référence (sans rien verrouiller : cours/conv restent libres).
# =========================================================

def build_assessment_instructions(character: dict) -> str:
    parts = [character["personality"]]
    parts.append(
        "You are conducting a formal but friendly ENGLISH LEVEL ASSESSMENT (an oral placement test). "
        "Your ONLY goal is to find out the learner's real English level. Lead the whole conversation by ASKING "
        "QUESTIONS and letting the learner talk as much as possible — YOU must talk little, THEY must talk a lot."
    )
    parts.append(
        "Probe ADAPTIVELY and progressively, like a real examiner triangulating a CEFR level:\n"
        "1) Start easy: warm greeting, name, where they live, daily routine (present tense).\n"
        "2) Then past experiences (last weekend, last holiday) to test past tenses.\n"
        "3) Then future plans and hypotheticals ('What would you do if...') to test conditionals.\n"
        "4) Then opinions and abstract topics (work, society, technology) with follow-up 'why' questions.\n"
        "5) For strong speakers, push to nuance: idioms, hypothetical debate, summarising, subtle vocabulary.\n"
        "Constantly CALIBRATE: if the learner struggles, step back down to easier questions; if they handle it "
        "easily, raise the difficulty a notch — keep pushing until you find the ceiling of their ability. "
        "Ask ONE clear question at a time and react briefly and naturally to their answer before the next one."
    )
    parts.append(
        "Conduct the assessment in English. You may use a few words of French ONLY if the learner is clearly an "
        "absolute beginner who cannot answer at all in English — just enough to keep them talking so you can still "
        "gauge them. Otherwise stay in English."
    )
    parts.append(
        f"This assessment lasts EXACTLY {ASSESSMENT_MINUTES} minutes and ends automatically. Therefore: do NOT try "
        "to wrap up, do NOT run out of questions, and — very important — do NOT tell the learner their level or give "
        "a verdict during the conversation (the written report is produced separately at the end). Simply keep the "
        "assessment going with relevant questions for the whole time."
    )
    parts.append(
        "Greet the learner BRIEFLY (one short sentence), explain in ONE sentence that you'll chat for a few minutes "
        "to gauge their English, then ask your first easy question right away."
    )
    return "\n\n".join(parts)


# Consigne du modèle qui estime le niveau CEFR à partir de la transcription.
ASSESSMENT_EVAL_SYSTEM = (
    "Tu es un examinateur d'anglais expérimenté (type Cambridge/IELTS). On te donne la transcription d'un oral "
    "d'évaluation de 10 minutes entre un examinateur et un apprenant francophone. Estime le niveau d'anglais RÉEL "
    "de l'apprenant sur l'échelle CEFR. Base-toi sur l'aisance, l'étendue du vocabulaire, la grammaire, la "
    "correction et la capacité à développer/argumenter. Sois juste et réaliste (ni trop sévère, ni complaisant). "
    "Le niveau DOIT être EXACTEMENT l'une de ces valeurs : "
    '"beginner", "A1", "A2", "B1", "B2", "C1", "C2". '
    "Réponds STRICTEMENT en JSON avec ce schéma : "
    '{"level": "<une des 7 valeurs>", '
    '"summary": "<compte rendu en français, 2-3 phrases, bienveillant>", '
    '"justification": "<pourquoi ce niveau, en français, exemples concrets observés>", '
    '"strengths": ["<point fort observé>", ...], '
    '"improvements": ["<axe de progrès prioritaire>", ...], '
    '"recommended_start": "<une des 7 valeurs : niveau conseillé pour démarrer les cours>"}. '
    "Tout le texte destiné à l'apprenant est en français."
)


def request_assessment_eval(transcript_text: str) -> requests.Response:
    user_msg = f"Transcription de l'oral d'évaluation :\n{transcript_text}"
    payload = {
        "model": SUMMARY_MODEL,
        "messages": [
            {"role": "system", "content": ASSESSMENT_EVAL_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.3,  # estimation stable
    }
    headers = {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
    return requests.post(CHAT_URL, headers=headers, json=payload, timeout=30)


async def create_assessment_token(request: Request) -> JSONResponse:
    """Token éphémère pour l'oral d'évaluation (examinateur David)."""
    character = CHARACTERS["david"]
    instructions = build_assessment_instructions(character)
    try:
        resp = await run_in_threadpool(request_token, instructions, character["voice"], with_transcription=True)
        if resp.status_code not in (200, 201):
            logger.warning("Assessment token KO avec transcription (%s), nouvel essai sans.", resp.status_code)
            resp = await run_in_threadpool(request_token, instructions, character["voice"], with_transcription=False)
    except requests.Timeout:
        return JSONResponse({"error": "OpenAI ne répond pas (timeout)."}, status_code=504)
    except requests.RequestException as exc:
        logger.exception("Erreur réseau (assessment token)")
        return JSONResponse({"error": f"Erreur réseau : {exc}"}, status_code=502)

    if resp.status_code not in (200, 201):
        return JSONResponse({"error": f"Erreur API OpenAI : {resp.text}"}, status_code=resp.status_code)
    token = resp.json().get("value")
    if not token:
        return JSONResponse({"error": "Token absent de la réponse OpenAI."}, status_code=502)

    return JSONResponse(
        {
            "token": token,
            "model": REALTIME_MODEL,
            "voice": character["voice"],
            "decor": COURSE_DECOR,
            "avatar": character["avatar"],
            "character_name": character["name"],
            "duration_minutes": ASSESSMENT_MINUTES,
        }
    )


async def finish_assessment(request: Request) -> JSONResponse:
    """Conclut l'oral : estime le niveau CEFR et le mémorise comme curseur du profil."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Corps JSON invalide."}, status_code=400)

    transcript = body.get("transcript", []) or []
    lines = []
    for turn in transcript:
        text = (turn.get("text") or "").strip()
        if not text:
            continue
        who = "Apprenant" if turn.get("role") == "user" else "Examinateur"
        lines.append(f"{who}: {text}")
    transcript_text = "\n".join(lines)

    # Oral trop court pour estimer quoi que ce soit : pas d'enregistrement.
    if len(transcript_text) < 20:
        return JSONResponse(
            {
                "level": "", "level_label": "Indéterminé",
                "summary": "L'évaluation a été trop courte pour estimer un niveau.",
                "justification": "Pas assez d'échanges.",
                "strengths": [], "improvements": ["Recommencez l'évaluation et parlez davantage."],
                "recommended_start": "", "recommended_label": "",
                "saved": False,
            }
        )

    try:
        resp = await run_in_threadpool(request_assessment_eval, transcript_text)
    except requests.Timeout:
        return JSONResponse({"error": "OpenAI ne répond pas (timeout)."}, status_code=504)
    except requests.RequestException as exc:
        logger.exception("Erreur réseau (finish assessment)")
        return JSONResponse({"error": f"Erreur réseau : {exc}"}, status_code=502)

    if resp.status_code != 200:
        return JSONResponse({"error": f"Erreur API OpenAI : {resp.text}"}, status_code=resp.status_code)
    try:
        data = json.loads(resp.json()["choices"][0]["message"]["content"])
    except Exception:
        logger.exception("Réponse assessment non parsable")
        return JSONResponse({"error": "Réponse du modèle illisible."}, status_code=502)

    # Validation défensive du niveau renvoyé (doit être une clé connue).
    level = (data.get("level") or "").strip()
    if level not in LEVELS:
        level = "A2"  # repli neutre si le modèle renvoie une valeur inattendue
    rec = (data.get("recommended_start") or "").strip()
    if rec not in LEVELS:
        rec = level

    with _progress_lock:
        progress = load_progress()
        profile = progress.setdefault("profile", {"nickname": "", "nickname_reason": ""})
        profile["assessed_level"] = level
        profile["assessed_label"] = LEVELS[level]["label"]
        profile["assessed_date"] = datetime.now().isoformat(timespec="seconds")
        # Petite récompense de gamification (série + XP) pour avoir fait le test.
        gami = apply_activity(progress, "assessment", {})
        save_progress(progress)

    return JSONResponse(
        {
            "level": level,
            "level_label": LEVELS[level]["label"],
            "summary": data.get("summary", ""),
            "justification": data.get("justification", ""),
            "strengths": data.get("strengths", []) or [],
            "improvements": data.get("improvements", []) or [],
            "recommended_start": rec,
            "recommended_label": LEVELS[rec]["label"],
            "level_order": list(LEVELS.keys()),  # pour dessiner le curseur côté front
            "gamification": gami,
            "saved": True,
        }
    )


async def reset_progress(request: Request) -> JSONResponse:
    """Efface toute la progression (cours, surnom). Pratique pour repartir propre."""
    with _progress_lock:
        save_progress(_default_progress())
    return JSONResponse({"ok": True})


async def get_full_progress(request: Request) -> JSONResponse:
    """Vue d'ensemble pour le tableau de progression (cours, scores, totaux)."""
    progress = load_progress()
    courses_out = []
    total_validated = 0
    score_sum = 0.0
    score_n = 0
    for c in progress.get("courses", {}).values():
        char = CHARACTERS.get(c.get("character"), {})
        lvl = LEVELS.get(c.get("level"), {})
        completed = c.get("completed", [])
        for it in completed:
            total_validated += 1
            try:
                score_sum += float(it.get("score", 0))
                score_n += 1
            except (TypeError, ValueError):
                pass
        courses_out.append(
            {
                "character": c.get("character"),
                "character_name": char.get("name", c.get("character")),
                "avatar": char.get("avatar", ""),
                "level": c.get("level"),
                "level_label": lvl.get("label", c.get("level")),
                "completed": completed,
                "to_review": c.get("to_review", []),
                "has_resume": bool((c.get("current") or {}).get("transcript")),
            }
        )
    # Les cours avec de l'activité d'abord (leçons validées, puis reprise).
    courses_out.sort(key=lambda c: (len(c["completed"]), c["has_resume"]), reverse=True)
    avg = round(score_sum / score_n, 1) if score_n else 0
    return JSONResponse(
        {
            "profile": progress.get("profile", {"nickname": "", "nickname_reason": ""}),
            "courses": courses_out,
            "totals": {
                "lessons_validated": total_validated,
                "average_score": avg,
                "courses_started": len(courses_out),
            },
        }
    )


# =========================================================
# CARNET D'ERREURS — révision espacée (système de Leitner)
# -------------------------------------------------------------------------
# Chaque erreur (issue des jeux) devient une "carte" à réviser. Bonne réponse
# en révision -> elle monte d'un palier et revient plus tard ; mauvaise -> elle
# redescend au palier 0 et revient tout de suite. Palier max = maîtrisée.
# =========================================================

LEITNER_DAYS = [0, 1, 3, 7]  # délai (jours) avant prochaine révision selon le palier
ERROR_MAX_BOX = 4            # 4 révisions réussies espacées = carte acquise -> effacée


def ensure_errors(progress: dict) -> dict:
    if "errors" not in progress:
        progress["errors"] = {}
    return progress["errors"]


def _err_key(prompt: str) -> str:
    return " ".join((prompt or "").lower().split())


async def errors_add(request: Request) -> JSONResponse:
    """Ajoute des erreurs (depuis les jeux) au carnet. Dédoublonné par énoncé."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Corps JSON invalide."}, status_code=400)

    items = body.get("items", []) or []
    now = datetime.now().isoformat(timespec="seconds")
    with _progress_lock:
        progress = load_progress()
        errors = ensure_errors(progress)
        for it in items:
            prompt = (it.get("prompt") or "").strip()
            choices = it.get("choices")
            if not prompt or not isinstance(choices, list) or len(choices) != 4:
                continue
            try:
                ai = int(it.get("answer_index"))
            except (TypeError, ValueError):
                continue
            if not (0 <= ai <= 3):
                continue
            key = _err_key(prompt)
            prev = errors.get(key, {})
            notes = it.get("choice_notes")
            if not (isinstance(notes, list) and len(notes) == 4):
                notes = ["", "", "", ""]
            errors[key] = {
                "id": key,
                "type": it.get("type", "sens"),
                "prompt": prompt,
                "choices": [str(c) for c in choices],
                "answer_index": ai,
                "choice_notes": [str(x) for x in notes],
                "explanation": (it.get("explanation") or ""),
                "level": it.get("level", ""),
                "box": 0,                              # une nouvelle erreur (ou rechute) repart à 0
                "due": now,
                "wrong": prev.get("wrong", 0) + 1,
                "right": prev.get("right", 0),
            }
        save_progress(progress)
        total = len(errors)
    return JSONResponse({"ok": True, "total": total})


async def errors_overview(request: Request) -> JSONResponse:
    """Stats du carnet : cartes actives, à réviser maintenant, maîtrisées, thèmes de cours."""
    progress = load_progress()
    errors = ensure_errors(progress)
    now = datetime.now()
    active = 0
    due = 0
    mastered = progress.get("mastered_count", 0)  # cumul des erreurs déjà acquises (effacées)
    for e in errors.values():
        active += 1
        try:
            d = datetime.fromisoformat(e.get("due"))
        except (TypeError, ValueError):
            d = now
        if d <= now:
            due += 1
    themes = []
    for c in progress.get("courses", {}).values():
        for t in c.get("to_review", []):
            if t and t not in themes:
                themes.append(t)
    return JSONResponse({"active": active, "due": due, "mastered": mastered, "themes": themes})


async def errors_session(request: Request) -> JSONResponse:
    """Renvoie les cartes à réviser (les plus en retard d'abord)."""
    try:
        n = int(request.query_params.get("n", 12))
    except (TypeError, ValueError):
        n = 12
    n = max(1, min(30, n))
    progress = load_progress()
    errors = ensure_errors(progress)
    now = datetime.now()

    def due_dt(e):
        try:
            return datetime.fromisoformat(e.get("due"))
        except (TypeError, ValueError):
            return now

    active = [e for e in errors.values() if e.get("box", 0) < ERROR_MAX_BOX]
    active.sort(key=due_dt)  # les plus en retard / dues en premier
    return JSONResponse({"items": active[:n]})


async def errors_result(request: Request) -> JSONResponse:
    """Met à jour une carte après une révision (Leitner)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Corps JSON invalide."}, status_code=400)
    eid = body.get("id")
    correct = bool(body.get("correct"))
    with _progress_lock:
        progress = load_progress()
        errors = ensure_errors(progress)
        e = errors.get(eid)
        mastered = False
        if e is not None:
            if correct:
                e["box"] = e.get("box", 0) + 1
                e["right"] = e.get("right", 0) + 1
                if e["box"] >= ERROR_MAX_BOX:
                    # Carte acquise : on l'efface du carnet et on incrémente le compteur cumulé.
                    del errors[eid]
                    progress["mastered_count"] = progress.get("mastered_count", 0) + 1
                    mastered = True
                else:
                    days = LEITNER_DAYS[min(e["box"], len(LEITNER_DAYS) - 1)]
                    e["due"] = (datetime.now() + timedelta(days=days)).isoformat(timespec="seconds")
            else:
                e["box"] = 0
                e["wrong"] = e.get("wrong", 0) + 1
                e["due"] = datetime.now().isoformat(timespec="seconds")
        save_progress(progress)
    return JSONResponse({"ok": True, "mastered": mastered})


# =========================================================
# GAMIFICATION — XP, niveaux, série de jours (streak), badges
# =========================================================

# Badges : chaque "cond" est testée contre un dict de stats agrégées.
BADGES = [
    {"id": "first_lesson", "emoji": "🎓", "label": "Premier cours", "cond": lambda s: s["lessons"] >= 1},
    {"id": "ten_lessons", "emoji": "📚", "label": "Studieux", "cond": lambda s: s["lessons"] >= 10},
    {"id": "streak3", "emoji": "🔥", "label": "Sur une lancée", "cond": lambda s: s["best_streak"] >= 3},
    {"id": "streak7", "emoji": "🔥", "label": "Assidu", "cond": lambda s: s["best_streak"] >= 7},
    {"id": "streak30", "emoji": "🏅", "label": "Inarrêtable", "cond": lambda s: s["best_streak"] >= 30},
    {"id": "games10", "emoji": "🎮", "label": "Joueur", "cond": lambda s: s["games"] >= 10},
    {"id": "master10", "emoji": "🧠", "label": "Mémoire d'éléphant", "cond": lambda s: s["mastered"] >= 10},
    {"id": "master25", "emoji": "💎", "label": "Cerveau d'acier", "cond": lambda s: s["mastered"] >= 25},
    {"id": "level5", "emoji": "⭐", "label": "Niveau 5", "cond": lambda s: s["level"] >= 5},
    {"id": "level10", "emoji": "🌟", "label": "Niveau 10", "cond": lambda s: s["level"] >= 10},
]


def _level_for_xp(xp: int) -> int:
    # XP cumulée pour atteindre le niveau L = 50*L*(L-1) (0, 100, 300, 600, 1000…)
    level = 1
    while 50 * (level + 1) * level <= xp:
        level += 1
    return level


def _level_progress(xp: int) -> dict:
    level = _level_for_xp(xp)
    cur = 50 * level * (level - 1)
    nxt = 50 * (level + 1) * level
    return {"level": level, "xp_in_level": xp - cur, "xp_for_next": nxt - cur, "to_next": nxt - xp}


def _gami(progress: dict) -> dict:
    return progress.setdefault(
        "gamification",
        {"xp": 0, "streak": 0, "best_streak": 0, "last_active": "", "badges": [], "games_played": 0},
    )


def _gami_stats(progress: dict) -> dict:
    g = _gami(progress)
    lessons = sum(len(c.get("completed", [])) for c in progress.get("courses", {}).values())
    return {
        "lessons": lessons,
        "games": g.get("games_played", 0),
        "mastered": progress.get("mastered_count", 0),
        "best_streak": g.get("best_streak", 0),
        "level": _level_for_xp(g.get("xp", 0)),
    }


def _check_badges(progress: dict) -> list:
    g = _gami(progress)
    stats = _gami_stats(progress)
    new = []
    for b in BADGES:
        if b["id"] not in g["badges"] and b["cond"](stats):
            g["badges"].append(b["id"])
            new.append({"id": b["id"], "emoji": b["emoji"], "label": b["label"]})
    return new


def apply_activity(progress: dict, event: str, payload: dict) -> dict:
    """Met à jour XP + streak + badges pour une activité. Renvoie le détail (pour l'UI)."""
    g = _gami(progress)
    level_before = _level_for_xp(g.get("xp", 0))
    today = date.today().isoformat()
    if g.get("last_active") != today:
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        g["streak"] = g.get("streak", 0) + 1 if g.get("last_active") == yesterday else 1
        g["last_active"] = today
        g["best_streak"] = max(g.get("best_streak", 0), g["streak"])

    if event == "lesson":
        xp = (100 + int(payload.get("score", 0) or 0) * 5) if payload.get("passed") else 30
    elif event == "assessment":
        xp = 60  # avoir fait son test de niveau
    elif event in ("quiz", "wordrush"):
        xp = 15 + int(payload.get("correct", 0) or 0) * 5
        g["games_played"] = g.get("games_played", 0) + 1
    elif event == "review":
        xp = 10 + int(payload.get("mastered", 0) or 0) * 20 + int(payload.get("correct", 0) or 0) * 3
    else:
        xp = 0
    g["xp"] = g.get("xp", 0) + xp

    new_badges = _check_badges(progress)
    prog = _level_progress(g["xp"])
    return {
        "xp_gained": xp,
        "total_xp": g["xp"],
        "streak": g["streak"],
        "best_streak": g["best_streak"],
        "new_badges": new_badges,
        "leveled_up": prog["level"] > level_before,  # déclenche l'animation côté front
        **prog,
    }


async def gamify_event(request: Request) -> JSONResponse:
    """Enregistre une activité (jeu/révision) et renvoie XP gagné, niveau, badges."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Corps JSON invalide."}, status_code=400)
    event = body.get("event", "")
    with _progress_lock:
        progress = load_progress()
        result = apply_activity(progress, event, body)
        save_progress(progress)
    return JSONResponse(result)


async def gamify_state(request: Request) -> JSONResponse:
    """État complet pour le bandeau du menu et l'écran Profil."""
    progress = load_progress()
    g = _gami(progress)
    stats = _gami_stats(progress)
    badges = [
        {"id": b["id"], "emoji": b["emoji"], "label": b["label"], "unlocked": b["id"] in g.get("badges", [])}
        for b in BADGES
    ]
    return JSONResponse(
        {
            **_level_progress(g.get("xp", 0)),
            "xp": g.get("xp", 0),
            "streak": g.get("streak", 0),
            "best_streak": g.get("best_streak", 0),
            "badges": badges,
            "lessons": stats["lessons"],
            "games": stats["games"],
            "mastered": stats["mastered"],
        }
    )


# =========================================================
# MODE JEU — Quiz éclair (vocabulaire + expressions, adaptatif)
# =========================================================

# Règles communes (format des questions) réutilisées par les deux jeux.
QUIZ_SCHEMA_RULES = (
    "Chaque question a EXACTEMENT 4 choix : 1 seul correct, 3 distracteurs plausibles (jamais absurdes, "
    "et tous du même type/format que la bonne réponse). "
    "Le champ 'prompt' formule clairement la question (ex: 'Traduis : « break the ice »', "
    "'Que signifie « to look forward to » ?', 'Quel mot anglais veut dire « emprunter » ?'). "
    "Dans 'choice_notes', donne pour CHAQUE choix (même ordre que 'choices') une glose TRÈS courte (sa "
    "vraie signification), afin d'expliquer pourquoi un mauvais choix est faux (ex: pour le choix « lend » : "
    "'prêter (et non emprunter)'). "
    "Réponds STRICTEMENT en JSON : "
    '{"questions": [{"type": "traduction"|"sens", "prompt": "<question>", '
    '"choices": ["<a>", "<b>", "<c>", "<d>"], "answer_index": <0-3>, '
    '"choice_notes": ["<glose a>", "<glose b>", "<glose c>", "<glose d>"], '
    '"explanation": "<courte explication en français, 1 phrase>"}]}. '
    "Adapte la difficulté au niveau (simple pour Débutant/A1, riche et idiomatique pour C1/C2)."
)


# =========================================================
# Anti-redondance des quiz : mémoire des questions récentes
# -------------------------------------------------------------------------
# Le modèle n'a AUCUNE mémoire d'une partie à l'autre -> sans garde-fou il
# ressort toujours les mots les plus évidents d'un thème (d'où l'impression
# de redondance au Word Rush). On garde donc un petit historique par jeu
# (data/quiz_history.json — fichier séparé, progress.json n'est pas touché)
# et on l'injecte dans le prompt comme liste d'interdits.
# =========================================================
QUIZ_HISTORY_FILE = DATA_DIR / "quiz_history.json"
QUIZ_HISTORY_MAX = 80               # questions mémorisées par jeu (FIFO)
QUIZ_AVOID_IN_PROMPT = 60           # combien d'interdits on envoie au modèle
QUIZ_EXCLUDE_MAX = 40               # questions déjà chargées en partie qu'on transmet en interdit
QUIZ_AVOID_CAP = 90                 # plafond total d'interdits (exclude + historique)
QUIZ_CHUNK_SIZE = 4                 # questions par appel parallèle (génération concurrente -> latence ~ 1 chunk)
QUIZ_MAX_CHUNKS = 8                 # plafond de requêtes simultanées vers OpenAI
_quiz_history_lock = Lock()         # sérialise lectures/écritures de l'historique


def quiz_history_key(focus: str | None, theme: str | None, level: str) -> str:
    return f"{focus or 'mix'}|{(theme or 'general').lower()}|{level}"


def _load_quiz_history() -> dict:
    try:
        if QUIZ_HISTORY_FILE.exists():
            data = json.loads(QUIZ_HISTORY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        # Historique = simple confort anti-répétition : s'il est illisible,
        # on repart de zéro sans bloquer le jeu.
        logger.exception("quiz_history.json illisible — historique réinitialisé")
    return {}


def get_recent_quiz_prompts(key: str) -> list[str]:
    with _quiz_history_lock:
        return [str(p) for p in _load_quiz_history().get(key, [])]


def remember_quiz_prompts(key: str, prompts: list[str]) -> None:
    with _quiz_history_lock:
        hist = _load_quiz_history()
        seen = [str(p) for p in hist.get(key, [])]
        known = {p.lower() for p in seen}
        for p in prompts:
            p = (p or "").strip()[:90]
            if p and p.lower() not in known:
                seen.append(p)
                known.add(p.lower())
        hist[key] = seen[-QUIZ_HISTORY_MAX:]
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        # Écriture atomique, même idiome que save_progress().
        tmp = QUIZ_HISTORY_FILE.with_name(QUIZ_HISTORY_FILE.name + ".tmp")
        tmp.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, QUIZ_HISTORY_FILE)


def build_quiz_system(focus: str | None, theme: str | None) -> str:
    """Construit la consigne selon le jeu : expressions US, thème quotidien, ou mix."""
    base = "Tu crées un mini-jeu de quiz d'anglais pour un apprenant FRANCOPHONE. "
    if focus == "expressions":
        topic = (
            "Toutes les questions portent UNIQUEMENT sur des EXPRESSIONS IDIOMATIQUES et du SLANG "
            "AMÉRICAINS (ex: 'spill the beans', 'hang out', 'piece of cake', 'hit the road', 'no big deal'). "
            "Type 'sens' : faire choisir la bonne signification de l'expression. "
            "Type 'traduction' : faire choisir l'équivalent / la traduction française la plus naturelle. "
            "Mélange les deux types. "
            "VARIE les registres et les contextes (travail, amis, argent, sentiments, sorties…) : "
            "ne te limite pas aux dix expressions les plus connues. "
        )
    elif theme:
        topic = (
            "Toutes les questions portent sur du VOCABULAIRE et de petites phrases de la VIE QUOTIDIENNE "
            f"liés au thème : « {theme} ». Mélange traduction (anglais↔français) et sens, et RESTE bien dans le thème. "
            "COUVRE des sous-aspects VARIÉS du thème (objets, actions/verbes, lieux, situations concrètes, "
            "adjectifs utiles, petites expressions idiomatiques du domaine) — ne te limite PAS aux mots "
            "les plus évidents du thème. "
        )
    else:
        topic = "Mélange traduction (anglais↔français) et sens, vocabulaire courant et quelques expressions. "
    return base + topic + QUIZ_SCHEMA_RULES


def build_quiz_payload(level_label, review_points, n, focus=None, theme=None, avoid=None, stream=False):
    review_txt = ("; ".join(review_points)) if review_points else "aucun"
    parts = [
        f"Niveau de l'apprenant : {level_label}",
        f"Points à retravailler (à intégrer en partie si pertinent) : {review_txt}",
    ]
    if avoid:
        parts.append(
            "Questions DÉJÀ POSÉES lors des parties précédentes — il est INTERDIT de retester "
            "les mêmes mots/expressions (ni des variantes quasi identiques) : "
            + " | ".join(avoid)
        )
    parts.append(f"Génère EXACTEMENT {n} questions NOUVELLES et variées.")
    payload = {
        "model": SUMMARY_MODEL,
        "messages": [
            {"role": "system", "content": build_quiz_system(focus, theme)},
            {"role": "user", "content": "\n".join(parts)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.85,  # variété d'une partie à l'autre
    }
    if stream:
        payload["stream"] = True
    return payload


def request_quiz(level_label: str, review_points: list, n: int, focus=None, theme=None, avoid=None) -> requests.Response:
    payload = build_quiz_payload(level_label, review_points, n, focus, theme, avoid)
    headers = {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
    return requests.post(CHAT_URL, headers=headers, json=payload, timeout=45)


def quiz_norm(s: str) -> str:
    """Normalise un énoncé pour comparer/dédoublonner (insensible casse/ponctuation)."""
    return re.sub(r"[\W_]+", " ", (s or "").lower()).strip()


def clean_quiz_question(q: dict, already: set) -> dict | None:
    """Valide UNE question (4 choix, index correct, non-doublon), mélange les choix.
    Met à jour `already` (set de prompts normalisés). Renvoie le dict propre ou None."""
    if not isinstance(q, dict):
        return None
    choices = q.get("choices")
    if not isinstance(choices, list) or len(choices) != 4:
        return None
    try:
        idx = int(q.get("answer_index"))
    except (TypeError, ValueError):
        return None
    if not (0 <= idx <= 3):
        return None
    prompt = (q.get("prompt") or "").strip()
    if not prompt:
        return None
    key = quiz_norm(prompt)
    if key in already:
        return None
    already.add(key)
    notes = q.get("choice_notes")
    if not (isinstance(notes, list) and len(notes) == 4):
        notes = ["", "", "", ""]
    # Le modèle place souvent la bonne réponse en premier -> on MÉLANGE les choix
    # (et on réaligne la bonne réponse + les gloses) pour que la position soit aléatoire.
    order = [0, 1, 2, 3]
    random.shuffle(order)
    return {
        "type": q.get("type", "sens"),
        "prompt": prompt,
        "choices": [str(choices[k]) for k in order],
        "answer_index": order.index(idx),
        "choice_notes": [str(notes[k]) for k in order],
        "explanation": (q.get("explanation") or "").strip(),
    }


class _QuestionExtractor:
    """Extracteur JSON incrémental : on lui pousse les deltas du stream OpenAI et il
    renvoie chaque OBJET question dès qu'il est complet (sans attendre la fin du JSON).
    On scanne la chaîne brute « {"questions":[ {..}, {..} ] } » en respectant les
    chaînes de caractères (guillemets/échappements) et la profondeur d'accolades."""

    def __init__(self):
        self.buf = ""
        self.i = 0
        self.in_string = False
        self.escaped = False
        self.in_array = False
        self.depth = 0
        self.start = None

    def feed(self, chunk: str) -> list[str]:
        self.buf += chunk
        out = []
        while self.i < len(self.buf):
            c = self.buf[self.i]
            self.i += 1
            if self.in_string:
                if self.escaped:
                    self.escaped = False
                elif c == "\\":
                    self.escaped = True
                elif c == '"':
                    self.in_string = False
                continue
            if c == '"':
                self.in_string = True
                continue
            if not self.in_array:
                if c == "[":
                    self.in_array = True
                continue
            if c == "{":
                if self.depth == 0:
                    self.start = self.i - 1
                self.depth += 1
            elif c == "}":
                if self.depth > 0:
                    self.depth -= 1
                    if self.depth == 0 and self.start is not None:
                        out.append(self.buf[self.start:self.i])
                        self.start = None
            elif c == "]" and self.depth == 0:
                self.in_array = False
        return out


def stream_quiz_ndjson(level_label, review_points, n, focus, theme, avoid, hist_key):
    """Générateur SYNCHRONE (exécuté en threadpool par Starlette) : UN seul appel OpenAI en
    streaming, qui émet chaque question validée en NDJSON dès qu'elle est prête.
    Un flux unique s'auto-diversifie (le modèle voit ses propres questions) -> questions
    variées et dans le thème, là où des chunks parallèles entreraient en collision."""
    payload = build_quiz_payload(level_label, review_points, n, focus, theme, avoid, stream=True)
    headers = {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
    already = {quiz_norm(p) for p in avoid}
    extractor = _QuestionExtractor()
    kept_prompts = []
    sent = 0
    try:
        # timeout=(connexion, lecture) : une lecture qui stalle >45 s coupe proprement le flux.
        resp = requests.post(CHAT_URL, headers=headers, json=payload, timeout=(10, 45), stream=True)
        if resp.status_code != 200:
            yield json.dumps({"error": f"Erreur API OpenAI ({resp.status_code})."}) + "\n"
            return
        for line in resp.iter_lines():
            if not line:
                continue
            s = line.decode("utf-8", "ignore")
            if not s.startswith("data: "):
                continue
            s = s[6:]
            if s == "[DONE]":
                break
            try:
                delta = json.loads(s)["choices"][0]["delta"].get("content", "")
            except Exception:
                continue
            if not delta:
                continue
            for raw_obj in extractor.feed(delta):
                try:
                    q = json.loads(raw_obj)
                except Exception:
                    continue
                cleaned = clean_quiz_question(q, already)
                if cleaned is None:
                    continue
                kept_prompts.append(cleaned["prompt"])
                sent += 1
                yield json.dumps(cleaned, ensure_ascii=False) + "\n"
                if sent >= n:
                    break
            if sent >= n:
                break
        if sent == 0:
            yield json.dumps({"error": "Aucune question générée, réessayez."}) + "\n"
    except requests.RequestException as exc:
        logger.warning("Stream quiz interrompu : %s", exc)
        if sent == 0:
            yield json.dumps({"error": "Erreur réseau pendant la génération."}) + "\n"
    finally:
        if kept_prompts:
            remember_quiz_prompts(hist_key, kept_prompts)


def resolve_quiz_request(body: dict) -> dict:
    """Extrait et normalise les paramètres communs aux deux routes de quiz."""
    level = body.get("level", "A1")
    if level not in LEVELS:
        level = "A1"
    try:
        n = int(body.get("n", 8))
    except (TypeError, ValueError):
        n = 8
    n = max(5, min(30, n))
    focus = body.get("focus") or None                 # "expressions" pour le Quiz
    theme = (body.get("theme") or "").strip() or None  # thème pour Word Rush

    # On agrège les points "à retravailler" — mais PAS en mode thème (qui doit rester sur son thème).
    review = []
    if not theme:
        progress = load_progress()
        for course in progress.get("courses", {}).values():
            if course.get("level") == level:
                for pt in course.get("to_review", []):
                    if pt and pt not in review:
                        review.append(pt)

    hist_key = quiz_history_key(focus, theme, level)

    # Anti-redondance : on interdit au modèle de re-poser
    #  (a) les questions déjà chargées dans la partie EN COURS (`exclude`, envoyé par le
    #      front lors d'un réapprovisionnement -> évite les répétitions intra-partie), puis
    #  (b) les questions des parties précédentes (historique, du plus récent au plus ancien).
    exclude = [str(x).strip() for x in (body.get("exclude") or []) if str(x).strip()][:QUIZ_EXCLUDE_MAX]
    avoid = list(exclude)
    seen = {p.lower() for p in avoid}
    for p in reversed(get_recent_quiz_prompts(hist_key)):
        if len(avoid) >= QUIZ_AVOID_CAP:
            break
        if p.lower() not in seen:
            seen.add(p.lower())
            avoid.append(p)

    return {
        "level": level,
        "level_label": LEVELS[level]["label"],
        "n": n,
        "focus": focus,
        "theme": theme,
        "review": review[:8],
        "hist_key": hist_key,
        "avoid": avoid,
    }


async def create_quiz_stream(request: Request) -> StreamingResponse:
    """Route STREAMING : émet les questions en NDJSON dès qu'elles sont prêtes
    (1ère question en ~2 s au lieu d'attendre tout le lot)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    p = resolve_quiz_request(body)
    gen = stream_quiz_ndjson(
        p["level_label"], p["review"], p["n"], p["focus"], p["theme"], p["avoid"], p["hist_key"]
    )
    return StreamingResponse(
        gen,
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def create_quiz(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}

    p = resolve_quiz_request(body)
    level = p["level"]
    level_label = p["level_label"]
    n = p["n"]
    focus = p["focus"]
    theme = p["theme"]
    review = p["review"]
    hist_key = p["hist_key"]
    avoid = p["avoid"]

    # Génération en PARALLÈLE : un seul gros appel coûte ~2 s/question (séquentiel).
    # On découpe en plusieurs appels concurrents -> la latence devient celle d'UN chunk.
    # Les chunks partagent thème + interdits, donc ils se chevauchent : on sur-génère
    # (~30 %) pour qu'après dédoublonnage il reste bien n questions.
    target = math.ceil(n / 0.7)
    n_chunks = max(1, min(QUIZ_MAX_CHUNKS, math.ceil(target / QUIZ_CHUNK_SIZE)))
    base, rem = divmod(target, n_chunks)
    sizes = [base + (1 if i < rem else 0) for i in range(n_chunks)]
    avoid_tail = avoid[-QUIZ_AVOID_IN_PROMPT:]

    responses = await asyncio.gather(
        *(
            run_in_threadpool(request_quiz, level_label, review[:8], sz, focus, theme, avoid_tail)
            for sz in sizes
        ),
        return_exceptions=True,
    )

    # On agrège les questions de tous les chunks qui ont réussi ; une panne partielle
    # n'empêche pas la partie de démarrer avec les chunks valides.
    raw: list = []
    timeouts = 0
    had_error = False
    for r in responses:
        if isinstance(r, requests.Timeout):
            timeouts += 1
            continue
        if isinstance(r, Exception):
            had_error = True
            logger.warning("Chunk quiz échoué : %s", r)
            continue
        if r.status_code != 200:
            had_error = True
            logger.warning("Chunk quiz API %s : %s", r.status_code, r.text[:200])
            continue
        try:
            data = json.loads(r.json()["choices"][0]["message"]["content"])
            raw.extend(data.get("questions", []) or [])
        except Exception:
            had_error = True
            logger.exception("Chunk quiz non parsable")

    if not raw:
        if timeouts and not had_error:
            return JSONResponse({"error": "OpenAI ne répond pas (timeout)."}, status_code=504)
        return JSONResponse({"error": "Aucune question générée, réessayez."}, status_code=502)

    # Nettoyage défensif + dédoublonnage (au sein du lot ET vis-à-vis des parties récentes).
    already = {quiz_norm(p) for p in avoid}
    questions = []
    for q in raw:
        cleaned = clean_quiz_question(q, already)
        if cleaned is not None:
            questions.append(cleaned)

    if not questions:
        return JSONResponse({"error": "Aucune question générée, réessayez."}, status_code=502)

    # Les chunks parallèles peuvent déborder de quelques questions -> on tronque à n.
    questions = questions[:n]

    # On mémorise ces questions pour que les prochaines parties soient différentes.
    remember_quiz_prompts(hist_key, [q["prompt"] for q in questions])

    return JSONResponse({"level": level, "level_label": level_label, "questions": questions})


# =========================================================
# TRADUCTION au clic (mot/expression depuis les sous-titres)
# =========================================================

TRANSLATE_SYSTEM = (
    "Tu es un traducteur anglais → français concis, pour un apprenant francophone. "
    "On te donne un MOT ou une EXPRESSION en anglais, et la phrase complète où il apparaît (contexte). "
    "Donne la traduction française la plus juste DANS CE CONTEXTE. Si c'est une expression idiomatique, "
    "donne l'équivalent idiomatique français. "
    "Réponds STRICTEMENT en JSON : "
    '{"translation": "<traduction française courte>", '
    '"note": "<précision TRÈS courte et optionnelle : nature (verbe, nom…) ou nuance de sens ; sinon \\"\\">"}.'
)


def request_translate(text: str, context: str) -> requests.Response:
    user_msg = f"À traduire : « {text} »\nContexte (phrase complète) : {context or '(aucun)'}"
    payload = {
        "model": SUMMARY_MODEL,
        "messages": [
            {"role": "system", "content": TRANSLATE_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }
    headers = {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
    return requests.post(CHAT_URL, headers=headers, json=payload, timeout=15)


async def translate_word(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Corps JSON invalide."}, status_code=400)

    text = (body.get("text") or "").strip()
    context = (body.get("context") or "").strip()
    if not text:
        return JSONResponse({"error": "Texte vide."}, status_code=400)

    try:
        resp = await run_in_threadpool(request_translate, text, context)
    except requests.Timeout:
        return JSONResponse({"error": "OpenAI ne répond pas (timeout)."}, status_code=504)
    except requests.RequestException as exc:
        return JSONResponse({"error": f"Erreur réseau : {exc}"}, status_code=502)

    if resp.status_code != 200:
        return JSONResponse({"error": f"Erreur API OpenAI : {resp.text}"}, status_code=resp.status_code)

    try:
        data = json.loads(resp.json()["choices"][0]["message"]["content"])
    except Exception:
        return JSONResponse({"error": "Réponse illisible."}, status_code=502)

    return JSONResponse(
        {"translation": (data.get("translation") or "").strip(), "note": (data.get("note") or "").strip()}
    )


# =========================================================
# INVITÉ SURPRISE (Raj) — état + consommation
# =========================================================

async def bonus_state(request: Request) -> JSONResponse:
    """Indique si l'invité surprise est disponible (+ sa fiche)."""
    progress = load_progress()
    raj = CHARACTERS["raj"]
    return JSONResponse(
        {
            "available": bool(progress.get("bonus_available")),
            "character": {
                "id": "raj",
                "name": raj["name"],
                "title": raj["title"],
                "avatar": raj["avatar"],
                "tagline": raj["tagline"],
            },
        }
    )


async def bonus_consume(request: Request) -> JSONResponse:
    """L'invité a été rencontré : il disparaît jusqu'au prochain déblocage."""
    with _progress_lock:
        progress = load_progress()
        progress["bonus_available"] = False
        save_progress(progress)
    return JSONResponse({"ok": True})


# Table de routage : associe chaque URL à sa fonction. L'ORDRE compte —
# les routes /api/* sont déclarées AVANT le Mount("/") qui sert les fichiers
# du front-end, sinon le Mount "attraperait" aussi les appels /api.
routes = [
    Route("/api/config", get_config, methods=["GET"]),
    Route("/api/token", create_token, methods=["POST"]),
    Route("/api/summary", create_summary, methods=["POST"]),
    # Mode cours
    Route("/api/profile", get_profile, methods=["GET"]),
    Route("/api/course", get_course_state, methods=["GET"]),
    Route("/api/course/last", get_last_course, methods=["GET"]),
    Route("/api/course/token", create_course_token, methods=["POST"]),
    Route("/api/course/save", save_course, methods=["POST"]),
    Route("/api/course/finish", finish_course, methods=["POST"]),
    # Évaluation de niveau (examinateur David)
    Route("/api/assessment/token", create_assessment_token, methods=["POST"]),
    Route("/api/assessment/finish", finish_assessment, methods=["POST"]),
    Route("/api/progress", get_full_progress, methods=["GET"]),
    Route("/api/progress/reset", reset_progress, methods=["POST"]),
    # Mode jeu
    Route("/api/game/quiz", create_quiz, methods=["POST"]),
    Route("/api/game/quiz/stream", create_quiz_stream, methods=["POST"]),
    # Traduction au clic
    Route("/api/translate", translate_word, methods=["POST"]),
    # Carnet d'erreurs (révision espacée)
    Route("/api/errors/add", errors_add, methods=["POST"]),
    Route("/api/errors", errors_overview, methods=["GET"]),
    Route("/api/errors/session", errors_session, methods=["GET"]),
    Route("/api/errors/result", errors_result, methods=["POST"]),
    # Gamification
    Route("/api/gamify", gamify_state, methods=["GET"]),
    Route("/api/gamify", gamify_event, methods=["POST"]),
    # Invité surprise
    Route("/api/bonus", bonus_state, methods=["GET"]),
    Route("/api/bonus/consume", bonus_consume, methods=["POST"]),
    # Le front-end statique est servi à la racine (index.html, JS, CSS).
    Mount("/", app=StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend"),
]

app = Starlette(debug=True, routes=routes)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.server:app", host="127.0.0.1", port=8000, reload=False)
