/* =========================================================================
 * realtime.js — LE MOTEUR AUDIO TEMPS RÉEL
 * -------------------------------------------------------------------------
 * La classe RealtimeSession gère toute la "plomberie" vocale. Le schéma :
 *
 *   MICRO ─▶ AudioWorklet (24kHz, PCM16) ─▶ WebSocket ─▶ OpenAI
 *   OpenAI ─▶ WebSocket ─▶ décodage PCM16 ─▶ lecture audio planifiée ─▶ HP
 *
 * L'UI (app.js) ne sait RIEN de tout ça : elle reçoit juste des évènements
 * via des callbacks (onStatus, onAiSpeaking, onTranscript, onPlaybackDrained…).
 * C'est ce qui garde le code propre et séparé.
 * ========================================================================= */
// Traduit une erreur getUserMedia en message clair pour l'utilisateur.
// On marque l'erreur (kind = "mic") pour que l'UI affiche une alerte "Réessayer".
export function friendlyMicError(err) {
  const name = err && err.name;
  let msg;
  if (name === "NotFoundError" || name === "DevicesNotFoundError") {
    msg = "🎤 Aucun micro détecté. Branche un micro (ou un casque avec micro), puis réessaie.";
  } else if (name === "NotAllowedError" || name === "PermissionDeniedError" || name === "SecurityError") {
    msg = "🎤 L'accès au micro a été refusé. Autorise le micro pour ce site dans ton navigateur, puis réessaie.";
  } else if (name === "NotReadableError" || name === "TrackStartError") {
    msg = "🎤 Le micro est déjà utilisé par une autre application (Zoom, Teams…). Ferme-la, puis réessaie.";
  } else {
    msg = "🎤 Impossible d'accéder au micro : " + ((err && err.message) || "erreur inconnue") + ".";
  }
  const e = new Error(msg);
  e.kind = "mic";
  return e;
}

export class RealtimeSession {
  // Le constructeur reçoit le token (clé de session), le modèle, et les
  // callbacks que l'UI veut écouter. Les "|| (() => {})" = callbacks optionnels.
  constructor({ token, model, greetFirst, onStatus, onAiSpeaking, onUserSpeaking, onUserSpeechStopped, onTranscript, onError, onClose, onPlaybackDrained, onResponseDone }) {
    this.token = token;
    this.model = model;
    this.greetFirst = !!greetFirst; // si vrai, l'IA prend la parole en premier (cours)
    this.onStatus = onStatus || (() => {});
    this.onAiSpeaking = onAiSpeaking || (() => {});         // l'IA parle / s'arrête
    this.onUserSpeaking = onUserSpeaking || (() => {});     // l'utilisateur parle / s'arrête
    this.onUserSpeechStopped = onUserSpeechStopped || (() => {}); // l'utilisateur VIENT de finir de parler (avant que le sous-titre Whisper n'arrive)
    this.onTranscript = onTranscript || (() => {});         // sous-titres (ai/user)
    this.onError = onError || (() => {});
    this.onClose = onClose || (() => {});
    this.onPlaybackDrained = onPlaybackDrained || (() => {}); // file audio vidée (peut arriver entre 2 phrases !)
    this.onResponseDone = onResponseDone || (() => {});       // l'IA a FINI de générer sa réponse complète

    this.ws = null;            // la connexion WebSocket vers OpenAI
    this.stream = null;        // le flux du micro
    this.captureCtx = null;    // contexte audio d'ENTRÉE (micro)
    this.playbackCtx = null;   // contexte audio de SORTIE (haut-parleurs), forcé à 24 kHz
    this.workletNode = null;   // le worklet qui rééchantillonne le micro
    this.sourceNode = null;    // la source = le micro branché dans le contexte
    this.analyser = null;      // "sonde" qui mesure le volume de la voix de l'IA (pour l'animation)
    this._freqData = null;     // tampon réutilisé pour lire le spectre

    // Lecture audio : on programme chaque morceau à un instant précis
    // (nextStart) au lieu d'attendre la fin du précédent -> moins de coupures.
    this.nextStart = 0;
    this.scheduledSources = []; // les morceaux audio en cours de lecture
    this.ready = false;         // vrai quand OpenAI a confirmé la session
    this._intentional = false;  // vrai SI c'est nous qui fermons (stop()) -> sinon = coupure subie
  }

