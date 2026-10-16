#!/usr/bin/env python3
"""TurboDL — accélérateur de téléchargement multi-connexions.

Principe (comme IDM / aria2) : le fichier est découpé en segments téléchargés
en parallèle via des requêtes HTTP « Range ». Quand une connexion termine son
segment, elle « vole » la moitié du plus gros segment restant, pour que toutes
les connexions travaillent jusqu'à la dernière seconde. L'état est sauvegardé
en continu : un téléchargement interrompu reprend là où il s'était arrêté.

Torrents : liens magnet et fichiers .torrent via libtorrent (même moteur
que qBittorrent).

Usage :
    python turbodl.py                         -> interface graphique
    python turbodl.py fichier.torrent|magnet  -> interface, téléchargement lancé
    python turbodl.py URL [-c 16] [-o DOSSIER] -> ligne de commande
"""
import argparse
import http.client
import hashlib
import json
import math
import os
import queue
import re
import shutil
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.7.0"
UA = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) TurboDL/{VERSION}"
CHUNK = 256 * 1024             # taille de lecture réseau
MIN_SPLIT = 2 * 1024 * 1024    # on ne recoupe pas un segment restant < 2 Mo
MAX_RETRIES = 8
NET_ERRORS = (urllib.error.URLError, OSError, http.client.HTTPException)
BRIDGE_PORT = 47321
EXT_ORIGIN = "chrome-extension://meihfeagmipglccincpdabodlikepcmo"  # ID fixé par la clé du manifest
PRIVATE_HEADERS = ("Cookie", "Authorization")


# --------------------------------------------------------------------- réseau
def _ssl_context():
    """Magasin Windows + liste Mozilla (certifi) : Windows ne contient pas
    toujours les racines récentes, que Python ne sait pas télécharger seul."""
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except (ImportError, OSError, ssl.SSLError):
        pass
    return ctx


SSL_CTX = _ssl_context()


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Ne transmet pas les cookies du navigateur à un autre domaine."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and not same_host(newurl, req.full_url):
            for h in PRIVATE_HEADERS:
                new.remove_header(h)
        return new


OPENER = urllib.request.build_opener(urllib.request.HTTPSHandler(context=SSL_CTX), _SafeRedirect)


def open_url(url, headers=None, timeout=30):
    h = {"User-Agent": UA, "Accept-Encoding": "identity"}
    if headers:
        h.update(headers)
    return OPENER.open(urllib.request.Request(url, headers=h), timeout=timeout)


def same_host(a, b):
    return urllib.parse.urlparse(a).hostname == urllib.parse.urlparse(b).hostname


def filename_from(content_disposition, url):
    name = None
    if content_disposition:
        m = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", content_disposition, re.I)
        if m:
            name = urllib.parse.unquote(m.group(1).strip().strip('"'))
        else:
            m = re.search(r'filename\s*=\s*"?([^";]+)"?', content_disposition, re.I)
            if m:
                name = m.group(1).strip()
    if not name:
        name = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(url).path))
    return clean_name(name) or "telechargement"


def clean_name(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or "")).strip(" .")


def probe(url, headers=None):
    """Interroge le serveur : (url finale, taille ou None, Range supporté, nom)."""
    with open_url(url, {**(headers or {}), "Range": "bytes=0-0"}) as r:
        final = r.geturl()
        cd = r.headers.get("Content-Disposition", "")
        ctype = r.headers.get("Content-Type", "").lower()
        if ctype.startswith("text/html") and "attachment" not in cd.lower():
            raise ValueError(
                "Ce lien mène à une page web, pas à un fichier. Ouvrez-le dans le "
                "navigateur, lancez le téléchargement, puis copiez le lien direct "
                "(liste des téléchargements du navigateur > clic droit > Copier le lien).")
        if r.status == 206:
            m = re.search(r"/(\d+)\s*$", r.headers.get("Content-Range", ""))
            size = int(m.group(1)) if m else None
            ranges = size is not None
        else:
            cl = r.headers.get("Content-Length")
            size = int(cl) if cl and cl.isdigit() else None
            ranges = False
    return final, size, ranges, filename_from(cd, final)


def unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


def human(n):
    n = float(n or 0)
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if n < 1024 or unit == "To":
            return f"{n:.0f} {unit}" if unit == "o" else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024


def friendly_error(e):
    """Traduit une erreur réseau en phrase compréhensible, avec la marche à suivre."""
    code = getattr(e, "code", None)
    known = {
        401: "Le site demande d'être connecté (401). Lancez le téléchargement depuis le "
             "navigateur, connecté au site : l'extension transmettra votre session.",
        403: "Accès refusé par le serveur (403). Le lien a peut-être expiré : relancez-le "
             "depuis la page d'origine.",
        404: "Fichier introuvable sur le serveur (404). Le lien est peut-être expiré ou erroné.",
        410: "Ce fichier n'est plus disponible sur le serveur (410).",
        429: "Le serveur limite les téléchargements (429). Réessayez dans quelques minutes.",
        503: "Serveur momentanément indisponible (503). Réessayez plus tard.",
    }
    if code in known:
        return known[code]
    if isinstance(code, int):
        return f"Le serveur a répondu par une erreur {code}."
    reason = getattr(e, "reason", e)
    if isinstance(reason, ssl.SSLError):
        return "Connexion sécurisée impossible avec ce site (certificat refusé)."
    if isinstance(reason, (TimeoutError,)) or "timed out" in str(reason):
        return "Le serveur ne répond pas (délai dépassé). Vérifiez votre connexion et réessayez."
    if isinstance(reason, ConnectionRefusedError):
        return "Le serveur refuse la connexion."
    if "getaddrinfo" in str(reason) or "Name or service" in str(reason):
        return "Adresse introuvable : vérifiez le lien et votre connexion internet."
    return str(e) or e.__class__.__name__


def human_time(s):
    if s is None or s == float("inf"):
        return "—"
    s = int(s)
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s" if m else f"{s}s"


# ------------------------------------------------------------ outils système
def app_dir(*parts):
    d = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "TurboDL", *parts)
    os.makedirs(d, exist_ok=True)
    return d


def check_space(folder, need):
    """Refuse de démarrer si le disque ne peut pas contenir le fichier."""
    free = shutil.disk_usage(folder).free
    if need and free < need + 100 * 1024 * 1024:
        raise IOError(f"Espace disque insuffisant : il faut {human(need)}, "
                      f"il reste {human(free)}. Choisissez un autre dossier ou libérez de la place.")


def keep_awake(on):
    """Empêche la mise en veille automatique pendant un téléchargement."""
    if sys.platform != "win32":
        return
    import ctypes
    es_continuous, es_system_required = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(
        es_continuous | (es_system_required if on else 0))


PREFERRED_DIRS = [r"D:\Téléchargements"]   # disque de stockage, avant le disque système


def default_dir():
    for d in PREFERRED_DIRS:
        if os.path.isdir(d):
            return d
    return os.path.join(os.path.expanduser("~"), "Downloads")


