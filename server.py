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
    """Load the on-disk config. Robust against missing/corrupt/old-format files:
    * Missing file → empty dict.
    * Unreadable / invalid JSON → log, back up the bad file, return empty dict.
    * Non-dict top-level (e.g. an old list-shaped file) → same recovery path.
    """
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        msg = f"Config file unreadable ({e}); backing up and starting fresh."
        print(msg)
        if DEBUG_MODE:
            logger.warning(msg)
        try:
            backup = CONFIG_FILE + ".corrupt"
            os.replace(CONFIG_FILE, backup)
        except OSError:
            pass
        return {}
    if not isinstance(data, dict):
        msg = f"Config file has unexpected shape ({type(data).__name__}); ignoring."
        print(msg)
        if DEBUG_MODE:
            logger.warning(msg)
        return {}
    return data


def save_config(cfg):
    """Atomically write config to disk so a crash mid-write cannot corrupt it."""
    tmp_path = CONFIG_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp_path, CONFIG_FILE)


def get_effect_config(effect_name):
    cfg = load_config()
    etype = EFFECTS[effect_name]["type"]
    defaults = {"brightness": 4}
    if etype == "rgb":
        defaults.update({"speed": 0, "rgb": [255, 0, 0], "default_color": False})
    stored = cfg.get(effect_name)
    if not isinstance(stored, dict):
        return defaults
    # Merge so a partial/old entry still has every key the builders expect.
    merged = dict(defaults)
    merged.update(stored)
    return merged


def set_effect_config(effect_name, settings):
    cfg = load_config()
    cfg[effect_name] = settings
    save_config(cfg)


# ─── HID send ────────────────────────────────

def _try_send_hid(payload, vendor_id, product_id, effect_name, label):
    """Try to send payload to a keyboard matching vendor/product on interface 2.
    Returns True on success, False otherwise (also returns False if the
    device is found but cannot be opened, e.g. another process has it open)."""
    try:
        devices = list(hid.enumerate(vendor_id, product_id))
    except Exception as e:
        print(f"[hid] enumerate failed for {label}: {e}")
        return False
    for device_dict in devices:
        path = device_dict['path'].decode()
        if '&mi_02' in path.lower():
            dev = hid.device()
            try:
                dev.open_path(device_dict['path'])
                dev.send_feature_report([0x00] + payload)
                hex_str = ' '.join(f'{b:02x}' for b in payload[:9])
                msg = f"Sent {effect_name} via {label}: [{hex_str} ...]"
                print(msg)
                if DEBUG_MODE:
                    logger.info(msg)
                return True
            except Exception as e:
                # Most common reason: another process (or the keyboard itself
                # during firmware switchover) has the interface open. Log and
                # let the watcher retry on the next tick.
                msg = f"[hid] send to {label} failed for {effect_name}: {e}"
                print(msg)
                if DEBUG_MODE:
                    logger.warning(msg)
                return False
            finally:
                try:
                    dev.close()
                except Exception:
                    pass
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


def build_profile_switch_packet(profile):
    """Build a packet to switch keyboard profile. profile: 1, 2, or 3."""
    profile_index = profile - 1  # 0-based
    header = [0x04, profile_index, 0x00, 0x00, 0x00, 0x00, 0x00]
    return build_packet(header)


