/* =========================================================================
 * app.js — CHEF D'ORCHESTRE DE L'INTERFACE
 * -------------------------------------------------------------------------
 * Ce fichier gère TOUT ce que l'utilisateur voit et clique :
 *
 *   1. Il charge la config (personnages/décors) depuis le back-end.
 *   2. Il navigue entre 4 "écrans" : accueil -> réglages -> appel -> bilan.
 *   3. Pendant l'appel, il délègue l'audio/WebSocket à RealtimeSession
 *      (fichier realtime.js) et réagit à ses évènements (callbacks).
 *   4. Il détecte la fin de conversation ("au revoir") et affiche le bilan /10.
 *
 * Tout l'audio temps réel est dans realtime.js ; ici c'est purement l'UI.
 * ========================================================================= */

import { RealtimeSession } from "./realtime.js?v=9";

// "Mémoire" globale de l'app : les choix de l'utilisateur + la session en cours.
const state = {
  config: null,        // contenu reçu du back-end (personnages, décors, niveaux)
  character: null,     // id du personnage choisi ("john" / "marcus")
  situation: null,     // id du décor choisi ("bakery" / "pub" / "nyc")
  level: "beginner",   // niveau déclaré
  session: null,       // l'objet RealtimeSession actif (ou null)
};

// Les 4 sections <section> de index.html. On en affiche UNE à la fois (cf. show()).
const screens = {
  menu: document.getElementById("screen-menu"),
  home: document.getElementById("screen-home"),
  setup: document.getElementById("screen-setup"),
  call: document.getElementById("screen-call"),
  summary: document.getElementById("screen-summary"),
  "course-home": document.getElementById("screen-course-home"),
  "course-setup": document.getElementById("screen-course-setup"),
  progress: document.getElementById("screen-progress"),
  lesson: document.getElementById("screen-lesson"),
  "lesson-summary": document.getElementById("screen-lesson-summary"),
  assessment: document.getElementById("screen-assessment"),
  "assessment-summary": document.getElementById("screen-assessment-summary"),
  "games-hub": document.getElementById("screen-games-hub"),
  "game-home": document.getElementById("screen-game-home"),
  game: document.getElementById("screen-game"),
  "game-over": document.getElementById("screen-game-over"),
  "wordrush-home": document.getElementById("screen-wordrush-home"),
  wordrush: document.getElementById("screen-wordrush"),
  errorbook: document.getElementById("screen-errorbook"),
  review: document.getElementById("screen-review"),
  profile: document.getElementById("screen-profile"),
};

// Raccourci de test : ouvrir l'app avec ?dev=1 débloque "Terminer la leçon" après ~30 s
// (au lieu d'attendre la durée cible). À retirer/ignorer en usage normal.
const DEV_QUICK = new URLSearchParams(location.search).get("dev") === "1";

// Formules de fin de conversation (apprenant). Insensible à la casse.
// Volontairement strict : on EXCLUT le « bye » seul (whisper l'hallucine sur du
// bruit) et les salutations type « nice to see you » (lookbehind sur "to").
const GOODBYE_RE = new RegExp(
  "\\b(good\\s?bye|bye[\\s-]bye|(?<!\\bto\\s)see\\s+(you|ya)(\\s+(later|soon|around|tomorrow))?|" +
  "farewell|take\\s+care|catch\\s+you\\s+later|" +
  "have\\s+a\\s+(nice|good|great|lovely)\\s+(day|night|evening|one)|" +
  "i\\s+(have|gotta|got|need)\\s+to\\s+(go|leave|run|head\\s+out)|" +
  "i'?m\\s+(leaving|gonna\\s+go|gonna\\s+head\\s+out)|i\\s+(should|better|gotta)\\s+(go|get\\s+going)|" +
  "that'?s\\s+(all|it\\s+for\\s+(today|now))|au\\s?revoir|à\\s?bientôt)\\b",
  "i"
);

// Phrases fréquemment "hallucinées" par whisper sur silence/bruit -> jamais une vraie fin.
const HALLUCINATIONS = new Set([
  "bye", "thank you", "thanks", "thanks for watching", "you", "merci",
  "thank you very much", "thank you for watching", "merci d'avoir regardé",
]);

function normalizeText(t) {
  return (t || "").toLowerCase().replace(/[.,!?¡¿…]+/g, "").trim();
}

function isGoodbye(rawText) {
  const norm = normalizeText(rawText);
  if (!norm) return false;
  if (HALLUCINATIONS.has(norm)) return false;
  return GOODBYE_RE.test(rawText);
}

// Affiche l'écran demandé et cache les 3 autres (via la classe CSS "active").
function show(name) {
  for (const [key, el] of Object.entries(screens)) {
    el.classList.toggle("active", key === name);
  }
  if (name === "menu") {
    refreshMenuGami();        // bandeau XP/streak
    refreshMenuBonus();       // cadeau "invité surprise" si débloqué
    refreshMenuAssessment();  // reco "test de niveau" + curseur de niveau
    refreshResumeBars();      // "Reprendre le dernier cours" si une leçon est en pause
  }
}

// =========================================================
// Overlay "Connexion perdue" — filet de sécurité réseau
// -------------------------------------------------------------------------
// Affiché UNIQUEMENT sur une coupure SUBIE (réseau coupé, token expiré),
// jamais sur un raccrochage volontaire. Deux choix clairs, dont le sens est
// adapté à chaque mode (conversation / leçon / évaluation) par l'appelant.
// =========================================================
function hideDisconnect() {
  document.getElementById("disconnect-overlay").hidden = true;
}
function showDisconnect({ message, retryLabel, onRetry, finishLabel, onFinish, title, icon }) {
  // Titre/icône par défaut = coupure réseau ; surchargeables (ex. alerte micro).
  document.getElementById("disconnect-title").textContent = title || "Connexion perdue";
  document.getElementById("disconnect-icon").textContent = icon || "📡";
  document.getElementById("disconnect-msg").textContent = message;
  // On remplace les boutons par des clones neufs : ça purge les éventuels
  // listeners d'une coupure précédente (aucune accumulation, aucun double-clic
  // fantôme). Chaque action ferme l'overlay AVANT d'agir.
  const swap = (id, label, handler) => {
    const old = document.getElementById(id);
    const fresh = old.cloneNode(true);
    fresh.textContent = label;
    old.parentNode.replaceChild(fresh, old);
    fresh.addEventListener("click", () => { hideDisconnect(); handler(); }, { once: true });
  };
  swap("disconnect-retry", retryLabel, onRetry);
  swap("disconnect-finish", finishLabel, onFinish);
  document.getElementById("disconnect-overlay").hidden = false;
}

// Échec du démarrage d'une session vocale. Erreur micro (absent / refusé / occupé)
// -> alerte claire avec "Réessayer". Autre erreur -> message d'état technique.
function showStartError(e, { setStatus, onRetry, onBack }) {
  if (e && e.kind === "mic") {
    showDisconnect({
      title: "Micro indisponible",
      icon: "🎤",
      message: e.message,
      retryLabel: "🔄 Réessayer",
      onRetry,
      finishLabel: "← Retour",
      onFinish: onBack,
    });
  } else if (setStatus) {
    setStatus("❌ " + e.message);
  }
}

// Applique l'état de la carte "test de niveau" : masquée si déjà évalué (David se
// range alors dans la page des cours), sinon mise en avant brillante (halo).
function applyAssessmentCard(card, assessed) {
  if (assessed) {
    card.classList.remove("first-time");
    card.hidden = true;
    return;
  }
  document.getElementById("ar-tag").textContent = "⭐ Recommandé pour bien démarrer";
  document.getElementById("ar-title").textContent = "Test de niveau · 10 min";
  document.getElementById("ar-desc").textContent =
    "Découvre ton niveau réel (du débutant au C2) avec David, l'examinateur. C'est optionnel, mais c'est le meilleur point de départ.";
  document.getElementById("ar-cta").textContent = "Démarrer →";
  card.classList.add("first-time");
  card.hidden = false;
}

// Carte de recommandation du test de niveau sur le menu. Mise en avant à la
// PREMIÈRE utilisation (aucune évaluation faite) ; disparaît une fois l'éval faite.
// Affichage IMMÉDIAT (dernier état connu en localStorage) pour ne pas dépendre de la
// latence réseau, puis confirmation par le serveur — sans clignotement.
async function refreshMenuAssessment() {
  const card = document.getElementById("assessment-reco");
  if (!card) return;
  // Avatar de David (depuis la config si dispo).
  const david = (state.config && state.config.characters && state.config.characters.david) || null;
  if (david) document.getElementById("ar-avatar").src = david.avatar;

  // 1) Rendu instantané sur la base du dernier état connu (évite l'attente du fetch).
  applyAssessmentCard(card, localStorage.getItem("assessed") === "1");

  // 2) Confirmation serveur : on ajuste seulement si l'état réel diffère.
  try {
    const p = await (await fetch("/api/profile")).json();
    state.assessedLevel = p.assessed_level || "";
    state.assessedLabel = p.assessed_label || "";
    const assessed = !!p.assessed_level;
    localStorage.setItem("assessed", assessed ? "1" : "0");
    applyAssessmentCard(card, assessed);
  } catch (_) {
    // Hors-ligne : on conserve l'affichage issu du dernier état connu.
  }
}

// Affiche/masque le cadeau brillant de l'invité surprise sur le menu.
async function refreshMenuBonus() {
  const gift = document.getElementById("menu-gift");
  try {
    const data = await (await fetch("/api/bonus")).json();
    gift.hidden = !data.available;
  } catch (_) {
    gift.hidden = true;
  }
}

// =========================================================
// GAMIFICATION — bandeau menu, profil, toasts XP/badges
// =========================================================
async function refreshMenuGami() {
  try {
    const g = await (await fetch("/api/gamify")).json();
    document.getElementById("gami-level").textContent = g.level;
    document.getElementById("gami-streak").textContent = g.streak;
    const pct = g.xp_for_next ? Math.round((g.xp_in_level / g.xp_for_next) * 100) : 0;
    document.getElementById("gami-bar-fill").style.width = pct + "%";
  } catch (_) {}
}

// =========================================================
// REPRENDRE LE DERNIER COURS — barre sur l'accueil + écran des profs
// =========================================================
// Récupère la dernière leçon en pause et alimente les deux barres "Reprendre".
async function refreshResumeBars() {
  let info = null;
  try {
    info = await (await fetch("/api/course/last")).json();
  } catch (_) {
    info = null; // hors-ligne : on masque simplement la barre
  }
  applyResumeBar("menu", info);
  applyResumeBar("home", info);
}

// Affiche/masque + remplit une barre "Reprendre" (suffix = "menu" ou "home").
function applyResumeBar(suffix, info) {
  const bar = document.getElementById("resume-course-" + suffix);
  if (!bar) return;
  const screenEl =
    suffix === "menu" ? screens.menu : document.getElementById("screen-course-home");
  const has = !!(info && info.has_resume);
  bar.hidden = !has;
  // La classe réserve l'espace en bas sur mobile (barre fixée) pour ne rien masquer.
  if (screenEl) screenEl.classList.toggle("resume-active", has);
  bar._resumeInfo = has ? info : null;
  if (!has) return;
  const img = bar.querySelector(".rb-avatar img");
  if (img) { img.src = info.avatar || ""; img.alt = info.character_name || ""; }
  document.getElementById("rb-title-" + suffix).textContent =
    `${info.character_name} · ${info.level_label}` +
    (info.theme_label ? ` — ${info.theme_label}` : "");
  const mins = Math.round((info.elapsed_seconds || 0) / 60);
  document.getElementById("rb-sub-" + suffix).textContent =
    mins > 0
      ? `Déjà ${mins} min échangées · reprends où tu t'es arrêté`
      : "Reprends ta leçon là où tu t'es arrêté";
}

// Saute directement dans la dernière leçon (sans repasser par le setup).
function resumeLastCourse(info) {
  if (!info || !info.has_resume) return;
  state.courseCharacter = info.character;
  state.courseLevel = info.level;
  state.courseTarget = info.target_minutes || 10;
  state.courseTheme = info.theme || null;
  state.courseThemeLabel = info.theme_label || "";
  state.courseIsVocab = !!info.theme;
  startLesson();
}

async function openProfile() {
  show("profile");
  let g;
  try {
    g = await (await fetch("/api/gamify")).json();
  } catch (e) {
    return;
  }
  document.getElementById("pf-level").textContent = g.level;
  document.getElementById("pf-streak").textContent = g.streak;
  document.getElementById("pf-best").textContent = g.best_streak;
  document.getElementById("pf-mastered").textContent = g.mastered;
  const pct = g.xp_for_next ? Math.round((g.xp_in_level / g.xp_for_next) * 100) : 0;
  document.getElementById("pf-bar-fill").style.width = pct + "%";
  document.getElementById("pf-xp-text").textContent =
    `${g.xp} XP · encore ${g.to_next} pour le niveau ${g.level + 1}`;

  const grid = document.getElementById("pf-badges");
  grid.innerHTML = "";
  g.badges.forEach((b) => {
    const el = document.createElement("div");
    el.className = "badge" + (b.unlocked ? "" : " locked");
    el.innerHTML = `<div class="badge-emoji">${b.emoji}</div><div class="badge-label">${b.label}</div>`;
    grid.appendChild(el);
  });
  updateSoundToggle();
}

// Petit toast (XP gagné + badges débloqués). Réutilisé après jeux/leçons/révisions.
let toastEl = null;
let toastTimer = null;
function showXpToast(gami, prefix) {
  if (!gami) return;
  // Montée de niveau : confettis + fanfare de palier AVANT le toast. Centralisé ici
  // pour que TOUTES les sources (jeux, leçons, test de niveau) la déclenchent.
  if (gami.leveled_up) showLevelUp(gami.level);
  if (!toastEl) {
    toastEl = document.createElement("div");
    toastEl.className = "toast";
    document.body.appendChild(toastEl);
  }
  let html = `<div class="toast-xp">+${gami.xp_gained} XP</div>`;
  if (prefix) html = `<div>${prefix}</div>` + html;
  (gami.new_badges || []).forEach((b) => {
    html += `<div class="toast-badge">${b.emoji} Badge débloqué : ${b.label}</div>`;
  });
  toastEl.innerHTML = html;
  toastEl.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove("show"), 4200);
}