def load_settings():
    try:
        with open(os.path.join(app_dir(), "config.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(**kw):
    data = load_settings()
    data.update(kw)
    path = os.path.join(app_dir(), "config.json")
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(path + ".tmp", path)


# ------------------------------------------------------------- mémoire
STATUS_FR = {"waiting": "en attente", "running": "en cours", "paused": "en pause",
             "done": "terminé", "error": "erreur", "interrupted": "interrompu",
             "removed": "retiré de la liste"}


def history_id(source, out_dir):
    return hashlib.sha1(f"{source}|{os.path.abspath(out_dir)}".encode()).hexdigest()[:16]


class History:
    """Mémoire des téléchargements (%LOCALAPPDATA%\\TurboDL\\historique.json).
    On y garde le lien, la page d'origine, le dossier et l'état ; jamais les
    cookies. Les .torrent sont copiés dans TurboDL\\torrents pour pouvoir
    reprendre même si le fichier d'origine a disparu."""

    def __init__(self, path=None):
        self.path = path or os.path.join(app_dir(), "historique.json")
        self.lock = threading.RLock()
        try:
            with open(self.path, encoding="utf-8") as fh:
                self.items = json.load(fh)
            if not isinstance(self.items, list):
                self.items = []
        except (OSError, ValueError):
            self.items = []

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.items, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def get(self, hid):
        with self.lock:
            return next((e for e in self.items if e.get("id") == hid), None)

    def upsert(self, hid, **fields):
        with self.lock:
            e = self.get(hid)
            if e is None:
                e = {"id": hid, "added": time.strftime("%Y-%m-%d %H:%M")}
                self.items.insert(0, e)
            e.update({k: v for k, v in fields.items() if v is not None})
            e["updated"] = time.strftime("%Y-%m-%d %H:%M")
            self._save()
            return dict(e)

    def remove(self, hid):
        with self.lock:
            e = self.get(hid)
            self.items = [x for x in self.items if x.get("id") != hid]
            self._save()
        tf = e and e.get("torrent_file")
        if tf and os.path.isfile(tf) and not any(x.get("torrent_file") == tf for x in self.items):
            os.remove(tf)

    def mark_unfinished_interrupted(self):
        """Au démarrage : ce qui était en cours/en attente a été coupé."""
        with self.lock:
            out = []
            for e in self.items:
                if e.get("status") in ("running", "waiting", "interrupted"):
                    e["status"] = "interrupted"
                    out.append(dict(e))
            if out:
                self._save()
            return out

    def snapshot(self):
        with self.lock:
            return [dict(e) for e in self.items]


def job_from_entry(e):
    tf = e.get("torrent_file")
    return {"url": tf if tf and os.path.isfile(tf) else e["source"], "hid": e["id"],
            "out_dir": e.get("out_dir"), "origin": e["source"], "page": e.get("page"),
            "name": e.get("name"), "torrent": e.get("kind") == "torrent"}


# ----------------------------------------------------------------- moteur
class Segment:
    __slots__ = ("start", "end", "pos", "active")

    def __init__(self, start, end, pos=None):
        self.start, self.end = start, end          # end inclus
        self.pos = start if pos is None else pos   # prochain octet à écrire
        self.active = False

    @property
    def remaining(self):
        return self.end - self.pos + 1


class Downloader:
    def __init__(self, url, out_dir=".", connections=16, filename=None, headers=None):
        if urllib.parse.urlparse(url).scheme not in ("http", "https"):
            raise ValueError("Seuls les liens http:// et https:// sont acceptés.")
        self.src_url = url
        self.url = url
        self.out_dir = out_dir
        self.connections = max(1, min(64, int(connections)))
        self.filename = filename
        self.headers = dict(headers or {})   # cookies/referer du navigateur (jamais sur disque)
        self.size = None
        self.ranges = False
        self.segments = []
        self.downloaded = 0
        self.speed = 0.0
        self.status = "init"     # init, running, paused, done, error
        self.error = None
        self.final_path = None
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()

    # -- état / reprise
    def _prepare(self):
        self.url, self.size, self.ranges, name = probe(self.src_url, self.headers)
        if not same_host(self.url, self.src_url):
            self.headers = {k: v for k, v in self.headers.items() if k not in PRIVATE_HEADERS}
        self.filename = self.filename or name
        out = os.path.abspath(self.out_dir)
        if sys.platform == "win32" and len(out) > 200 and not out.startswith("\\\\?\\"):
            out = "\\\\?\\" + out              # chemins longs Windows (> 260 car.)
        os.makedirs(out, exist_ok=True)
        self.path = os.path.join(out, self.filename)
        self.part = self.path + ".turbodl"
        self.state_path = self.part + ".json"

    def _load_state(self):
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                st = json.load(fh)
            if (st.get("src") != self.src_url or st.get("size") != self.size
                    or os.path.getsize(self.part) != self.size):
                return None
            return [Segment(s, e, p) for s, e, p in st["segments"]]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _save_state(self):
        with self.lock:
            st = {"src": self.src_url, "size": self.size,
                  "segments": [[s.start, s.end, s.pos] for s in self.segments]}
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh)
        os.replace(tmp, self.state_path)

    # -- API
    def stop(self):
        self.stop_evt.set()

    def snapshot(self):
        with self.lock:
            segs = [(s.start, s.end, s.pos, s.active) for s in self.segments]
            active = sum(1 for s in self.segments if s.active)
        left = (self.size - self.downloaded) if self.size else None
        eta = left / self.speed if (left is not None and self.speed > 0) else None
        return {"status": self.status, "filename": self.filename, "size": self.size,
                "downloaded": self.downloaded, "speed": self.speed, "eta": eta,
                "active": active, "segments": segs, "ranges": self.ranges,
                "error": self.error,
                "path": self.final_path and self.final_path.removeprefix("\\\\?\\")}

    def run(self):
        self.status = "running"
        try:
            self._prepare()
            if self.ranges and self.size:
                self._multi()
            else:
                self._single()
        except Exception as e:  # noqa: BLE001 — remonté à l'interface
            self.error = friendly_error(e)
            self.status = "error"
        return self.status

    # -- téléchargement multi-connexions
    def _multi(self):
        segs = self._load_state()
        if not segs:
            n = max(1, min(self.connections, math.ceil(self.size / MIN_SPLIT)))
            step = self.size // n
            segs = [Segment(i * step, (i + 1) * step - 1 if i < n - 1 else self.size - 1)
                    for i in range(n)]
            check_space(os.path.dirname(self.part), self.size)
            with open(self.part, "wb") as fh:
                fh.truncate(self.size)          # pré-allocation
        self.segments = segs
        self.downloaded = sum(s.pos - s.start for s in segs)

        workers = [threading.Thread(target=self._worker, daemon=True)
                   for _ in range(self.connections)]
        for t in workers:
            t.start()
        self._monitor(lambda: any(t.is_alive() for t in workers), save=True)
        for t in workers:
            t.join()
        self._save_state()

        if all(s.remaining <= 0 for s in self.segments):
            self._finish()
        elif self.stop_evt.is_set():
            self.status = "paused"
        else:
            raise IOError(self.error or "téléchargement incomplet")

    def _monitor(self, alive, save):
        last_bytes, last_t, last_save = self.downloaded, time.monotonic(), 0.0
        while alive():
            time.sleep(0.25)
            now = time.monotonic()
            inst = (self.downloaded - last_bytes) / max(now - last_t, 1e-6)
            self.speed = inst if self.speed == 0 else 0.7 * self.speed + 0.3 * inst
            last_bytes, last_t = self.downloaded, now
            if save and now - last_save > 1.0:
                self._save_state()
                last_save = now

    def _next_segment(self):
        with self.lock:
            for s in self.segments:
                if not s.active and s.remaining > 0:
                    s.active = True
                    return s
            # Vol de travail : on coupe en deux le plus gros segment en cours.
            big = max((s for s in self.segments if s.active),
                      key=lambda s: s.remaining, default=None)
            if big is None or big.remaining < MIN_SPLIT:
                return None
            mid = big.pos + big.remaining // 2
            new = Segment(mid, big.end)
            new.active = True
            big.end = mid - 1
            self.segments.append(new)
            return new

    def _worker(self):
        with open(self.part, "r+b") as fh:
            while not self.stop_evt.is_set():
                seg = self._next_segment()
                if seg is None:
                    return
                try:
                    self._fetch(seg, fh)
                except Exception as e:  # noqa: BLE001
                    self.error = f"{e}"
                finally:
                    with self.lock:
                        seg.active = False

    def _fetch(self, seg, fh):
        retries = 0
        while not self.stop_evt.is_set():
            with self.lock:
                if seg.remaining <= 0:
                    return
                start, end = seg.pos, seg.end
            got = False
            try:
                with open_url(self.url, {**self.headers, "Range": f"bytes={start}-{end}"}) as r:
                    m = re.match(r"bytes (\d+)-", r.headers.get("Content-Range", ""))
                    if r.status != 206 or not m or int(m.group(1)) != start:
                        raise IOError(f"le serveur a refusé la reprise (HTTP {r.status})")
                    while not self.stop_evt.is_set():
                        data = r.read(CHUNK)
                        if not data:
                            break
                        got = True
                        with self.lock:   # l'écriture et l'avancement restent atomiques
                            n = min(len(data), seg.end - seg.pos + 1)
                            if n > 0:
                                fh.seek(seg.pos)
                                fh.write(data[:n])
                                seg.pos += n
                                self.downloaded += n
                            if seg.remaining <= 0:
                                return
                if got:
                    retries = 0
                    continue
                raise IOError("connexion fermée sans données")
            except NET_ERRORS as e:
                retries += 1
                if retries > MAX_RETRIES:
                    raise IOError(f"échec après {MAX_RETRIES} essais : {e}") from e
                code = getattr(e, "code", None)
                wait = min(30.0, 0.5 * 2 ** retries) * (2 if code in (429, 503) else 1)
                self.stop_evt.wait(wait)

    # -- repli : une seule connexion (serveur sans Range)
    def _single(self):
        self.segments = []
        self.downloaded = 0
        done = threading.Event()
        mon = threading.Thread(target=self._monitor,
                               args=(lambda: not done.is_set(), False), daemon=True)
        mon.start()
        if self.size:
            check_space(os.path.dirname(self.part), self.size)
        try:
            with open_url(self.url, self.headers) as r, open(self.part, "wb") as fh:
                while not self.stop_evt.is_set():
                    data = r.read(CHUNK)
                    if not data:
                        break
                    fh.write(data)
                    self.downloaded += len(data)
        finally:
            done.set()
            mon.join()
        if self.stop_evt.is_set():
            os.remove(self.part)   # sans Range, aucune reprise possible
            self.status = "paused"
            return
        if self.size and self.downloaded != self.size:
            raise IOError("fichier incomplet")
        self.size = self.downloaded
        self._finish()

    def _finish(self):
        self.final_path = unique_path(self.path)
        os.replace(self.part, self.final_path)
        try:
            os.remove(self.state_path)
        except OSError:
            pass
        self.status = "done"


# ------------------------------------------------------------- torrents
MAX_TORRENT_FILE = 20 * 1024 * 1024
_LT = {"session": None}
_LT_LOCK = threading.Lock()


def is_torrent(url, hint=False):
    u = (url or "").strip()
    if u.lower().startswith("magnet:") or hint:
        return True
    path = urllib.parse.urlparse(u).path if re.match(r"https?://", u, re.I) else u
    return path.lower().endswith(".torrent")


def magnet_name(url):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return clean_name((q.get("dn") or ["torrent"])[0])


EXTRA_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://explodie.org:6969/announce",
    "udp://tracker.qu.ax:6969/announce",
]


def lt_session():
    """Une seule session BitTorrent partagée (DHT, découverte locale, UPnP)."""
    import libtorrent as lt
    with _LT_LOCK:
        if _LT["session"] is None:
            _LT["session"] = lt.session({
                "user_agent": f"TurboDL/{VERSION}",
                "listen_interfaces": "0.0.0.0:6881,[::]:6881",
                "enable_dht": True, "enable_lsd": True,
                "enable_upnp": True, "enable_natpmp": True,
                # vitesse : plus de pairs, connexions plus rapides, files de requêtes
                # plus longues ; TurboDL gère lui-même la file d'attente
                "connections_limit": 1000, "connection_speed": 200,
                "torrent_connect_boost": 100, "max_peerlist_size": 8000,
                "max_out_request_queue": 2000, "max_allowed_in_request_queue": 4000,
                "request_queue_time": 5, "whole_pieces_threshold": 20,
                "active_downloads": -1, "active_seeds": -1, "active_limit": -1,
                "active_checking": 2, "aio_threads": 16, "hashing_threads": 4,
                "send_buffer_watermark": 3 * 1024 * 1024,
            })
        return _LT["session"]


class TorrentJob:
    """Téléchargement BitTorrent ; même interface que Downloader
    (run / stop / snapshot) pour que l'affichage soit commun."""

    PHASES = {"queued_for_checking": "vérification…", "checking_files": "vérification des fichiers…",
              "checking_resume_data": "vérification…", "allocating": "préparation…",
              "downloading_metadata": "recherche des pairs et des métadonnées…"}

    def __init__(self, source, out_dir=".", headers=None):
        self.source = source.strip()
        self.out_dir = out_dir
        self.headers = dict(headers or {})
        self.filename = magnet_name(self.source) if self.source.lower().startswith("magnet:") \
            else os.path.basename(urllib.parse.urlparse(self.source).path or self.source)
        self.size = None
        self.downloaded = 0
        self.speed = 0.0
        self.upload = 0.0
        self.peers = self.seeds = 0
        self.pieces = []
        self.phase = ""
        self.status = "init"
        self.error = None
        self.final_path = None
        self.extra_peers = []          # (ip, port) à contacter directement (tests)
        self.saved_torrent = None      # copie gardée par TurboDL pour la reprise
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()

    def stop(self):
        self.stop_evt.set()

    def _params(self):
        import libtorrent as lt
        src = self.source
        if src.lower().startswith("magnet:"):
            return lt.parse_magnet_uri(src)
        if re.match(r"https?://", src, re.I):
            with open_url(src, self.headers) as r:
                data = r.read(MAX_TORRENT_FILE + 1)
        else:
            with open(src, "rb") as fh:
                data = fh.read(MAX_TORRENT_FILE + 1)
        if len(data) > MAX_TORRENT_FILE:
            raise ValueError("fichier .torrent trop gros")
        try:
            atp = lt.load_torrent_buffer(data)
        except Exception as e:  # noqa: BLE001
            raise ValueError("ce fichier n'est pas un .torrent valide") from e
        copy = os.path.join(app_dir("torrents"), hashlib.sha1(data).hexdigest() + ".torrent")
        if not os.path.isfile(copy):
            with open(copy, "wb") as fh:
                fh.write(data)
        self.saved_torrent = copy
        return atp

    def run(self):
        self.status = "running"
        try:
            self._run()
        except ImportError:
            self.error = "moteur torrent (libtorrent) introuvable"
            self.status = "error"
        except Exception as e:  # noqa: BLE001 — remonté à l'interface
            self.error = friendly_error(e)
            self.status = "error"
        return self.status

    def _run(self):
        atp = self._params()
        out = os.path.abspath(self.out_dir)
        os.makedirs(out, exist_ok=True)
        atp.save_path = out
        ses = lt_session()
        if not self.extra_peers:                 # trackers publics : plus de pairs
            atp.trackers = list(atp.trackers) + [t for t in EXTRA_TRACKERS
                                                 if t not in atp.trackers]
        h = ses.add_torrent(atp)
        for peer in self.extra_peers:
            h.connect_peer(peer)
        space_checked = False
        full_since = None                # 100 % atteint mais libtorrent pas encore « fini »
        try:
            while not self.stop_evt.is_set():
                ses.pop_alerts()
                st = h.status()
                if st.errc.value():
                    raise IOError(st.errc.message())
                with self.lock:
                    if st.name:
                        self.filename = st.name
                    if st.has_metadata:
                        self.size = st.total_wanted
                    self.downloaded = st.total_wanted_done
                    self.speed, self.upload = st.download_rate, st.upload_rate
                    self.peers, self.seeds = st.num_peers, st.num_seeds
                    self.phase = self.PHASES.get(str(st.state).rsplit(".", 1)[-1], "")
                    self.pieces = _buckets(st.pieces, 240)
                if (not space_checked and st.has_metadata
                        and str(st.state).endswith("downloading")):   # après la vérification
                    check_space(out, st.total_wanted - st.total_wanted_done)
                    space_checked = True
                complete = (st.has_metadata and st.total_wanted > 0
                            and st.total_wanted_done >= st.total_wanted
                            and not str(st.state).rsplit(".", 1)[-1].startswith("checking"))
                full_since = (full_since or time.monotonic()) if complete else None
                if st.has_metadata and (st.is_finished or st.is_seeding or (
                        full_since and time.monotonic() - full_since > 10)):
                    self.final_path = os.path.join(out, st.name)
                    self.status = "done"     # pas de partage après la fin
                    return
                self.stop_evt.wait(0.5)
            self.status = "paused"           # relancer = revérifie puis reprend
        finally:
            ses.remove_torrent(h)            # les fichiers restent sur le disque

    def snapshot(self):
        with self.lock:
            left = (self.size - self.downloaded) if self.size else None
            return {"kind": "torrent", "status": self.status, "filename": self.filename,
                    "size": self.size, "downloaded": self.downloaded, "speed": self.speed,
                    "upload": self.upload,
                    "eta": left / self.speed if (left is not None and self.speed > 0) else None,
                    "active": self.peers, "peers": self.peers, "seeds": self.seeds,
                    "segments": [], "pieces": list(self.pieces), "phase": self.phase,
                    "ranges": True, "error": self.error, "path": self.final_path}


