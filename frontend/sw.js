/* Service worker — English SpeakApp (PWA)
 * Objectifs : lancement hors-ligne (coquille de l'app) + chargements plus rapides.
 * Stratégies :
 *   - navigation (HTML)      -> network-first, repli sur l'index en cache (offline)
 *   - statiques même origine -> stale-while-revalidate (sert vite, met à jour en fond)
 *   - /api/ et realtime      -> JAMAIS de cache (toujours le réseau)
 * Le cache-busting "?v=N" des fichiers fait que chaque version a sa propre URL :
 * bumper la version récupère naturellement le nouveau fichier.
 */
const SW_VERSION = "v9";
const CACHE = "speakapp-" + SW_VERSION;
const APP_SHELL = [
  "/",
  "/index.html",
  "/manifest.webmanifest",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(APP_SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;        // tiers (fonts, OpenAI…) -> réseau direct
  if (url.pathname.startsWith("/api/")) return;            // API -> jamais de cache

  // Navigation -> network-first (repli index hors-ligne)
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put("/index.html", copy));
          return res;
        })
        .catch(() => caches.match("/index.html").then((r) => r || caches.match("/")))
    );
    return;
  }

  // Statiques -> stale-while-revalidate
  event.respondWith(
    caches.match(req).then((cached) => {
      const network = fetch(req)
        .then((res) => {
          if (res && res.status === 200) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(req, copy));
          }
          return res;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});
