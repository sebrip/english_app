<div align="center">

# 🎙️ English SpeakApp

### Apprends l'anglais en **parlant pour de vrai**.

Application web de pratique de l'anglais à l'oral, propulsée par l'**API OpenAI Realtime** :
conversations vocales en temps réel, cours particuliers avec des profs qui se souviennent
de toi, et mini-jeux de vocabulaire — le tout dans une PWA installable.

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![Starlette](https://img.shields.io/badge/Starlette-ASGI-0a0c18)
![OpenAI](https://img.shields.io/badge/OpenAI-Realtime%20API-412991?logo=openai&logoColor=white)
![JavaScript](https://img.shields.io/badge/JavaScript-vanilla-F7DF1E?logo=javascript&logoColor=black)
![PWA](https://img.shields.io/badge/PWA-installable-5A0FC8?logo=pwa&logoColor=white)

</div>

---

## ▶️ Démo

🎬 **[Voir la vidéo de visite guidée](demo_video/english_speakapp_visite_guidee.mp4)** *(1 min 53)*

> *(Astuce : sur GitHub la vidéo se télécharge. Pour une lecture en ligne, voir le lien de démo de mon portfolio.)*

---

## ✨ Fonctionnalités

- **💬 Conversation libre** — discute à la voix avec différents personnages, dans le décor de ton choix.
- **🎓 Cours d'anglais** — leçons avec des profs au caractère distinct (John, Marcus, Brenda, Zoe, Lucy…) qui **mémorisent ta progression** d'une séance à l'autre : reprise d'une leçon en pause, rattrapage ciblé sur tes points faibles, bilan noté à la fin.
- **🎯 Test de niveau** — un examinateur estime ton niveau CEFR (débutant → C2).
- **🎮 Mini-jeux** — Quiz d'expressions, Word Rush (chrono + combos), Carnet d'erreurs (révision espacée).
- **🏆 Gamification** — XP, niveaux, badges, séries (streaks) et célébrations animées.
- **📲 PWA installable** — « Ajouter à l'écran d'accueil », plein écran sur mobile, fonctionne hors-ligne pour la coquille de l'app.
- **🔊 Retours sensoriels** — sons synthétisés (Web Audio, **aucun fichier audio**), vibrations, animations « juice ».

---

## 🛠️ Points techniques notables

Ce projet va au-delà du « CRUD » : il manipule de l'**audio temps réel bas niveau** et plusieurs optimisations concrètes.

- **🎧 Pipeline audio temps réel** — capture micro → `AudioWorklet` (PCM16, 24 kHz) → **WebSocket** vers OpenAI, et décodage + lecture audio planifiée dans l'autre sens. Détection de parole, animation d'avatar pilotée par l'analyse du volume en direct.
- **⚡ Streaming des questions en NDJSON** — les quiz s'affichent dès la **1ʳᵉ question (~2-3 s)** au lieu d'attendre tout le lot (~20-30 s avant). Génération en un flux unique, extraction JSON incrémentale.
- **🧠 Mémoire des cours** — résumé glissant (*rolling summary*) par prof/niveau, reprise exacte d'une leçon interrompue, et anti-redondance des questions (historique FIFO injecté dans le prompt).
- **🚫 Backend async non bloquant** — les appels réseau (OpenAI) passent par `run_in_threadpool` pour ne **jamais geler la boucle asyncio** (sinon le serveur devient injoignable sous charge).
- **🪶 Front « zéro framework »** — HTML/CSS/JavaScript *vanilla*, modules ES, architecture par écrans. Léger, lisible, sans dépendance.
- **📦 PWA soignée** — service worker (navigation *network-first*, statiques *stale-while-revalidate*, `/api/` jamais caché), manifest, icônes générées, versionnage de cache.
- **🔐 Sécurité** — la clé OpenAI n'atteint **jamais** le navigateur : seuls des **jetons de session éphémères** sont émis côté serveur.

---

## 🧱 Stack

| Côté | Techno |
|------|--------|
| Backend | Python 3.11+ · [Starlette](https://www.starlette.io/) (ASGI) · Uvicorn |
| Frontend | HTML / CSS / JS *vanilla* · Web Audio API · `AudioWorklet` · Service Worker (PWA) |
| Voix | **API OpenAI Realtime** (WebSocket, audio PCM16) |
| Évaluations & quiz | Modèles texte OpenAI (Chat Completions, sorties JSON / streaming) |

---

## 🚀 Lancer le projet en local

### Prérequis
- **Python 3.11+** (testé sur 3.14)
- Une **clé API OpenAI** avec accès à l'API Realtime

### Installation
```bash
git clone <url-du-repo>
cd english_app

python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS / Linux

pip install -r requirements.txt
```

### Configurer la clé OpenAI
```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```
Puis édite `.streamlit/secrets.toml` :
```toml
OPENAI_API_KEY = "sk-..."
```
> 🔒 Ce fichier est **ignoré par git** : ta clé ne sera jamais committée. *(Alternative : variable d'environnement `OPENAI_API_KEY`, prioritaire.)*

### Démarrer
```bash
python -m uvicorn backend.server:app --host 127.0.0.1 --port 8000
```
➡️ Ouvre **http://127.0.0.1:8000/** *(sous Windows, `start.bat` lance le serveur et ouvre Chrome).*

### 📱 Tester sur smartphone
Le micro exige un **contexte HTTPS**. Tunnel Cloudflare (gratuit) :
```bash
cloudflared tunnel --url http://127.0.0.1:8000 --no-autoupdate
```
Ouvre l'URL `https://...trycloudflare.com` sur ton téléphone. La clé reste côté serveur.

---

## 📂 Structure

```
english_app/
├── backend/
│   └── server.py            # serveur Starlette : API + fichiers statiques + logique métier
├── frontend/
│   ├── index.html           # toute l'UI (écrans en sections)
│   ├── app.js               # logique UI (module ES)
│   ├── realtime.js          # moteur audio temps réel (micro ↔ WebSocket OpenAI)
│   ├── styles.css
│   ├── sw.js                # service worker (PWA)
│   └── avatars/ decors/ icons/
├── data/                    # données utilisateur (créées au runtime, hors dépôt)
├── demo_video/              # script + vidéo de visite guidée
└── requirements.txt
```

---

## 🔒 Sécurité & confidentialité

- Clé OpenAI **jamais exposée** au client (jetons éphémères uniquement).
- `secrets.toml` et le dossier `data/` (progression personnelle) sont **exclus du dépôt** (`.gitignore`).
- Application **mono-utilisateur** : données stockées localement dans `data/progress.json`.

---

<div align="center">

*Projet personnel — développé pour explorer l'audio temps réel et l'API OpenAI Realtime.*

</div>
