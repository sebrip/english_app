/*
 * AudioWorklet de capture micro.
 * Tourne sur le thread audio dédié (pas le thread UI) => zéro glitch, faible latence.
 * Rôle : rééchantillonner le flux micro (souvent 48 kHz) vers 24 kHz attendu par
 * l'API Realtime, convertir en PCM16, et poster des paquets vers le thread principal.
 */
class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.targetRate = 24000;
    this.ratio = sampleRate / this.targetRate; // sampleRate = taux réel du contexte
    this._acc = [];        // échantillons en attente (float32)
    this._flushSize = 2048; // ~85 ms à 24 kHz : compromis latence / nombre de messages
    this._muted = false;
    this.port.onmessage = (e) => {
      if (e.data && e.data.type === "mute") this._muted = e.data.value;
    };
  }

  _downsampleAndQueue(input) {
    const ratio = this.ratio;
    if (ratio === 1) {
      for (let i = 0; i < input.length; i++) this._acc.push(input[i]);
      return;
    }
    // Moyenne par fenêtre — anti-aliasing léger et peu coûteux.
    const newLen = Math.round(input.length / ratio);
    let offset = 0;
    for (let i = 0; i < newLen; i++) {
      const next = Math.round((i + 1) * ratio);
      let sum = 0, count = 0;
      for (let j = offset; j < next && j < input.length; j++) { sum += input[j]; count++; }
      this._acc.push(count ? sum / count : 0);
      offset = next;
    }
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    if (this._muted) return true; // on coupe l'entrée pendant que l'IA parle (anti-écho)

    this._downsampleAndQueue(input[0]);

    while (this._acc.length >= this._flushSize) {
      const slice = this._acc.splice(0, this._flushSize);
      const pcm16 = new Int16Array(slice.length);
      for (let i = 0; i < slice.length; i++) {
        const s = Math.max(-1, Math.min(1, slice[i]));
        pcm16[i] = s * 0x7fff;
      }
      // Transfert (zero-copy) du buffer vers le thread principal.
      this.port.postMessage(pcm16.buffer, [pcm16.buffer]);
    }
    return true;
  }
}

registerProcessor("capture-processor", CaptureProcessor);
