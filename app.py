#!/usr/bin/env python
import json
import time
import threading
import subprocess
import sys
import os
import signal
import atexit
import requests
from urllib.parse import quote_plus
import spotipy
from spotipy import SpotifyException
from spotipy.oauth2 import SpotifyOAuth
from flask import Flask, render_template, Response, jsonify, request
import psutil
from lyricsgenius import Genius
from collections import deque

def _load_env_file(path):
    if not os.path.exists(path):
        return
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and (k not in os.environ):
                    os.environ[k] = v
    except Exception as e:
        print(f"Failed loading .env file {path}: {e}")

# Load .env if present (local secrets)
_load_env_file(os.path.join(os.path.dirname(__file__), '.env'))
# Read .env if present (local secrets)
_load_env_file(os.path.join(os.path.dirname(__file__), '.env'))

# Configuration from environment (defaults for non-sensitive settings)
# WEATHER_CITY: nom de la ville pour wttr.in (ex: paris, new+york). Encodée automatiquement.
WEATHER_CITY = os.getenv('WEATHER_CITY', 'paris')
WEATHER_CITY_ENCODED = quote_plus(WEATHER_CITY)
# HOST / PORT: interface et port d'écoute du serveur Flask
HOST = os.getenv('HOST', '127.0.0.1')
try:
    PORT = int(os.getenv('PORT', '8080'))
except Exception:
    PORT = 8080

# Developer PIN and control enforcement
# DEV_PIN must be set in the environment (.env). If empty, dev endpoints will be rejected.
DEV_PIN = os.getenv('DEV_PIN', '')
# Control endpoints protection is optional. Set to 'true' to require the PIN for /control/* as well.
REQUIRE_CONTROL_PIN = os.getenv('REQUIRE_CONTROL_PIN', 'false').lower() in ('1', 'true', 'yes')

# Read Spotify credentials from environment (no fallbacks)
CLIENT_ID = os.getenv('SPOTIPY_CLIENT_ID')
CLIENT_SECRET = os.getenv('SPOTIPY_CLIENT_SECRET')
REDIRECT_URI = os.getenv('SPOTIPY_REDIRECT_URI')

# Require credentials to be set (enforce .env / environment-only)
missing = []
if not CLIENT_ID:
    missing.append('SPOTIPY_CLIENT_ID')
if not CLIENT_SECRET:
    missing.append('SPOTIPY_CLIENT_SECRET')
if not REDIRECT_URI:
    missing.append('SPOTIPY_REDIRECT_URI')
if missing:
    print(f"Missing required environment variables: {', '.join(missing)}")
    print("Create a .env file or set those environment variables. See .env.example")
    sys.exit(1)

app = Flask(__name__)

scope = "user-read-currently-playing user-read-playback-state user-modify-playback-state"
sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    redirect_uri=REDIRECT_URI,
    scope=scope
))

state = {
    "track_id": None,
    "title": None,
    "artist": None,
    "album": None,
    "context_name": None,  # Playlist name if playing from playlist
    "cover_url": None,
    "progress_ms": 0,
    "duration_ms": 1,
    "is_playing": False,
    "last_api_time": 0,
}
state_lock = threading.Lock()
dev_overrides = {"cpu": None, "ram": None, "battery": None, "auto_sleep": True, "forced_sleep": False, "brightness": None}

# Weather cache (update only every 20 minutes)
weather_cache = {"data": None, "timestamp": 0}
WEATHER_UPDATE_INTERVAL = 20 * 60  # 20 minutes

WEATHER_FR = {
    "113": "Ensoleillé", "116": "Partiellement nuageux", "119": "Nuageux",
    "122": "Couvert", "143": "Brumeux", "176": "Pluie légère",
    "200": "Orages", "227": "Tempête de neige", "230": "Blizzard",
    "248": "Brouillard", "260": "Brouillard givrant",
    "263": "Bruine légère", "266": "Bruine", "293": "Pluie légère",
    "296": "Pluie légère", "299": "Pluie modérée", "302": "Pluie modérée",
    "305": "Pluie forte", "308": "Pluie forte", "311": "Pluie verglaçante",
    "317": "Grésil", "320": "Grésil", "323": "Neige légère",
    "326": "Neige légère", "329": "Neige modérée", "332": "Neige modérée",
    "335": "Neige forte", "338": "Neige forte", "350": "Grêle",
    "353": "Averse légère", "356": "Averse modérée", "359": "Averse forte",
    "368": "Averse de neige", "371": "Averse de neige", "374": "Averse de grêle",
    "386": "Orage", "389": "Orage fort", "392": "Orage neigeux",
}