// Enregistre une activité de jeu/révision côté serveur, puis affiche le toast.
async function recordActivity(payload, prefix) {
  try {
    const gami = await (await fetch("/api/gamify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })).json();
    showXpToast(gami, prefix); // gère lui-même la montée de niveau (confettis + son)
  } catch (_) {}
}

// =========================================================
// FEEDBACK — sons, vibration, animations (montée de niveau + juice des jeux)
// =========================================================
const FX = {
  get muted() { return localStorage.getItem("fxMuted") === "1"; },
  set muted(v) { localStorage.setItem("fxMuted", v ? "1" : "0"); },
};

let _audioCtx = null;
function audioCtx() {
  if (_audioCtx === null) {
    try { _audioCtx = new (window.AudioContext || window.webkitAudioContext)(); }
    catch (_) { _audioCtx = false; }
  }
  if (_audioCtx && _audioCtx.state === "suspended") _audioCtx.resume().catch(() => {});
  return _audioCtx || null;
}

// Joue une courte séquence de notes (synthèse WebAudio — aucun fichier audio requis).
function tone(notes) {
  if (FX.muted) return;
  const ctx = audioCtx();
  if (!ctx) return;
  const t0 = ctx.currentTime;
  notes.forEach((n) => {
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.type = n.type || "sine";
    o.frequency.value = n.f;
    const start = t0 + (n.t || 0);
    const dur = n.d || 0.12;
    g.gain.setValueAtTime(0.0001, start);
    g.gain.linearRampToValueAtTime(n.v != null ? n.v : 0.16, start + 0.012);
    g.gain.exponentialRampToValueAtTime(0.0001, start + dur);
    o.connect(g); g.connect(ctx.destination);
    o.start(start); o.stop(start + dur + 0.02);
  });
}

function vibrate(pattern) {
  if (FX.muted || !navigator.vibrate) return;
  try { navigator.vibrate(pattern); } catch (_) {}
}

function fxCorrect() { tone([{ f: 660, d: 0.10 }, { f: 990, t: 0.07, d: 0.14 }]); vibrate(18); }
function fxWrong() { tone([{ f: 196, type: "sawtooth", d: 0.20, v: 0.13 }]); vibrate([25, 45, 25]); }
function fxLevelUp() {
  tone([{ f: 523, d: 0.14 }, { f: 659, t: 0.12, d: 0.14 },
         { f: 784, t: 0.24, d: 0.16 }, { f: 1047, t: 0.38, d: 0.30 }]);
  vibrate([0, 50, 40, 120]);
}
// Fin de cours RÉUSSI : fanfare vive et brillante (ondes triangle = plus de peps),
// petit "pop" d'accroche, rebond joyeux, puis accord final plein (basse + sparkle).
function fxLessonPass() {
  const L = "triangle"; // mélodie : plus claquant qu'une sine
  tone([
    { f: 880,  t: 0.00, d: 0.05, v: 0.13, type: L },   // pop d'accroche
    { f: 523,  t: 0.05, d: 0.12, v: 0.22, type: L },   // C5  ─┐ montée vive
    { f: 659,  t: 0.16, d: 0.12, v: 0.22, type: L },   // E5   │
    { f: 784,  t: 0.27, d: 0.12, v: 0.22, type: L },   // G5   │
    { f: 1047, t: 0.38, d: 0.16, v: 0.24, type: L },   // C6  ─┘
    { f: 784,  t: 0.55, d: 0.10, v: 0.18, type: L },   // G5  ─┐ rebond joyeux
    { f: 1047, t: 0.64, d: 0.42, v: 0.22, type: L },   // C6  ─┘ note finale (lead)
    // Accord final plein (do majeur) — sines propres + basse + sparkle aigu
    { f: 523,  t: 0.64, d: 0.44, v: 0.11 },            // C5
    { f: 659,  t: 0.64, d: 0.44, v: 0.11 },            // E5
    { f: 784,  t: 0.64, d: 0.44, v: 0.11 },            // G5
    { f: 131,  t: 0.64, d: 0.44, v: 0.13 },            // C3 (basse = du corps)
    { f: 1568, t: 0.66, d: 0.40, v: 0.08, type: L },   // G6 (sparkle)
  ]);
  vibrate([0, 45, 35, 45, 35, 120]);
}
// Fin de cours à RETRAVAILLER : deux notes montantes douces (encourageant, jamais "raté").
function fxLessonDone() {
  tone([{ f: 523, d: 0.14 }, { f: 659, t: 0.13, d: 0.22, v: 0.14 }]);
  vibrate(30);
}

// Overlay festif de montée de niveau + pluie de confettis.
function showLevelUp(level) {
  fxLevelUp();
  const ov = document.createElement("div");
  ov.className = "levelup";
  const colors = ["#ff7a59", "#6c8bff", "#3ddc84", "#ffd166", "#f2f3fb"];
  let confetti = "";
  for (let i = 0; i < 80; i++) {
    const c = colors[i % colors.length];
    confetti += `<i style="left:${(Math.random() * 100).toFixed(2)}%;` +
      `background:${c};animation-delay:${(Math.random() * 0.5).toFixed(2)}s;` +
      `animation-duration:${(1.6 + Math.random() * 1.4).toFixed(2)}s"></i>`;
  }
  ov.innerHTML = `<div class="confetti">${confetti}</div>
    <div class="levelup-card">
      <div class="levelup-burst">⭐</div>
      <div class="levelup-title">Niveau ${level} !</div>
      <div class="levelup-sub">Bravo, tu progresses 🎉</div>
    </div>`;
  document.body.appendChild(ov);
  const close = () => { ov.classList.add("out"); setTimeout(() => ov.remove(), 360); };
  ov.addEventListener("click", close);
  requestAnimationFrame(() => ov.classList.add("show"));
  setTimeout(close, 3200);
}

// Points qui s'envolent depuis un élément (juice Word Rush).
function flyPoints(anchorId, text) {
  const a = document.getElementById(anchorId);
  if (!a) return;
  const r = a.getBoundingClientRect();
  const el = document.createElement("div");
  el.className = "fly-points";
  el.textContent = text;
  el.style.left = (r.left + r.width / 2) + "px";
  el.style.top = r.top + "px";
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 950);
}

// (re)déclenche une animation CSS en retirant/réappliquant la classe.
function replayAnim(id, cls) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove(cls);
  void el.offsetWidth;
  el.classList.add(cls);
}

// Reflète l'état son/vibration dans l'écran Profil.
function updateSoundToggle() {
  const icon = document.getElementById("pf-sound-icon");
  const state = document.getElementById("pf-sound-state");
  if (!icon || !state) return;
  const muted = FX.muted;
  icon.textContent = muted ? "🔇" : "🔊";
  state.textContent = muted ? "Coupés" : "Activés";
  state.classList.toggle("off", muted);
}

// =========================================================
// TRADUCTION AU CLIC (mots/expressions des sous-titres)
// =========================================================
const translationCache = new Map();
let tipEl = null;
let tipTimer = null;

function ensureTip() {
  if (!tipEl) {
    tipEl = document.createElement("div");
    tipEl.className = "tip";
    tipEl.style.display = "none";
    document.body.appendChild(tipEl);
    // Clic ailleurs => on ferme la bulle (sauf clic sur un mot ou dans la bulle).
    document.addEventListener("click", (e) => {
      if (!tipEl.contains(e.target) && !(e.target.classList && e.target.classList.contains("word"))) {
        hideTip();
      }
    });
  }
  return tipEl;
}
function hideTip() {
  clearTimeout(tipTimer);
  if (tipEl) tipEl.style.display = "none";
}
function positionTip(rect) {
  const t = ensureTip();
  t.style.display = "block";
  let left = Math.max(8, Math.min(rect.left, window.innerWidth - t.offsetWidth - 8));
  let top = rect.top - t.offsetHeight - 8;
  if (top < 8) top = rect.bottom + 8; // pas de place au-dessus -> en dessous
  t.style.left = left + "px";
  t.style.top = top + "px";
}

async function translateAndShow(text, context, rect) {
  const clean = (text || "").trim();
  if (!clean) return;
  clearTimeout(tipTimer);
  const t = ensureTip();
  t.innerHTML = `<div class="tip-src">${clean}</div><div class="tip-fr">…</div>`;
  positionTip(rect);

  const key = clean.toLowerCase();
  let data = translationCache.get(key);
  if (!data) {
    try {
      const res = await fetch("/api/translate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: clean, context: context || "" }),
      });
      data = await res.json();
      if (!res.ok) throw new Error(data.error || "err");
      translationCache.set(key, data);
    } catch (e) {
      data = { translation: "(traduction indisponible)", note: "" };
    }
  }
  t.innerHTML =
    `<div class="tip-src">${clean}</div>` +
    `<div class="tip-fr">${data.translation || "—"}</div>` +
    (data.note ? `<div class="tip-note">${data.note}</div>` : "");
  positionTip(rect);
  // Auto-fermeture après 5 s si l'utilisateur ne fait rien.
  clearTimeout(tipTimer);
  tipTimer = setTimeout(hideTip, 5000);
}

// Transforme le texte d'une bulle en mots cliquables (à appeler quand la bulle est finie).
function makeClickableWords(el) {
  const text = el.textContent;
  el.dataset.full = text;
  el.textContent = "";
  for (const part of text.split(/(\s+)/)) {
    if (part === "" || /^\s+$/.test(part)) {
      el.appendChild(document.createTextNode(part));
      continue;
    }
    const span = document.createElement("span");
    span.className = "word";
    span.textContent = part;
    span.addEventListener("click", (e) => {
      const sel = window.getSelection();
      if (sel && !sel.isCollapsed) return; // une sélection multi-mots est gérée ailleurs
      e.stopPropagation();
      const clean = part.replace(/^[^\p{L}'’-]+|[^\p{L}'’-]+$/gu, ""); // retire la ponctuation
      translateAndShow(clean || part, text, span.getBoundingClientRect());
    });
    el.appendChild(span);
  }
}

// La bulle est en position fixe : dès que les sous-titres défilent (nouvelle réplique
// = auto-scroll) ou que la fenêtre bouge, on la ferme pour ne pas la laisser "flotter".
["transcript", "lesson-transcript"].forEach((id) => {
  const el = document.getElementById(id);
  if (el) el.addEventListener("scroll", hideTip);
});
window.addEventListener("scroll", hideTip, true);
window.addEventListener("resize", hideTip);

// Sélection de plusieurs mots (expression) dans une bulle -> on traduit la sélection.
document.addEventListener("mouseup", () => {
  const sel = window.getSelection();
  const s = sel ? sel.toString().trim() : "";
  if (!s || s.split(/\s+/).length < 2) return; // 0 ou 1 mot : géré par le clic
  let node = sel.anchorNode;
  while (node && node.nodeType !== 1) node = node.parentNode;
  const bubble = node && node.closest ? node.closest(".bubble") : null;
  if (!bubble) return;
  const rect = sel.getRangeAt(0).getBoundingClientRect();
  translateAndShow(s, bubble.dataset.full || bubble.textContent, rect);
});

// =========================================================
// Chargement de la config (personnages / décors) depuis le back-end
// =========================================================
async function loadConfig() {
  const res = await fetch("/api/config");
  if (!res.ok) throw new Error("Config indisponible");
  state.config = await res.json();
  renderCharacters();
  // L'avatar de David (carte "test de niveau" de l'accueil) vient de la config :
  // refreshMenuAssessment() a pu tourner AVANT son chargement (boot), donc on le pose
  // ici dès qu'elle est dispo, sinon la petite photo reste vide au premier affichage.
  const david = state.config.characters && state.config.characters.david;
  const arAvatar = document.getElementById("ar-avatar");
  if (david && arAvatar) arAvatar.src = david.avatar;
}

// Spécialité d'un prof -> classe de couleur de carte + texte du badge.
// Les profs de conversation générale (sans spécialité) gardent une carte neutre.
function specialtyOf(c) {
  if (c.beginner_only) return { cls: "char-card--beginner", badge: "🐣 Spécial grands débutants" };
  if (c.vocab_coach)   return { cls: "char-card--vocab",    badge: "🗂️ Vocabulaire par thème" };
  if (c.examiner)      return { cls: "char-card--exam",     badge: "🎓 Test de niveau · 10 min" };
  return { cls: "", badge: "" };
}

// Fabrique dynamiquement une carte cliquable par personnage sur l'écran d'accueil.
function renderCharacters() {
  const wrap = document.getElementById("character-cards");
  wrap.innerHTML = "";
  for (const [id, c] of Object.entries(state.config.characters)) {
    if (c.course_only) continue; // les profs réservés aux cours (ex: Lucy) n'apparaissent pas ici
    const sp = specialtyOf(c);
    const card = document.createElement("button");
    card.className = "char-card" + (sp.cls ? " " + sp.cls : "");
    card.innerHTML = `
      <div class="char-avatar"><img src="${c.avatar}" alt="${c.name}"></div>
      <div class="char-name">${c.name}</div>
      <div class="char-title">${c.title}</div>
      ${sp.badge ? `<span class="char-badge">${sp.badge}</span>` : ""}
      <p class="char-tagline">${c.tagline}</p>
      <span class="char-cta">Parler avec ${c.name} →</span>
    `;
    card.addEventListener("click", () => {
      state.character = id;
      goToSetup();
    });
    wrap.appendChild(card);
  }
}

// Ouvre l'accueil Conversation libre + affiche l'invité surprise s'il est débloqué.
async function openFreeHome() {
  show("home");
  const banner = document.getElementById("bonus-guest");
  banner.hidden = true;
  try {
    const data = await (await fetch("/api/bonus")).json();
    if (data.available && data.character) {
      const c = data.character;
      banner.innerHTML = `
        <img src="${c.avatar}" alt="${c.name}">
        <div class="bg-info">
          <div class="bg-tag">✨ Invité surprise débloqué</div>
          <div class="bg-name">${c.name} — ${c.title}</div>
          <div class="bg-desc">${c.tagline}</div>
        </div>
        <button id="bonus-start">Parler avec ${c.name} →</button>`;
      banner.hidden = false;
      document.getElementById("bonus-start").addEventListener("click", startBonusConversation);
    }
  } catch (_) {}
}

// Lance la conversation avec l'invité surprise (Raj), dans un décor décontracté.
function startBonusConversation() {
  state.character = "raj";
  state.situation = "pub";
  state.level = "B2"; // il parle naturellement
  startCall();
}

// L'invité a été rencontré -> on le consomme (il disparaît jusqu'au prochain déblocage).
function consumeBonusIfRaj() {
  if (state.character !== "raj") return;
  state.character = null; // évite toute re-consommation / re-déclenchement
  // Masquage IMMÉDIAT côté UI (ne pas attendre le serveur -> pas de cadeau fantôme).
  const gift = document.getElementById("menu-gift");
  if (gift) gift.hidden = true;
  const banner = document.getElementById("bonus-guest");
  if (banner) banner.hidden = true;
  fetch("/api/bonus/consume", { method: "POST" }).catch(() => {});
}