def send_profile_switch(profile):
    """Send a profile switch command to the keyboard (sent twice for reliability)."""
    if profile not in (1, 2):
        return False
    payload = build_profile_switch_packet(profile)
    label = f"profile_{profile}"
    ok = False
    if _try_send_hid(payload, VENDOR_ID, WIRED_PID, label, "wired"):
        ok = True
    elif _try_send_hid(payload, VENDOR_ID, WIRELESS_PID, label, "wireless"):
        ok = True
    if ok:
        # Send a second time after a short gap to ensure the keyboard registers it.
        time.sleep(0.05)
        _try_send_hid(payload, VENDOR_ID, WIRED_PID, label + "_retry", "wired") or \
            _try_send_hid(payload, VENDOR_ID, WIRELESS_PID, label + "_retry", "wireless")
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
      "polling_rates": { "__default__": 1000, "doom.exe": 8000, ... },
      "enabled": True, "notifications_enabled": True }

    Defensive: any missing or wrong-typed field is replaced with a safe
    default so older config files keep working after upgrades.
    """
    cfg = load_config()
    raw = cfg.get("_associations")
    if not isinstance(raw, dict):
        raw = {}

    def _as_dict(v):
        return v if isinstance(v, dict) else {}

    def _as_str(v, default):
        return v if isinstance(v, str) and v else default

    def _as_bool(v, default):
        return v if isinstance(v, bool) else default

    default_effect = _as_str(raw.get("default_effect"), "colorful_vh")
    # If the saved default_effect references an effect we no longer know
    # about, fall back to a safe built-in.
    if default_effect not in EFFECTS:
        default_effect = "colorful_vh"

    programs = {}
    for k, v in _as_dict(raw.get("programs")).items():
        if isinstance(k, str) and isinstance(v, str) and v in EFFECTS:
            programs[k.lower()] = v

    program_names = {}
    for k, v in _as_dict(raw.get("program_names")).items():
        if isinstance(k, str) and isinstance(v, str):
            program_names[k.lower()] = v

    polling_rates = {}
    for k, v in _as_dict(raw.get("polling_rates")).items():
        if isinstance(k, str) and isinstance(v, int) and v in POLLING_RATE_MAP:
            polling_rates[k if k == "__default__" else k.lower()] = v

    # Keyboard profiles (1 or 2) per program
    keyboard_profiles = {}
    for k, v in _as_dict(raw.get("keyboard_profiles")).items():
        if isinstance(k, str) and isinstance(v, int) and v in (1, 2):
            keyboard_profiles[k if k == "__default__" else k.lower()] = v

    def _as_int(v, default, lo=None, hi=None):
        if not isinstance(v, int) or isinstance(v, bool):
            return default
        if lo is not None and v < lo: return default
        if hi is not None and v > hi: return default
        return v

    return {
        "default_effect": default_effect,
        "default_exe": _as_str(raw.get("default_exe"), "explorer.exe"),
        "programs": programs,
        "program_names": program_names,
        "polling_rates": polling_rates,
        "keyboard_profiles": keyboard_profiles,
        "enabled": _as_bool(raw.get("enabled"), True),
        "notifications_enabled": _as_bool(raw.get("notifications_enabled"), True),
        "notification_delay_seconds": _as_int(raw.get("notification_delay_seconds"), 0, lo=0, hi=60),
    }


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
        self._current_kb_profile = None  # keyboard profile (1 or 2)
        self._current_profile = None     # exe name or None for Default
        self._last_send_time = 0         # rate limit
        self._pending_effect = None      # debounce: what we want to switch to
        self._pending_rate = None        # debounce: polling rate to switch to
        self._pending_since = 0          # debounce: when we first saw it
        self._force_resend = False       # external request to re-apply settings immediately
        self._switch_sequence = 0        # monotonically incremented on each switch event
        self._last_switch = None         # dict with details of the last switch event
        self._switch_callbacks = []      # list of callables invoked on every switch
        self.POLL_INTERVAL = 1.0         # how often to scan processes (seconds)
        self.DEBOUNCE_TIME = 0.3         # seconds before committing a switch
        self.MIN_SEND_GAP = 0.3          # minimum seconds between USB sends

    def add_switch_callback(self, cb):
        """Register a callback invoked as cb(switch_info_dict) on every switch."""
        self._switch_callbacks.append(cb)

    def request_resend(self):
        """Ask the watcher to re-apply its current target on the next tick.

        Used when settings/mappings change so the keyboard reflects them immediately
        without waiting for a process change.
        """
        with self._lock:
            self._force_resend = True

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
        """Determine which (effect, polling_rate, kb_profile, exe) should be active.

        Rules (simplified):
          * If any mapped program exe is running → use that profile.
          * Otherwise → use the default profile.
        The ``default_exe`` setting is informational only; it is NOT required
        to be running (explorer.exe is essentially always running, so using it
        as a trigger would be useless).

        Returns (effect_name, polling_rate_hz, kb_profile, matched_exe_or_None)
        when the watcher is enabled, else (None, None, None, None).
        """
        assoc = load_associations()
        if not assoc.get("enabled", True):
            return None, None, None, None

        programs = assoc.get("programs", {})
        polling_rates = assoc.get("polling_rates", {})
        keyboard_profiles = assoc.get("keyboard_profiles", {})
        default_effect = assoc.get("default_effect", "colorful_vh")
        default_rate = polling_rates.get("__default__", 1000)
        default_kb_profile = keyboard_profiles.get("__default__", 1)

        if programs:
            running = self._get_running_exes()
            for exe_name, effect_name in programs.items():
                exe_lc = exe_name.lower()
                if exe_lc in running:
                    rate = polling_rates.get(exe_lc, default_rate)
                    kb_prof = keyboard_profiles.get(exe_lc, default_kb_profile)
                    return effect_name, rate, kb_prof, exe_lc

        # Nothing mapped is running → default profile.
        return default_effect, default_rate, default_kb_profile, None

    def _send_effect(self, effect_name, polling_rate_hz=None, kb_profile=None, force=False):
        """Send effect (and optionally polling rate + keyboard profile) to keyboard.

        Returns True on a successful USB send, False otherwise (e.g. rate-limited
        or keyboard not found).
        """
        now = time.time()
        if not force and now - self._last_send_time < self.MIN_SEND_GAP:
            return False
        if effect_name not in EFFECTS:
            return False
        cfg = get_effect_config(effect_name)
        ok = send_to_keyboard(effect_name, cfg)
        if ok:
            self._current_effect = effect_name
            self._last_send_time = now
            display = EFFECT_DISPLAY_NAMES.get(effect_name, effect_name)
            msg = f"Watcher switched to: {display}"
            # Send keyboard profile if changed (or if forced)
            if kb_profile and (force or kb_profile != self._current_kb_profile):
                time.sleep(0.3)
                prof_ok = send_profile_switch(kb_profile)
                if prof_ok:
                    self._current_kb_profile = kb_profile
                    msg += f" [Profile {kb_profile}]"
            # Send polling rate if changed (or if forced), with a gap so
            # the keyboard firmware has time to process the effect packet.
            if polling_rate_hz and (force or polling_rate_hz != self._current_rate):
                time.sleep(0.5)
                rate_ok = send_polling_rate(polling_rate_hz)
                if rate_ok:
                    self._current_rate = polling_rate_hz
                    msg += f" @ {polling_rate_hz}Hz"
            print(msg)
            if DEBUG_MODE:
                logger.info(msg)
        return ok

    def _record_switch(self, effect_name, exe, polling_rate_hz):
        """Record a switch event so the GUI can surface a notification."""
        assoc = load_associations()
        if exe is None:
            profile_name = "Default"
        else:
            profile_name = assoc.get("program_names", {}).get(exe, exe)
        with self._lock:
            self._switch_sequence += 1
            info = {
                "sequence": self._switch_sequence,
                "effect": effect_name,
                "effect_display": EFFECT_DISPLAY_NAMES.get(effect_name, effect_name),
                "profile_exe": exe,
                "profile_name": profile_name,
                "polling_rate": polling_rate_hz,
                "timestamp": time.time(),
            }
            self._last_switch = info
            callbacks = list(self._switch_callbacks)
        for cb in callbacks:
            try:
                cb(info)
            except Exception as e:
                print(f"Switch callback error: {e}")

    def _loop(self):
        while True:
            with self._lock:
                if not self._running:
                    break
                force = self._force_resend
                self._force_resend = False

            try:
                target, target_rate, target_kb_prof, target_exe = self._resolve_effect()
                if target is None:
                    time.sleep(self.POLL_INTERVAL)
                    continue

                now = time.time()
                profile_changed = target_exe != self._current_profile

                if force:
                    # Settings or mapping changed externally — re-apply now,
                    # bypassing debounce and rate limit.
                    effect_changed = target != self._current_effect
                    print(f"[watcher] force-resend target={target} exe={target_exe} rate={target_rate} kb_prof={target_kb_prof}")
                    if self._send_effect(target, target_rate, target_kb_prof, force=True):
                        self._current_profile = target_exe
                        self._pending_effect = None
                        self._pending_rate = None
                        # Only surface a notification if the active profile
                        # or effect actually changed; silent re-apply of the
                        # same profile (e.g. brightness tweak) shouldn't spam.
                        if profile_changed or effect_changed:
                            self._record_switch(target, target_exe, target_rate)
                elif (profile_changed or target != self._current_effect
                      or target_rate != self._current_rate
                      or target_kb_prof != self._current_kb_profile):
                    # New target detected — start debounce
                    if target != self._pending_effect or target_rate != self._pending_rate:
                        self._pending_effect = target
                        self._pending_rate = target_rate
                        self._pending_since = now
                        print(f"[watcher] pending switch: cur={self._current_effect}/{self._current_profile} -> {target}/{target_exe} @ {target_rate}Hz prof={target_kb_prof} (debouncing)")
                    elif now - self._pending_since >= self.DEBOUNCE_TIME:
                        # Debounce passed — commit the switch
                        if self._send_effect(target, target_rate, target_kb_prof):
                            self._current_profile = target_exe  # None = Default
                            self._pending_effect = None
                            self._pending_rate = None
                            self._record_switch(target, target_exe, target_rate)
                        else:
                            # If send failed (rate-limited / no keyboard), keep
                            # pending state so we retry on the next tick.
                            print(f"[watcher] send failed for {target}; will retry")
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
        # If the watcher is currently driving this effect, keep its cache in sync.
        if watcher.is_running and watcher._current_effect == effect_name:
            watcher._last_send_time = time.time()
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
    # If the watcher's current effect is the one we just edited, re-apply it
    # to the keyboard so the change is visible immediately.
    if watcher.is_running and watcher._current_effect == effect_name:
        watcher.request_resend()
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
    mapping_changed = False
    if "default_effect" in data:
        if assoc.get("default_effect") != data["default_effect"]:
            mapping_changed = True
        assoc["default_effect"] = data["default_effect"]
    if "default_exe" in data:
        assoc["default_exe"] = data["default_exe"]
    if "programs" in data:
        if assoc.get("programs") != data["programs"]:
            mapping_changed = True
        assoc["programs"] = data["programs"]
    if "polling_rates" in data:
        if assoc.get("polling_rates") != data["polling_rates"]:
            mapping_changed = True
        assoc["polling_rates"] = data["polling_rates"]
    if "keyboard_profiles" in data:
        if assoc.get("keyboard_profiles") != data["keyboard_profiles"]:
            mapping_changed = True
        assoc["keyboard_profiles"] = data["keyboard_profiles"]
    if "enabled" in data:
        assoc["enabled"] = data["enabled"]
    if "program_names" in data:
        assoc["program_names"] = data["program_names"]
    if "notifications_enabled" in data:
        assoc["notifications_enabled"] = bool(data["notifications_enabled"])
    if "notification_delay_seconds" in data:
        try:
            delay = int(data["notification_delay_seconds"])
        except (TypeError, ValueError):
            delay = 0
        assoc["notification_delay_seconds"] = max(0, min(60, delay))
    save_associations(assoc)
    # Restart watcher if enabled changed
    if assoc.get("enabled", True) and not watcher.is_running:
        watcher.start()
    elif not assoc.get("enabled", True) and watcher.is_running:
        watcher.stop()
    # If mappings changed and watcher is running, re-evaluate immediately.
    if mapping_changed and watcher.is_running:
        watcher.request_resend()
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
    previous_effect = assoc.get("programs", {}).get(exe_name)
    assoc.setdefault("programs", {})[exe_name] = effect
    # Store display name (default to exe filename without extension)
    display_name = name if name else os.path.splitext(exe_name)[0]
    assoc.setdefault("program_names", {})[exe_name] = display_name
    save_associations(assoc)
    # If the watcher is currently showing this program's profile (or might
    # need to pick it up), ask it to re-evaluate now.
    if watcher.is_running and previous_effect != effect:
        watcher.request_resend()
    return jsonify({"status": "ok", "exe": exe_name, "effect": effect, "name": display_name})


@app.route("/api/associations/remove", methods=["POST"])
def api_remove_program():
    data = request.get_json()
    exe = data.get("exe", "").strip().lower()
    assoc = load_associations()
    existed = exe in assoc.get("programs", {})
    assoc.get("programs", {}).pop(exe, None)
    assoc.get("polling_rates", {}).pop(exe, None)
    assoc.get("program_names", {}).pop(exe, None)
    assoc.get("keyboard_profiles", {}).pop(exe, None)
    save_associations(assoc)
    if existed and watcher.is_running:
        watcher.request_resend()
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
        # Keep watcher's cached rate in sync so it doesn't immediately re-send.
        if watcher.is_running:
            active_profile = watcher._current_profile or "__default__"
            if active_profile == profile:
                watcher._current_rate = hz
        return jsonify({"status": "ok", "hz": hz})
    else:
        return jsonify({"error": "Could not find keyboard"}), 500


@app.route("/api/keyboard-profile", methods=["POST"])
def api_set_keyboard_profile():
    data = request.get_json()
    kb_profile = data.get("keyboard_profile")
    profile = data.get("profile", "__default__")  # "__default__" or exe name
    if kb_profile not in (1, 2):
        return jsonify({"error": f"Invalid keyboard profile: {kb_profile}"}), 400
    # Save to associations
    assoc = load_associations()
    assoc.setdefault("keyboard_profiles", {})[profile] = kb_profile
    save_associations(assoc)
    # Send to keyboard immediately
    ok = send_profile_switch(kb_profile)
    if ok:
        if watcher.is_running:
            active_profile = watcher._current_profile or "__default__"
            if active_profile == profile:
                watcher._current_kb_profile = kb_profile
        return jsonify({"status": "ok", "keyboard_profile": kb_profile})
    else:
        return jsonify({"error": "Could not find keyboard"}), 500


@app.route("/api/watcher/status")
def api_watcher_status():
    return jsonify({
        "running": watcher.is_running,
        "current_effect": watcher._current_effect,
        "current_profile": watcher._current_profile,
        "last_switch": watcher._last_switch,
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


# Module-level reference so callbacks can fire native notifications.
_tray_icon = None
_notify_timer = None  # threading.Timer for delayed tray notifications


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


def on_force_refresh(icon, item):
    """Tray action: re-scan processes and re-apply the active profile now."""
    if watcher.is_running:
        watcher.request_resend()
        print("[tray] Force refresh requested")
    else:
        print("[tray] Force refresh ignored — watcher is disabled")


def on_toggle_notifications(icon, item):
    assoc = load_associations()
    assoc["notifications_enabled"] = not assoc.get("notifications_enabled", True)
    save_associations(assoc)


def _notifications_enabled():
    return load_associations().get("notifications_enabled", True)


def _fire_tray_notification(info):
    """Actually push the balloon to the tray (called possibly after a delay)."""
    if not _notifications_enabled() or _tray_icon is None:
        return
    profile = info.get("profile_name") or "Default"
    effect  = info.get("effect_display") or info.get("effect") or ""
    rate    = info.get("polling_rate")
    kb_prof = watcher._current_kb_profile
    body = f"Now active: {profile}\n{effect}"
    if rate:
        body += f" @ {rate}Hz"
    if kb_prof:
        body += f" · P{kb_prof}"
    try:
        _tray_icon.notify(body, "Profile switched")
    except Exception as e:
        print(f"Tray notify failed: {e}")


def _on_profile_switched(info):
    """Switch-callback: schedule a tray balloon, honoring the configured delay.

    If another switch happens while a previous notification is still pending,
    the pending one is cancelled so the user only ever sees the latest profile.
    """
    global _notify_timer
    # Force the tray menu to re-evaluate dynamic labels (e.g. "Active: ...")
    if _tray_icon is not None:
        try:
            _tray_icon.update_menu()
        except Exception:
            pass
    if not _notifications_enabled() or _tray_icon is None:
        return
    # Cancel any in-flight scheduled notification.
    if _notify_timer is not None:
        try:
            _notify_timer.cancel()
        except Exception:
            pass
        _notify_timer = None

    delay = load_associations().get("notification_delay_seconds", 0)
    try:
        delay = max(0, int(delay))
    except (TypeError, ValueError):
        delay = 0

    if delay <= 0:
        _fire_tray_notification(info)
        return

    _notify_timer = threading.Timer(delay, _fire_tray_notification, args=(info,))
    _notify_timer.daemon = True
    _notify_timer.start()


def on_test_notification(icon, item):
    """Tray menu action: fire a sample notification so the user can verify
    that Windows toast notifications are reaching them."""
    try:
        icon.notify("If you can see this, notifications are working.",
                    "Light Manager test")
    except Exception as e:
        print(f"Test notify failed: {e}")


def _get_active_profile_label():
    """Return a label like 'Active: DOOM · 1000Hz · P1' for the tray menu."""
    if not watcher.is_running:
        return "Watcher disabled"
    exe = watcher._current_profile
    rate = watcher._current_rate
    kb_prof = watcher._current_kb_profile
    if exe is None:
        name = "Default"
    else:
        assoc = load_associations()
        name = assoc.get("program_names", {}).get(exe, exe)
    parts = [f"Active: {name}"]
    if rate:
        parts.append(f"{rate}Hz")
    if kb_prof:
        parts.append(f"P{kb_prof}")
    return " \u00B7 ".join(parts)


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

    profile_items = []
    for p in (1, 2):
        def make_profile_callback(prof):
            def cb(icon, item):
                send_profile_switch(prof)
            return cb
        profile_items.append(pystray.MenuItem(f"Profile {p}", make_profile_callback(p)))

    return pystray.Menu(
        pystray.MenuItem("Open GUI", on_open_gui, default=True),
        pystray.MenuItem(
            lambda item: _get_active_profile_label(),
            None,
            enabled=False,
        ),
        pystray.MenuItem("Effects", pystray.Menu(*effect_items)),
        pystray.MenuItem("Polling Rate", pystray.Menu(*polling_items)),
        pystray.MenuItem("Switch Profile", pystray.Menu(*profile_items)),
        pystray.MenuItem("Force Refresh", on_force_refresh),
        pystray.MenuItem(
            lambda item: "Disable Watcher" if watcher.is_running else "Enable Watcher",
            on_toggle_watcher,
        ),
        pystray.MenuItem(
            lambda item: ("\u2713 Notifications" if _notifications_enabled()
                          else "  Notifications"),
            on_toggle_notifications,
        ),
        pystray.MenuItem("Quit", on_quit),
    )


def run_tray():
    global _tray_icon
    _tray_icon = pystray.Icon(
        "RGB Keyboard",
        create_tray_image(),
        "L.A's MonsGeek M1 V5 TMR Light Manager",
        menu=build_tray_menu(),
    )
    # Register the switch callback so we get a balloon on every profile change.
    watcher.add_switch_callback(_on_profile_switched)
    _tray_icon.run()


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