def _buckets(bits, n):
    bits = list(bits)
    if not bits:
        return []
    n = min(n, len(bits))
    out = []
    for i in range(n):
        a, b = i * len(bits) // n, (i + 1) * len(bits) // n
        out.append(sum(bits[a:b]) / max(b - a, 1))
    return out


def make_job(job, out_dir, connections):
    if is_torrent(job["url"], job.get("torrent")):
        return TorrentJob(job["url"], out_dir, job.get("headers"))
    return Downloader(job["url"], out_dir, connections, None, job.get("headers"))


# ------------------------------------------------------------- pont navigateur
def start_bridge(on_job):
    """Serveur local (127.0.0.1 seulement) qui reçoit les téléchargements de
    l'extension. Seule l'extension TurboDL est acceptée (en-tête Origin, qu'une
    page web ne peut pas falsifier). Renvoie le serveur, ou None si port pris."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    hosts = {f"127.0.0.1:{BRIDGE_PORT}", f"localhost:{BRIDGE_PORT}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.headers.get("Origin") != EXT_ORIGIN or self.headers.get("Host") not in hosts:
                return self._reply(403, {"ok": False, "error": "origine refusée"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > 256 * 1024:
                return self._reply(413, {"ok": False, "error": "requête trop grosse"})
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._reply(400, {"ok": False, "error": "JSON invalide"})
            if self.path == "/ping":
                return self._reply(200, {"ok": True, "app": "TurboDL", "version": VERSION})
            if self.path != "/add":
                return self._reply(404, {"ok": False, "error": "inconnu"})
            url = str(data.get("url") or "")
            torrent = bool(data.get("torrent"))
            if url.lower().startswith("magnet:") or (
                    os.path.isfile(url) and url.lower().endswith(".torrent")):
                on_job({"url": url, "headers": {}, "name": magnet_name(url)
                        if url.lower().startswith("magnet:") else os.path.basename(url),
                        "torrent": True, "page": str(data.get("referrer") or "")})
                return self._reply(200, {"ok": True})
            headers = {}
            for key, name in (("cookies", "Cookie"), ("referrer", "Referer"),
                              ("userAgent", "User-Agent")):
                v = str(data.get(key) or "")
                if v and "\r" not in v and "\n" not in v:
                    headers[name] = v
            try:
                if urllib.parse.urlparse(url).scheme not in ("http", "https"):
                    raise ValueError("lien non http(s)")
                _, size, ranges, name = probe(url, headers)   # vérifie avant d'accepter
            except Exception as e:  # noqa: BLE001 — le navigateur reprend la main
                return self._reply(200, {"ok": False, "error": str(e)[:300]})
            on_job({"url": url, "headers": headers, "name": name, "size": size,
                    "torrent": torrent or is_torrent(name)})
            self._reply(200, {"ok": True, "name": name, "ranges": ranges})

    class Server(ThreadingHTTPServer):
        allow_reuse_address = False   # sous Windows, sinon 2 instances partagent le port

    try:
        srv = Server(("127.0.0.1", BRIDGE_PORT), Handler)
    except OSError:
        return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def extension_dir():
    """Dossier de l'extension. Dans l'exe, on la copie à un endroit stable
    (le dossier temporaire de l'exe change à chaque lancement)."""
    src = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))),
                       "extension")
    if not getattr(sys, "frozen", False):
        return src
    dst = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                       "TurboDL", "extension")
    try:
        shutil.copytree(src, dst, dirs_exist_ok=True)
    except OSError:
        pass
    return dst


# ------------------------------------------------------------- ligne de commande
def cli(args):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    dl = Downloader(args.url, args.output, args.connections)
    t = threading.Thread(target=dl.run, daemon=True)
    t.start()
    try:
        while t.is_alive():
            t.join(0.3)
            s = dl.snapshot()
            if s["size"]:
                pct = 100 * s["downloaded"] / s["size"]
                bar = "█" * int(pct / 4) + "░" * (25 - int(pct / 4))
                line = (f"\r{bar} {pct:5.1f}%  {human(s['downloaded'])}/{human(s['size'])}"
                        f"  {human(s['speed'])}/s  {s['active']:>2} conn  ETA {human_time(s['eta'])}  ")
            else:
                line = f"\r{human(s['downloaded'])}  {human(s['speed'])}/s  "
            sys.stdout.write(line)
            sys.stdout.flush()
    except KeyboardInterrupt:
        dl.stop()
        t.join()
    s = dl.snapshot()
    print()
    if s["status"] == "done":
        print(f"✔ Terminé : {s['path']}")
    elif s["status"] == "paused":
        print("⏸ En pause — relancez la même commande pour reprendre.")
    else:
        print(f"✘ Erreur : {s['error']}")
        return 1
    return 0


# ---------------------------------------------------------- interface graphique
def register_handlers(exe, base=r"Software\Classes"):
    """Associe les fichiers .torrent et les liens magnet à TurboDL
    (réglages de l'utilisateur uniquement, HKEY_CURRENT_USER)."""
    import winreg
    cmd = f'"{exe}" "%1"'
    icon = f'"{exe}",0'
    keys = {
        "TurboDL.torrent": {"": "Fichier torrent"},
        r"TurboDL.torrent\DefaultIcon": {"": icon},
        r"TurboDL.torrent\shell\open\command": {"": cmd},
        ".torrent": {"": "TurboDL.torrent", "Content Type": "application/x-bittorrent"},
        r".torrent\OpenWithProgids": {"TurboDL.torrent": ""},
        "magnet": {"": "URL:Magnet Link", "URL Protocol": ""},
        r"magnet\DefaultIcon": {"": icon},
        r"magnet\shell\open\command": {"": cmd},
    }
    for path, values in keys.items():
        k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, base + "\\" + path, 0, winreg.KEY_WRITE)
        try:
            for name, val in values.items():
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
        finally:
            winreg.CloseKey(k)
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)  # rafraîchit l'Explorateur


def enable_file_drop(root, on_files):
    """Glisser-déposer de fichiers depuis l'Explorateur (Windows, sans dépendance)."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes
    user32, shell32 = ctypes.windll.user32, ctypes.windll.shell32
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                 wintypes.LPARAM)
    user32.GetParent.restype = wintypes.HWND
    user32.SetWindowLongPtrW.restype = ctypes.c_void_p
    user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    user32.CallWindowProcW.restype = LRESULT
    user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                       wintypes.WPARAM, wintypes.LPARAM]
    shell32.DragQueryFileW.restype = wintypes.UINT
    shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.LPWSTR,
                                       wintypes.UINT]
    shell32.DragFinish.argtypes = [ctypes.c_void_p]
    shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
    root.update_idletasks()
    hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
    state = {}

    def proc(h, msg, wp, lp):
        if msg == 0x0233:                                  # WM_DROPFILES
            files = []
            for i in range(shell32.DragQueryFileW(wp, 0xFFFFFFFF, None, 0)):
                n = shell32.DragQueryFileW(wp, i, None, 0)
                buf = ctypes.create_unicode_buffer(n + 1)
                shell32.DragQueryFileW(wp, i, buf, n + 1)
                files.append(buf.value)
            shell32.DragFinish(wp)
            on_files(files)
            return 0
        return user32.CallWindowProcW(state["old"], h, msg, wp, lp)

    state["proc"] = WNDPROC(proc)                          # garder une référence
    state["old"] = user32.SetWindowLongPtrW(hwnd, -4, ctypes.cast(state["proc"], ctypes.c_void_p))
    shell32.DragAcceptFiles(hwnd, True)
    state["hwnd"] = hwnd
    root._turbodl_drop = state
    return True


def short(text, n=80):
    text = str(text or "")
    return text if len(text) <= n else text[:n - 1] + "…"


# Palette PROVISOIRE : à remplacer par les jetons de kdl-design-system
# (dominante bleu KDL, un seul accent cyan, neutres ardoise / ivoire).
THEMES = {
    "sombre": {"bg": "#0b1018", "side": "#0e141e", "surf": "#131a26", "surf2": "#1a2332",
               "line": "#222d3f", "hover": "#1f2938", "track": "#0a0f17", "text": "#e7ecf3", "muted": "#8e9aad",
               "faint": "#5b6678", "primary": "#3d6ff2", "primary_hi": "#5784f7",
               "on_primary": "#ffffff", "accent": "#2dd4ef", "ok": "#3ecf8e",
               "err": "#f2706b", "violet": "#a78bfa", "amber": "#fbbf24",
               "pink": "#f472b6", "dark": True},
    "clair": {"bg": "#f5f3ee", "side": "#efece5", "surf": "#fffdf9", "surf2": "#f0ece4", "line": "#e1dbcf",
              "hover": "#ebe6dc", "track": "#e9e4da", "text": "#18212d", "muted": "#566070",
              "faint": "#8a93a1", "primary": "#2456e3", "primary_hi": "#3a69ee",
              "on_primary": "#ffffff", "accent": "#0b8fae", "ok": "#1d9461",
              "err": "#cc4740", "violet": "#7c3aed", "amber": "#c27c0e",
              "pink": "#db2777", "dark": False},
}


def system_theme():
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        return "clair" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 1 else "sombre"
    except OSError:
        return "sombre"


def blend(c1, c2, t):
    """Mélange deux couleurs hex : t=1 -> c1, t=0 -> c2."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * t + y * (1 - t)):02x}" for x, y in zip(a, b))


def resource(name):
    return os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), name)


