"""
RGB Keyboard Controller — Tray + Web Server
Run this to start the tray icon and local web API on port 5123.
  LightManager.exe            — start normally
  LightManager.exe --debug    — start with file logging
"""

import ctypes
import json
import logging
import os
import sys
import threading
import time
import webbrowser

from flask import Flask, jsonify, request, send_from_directory
import hid
import psutil
import pystray
from PIL import Image, ImageDraw

# ──────────────────────────────────────────────
VENDOR_ID       = 0x3151
WIRED_PID       = 0x0000   # wired mode — match any PID from this vendor
WIRELESS_PID    = 0x5038   # wireless / 2.4 GHz dongle

# Resolve base dir: works both as .py and as PyInstaller .exe
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
    BUNDLE_DIR = sys._MEIPASS  # PyInstaller temp extraction dir
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    BUNDLE_DIR = BASE_DIR

CONFIG_FILE = os.path.join(BASE_DIR, "keyboard_config.json")
STATIC_DIR  = BUNDLE_DIR  # gui.html is bundled inside the exe
LOG_FILE = os.path.join(BASE_DIR, "keyboard_rgb.log")
PORT = 5123
DEBUG_MODE = "--debug" in sys.argv

# Setup logger
logger = logging.getLogger("rgb_keyboard")
if DEBUG_MODE:
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.DEBUG,
        format="%(asctime)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger.info("=== Server started (debug mode) ===")
# ──────────────────────────────────────────────


# ─── Protocol ─────────────────────────────────

def calc_checksum(data_bytes):
    return 0xFF - (sum(data_bytes[:8]) & 0xFF)


def build_packet(header_bytes):
    checksum = calc_checksum(header_bytes)
    return header_bytes + [checksum] + [0x00] * (64 - len(header_bytes) - 1)


def build_user_picture(brightness=4, pattern=1, **_):
    pattern_byte = (pattern - 1) * 0x10
    header = [0x07, 0x0d, 0x04, brightness, pattern_byte, 0x00, 0xc8, 0xc8]
    return build_packet(header)


def build_rgb_effect(effect_id, default_mode=0x07, brightness=4, speed=0,
                     rgb=(255, 0, 0), default_color=False, **_):
    mode = default_mode if default_color else (default_mode + 1)
    speed_byte = 4 - speed  # firmware: 0=fast, 4=slow; GUI: 0=slow, 4=fast
    r, g, b = (0xFA, 0xFF, 0xFA) if default_color else rgb
    header = [0x07, effect_id, speed_byte, brightness, mode, r, g, b]
    return build_packet(header)


# Polling-rate byte values  (byte[2] in a 0x03 SET_REPORT)
POLLING_RATE_MAP = {
    125:  6,
    250:  5,
    500:  4,
    1000: 3,
    2000: 2,
    4000: 1,
    8000: 0,
}
POLLING_RATE_OPTIONS = [125, 250, 500, 1000, 2000, 4000, 8000]


def build_polling_rate_packet(hz):
    rate_val = POLLING_RATE_MAP[hz]
    header = [0x03, 0x00, rate_val, 0x00, 0x00, 0x00, 0x00]
    return build_packet(header)