// =========================================================
// Écran de configuration (niveau + décor)
// =========================================================
function goToSetup() {
  const c = state.config.characters[state.character];
  document.getElementById("setup-avatar").src = c.avatar;
  document.getElementById("setup-charname").textContent = `${c.name} — ${c.title}`;

  // Niveaux (envoyés par le back-end sous forme {id, label}).
  // On crée une pastille par niveau ; cliquer mémorise l'id dans state.level.
  const levelWrap = document.getElementById("level-pills");
  levelWrap.innerHTML = "";
  // Profs réservés aux grands débutants (ex: Sophie) : un seul niveau proposé.
  const lvls = c.beginner_only
    ? state.config.levels.filter((l) => l.id === "beginner")
    : state.config.levels;
  // Pré-sélection sur le niveau évalué par David (curseur), sinon premier niveau.
  const _lvlIds = lvls.map((l) => l.id);
  state.level = _lvlIds.includes(state.assessedLevel) ? state.assessedLevel : lvls[0].id;
  lvls.forEach((lvl) => {
    const pill = document.createElement("button");
    pill.className = "pill" + (lvl.id === state.level ? " selected" : "");
    pill.textContent = lvl.label;
    pill.addEventListener("click", () => {
      state.level = lvl.id;
      [...levelWrap.children].forEach((p) => p.classList.remove("selected"));
      pill.classList.add("selected");
    });
    levelWrap.appendChild(pill);
  });

  // Décors
  const decorWrap = document.getElementById("decor-cards");
  decorWrap.innerHTML = "";
  state.situation = null;
  for (const [id, s] of Object.entries(state.config.situations)) {
    const card = document.createElement("button");
    card.className = "decor-card";
    card.style.backgroundImage = `linear-gradient(rgba(10,12,24,0.25), rgba(10,12,24,0.85)), url("${s.decor}")`;
    card.innerHTML = `<span class="decor-label">${s.label}</span>`;
    card.addEventListener("click", () => {
      state.situation = id;
      [...decorWrap.children].forEach((d) => d.classList.remove("selected"));
      card.classList.add("selected");
      document.getElementById("start-btn").disabled = false;
    });
    decorWrap.appendChild(card);
  }
  document.getElementById("start-btn").disabled = true;

  show("setup");
}

// =========================================================
// Démarrage de l'appel  (la fonction centrale de l'app)
// =========================================================
// Étapes : préparer l'UI -> demander un token au back-end -> ouvrir la session
// temps réel -> brancher tous les callbacks (statut, audio, sous-titres, fin).
async function startCall() {
  show("call");
  const setStatus = (t) => (document.getElementById("call-status").textContent = t);
  const transcriptEl = document.getElementById("transcript");
  transcriptEl.innerHTML = "";
  document.getElementById("call-debug").textContent = "";
  setStatus("🔧 Préparation de la session…");

  // Mémoire de la conversation + état de clôture.
  state.convo = [];
  const convo = state.convo;   // [{role:'user'|'ai', text}]
  let userTurns = 0;           // nb de répliques réelles de l'apprenant
  let endingRequested = false; // l'apprenant a dit au revoir
  let aiResponseDone = false;  // l'IA a fini de GÉNÉRER sa réplique d'adieu
  let finalizing = false;      // garde-fou anti double-bascule

  // 1) Récupération du token éphémère auprès du back-end
  let data;
  try {
    const res = await fetch("/api/token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        character: state.character,
        situation: state.situation,
        level: state.level,
      }),
    });
    data = await res.json();
    if (!res.ok) throw new Error(data.error || "Erreur token");
  } catch (e) {
    setStatus("❌ " + e.message);
    return;
  }

  // Décor + avatar de l'appel
  const callScreen = document.getElementById("screen-call");
  callScreen.style.backgroundImage =
    `linear-gradient(rgba(8,10,20,0.78), rgba(8,10,20,0.9)), url("${data.decor}")`;
  document.getElementById("call-avatar").src = data.avatar;
  document.getElementById("call-charname").textContent = data.character_name;
  document.getElementById("call-place").textContent = data.situation_label;

  // 2) Session temps réel.
  // On crée l'objet qui gère micro + WebSocket + audio, et on lui passe nos
  // "callbacks" : il les appellera quand tel ou tel évènement se produit.
  const session = new RealtimeSession({
    token: data.token,
    model: data.model,
    onStatus: setStatus, // met à jour le texte de statut (vert/connexion/erreur…)
    // L'IA parle -> on active l'anneau pulsant autour de l'avatar :
    onAiSpeaking: (on) => document.getElementById("call-avatar-ring").classList.toggle("speaking", on),
    // L'utilisateur parle (détecté par le serveur) -> on anime les barres "micro" :
    onUserSpeaking: (on) => document.getElementById("mic-indicator").classList.toggle("active", on),
    // Dès que l'utilisateur arrête de parler, on réserve sa bulle (placeholder
    // « … ») pour qu'elle se place AVANT la réponse de l'IA, même si le texte
    // Whisper arrive en retard. N'affecte en rien l'audio/débit de l'IA.
    onUserSpeechStopped: () => reserveUserTurn(),
    onError: (m) => {
      document.getElementById("call-debug").textContent = m;
    },
    onClose: (ev, intentional) => {
      setStatus("🔴 Déconnecté");
      // Raccrochage volontaire, fin en cours, ou close d'une session déjà
      // remplacée : rien à signaler.
      if (intentional || finalizing || state.session !== session) return;
      // --- Coupure SUBIE en pleine conversation ---
      stopVoiceAnimation(document.getElementById("call-avatar-ring"));
      state.session = null; // la session est morte : neutralise le filet 12s
      showDisconnect({
        message: "Connexion perdue. Tu peux reprendre une nouvelle conversation, ou voir le bilan de ce qui a déjà été dit.",
        retryLabel: "🔄 Reprendre",
        onRetry: () => startCall(),
        finishLabel: convo.length ? "📊 Voir le bilan" : "← Accueil",
        onFinish: () => { if (convo.length) showSummary(convo); else show("home"); },
      });
    },
    // L'IA a fini de GÉNÉRER toute sa réponse (≠ file audio momentanément vide).
    onResponseDone: () => {
      if (endingRequested) {
        aiResponseDone = true;
        maybeFinalize();
      }
    },
    // La file audio s'est vidée. On ne clôt QUE si la réponse est aussi terminée,
    // sinon on risquerait de couper l'IA entre deux phrases de son adieu.
    onPlaybackDrained: () => maybeFinalize(),
    onTranscript: (who, text, done, itemId) => {
      if (who === "ai") {
        if (text || done) addOrUpdateBubble(text || "", done, itemId);
        if (done && text) convo.push({ role: "ai", text });
      } else if (who === "user") {
        // Sous-titre utilisateur : on remplit la bulle déjà réservée (au moment
        // où l'utilisateur a cessé de parler), pour garder le bon ordre.
        if (!done) {
          updateUserTurn(text); // streaming éventuel
        } else {
          const finalText = fillUserTurn(text); // remplit la bulle ET le slot convo réservé
          if (finalText) {
            userTurns++;
            // Détection de fin : phrase d'adieu claire ET au moins 2 échanges réels
            // (évite les fins prématurées dues aux hallucinations de transcription).
            if (!endingRequested && userTurns >= 2 && isGoodbye(finalText)) {
              endingRequested = true;
              aiResponseDone = false; // on attend la réponse d'adieu À VENIR, pas la précédente
              setStatus("👋 Fin de conversation détectée…");
              // Filet de sécurité : si l'IA ne répond jamais, on clôt quand même après 12s.
              setTimeout(() => {
                if (endingRequested && !finalizing) {
                  finalizing = true;
                  finishConversation();
                }
              }, 12000);
            }
          }
        }
      }
    },
  });

  state.session = session;
  state.lastCharacterName = data.character_name;
  try {
    await session.start();
    // l'avatar se met à réagir à la voix de l'IA
    startVoiceAnimation(session, document.getElementById("call-avatar-ring"), document.getElementById("voice-eq"));
  } catch (e) {
    showStartError(e, { setStatus, onRetry: () => startCall(), onBack: () => show("menu") });
  }

  // Clôt la conversation SEULEMENT si : l'adieu est demandé, l'IA a fini de
  // générer sa réponse, et plus aucun son ne joue. Un petit délai laisse le
  // tout dernier extrait audio se terminer proprement (anti-coupure).
  function maybeFinalize() {
    if (!endingRequested || !aiResponseDone || finalizing) return;
    if (state.session && state.session.isPlaying()) return; // l'IA parle encore : on attend
    finalizing = true;
    setTimeout(finishConversation, 500);
  }

  async function finishConversation() {
    // Garde anti-double-bilan : si la session active n'est plus CELLE de cet
    // appel (l'utilisateur a déjà raccroché via endCall, ou un nouvel appel a
    // démarré), on ne refait pas un bilan. Couvre le cas du filet 12s qui se
    // déclenche après un raccrochage manuel.
    if (state.session !== session) return;
    stopVoiceAnimation(document.getElementById("call-avatar-ring"));
    state.session.stop();
    state.session = null;
    consumeBonusIfRaj(); // si c'était l'invité surprise, il disparaît
    await showSummary(convo);
  }

  // --- helpers de sous-titres ---
  function appendBubble(role, text) {
    const b = document.createElement("div");
    b.className = "bubble " + role;
    b.textContent = text;
    transcriptEl.appendChild(b);
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }

  // --- Bulle utilisateur "réservée" : on crée un emplacement vide AU MOMENT où
  // l'utilisateur finit de parler, puis on le remplit quand Whisper renvoie le
  // texte (en différé). Ça garantit l'ordre : utilisateur AVANT réponse IA. ---
  let pendingUserBubble = null;    // nœud DOM réservé
  let pendingUserSlot = -1;        // index réservé dans `convo`
  function reserveUserTurn() {
    if (pendingUserBubble) return; // une réservation est déjà en attente de texte
    pendingUserBubble = document.createElement("div");
    pendingUserBubble.className = "bubble user pending";
    pendingUserBubble.textContent = "…";
    transcriptEl.appendChild(pendingUserBubble);
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
    pendingUserSlot = convo.length;        // on réserve aussi la place dans le log
    convo.push({ role: "user", text: "" });
  }
  function updateUserTurn(partial) {
    if (!partial) return;
    if (!pendingUserBubble) reserveUserTurn();
    pendingUserBubble.textContent = partial; // aperçu en streaming si dispo
    pendingUserBubble.classList.remove("pending");
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }
  // Remplit la bulle/slot réservés. Renvoie le texte final (ou "" si rien d'utile).
  function fillUserTurn(text) {
    const clean = (text || "").trim();
    if (!pendingUserBubble) {
      // Pas de réservation (cas limite) : on ajoute normalement si du texte.
      if (clean) { appendBubble("user", clean); convo.push({ role: "user", text: clean }); }
      return clean;
    }
    if (clean) {
      pendingUserBubble.textContent = clean;
      pendingUserBubble.classList.remove("pending");
      makeClickableWords(pendingUserBubble);
      if (pendingUserSlot >= 0) convo[pendingUserSlot].text = clean;
    } else {
      // Bruit / hallucination silencieuse : on retire le placeholder vide.
      pendingUserBubble.remove();
      if (pendingUserSlot >= 0) convo.splice(pendingUserSlot, 1);
    }
    pendingUserBubble = null;
    pendingUserSlot = -1;
    return clean;
  }

  // Bulles IA indexées par item_id : chaque réponse de l'IA a SA bulle. Un 'done'
  // tardif (réponse interrompue) ne peut plus écraser la bulle d'une autre réponse,
  // et les deltas qui arrivent après le 'done' d'un item sont ignorés (anti-doublon).
  const aiBubbles = new Map(); // item_id -> { el, hasDeltas, done }
  function addOrUpdateBubble(text, done, itemId) {
    const key = itemId || "_single";
    let entry = aiBubbles.get(key);
    if (entry && entry.done) return; // item déjà finalisé : on ignore tout retardataire
    if (!entry) {
      const el = document.createElement("div");
      el.className = "bubble ai";
      transcriptEl.appendChild(el);
      entry = { el, hasDeltas: false, done: false };
      aiBubbles.set(key, entry);
    }
    if (!done) {
      entry.el.textContent += text; // streaming : on accumule les deltas
      entry.hasDeltas = true;
      transcriptEl.scrollTop = transcriptEl.scrollHeight;
    } else {
      // Repli si aucun delta n'a été streamé (certains modèles n'envoient que 'done').
      if (!entry.hasDeltas && text) entry.el.textContent = text;
      makeClickableWords(entry.el); // mots cliquables pour la traduction
      // Avec un vrai item_id on garde l'entrée (verrou anti-doublon) ; sans id, on
      // libère la clé pour que la réponse suivante reparte sur une bulle neuve.
      if (itemId) entry.done = true; else aiBubbles.delete(key);
    }
  }
}

function endCall() {
  stopVoiceAnimation(document.getElementById("call-avatar-ring"));
  if (state.session) {
    state.session.stop();
    state.session = null;
  }
  consumeBonusIfRaj(); // si c'était l'invité surprise, il disparaît
  // Quitter manuellement produit aussi un bilan s'il y a eu des échanges.
  if (state.convo && state.convo.length) {
    showSummary(state.convo);
  } else {
    show("home");
  }
}

// =========================================================
// Animation de l'avatar pilotée par la voix de l'IA
// =========================================================
// Boucle ~60x/s : on lit le volume (getOutputLevel) + le spectre (getSpectrum)
// de la voix en cours de lecture, et on les applique à l'avatar et à l'égaliseur.
function startVoiceAnimation(session, ring, eq) {
  // (Re)construit les barres de l'égaliseur.
  const BAR_COUNT = 18;
  eq.innerHTML = "";
  const bars = [];
  for (let i = 0; i < BAR_COUNT; i++) {
    const s = document.createElement("span");
    eq.appendChild(s);
    bars.push(s);
  }

  cancelAnimationFrame(state.animId);
  const loop = () => {
    const lvl = session.getOutputLevel();                 // volume global 0..1
    ring.style.setProperty("--level", lvl.toFixed(3));    // -> CSS anime l'avatar
    const spec = session.getSpectrum(BAR_COUNT);          // spectre -> hauteur des barres
    const t = performance.now() / 1000;
    for (let i = 0; i < bars.length; i++) {
      // En silence, une douce ondulation maintient les barres vivantes ;
      // dès que l'IA parle, le spectre prend le dessus.
      const idle = 3 + Math.sin(t * 3 + i * 0.5) * 2.5;
      const h = Math.max(idle, 4 + spec[i] * 42);
      bars[i].style.height = h.toFixed(1) + "px";
    }
    state.animId = requestAnimationFrame(loop);
  };
  state.animId = requestAnimationFrame(loop);
}

