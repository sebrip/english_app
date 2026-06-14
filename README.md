# 🎙️ English SpeakApp

**Apprends l'anglais en parlant pour de vrai.** Une application web de pratique de
l'anglais à l'oral, propulsée par l'API **OpenAI Realtime** : conversations vocales
en temps réel, cours particuliers avec des profs qui se souviennent de toi, et
mini-jeux de vocabulaire.

> Application mono-utilisateur, pensée pour tourner en local (et sur smartphone via
> un tunnel HTTPS). Interface en français, pratique de l'anglais.

---

## ✨ Fonctionnalités

- **💬 Conversation libre** — discute à la voix avec différents personnages, dans le décor de ton choix.
- **🎓 Cours d'anglais** — leçons avec des profs au caractère distinct (John, Marcus, Brenda, Zoe, Lucy…), qui **mémorisent ta progression** d'une séance à l'autre. Reprise d'une leçon en pause, rattrapage ciblé, bilan noté à la fin.
- **🎯 Test de niveau** — un examinateur estime ton niveau CEFR (débutant → C2).
- **🎮 Mini-jeux** — Quiz d'expressions, Word Rush (chrono + combos), Carnet d'erreurs (révision espacée).
- **🏆 Gamification** — XP, niveaux, badges, séries (streaks), célébrations animées.
- **📲 PWA installable** — « Ajouter à l'écran d'accueil », fonctionne en plein écran sur mobile.
- **🔊 Retours sensoriels** — sons synthétisés (WebAudio), vibrations, animations « juice ».

---

## 🧱 Stack technique

| Côté | Techno |
|------|--------|
| Backend | Python 3.11+ · [Starlette](https://www.starlette.io/) · Uvicorn |
| Frontend | HTML/CSS/JS « vanilla » (aucun framework), Web Audio API, Service Worker (PWA) |
| Voix | API **OpenAI Realtime** (WebSocket, audio PCM16) |
| Évaluations / quiz | Modèles texte OpenAI (Chat Completions) |

---

## 🚀 Installation & lancement

### 1. Prérequis
- **Python 3.11 ou plus** (testé sur 3.14)
- Une **clé API OpenAI** avec accès à l'API Realtime

### 2. Récupérer le projet et installer les dépendances
```bash
git clone <url-du-repo>
cd english_app

python -m venv venv
# Windows :
venv\Scripts\activate
# macOS / Linux :
source venv/bin/activate

pip install -r requirements.txt
```

### 3. Configurer la clé OpenAI
Copie le modèle puis renseigne ta clé :
```bash
# dans le dossier .streamlit/
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```
Édite `.streamlit/secrets.toml` et remplace la valeur par ta vraie clé :
```toml
OPENAI_API_KEY = "sk-..."
```
> 🔒 Ce fichier est **ignoré par git** : ta clé ne sera jamais committée.
> Alternative : définir la variable d'environnement `OPENAI_API_KEY` (prioritaire).

### 4. Lancer le serveur
```bash
python -m uvicorn backend.server:app --host 127.0.0.1 --port 8000
```
Puis ouvre **http://127.0.0.1:8000/**.

> 💡 Sous Windows, un double-clic sur **`start.bat`** lance le serveur et ouvre Chrome automatiquement.

---

## 📱 Tester sur smartphone

Le micro (`getUserMedia`) exige un **contexte sécurisé (HTTPS)** : `http://<ip-locale>:8000`
ne fonctionne donc pas sur téléphone. Solution simple avec un tunnel **Cloudflare** (gratuit) :

```bash
cloudflared tunnel --url http://127.0.0.1:8000 --no-autoupdate
```
Une URL `https://...trycloudflare.com` s'affiche : ouvre-la sur ton téléphone. La clé
OpenAI reste côté serveur (le navigateur ne reçoit que des jetons éphémères).

---

## 📂 Structure

```
english_app/
├── backend/
│   ├── server.py            # serveur Starlette : API + service des fichiers statiques
│   ├── generate_avatars.py  # génération des avatars (Pillow)
│   └── generate_decors.py   # génération des décors (Pillow)
├── frontend/
│   ├── index.html           # toute l'UI (écrans en sections)
│   ├── app.js               # logique UI (module ES)
│   ├── realtime.js          # moteur audio temps réel (micro ↔ WebSocket OpenAI)
│   ├── styles.css
│   ├── sw.js                # service worker (PWA)
│   ├── manifest.webmanifest
│   ├── avatars/ decors/ icons/
├── data/                    # données utilisateur (créées au runtime, ignorées par git)
├── demo_video/              # script + vidéo de visite guidée
├── requirements.txt
└── .streamlit/secrets.toml  # ta clé OpenAI (ignoré par git — voir .example)
```

---

## 🔒 Sécurité & confidentialité

- La **clé OpenAI** n'est jamais exposée au navigateur : seuls des **jetons de session
  éphémères** sont envoyés au client.
- `secrets.toml` et le dossier `data/` (progression personnelle) sont **exclus du dépôt**
  via `.gitignore`.
- Application **mono-utilisateur** : les données sont stockées localement dans `data/progress.json`.

---

## 📝 Licence

Projet personnel. Tous droits réservés.