def gui(initial=None):
    import collections
    import datetime
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import filedialog, messagebox, ttk

    settings = load_settings()
    theme = os.environ.get("TURBODL_THEME") or settings.get("theme") or system_theme()
    P = THEMES.get(theme, THEMES["sombre"])
    BG, SIDE, SURF, SURF2, LINE, HOVER = P["bg"], P["side"], P["surf"], P["surf2"], P["line"], P["hover"]
    FG, MUTED, FAINT = P["text"], P["muted"], P["faint"]
    PRIMARY, ACC, OK, ERR = P["primary"], P["accent"], P["ok"], P["err"]
    VIOLET, AMBER, PINK = P["violet"], P["amber"], P["pink"]
    NAV_COL = {"all": PRIMARY, "running": ACC, "waiting": AMBER,
               "done": OK, "torrent": VIOLET, "http": ACC, "history": PINK}

    def grad(t):
        """Dégradé signature : cyan à gauche -> violet à droite."""
        return blend(VIOLET, ACC, max(0.0, min(1.0, t)))
    CARD, ACC_HI = SURF, P["primary_hi"]          # noms utilisés par la fenêtre Historique

    root = tk.Tk()
    root.withdraw()
    root.title("TurboDL")
    root.configure(bg=BG)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{min(1240, sw - 60)}x{max(640, min(900, sh - 90))}")
    root.minsize(960, 620)
    try:
        root.iconbitmap(resource("turbodl.ico"))
    except tk.TclError:
        pass
    fams = set(tkfont.families(root))
    FONT = "Segoe UI Variable Text" if "Segoe UI Variable Text" in fams else "Segoe UI"
    DISPLAY = "Segoe UI Variable Display" if "Segoe UI Variable Display" in fams else "Segoe UI"
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("D.Vertical.TScrollbar", background=SURF2, troughcolor=BG, bordercolor=BG,
                    arrowcolor=MUTED, lightcolor=SURF2, darkcolor=SURF2, gripcount=0)
    style.map("D.Vertical.TScrollbar", background=[("active", LINE)])

    today = datetime.date.today().isoformat()
    saved_today = settings.get("today") or {}
    st = {"inbox": queue.Queue(), "awake": False, "clip_seen": "", "filter": "all",
          "sort": "added", "shown": [], "last_sort": 0.0, "last_today_save": 0.0,
          "spark": collections.deque([0.0] * 2, maxlen=120), "last_sample": 0.0,
          "today": saved_today if saved_today.get("date") == today else {"date": today, "bytes": 0}}
    items = {}                      # hid -> Row, dans l'ordre d'ajout
    hist = History()

    # ----------------------------------------------------------- briques
    def label(parent, text, size=10, color=MUTED, weight="normal", family=None, **kw):
        kw.setdefault("bg", parent["bg"])
        return tk.Label(parent, text=text, fg=color, font=(family or FONT, size, weight), **kw)

    def edit_menu(w):
        """Menu clic droit Couper / Copier / Coller / Tout sélectionner."""
        m = tk.Menu(w, tearoff=0, bg=SURF2, fg=FG, activebackground=PRIMARY,
                    activeforeground=P["on_primary"], font=(FONT, 10), bd=0)
        has_sel = lambda: w.selection_present()

        def paste():
            if has_sel():
                w.delete("sel.first", "sel.last")   # remplace la sélection
            try:
                w.insert("insert", root.clipboard_get().strip())
            except tk.TclError:
                pass

        def select_all():
            w.select_range(0, "end")
            w.icursor("end")

        m.add_command(label="Couper", command=lambda: w.event_generate("<<Cut>>"))
        m.add_command(label="Copier", command=lambda: w.event_generate("<<Copy>>"))
        m.add_command(label="Coller", command=paste)
        m.add_separator()
        m.add_command(label="Tout sélectionner", command=select_all)

        def popup(e):
            w.focus_set()
            sel = "normal" if has_sel() else "disabled"
            m.entryconfig("Couper", state=sel)
            m.entryconfig("Copier", state=sel)
            m.tk_popup(e.x_root, e.y_root)
        w.bind("<Button-3>", popup)

    def entry(parent, var, size=11):
        e = tk.Entry(parent, textvariable=var, bg=SURF, fg=FG, insertbackground=FG,
                     relief="flat", font=(FONT, size), highlightthickness=1,
                     highlightbackground=LINE, highlightcolor=ACC,
                     selectbackground=PRIMARY, selectforeground=P["on_primary"])
        edit_menu(e)
        return e

    def clickable(w, cmd):
        w.configure(takefocus=1, cursor="hand2")
        w.bind("<Button-1>", lambda e: cmd(), add="+")
        w.bind("<Return>", lambda e: cmd(), add="+")
        w.bind("<space>", lambda e: cmd(), add="+")

    def button(parent, text, cmd, primary=False, small=False):
        """Bouton texte simple (fenêtre Historique)."""
        padx, pady, size = (10, 4, 9) if small else (16, 8, 10)
        bg = PRIMARY if primary else parent["bg"]
        b = tk.Label(parent, text=text, bg=bg, fg=P["on_primary"] if primary else FG,
                     font=(FONT, size, "bold" if primary else "normal"), padx=padx, pady=pady,
                     highlightthickness=1, highlightbackground=PRIMARY if primary else LINE,
                     highlightcolor=ACC)
        hover = ACC_HI if primary else HOVER
        b.bind("<Enter>", lambda e: b.config(bg=hover))
        b.bind("<Leave>", lambda e: b.config(bg=bg))
        clickable(b, cmd)
        return b

    def tooltip(w, text):
        tip = {}

        def show():
            tip.pop("job", None)
            t = tk.Toplevel(root)
            t.overrideredirect(True)
            t.configure(bg=LINE)
            tk.Label(t, text=text, bg=SURF2, fg=FG, font=(FONT, 9), padx=8, pady=4).pack(
                padx=1, pady=1)
            t.geometry(f"+{w.winfo_rootx()}+{w.winfo_rooty() + w.winfo_height() + 6}")
            tip["w"] = t

        def hide(e=None):
            if tip.get("job"):
                root.after_cancel(tip.pop("job"))
            if tip.get("w"):
                tip.pop("w").destroy()
        w.bind("<Enter>", lambda e: tip.__setitem__("job", root.after(450, show)), add="+")
        w.bind("<Leave>", hide, add="+")
        w.bind("<Button-1>", hide, add="+")
        w.bind("<Destroy>", hide, add="+")

    def rounded(c, x0, y0, x1, y1, r, **kw):
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
               x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        return c.create_polygon(pts, smooth=True, **kw)

    def glow(c, x, y, color, base, r=3):
        """Effet signature : un halo lumineux sur ce qui avance."""
        for rad, t in ((r + 5, 0.12), (r + 3, 0.3), (r, 1.0)):
            c.create_oval(x - rad, y - rad, x + rad, y + rad, fill=blend(color, base, t), width=0)

    def draw_icon(c, kind, s, col, ox=0, oy=0):
        """Icônes vectorielles dessinées sur une grille 24×24 (aucun emoji)."""
        k = s / 24

        def X(*v):
            return [(ox if i % 2 == 0 else oy) + n * k for i, n in enumerate(v)]
        lw = max(1.4, 2 * k)
        ln = dict(fill=col, width=lw, capstyle="round", joinstyle="round")
        ol = dict(outline=col, width=lw)
        if kind == "down":
            c.create_line(X(12, 4, 12, 16), **ln)
            c.create_line(X(7, 11, 12, 16, 17, 11), **ln)
            c.create_line(X(5, 20, 19, 20), **ln)
        elif kind == "bolt":
            c.create_polygon(X(13.5, 2, 5, 13.5, 11, 13.5, 9.5, 22, 19, 9.5, 12.8, 9.5),
                             fill=col, width=0)
        elif kind == "list":
            for y in (6, 12, 18):
                c.create_line(X(8, y, 20, y), **ln)
                c.create_oval(X(3, y - 1, 5, y + 1), fill=col, width=0)
        elif kind == "play":
            c.create_polygon(X(7, 4.5, 7, 19.5, 19.5, 12), fill=col, width=0)
        elif kind == "pause":
            c.create_rectangle(X(6, 5, 10, 19), fill=col, width=0)
            c.create_rectangle(X(14, 5, 18, 19), fill=col, width=0)
        elif kind == "clock":
            c.create_oval(X(3.5, 3.5, 20.5, 20.5), **ol)
            c.create_line(X(12, 7.5, 12, 12, 15, 14), **ln)
        elif kind == "check":
            c.create_line(X(5, 12.5, 9.5, 17, 19, 7.5), **dict(ln, width=lw * 1.15))
        elif kind == "magnet":
            c.create_line(X(6, 3, 6, 11), **ln)
            c.create_line(X(18, 3, 18, 11), **ln)
            c.create_arc(X(6, 5, 18, 17), start=180, extent=180, style="arc", **ol)
            c.create_line(X(4.5, 7, 7.5, 7), **ln)
            c.create_line(X(16.5, 7, 19.5, 7), **ln)
        elif kind == "globe":
            c.create_oval(X(3.5, 3.5, 20.5, 20.5), **ol)
            c.create_oval(X(8.5, 3.5, 15.5, 20.5), **ol)
            c.create_line(X(3.5, 12, 20.5, 12), **ln)
        elif kind == "history":
            c.create_arc(X(4, 4, 20, 20), start=160, extent=-320, style="arc", **ol)
            c.create_line(X(3.5, 5, 4.5, 9.5, 9, 8.5), **ln)
            c.create_line(X(12, 8, 12, 12, 15, 14), **ln)
        elif kind == "retry":
            c.create_arc(X(4, 4, 20, 20), start=40, extent=300, style="arc", **ol)
            c.create_line(X(20, 4, 20, 8.5, 15.5, 8.5), **ln)
        elif kind == "folder":
            c.create_polygon(X(3.5, 6, 9.5, 6, 11.5, 8, 20.5, 8, 20.5, 19, 3.5, 19), fill="",
                             joinstyle="round", **ol)
        elif kind == "x":
            c.create_line(X(6, 6, 18, 18), **ln)
            c.create_line(X(18, 6, 6, 18), **ln)
        elif kind == "file":
            c.create_polygon(X(7, 3.5, 14, 3.5, 18.5, 8, 18.5, 20.5, 7, 20.5), fill="",
                             joinstyle="round", **ol)
            c.create_line(X(14, 3.5, 14, 8, 18.5, 8), **ln)
        elif kind == "link":
            c.create_oval(X(2.5, 8.5, 13.5, 15.5), **ol)
            c.create_oval(X(10.5, 8.5, 21.5, 15.5), **ol)
        elif kind == "plug":
            c.create_line(X(9, 3, 9, 8), **ln)
            c.create_line(X(15, 3, 15, 8), **ln)
            c.create_line(X(6.5, 8, 17.5, 8), **ln)
            c.create_arc(X(6.5, 5.5, 17.5, 16.5), start=180, extent=180, style="arc", **ol)
            c.create_line(X(12, 16.5, 12, 21), **ln)
        elif kind == "paste":
            c.create_rectangle(X(6, 5, 18, 21), **ol)
            c.create_rectangle(X(9, 3, 15, 7), fill=col, width=0)
        elif kind == "open":
            c.create_line(X(10, 5, 5, 5, 5, 19, 19, 19, 19, 14), **ln)
            c.create_line(X(13, 11, 20, 4), **ln)
            c.create_line(X(14, 4, 20, 4, 20, 10), **ln)

    def icon_button(parent, kind, cmd, tip, size=32):
        c = tk.Canvas(parent, width=size, height=size, bg=parent["bg"], bd=0,
                      highlightthickness=0)
        c.kind, c.hover, c.tip = kind, False, tip

        def paint():
            c.delete("all")
            if c.hover:
                rounded(c, 1, 1, size - 1, size - 1, 8, fill=HOVER, outline="")
            draw_icon(c, c.kind, 16, FG if c.hover else MUTED, (size - 16) / 2, (size - 16) / 2)

        def set_hover(v):
            c.hover = v
            paint()
        c.paint = paint
        c.bind("<Enter>", lambda e: set_hover(True))
        c.bind("<Leave>", lambda e: set_hover(False))
        c.bind("<FocusIn>", lambda e: set_hover(True))      # focus clavier visible
        c.bind("<FocusOut>", lambda e: set_hover(False))
        clickable(c, cmd)
        tooltip(c, tip)
        paint()
        return c

    def pill(parent, text, icon, cmd, primary=False, h=48):
        f = tkfont.Font(family=FONT, size=10, weight="bold" if primary else "normal")
        w = f.measure(text) + (24 if icon else 0) + 36
        c = tk.Canvas(parent, width=w, height=h, bg=parent["bg"], highlightthickness=0, bd=0)
        c.hover = False

        def paint():
            c.delete("all")
            if primary:
                fill, edge, fg = (P["primary_hi"] if c.hover else PRIMARY), PRIMARY, P["on_primary"]
            else:
                fill, edge, fg = (HOVER if c.hover else SURF), (ACC if c.focus else LINE), FG
            rounded(c, 0.5, 0.5, w - 1, h - 1, 10, fill=fill, outline=edge)
            x = 18
            if icon:
                draw_icon(c, icon, 16, fg, x, (h - 16) / 2)
                x += 24
            c.create_text(x, h / 2, text=text, anchor="w", fill=fg, font=f)
        c.focus = False

        def hov(v):
            c.hover = v
            paint()

        def foc(v):
            c.focus = v
            c.hover = v if primary else c.hover
            paint()
        c.bind("<Enter>", lambda e: hov(True))
        c.bind("<Leave>", lambda e: hov(False))
        c.bind("<FocusIn>", lambda e: foc(True))
        c.bind("<FocusOut>", lambda e: foc(False))
        clickable(c, cmd)
        paint()
        return c

    def rounded_box(parent, fill, outline, r=12, inset=4):
        """Cadre aux coins arrondis : un Canvas qui dessine le fond, un Frame dedans."""
        cv = tk.Canvas(parent, bg=parent["bg"], highlightthickness=0, bd=0, height=2 * inset + 2)
        inner = tk.Frame(cv, bg=fill)
        win = cv.create_window(inset, inset, window=inner, anchor="nw")
        cv.edge = outline

        def redraw(*_):
            w = cv.winfo_width()
            h = inner.winfo_reqheight() + 2 * inset
            if int(cv.cget("height")) != h:
                cv.config(height=h)
            cv.itemconfigure(win, width=max(1, w - 2 * inset))
            cv.delete("bg")
            rounded(cv, 0.5, 0.5, w - 1.5, h - 1.5, r, fill=fill, outline=cv.edge, tags="bg")
            cv.tag_lower("bg")

        def set_edge(col):
            cv.edge = col
            cv.itemconfigure("bg", outline=col)
        cv.set_edge = set_edge
        cv.bind("<Configure>", redraw)
        inner.bind("<Configure>", redraw)
        return cv, inner

    def notify(text, color=None):
        status_lbl.config(text=text, fg=FG if color in (None, FG) else color)
        status_dot.delete("all")
        status_dot.create_oval(1, 1, 8, 8, fill=color or MUTED, width=0)

    # ----------------------------------------------------------- variables
    url_var = tk.StringVar()
    saved_dir = settings.get("out_dir")
    dir_var = tk.StringVar(value=saved_dir if saved_dir and os.path.isdir(saved_dir)
                           else default_dir())
    conn_var = tk.StringVar(value=str(settings.get("connections", 16)))
    par_var = tk.StringVar(value=str(settings.get("parallel", 3)))
    bridge = start_bridge(st["inbox"].put)

    # ----------------------------------------------------------- barre latérale
    side = tk.Frame(root, bg=SIDE, width=224)
    side.pack(side="left", fill="y")
    side.pack_propagate(False)
    tk.Frame(root, bg=LINE, width=1).pack(side="left", fill="y")
    sbx = tk.Frame(side, bg=SIDE, padx=12, pady=24)
    sbx.pack(fill="both", expand=True)
    brand = tk.Frame(sbx, bg=SIDE)
    brand.pack(fill="x", padx=8, pady=(0, 24))
    logo = tk.Canvas(brand, width=32, height=32, bg=SIDE, highlightthickness=0)
    logo.pack(side="left")
    rounded(logo, 0.5, 0.5, 31.5, 31.5, 9, fill=PRIMARY, outline="")
    draw_icon(logo, "bolt", 18, P["on_primary"], 7, 7)
    label(brand, "TurboDL", 15, FG, "bold", DISPLAY).pack(side="left", padx=(10, 0))

    navs = {}

    def nav_item(parent, key, text, icon):
        c = tk.Canvas(parent, height=36, bg=SIDE, highlightthickness=0, bd=0)
        c.pack(fill="x", pady=1)
        c.hover, c.count = False, ""

        def paint():
            c.delete("all")
            w = max(c.winfo_width(), 40)
            active = st["filter"] == key
            tint = NAV_COL.get(key, ACC)
            if active or c.hover:
                rounded(c, 1, 1, w - 2, 35, 8, outline="",
                        fill=blend(tint, SIDE, 0.16) if active else HOVER)
            if active:
                c.create_rectangle(1, 10, 4, 26, fill=tint, width=0)
            col = FG if (active or c.hover) else MUTED
            draw_icon(c, icon, 16, tint if (active or c.hover) else blend(tint, MUTED, 0.7), 12, 10)
            c.create_text(38, 18, text=text, anchor="w", fill=col,
                          font=(FONT, 10, "bold" if active else "normal"))
            if c.count:
                c.create_text(w - 10, 18, text=c.count, anchor="e", font=(FONT, 9, "bold"),
                              fill=tint if active else FAINT)

        def hov(v):
            c.hover = v
            paint()
        c.paint = paint
        c.bind("<Configure>", lambda e: paint())
        c.bind("<Enter>", lambda e: hov(True))
        c.bind("<Leave>", lambda e: hov(False))
        c.bind("<FocusIn>", lambda e: hov(True))
        c.bind("<FocusOut>", lambda e: hov(False))
        clickable(c, lambda: show_history() if key == "history" else select_filter(key))
        navs[key] = c

    for spec in (("all", "Tout", "list"), ("running", "En cours", "down"),
                 ("waiting", "En attente", "clock"), ("done", "Terminés", "check"), None,
                 ("torrent", "Torrents", "magnet"), ("http", "Liens directs", "globe"),
                 ("history", "Historique", "history")):
        if spec is None:
            tk.Frame(sbx, bg=LINE, height=1).pack(fill="x", padx=8, pady=12)
        else:
            nav_item(sbx, *spec)

    sfoot = tk.Frame(sbx, bg=SIDE)
    sfoot.pack(side="bottom", fill="x")

    def side_line(parent, text, icon=None, dot=None, color=MUTED, cmd=None):
        f = tk.Frame(parent, bg=SIDE)
        c = tk.Canvas(f, width=16, height=16, bg=SIDE, highlightthickness=0)
        c.pack(side="left", padx=(10, 0))
        if dot:
            c.create_oval(4, 4, 12, 12, fill=dot, width=0)
        elif icon:
            draw_icon(c, icon, 14, color, 1, 1)
        lb = label(f, text, 9, color, anchor="w", justify="left", wraplength=150)
        lb.pack(side="left", padx=(8, 0), fill="x")
        if cmd:
            clickable(lb, cmd)
            lb.bind("<Enter>", lambda e: lb.config(fg=FG, font=(FONT, 9, "underline")))
            lb.bind("<Leave>", lambda e: lb.config(fg=color, font=(FONT, 9)))
        return f

    awake_row = side_line(sfoot, "Veille bloquée pendant les téléchargements", icon="plug")
    side_line(sfoot, "Extension connectée" if bridge else "Port de l'extension occupé",
              dot=OK if bridge else ERR).pack(fill="x", pady=(4, 0))
    tk.Frame(sfoot, bg=LINE, height=1).pack(fill="x", padx=8, pady=10)
    side_line(sfoot, "Installer l'extension", icon="plug", color=FAINT,
              cmd=lambda: install_extension()).pack(fill="x", pady=2)
    side_line(sfoot, "Associer les .torrent", icon="magnet", color=FAINT,
              cmd=lambda: associate()).pack(fill="x", pady=2)

    # ----------------------------------------------------------- zone principale
    main = tk.Frame(root, bg=BG, padx=32, pady=24)
    main.pack(side="left", fill="both", expand=True)

    cmd = tk.Frame(main, bg=BG)
    cmd.pack(fill="x")
    fbox, field = rounded_box(cmd, SURF, LINE, 10, 4)
    fbox.pack(side="left", fill="x", expand=True)
    ic = tk.Canvas(field, width=18, height=18, bg=SURF, highlightthickness=0)
    ic.pack(side="left", padx=(10, 8))
    draw_icon(ic, "link", 18, FAINT)
    url_entry = tk.Entry(field, textvariable=url_var, bg=SURF, fg=FG, insertbackground=FG,
                         relief="flat", font=(FONT, 11), highlightthickness=0,
                         selectbackground=PRIMARY, selectforeground=P["on_primary"])
    edit_menu(url_entry)
    url_entry.pack(side="left", fill="x", expand=True, ipady=9)
    url_entry.bind("<FocusIn>", lambda e: fbox.set_edge(ACC))
    url_entry.bind("<FocusOut>", lambda e: fbox.set_edge(LINE))

    def paste_url():
        try:
            url_var.set(root.clipboard_get().strip())   # remplace tout le champ
            url_entry.icursor("end")
            url_entry.focus_set()
        except tk.TclError:
            pass

    icon_button(field, "x", lambda: (url_var.set(""), url_entry.focus_set()),
                "Vider le champ").pack(side="right", padx=(0, 4))
    icon_button(field, "paste", paste_url, "Coller le lien").pack(side="right")
    hint = label(field, "", 10, FAINT, bg=SURF)

    def placeholder(*_):
        if url_var.get():
            hint.place_forget()
        else:
            hint.config(text="Collez un lien, un magnet… ou déposez des fichiers .torrent n'importe où")
            hint.place(in_=url_entry, x=2, rely=0.5, anchor="w")
    url_var.trace_add("write", placeholder)
    hint.bind("<Button-1>", lambda e: url_entry.focus_set())
    pill(cmd, ".torrent", "file", lambda: open_torrents()).pack(side="right", padx=(8, 0))
    pill(cmd, "Ajouter", "down", lambda: add_from_text(url_var.get()), primary=True).pack(
        side="right", padx=(8, 0))

    # réglages repliés en étiquettes
    chips_row = tk.Frame(main, bg=BG)
    chips_row.pack(fill="x", pady=(10, 0))
    chips = {}

    def chip(key, parts, icon=None, action=None, tip=None):
        c = tk.Canvas(chips_row, height=26, bg=BG, highlightthickness=0, bd=0)
        c.pack(side="left", padx=(0, 8))
        f_n, f_b = tkfont.Font(family=FONT, size=9), tkfont.Font(family=FONT, size=9, weight="bold")
        c.hover = False

        def paint():
            c.delete("all")
            segs = parts()
            w = 24 + (18 if icon else 0) + sum((f_b if b else f_n).measure(t) for t, b in segs)
            c.config(width=w)
            rounded(c, 0.5, 0.5, w - 1, 25, 12, fill=HOVER if c.hover else SURF, outline=LINE)
            x = 12
            if icon:
                draw_icon(c, icon, 13, MUTED, x, 6.5)
                x += 18
            for t, b in segs:
                c.create_text(x, 13, text=t, anchor="w", fill=FG if b else MUTED,
                              font=f_b if b else f_n)
                x += (f_b if b else f_n).measure(t)

        def hov(v):
            c.hover = v
            paint()
        c.paint = paint
        c.bind("<Enter>", lambda e: hov(True))
        c.bind("<Leave>", lambda e: hov(False))
        if action:
            clickable(c, lambda: action(c))
        if tip:
            tooltip(c, tip)
        chips[key] = c
        paint()

    def browse(_c=None):
        d = filedialog.askdirectory(initialdir=dir_var.get())
        if d:
            dir_var.set(os.path.normpath(d))
            save_settings(out_dir=dir_var.get())
            chips["dir"].paint()

    def choose(var, values, key):
        def run(c):
            m = tk.Menu(root, tearoff=0, bg=SURF2, fg=FG, activebackground=PRIMARY,
                        activeforeground=P["on_primary"], font=(FONT, 10), bd=0)
            for v in values:
                m.add_radiobutton(label=str(v), value=str(v), variable=var,
                                  command=lambda v=v: (save_settings(**{key: v}),
                                                       chips[key].paint(), schedule()))
            m.tk_popup(c.winfo_rootx(), c.winfo_rooty() + c.winfo_height() + 4)
        return run

    chip("dir", lambda: [("Enregistrer dans  ", False), (dir_var.get(), True)], "folder", browse,
         "Changer le dossier de destination")
    chip("connections", lambda: [(conn_var.get(), True), ("  connexions par fichier", False)],
         action=choose(conn_var, (1, 2, 4, 8, 16, 24, 32), "connections"),
         tip="Connexions par fichier (liens directs)")
    chip("parallel", lambda: [(par_var.get(), True), ("  en même temps", False)],
         action=choose(par_var, (1, 2, 3, 4, 5, 6, 8, 10), "parallel"),
         tip="Nombre de téléchargements simultanés")

    # ----------------------------------------------------------- cockpit (point focal)
    HERO_H = 176
    hero = tk.Canvas(main, height=HERO_H, bg=BG, highlightthickness=0, bd=0)
    hero.pack(fill="x", pady=(24, 0))
    big_f = tkfont.Font(family=DISPLAY, size=38, weight="bold")
    unit_f = tkfont.Font(family=DISPLAY, size=17, weight="bold")

    def draw_hero(speed=0.0, running=0, waiting=0, eta=None):
        c = hero
        c.delete("all")
        w, h = max(c.winfo_width(), 400), HERO_H
        rounded(c, 0.5, 0.5, w - 1.5, h - 1.5, 14, fill=SURF, outline=LINE)
        # courbe du débit, à droite du chiffre
        gx0, gx1, gy1 = 300, w - 16, h - 2
        vals = list(st["spark"])
        top = max(max(vals), 1.0)
        n = st["spark"].maxlen
        step = (gx1 - gx0) / (n - 1)
        pts = [(gx1 - (len(vals) - 1 - i) * step, gy1 - 6 - (v / top) * (h - 70))
               for i, v in enumerate(vals)]
        col = ACC if running else FAINT
        if len(pts) >= 2:
            area = [(pts[0][0], gy1)] + pts + [(pts[-1][0], gy1)]
            c.create_polygon([v for p in area for v in p], fill=blend(col, SURF, 0.14), width=0)
            if running:
                for i in range(len(pts) - 1):
                    c.create_line(*pts[i], *pts[i + 1], width=2.5, capstyle="round",
                                  fill=grad(i / max(1, len(pts) - 2)))
            else:
                c.create_line([v for p in pts for v in p], fill=col, width=2, smooth=True,
                              capstyle="round")
            if running:
                glow(c, pts[-1][0], pts[-1][1], ACC, SURF)
        # chiffre principal
        c.create_text(28, 34, text="DÉBIT TOTAL", anchor="w", fill=FAINT, font=(FONT, 8, "bold"))
        num, unit = (human(speed).rsplit(" ", 1) + [""])[:2]
        c.create_text(24, 80, text=num, anchor="w", fill=ACC if running else FAINT, font=big_f)
        c.create_text(28 + big_f.measure(num), 88, text=f"{unit}/s", anchor="w",
                      fill=ACC if running else FAINT, font=unit_f)
        sub = (f"{running} en cours" + (f"  ·  fin estimée dans {human_time(eta)}" if eta else "")
               if running else ("En attente d'une place" if waiting else "Rien en cours"))
        c.create_text(28, 128, text=sub, anchor="w", fill=MUTED, font=(FONT, 10))
        # pastilles de chiffres
        x = w - 24
        for value, lbl, tint in reversed(((str(running), "en cours", ACC),
                                          (str(waiting), "en attente", AMBER),
                                          (human(st["today"]["bytes"]), "reçus aujourd'hui", VIOLET))):
            bw = max(104, tkfont.Font(family=DISPLAY, size=14, weight="bold").measure(value) + 28)
            rounded(c, x - bw, 22, x, 76, 10, fill=blend(tint, SURF, 0.12),
                    outline=blend(tint, SURF, 0.35))
            c.create_text(x - bw + 14, 42, text=value, anchor="w", fill=tint,
                          font=(DISPLAY, 14, "bold"))
            c.create_text(x - bw + 14, 63, text=lbl, anchor="w", fill=FAINT, font=(FONT, 8))
            x -= bw + 8

    hero.bind("<Configure>", lambda e: draw_hero(*st.get("hero_args", ())))

    # ----------------------------------------------------------- en-tête de liste
    lhead = tk.Frame(main, bg=BG)
    lhead.pack(fill="x", pady=(24, 12))
    label(lhead, "Téléchargements", 15, FG, "bold", DISPLAY).pack(side="left")
    list_count = label(lhead, "", 11, FAINT)
    list_count.pack(side="left", padx=(10, 0), pady=(3, 0))
    segs = {}

    def seg(key, text):
        f = tkfont.Font(family=FONT, size=9)
        w = f.measure(text) + 20
        c = tk.Canvas(lhead, width=w, height=24, bg=BG, highlightthickness=0, bd=0)
        c.pack(side="left" if key == "clear" else "right", padx=(4, 0) if key != "clear" else (16, 0))
        c.hover = False

        def paint():
            c.delete("all")
            on = st["sort"] == key
            if on or c.hover:
                rounded(c, 0.5, 0.5, w - 1, 23, 6, fill=SURF2 if on else HOVER, outline="")
            c.create_text(w / 2, 12, text=text, fill=FG if (on or c.hover) else MUTED, font=f)

        def hov(v):
            c.hover = v
            paint()
        c.paint = paint
        c.bind("<Enter>", lambda e: hov(True))
        c.bind("<Leave>", lambda e: hov(False))
        clickable(c, (lambda: clear_done()) if key == "clear" else (lambda: select_sort(key)))
        segs[key] = c
        paint()

    for key, text in (("left", "Restant"), ("speed", "Vitesse"), ("added", "Ordre d'ajout")):
        seg(key, text)
    seg("clear", "Effacer les terminés")

    # ----------------------------------------------------------- liste défilante
    foot = tk.Frame(main, bg=BG)
    foot.pack(side="bottom", fill="x", pady=(12, 0))
    status_dot = tk.Canvas(foot, width=9, height=9, bg=BG, highlightthickness=0)
    status_dot.pack(side="left")
    status_lbl = label(foot, "", 9, FG, anchor="w", justify="left", wraplength=820)
    status_lbl.pack(side="left", fill="x", expand=True, padx=(8, 0))

    box = tk.Frame(main, bg=BG)
    box.pack(fill="both", expand=True)
    lcanvas = tk.Canvas(box, bg=BG, highlightthickness=0)
    sbar = ttk.Scrollbar(box, orient="vertical", command=lcanvas.yview,
                         style="D.Vertical.TScrollbar")
    inner = tk.Frame(lcanvas, bg=BG)
    inner_id = lcanvas.create_window(0, 0, window=inner, anchor="nw")
    inner.bind("<Configure>", lambda e: lcanvas.configure(scrollregion=lcanvas.bbox("all")))
    lcanvas.bind("<Configure>", lambda e: lcanvas.itemconfigure(inner_id, width=e.width))
    lcanvas.configure(yscrollcommand=sbar.set)
    lcanvas.pack(side="left", fill="both", expand=True)
    sbar.pack(side="right", fill="y", padx=(8, 0))
    wheel = lambda e: lcanvas.yview_scroll(int(-e.delta / 120), "units")
    box.bind("<Enter>", lambda e: root.bind_all("<MouseWheel>", wheel))
    box.bind("<Leave>", lambda e: root.unbind_all("<MouseWheel>"))

    # état vide : illustration, phrase utile, actions
    empty_box, empty = rounded_box(inner, SURF, LINE, 14, 4)
    epad = tk.Frame(empty, bg=SURF, pady=36)
    epad.pack(fill="x")
    ill = tk.Canvas(epad, width=88, height=88, bg=SURF, highlightthickness=0)
    ill.pack()
    ill.create_oval(8, 8, 80, 80, outline=blend(ACC, SURF, 0.35), width=2, dash=(4, 6))
    ill.create_oval(20, 20, 68, 68, fill=blend(PRIMARY, SURF, 0.16), width=0)
    draw_icon(ill, "down", 28, ACC, 30, 30)
    empty_title = label(epad, "", 13, FG, "bold", DISPLAY)
    empty_title.pack(pady=(16, 4))
    empty_text = label(epad, "", 10, MUTED, justify="center")
    empty_text.pack()
    eb = tk.Frame(epad, bg=SURF)
    eb.pack(pady=(20, 0))
    pill(eb, "Ouvrir des .torrent", "file", lambda: open_torrents(), primary=True, h=42).pack(
        side="left")
    pill(eb, "Coller un lien", "paste", lambda: (paste_url(), add_from_text(url_var.get())),
         h=42).pack(side="left", padx=(8, 0))

    # ----------------------------------------------------------- une carte
    STATE_TXT = {"waiting": ("En attente", MUTED), "running": ("En cours", ACC),
                 "paused": ("En pause", AMBER), "done": ("Terminé", OK), "error": ("Erreur", ERR)}

    class Row:
        def __init__(self, job, state="waiting", size=None, downloaded=0):
            self.job = job
            self.dl = None
            self.thread = None
            self.state = state
            self.stopping = False
            self.last_save = 0.0
            self.counted = None              # octets déjà comptés dans « reçus aujourd'hui »
            self.done_at = None
            self.torrent = is_torrent(job["url"], job.get("torrent"))
            self.snap = {"size": size, "downloaded": downloaded or 0, "speed": 0,
                         "segments": [], "pieces": [], "status": state, "error": None,
                         "path": None, "filename": job.get("name")}
            self.box, card = rounded_box(inner, SURF, LINE, 12, 4)
            g = tk.Frame(card, bg=SURF, padx=12, pady=10)
            g.pack(fill="x")
            g.columnconfigure(1, weight=1)
            g.columnconfigure(2, minsize=128)
            self.tile = tk.Canvas(g, width=40, height=40, bg=SURF, highlightthickness=0)
            self.tile.grid(row=0, column=0, sticky="n", padx=(0, 14))
            body = tk.Frame(g, bg=SURF)
            body.grid(row=0, column=1, sticky="ew")
            self.name_lbl = label(body, "", 11, FG, "bold", anchor="w")
            self.name_lbl.pack(fill="x")
            meta = tk.Frame(body, bg=SURF)
            meta.pack(fill="x")
            self.st_lbl = label(meta, "", 9, weight="bold")
            self.st_lbl.pack(side="left")
            self.meta_lbl = label(meta, "", 9, anchor="w")
            self.meta_lbl.pack(side="left", padx=(12, 0), fill="x")
            self.bar = tk.Canvas(body, height=14, bg=SURF, highlightthickness=0)
            self.bar.pack(fill="x", pady=(6, 0))
            spd = tk.Frame(g, bg=SURF)
            spd.grid(row=0, column=2, sticky="e", padx=(16, 8))
            self.speed_lbl = label(spd, "", 13, FG, "bold", DISPLAY, anchor="e")
            self.speed_lbl.pack(anchor="e")
            self.left_lbl = label(spd, "", 9, FAINT, anchor="e")
            self.left_lbl.pack(anchor="e")
            acts = tk.Frame(g, bg=SURF)
            acts.grid(row=0, column=3, sticky="e")
            self.btn = icon_button(acts, "pause", self.toggle, "Pause / reprise")
            self.btn.pack(side="left")
            icon_button(acts, "folder", self.open_dir, "Ouvrir le dossier").pack(side="left")
            icon_button(acts, "x", self.remove, "Retirer de la liste").pack(side="left")
            self.bar.bind("<Configure>", lambda e: self.draw())
            self.render()

        def name(self):
            n = self.snap.get("filename") or self.job.get("name")
            if not n:
                u = self.job.get("origin") or self.job["url"]
                n = magnet_name(u) if u.lower().startswith("magnet:") else \
                    os.path.basename(urllib.parse.urlparse(u).path or u) or u
            return n

        def start(self):
            remember(self.job, "running")
            try:
                self.dl = make_job(self.job, self.job["out_dir"], int(conn_var.get() or 16))
            except ValueError as e:
                self.state, self.snap["error"] = "error", str(e)
                hist.upsert(self.job["hid"], status="error", error=str(e))
                return False
            self.state, self.stopping, self.counted = "running", False, None
            self.thread = threading.Thread(target=self.dl.run, daemon=True)
            self.thread.start()
            return True

        def toggle(self):
            if self.state == "running":
                self.stopping = True
                self.dl.stop()
            elif self.state == "waiting":
                self.state = "paused"
                hist.upsert(self.job["hid"], status="paused")
            elif self.state == "done":
                p = self.snap.get("path")
                if p and os.path.exists(p) and sys.platform == "win32":
                    os.startfile(p)
            else:                                   # en pause / erreur : on relance
                self.state = "waiting"
                hist.upsert(self.job["hid"], status="waiting")
                schedule()
            self.render()

        def open_dir(self):
            d = self.job.get("out_dir", "")
            if os.path.isdir(d) and sys.platform == "win32":
                os.startfile(d)

        def remove(self):
            if self.state == "running":
                if not messagebox.askyesno("Retirer", f"Arrêter « {short(self.name(), 60)} » "
                                           "et le retirer de la liste ?\nIl reste dans l'historique "
                                           "et pourra être repris.", parent=root):
                    return
                self.dl.stop()
            if self.state != "done":            # sinon il revient à chaque lancement
                hist.upsert(self.job["hid"], status="removed")
            self.box.destroy()
            items.pop(self.job["hid"], None)
            relayout(force=True)
            schedule()

        def poll(self):
            if not self.dl:
                return
            if self.state == "running":
                self.snap = self.dl.snapshot()
                got = self.snap.get("downloaded") or 0
                if self.counted is not None and got > self.counted:
                    st["today"]["bytes"] += got - self.counted
                self.counted = got
            finished = self.thread is not None and not self.thread.is_alive()
            if self.state == "running" and (finished or time.monotonic() - self.last_save > 5):
                self.last_save = time.monotonic()
                s = self.snap
                hist.upsert(self.job["hid"], name=s["filename"], size=s["size"],
                            downloaded=s["downloaded"],
                            torrent_file=getattr(self.dl, "saved_torrent", None),
                            status=s["status"] if finished else None,
                            error=s["error"] if finished else None,
                            path=s["path"] if finished else None)
            if finished and self.state == "running":
                self.thread = None
                self.state = self.snap["status"] if self.snap["status"] in (
                    "done", "paused", "error") else "error"
                if self.state == "done":
                    self.done_at = time.strftime("%H:%M")
                    notify(f"Terminé : {short(self.name(), 90)}", OK)

        def render(self):
            s, state = self.snap, self.state
            self.name_lbl.config(text=short(self.name(), 90))
            size, got = s.get("size"), s.get("downloaded") or 0
            pct = f"{100 * got / size:.0f} %" if size else ""
            amount = f"{human(got)} / {human(size)}" if size else (human(got) if got else "")
            txt, col = STATE_TXT.get(state, (state, MUTED))
            if state == "running" and self.stopping:
                txt = "Mise en pause…"
            self.st_lbl.config(text=f"{txt} · {pct}" if pct and state in ("running", "paused")
                               else txt, fg=col)
            parts = []
            if state == "running":
                parts.append(amount)
                if self.torrent:
                    parts.append(s.get("phase") or f"{s.get('peers', 0)} pairs · "
                                                    f"{s.get('seeds', 0)} sources")
                    parts.append(f"↑ {human(s.get('upload', 0))}/s")
                else:
                    n = s.get("active", 0)
                    parts.append(f"{n} connexion{'s' if n > 1 else ''}" if s.get("ranges")
                                 else "1 connexion (serveur sans découpage)")
            elif state == "done":
                parts += [human(size) if size else "", self.job.get("out_dir", "")]
            elif state == "error":
                parts.append(s.get("error") or "Erreur inconnue")
            else:
                parts.append(amount)
            self.meta_lbl.config(text="   ".join(p for p in parts if p),
                                 fg=ERR if state == "error" else MUTED)
            if state == "running":
                self.speed_lbl.config(text=f"{human(s['speed'])}/s", fg=FG)
                self.left_lbl.config(text=f"reste {human_time(s.get('eta'))}")
            else:
                self.speed_lbl.config(text="—", fg=FAINT)
                self.left_lbl.config(text={"paused": "en pause", "waiting": "en attente",
                                           "error": "à réessayer",
                                           "done": f"terminé à {self.done_at}" if self.done_at
                                           else "terminé"}.get(state, ""))
            kind = {"running": "pause", "waiting": "pause", "paused": "play",
                    "error": "retry", "done": "open"}.get(state, "pause")
            if self.btn.kind != kind:
                self.btn.kind = kind
                self.btn.paint()
            self.paint_tile()
            self.draw()

        def paint_tile(self):
            c = self.tile
            c.delete("all")
            col = {"done": OK, "error": ERR, "paused": AMBER, "waiting": AMBER}.get(
                self.state, VIOLET if self.torrent else ACC)
            rounded(c, 0.5, 0.5, 39.5, 39.5, 10, fill=blend(col, SURF, 0.16), outline="")
            kind = "check" if self.state == "done" else ("magnet" if self.torrent else "globe")
            draw_icon(c, kind, 20, col, 10, 10)

        def draw(self):
            c, s = self.bar, self.snap
            c.delete("all")
            w, h = max(c.winfo_width(), 20), 14
            y, x0, x1 = h / 2, 5, w - 5
            span = x1 - x0
            size = s.get("size")
            c.create_line(x0, y, x1, y, width=6, fill=P["track"], capstyle="round")
            running = self.state == "running"
            col = {"done": OK, "error": ERR, "paused": AMBER}.get(self.state, ACC if running else FAINT)
            g = (lambda x: grad((x - x0) / span)) if running else (lambda x: col)
            if self.state == "done":
                c.create_line(x0, y, x1, y, width=6, fill=OK, capstyle="round")
            elif s.get("pieces"):
                pieces = s["pieces"]
                bw = span / len(pieces)
                for i, f in enumerate(pieces):
                    if f > 0:
                        pc = g(x0 + i * bw)
                        c.create_line(x0 + i * bw, y, x0 + (i + 1) * bw + 0.6, y, width=6,
                                      fill=pc if f >= 1 else blend(pc, P["track"], 0.45))
            elif size and s.get("segments"):
                for start, end, pos, active in s["segments"]:
                    a, b = x0 + start / size * span, x0 + pos / size * span
                    xs = a
                    while xs < b:                     # par tranches pour le dégradé
                        xe = min(b, xs + 8)
                        c.create_line(xs, y, xe + 0.5, y, width=6, fill=g(xs))
                        xs = xe
                    if active and running:
                        glow(c, b, y, g(b), SURF, 2.5)
            elif size:
                b = x0 + min(1.0, (s.get("downloaded") or 0) / size) * span
                if b > x0:
                    c.create_line(x0, y, b, y, width=6, fill=g(x0), capstyle="round")
                    xs = x0 + 3
                    while xs < b - 3:
                        xe = min(b - 3, xs + 8)
                        c.create_line(xs, y, xe + 0.5, y, width=6, fill=g(xs))
                        xs = xe
                    if running:
                        glow(c, b, y, g(b), SURF)
            elif running:
                a = (time.time() * 140) % (span + 140) - 140
                c.create_line(max(x0, x0 + a), y, min(x1, x0 + a + 140), y, width=6, fill=ACC,
                              capstyle="round")

    # ----------------------------------------------------------- filtres, tri, liste
    FILTERS = {"all": lambda r: True, "running": lambda r: r.state == "running",
               "waiting": lambda r: r.state in ("waiting", "paused"),
               "done": lambda r: r.state == "done", "torrent": lambda r: r.torrent,
               "http": lambda r: not r.torrent}
    FILTER_NAMES = {"running": "En cours", "waiting": "En attente", "done": "Terminés",
                    "torrent": "Torrents", "http": "Liens directs"}

    def select_filter(key):
        st["filter"] = key
        for c in navs.values():
            c.paint()
        relayout(force=True)

    def select_sort(key):
        st["sort"] = key
        for k, c in segs.items():
            c.paint()
        relayout(force=True)

    def relayout(force=False):
        rows = [r for r in items.values() if FILTERS[st["filter"]](r)]
        if st["sort"] != "added":
            if force or time.monotonic() - st["last_sort"] > 2:    # évite que la liste saute
                st["last_sort"] = time.monotonic()
                if st["sort"] == "speed":
                    rows.sort(key=lambda r: -(r.snap.get("speed") or 0) if r.state == "running"
                              else 1)
                else:
                    rows.sort(key=lambda r: (r.snap.get("eta") or float("inf"))
                              if r.state == "running" else float("inf"))
            else:
                order = {id(r): i for i, r in enumerate(st["shown"])}
                rows.sort(key=lambda r: order.get(id(r), len(order)))
        if not force and rows == st["shown"]:
            return
        for r in st["shown"]:
            if r.box.winfo_exists():
                r.box.pack_forget()
        empty_box.pack_forget()
        for r in rows:
            r.box.pack(fill="x", pady=(0, 8))
        if not rows:
            if st["filter"] == "all":
                empty_title.config(text="Rien en cours pour l'instant")
                empty_text.config(text="Collez un lien ou un magnet ci-dessus, ou glissez vos "
                                       "fichiers .torrent\nn'importe où dans cette fenêtre.")
            else:
                empty_title.config(text=f"Rien dans « {FILTER_NAMES[st['filter']]} »")
                empty_text.config(text="Choisissez « Tout » dans la colonne de gauche pour "
                                       "voir l'ensemble de vos téléchargements.")
            empty_box.pack(fill="x")
        st["shown"] = rows

    # ----------------------------------------------------------- logique
    def current_dir():
        d = dir_var.get().strip() or default_dir()
        save_settings(out_dir=d)
        return d

    def remember(job, status):
        """Inscrit le travail dans la mémoire (sans les cookies)."""
        page = job.get("page") or (job.get("headers") or {}).get("Referer") or None
        hist.upsert(job["hid"], source=job["origin"], page=page, out_dir=job["out_dir"],
                    name=job.get("name"), status=status,
                    kind="torrent" if is_torrent(job["url"], job.get("torrent")) else "http")

    def add_job(job, state="waiting", size=None, downloaded=0):
        job.setdefault("out_dir", current_dir())
        job.setdefault("origin", job["url"])
        job.setdefault("hid", history_id(job["origin"], job["out_dir"]))
        r = items.get(job["hid"])
        if r:
            if job.get("headers"):
                r.job["headers"] = job["headers"]          # cookies tout frais
            if r.state in ("paused", "error"):
                r.state = "waiting"
                r.render()
            elif r.state in ("running", "waiting"):
                notify(f"Déjà dans la liste : {short(r.name(), 80)}", MUTED)
        else:
            r = Row(job, state, size, downloaded)
            items[job["hid"]] = r
            if state == "waiting":
                remember(job, "waiting")
        relayout(force=True)
        schedule()
        return r

    enqueue = add_job                         # utilisé par la fenêtre Historique

    def add_from_text(text):
        """Accepte un ou plusieurs liens / magnets / chemins .torrent."""
        text = (text or "").strip()
        if not text:
            notify("Collez d'abord un lien ou un magnet, ou choisissez un fichier .torrent.", ERR)
            url_entry.focus_set()
            return 0
        tokens = []
        for line in text.splitlines():
            line = line.strip().strip('"')
            if not line:
                continue
            if os.path.isfile(line):
                tokens.append(line)
            else:
                tokens += re.findall(r"(?:magnet:\?\S+|https?://\S+)", line) or [line]
        added, bad = 0, []
        for t in tokens:
            if t.lower().startswith(("magnet:", "http://", "https://")) or (
                    os.path.isfile(t) and t.lower().endswith(".torrent")):
                add_job({"url": t, "torrent": is_torrent(t)})
                added += 1
            else:
                bad.append(t)
        if added:
            url_var.set("")                   # le champ se vide tout seul
            notify(f"{added} téléchargements ajoutés à la liste." if added > 1
                   else "Ajouté à la liste.", OK)
        if bad:
            notify("Ce n'est ni un lien, ni un magnet, ni un fichier .torrent : "
                   f"{short(', '.join(bad), 100)}", ERR)
        return added

    def open_torrents():
        files = filedialog.askopenfilenames(title="Ouvrir des fichiers .torrent",
                                            filetypes=[("Torrent", "*.torrent"), ("Tous", "*.*")])
        if files:
            add_from_text("\n".join(files))

    def on_drop(files):
        st["inbox"].put(("files", files))     # traité dans la boucle de l'interface

    def schedule():
        try:
            limit = max(1, min(10, int(par_var.get())))
        except ValueError:
            limit = 3
        running = sum(1 for r in items.values() if r.state == "running")
        for r in list(items.values()):
            if running >= limit:
                break
            if r.state == "waiting":
                if r.start():
                    running += 1
                r.render()

    def clear_done():
        for hid, r in list(items.items()):
            if r.state == "done":
                r.box.destroy()
                items.pop(hid)
        st["shown"] = [r for r in st["shown"] if r.box.winfo_exists()]
        relayout(force=True)

    def bring_to_front():
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)
        root.after(400, lambda: root.attributes("-topmost", False))

    def tick():
        while not st["inbox"].empty():
            msg = st["inbox"].get_nowait()
            if isinstance(msg, tuple) and msg[0] == "files":
                torrents = [f for f in msg[1] if f.lower().endswith(".torrent")]
                if torrents:
                    add_from_text("\n".join(torrents))
                if len(torrents) < len(msg[1]):
                    notify("Seuls les fichiers .torrent peuvent être déposés ici.", ERR)
            else:                                   # extension ou « Ouvrir avec »
                add_job(msg)
                bring_to_front()
        for r in list(items.values()):
            was = r.state
            r.poll()
            if r.state == "running" or r.state != was:
                r.render()
        schedule()
        relayout()
        running = [r for r in items.values() if r.state == "running"]
        count = collections.Counter(r.state for r in items.values())
        speed = sum(r.snap.get("speed") or 0 for r in running)
        now = time.monotonic()
        if now - st["last_sample"] >= 1:
            st["last_sample"] = now
            st["spark"].append(speed)
        eta = max((r.snap.get("eta") for r in running if r.snap.get("eta")), default=None)
        st["hero_args"] = (speed, len(running), count["waiting"] + count["paused"], eta)
        draw_hero(*st["hero_args"])
        for key, c in navs.items():
            n = sum(1 for r in items.values() if FILTERS[key](r)) if key in FILTERS else 0
            new = str(n) if key in FILTERS and n else ""
            if c.count != new:
                c.count = new
                c.paint()
        list_count.config(text=str(len(st["shown"])) if st["shown"] else "")
        root.title(f"TurboDL — ↓ {human(speed)}/s" if running else "TurboDL")
        if bool(running) != st["awake"]:
            st["awake"] = bool(running)
            keep_awake(st["awake"])
            if st["awake"]:
                awake_row.pack(fill="x", before=sfoot.winfo_children()[1])
            else:
                awake_row.pack_forget()
        if now - st["last_today_save"] > 10:
            st["last_today_save"] = now
            save_settings(today=st["today"])
        root.after(400, tick)

    def check_clipboard(event=None):
        if event is not None and event.widget is not root:
            return
        try:
            clip = root.clipboard_get().strip()
        except tk.TclError:
            return
        if (clip and clip != st["clip_seen"] and not url_var.get().strip()
                and re.match(r"(magnet:\?|https?://)\S+$", clip, re.I)):
            st["clip_seen"] = clip
            url_var.set(clip)
            url_entry.icursor("end")
            notify("Lien trouvé dans le presse-papiers : cliquez « Ajouter » ou appuyez sur Entrée.",
                   ACC)

    def show_history():
        win = tk.Toplevel(root, bg=BG, padx=20, pady=16)
        win.title("TurboDL — historique")
        win.geometry("900x480")
        win.minsize(700, 380)
        style = ttk.Style(win)
        style.theme_use("clam")
        style.configure("H.Treeview", background=CARD, fieldbackground=CARD, foreground=FG,
                        rowheight=26, borderwidth=0, font=(FONT, 10))
        style.configure("H.Treeview.Heading", background=BG, foreground=MUTED, relief="flat",
                        font=(FONT, 8, "bold"))
        style.map("H.Treeview", background=[("selected", blend(PRIMARY, SURF, 0.35))])
        top = tk.Frame(win, bg=BG)
        top.pack(fill="x")
        label(top, "Historique", 16, FG, "bold").pack(side="left")
        q_var = tk.StringVar()
        q_entry = entry(top, q_var)
        q_entry.pack(side="right", ipady=4)
        label(top, "Rechercher ", 9).pack(side="right")

        cols = (("name", "Nom", 300), ("status", "État", 90), ("size", "Taille", 90),
                ("added", "Ajouté le", 120), ("page", "Trouvé sur", 220))
        tree = ttk.Treeview(win, columns=[c for c, _, _ in cols], show="headings",
                            style="H.Treeview", selectmode="browse")
        for c, title, w in cols:
            tree.heading(c, text=title, anchor="w")
            tree.column(c, width=w, anchor="w")
        tree.pack(fill="both", expand=True, pady=(12, 8))
        detail = label(win, "", 9, anchor="w", justify="left", wraplength=840)
        detail.pack(fill="x")

        def fill(*_):
            q = q_var.get().lower().strip()
            tree.delete(*tree.get_children())
            for e in hist.snapshot():
                hay = " ".join(str(e.get(k, "")) for k in ("name", "source", "page")).lower()
                if q and q not in hay:
                    continue
                pct = (f" {100 * e['downloaded'] / e['size']:.0f} %"
                       if e.get("size") and e.get("status") != "done" else "")
                page = e.get("page") or ""
                tree.insert("", "end", iid=e["id"], values=(
                    e.get("name") or e.get("source", ""),
                    STATUS_FR.get(e.get("status"), "?") + pct,
                    human(e["size"]) if e.get("size") else "—", e.get("added", ""),
                    urllib.parse.urlparse(page).hostname or page or "—"))

        def current():
            sel = tree.selection()
            return hist.get(sel[0]) if sel else None

        def on_select(*_):
            e = current()
            if e:
                detail.config(fg=FG, text=(
                    f"Lien : {e.get('source', '')}\n"
                    f"Trouvé sur : {e.get('page') or 'inconnu (lien collé à la main)'}\n"
                    f"Dossier : {e.get('out_dir', '')}"
                    + (f"\nErreur : {e['error']}" if e.get("status") == "error" and e.get("error") else "")))

        def resume():
            e = current()
            if not e:
                return
            if e["id"] in items and items[e["id"]].state in ("running", "waiting"):
                detail.config(text="Déjà en cours.", fg=MUTED)
                return
            enqueue(job_from_entry(e))
            fill()

        def copy_link():
            e = current()
            if e:
                root.clipboard_clear()
                root.clipboard_append(e.get("source", ""))
                detail.config(text="Lien copié.", fg=OK)

        def open_page():
            e = current()
            if e and re.match(r"https?://", e.get("page") or ""):
                import webbrowser
                webbrowser.open(e["page"])

        def open_folder():
            e = current()
            if e and os.path.isdir(e.get("out_dir", "")) and sys.platform == "win32":
                os.startfile(e["out_dir"])

        def forget():
            e = current()
            if e and messagebox.askyesno(
                    "Retirer de l'historique",
                    f"Retirer « {e.get('name') or e.get('source')} » de l'historique ?\n"
                    "Les fichiers téléchargés ne sont pas supprimés.", parent=win):
                hist.remove(e["id"])
                fill()
                detail.config(text="")

        bar = tk.Frame(win, bg=BG)
        bar.pack(fill="x", pady=(10, 0))
        button(bar, "Reprendre", resume, primary=True).pack(side="left")
        button(bar, "Copier le lien", copy_link).pack(side="left", padx=(8, 0))
        button(bar, "Ouvrir la page d'origine", open_page).pack(side="left", padx=(8, 0))
        button(bar, "Ouvrir le dossier", open_folder).pack(side="left", padx=(8, 0))
        button(bar, "Retirer", forget).pack(side="right")
        tree.bind("<<TreeviewSelect>>", on_select)
        tree.bind("<Double-1>", lambda e: resume())
        q_var.trace_add("write", fill)
        fill()

    def install_extension():
        d = extension_dir()
        root.clipboard_clear()
        root.clipboard_append(d)
        if sys.platform == "win32" and os.path.isdir(d):
            os.startfile(d)
        status_lbl.config(fg=FG, text=(
            "Installer l'extension (une seule fois) :\n"
            "1. Ouvrez chrome://extensions (Edge : edge://extensions, Brave : brave://extensions)\n"
            "2. Activez « Mode développeur »\n"
            "3. Cliquez « Charger l'extension non empaquetée » et collez le chemin (déjà copié) :\n"
            f"    {d}"))



    def associate():
        if not getattr(sys, "frozen", False):
            notify("Disponible uniquement dans TurboDL.exe.", ERR)
            return
        if not messagebox.askyesno(
                "Ouvrir les torrents avec TurboDL",
                "TurboDL va devenir le programme qui ouvre :\n"
                "  • les fichiers .torrent (double-clic)\n"
                "  • les liens magnet (clic dans le navigateur)\n\n"
                "Cela ne modifie que vos réglages Windows d'utilisateur.\n"
                "Si vous déplacez TurboDL.exe, refaites ce réglage.\n\nContinuer ?", parent=root):
            return
        try:
            register_handlers(sys.executable)
            notify("Les .torrent et les liens magnet s'ouvrent maintenant avec TurboDL. Si Windows "
                   "demande quel programme utiliser, choisissez TurboDL puis « Toujours ».", OK)
        except OSError as e:
            notify(f"Association impossible : {e}", ERR)

    # ----------------------------------------------------------- démarrage
    url_entry.bind("<Return>", lambda e: add_from_text(url_var.get()))
    root.bind("<FocusIn>", check_clipboard)
    placeholder()
    relayout(force=True)
    notify("Prêt.", OK)

    unfinished = hist.mark_unfinished_interrupted()
    for e in unfinished:                        # coupés : reprise automatique
        add_job(job_from_entry(e))
    for e in hist.snapshot():                   # en pause : listés, pas relancés
        if e.get("status") == "paused" and e["id"] not in items:
            add_job(job_from_entry(e), state="paused", size=e.get("size"),
                    downloaded=e.get("downloaded"))
    if unfinished:
        notify(f"Reprise automatique de {len(unfinished)} téléchargement(s) interrompu(s).", OK)
    if initial:
        add_from_text(initial)

    def on_close():
        active = [r for r in items.values() if r.thread and r.thread.is_alive()]
        for r in active:
            r.dl.stop()                         # chaque moteur sauvegarde son état
        for r in active:
            r.thread.join(timeout=5)
            s = r.dl.snapshot()
            hist.upsert(r.job["hid"], status="interrupted", downloaded=s["downloaded"],
                        torrent_file=getattr(r.dl, "saved_torrent", None))
        for r in items.values():
            if r.state == "waiting":
                hist.upsert(r.job["hid"], status="interrupted")
        save_settings(today=st["today"])
        keep_awake(False)
        if bridge:
            bridge.shutdown()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.deiconify()
    root.update_idletasks()
    if P["dark"] and sys.platform == "win32":    # barre de titre Windows assortie
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
            v = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(v), 4)
        except (OSError, AttributeError):
            pass
    enable_file_drop(root, on_drop)
    if not initial:
        check_clipboard()
    url_entry.focus_set()
    tick()
    root.mainloop()