function stopVoiceAnimation(ring) {
  cancelAnimationFrame(state.animId);
  state.animId = null;
  if (ring) ring.style.setProperty("--level", "0");
}

// =========================================================
// Écran de bilan (note /10 + résumé)
// =========================================================
// Envoie la transcription au back-end (/api/summary) qui interroge un modèle
// texte, puis affiche la note et les conseils. En cas d'erreur réseau, on
// montre un bilan de repli plutôt que de planter.
async function showSummary(convo) {
  show("summary");
  document.getElementById("summary-loading").hidden = false;
  document.getElementById("summary-content").hidden = true;

  let data;
  try {
    const res = await fetch("/api/summary", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        transcript: convo,
        character_name: state.lastCharacterName || "",
        level: state.level,
      }),
    });
    data = await res.json();
    if (!res.ok) throw new Error(data.error || "Erreur bilan");
  } catch (e) {
    data = {
      score: 0,
      summary: "Le bilan n'a pas pu être généré (" + e.message + ").",
      justification: "",
      strengths: [],
      improvements: [],
    };
  }

  renderSummary(data);
  document.getElementById("summary-loading").hidden = true;
  document.getElementById("summary-content").hidden = false;
}

// Remplit l'écran de bilan avec les données reçues (note, résumé, listes).
function renderSummary(data) {
  const score = Math.max(0, Math.min(10, Number(data.score) || 0)); // borne 0..10 par sécurité

  // --- Jauge circulaire animée ---
  // Astuce SVG : on dessine un cercle puis on "cache" une partie de son trait
  // (stroke-dashoffset). On part du cercle plein caché, puis on révèle la
  // fraction correspondant au score -> effet de remplissage animé par le CSS.
  const numEl = document.getElementById("score-num");
  const arc = document.getElementById("gauge-arc");
  const circumference = 2 * Math.PI * 52;        // périmètre du cercle (r=52)
  arc.style.strokeDasharray = `${circumference}`;
  arc.style.strokeDashoffset = `${circumference}`; // état initial : trait masqué
  const hue = score * 12; // teinte 0=rouge -> 120=vert : la couleur reflète la note
  arc.style.stroke = `hsl(${hue}, 75%, 55%)`;

  requestAnimationFrame(() => {
    // On révèle la portion = score/10 (le CSS anime la transition).
    arc.style.strokeDashoffset = `${circumference * (1 - score / 10)}`;
  });

  // --- Compteur de chiffres qui monte de 0 jusqu'à la note ---
  let cur = 0;
  const step = () => {
    cur += 1;
    numEl.textContent = cur;
    if (cur < score) setTimeout(step, 80);
  };
  numEl.textContent = "0";
  if (score > 0) setTimeout(step, 200);

  document.getElementById("score-justif").textContent = data.justification || "";
  document.getElementById("summary-text").textContent = data.summary || "";

  const fill = (id, items, empty) => {
    const ul = document.getElementById(id);
    ul.innerHTML = "";
    const list = items && items.length ? items : [empty];
    for (const it of list) {
      const li = document.createElement("li");
      li.textContent = it;
      ul.appendChild(li);
    }
  };
  fill("summary-strengths", data.strengths, "—");
  fill("summary-improvements", data.improvements, "—");
}

// =========================================================
// MODE COURS — accueil, setup, leçon, bilan
// =========================================================
const DURATIONS = [5, 10]; // durée d'un cours, plafonnée à 10 min (anti-décrochage)

// Accueil des cours : surnom + choix du professeur.
async function openCourseHome() {
  show("course-home");
  try {
    const p = await (await fetch("/api/profile")).json();
    // On garde le niveau évalué pour pré-sélectionner les setups.
    state.assessedLevel = p.assessed_level || "";
    state.assessedLabel = p.assessed_label || "";
    const banner = document.getElementById("nickname-banner");
    const hasNick = !!(p && p.nickname);
    const hasLevel = !!(p && p.assessed_label);
    if (hasNick || hasLevel) {
      // Surnom + curseur de niveau (🎯) côte à côte selon ce qui est dispo.
      const nick = hasNick ? "🏅 " + p.nickname : "";
      const lvl = hasLevel ? "🎯 " + p.assessed_label : "";
      document.getElementById("nick-name").textContent = [nick, lvl].filter(Boolean).join("   ·   ");
      document.getElementById("nick-reason").textContent = hasNick
        ? (p.nickname_reason || "")
        : "Niveau estimé par David, l'examinateur.";
      banner.hidden = false;
    } else {
      banner.hidden = true;
    }
  } catch (_) {}
  // Rendu APRÈS la récupération du profil : David n'apparaît que si l'éval est faite.
  renderCourseCharacters();
  refreshResumeBars(); // barre "Reprendre" si une leçon est en pause
}

function renderCourseCharacters() {
  const wrap = document.getElementById("course-character-cards");
  wrap.innerHTML = "";
  const assessed = !!state.assessedLevel; // évaluation de niveau déjà passée ?
  for (const [id, c] of Object.entries(state.config.characters)) {
    // L'examinateur (David) n'apparaît dans les cours qu'UNE FOIS l'évaluation faite :
    // avant, il est mis en avant sur l'accueil (carte brillante) ; après, il se range
    // ici avec les autres profs. Un clic relance le test (et non un cours classique).
    if (c.examiner && !assessed) continue;
    const sp = specialtyOf(c); // couleur de carte + badge selon la spécialité
    const card = document.createElement("button");
    card.className = "char-card" + (sp.cls ? " " + sp.cls : "");
    const cta = c.examiner ? "Repasser le test →" : `Cours avec ${c.name} →`;
    card.innerHTML = `
      <div class="char-avatar"><img src="${c.avatar}" alt="${c.name}"></div>
      <div class="char-name">${c.name}</div>
      <div class="char-title">${c.title}</div>
      ${sp.badge ? `<span class="char-badge">${sp.badge}</span>` : ""}
      <p class="char-tagline">${c.tagline}</p>
      <span class="char-cta">${cta}</span>`;
    card.addEventListener("click", () => {
      if (c.examiner) {
        startAssessment(); // David : parcours dédié (évaluation), pas un cours classique
        return;
      }
      state.courseCharacter = id;
      goToCourseSetup();
    });
    wrap.appendChild(card);
  }
}

// Setup d'un cours : niveau + durée + état (reprise / leçons validées / rattrapage).
function goToCourseSetup() {
  const c = state.config.characters[state.courseCharacter];
  document.getElementById("course-setup-avatar").src = c.avatar;
  document.getElementById("course-setup-charname").textContent = `${c.name} — ${c.title}`;

  // Prof de vocabulaire : on active le sélecteur de thème (un thème = obligatoire).
  state.courseIsVocab = !!c.vocab_coach;
  state.courseTheme = null;
  state.courseThemeLabel = "";
  const themeBlock = document.getElementById("course-theme-block");
  const themeWrap = document.getElementById("course-theme-pills");
  if (state.courseIsVocab) {
    themeWrap.innerHTML = "";
    (state.config.vocab_themes || []).forEach((t) => {
      const pill = document.createElement("button");
      pill.className = "pill";
      pill.textContent = t.label;
      pill.addEventListener("click", () => {
        state.courseTheme = t.id;
        state.courseThemeLabel = t.label;
        [...themeWrap.children].forEach((p) => p.classList.remove("selected"));
        pill.classList.add("selected");
        refreshCourseState(); // l'état dépend de (prof, niveau, thème)
      });
      themeWrap.appendChild(pill);
    });
    themeBlock.hidden = false;
  } else {
    themeBlock.hidden = true;
  }

  // Niveaux — pré-sélection sur le niveau évalué par David (curseur), si dispo.
  const levelWrap = document.getElementById("course-level-pills");
  levelWrap.innerHTML = "";
  // Profs réservés aux grands débutants (ex: Sophie) : un seul niveau proposé.
  const courseLvls = c.beginner_only
    ? state.config.levels.filter((l) => l.id === "beginner")
    : state.config.levels;
  const levelIds = courseLvls.map((l) => l.id);
  state.courseLevel = levelIds.includes(state.assessedLevel) ? state.assessedLevel : courseLvls[0].id;
  courseLvls.forEach((lvl) => {
    const pill = document.createElement("button");
    pill.className = "pill" + (lvl.id === state.courseLevel ? " selected" : "");
    pill.textContent = lvl.label;
    pill.addEventListener("click", () => {
      state.courseLevel = lvl.id;
      [...levelWrap.children].forEach((p) => p.classList.remove("selected"));
      pill.classList.add("selected");
      refreshCourseState(); // l'état dépend du couple (prof, niveau)
    });
    levelWrap.appendChild(pill);
  });

  // Durées
  const durWrap = document.getElementById("course-duration-pills");
  durWrap.innerHTML = "";
  state.courseTarget = 10;
  DURATIONS.forEach((m) => {
    const pill = document.createElement("button");
    pill.className = "pill" + (m === state.courseTarget ? " selected" : "");
    pill.textContent = m + " min";
    pill.addEventListener("click", () => {
      state.courseTarget = m;
      [...durWrap.children].forEach((p) => p.classList.remove("selected"));
      pill.classList.add("selected");
    });
    durWrap.appendChild(pill);
  });

  refreshCourseState();
  show("course-setup");
}

// Récupère l'état du cours (perso+niveau) et adapte l'UI (reprise, rattrapage…).
async function refreshCourseState() {
  const box = document.getElementById("course-state-box");
  const btn = document.getElementById("course-start-btn");
  box.hidden = true;
  box.innerHTML = "";
  btn.textContent = "🎓 Commencer le cours";
  state.resumeElapsed = 0;

  // Prof de vocabulaire : tant qu'aucun thème n'est choisi, on bloque le démarrage.
  if (state.courseIsVocab && !state.courseTheme) {
    btn.disabled = true;
    btn.textContent = "🗂️ Choisissez d'abord un thème";
    return;
  }
  btn.disabled = false;

  // Le thème (s'il existe) cible la bonne progression côté serveur.
  const themeParam = state.courseTheme ? `&theme=${encodeURIComponent(state.courseTheme)}` : "";
  try {
    const st = await (
      await fetch(`/api/course?character=${state.courseCharacter}&level=${state.courseLevel}${themeParam}`)
    ).json();
    let html = "";
    if (state.courseIsVocab && st.learned_count > 0)
      html += `<div>🗂️ Mots déjà appris sur ce thème : <b>${st.learned_count}</b></div>`;
    if (st.completed_count > 0)
      html += `<div>📚 Leçons validées à ce niveau : <b>${st.completed_count}</b></div>`;
    if (st.to_review && st.to_review.length)
      html += `<div class="cs-review">🎯 Prochaine séance = rattrapage : ${st.to_review.join(", ")}</div>`;
    if (st.has_resume) {
      html += `<div class="cs-resume">⏸️ Leçon en cours — vous reprendrez là où vous vous êtes arrêté.</div>`;
      btn.textContent = "▶️ Reprendre la leçon";
      state.resumeElapsed = st.elapsed_seconds || 0;
      if (st.target_minutes) {
        state.courseTarget = st.target_minutes;
        [...document.getElementById("course-duration-pills").children].forEach((p) =>
          p.classList.toggle("selected", p.textContent === st.target_minutes + " min")
        );
      }
    }
    if (html) {
      box.innerHTML = html;
      box.hidden = false;
    }
  } catch (_) {}
}