# Polling / fast-poll intervals (seconds)
# Predictive polling strategy:
# - idle (no track / not playing): 30s
# - playing & time_left > 30s: 20s
# - playing & 10s <= time_left <= 30s: 5s
# - playing & time_left < 10s: 2s
# - FAST_POLL used for immediate bursts after actions: 1.5s (3 times)
POLL_IDLE = 30
POLL_PLAYING_LONG = 20
POLL_PLAYING_MED = 5
POLL_PLAYING_SHORT = 2
FAST_POLL_INTERVAL = 1.5
FAST_POLL_COUNT = 3

# Quota limits and tracking
API_LIMIT_PER_MIN = 300
API_LIMIT_PER_DAY = 8000
api_calls_min = deque()
api_calls_day = deque()
api_lock = threading.Lock()

# Persist quota state across restarts
QUOTA_STATE_PATH = os.path.join(os.path.dirname(__file__), "quota_state.json")

# Simple runtime metrics
metrics_lock = threading.Lock()
metrics = {
    "api_calls_total": 0,
    "quota_exceeded_total": 0
}
# Debounce interval for persisting quota state to disk (seconds)
QUOTA_SAVE_INTERVAL = 60
# Last time we saved quota state (epoch seconds)
LAST_QUOTA_SAVE_TIME = 0

def load_quota_state():
    global api_calls_min, api_calls_day
    try:
        if os.path.exists(QUOTA_STATE_PATH):
            with open(QUOTA_STATE_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f) or {}
                min_list = data.get("api_calls_min", []) or []
                day_list = data.get("api_calls_day", []) or []
                now = time.time()
                api_calls_min = deque([ts for ts in min_list if ts >= now - 60])
                api_calls_day = deque([ts for ts in day_list if ts >= now - 86400])
    except Exception as e:
        print(f"Failed loading quota state: {e}")
        api_calls_min = deque()
        api_calls_day = deque()

