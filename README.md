# TurboDL

**FR** — Gestionnaire de téléchargements pour Windows et Linux : liens directs multi-connexions et BitTorrent (magnet / .torrent) dans une seule interface, avec reprise automatique et extension navigateur.
**EN** — Download manager for Windows and Linux: multi-connection direct links and BitTorrent (magnet / .torrent) in one window, with automatic resume and a browser extension.

| Sombre / Dark | Clair / Light |
|---|---|
| ![TurboDL sombre](docs/turbodl-sombre.png) | ![TurboDL clair](docs/turbodl-clair.png) |

![TurboDL 1.8.0 sous Linux / on Linux](docs/turbodl-linux-sombre.png)

---

## Français

### Fonctions
- **Liens directs** découpés en segments (jusqu'à 32 connexions par fichier), reprise après coupure.
- **BitTorrent** via libtorrent : magnets, fichiers .torrent, DHT, découverte locale, UPnP/NAT-PMP, trackers publics ajoutés automatiquement.
- **File d'attente** : nombre de téléchargements simultanés réglable.
- **Historique** (`%LOCALAPPDATA%\TurboDL\historique.json`, Linux : `~/.local/share/TurboDL/`) : reprise automatique au redémarrage de ce qui était en cours.
- **Extension navigateur** (Chrome/Edge) pour envoyer un lien à TurboDL d'un clic.
- **Limite de débit** réglable (liens directs et torrents), pour garder de la bande passante pour le reste.
- Thèmes clair et sombre (suit Windows ou le bureau Linux), mise en veille bloquée pendant les téléchargements.

### Installer
- **Windows** : `TurboDL-1.7.1.exe` sur [kdl-tech.fr](https://kdl-tech.fr/logiciels/turbodl/).
- **Linux (Debian, Ubuntu, Mint…)** : `turbodl_1.8.0_all.deb` sur [kdl-tech.fr](https://kdl-tech.fr/logiciels/turbodl/), puis `sudo apt install ./turbodl_1.8.0_all.deb` (installe aussi Python, Tk et libtorrent).

### Nouveautés 1.8.0
- **Version Linux** : paquet .deb, dossier Téléchargements de la session, ouverture des fichiers et dossiers avec les programmes du bureau, association des .torrent et des liens magnet (xdg-mime), thème clair/sombre du bureau, mise en veille bloquée (systemd-inhibit).
- **Limite de débit** : puce « Débit » (illimité à 50 Mo/s), appliquée aux liens directs et aux torrents.
- **Notification du bureau** quand un téléchargement se termine (Linux).
- Compteurs du tableau de bord : libellés jamais coupés.

### Nouveautés 1.7.1
- Reprise rapide des torrents : plus de revérification complète à chaque lancement.
- Logo KDL TECH dans l'application, couleurs renforcées, état « Vérification » clair.

### Nouveautés 1.7.0
- Un torrent bloqué à 100 % se termine désormais tout seul.
- Un élément retiré de la liste ne revient plus à chaque lancement (il reste dans l'historique).
- Moteur torrent plus rapide : plus de pairs, connexions plus rapides, files de requêtes plus longues.
- Interface plus colorée : barres de progression en dégradé, catégories et compteurs en couleur.

### Lancer depuis le source
```
pip install libtorrent certifi
python turbodl.py
```
Linux : `sudo apt install python3-tk python3-libtorrent` puis `python3 turbodl.py`. Paquet : `sh packaging/construire-deb.sh`.

### Compiler l'exe
```
pip install pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name TurboDL --icon turbodl.ico ^
  --hidden-import certifi --collect-data certifi --collect-all libtorrent ^
  --add-data "extension;extension" --add-data "turbodl.ico;." turbodl.py
```

---

## English

### Features
- **Direct links** split into segments (up to 32 connections per file), resumable.
- **BitTorrent** via libtorrent: magnets, .torrent files, DHT, local discovery, UPnP/NAT-PMP, public trackers added automatically.
- **Queue** with a configurable number of parallel downloads.
- **History** (`%LOCALAPPDATA%\TurboDL\historique.json`, Linux: `~/.local/share/TurboDL/`): unfinished downloads resume on restart.
- **Browser extension** (Chrome/Edge) to send a link to TurboDL in one click.
- **Speed limit** (direct links and torrents) to keep bandwidth for everything else.
- Light and dark themes (follows Windows or the Linux desktop); sleep is blocked while downloading.

### Install
- **Windows**: `TurboDL-1.7.1.exe` from [kdl-tech.fr](https://kdl-tech.fr/en/software/turbodl/).
- **Linux (Debian, Ubuntu, Mint…)**: `turbodl_1.8.0_all.deb` from [kdl-tech.fr](https://kdl-tech.fr/en/software/turbodl/), then `sudo apt install ./turbodl_1.8.0_all.deb`.

### What's new in 1.8.0
- **Linux version**: .deb package, session Downloads folder, files and folders opened with desktop apps, .torrent and magnet association (xdg-mime), desktop light/dark theme, sleep blocked (systemd-inhibit).
- **Speed limit**: "Speed" chip (unlimited to 50 MB/s), for direct links and torrents.
- **Desktop notification** when a download finishes (Linux).
- Dashboard counters: labels never cut off.

### What's new in 1.7.1
- Torrent fast resume: no full re-check on every launch.
- KDL TECH logo in the app, stronger colours, clear "checking" state.

### What's new in 1.7.0
- Torrents stuck at 100 % now complete on their own.
- Items removed from the list no longer come back on every launch (they stay in the history).
- Faster torrent engine: more peers, faster connection ramp-up, deeper request queues.
- More colourful UI: gradient progress bars, coloured categories and counters.

### Run from source / build
See the commands in the French section above (`python turbodl.py`, PyInstaller one-file build).

---

Licence : gratuiciel KDL TECH (code source non libre) — © KDL TECH. Voir [LICENSE](LICENSE).