EFFECTS = {
    "user_picture_1": {"builder": lambda **kw: build_user_picture(pattern=1, **kw), "type": "picture"},
    "user_picture_2": {"builder": lambda **kw: build_user_picture(pattern=2, **kw), "type": "picture"},
    "user_picture_3": {"builder": lambda **kw: build_user_picture(pattern=3, **kw), "type": "picture"},
    "user_picture_4": {"builder": lambda **kw: build_user_picture(pattern=4, **kw), "type": "picture"},
    "user_picture_5": {"builder": lambda **kw: build_user_picture(pattern=5, **kw), "type": "picture"},
    "colorful_vh":       {"builder": lambda **kw: build_rgb_effect(0x10, **kw), "type": "rgb"},
    "dynamic_breathing": {"builder": lambda **kw: build_rgb_effect(0x02, **kw), "type": "rgb"},
    "meteor":            {"builder": lambda **kw: build_rgb_effect(0x12, **kw), "type": "rgb"},
    "waves_ripple":      {"builder": lambda **kw: build_rgb_effect(0x05, **kw), "type": "rgb"},
    "train":             {"builder": lambda **kw: build_rgb_effect(0x17, **kw), "type": "rgb"},
    "snow":              {"builder": lambda **kw: build_rgb_effect(0x11, **kw), "type": "rgb"},
    "peaks_rising":      {"builder": lambda **kw: build_rgb_effect(0x09, **kw), "type": "rgb"},
    "steam_stream":      {"builder": lambda **kw: build_rgb_effect(0x07, **kw), "type": "rgb"},
    "drift":             {"builder": lambda **kw: build_rgb_effect(0x04, default_mode=0x27, **kw), "type": "rgb"},
    "stars_twinkle":     {"builder": lambda **kw: build_rgb_effect(0x06, **kw), "type": "rgb"},
    "like_shadows":      {"builder": lambda **kw: build_rgb_effect(0x08, **kw), "type": "rgb"},
    "sine_wave":         {"builder": lambda **kw: build_rgb_effect(0x0A, **kw), "type": "rgb"},
    "flowing_spring":    {"builder": lambda **kw: build_rgb_effect(0x0B, **kw), "type": "rgb"},
    "flowers_blooming":  {"builder": lambda **kw: build_rgb_effect(0x0C, **kw), "type": "rgb"},
    "lasers":            {"builder": lambda **kw: build_rgb_effect(0x0E, **kw), "type": "rgb"},
    "peak_turn":         {"builder": lambda **kw: build_rgb_effect(0x0F, **kw), "type": "rgb"},
    "light_trace":       {"builder": lambda **kw: build_rgb_effect(0x13, **kw), "type": "rgb"},
    "endless":           {"builder": lambda **kw: build_rgb_effect(0x18, **kw), "type": "rgb"},
}

EFFECT_DISPLAY_NAMES = {
    "user_picture_1": "User Picture 1",
    "user_picture_2": "User Picture 2",
    "user_picture_3": "User Picture 3",
    "user_picture_4": "User Picture 4",
    "user_picture_5": "User Picture 5",
    "colorful_vh": "Colorful Vertical & Horizontal",
    "dynamic_breathing": "Dynamic Breathing",
    "meteor": "Meteor",
    "waves_ripple": "Waves Ripple",
    "train": "Train",
    "snow": "Snow",
    "peaks_rising": "Peaks Rising",
    "steam_stream": "Steam Stream",
    "drift": "Drift",
    "stars_twinkle": "Stars Twinkle",
    "like_shadows": "Like Shadows",
    "sine_wave": "Sine Wave",
    "flowing_spring": "Flowing Spring",
    "flowers_blooming": "Flowers Blooming",
    "lasers": "Lasers",
    "peak_turn": "Peak Turn",
    "light_trace": "Light Trace",
    "endless": "Endless",
}


# ─── Config persistence ──────────────────────

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {}


def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def get_effect_config(effect_name):
    cfg = load_config()
    etype = EFFECTS[effect_name]["type"]
    defaults = {"brightness": 4}
    if etype == "rgb":
        defaults.update({"speed": 0, "rgb": [255, 0, 0], "default_color": False})
    return cfg.get(effect_name, defaults)


def set_effect_config(effect_name, settings):
    cfg = load_config()
    cfg[effect_name] = settings
    save_config(cfg)


# ─── HID send ────────────────────────────────

def _try_send_hid(payload, vendor_id, product_id, effect_name, label):
    """Try to send payload to a keyboard matching vendor/product on interface 2.
    Returns True on success."""
    for device_dict in hid.enumerate(vendor_id, product_id):
        path = device_dict['path'].decode()
        if '&mi_02' in path.lower():
            dev = hid.device()
            dev.open_path(device_dict['path'])
            dev.send_feature_report([0x00] + payload)
            dev.close()
            hex_str = ' '.join(f'{b:02x}' for b in payload[:9])
            msg = f"Sent {effect_name} via {label}: [{hex_str} ...]"
            print(msg)
            if DEBUG_MODE:
                logger.info(msg)
            return True
    return False


