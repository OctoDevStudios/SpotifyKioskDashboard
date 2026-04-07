# 🎵 Spotify Kiosk Dashboard

Interface kiosque tactile locale pour afficher et contrôler la lecture Spotify via une instance Flask (SSE + interpolation côté client).

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python: 3.8+](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://www.python.org/)
[![Microsoft Edge](https://custom-icon-badges.demolab.com/badge/Microsoft%20Edge-2771D8?logo=edge-white&logoColor=white)](https://www.microsoft.com/fr-fr/edge/)

---

🎯 Matériel Cible — Microsoft Surface Go 2

- Écran : 10.5" PixelSense tactile, 1920 × 1280 (220 ppi), format 3:2, Gorilla Glass 3
- CPU : Intel Pentium Gold 4425Y ou Intel Core m3-8100Y (selon modèle)
- GPU : Intel UHD Graphics 615
- RAM : 4 GB / 8 GB
- Stockage : 64 GB eMMC ou 128 / 256 GB NVMe SSD
- Connectivité : USB‑C (Power Delivery), microSDXC, jack 3.5 mm, option LTE
- Batterie : ≈10 heures d'utilisation typique

- Conseil BIOS : La Surface Go 2 permet d'activer dans le UEFI le mode kiosque qui limite de charge à ~50% afin de préserver la batterie et éviter une usure prématurée.

Optimisations d'interface pour tactile : commandes oversize, zones tactiles dédiées, navigation en plein écran et suppression du curseur en mode kiosque.

---

⚡ Optimisations Techniques (les 4 piliers)

1) Polling adaptatif

- Sondage dynamique selon état de lecture pour réduire les appels Spotify : idle (30s), playing long (20s), playing med (5s), playing short (2s).
- Rafales courtes (`FAST_POLL`) d'environ 1.5s x3 après événements (changement de piste, contrôle utilisateur) pour réactivité.

2) Interpolation côté client (smooth UI)

- Le client interpole localement la progression (tick ~100ms / requestAnimationFrame) pour une UI fluide entre les resynchronisations serveur.
- Le serveur reste la source de vérité et force une resync si l'écart dépasse un seuil (ex. >2s).

3) Réduction SSE & payloads minimaux

- `/stream` envoie uniquement diffs et événements signifiants (changement de piste, play/pause, overrides), pas des payloads complets en continu.
- Heartbeat et backoff en idle pour minimiser l'utilisation réseau/CPU.

4) Debounce & persistance optimisée (protection SSD)

- Écritures disque débouncées (ex : `quota_state.json`, `playlist_cache.json`) avec intervalle de flush par défaut (≈60s).
- Sauvegarde forcée au shutdown pour garantir l'intégrité des données.

---

⚙️ Installation & Configuration

Installation rapide (Windows) :

```powershell
cd "<chemin_du_projet>"
.\install.bat
```

Le script `install.bat` installe directement les dépendances listées dans `requirements.txt` (installation globale via `pip`).
Note: ce script n'utilise pas d'environnement virtuel par défaut.

Configuration : copiez et éditez ` .env ` :

```powershell
copy .env.example .env
notepad .env
```

Variables `.env` (essentielles et courantes) :

| Variable | Requise | Description |
|---|---:|---|
| SPOTIPY_CLIENT_ID | Oui | Client ID de l'app Spotify |
| SPOTIPY_CLIENT_SECRET | Oui | Client Secret de l'app Spotify |
| SPOTIPY_REDIRECT_URI | Oui | Redirect URI utilisée pour OAuth |
| DEV_PIN | Non | PIN 4 chiffres pour accéder au panneau dev |
| REQUIRE_CONTROL_PIN | Non | `true`/`false` — appliquer le PIN aux `/control/*` |
| HOST | Non | Interface d'écoute (par défaut `127.0.0.1`) |
| PORT | Non | Port d'écoute (par défaut `8080`) |
| WEATHER_CITY | Non | Ville pour la météo (ex: `paris`) |

---

🚀 Lancement

- Recommandé (Windows) :

```powershell
cd "<chemin_du_projet>"
.\launch.bat
```

- Démarrage manuel :

```powershell
cd "<chemin_du_projet>"
$env:HOST='127.0.0.1'; $env:PORT='8080'; python app.py
```

`launch.bat` : lit `.env` si présent, démarre `app.py`, attend la disponibilité du serveur puis ouvre Microsoft Edge en mode kiosque sur l'URL configurée.

---

🛠️ Panneau Développeur

- Ouvrir : taper 5 fois sur la zone secrète en haut‑droite (`#secret-zone`) pour afficher la saisie PIN.
- Accès : saisir `DEV_PIN` (valeur définie dans `.env`). Les endpoints `/dev/*` sont protégés côté serveur et acceptent le PIN via :
  - header `X-DEV-PIN`, ou
  - `Authorization: Bearer <PIN>`, ou
  - paramètre `?pin=` ou JSON `{ "pin": "..." }`.
- Actions disponibles : reset météo, overrides système (CPU/RAM/BAT), forcer/lâcher veille, réglage brightness, reconnect Spotify, etc.

---

🔔 Notifications SMS (Free Mobile)

Vous pouvez configurer l'envoi de notifications SMS via l'API Free Mobile pour recevoir des alertes sur votre téléphone lorsque certains événements critiques se produisent (mauvais code PIN dev, dépassement de quota Spotify, ou recevoir manuellement les métriques via le panneau développeur).

Comment ça marche : utilisez votre identifiant Free et la clé d'identification fournie par Free Mobile, et le serveur appellera `https://smsapi.free-mobile.fr/sendmsg` en POST (`user`, `pass`, `msg`).

Variables `.env` associées :

```
FREE_SMS_ENABLED=false
FREE_SMS_USER=
FREE_SMS_PASS=
FREE_SMS_NOTIFY_PIN=true
FREE_SMS_NOTIFY_QUOTA=true
FREE_SMS_NOTIFY_METRICS=true
FREE_SMS_MIN_INTERVAL=60
```

- `FREE_SMS_ENABLED` : activer/désactiver globalement l'envoi de SMS.
- `FREE_SMS_USER` / `FREE_SMS_PASS` : identifiants Free Mobile (ne pas committer vos vraies clés).
- `FREE_SMS_NOTIFY_PIN` : envoyer un SMS en cas de tentative de connexion dev avec un PIN invalide.
- `FREE_SMS_NOTIFY_QUOTA` : envoyer un SMS quand l'application détecte un dépassement de quota Spotify (429 ou quota local).
- `FREE_SMS_NOTIFY_METRICS` : autorise l'envoi manuel des métriques via le bouton "Envoyer Metrics SMS" dans le panneau développeur.
- `FREE_SMS_MIN_INTERVAL` : intervalle minimum (secondes) entre deux SMS pour le même type d'événement (protection anti-spam).

Comportement UI : le bouton "Envoyer Metrics SMS" dans le panneau développeur n'apparaît que si `FREE_SMS_ENABLED=true` ET `FREE_SMS_NOTIFY_METRICS=true`.

---

📜 Licence

Ce projet est distribué sous licence **MIT** — voir le fichier `LICENSE` pour le texte complet.