// Démarre (ou reprend) une leçon.
async function startLesson() {
  show("lesson");
  const setStatus = (t) => (document.getElementById("lesson-status").textContent = t);
  const transcriptEl = document.getElementById("lesson-transcript");
  transcriptEl.innerHTML = "";
  document.getElementById("lesson-debug").textContent = "";
  document.getElementById("lesson-finish-btn").hidden = true;
  document.getElementById("lesson-timer").classList.remove("ready");
  setStatus("🔧 Préparation du cours…");

  // 1) Token (le contexte du cours est injecté côté serveur)
  let data;
  try {
    const res = await fetch("/api/course/token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        character: state.courseCharacter,
        level: state.courseLevel,
        target_minutes: state.courseTarget,
        theme: state.courseTheme || null, // thème pour la prof de vocabulaire
      }),
    });
    data = await res.json();
    if (!res.ok) throw new Error(data.error || "Erreur token");
  } catch (e) {
    setStatus("❌ " + e.message);
    return;
  }

  state.courseTarget = data.target_minutes;
  const targetSec = data.target_minutes * 60;

  // Transcript déjà échangé (reprise) : on l'amorce pour ne rien perdre.
  const convo = Array.isArray(data.resume_transcript) ? data.resume_transcript.slice() : [];
  state.lessonConvo = convo;

  // Décor + identité
  document.getElementById("screen-lesson").style.backgroundImage =
    `linear-gradient(rgba(8,10,20,0.80), rgba(8,10,20,0.92)), url("${data.decor}")`;
  document.getElementById("lesson-avatar").src = data.avatar;
  document.getElementById("lesson-charname").textContent = data.character_name;
  // Entête : niveau, + thème du jour si cours de vocabulaire.
  document.getElementById("lesson-level").textContent =
    "🎓 " + data.level_label + (data.theme_label ? " · " + data.theme_label : "");

  // Affiche l'historique repris en bulles (contexte visuel)
  for (const t of convo) {
    if (!t.text) continue;
    const b = document.createElement("div");
    b.className = "bubble " + (t.role === "user" ? "user" : "ai");
    b.textContent = t.text;
    if (t.role !== "user") makeClickableWords(b); // bulles IA : mots cliquables
    transcriptEl.appendChild(b);
  }
  transcriptEl.scrollTop = transcriptEl.scrollHeight;

  // 2) Chrono (reprend au temps déjà cumulé)
  let elapsed = data.elapsed_seconds || 0;
  let reached = false;
  const unlockSec = DEV_QUICK ? Math.min(30, targetSec) : targetSec; // ?dev=1 : déblocage rapide
  const timerEl = document.getElementById("lesson-timer");
  const finishBtn = document.getElementById("lesson-finish-btn");
  const fmt = (s) =>
    `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  const renderTimer = () => {
    timerEl.textContent = `${fmt(Math.min(elapsed, targetSec))} / ${fmt(targetSec)}`;
    if (elapsed >= unlockSec && !reached) {
      reached = true;
      timerEl.classList.add("ready");
      finishBtn.hidden = false; // on ne peut valider qu'à partir de la durée cible
    }
  };
  renderTimer();
  clearInterval(state.lessonTimer);
  state.lessonTimer = setInterval(() => {
    elapsed++;
    renderTimer();
  }, 1000);
  state.lessonGetElapsed = () => elapsed;

  // 3) Session temps réel (le prof parle en premier)
  const session = new RealtimeSession({
    token: data.token,
    model: data.model,
    greetFirst: true,
    onStatus: setStatus,
    onAiSpeaking: (on) =>
      document.getElementById("lesson-avatar-ring").classList.toggle("speaking", on),
    onError: (m) => (document.getElementById("lesson-debug").textContent = m),
    onClose: async (ev, intentional) => {
      setStatus("🔴 Déconnecté");
      if (intentional || state.session !== session) return;
      // --- Coupure SUBIE pendant la leçon ---
      clearInterval(state.lessonTimer);
      stopVoiceAnimation(document.getElementById("lesson-avatar-ring"));
      state.session = null;
      // Sauvegarde de secours : on persiste le transcript pour pouvoir REPRENDRE
      // la leçon plus tard. On n'écrase aucune note (la leçon n'est pas validée).
      try {
        await fetch("/api/course/save", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            character: state.courseCharacter,
            level: state.courseLevel,
            elapsed_seconds: state.lessonGetElapsed ? state.lessonGetElapsed() : 0,
            target_minutes: state.courseTarget,
            transcript: state.lessonConvo || [],
            theme: state.courseTheme || null,
          }),
        });
      } catch (_) {}
      showDisconnect({
        message: "Connexion perdue. Ta progression a été sauvegardée : tu peux reprendre la leçon là où tu t'es arrêté.",
        retryLabel: "🔄 Reprendre la leçon",
        onRetry: () => startLesson(),
        finishLabel: "← Retour au cours",
        onFinish: () => openCourseHome(),
      });
    },
    // Réserve la bulle utilisateur dès la fin de sa prise de parole (ordre correct).
    onUserSpeechStopped: () => reserveUserTurn(),
    onTranscript: (who, text, done, itemId) => {
      if (who === "ai") {
        if (text || done) aiBubble(text || "", done, itemId);
        if (done && text) convo.push({ role: "ai", text });
      } else if (who === "user") {
        if (!done) updateUserTurn(text);     // streaming éventuel
        else fillUserTurn(text);             // remplit bulle + slot convo réservés
      }
    },
  });
  state.session = session;
  try {
    await session.start();
    startVoiceAnimation(
      session,
      document.getElementById("lesson-avatar-ring"),
      document.getElementById("lesson-voice-eq")
    );
  } catch (e) {
    showStartError(e, { setStatus, onRetry: () => startLesson(), onBack: () => openCourseHome() });
  }

  // Bulles de sous-titres — bulle utilisateur "réservée" (cf. conversation libre) :
  // emplacement créé dès la fin de parole, rempli quand Whisper renvoie le texte.
  let pendingUserBubble = null;
  let pendingUserSlot = -1;
  function reserveUserTurn() {
    if (pendingUserBubble) return;
    pendingUserBubble = document.createElement("div");
    pendingUserBubble.className = "bubble user pending";
    pendingUserBubble.textContent = "…";
    transcriptEl.appendChild(pendingUserBubble);
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
    pendingUserSlot = convo.length;
    convo.push({ role: "user", text: "" });
  }
  function updateUserTurn(partial) {
    if (!partial) return;
    if (!pendingUserBubble) reserveUserTurn();
    pendingUserBubble.textContent = partial;
    pendingUserBubble.classList.remove("pending");
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }
  function fillUserTurn(text) {
    const clean = (text || "").trim();
    if (!pendingUserBubble) {
      if (clean) { userBubble(clean); convo.push({ role: "user", text: clean }); }
      return clean;
    }
    if (clean) {
      pendingUserBubble.textContent = clean;
      pendingUserBubble.classList.remove("pending");
      makeClickableWords(pendingUserBubble);
      if (pendingUserSlot >= 0) convo[pendingUserSlot].text = clean;
    } else {
      pendingUserBubble.remove();
      if (pendingUserSlot >= 0) convo.splice(pendingUserSlot, 1);
    }
    pendingUserBubble = null;
    pendingUserSlot = -1;
    return clean;
  }
  function userBubble(t) {
    const b = document.createElement("div");
    b.className = "bubble user";
    b.textContent = t;
    transcriptEl.appendChild(b);
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }
  // Bulles IA indexées par item_id (cf. conversation libre) : une bulle par réponse,
  // verrou anti-doublon une fois le 'done' reçu.
  const aiBubbles = new Map(); // item_id -> { el, hasDeltas, done }
  function aiBubble(text, done, itemId) {
    const key = itemId || "_single";
    let entry = aiBubbles.get(key);
    if (entry && entry.done) return;
    if (!entry) {
      const el = document.createElement("div");
      el.className = "bubble ai";
      transcriptEl.appendChild(el);
      entry = { el, hasDeltas: false, done: false };
      aiBubbles.set(key, entry);
    }
    if (!done) {
      entry.el.textContent += text;
      entry.hasDeltas = true;
      transcriptEl.scrollTop = transcriptEl.scrollHeight;
    } else {
      if (!entry.hasDeltas && text) entry.el.textContent = text;
      makeClickableWords(entry.el);
      if (itemId) entry.done = true; else aiBubbles.delete(key);
    }
  }
}

// Quitter une leçon = PAUSE : on sauvegarde pour reprendre plus tard (pas de bilan).
async function quitLesson() {
  clearInterval(state.lessonTimer);
  stopVoiceAnimation(document.getElementById("lesson-avatar-ring"));
  const elapsed = state.lessonGetElapsed ? state.lessonGetElapsed() : 0;
  if (state.session) {
    state.session.stop();
    state.session = null;
  }
  try {
    await fetch("/api/course/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        character: state.courseCharacter,
        level: state.courseLevel,
        elapsed_seconds: elapsed,
        target_minutes: state.courseTarget,
        transcript: state.lessonConvo || [],
        theme: state.courseTheme || null,
      }),
    });
  } catch (_) {}
  openCourseHome();
}

// Terminer une leçon (uniquement après la durée cible) = évaluation + bilan.
async function finishLesson() {
  clearInterval(state.lessonTimer);
  stopVoiceAnimation(document.getElementById("lesson-avatar-ring"));
  if (state.session) {
    state.session.stop();
    state.session = null;
  }
  await showLessonSummary(state.lessonConvo || []);
}

async function showLessonSummary(convo) {
  show("lesson-summary");
  document.getElementById("lesson-summary-loading").hidden = false;
  document.getElementById("lesson-summary-content").hidden = true;
  let data;
  try {
    const res = await fetch("/api/course/finish", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        character: state.courseCharacter,
        level: state.courseLevel,
        transcript: convo,
        elapsed_seconds: state.lessonGetElapsed ? state.lessonGetElapsed() : 0,
        theme: state.courseTheme || null,
      }),
    });
    data = await res.json();
    if (!res.ok) throw new Error(data.error || "Erreur bilan");
  } catch (e) {
    data = {
      score: 0, passed: false, topic: "Leçon",
      summary: "Le bilan n'a pas pu être généré (" + e.message + ").",
      justification: "", acquired: [], to_review: [], nickname: "", nickname_reason: "",
    };
  }
  renderLessonSummary(data);
  document.getElementById("lesson-summary-loading").hidden = true;
  document.getElementById("lesson-summary-content").hidden = false;
  // XP + badges de la leçon, et message si l'invité surprise est débloqué.
  const leveledUp = !!(data.gamification && data.gamification.leveled_up);
  if (data.gamification) {
    showXpToast(data.gamification, data.bonus_unlocked ? "🎁 Invité surprise débloqué !" : "");
  }
  // Petit son de fin de cours — sauf si une montée de niveau joue déjà SA fanfare
  // (sinon les deux sons se chevaucheraient).
  if (!leveledUp) {
    if (data.passed) fxLessonPass();
    else fxLessonDone();
  }
}

function renderLessonSummary(d) {
  const score = Math.max(0, Math.min(10, Number(d.score) || 0));
  document.getElementById("lesson-verdict-title").textContent = d.passed
    ? "Leçon validée ! 🎉"
    : "On reverra ça la prochaine fois 💪";
  const badge = document.getElementById("lesson-verdict-badge");
  badge.textContent = d.passed ? "✅ Validée" : "🔁 À retravailler";
  badge.className = "verdict-badge " + (d.passed ? "pass" : "fail");

  // Jauge + compteur (même principe que le bilan de conversation libre)
  const arc = document.getElementById("lesson-gauge-arc");
  const numEl = document.getElementById("lesson-score-num");
  const circ = 2 * Math.PI * 52;
  arc.style.strokeDasharray = `${circ}`;
  arc.style.strokeDashoffset = `${circ}`;
  arc.style.stroke = `hsl(${score * 12}, 75%, 55%)`;
  requestAnimationFrame(() => (arc.style.strokeDashoffset = `${circ * (1 - score / 10)}`));
  let cur = 0;
  numEl.textContent = "0";
  const step = () => {
    cur += 1;
    numEl.textContent = cur;
    if (cur < score) setTimeout(step, 80);
  };
  if (score > 0) setTimeout(step, 200);

  document.getElementById("lesson-score-justif").textContent = d.justification || "";
  document.getElementById("lesson-summary-text").textContent = d.summary || "";

  const nb = document.getElementById("lesson-nickname");
  if (d.nickname) {
    document.getElementById("lesson-nick-name").textContent = "🏅 " + d.nickname;
    document.getElementById("lesson-nick-reason").textContent = d.nickname_reason || "";
    nb.hidden = false;
  } else {
    nb.hidden = true;
  }

  const fill = (id, items, empty) => {
    const ul = document.getElementById(id);
    ul.innerHTML = "";
    (items && items.length ? items : [empty]).forEach((it) => {
      const li = document.createElement("li");
      li.textContent = it;
      ul.appendChild(li);
    });
  };
  fill("lesson-acquired", d.acquired, "—");
  fill("lesson-toreview", d.to_review, "—");

  // Carte de vocabulaire du jour (cours de vocabulaire uniquement) : mot + traduction,
  // les mots restant à pratiquer sont signalés.
  const vocabCard = document.getElementById("lesson-vocab");
  const taught = Array.isArray(d.taught_words) ? d.taught_words : [];
  if (taught.length) {
    const struggled = new Set(
      (Array.isArray(d.struggled_words) ? d.struggled_words : []).map((w) => (w.word || "").toLowerCase())
    );
    const listEl = document.getElementById("lesson-vocab-list");
    listEl.innerHTML = "";
    taught.forEach((w) => {
      const word = (w.word || "").trim();
      if (!word) return;
      const toPractice = struggled.has(word.toLowerCase());
      const chip = document.createElement("div");
      chip.className = "vocab-chip" + (toPractice ? " to-practice" : "");
      chip.innerHTML =
        `<span class="vc-word">${word}</span>` +
        (w.gloss ? `<span class="vc-gloss">${w.gloss}</span>` : "") +
        (toPractice ? `<span class="vc-tag">à revoir</span>` : "");
      listEl.appendChild(chip);
    });
    vocabCard.hidden = false;
  } else {
    vocabCard.hidden = true;
  }
}

// =========================================================
// ÉVALUATION DE NIVEAU (examinateur David) — 10 min figées, fin automatique
// =========================================================
async function startAssessment() {
  show("assessment");
  const setStatus = (t) => (document.getElementById("assess-status").textContent = t);
  const transcriptEl = document.getElementById("assess-transcript");
  transcriptEl.innerHTML = "";
  document.getElementById("assess-debug").textContent = "";
  setStatus("🔧 Préparation de l'évaluation…");

  // 1) Token (instructions de l'examinateur injectées côté serveur)
  let data;
  try {
    const res = await fetch("/api/assessment/token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    data = await res.json();
    if (!res.ok) throw new Error(data.error || "Erreur token");
  } catch (e) {
    setStatus("❌ " + e.message);
    return;
  }

  const convo = [];
  state.assessConvo = convo;

  // Décor + identité
  document.getElementById("screen-assessment").style.backgroundImage =
    `linear-gradient(rgba(8,10,20,0.80), rgba(8,10,20,0.92)), url("${data.decor}")`;
  document.getElementById("assess-avatar").src = data.avatar;
  document.getElementById("assess-charname").textContent = data.character_name;

  // 2) Chrono FIGÉ : on compte jusqu'à 10:00 puis on clôt automatiquement.
  const durationSec = (DEV_QUICK ? 0.7 : data.duration_minutes) * 60; // ?dev=1 : ~42 s pour tester
  let elapsed = 0;
  let finishing = false;
  const timerEl = document.getElementById("assess-timer");
  const totalLabel = `${String(Math.floor(durationSec / 60)).padStart(2, "0")}:${String(Math.round(durationSec % 60)).padStart(2, "0")}`;
  const fmt = (s) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  const renderTimer = () => (timerEl.textContent = `${fmt(Math.min(elapsed, durationSec))} / ${totalLabel}`);
  renderTimer();
  clearInterval(state.assessTimer);
  state.assessTimer = setInterval(() => {
    elapsed++;
    renderTimer();
    if (elapsed >= durationSec && !finishing) {
      finishing = true;
      clearInterval(state.assessTimer);
      setStatus("⏱️ Temps écoulé — estimation de ton niveau…");
      finishAssessment();
    }
  }, 1000);

  // 3) Session temps réel (l'examinateur parle en premier)
  const session = new RealtimeSession({
    token: data.token,
    model: data.model,
    greetFirst: true,
    onStatus: setStatus,
    onAiSpeaking: (on) =>
      document.getElementById("assess-avatar-ring").classList.toggle("speaking", on),
    onError: (m) => (document.getElementById("assess-debug").textContent = m),
    onClose: (ev, intentional) => {
      setStatus("🔴 Déconnecté");
      if (intentional || finishing || state.session !== session) return;
      // --- Coupure SUBIE pendant l'évaluation ---
      clearInterval(state.assessTimer);
      stopVoiceAnimation(document.getElementById("assess-avatar-ring"));
      state.session = null;
      // RÈGLE STRICTE : une éval doit durer 10 min complètes pour donner un niveau
      // fiable. On ne note JAMAIS une éval coupée -> aucun niveau erroné enregistré.
      showDisconnect({
        message: "Connexion perdue. Une évaluation doit se dérouler en entier pour estimer ton niveau correctement. Tu peux la recommencer ou revenir au menu — aucun niveau ne sera enregistré tant qu'elle n'est pas terminée.",
        retryLabel: "🔄 Recommencer l'évaluation",
        onRetry: () => startAssessment(),
        finishLabel: "← Menu",
        onFinish: () => show("menu"),
      });
    },
    onUserSpeechStopped: () => reserveUserTurn(),
    onTranscript: (who, text, done, itemId) => {
      if (who === "ai") {
        if (text || done) aiBubble(text || "", done, itemId);
        if (done && text) convo.push({ role: "ai", text });
      } else if (who === "user") {
        if (!done) updateUserTurn(text);
        else fillUserTurn(text);
      }
    },
  });
  state.session = session;
  try {
    await session.start();
    startVoiceAnimation(
      session,
      document.getElementById("assess-avatar-ring"),
      document.getElementById("assess-voice-eq")
    );
  } catch (e) {
    showStartError(e, { setStatus, onRetry: () => startAssessment(), onBack: () => show("menu") });
  }

  // --- Bulles (même logique réserve/remplit + index par item_id que les cours) ---
  let pendingUserBubble = null;
  let pendingUserSlot = -1;
  function reserveUserTurn() {
    if (pendingUserBubble) return;
    pendingUserBubble = document.createElement("div");
    pendingUserBubble.className = "bubble user pending";
    pendingUserBubble.textContent = "…";
    transcriptEl.appendChild(pendingUserBubble);
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
    pendingUserSlot = convo.length;
    convo.push({ role: "user", text: "" });
  }
  function updateUserTurn(partial) {
    if (!partial) return;
    if (!pendingUserBubble) reserveUserTurn();
    pendingUserBubble.textContent = partial;
    pendingUserBubble.classList.remove("pending");
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }
  function fillUserTurn(text) {
    const clean = (text || "").trim();
    if (!pendingUserBubble) {
      if (clean) { userBubble(clean); convo.push({ role: "user", text: clean }); }
      return;
    }
    if (clean) {
      pendingUserBubble.textContent = clean;
      pendingUserBubble.classList.remove("pending");
      makeClickableWords(pendingUserBubble);
      if (pendingUserSlot >= 0) convo[pendingUserSlot].text = clean;
    } else {
      pendingUserBubble.remove();
      if (pendingUserSlot >= 0) convo.splice(pendingUserSlot, 1);
    }
    pendingUserBubble = null;
    pendingUserSlot = -1;
  }
  function userBubble(t) {
    const b = document.createElement("div");
    b.className = "bubble user";
    b.textContent = t;
    transcriptEl.appendChild(b);
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }
  const aiBubbles = new Map();
  function aiBubble(text, done, itemId) {
    const key = itemId || "_single";
    let entry = aiBubbles.get(key);
    if (entry && entry.done) return;
    if (!entry) {
      const el = document.createElement("div");
      el.className = "bubble ai";
      transcriptEl.appendChild(el);
      entry = { el, hasDeltas: false, done: false };
      aiBubbles.set(key, entry);
    }
    if (!done) {
      entry.el.textContent += text;
      entry.hasDeltas = true;
      transcriptEl.scrollTop = transcriptEl.scrollHeight;
    } else {
      if (!entry.hasDeltas && text) entry.el.textContent = text;
      makeClickableWords(entry.el);
      if (itemId) entry.done = true; else aiBubbles.delete(key);
    }
  }
}

// Abandon volontaire de l'évaluation : on coupe tout et on revient au menu (aucun bilan).
function quitAssessment() {
  clearInterval(state.assessTimer);
  stopVoiceAnimation(document.getElementById("assess-avatar-ring"));
  if (state.session) {
    state.session.stop();
    state.session = null;
  }
  show("menu");
}

// Fin (automatique à 10:00) : on évalue et on affiche le curseur de niveau.
async function finishAssessment() {
  clearInterval(state.assessTimer);
  stopVoiceAnimation(document.getElementById("assess-avatar-ring"));
  if (state.session) {
    state.session.stop();
    state.session = null;
  }
  await showAssessmentSummary(state.assessConvo || []);
}

async function showAssessmentSummary(convo) {
  show("assessment-summary");
  document.getElementById("assess-summary-loading").hidden = false;
  document.getElementById("assess-summary-content").hidden = true;
  let data;
  try {
    const res = await fetch("/api/assessment/finish", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ transcript: convo }),
    });
    data = await res.json();
    if (!res.ok) throw new Error(data.error || "Erreur évaluation");
  } catch (e) {
    data = {
      level: "", level_label: "Indéterminé",
      summary: "Le bilan n'a pas pu être généré (" + e.message + ").",
      justification: "", strengths: [], improvements: [],
      recommended_start: "", recommended_label: "",
      level_order: ["beginner", "A1", "A2", "B1", "B2", "C1", "C2"],
    };
  }
  renderAssessmentSummary(data);
  document.getElementById("assess-summary-loading").hidden = true;
  document.getElementById("assess-summary-content").hidden = false;
  if (data.gamification) showXpToast(data.gamification, "🎯 Niveau évalué !");
  // On mémorise pour pré-sélectionner ce niveau par défaut dans les setups, et on note
  // l'état "évalué" pour que la carte d'accueil se masque tout de suite (sans clignoter).
  if (data.level) {
    state.assessedLevel = data.level;
    state.assessedLabel = data.level_label;
    localStorage.setItem("assessed", "1");
  }
}

function renderAssessmentSummary(d) {
  // Curseur : on dessine l'échelle Débutant → C2 avec un marqueur sur le niveau estimé.
  const order = Array.isArray(d.level_order) && d.level_order.length
    ? d.level_order
    : ["beginner", "A1", "A2", "B1", "B2", "C1", "C2"];
  const labelsShort = { beginner: "Déb.", A1: "A1", A2: "A2", B1: "B1", B2: "B2", C1: "C1", C2: "C2" };
  const scale = document.getElementById("level-scale");
  scale.innerHTML = "";
  const idx = order.indexOf(d.level);
  order.forEach((lvl, i) => {
    const step = document.createElement("div");
    step.className = "ls-step" + (i === idx ? " current" : "") + (idx >= 0 && i <= idx ? " reached" : "");
    step.innerHTML = `<span class="ls-dot"></span><span class="ls-name">${labelsShort[lvl] || lvl}</span>`;
    scale.appendChild(step);
  });

  document.getElementById("assess-level-label").textContent = d.level_label || "—";
  document.getElementById("assess-summary-text").textContent = d.summary || "";
  document.getElementById("assess-justif").textContent = d.justification || "";

  const fill = (id, items, empty) => {
    const ul = document.getElementById(id);
    ul.innerHTML = "";
    (items && items.length ? items : [empty]).forEach((it) => {
      const li = document.createElement("li");
      li.textContent = it;
      ul.appendChild(li);
    });
  };
  fill("assess-strengths", d.strengths, "—");
  fill("assess-improvements", d.improvements, "—");

  const reco = document.getElementById("assess-reco-box");
  if (d.recommended_label) {
    reco.innerHTML = `🎓 Pour démarrer tes cours, je te conseille le niveau <b>${d.recommended_label}</b> — mais tu restes libre de choisir.`;
    reco.hidden = false;
  } else {
    reco.hidden = true;
  }
}

// =========================================================
// TABLEAU DE PROGRESSION
// =========================================================
async function openProgress() {
  show("progress");
  const wrap = document.getElementById("progress-courses");
  wrap.innerHTML = '<p class="progress-empty">Chargement…</p>';
  let data;
  try {
    data = await (await fetch("/api/progress")).json();
  } catch (e) {
    wrap.innerHTML = '<p class="progress-empty">Impossible de charger la progression.</p>';
    return;
  }

  // Surnom
  const banner = document.getElementById("progress-nickname");
  if (data.profile && data.profile.nickname) {
    document.getElementById("pg-nick-name").textContent = "🏅 " + data.profile.nickname;
    document.getElementById("pg-nick-reason").textContent = data.profile.nickname_reason || "";
    banner.hidden = false;
  } else {
    banner.hidden = true;
  }

  // Totaux
  document.getElementById("pg-validated").textContent = data.totals.lessons_validated;
  document.getElementById("pg-avg").textContent = data.totals.average_score || "—";
  document.getElementById("pg-started").textContent = data.totals.courses_started;

  // Cartes par cours
  wrap.innerHTML = "";
  if (!data.courses.length) {
    wrap.innerHTML =
      '<p class="progress-empty">Aucun cours pour l\'instant. Lance ta première leçon ! 🎓</p>';
    return;
  }
  data.courses.forEach((c) => {
    const card = document.createElement("div");
    card.className = "pg-course";
    const lessons = c.completed.length
      ? c.completed
          .map((l) => `<li><span class="gr-ok">✓</span> ${l.topic} <b>${l.score}/10</b></li>`)
          .join("")
      : '<li class="muted">Aucune leçon validée pour le moment.</li>';
    const review =
      c.to_review && c.to_review.length
        ? `<div class="pg-review">🎯 À retravailler : ${c.to_review.join(", ")}</div>`
        : "";
    const resume = c.has_resume ? '<span class="pg-resume">⏸️ leçon en cours</span>' : "";
    card.innerHTML = `
      <div class="pg-course-head">
        <img src="${c.avatar}" alt="">
        <div><b>${c.character_name}</b> · ${c.level_label}${resume}</div>
      </div>
      <ul class="pg-lessons">${lessons}</ul>
      ${review}`;
    wrap.appendChild(card);
  });
}

// =========================================================
// MODE JEU — Quiz éclair
// =========================================================
const QUIZ_TIME_MS = 12000; // temps par question

const game = {
  level: null,
  questions: [],
  index: 0,
  score: 0,
  combo: 0,
  lives: 3,
  correct: 0,
  recap: [],
  answered: false,
  timer: null,
  deadline: 0,
  target: 10,        // nb de questions visé (le flux les livre une à une)
  loading: false,    // flux encore en cours ?
  started: false,    // 1ère question affichée ?
  waiting: false,    // on attend une question pas encore arrivée
  loadId: 0,         // invalide les flux d'une partie précédente
  errors: [],
};

function openGameHome() {
  show("game-home");
  // Pastilles de niveau
  const wrap = document.getElementById("game-level-pills");
  wrap.innerHTML = "";
  game.level = state.config.levels[0].id;
  state.config.levels.forEach((lvl) => {
    const pill = document.createElement("button");
    pill.className = "pill" + (lvl.id === game.level ? " selected" : "");
    pill.textContent = lvl.label;
    pill.addEventListener("click", () => {
      game.level = lvl.id;
      [...wrap.children].forEach((p) => p.classList.remove("selected"));
      pill.classList.add("selected");
      showBest();
    });
    wrap.appendChild(pill);
  });
  showBest();
}

function bestKey(level) {
  return "quizBest_" + level;
}
function showBest() {
  const box = document.getElementById("game-best");
  const best = Number(localStorage.getItem(bestKey(game.level)) || 0);
  if (best > 0) {
    box.innerHTML = `🏆 Meilleur score à ce niveau : <b>${best}</b>`;
    box.hidden = false;
  } else {
    box.hidden = true;
  }
}

// Lit le flux NDJSON de génération de quiz et appelle onQuestion(q) dès qu'une
// question arrive (1ère en ~2 s). Résout à la fin du flux, rejette sur erreur.
async function streamQuizQuestions(body, onQuestion) {
  const res = await fetch("/api/game/quiz/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok || !res.body) throw new Error("Erreur de génération");
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  const handle = (line) => {
    const s = line.trim();
    if (!s) return;
    const obj = JSON.parse(s);
    if (obj.error) throw new Error(obj.error);
    onQuestion(obj);
  };
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl);
      buf = buf.slice(nl + 1);
      handle(line);
    }
  }
  if (buf.trim()) handle(buf);
}

async function startGame() {
  show("game");
  state.gameReplay = startGame; // pour le bouton "Rejouer" de l'écran de fin
  document.getElementById("game-feedback").textContent = "";
  document.getElementById("game-prompt").textContent = "Chargement…";
  document.getElementById("game-choices").innerHTML = "";
  // Réinitialisation (le flux remplit game.questions au fur et à mesure)
  game.questions = [];
  game.index = 0;
  game.score = 0;
  game.combo = 0;
  game.lives = 3;
  game.correct = 0;
  game.recap = [];
  game.errors = [];
  game.target = 10;
  game.loading = true;
  game.started = false;
  game.waiting = false;
  const myLoad = ++game.loadId;

  streamQuizQuestions(
    { level: game.level, n: game.target, focus: "expressions" },
    (q) => {
      if (game.loadId !== myLoad) return; // partie obsolète (rejouée entre-temps)
      game.questions.push(q);
      if (!game.started) {
        game.started = true;
        renderQuestion(); // démarre dès la 1ère question
      } else if (game.waiting) {
        game.waiting = false;
        renderQuestion(); // on attendait justement celle-ci
      }
    }
  )
    .then(() => { if (game.loadId === myLoad) game.loading = false; })
    .catch((e) => {
      if (game.loadId !== myLoad) return;
      game.loading = false;
      if (!game.started) document.getElementById("game-prompt").textContent = "❌ " + e.message;
    });
}

// Envoie les énoncés ratés au carnet d'erreurs (révision espacée).
function sendErrorsToNotebook(items, level) {
  if (!items || !items.length) return;
  const payload = items.map((q) => ({
    type: q.type, prompt: q.prompt, choices: q.choices,
    answer_index: q.answer_index, choice_notes: q.choice_notes,
    explanation: q.explanation, level: level || "",
  }));
  fetch("/api/errors/add", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items: payload }),
  }).catch(() => {});
}

// Écran de fin générique (réutilisé par Quiz, Word Rush et Révision).
function showGameOver(title, nums, labels, recap, replayFn) {
  document.getElementById("game-over-title").textContent = title;
  document.getElementById("go-score").textContent = nums[0];
  document.getElementById("go-best").textContent = nums[1];
  document.getElementById("go-acc").textContent = nums[2];
  document.getElementById("go-label-1").textContent = labels[0];
  document.getElementById("go-label-2").textContent = labels[1];
  document.getElementById("go-label-3").textContent = labels[2];
  const recapEl = document.getElementById("go-recap");
  recapEl.innerHTML = "";
  recap.forEach((r) => {
    const li = document.createElement("li");
    const mark = r.ok ? '<span class="gr-ok">✓</span>' : '<span class="gr-ko">✗</span>';
    li.innerHTML = `${mark} <span class="gr-q">${r.prompt}</span> → <b>${r.good}</b>`;
    recapEl.appendChild(li);
  });
  state.gameReplay = replayFn;
  show("game-over");
}

function renderLives() {
  document.getElementById("game-lives").textContent =
    "❤️".repeat(game.lives) + "🤍".repeat(Math.max(0, 3 - game.lives));
}

function renderQuestion() {
  clearInterval(game.timer);
  if (game.lives <= 0 || game.index >= game.target) {
    endGame();
    return;
  }
  if (game.index >= game.questions.length) {
    // La question n'est pas encore arrivée dans le flux.
    if (!game.loading) { endGame(); return; } // flux terminé avec moins de questions
    game.waiting = true;                       // le flux relancera renderQuestion
    document.getElementById("game-prompt").textContent = "Chargement…";
    document.getElementById("game-choices").innerHTML = "";
    return;
  }
  const q = game.questions[game.index];
  game.answered = false;

  document.getElementById("game-score").textContent = game.score;
  document.getElementById("game-combo").textContent = game.combo >= 2 ? `🔥 x${game.combo}` : "";
  renderLives();
  document.getElementById("game-qtype").textContent =
    q.type === "traduction" ? "Traduction" : "Sens";
  document.getElementById("game-progress").textContent = `${game.index + 1} / ${game.target}`;
  document.getElementById("game-prompt").textContent = q.prompt;
  const gfb = document.getElementById("game-feedback");
  gfb.className = "game-feedback";
  gfb.textContent = "";

  const choicesEl = document.getElementById("game-choices");
  choicesEl.innerHTML = "";
  q.choices.forEach((choice, i) => {
    const btn = document.createElement("button");
    btn.className = "game-choice";
    btn.textContent = choice;
    btn.addEventListener("click", () => answer(i));
    choicesEl.appendChild(btn);
  });

  // Chrono visuel par question
  const bar = document.getElementById("game-timer-bar");
  bar.classList.remove("low");
  bar.style.width = "100%";
  game.deadline = performance.now() + QUIZ_TIME_MS;
  game.timer = setInterval(() => {
    const left = Math.max(0, game.deadline - performance.now());
    const frac = left / QUIZ_TIME_MS;
    bar.style.width = (frac * 100).toFixed(1) + "%";
    bar.classList.toggle("low", frac < 0.3);
    if (left <= 0) answer(-1); // temps écoulé = raté
  }, 50);
}

function answer(choiceIdx) {
  if (game.answered) return;
  game.answered = true;
  clearInterval(game.timer);

  const q = game.questions[game.index];
  const correctIdx = q.answer_index;
  const isCorrect = choiceIdx === correctIdx;
  const left = Math.max(0, game.deadline - performance.now());
  const frac = left / QUIZ_TIME_MS;

  // Affiche bonne/mauvaise réponse
  const buttons = [...document.getElementById("game-choices").children];
  buttons.forEach((b, i) => {
    b.disabled = true;
    if (i === correctIdx) b.classList.add("correct");
    else if (i === choiceIdx) b.classList.add("wrong");
    else b.classList.add("dim");
  });

  const fb = document.getElementById("game-feedback");
  if (isCorrect) {
    const pts = Math.round((100 + 100 * frac) * (1 + game.combo * 0.2));
    game.score += pts;
    game.combo += 1;
    game.correct += 1;
    fb.className = "game-feedback ok";
    fb.innerHTML =
      `<div class="fb-answer"><span class="gf-points">+${pts}</span> ✅ Correct !</div>` +
      (q.explanation ? `<div class="fb-explain">${q.explanation}</div>` : "");
    fxCorrect();
    flyPoints("game-score", `+${pts}`);
    if (game.combo >= 2) replayAnim("game-combo", "pulse");
  } else {
    game.combo = 0;
    game.lives -= 1;
    fxWrong();
    replayAnim("game-prompt", "shake");
    const good = q.choices[correctIdx];
    const notes = q.choice_notes || [];
    fb.className = "game-feedback ko";
    let html = `<div class="fb-answer">${choiceIdx === -1 ? "⏱️ Trop tard ! " : "❌ "}Bonne réponse : <b>${good}</b></div>`;
    if (q.explanation) html += `<div class="fb-explain">${q.explanation}</div>`;
    // Pourquoi le choix de l'utilisateur était faux (glose du choix sélectionné).
    if (choiceIdx >= 0 && notes[choiceIdx]) {
      html += `<div class="fb-why">Ton choix « ${q.choices[choiceIdx]} » = ${notes[choiceIdx]}</div>`;
    }
    fb.innerHTML = html;
    game.errors.push(q); // mémorise l'erreur pour le carnet
  }
  document.getElementById("game-score").textContent = game.score;
  document.getElementById("game-combo").textContent = game.combo >= 2 ? `🔥 x${game.combo}` : "";
  renderLives();

  game.recap.push({
    prompt: q.prompt,
    good: q.choices[correctIdx],
    chosen: choiceIdx >= 0 ? q.choices[choiceIdx] : null,
    ok: isCorrect,
  });

  game.index += 1;
  // 5 s si correct ; 6 s si erreur (le temps de lire pourquoi c'était faux).
  setTimeout(renderQuestion, isCorrect ? 5000 : 6000);
}

function endGame() {
  clearInterval(game.timer);
  const total = game.recap.length || game.questions.length;
  const acc = total ? Math.round((game.correct / total) * 100) : 0;
  const prevBest = Number(localStorage.getItem(bestKey(game.level)) || 0);
  const best = Math.max(prevBest, game.score);
  localStorage.setItem(bestKey(game.level), String(best));
  sendErrorsToNotebook(game.errors, game.level);

  const outOfLives = game.lives <= 0;
  const title = outOfLives
    ? "Plus de vies ! 💔"
    : game.score > prevBest
    ? "Nouveau record ! 🏆"
    : "Quiz terminé ! 🎉";
  showGameOver(title, [game.score, best, acc + "%"], ["Score", "Meilleur", "Réussite"], game.recap, startGame);
  recordActivity({ event: "quiz", correct: game.correct }); // XP + badges
}

function quitGame() {
  clearInterval(game.timer);
  openGameHome();
}

// =========================================================
// MODE JEU — Word Rush (vocabulaire par thème, time attack)
// =========================================================
const RUSH_TIME_MS = 60000;        // 60 secondes
const RUSH_COMBO_BONUS_MS = 2000;  // +2 s par bonne réponse
const RUSH_THEMES = [
  { id: "food", emoji: "🍔", label: "Nourriture", theme: "food and everyday meals" },
  { id: "travel", emoji: "✈️", label: "Voyage", theme: "travel and holidays" },
  { id: "work", emoji: "💼", label: "Travail", theme: "work and the office" },
  { id: "home", emoji: "🏠", label: "Maison", theme: "the home, rooms and furniture" },
  { id: "shopping", emoji: "🛒", label: "Courses", theme: "shopping and groceries" },
  { id: "health", emoji: "🩺", label: "Santé", theme: "health, the body and the doctor" },
  { id: "family", emoji: "👪", label: "Famille", theme: "family and relationships" },
  { id: "transport", emoji: "🚆", label: "Transports", theme: "transport and getting around" },
  { id: "weather", emoji: "🌦️", label: "Météo", theme: "weather and seasons" },
  { id: "clothes", emoji: "👕", label: "Vêtements", theme: "clothes and fashion" },
];

const rush = {
  level: null, themeId: null, themeLabel: null, themeQuery: null,
  questions: [], index: 0, score: 0, combo: 0, correct: 0, total: 0,
  recap: [], errors: [], answered: false, deadline: 0, timer: null,
  loadId: 0, started: false,
};

const RUSH_TOTAL = 24;        // questions par lot (livrées en flux, une à une)
const RUSH_REFILL_AT = 8;     // stock de questions non vues en dessous duquel on réapprovisionne

function shuffle(a) {
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
}

function openWordRushHome() {
  show("wordrush-home");
  // Niveaux
  const lw = document.getElementById("wr-level-pills");
  lw.innerHTML = "";
  rush.level = state.config.levels[0].id;
  state.config.levels.forEach((lvl) => {
    const pill = document.createElement("button");
    pill.className = "pill" + (lvl.id === rush.level ? " selected" : "");
    pill.textContent = lvl.label;
    pill.addEventListener("click", () => {
      rush.level = lvl.id;
      [...lw.children].forEach((p) => p.classList.remove("selected"));
      pill.classList.add("selected");
      showRushBest();
    });
    lw.appendChild(pill);
  });
  // Thèmes
  const tg = document.getElementById("wr-theme-grid");
  tg.innerHTML = "";
  rush.themeId = null;
  document.getElementById("wr-play-btn").disabled = true;
  RUSH_THEMES.forEach((t) => {
    const card = document.createElement("button");
    card.className = "wr-theme";
    card.innerHTML = `<span class="wr-emoji">${t.emoji}</span>${t.label}`;
    card.addEventListener("click", () => {
      rush.themeId = t.id;
      rush.themeLabel = `${t.emoji} ${t.label}`;
      rush.themeQuery = t.theme;
      [...tg.children].forEach((c) => c.classList.remove("selected"));
      card.classList.add("selected");
      document.getElementById("wr-play-btn").disabled = false;
      showRushBest();
    });
    tg.appendChild(card);
  });
  showRushBest();
}

function rushBestKey() {
  return `rushBest_${rush.level}_${rush.themeId || "none"}`;
}
function showRushBest() {
  const box = document.getElementById("wr-best");
  if (!rush.themeId) {
    box.hidden = true;
    return;
  }
  const best = Number(localStorage.getItem(rushBestKey()) || 0);
  if (best > 0) {
    box.innerHTML = `🏆 Meilleur (${rush.themeLabel}) : <b>${best}</b>`;
    box.hidden = false;
  } else {
    box.hidden = true;
  }
}

async function startWordRush() {
  show("wordrush");
  state.gameReplay = startWordRush;
  const myLoad = ++rush.loadId;   // invalide le flux d'une partie précédente
  document.getElementById("wr-feedback").textContent = "";
  document.getElementById("wr-prompt").textContent = "Chargement…";
  document.getElementById("wr-choices").innerHTML = "";
  document.getElementById("wr-theme-label").textContent = rush.themeLabel || "";
  // Réinitialisation (le flux remplit rush.questions au fur et à mesure)
  rush.questions = [];
  rush.seen = new Set();   // prompts déjà dans le pool -> jamais de doublon
  rush.index = 0;
  rush.score = 0;
  rush.combo = 0;
  rush.correct = 0;
  rush.total = 0;
  rush.recap = [];
  rush.errors = [];
  rush.started = false;
  rush.fetching = false;   // un réapprovisionnement est-il en cours ?
  rush.exhausted = false;  // le thème n'a plus rien de nouveau à proposer ?

  streamQuizQuestions(
    { level: rush.level, theme: rush.themeQuery, n: RUSH_TOTAL },
    (q) => {
      if (rush.loadId !== myLoad) return; // partie obsolète
      rushAddQuestion(q);
      if (!rush.started && rush.questions.length) {
        // On lance le chrono et la 1ère question dès qu'elle arrive (~2 s).
        rush.started = true;
        rush.deadline = performance.now() + RUSH_TIME_MS;
        clearInterval(rush.timer);
        rush.timer = setInterval(rushTick, 50);
        rushRenderQuestion();
      }
    }
  ).catch((e) => {
    if (rush.loadId !== myLoad) return;
    if (!rush.started) document.getElementById("wr-prompt").textContent = "❌ " + e.message;
  });
}

// Ajoute une question au pool en évitant tout doublon (même prompt).
function rushAddQuestion(q) {
  const k = (q.prompt || "").trim().toLowerCase();
  if (!k || rush.seen.has(k)) return false;
  rush.seen.add(k);
  rush.questions.push(q);
  return true;
}

// Réapprovisionnement continu : quand le stock de questions NON ENCORE VUES passe sous le
// seuil, on récupère en fond de NOUVELLES questions (en excluant celles déjà chargées),
// pour que la partie ne reboucle PAS sur les mêmes questions.
function rushMaybeRefill() {
  if (rush.fetching || rush.exhausted) return;
  if (rush.questions.length - rush.index > RUSH_REFILL_AT) return;
  const myLoad = rush.loadId;
  rush.fetching = true;
  let added = 0;
  streamQuizQuestions(
    {
      level: rush.level,
      theme: rush.themeQuery,
      n: RUSH_TOTAL,
      exclude: rush.questions.map((q) => q.prompt),
    },
    (q) => { if (rush.loadId === myLoad && rushAddQuestion(q)) added++; }
  )
    .then(() => {
      if (rush.loadId !== myLoad) return;
      rush.fetching = false;
      if (added === 0) rush.exhausted = true; // thème épuisé -> on arrête de demander
    })
    .catch(() => { if (rush.loadId === myLoad) rush.fetching = false; });
}

function rushTick() {
  const left = Math.max(0, rush.deadline - performance.now());
  const bar = document.getElementById("wr-timer-bar");
  bar.style.width = ((left / RUSH_TIME_MS) * 100).toFixed(1) + "%";
  bar.classList.toggle("low", left < 10000);
  const timeEl = document.getElementById("wr-time");
  timeEl.textContent = "0:" + String(Math.ceil(left / 1000)).padStart(2, "0");
  timeEl.classList.toggle("low", left < 10000);
  if (left <= 0) endWordRush();
}

function rushRenderQuestion() {
  // Boucle sur les questions (on rebrasse) tant qu'il reste du temps.
  if (rush.index >= rush.questions.length) {
    rush.index = 0;
    shuffle(rush.questions);
  }
  const q = rush.questions[rush.index];
  rush.answered = false;
  document.getElementById("wr-score").textContent = rush.score;
  document.getElementById("wr-combo").textContent = rush.combo >= 2 ? `🔥 x${rush.combo}` : "";
  document.getElementById("wr-count").textContent = q.type === "traduction" ? "Traduction" : "Sens";
  document.getElementById("wr-prompt").textContent = q.prompt;
  const wfb = document.getElementById("wr-feedback");
  wfb.className = "game-feedback";
  wfb.textContent = "";
  const ce = document.getElementById("wr-choices");
  ce.innerHTML = "";
  q.choices.forEach((c, i) => {
    const b = document.createElement("button");
    b.className = "game-choice";
    b.textContent = c;
    b.addEventListener("click", () => rushAnswer(i));
    ce.appendChild(b);
  });
}

function rushAnswer(i) {
  if (rush.answered) return;
  rush.answered = true;
  const q = rush.questions[rush.index];
  const correctIdx = q.answer_index;
  const ok = i === correctIdx;
  rush.total += 1;

  const buttons = [...document.getElementById("wr-choices").children];
  buttons.forEach((b, bi) => {
    b.disabled = true;
    if (bi === correctIdx) b.classList.add("correct");
    else if (bi === i) b.classList.add("wrong");
    else b.classList.add("dim");
  });

  const fb = document.getElementById("wr-feedback");
  if (ok) {
    const pts = Math.round(100 * (1 + rush.combo * 0.2));
    rush.score += pts;
    rush.combo += 1;
    rush.correct += 1;
    rush.deadline += RUSH_COMBO_BONUS_MS; // bonus de temps
    fb.className = "game-feedback ok";
    fb.innerHTML = `<div class="fb-answer"><span class="gf-points">+${pts}</span> ✅ · ⏱️ +2 s</div>`;
    fxCorrect();
    flyPoints("wr-score", `+${pts}`);
    if (rush.combo >= 2) replayAnim("wr-combo", "pulse");
  } else {
    rush.combo = 0;
    fb.className = "game-feedback ko";
    fb.innerHTML = `<div class="fb-answer">❌ Réponse : <b>${q.choices[correctIdx]}</b></div>`;
    rush.errors.push(q); // mémorise l'erreur pour le carnet
    fxWrong();
    replayAnim("wr-prompt", "shake");
  }
  document.getElementById("wr-score").textContent = rush.score;
  document.getElementById("wr-combo").textContent = rush.combo >= 2 ? `🔥 x${rush.combo}` : "";

  rush.recap.push({ prompt: q.prompt, good: q.choices[correctIdx], ok });
  rush.index += 1;
  rushMaybeRefill(); // récupère de nouvelles questions avant d'épuiser le pool
  // Enchaînement rapide (arcade) ; on n'enchaîne que s'il reste du temps.
  setTimeout(() => {
    if (performance.now() < rush.deadline) rushRenderQuestion();
  }, ok ? 550 : 1100);
}

function endWordRush() {
  clearInterval(rush.timer);
  const acc = rush.total ? Math.round((rush.correct / rush.total) * 100) : 0;
  const prevBest = Number(localStorage.getItem(rushBestKey()) || 0);
  const best = Math.max(prevBest, rush.score);
  localStorage.setItem(rushBestKey(), String(best));
  sendErrorsToNotebook(rush.errors, rush.level);

  const title = rush.score > prevBest && rush.score > 0 ? "Nouveau record ! 🏆" : "Temps écoulé ! ⏱️";
  showGameOver(title, [rush.score, best, acc + "%"], ["Score", "Meilleur", "Réussite"], rush.recap, startWordRush);
  recordActivity({ event: "wordrush", correct: rush.correct }); // XP + badges
}

function quitWordRush() {
  clearInterval(rush.timer);
  openWordRushHome();
}

// =========================================================
// CARNET D'ERREURS — accueil + révision espacée
// =========================================================
const review = { items: [], index: 0, correct: 0, total: 0, recap: [], answered: false, next: null };

async function openErrorbook() {
  show("errorbook");
  let data;
  try {
    data = await (await fetch("/api/errors")).json();
  } catch (e) {
    data = { active: 0, due: 0, mastered: 0, themes: [] };
  }
  document.getElementById("eb-due").textContent = data.due;
  document.getElementById("eb-active").textContent = data.active;
  document.getElementById("eb-mastered").textContent = data.mastered;

  const empty = document.getElementById("eb-empty");
  const btn = document.getElementById("eb-review-btn");
  if (data.active === 0) {
    empty.hidden = false;
    btn.disabled = true;
    btn.textContent = "🧠 Réviser maintenant";
  } else {
    empty.hidden = true;
    btn.disabled = false;
    btn.textContent = data.due > 0 ? `🧠 Réviser (${data.due} à revoir)` : "🧠 Réviser quand même";
  }

  const th = document.getElementById("eb-themes");
  if (data.themes && data.themes.length) {
    th.hidden = false;
    th.innerHTML =
      `<div class="block-title">Thèmes à retravailler (vus en cours)</div>` +
      `<ul>${data.themes.map((t) => `<li>${t}</li>`).join("")}</ul>`;
  } else {
    th.hidden = true;
  }
}

async function reviewStart() {
  show("review");
  const fb = document.getElementById("rv-feedback");
  fb.className = "game-feedback";
  fb.textContent = "";
  document.getElementById("rv-prompt").textContent = "Chargement…";
  document.getElementById("rv-choices").innerHTML = "";
  let data;
  try {
    data = await (await fetch("/api/errors/session?n=12")).json();
  } catch (e) {
    document.getElementById("rv-prompt").textContent = "❌ " + e.message;
    return;
  }
  review.items = data.items || [];
  if (!review.items.length) {
    openErrorbook();
    return;
  }
  // On rebrasse les choix de chaque carte (évite de mémoriser la position).
  review.items.forEach((it) => {
    const choices = Array.isArray(it.choices) ? it.choices : [];
    const notes = it.choice_notes || [];
    // On rebrasse sur le nombre RÉEL de choix (pas un 4 codé en dur) : sinon une
    // carte à 3 choix produirait un bouton vide et un answer_index décalé.
    const order = choices.map((_, k) => k);
    shuffle(order);
    it.choices = order.map((k) => choices[k]);
    it.choice_notes = order.map((k) => notes[k] || "");
    it.answer_index = order.indexOf(it.answer_index);
  });
  review.index = 0;
  review.correct = 0;
  review.total = 0;
  review.mastered = 0;
  review.recap = [];
  reviewRender();
}

function reviewRender() {
  if (review.index >= review.items.length) {
    reviewEnd();
    return;
  }
  const q = review.items[review.index];
  review.answered = false;
  document.getElementById("rv-count").textContent = `${review.index + 1} / ${review.items.length}`;
  document.getElementById("rv-qtype").textContent = q.type === "traduction" ? "Traduction" : "Sens";
  document.getElementById("rv-box").textContent = "Palier " + (q.box || 0);
  document.getElementById("rv-prompt").textContent = q.prompt;
  const fb = document.getElementById("rv-feedback");
  fb.className = "game-feedback";
  fb.textContent = "";
  const ce = document.getElementById("rv-choices");
  ce.innerHTML = "";
  q.choices.forEach((c, i) => {
    const b = document.createElement("button");
    b.className = "game-choice";
    b.textContent = c;
    b.addEventListener("click", () => reviewAnswer(i));
    ce.appendChild(b);
  });
}

function reviewAnswer(i) {
  if (review.answered) return;
  review.answered = true;
  const q = review.items[review.index];
  const correctIdx = q.answer_index;
  const ok = i === correctIdx;
  review.total += 1;
  if (ok) {
    review.correct += 1;
    // Carte acquise (palier max = 4 côté serveur) -> compte pour l'XP.
    if ((q.box || 0) + 1 >= 4) review.mastered += 1;
  }

  const buttons = [...document.getElementById("rv-choices").children];
  buttons.forEach((b, bi) => {
    b.disabled = true;
    if (bi === correctIdx) b.classList.add("correct");
    else if (bi === i) b.classList.add("wrong");
    else b.classList.add("dim");
  });

  const fb = document.getElementById("rv-feedback");
  if (ok) {
    fb.className = "game-feedback ok";
    fb.innerHTML =
      `<div class="fb-answer">✅ Correct !</div>` +
      (q.explanation ? `<div class="fb-explain">${q.explanation}</div>` : "");
  } else {
    fb.className = "game-feedback ko";
    let html = `<div class="fb-answer">❌ Bonne réponse : <b>${q.choices[correctIdx]}</b></div>`;
    if (q.explanation) html += `<div class="fb-explain">${q.explanation}</div>`;
    const notes = q.choice_notes || [];
    if (notes[i]) html += `<div class="fb-why">Ton choix « ${q.choices[i]} » = ${notes[i]}</div>`;
    fb.innerHTML = html;
  }

  // Mise à jour Leitner (palier + prochaine échéance) côté serveur.
  fetch("/api/errors/result", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: q.id, correct: ok }),
  }).catch(() => {});

  review.recap.push({ prompt: q.prompt, good: q.choices[correctIdx], ok });
  review.index += 1;
  review.next = setTimeout(reviewRender, ok ? 4000 : 6000);
}

function reviewEnd() {
  const acc = review.total ? Math.round((review.correct / review.total) * 100) : 0;
  showGameOver(
    "Révision terminée 📒",
    [review.correct, review.total, acc + "%"],
    ["Réussies", "Révisées", "Réussite"],
    review.recap,
    reviewStart
  );
  recordActivity({ event: "review", correct: review.correct, mastered: review.mastered }); // XP + badges
}

function quitReview() {
  clearTimeout(review.next);
  openErrorbook();
}

// =========================================================
// Câblage des boutons (qui fait quoi au clic) + démarrage
// =========================================================
// Menu
document.getElementById("mode-free").addEventListener("click", openFreeHome);
document.getElementById("mode-course").addEventListener("click", openCourseHome);
document.getElementById("mode-game").addEventListener("click", () => show("games-hub"));
document.getElementById("home-back").addEventListener("click", () => show("menu"));
document.getElementById("gami-banner").addEventListener("click", openProfile);
document.getElementById("profile-back").addEventListener("click", () => show("menu"));
document.getElementById("menu-gift").addEventListener("click", startBonusConversation); // ouvrir le cadeau = rencontrer Raj

// Évaluation de niveau (David)
document.getElementById("assessment-reco").addEventListener("click", startAssessment);
document.getElementById("assess-quit-btn").addEventListener("click", quitAssessment);
document.getElementById("assess-courses-btn").addEventListener("click", openCourseHome);
document.getElementById("assess-retake-btn").addEventListener("click", startAssessment);
document.getElementById("assess-home-btn").addEventListener("click", () => show("menu"));

// Conversation libre
document.getElementById("start-btn").addEventListener("click", startCall);
document.getElementById("end-btn").addEventListener("click", endCall);
document.getElementById("setup-back").addEventListener("click", () => show("home"));
document.getElementById("home-btn").addEventListener("click", () => show("menu"));
document.getElementById("replay-btn").addEventListener("click", () => {
  // L'invité surprise (raj) est caché : pas de "setup" possible -> retour à l'accueil libre.
  if (state.character && state.config.characters[state.character]) goToSetup();
  else openFreeHome();
});

// Cours
document.getElementById("course-back").addEventListener("click", () => show("menu"));
document.getElementById("course-setup-back").addEventListener("click", () => show("course-home"));
document.getElementById("course-start-btn").addEventListener("click", startLesson);
// Barres "Reprendre le dernier cours" (accueil + écran des profs)
document
  .getElementById("resume-course-menu")
  .addEventListener("click", function () { resumeLastCourse(this._resumeInfo); });
document
  .getElementById("resume-course-home")
  .addEventListener("click", function () { resumeLastCourse(this._resumeInfo); });
document.getElementById("lesson-quit-btn").addEventListener("click", quitLesson);
document.getElementById("lesson-finish-btn").addEventListener("click", finishLesson);
document.getElementById("lesson-home-btn").addEventListener("click", () => show("menu"));
document.getElementById("lesson-replay-btn").addEventListener("click", openCourseHome);
document.getElementById("course-progress-btn").addEventListener("click", openProgress);
document.getElementById("progress-back").addEventListener("click", () => show("course-home"));
document.getElementById("course-reset").addEventListener("click", async () => {
  if (!confirm("Effacer toute ta progression (cours, surnom) ? Action irréversible.")) return;
  try {
    await fetch("/api/progress/reset", { method: "POST" });
  } catch (_) {}
  localStorage.removeItem("assessed"); // l'éval est effacée : la reco brillante revient
  openCourseHome();
});

// Hub des jeux
document.getElementById("hub-back").addEventListener("click", () => show("menu"));
document.getElementById("hub-quiz").addEventListener("click", openGameHome);
document.getElementById("hub-rush").addEventListener("click", openWordRushHome);
document.getElementById("hub-errors").addEventListener("click", openErrorbook);

// Carnet d'erreurs
document.getElementById("eb-back").addEventListener("click", () => show("games-hub"));
document.getElementById("eb-review-btn").addEventListener("click", reviewStart);
document.getElementById("rv-quit").addEventListener("click", quitReview);

// Quiz expressions
document.getElementById("game-back").addEventListener("click", () => show("games-hub"));
document.getElementById("game-play-btn").addEventListener("click", startGame);
document.getElementById("game-quit").addEventListener("click", quitGame);
document.getElementById("game-menu").addEventListener("click", () => show("menu"));
// Rejouer = relance le DERNIER jeu joué (quiz ou word rush).
document.getElementById("game-replay").addEventListener("click", () => state.gameReplay && state.gameReplay());

// Word Rush
document.getElementById("wr-back").addEventListener("click", () => show("games-hub"));
document.getElementById("wr-play-btn").addEventListener("click", startWordRush);
document.getElementById("wr-quit").addEventListener("click", quitWordRush);

// Toggle son/vibration (écran Profil) : bascule la préférence + petit bip de confirmation.
document.getElementById("pf-sound-toggle").addEventListener("click", () => {
  FX.muted = !FX.muted;
  updateSoundToggle();
  if (!FX.muted) fxCorrect(); // bip témoin quand on réactive
});

// Point d'entrée : au chargement de la page, on récupère la config du back-end.
// Si ça échoue (back-end éteint), on affiche un message clair plutôt qu'un écran blanc.
loadConfig().catch((e) => {
  document.body.innerHTML =
    `<div style="color:#fff;padding:40px;font-family:sans-serif">Impossible de charger l'application : ${e.message}<br>Le back-end est-il démarré ?</div>`;
});
// Petit "pop" + micro-vibration au tap d'un choix (pills de niveau/durée/thème,
// cartes Word Rush, réponses de quiz). Délégué : couvre tous les choix actuels ET
// futurs sans toucher chaque handler. En capture pour passer AVANT un éventuel
// désactivage du bouton (réponses).
document.addEventListener("click", (e) => {
  // Retour haptique sur TOUT élément tapable (choix + cartes qui naviguent).
  const tappable = e.target.closest(
    ".pill, .wr-theme, .game-choice, .char-card, .mode-card, .decor-card, .resume-bar"
  );
  if (!tappable) return;
  vibrate(12);           // micro-retour haptique (respecte le mute du Profil)
  // "Pop" visible uniquement sur les choix qui RESTENT à l'écran (pas les cartes
  // qui changent d'écran : l'effet "pressé" :active s'en charge, lui, AVANT la transition).
  const choice = e.target.closest(".pill, .wr-theme, .game-choice");
  if (choice) {
    choice.classList.remove("pop-anim");
    void choice.offsetWidth; // force le reflow → rejoue l'animation à chaque tap
    choice.classList.add("pop-anim");
  }
}, true);

refreshMenuGami(); // bandeau XP/streak dès le chargement (le menu est actif d'emblée)
refreshResumeBars(); // "Reprendre le dernier cours" dès le chargement si une leçon est en pause
refreshMenuBonus(); // cadeau invité surprise dès le chargement
refreshMenuAssessment(); // reco "test de niveau" (brillante tant que l'éval n'est pas faite)

// PWA : enregistre le service worker (lancement hors-ligne + chargements plus rapides).
// Fonctionne sur HTTPS et localhost ; ignoré ailleurs.
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}