def main():
    p = argparse.ArgumentParser(description="TurboDL — téléchargement multi-connexions")
    p.add_argument("url", nargs="?", help="lien à télécharger (sans lien : interface graphique)")
    p.add_argument("-c", "--connections", type=int, default=16, help="connexions parallèles (défaut 16)")
    p.add_argument("-o", "--output", default=".", help="dossier de destination")
    p.add_argument("--selftest", metavar="FICHIER", help=argparse.SUPPRESS)
    args = p.parse_args()
    if args.selftest:                      # diagnostic : moteurs embarqués
        lines = [f"TurboDL {VERSION}", f"certificats : {len(SSL_CTX.get_ca_certs())}"]
        try:
            import libtorrent as lt
            lines.append(f"libtorrent {lt.__version__}")
        except ImportError as e:
            lines.append(f"libtorrent ABSENT : {e}")
        lines.append(f"extension : {os.path.isfile(os.path.join(extension_dir(), 'manifest.json'))}")
        with open(args.selftest, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        return
    windowed = sys.stdout is None          # exe sans console
    if args.url and (windowed or is_torrent(args.url) and not args.url.startswith("http")):
        # « Ouvrir avec TurboDL » : si TurboDL tourne déjà, on lui confie le travail
        url = os.path.abspath(args.url) if os.path.isfile(args.url) else args.url
        if forward_to_running(url):
            return
        gui(url)
        return
    if args.url:
        sys.exit(cli(args))
    gui()


def forward_to_running(url):
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{BRIDGE_PORT}/add", json.dumps({"url": url}).encode(),
            {"Content-Type": "application/json", "Origin": EXT_ORIGIN})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()).get("ok", False)
    except (OSError, ValueError):
        return False


if __name__ == "__main__":
    main()