def send_to_keyboard(effect_name, settings):
    effect = EFFECTS[effect_name]
    kwargs = {"brightness": settings.get("brightness", 4)}
    if effect["type"] == "rgb":
        kwargs["speed"] = settings.get("speed", 0)
        kwargs["default_color"] = settings.get("default_color", False)
        kwargs["rgb"] = tuple(settings.get("rgb", [255, 0, 0]))

    payload = effect["builder"](**kwargs)

    # 1) Try wired keyboard first
    if _try_send_hid(payload, VENDOR_ID, WIRED_PID, effect_name, "wired"):
        return True

    # 2) Fall back to wireless
    if _try_send_hid(payload, VENDOR_ID, WIRELESS_PID, effect_name, "wireless"):
        return True

    if DEBUG_MODE:
        logger.warning(f"Keyboard not found for {effect_name} (tried wired & wireless)")
    return False


def send_polling_rate(hz):
    """Send a polling-rate change to the keyboard."""
    if hz not in POLLING_RATE_MAP:
        return False
    payload = build_polling_rate_packet(hz)
    label = f"polling_rate_{hz}Hz"
    if _try_send_hid(payload, VENDOR_ID, WIRED_PID, label, "wired"):
        return True
    if _try_send_hid(payload, VENDOR_ID, WIRELESS_PID, label, "wireless"):
        return True
    if DEBUG_MODE:
        logger.warning(f"Keyboard not found for {label} (tried wired & wireless)")
    return False


# ─── Program associations ─────────────────────

def load_associations():
    """Load program-to-effect mappings. Returns dict like:
    { "default_effect": "lasers", "default_exe": "explorer.exe",
      "programs": { "doom.exe": "meteor", "spotify.exe": "sine_wave", ... },
      "program_names": { "doom.exe": "DOOM", ... },
      "polling_rates": { "__default__": 1000, "doom.exe": 8000, ... } }
    """
    cfg = load_config()
    assoc = cfg.get("_associations", {
        "default_effect": "colorful_vh",
        "default_exe": "explorer.exe",
        "programs": {},
        "program_names": {},
        "polling_rates": {},
        "enabled": True,
    })
    assoc.setdefault("program_names", {})
    return assoc


def save_associations(assoc):
    cfg = load_config()
    cfg["_associations"] = assoc
    save_config(cfg)


# ─── Process watcher ──────────────────────────