  // start() = tout met en route : micro, WebSocket, contextes audio, worklet.
  async start() {
    this.onStatus("🎤 Demande d'autorisation micro…");
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      const e = new Error(
        "🎤 Le micro n'est pas accessible ici. Ouvre l'app en HTTPS (ou via localhost) pour pouvoir parler."
      );
      e.kind = "mic";
      throw e;
    }

    // echoCancellation/noiseSuppression/autoGainControl : l'IA ne s'entend plus,
    // meilleure détection de parole (VAD).
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
          sampleRate: 24000,
        },
      });
    } catch (err) {
      throw friendlyMicError(err); // message clair (micro absent / refusé / occupé)
    }

    this.onStatus("📡 Connexion à OpenAI…");
    this.ws = new WebSocket(
      `wss://api.openai.com/v1/realtime?model=${encodeURIComponent(this.model)}`,
      ["realtime", "openai-insecure-api-key." + this.token]
    );

    this.captureCtx = new (window.AudioContext || window.webkitAudioContext)();
    this.playbackCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
    // Politique autoplay de Chrome : on réveille les contextes après le geste utilisateur.
    try { await this.captureCtx.resume(); } catch (_) {}
    try { await this.playbackCtx.resume(); } catch (_) {}

    // Analyseur branché AVANT les haut-parleurs : chaque son de l'IA passe par lui,
    // ce qui nous permet de mesurer le volume en direct pour animer l'avatar.
    //   source audio ─▶ analyser ─▶ destination (HP)
    this.analyser = this.playbackCtx.createAnalyser();
    this.analyser.fftSize = 256;
    this.analyser.smoothingTimeConstant = 0.8; // lissage : évite que ça tremble trop
    this.analyser.connect(this.playbackCtx.destination);
    this._freqData = new Uint8Array(this.analyser.frequencyBinCount);

    // On charge le worklet (capture-processor.js) et on branche le micro dedans.
    await this.captureCtx.audioWorklet.addModule("capture-processor.js?v=2");
    this.sourceNode = this.captureCtx.createMediaStreamSource(this.stream);
    this.workletNode = new AudioWorkletNode(this.captureCtx, "capture-processor");

    // Le worklet nous renvoie des paquets audio PCM16 prêts à envoyer.
    // On les encode en base64 et on les pousse dans le WebSocket vers OpenAI.
    this.workletNode.port.onmessage = (e) => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN && this.ready) {
        const b64 = this._arrayBufferToBase64(e.data);
        this.ws.send(JSON.stringify({ type: "input_audio_buffer.append", audio: b64 }));
      }
    };

    this._wireSocket();
  }

  // Branche tous les gestionnaires d'évènements du WebSocket (le "standard
  // téléphonique" : selon le type de message reçu, on réagit différemment).
  _wireSocket() {
    this.ws.onopen = () => this.onStatus("⏳ Connexion établie, attente du serveur…");

    this.ws.onclose = (event) => {
      this.ready = false;
      this._teardownAudio();
      // 2e argument : true si la fermeture est volontaire (on a appelé stop()),
      // false si c'est une coupure subie (réseau coupé, token expiré...). L'UI
      // s'en sert pour n'alerter l'utilisateur QUE sur les vraies coupures.
      this.onClose(event, this._intentional);
    };

    this.ws.onerror = () => this.onError("Erreur de connexion WebSocket.");

    this.ws.onmessage = (event) => {
      // L'API peut émettre des frames binaires (Blob/ArrayBuffer) ou non-JSON :
      // on les ignore au lieu de laisser planter le handler (ce qui ferait perdre
      // tous les messages suivants).
      if (typeof event.data !== "string") return;
      let msg;
      try { msg = JSON.parse(event.data); } catch (_) { return; }
      const type = msg.type || "";

      // Log de diagnostic (on masque les deltas audio pour ne pas inonder la console).
      if (!type.endsWith("audio.delta")) console.log("<<< IA:", type);

      // --- Session prête (ancien & nouveau schéma) ---
      if (type === "session.created" || type === "session.updated") {
        if (!this.ready) {
          this.ready = true;
          this.sourceNode.connect(this.workletNode);
          this.onStatus("🟢 Connecté ! Parlez maintenant.");
          // En mode cours, on demande à l'IA de prendre la parole en premier.
          if (this.greetFirst) {
            try { this.ws.send(JSON.stringify({ type: "response.create" })); } catch (_) {}
          }
        }
        return;
      }

      // --- Audio de l'IA : noms d'évènements ancien ET nouveau ---
      if (type === "response.audio.delta" || type === "response.output_audio.delta") {
        this._enqueueAudio(msg.delta);
        this.onAiSpeaking(true);
        return;
      }
      if (
        type === "response.audio.done" ||
        type === "response.output_audio.done" ||
        type === "response.done"
      ) {
        this.onAiSpeaking(false);
        // Ces évènements = l'IA a fini de générer sa réponse (≠ file audio
        // momentanément vide entre deux phrases). On le signale pour clore proprement.
        this.onResponseDone();
        return;
      }

      // --- VAD utilisateur ---
      if (type === "input_audio_buffer.speech_started") {
        this._stopPlayback(); // barge-in
        this.onUserSpeaking(true);
        return;
      }
      if (type === "input_audio_buffer.speech_stopped") {
        this.onUserSpeaking(false);
        // L'utilisateur a fini de parler : on le signale TOUT DE SUITE pour
        // réserver sa bulle AVANT la réponse de l'IA (le texte Whisper, lui,
        // arrivera en différé via ...transcription.completed).
        this.onUserSpeechStopped();
        return;
      }

      // --- Sous-titres IA --- (on transmet l'item_id pour que chaque réponse
      // ait SA propre bulle : évite qu'un 'done' tardif d'une réponse interrompue
      // vienne écraser/dupliquer la bulle de la réponse suivante.)
      if (
        type === "response.audio_transcript.delta" ||
        type === "response.output_audio_transcript.delta"
      ) {
        this.onTranscript("ai", msg.delta || "", false, msg.item_id);
        return;
      }
      if (
        type === "response.audio_transcript.done" ||
        type === "response.output_audio_transcript.done"
      ) {
        this.onTranscript("ai", msg.transcript || "", true, msg.item_id);
        return;
      }

      // --- Sous-titres utilisateur ---
      if (type === "conversation.item.input_audio_transcription.delta") {
        this.onTranscript("user", msg.delta || "", false, msg.item_id);
        return;
      }
      if (type === "conversation.item.input_audio_transcription.completed") {
        this.onTranscript("user", msg.transcript || "", true, msg.item_id);
        return;
      }

      if (type === "error") {
        console.error("❌ OpenAI:", msg.error);
        this.onError("Erreur OpenAI : " + JSON.stringify(msg.error));
        return;
      }
    };
  }

  // ---- Lecture audio : on planifie chaque chunk dès l'arrivée (pas d'attente onended). ----
  // OpenAI envoie l'audio en petits morceaux ("deltas") encodés en base64.
  // Ici on : 1) décode le base64, 2) reconvertit PCM16 -> float, 3) crée un
  // petit buffer audio, 4) le programme pour qu'il s'enchaîne pile après le précédent.
  _enqueueAudio(b64delta) {
    const binary = atob(b64delta);                       // base64 -> octets bruts
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    const pcm16 = new Int16Array(bytes.buffer);          // octets -> entiers 16 bits
    const float32 = new Float32Array(pcm16.length);
    for (let i = 0; i < pcm16.length; i++) float32[i] = pcm16[i] / 0x7fff; // -> -1.0..1.0

    const buffer = this.playbackCtx.createBuffer(1, float32.length, 24000);
    buffer.getChannelData(0).set(float32);
    const src = this.playbackCtx.createBufferSource();
    src.buffer = buffer;
    // On passe par l'analyseur (qui est lui-même branché aux haut-parleurs).
    src.connect(this.analyser || this.playbackCtx.destination);

    const now = this.playbackCtx.currentTime;
    if (this.nextStart < now) this.nextStart = now;
    src.start(this.nextStart);
    this.nextStart += buffer.duration;

    this.scheduledSources.push(src);
    src.onended = () => {
      this.scheduledSources = this.scheduledSources.filter((s) => s !== src);
      if (this.scheduledSources.length === 0) {
        this.onAiSpeaking(false);
        this.onPlaybackDrained(); // l'IA a fini de parler : utile pour clore après un adieu
      }
    };
  }

  // Coupe immédiatement toute la lecture en cours (utilisé pour le "barge-in" :
  // quand l'utilisateur reprend la parole, on fait taire l'IA aussitôt).
  _stopPlayback() {
    for (const s of this.scheduledSources) {
      try { s.stop(); } catch (_) {}
    }
    this.scheduledSources = [];
    this.nextStart = this.playbackCtx ? this.playbackCtx.currentTime : 0;
    this.onAiSpeaking(false);
  }

  // Libère proprement le micro et ferme les contextes audio (fin d'appel).
  _teardownAudio() {
    this._stopPlayback();
    if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
    if (this.captureCtx) this.captureCtx.close().catch(() => {});
    if (this.playbackCtx) this.playbackCtx.close().catch(() => {});
  }

  // Vrai s'il reste de l'audio en cours de lecture (sert à ne pas couper l'IA).
  isPlaying() {
    return this.scheduledSources.length > 0;
  }

  // Niveau sonore global de la voix de l'IA, entre 0 (silence) et ~1 (fort).
  // Sert à faire "respirer"/pulser l'avatar au rythme de la parole.
  getOutputLevel() {
    if (!this.analyser) return 0;
    try { this.analyser.getByteFrequencyData(this._freqData); } catch (_) { return 0; }
    let sum = 0;
    for (let i = 0; i < this._freqData.length; i++) sum += this._freqData[i];
    const avg = sum / this._freqData.length / 255; // moyenne normalisée 0..1
    return Math.min(1, avg * 2.2);                  // on amplifie un peu pour l'effet visuel
  }

  // Renvoie `bands` valeurs 0..1 = un petit spectre, pour dessiner un égaliseur
  // qui "danse" avec la voix de l'IA.
  getSpectrum(bands) {
    const out = new Array(bands).fill(0);
    if (!this.analyser) return out;
    try { this.analyser.getByteFrequencyData(this._freqData); } catch (_) { return out; }
    // On garde surtout les basses/médiums (la voix), puis on regroupe en `bands` paquets.
    const usable = Math.floor(this._freqData.length * 0.7);
    const size = Math.max(1, Math.floor(usable / bands));
    for (let b = 0; b < bands; b++) {
      let sum = 0;
      for (let i = 0; i < size; i++) sum += this._freqData[b * size + i] || 0;
      out[b] = Math.min(1, (sum / size / 255) * 1.6);
    }
    return out;
  }

  // stop() = raccrocher : ferme le WebSocket et coupe l'audio. Appelé par l'UI.
  stop() {
    this._intentional = true; // on marque AVANT close() : la fermeture est voulue
    this.ready = false;
    if (this.ws) {
      try { this.ws.close(); } catch (_) {}
    }
    this._teardownAudio();
  }

  // Convertit un buffer binaire en base64 (format texte attendu par l'API).
  // Le découpage par tranches de 0x8000 évite de dépasser la limite d'arguments
  // de String.fromCharCode sur les gros buffers.
  _arrayBufferToBase64(buffer) {
    let binary = "";
    const bytes = new Uint8Array(buffer);
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return btoa(binary);
  }
}