def save_quota_state():
    global LAST_QUOTA_SAVE_TIME
    try:
        with api_lock:
            data = {
                "api_calls_min": list(api_calls_min),
                "api_calls_day": list(api_calls_day),
                "saved_at": time.time()
            }
        # Write state to disk
        with open(QUOTA_STATE_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        # record last successful save time
        LAST_QUOTA_SAVE_TIME = time.time()
    except Exception as e:
        print(f"Failed saving quota state: {e}")

def should_make_api_call():
    now = time.time()
    with api_lock:
        while api_calls_min and api_calls_min[0] < now - 60:
            api_calls_min.popleft()
        while api_calls_day and api_calls_day[0] < now - 86400:
            api_calls_day.popleft()
        return len(api_calls_min) < API_LIMIT_PER_MIN and len(api_calls_day) < API_LIMIT_PER_DAY

def record_api_call():
    global LAST_QUOTA_SAVE_TIME
    now = time.time()
    with api_lock:
        api_calls_min.append(now)
        api_calls_day.append(now)
    with metrics_lock:
        metrics["api_calls_total"] += 1
    # Debounced save: only flush to disk if enough time passed since last save
    try:
        if now - LAST_QUOTA_SAVE_TIME >= QUOTA_SAVE_INTERVAL:
            save_quota_state()
    except Exception:
        pass

def safe_spotify_request(func, *args, **kwargs):
    """Wrapper for calling Spotify API that enforces quota limits.
    Returns a dict: {'ok': True, 'result': ...} or {'ok': False, 'quota': True} or {'ok': False, 'error': Exception}
    """
    if not should_make_api_call():
        with metrics_lock:
            metrics["quota_exceeded_total"] += 1
        return {'ok': False, 'quota': True}
    try:
        res = func(*args, **kwargs)
    except Exception as e:
        return {'ok': False, 'error': e}
    record_api_call()
    return {'ok': True, 'result': res}

# Poll event used to interrupt waits in fetch_spotify_loop
poll_event = threading.Event()
# fast poll counter (global), modified by control endpoints
fast_polls_remaining = 0

# Persistent cache for playlist names to avoid repeated calls to sp.playlist()
PLAYLIST_CACHE_PATH = os.path.join(os.path.dirname(__file__), "playlist_cache.json")
playlist_cache = {}

def load_playlist_cache():
    global playlist_cache
    try:
        if os.path.exists(PLAYLIST_CACHE_PATH):
            with open(PLAYLIST_CACHE_PATH, 'r', encoding='utf-8') as f:
                playlist_cache = json.load(f) or {}
    except Exception as e:
        print(f"Failed loading playlist cache: {e}")
        playlist_cache = {}

def save_playlist_cache():
    try:
        with open(PLAYLIST_CACHE_PATH, 'w', encoding='utf-8') as f:
            json.dump(playlist_cache, f)
    except Exception as e:
        print(f"Failed saving playlist cache: {e}")

# Initialize cache at startup
load_playlist_cache()
load_quota_state()

# Register graceful shutdown to ensure quota state is saved at exit
def _on_exit(signum=None, frame=None):
    try:
        save_quota_state()
    except Exception:
        pass
    try:
        sys.exit(0)
    except SystemExit:
        os._exit(0)

atexit.register(save_quota_state)
signal.signal(signal.SIGINT, _on_exit)
try:
    signal.signal(signal.SIGTERM, _on_exit)
except Exception:
    # SIGTERM may not be available on some platforms
    pass


# Server-side enforcement: require DEV_PIN for /dev/* and optionally for /control/*
@app.before_request
def _enforce_dev_pin():
    path = (request.path or "").lower()
    # only enforce for endpoints that start with /dev or /control
    if path.startswith('/dev') or path.startswith('/control'):
        # /control endpoints can be left open if REQUIRE_CONTROL_PIN is False
        if path.startswith('/control') and not REQUIRE_CONTROL_PIN:
            return None

        # DEV_PIN must be configured on the server
        if not DEV_PIN:
            return jsonify({"ok": False, "error": "dev_pin_not_configured"}), 503

        # Extract provided pin from header / auth / query / json body
        provided = None
        # headers (case-insensitive)
        provided = request.headers.get('X-DEV-PIN') or request.headers.get('X-ADMIN-PIN')
        if not provided:
            auth = (request.headers.get('Authorization') or '')
            if auth.lower().startswith('bearer '):
                provided = auth[7:].strip()
        if not provided:
            provided = request.args.get('pin') or request.args.get('dev_pin')
        if not provided and request.is_json:
            try:
                j = request.get_json(silent=True)
                if isinstance(j, dict):
                    provided = j.get('pin') or j.get('dev_pin')
            except Exception:
                provided = None

        if not provided or provided != DEV_PIN:
            return jsonify({"ok": False, "error": "unauthorized"}), 401

def fetch_spotify_loop():
    last_track_id = None
    last_playlist_id = None

    global fast_polls_remaining

    while True:
        try:
            # Use safe wrapper for current_playback to respect quota
            playback_call = safe_spotify_request(sp.current_playback)
            playback = None
            if playback_call.get('ok'):
                playback = playback_call['result']
            elif playback_call.get('quota'):
                # Quota exhausted; skip this cycle and behave as if nothing is playing
                print('Spotify quota reached, skipping playback fetch')
                playback = None
            else:
                # Other error, log and continue
                print(f"Spotify current_playback error: {playback_call.get('error')}")
                playback = None

            save_needed = False
            with state_lock:
                state["last_api_time"] = time.time()
                if playback and playback.get("item"):
                    track = playback["item"]
                    current_track_id = track.get("id")
                    state["track_id"] = current_track_id
                    state["title"] = track.get("name")
                    state["artist"] = track.get("artists")[0].get("name") if track.get("artists") else None
                    state["album"] = track.get("album", {}).get("name")
                    images = track.get("album", {}).get("images", [])
                    state["cover_url"] = images[0]["url"] if images else None
                    state["progress_ms"] = playback.get("progress_ms", 0)
                    state["duration_ms"] = track.get("duration_ms", 1)
                    state["is_playing"] = playback.get("is_playing", False)

                    # LAZY LOAD + CACHE: Only fetch playlist info if track actually changed
                    if current_track_id != last_track_id:
                        # quick polling bursts after a track change for reactivity
                        fast_polls_remaining = FAST_POLL_COUNT
                        state["context_name"] = None
                        last_playlist_id = None
                        if playback.get("context"):
                            context = playback["context"]
                            if context.get("type") == "playlist":
                                try:
                                    uri = context.get("uri", "")
                                    if uri:
                                        playlist_id = uri.split(":")[-1]
                                        if playlist_id != last_playlist_id:
                                            # check persistent cache first
                                            cached = playlist_cache.get(playlist_id)
                                            if cached:
                                                state["context_name"] = cached
                                                last_playlist_id = playlist_id
                                            else:
                                                # call sp.playlist through safe wrapper
                                                pcall = safe_spotify_request(lambda pid=playlist_id: sp.playlist(pid))
                                                if pcall.get('ok'):
                                                    name = pcall['result'].get("name")
                                                    state["context_name"] = name
                                                    playlist_cache[playlist_id] = name
                                                    save_needed = True
                                                    last_playlist_id = playlist_id
                                                elif pcall.get('quota'):
                                                    # cannot fetch playlist name due to quota
                                                    pass
                                                else:
                                                    print(f"Error getting playlist name: {pcall.get('error')}")
                                except Exception as e:
                                    print(f"Error parsing playback context: {e}")
                        last_track_id = current_track_id
                else:
                    state["is_playing"] = False
                    state["track_id"] = None
                    last_track_id = None

            # persist cache if we added new entries (do this outside the lock)
            if save_needed:
                try:
                    save_playlist_cache()
                except Exception:
                    pass
        except Exception as e:
            print(f"Spotify loop error: {e}")

        # Determine adaptive timeout
        try:
            timeout = POLL_IDLE
            if fast_polls_remaining > 0:
                timeout = FAST_POLL_INTERVAL
            else:
                with state_lock:
                    if state.get('is_playing'):
                        duration = max(1, float(state.get('duration_ms', 1)))
                        progress = float(state.get('progress_ms', 0))
                        time_left = max(0.0, (duration - progress) / 1000.0)
                        if time_left > 30:
                            timeout = POLL_PLAYING_LONG
                        elif time_left >= 10:
                            timeout = POLL_PLAYING_MED
                        else:
                            timeout = POLL_PLAYING_SHORT
                    else:
                        timeout = POLL_IDLE
        except Exception:
            timeout = POLL_IDLE

        # Wait interruptibly
        poll_event.clear()
        woke = poll_event.wait(timeout)
        if woke:
            # immediate re-poll requested (e.g. a control endpoint triggered)
            continue

        # decrement fast poll counter if active
        if fast_polls_remaining > 0:
            fast_polls_remaining -= 1

def get_weather():
    global weather_cache
    
    now = time.time()
    # Only update cache if it's been more than 20 minutes
    if weather_cache["data"] is None or (now - weather_cache["timestamp"]) > WEATHER_UPDATE_INTERVAL:
        try:
            # Use configured city for wttr.in (encoded)
            url = f"https://wttr.in/{WEATHER_CITY_ENCODED}?format=j1"
            r = requests.get(url, timeout=5)
            data = r.json()
            current = data["current_condition"][0]
            code = str(current["weatherCode"])
            desc = WEATHER_FR.get(code, current["weatherDesc"][0]["value"])
            weather_cache["data"] = {"temp": current["temp_C"], "desc": desc, "icon": code}
            weather_cache["timestamp"] = now
            print(f"Weather updated: {weather_cache['data']}")
        except Exception as e:
            print(f"Weather error: {e}")
            if weather_cache["data"] is None:
                weather_cache["data"] = {"temp": "--", "desc": "N/A", "icon": "113"}
    
    return weather_cache["data"]

@app.route("/")
def index():
    # Pass developer PIN from environment into the template (avoid hard-coded PIN in source)
    dev_pin = os.getenv('DEV_PIN', '')
    return render_template("index.html", dev_pin=dev_pin)

@app.route("/stream")
def stream():
    def event_generator():
            # Send SSE only on real changes or as a heartbeat every few seconds
            last_sent = None
            last_sent_api_time = 0
            last_sent_time = 0
            HEARTBEAT_INTERVAL = 5  # seconds, max interval for resync
            SLEEP_INTERVAL = 1      # loop sleep to reduce CPU
            while True:
                send = False
                with state_lock:
                    now = time.time()
                    time_since_api = (now - state["last_api_time"]) * 1000
                    if state["is_playing"]:
                        interpolated_progress = min(
                            state["progress_ms"] + time_since_api,
                            state["duration_ms"]
                        )
                    else:
                        interpolated_progress = state["progress_ms"]
                    api_time = state.get("last_api_time", 0)
                    data = {
                        "track_id": state["track_id"],
                        "title": state["title"],
                        "artist": state["artist"],
                        "album": state["album"],
                        "context_name": state["context_name"],
                        "cover_url": state["cover_url"],
                        "progress_ms": int(interpolated_progress),
                        "duration_ms": state["duration_ms"],
                        "is_playing": state["is_playing"],
                        "auto_sleep": True if dev_overrides.get("auto_sleep") is None else bool(dev_overrides.get("auto_sleep")),
                        "forced_sleep": False if dev_overrides.get("forced_sleep") is None else bool(dev_overrides.get("forced_sleep")),
                        "brightness": dev_overrides.get("brightness", None),
                    }

                    # Decide if this update should be sent:
                    if last_sent is None:
                        send = True
                    else:
                        # Fundamental metadata changes
                        if (
                            data["track_id"] != last_sent.get("track_id") or
                            data["is_playing"] != last_sent.get("is_playing") or
                            data["duration_ms"] != last_sent.get("duration_ms") or
                            data["title"] != last_sent.get("title") or
                            data["artist"] != last_sent.get("artist") or
                            data["album"] != last_sent.get("album") or
                            data["context_name"] != last_sent.get("context_name") or
                            data["cover_url"] != last_sent.get("cover_url")
                        ):
                            send = True
                        else:
                            # If the server did a fresh API poll and progress jumped significantly, send to resync
                            if api_time != last_sent_api_time:
                                if abs(data["progress_ms"] - last_sent.get("progress_ms", 0)) > 2000:
                                    send = True

                    # Heartbeat: force a send at least every HEARTBEAT_INTERVAL seconds
                    if not send and (time.time() - last_sent_time) >= HEARTBEAT_INTERVAL:
                        send = True

                if send:
                    yield f"data: {json.dumps(data)}\n\n"
                    # copy snapshot for comparisons
                    last_sent = data.copy()
                    last_sent_api_time = api_time
                    last_sent_time = time.time()

                time.sleep(SLEEP_INTERVAL)
    return Response(event_generator(), mimetype="text/event-stream")

@app.route("/metrics")
def metrics_endpoint():
    with api_lock, metrics_lock:
        now = time.time()
        calls_min = sum(1 for ts in api_calls_min if ts >= now - 60)
        calls_day = sum(1 for ts in api_calls_day if ts >= now - 86400)
        data = {
            "api_calls_total": metrics.get("api_calls_total", 0),
            "api_calls_last_min": calls_min,
            "api_calls_last_day": calls_day,
            "quota_exceeded_total": metrics.get("quota_exceeded_total", 0),
            "api_limit_per_min": API_LIMIT_PER_MIN,
            "api_limit_per_day": API_LIMIT_PER_DAY,
            "quota_state_file": QUOTA_STATE_PATH
        }
    return jsonify(data)

@app.route("/weather")
def weather():
    return jsonify(get_weather())

@app.route("/sysinfo")
def sysinfo():
    battery = psutil.sensors_battery()
    cpu = psutil.cpu_percent(interval=0.1)
    ram = psutil.virtual_memory().percent
    bat = battery.percent if battery else None
    plugged = battery.power_plugged if battery else True
    # CPU temperature support removed (not used)
    # Apply developer overrides if present
    with state_lock:
        if dev_overrides.get('cpu') is not None:
            cpu = dev_overrides['cpu']
        if dev_overrides.get('ram') is not None:
            ram = dev_overrides['ram']
        if dev_overrides.get('battery') is not None:
            bat = dev_overrides['battery']
    return jsonify({
        "cpu": cpu,
        "ram": ram,
        "battery": bat,
        "plugged": plugged,
    })


@app.route("/dev/set_sysinfo", methods=["POST"])
def dev_set_sysinfo():
    data = request.get_json() or {}
    with state_lock:
        if 'cpu' in data:
            dev_overrides['cpu'] = float(data['cpu']) if data['cpu'] is not None else None
        if 'ram' in data:
            dev_overrides['ram'] = float(data['ram']) if data['ram'] is not None else None
        if 'battery' in data:
            dev_overrides['battery'] = float(data['battery']) if data['battery'] is not None else None
    return jsonify({"ok": True, "overrides": dev_overrides})


@app.route("/dev/clear_overrides", methods=["POST"])
def dev_clear_overrides():
    with state_lock:
        for k in dev_overrides:
            dev_overrides[k] = None
    return jsonify({"ok": True})


@app.route("/dev/set_auto_sleep", methods=["POST"])
def dev_set_auto_sleep():
    data = request.get_json() or {}
    val = data.get('auto_sleep')
    with state_lock:
        dev_overrides['auto_sleep'] = bool(val)
    return jsonify({"ok": True, "overrides": dev_overrides})


@app.route("/dev/set_forced_sleep", methods=["POST"])
def dev_set_forced_sleep():
    data = request.get_json() or {}
    val = data.get('forced_sleep')
    with state_lock:
        dev_overrides['forced_sleep'] = bool(val)
    return jsonify({"ok": True, "overrides": dev_overrides})


@app.route("/dev/set_brightness", methods=["POST"])
def dev_set_brightness():
    data = request.get_json() or {}
    val = data.get('brightness')
    with state_lock:
        if val is None:
            dev_overrides['brightness'] = None
        else:
            try:
                dev_overrides['brightness'] = float(val)
            except Exception:
                dev_overrides['brightness'] = None
    return jsonify({"ok": True, "overrides": dev_overrides})


@app.route("/dev/reset_brightness", methods=["POST"])
def dev_reset_brightness():
    with state_lock:
        dev_overrides['brightness'] = None
    return jsonify({"ok": True, "overrides": dev_overrides})


@app.route("/dev/get_dev_overrides")
def dev_get_dev_overrides():
    with state_lock:
        return jsonify(dev_overrides)


@app.route("/dev/reset_weather", methods=["POST"])
def dev_reset_weather():
    global weather_cache
    with state_lock:
        weather_cache["data"] = None
        weather_cache["timestamp"] = 0
    return jsonify({"ok": True})


@app.route("/dev/get_server_time")
def dev_get_server_time():
    return jsonify({"server_time": int(time.time() * 1000)})


@app.route("/dev/reconnect_spotify", methods=["POST"])
def dev_reconnect_spotify():
    # Try to fetch current playback using safe wrapper
    res = safe_spotify_request(sp.current_playback)
    if res.get('ok'):
        return jsonify({"ok": True, "info": bool(res.get('result'))})
    else:
        if res.get('quota'):
            return jsonify({"ok": False, "error": "quota_exceeded"}), 429
        return jsonify({"ok": False, "error": str(res.get('error'))}), 500


@app.route("/dev/full_reload", methods=["POST"])
def dev_full_reload():
    # Attempt to fully restart the Python process and relaunch Edge (Windows).
    def do_restart():
        # Kill any existing Edge instances first
        try:
            subprocess.run(["taskkill", "/F", "/IM", "msedge.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

        # Prepare a detached PowerShell command to launch Edge after a short delay
        edge_paths = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
        edge_exe = None
        for p in edge_paths:
            if os.path.exists(p):
                edge_exe = p
                break

        if edge_exe is None:
            edge_exe = "msedge"


        # PowerShell helper: poll the local server until it responds, then launch Edge.
        server_url = f"http://{HOST}:{PORT}"
        ps_cmd = (
            f"$url='{server_url}'; $max=60; $i=0; "
            "while ($i -lt $max) { try { $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 1; "
            "if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 400) { break } } catch { } Start-Sleep -Milliseconds 250; $i++ } ; "
            "Start-Process -FilePath '" + edge_exe + "' -ArgumentList '--kiosk','" + server_url + "','--edge-kiosk-type=fullscreen','--no-first-run'"
        )
        try:
            subprocess.Popen(["powershell", "-NoProfile", "-Command", ps_cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        except Exception:
            pass

        # Small pause to ensure the launch helper is started, then replace process
        time.sleep(0.3)
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception:
            try:
                subprocess.Popen([sys.executable] + sys.argv, close_fds=True)
            except Exception:
                pass
            time.sleep(0.3)
            os._exit(0)

    threading.Thread(target=do_restart, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/control/play", methods=["POST"])
def control_play():
    global fast_polls_remaining
    # Call Spotify via safe wrapper
    res = safe_spotify_request(sp.start_playback)
    # Wake fetch loop regardless so it can re-sync quickly
    poll_event.set()
    if not res.get('ok'):
        if res.get('quota'):
            return jsonify({"ok": False, "error": "quota_exceeded"}), 429
        return jsonify({"ok": False, "error": str(res.get('error'))}), 500
    with state_lock:
        fast_polls_remaining = FAST_POLL_COUNT
    return "", 204

@app.route("/control/pause", methods=["POST"])
def control_pause():
    global fast_polls_remaining
    res = safe_spotify_request(sp.pause_playback)
    poll_event.set()
    if not res.get('ok'):
        if res.get('quota'):
            return jsonify({"ok": False, "error": "quota_exceeded"}), 429
        return jsonify({"ok": False, "error": str(res.get('error'))}), 500
    with state_lock:
        fast_polls_remaining = FAST_POLL_COUNT
    return "", 204

@app.route("/control/next", methods=["POST"])
def control_next():
    global fast_polls_remaining
    res = safe_spotify_request(sp.next_track)
    poll_event.set()
    if not res.get('ok'):
        if res.get('quota'):
            return jsonify({"ok": False, "error": "quota_exceeded"}), 429
        return jsonify({"ok": False, "error": str(res.get('error'))}), 500
    with state_lock:
        fast_polls_remaining = FAST_POLL_COUNT
    return "", 204

@app.route("/control/prev", methods=["POST"])
def control_prev():
    global fast_polls_remaining
    res = safe_spotify_request(sp.previous_track)
    poll_event.set()
    if not res.get('ok'):
        if res.get('quota'):
            return jsonify({"ok": False, "error": "quota_exceeded"}), 429
        return jsonify({"ok": False, "error": str(res.get('error'))}), 500
    with state_lock:
        fast_polls_remaining = FAST_POLL_COUNT
    return "", 204

@app.route("/control/seek/<int:ms>", methods=["POST"])
def control_seek(ms):
    global fast_polls_remaining
    res = safe_spotify_request(lambda m=ms: sp.seek_track(m))
    poll_event.set()
    if not res.get('ok'):
        if res.get('quota'):
            return jsonify({"ok": False, "error": "quota_exceeded"}), 429
        return jsonify({"ok": False, "error": str(res.get('error'))}), 500
    with state_lock:
        fast_polls_remaining = FAST_POLL_COUNT
    return "", 204

@app.route("/exit", methods=["POST"])
def exit_kiosk():
    subprocess.Popen(["taskkill", "/F", "/IM", "msedge.exe"])
    threading.Timer(0.5, lambda: os._exit(0)).start()
    return "", 204

if __name__ == "__main__":
    t = threading.Thread(target=fetch_spotify_loop, daemon=True)
    t.start()
    app.run(host=HOST, port=PORT, threaded=True)