class ProcessWatcher:
    def __init__(self):
        self._lock = threading.Lock()
        self._running = False
        self._thread = None
        self._current_effect = None      # effect currently active
        self._current_rate = None        # polling rate currently active
        self._current_profile = None     # exe name or "Default" for active profile
        self._last_send_time = 0         # rate limit
        self._pending_effect = None      # debounce: what we want to switch to
        self._pending_rate = None        # debounce: polling rate to switch to
        self._pending_since = 0          # debounce: when we first saw it
        self.POLL_INTERVAL = 2.0         # how often to scan processes (seconds)
        self.DEBOUNCE_TIME = 1.0         # seconds before committing a switch
        self.MIN_SEND_GAP = 1.0          # minimum seconds between USB sends

    def start(self):
        with self._lock:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print("Process watcher started")

    def stop(self):
        with self._lock:
            self._running = False
        print("Process watcher stopped")

    @property
    def is_running(self):
        with self._lock:
            return self._running

    def _get_running_exes(self):
        """Get set of lowercase exe names currently running."""
        exes = set()
        for proc in psutil.process_iter(['name']):
            try:
                name = proc.info['name']
                if name:
                    exes.add(name.lower())
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return exes

    def _resolve_effect(self):
        """Determine which effect and polling rate should be active based on running processes.
        Returns (effect_name, polling_rate_hz, matched_exe) or (None, None, None)."""
        assoc = load_associations()
        if not assoc.get("enabled", True):
            return None, None, None

        programs = assoc.get("programs", {})
        polling_rates = assoc.get("polling_rates", {})
        default_effect = assoc.get("default_effect", "colorful_vh")
        default_rate = polling_rates.get("__default__", 1000)

        if not programs:
            return default_effect, default_rate, None

        running = self._get_running_exes()

        # Check if any associated program is running
        for exe_name, effect_name in programs.items():
            if exe_name.lower() in running:
                rate = polling_rates.get(exe_name.lower(), default_rate)
                return effect_name, rate, exe_name.lower()

        return default_effect, default_rate, None

    def _send_effect(self, effect_name, polling_rate_hz=None):
        """Send effect (and optionally polling rate) to keyboard with rate limiting."""
        now = time.time()
        if now - self._last_send_time < self.MIN_SEND_GAP:
            return
        if effect_name not in EFFECTS:
            return
        cfg = get_effect_config(effect_name)
        ok = send_to_keyboard(effect_name, cfg)
        if ok:
            self._current_effect = effect_name
            self._last_send_time = now
            display = EFFECT_DISPLAY_NAMES.get(effect_name, effect_name)
            msg = f"Watcher switched to: {display}"
            # Send polling rate if changed
            if polling_rate_hz and polling_rate_hz != self._current_rate:
                rate_ok = send_polling_rate(polling_rate_hz)
                if rate_ok:
                    self._current_rate = polling_rate_hz
                    msg += f" @ {polling_rate_hz}Hz"
            print(msg)
            if DEBUG_MODE:
                logger.info(msg)

    def _loop(self):
        while True:
            with self._lock:
                if not self._running:
                    break

            try:
                target, target_rate, target_exe = self._resolve_effect()
                if target is None:
                    time.sleep(self.POLL_INTERVAL)
                    continue

                now = time.time()

                if target != self._current_effect or target_rate != self._current_rate:
                    # New target detected — start debounce
                    if target != self._pending_effect or target_rate != self._pending_rate:
                        self._pending_effect = target
                        self._pending_rate = target_rate
                        self._pending_since = now
                    elif now - self._pending_since >= self.DEBOUNCE_TIME:
                        # Debounce passed — commit the switch
                        self._send_effect(target, target_rate)
                        self._current_profile = target_exe  # None = Default
                        self._pending_effect = None
                        self._pending_rate = None
                else:
                    # Already on the right effect + rate
                    self._current_profile = target_exe
                    self._pending_effect = None
                    self._pending_rate = None

            except Exception as e:
                print(f"Watcher error: {e}")

            time.sleep(self.POLL_INTERVAL)


watcher = ProcessWatcher()


# ─── Flask app ────────────────────────────────

app = Flask(__name__)


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "gui.html")


@app.route("/api/effects")
def api_effects():
    result = []
    for name, info in EFFECTS.items():
        result.append({
            "id": name,
            "display": EFFECT_DISPLAY_NAMES.get(name, name),
            "type": info["type"],
            "config": get_effect_config(name),
        })
    return jsonify(result)


@app.route("/api/effect/<name>/config")
def api_get_config(name):
    if name not in EFFECTS:
        return jsonify({"error": "Unknown effect"}), 404
    return jsonify(get_effect_config(name))


@app.route("/api/load", methods=["POST"])
def api_load():
    data = request.get_json()
    effect_name = data.get("effect")
    settings = data.get("settings", {})

    if effect_name not in EFFECTS:
        return jsonify({"error": "Unknown effect"}), 404

    # Save config
    set_effect_config(effect_name, settings)

    # Send to keyboard
    ok = send_to_keyboard(effect_name, settings)
    if ok:
        return jsonify({"status": "ok", "effect": effect_name})
    else:
        return jsonify({"error": "Could not find keyboard"}), 500


@app.route("/api/save", methods=["POST"])
def api_save():
    data = request.get_json()
    effect_name = data.get("effect")
    settings = data.get("settings", {})

    if effect_name not in EFFECTS:
        return jsonify({"error": "Unknown effect"}), 404

    set_effect_config(effect_name, settings)
    return jsonify({"status": "ok", "effect": effect_name})


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    os._exit(0)


@app.route("/api/associations", methods=["GET"])
def api_get_associations():
    return jsonify(load_associations())


@app.route("/api/associations", methods=["POST"])
def api_set_associations():
    data = request.get_json()
    assoc = load_associations()
    if "default_effect" in data:
        assoc["default_effect"] = data["default_effect"]
    if "default_exe" in data:
        assoc["default_exe"] = data["default_exe"]
    if "programs" in data:
        assoc["programs"] = data["programs"]
    if "polling_rates" in data:
        assoc["polling_rates"] = data["polling_rates"]
    if "enabled" in data:
        assoc["enabled"] = data["enabled"]
    if "program_names" in data:
        assoc["program_names"] = data["program_names"]
    save_associations(assoc)
    # Restart watcher if enabled changed
    if assoc.get("enabled", True) and not watcher.is_running:
        watcher.start()
    elif not assoc.get("enabled", True) and watcher.is_running:
        watcher.stop()
    return jsonify({"status": "ok"})


@app.route("/api/associations/add", methods=["POST"])
def api_add_program():
    data = request.get_json()
    exe = data.get("exe", "").strip()
    effect = data.get("effect", "")
    name = data.get("name", "").strip()
    if not exe or effect not in EFFECTS:
        return jsonify({"error": "Invalid exe or effect"}), 400
    # Store just the exe filename (lowercase)
    exe_name = os.path.basename(exe).lower()
    assoc = load_associations()
    assoc.setdefault("programs", {})[exe_name] = effect
    # Store display name (default to exe filename without extension)
    display_name = name if name else os.path.splitext(exe_name)[0]
    assoc.setdefault("program_names", {})[exe_name] = display_name
    save_associations(assoc)
    return jsonify({"status": "ok", "exe": exe_name, "effect": effect, "name": display_name})


@app.route("/api/associations/remove", methods=["POST"])
def api_remove_program():
    data = request.get_json()
    exe = data.get("exe", "").strip().lower()
    assoc = load_associations()
    assoc.get("programs", {}).pop(exe, None)
    assoc.get("polling_rates", {}).pop(exe, None)
    assoc.get("program_names", {}).pop(exe, None)
    save_associations(assoc)
    return jsonify({"status": "ok"})


@app.route("/api/polling-rates")
def api_polling_rates():
    return jsonify(POLLING_RATE_OPTIONS)


@app.route("/api/polling-rate", methods=["POST"])
def api_set_polling_rate():
    data = request.get_json()
    hz = data.get("hz")
    profile = data.get("profile", "__default__")  # "__default__" or exe name
    if hz not in POLLING_RATE_MAP:
        return jsonify({"error": f"Invalid polling rate: {hz}"}), 400
    # Save to associations
    assoc = load_associations()
    assoc.setdefault("polling_rates", {})[profile] = hz
    save_associations(assoc)
    # Send to keyboard immediately
    ok = send_polling_rate(hz)
    if ok:
        return jsonify({"status": "ok", "hz": hz})
    else:
        return jsonify({"error": "Could not find keyboard"}), 500


@app.route("/api/watcher/status")
def api_watcher_status():
    return jsonify({
        "running": watcher.is_running,
        "current_effect": watcher._current_effect,
        "current_profile": watcher._current_profile,
    })


@app.route("/api/connection")
def api_connection():
    """Report which keyboard connection is currently detected."""
    wired = any(
        '&mi_02' in d['path'].decode().lower()
        for d in hid.enumerate(VENDOR_ID, WIRED_PID)
    )
    wireless = any(
        '&mi_02' in d['path'].decode().lower()
        for d in hid.enumerate(VENDOR_ID, WIRELESS_PID)
    )
    if wired:
        mode = "wired"
    elif wireless:
        mode = "wireless"
    else:
        mode = "none"
    return jsonify({"mode": mode, "wired": wired, "wireless": wireless})


# ─── Tray icon ────────────────────────────────

def create_tray_image():
    img = Image.new("RGB", (64, 64), (30, 30, 30))
    draw = ImageDraw.Draw(img)
    draw.rectangle([8, 8, 24, 56], fill=(255, 0, 0))
    draw.rectangle([24, 8, 40, 56], fill=(0, 255, 0))
    draw.rectangle([40, 8, 56, 56], fill=(0, 0, 255))
    return img


def on_open_gui(icon, item):
    webbrowser.open(f"http://localhost:{PORT}")


def on_quit(icon, item):
    icon.stop()
    os._exit(0)


def on_toggle_watcher(icon, item):
    if watcher.is_running:
        watcher.stop()
        assoc = load_associations()
        assoc["enabled"] = False
        save_associations(assoc)
    else:
        watcher.start()
        assoc = load_associations()
        assoc["enabled"] = True
        save_associations(assoc)


def _get_active_profile_label():
    """Return a label like 'Active: DOOM' or 'Active: Default' for the tray menu."""
    if not watcher.is_running:
        return "Watcher disabled"
    exe = watcher._current_profile
    if exe is None:
        return "Active: Default"
    assoc = load_associations()
    name = assoc.get("program_names", {}).get(exe, exe)
    return f"Active: {name}"


def build_tray_menu():
    effect_items = []
    for name in EFFECTS:
        display = EFFECT_DISPLAY_NAMES.get(name, name)
        def make_callback(n):
            def cb(icon, item):
                cfg = get_effect_config(n)
                send_to_keyboard(n, cfg)
            return cb
        effect_items.append(pystray.MenuItem(display, make_callback(name)))

    polling_items = []
    for hz in POLLING_RATE_OPTIONS:
        def make_rate_callback(rate):
            def cb(icon, item):
                send_polling_rate(rate)
            return cb
        polling_items.append(pystray.MenuItem(f"{hz} Hz", make_rate_callback(hz)))

    return pystray.Menu(
        pystray.MenuItem("Open GUI", on_open_gui, default=True),
        pystray.MenuItem(
            lambda item: _get_active_profile_label(),
            None,
            enabled=False,
        ),
        pystray.MenuItem("Effects", pystray.Menu(*effect_items)),
        pystray.MenuItem("Polling Rate", pystray.Menu(*polling_items)),
        pystray.MenuItem(
            lambda item: "Disable Watcher" if watcher.is_running else "Enable Watcher",
            on_toggle_watcher,
        ),
        pystray.MenuItem("Quit", on_quit),
    )


def run_tray():
    icon = pystray.Icon(
        "RGB Keyboard",
        create_tray_image(),
        "L.A's MonsGeek M1 V5 TMR Light Manager",
        menu=build_tray_menu(),
    )
    icon.run()


# ─── Single instance (Windows named mutex) ───

def ensure_single_instance():
    """Prevent multiple instances using a Windows named mutex."""
    mutex_name = "Global\\LA_MonsGeek_LightManager"
    # CreateMutexW returns handle; GetLastError()==183 means already exists
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, True, mutex_name)
    last_error = kernel32.GetLastError()
    if last_error == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.user32.MessageBoxW(
            0,
            "L.A's MonsGeek Light Manager is already running.\n"
            "Check the system tray.",
            "Already Running",
            0x40  # MB_ICONINFORMATION
        )
        sys.exit(0)
    return handle  # must keep reference so mutex stays alive


# ─── Main ─────────────────────────────────────

def main():
    _mutex = ensure_single_instance()
    open_gui = "--gui" in sys.argv  # default: tray only; pass --gui to auto-open browser

    # Start Flask in a background thread
    flask_thread = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=PORT, use_reloader=False),
        daemon=True,
    )
    flask_thread.start()
    print(f"Server running at http://localhost:{PORT}")

    # Start process watcher if enabled
    assoc = load_associations()
    if assoc.get("enabled", True):
        watcher.start()

    # Auto-open browser only if --gui flag passed
    if open_gui:
        def open_browser():
            time.sleep(1.5)
            webbrowser.open(f"http://localhost:{PORT}")
        threading.Thread(target=open_browser, daemon=True).start()

    # Run tray icon on main thread
    run_tray()


if __name__ == "__main__":
    main()
