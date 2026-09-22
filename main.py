import sys as _sys
from pathlib import Path as _Path

if _sys.stdout is None or _sys.stderr is None:
    # No console attached (pythonw.exe, a --windowed PyInstaller build, or
    # similar) -- stdout/stderr are None in that case, and there are
    # dozens of print() calls across the codebase (self_maintain's
    # [SelfMaintain] logs, tool activity, etc.) that would raise
    # AttributeError the instant one fires. Redirect both to a log file
    # instead so nothing crashes and everything's still inspectable.
    _log_dir = _Path(__file__).resolve().parent / "logs"
    _log_dir.mkdir(parents=True, exist_ok=True)
    _log_file = open(_log_dir / "console.log", "a", encoding="utf-8", buffering=1)
    _sys.stdout = _log_file
    _sys.stderr = _log_file
    from datetime import datetime as _datetime
    print(f"\n{'=' * 60}\nLITE starting (no console attached) — {_datetime.now()}\n{'=' * 60}")

# Force UTF-8 on stdout/stderr in every launch mode. pythonw already gets a
# UTF-8 log file above; but when LITE runs with a console or is piped, the
# default encoding is the machine's legacy codepage (cp1252 on most Western
# installs), and the codebase prints emoji/status glyphs everywhere — one
# unencodable character inside an error handler would itself crash the task
# handling the error (seen as UnicodeEncodeError under unittest's pipe).
# errors="replace" guarantees a print can never take down a thread.
for _stream in (_sys.stdout, _sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass  # exotic stream without reconfigure — best-effort only

# ── Single-instance guard ─────────────────────────────────────────────────
# Prevents a second launch (e.g. an accidental double-click on the icon)
# from opening a duplicate UI. Runs before the heavy imports below so a
# duplicate launch exits almost instantly instead of loading Gemini/audio/
# agent modules first.
#
# Windows: the authoritative check is a named mutex created via
# kernel32.CreateMutexW — the canonical single-instance mechanism there,
# immune to port re-use quirks. (This guard used to be socket-only and set
# SO_REUSEADDR before bind — but on Windows that flag lets a second socket
# bind the same port that is already listening, so the duplicate launch
# silently succeeded and two full LITE instances ran at once, fighting over
# the microphone and dashboard. SO_REUSEADDR is gone; the mutex decides.)
# The loopback TCP socket below is now only the "raise my window" channel:
# a duplicate pings it so the running instance pops to the foreground. If
# some unrelated app happens to own that port, LITE still starts (the mutex
# is authoritative) — later clicks just can't raise the window.
import os as _os
import socket as _socket

_SINGLE_INSTANCE_PORT = 51477  # arbitrary, LITE-specific, loopback-only
_single_instance_socket = None
_single_instance_mutex  = None
_SINGLE_INSTANCE_MUTEX_NAME = "Local\\LITE.SingleInstance.Mutex"

def _ping_raising_instance() -> None:
    """Best-effort: tell the running instance to raise its window."""
    try:
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(("127.0.0.1", _SINGLE_INSTANCE_PORT))
            client.sendall(b"raise")
    except OSError:
        pass  # stale/unreachable owner — nothing more we can do

def _try_acquire_windows_mutex() -> bool:
    """Windows: create/own LITE's named mutex. Returns False — without
    keeping ownership — if another LITE instance already owns it."""
    global _single_instance_mutex
    import ctypes as _ctypes
    try:
        kernel32     = _ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = [_ctypes.c_void_p, _ctypes.c_int, _ctypes.c_wchar_p]
        create_mutex.restype  = _ctypes.c_void_p
        close_handle          = kernel32.CloseHandle
        close_handle.argtypes = [_ctypes.c_void_p]
        close_handle.restype  = _ctypes.c_int
    except Exception:
        # ctypes config failed (never expected on Windows) — don't block
        # startup; the socket path below still provides a best-effort check.
        return True
    handle = create_mutex(None, 1, _SINGLE_INSTANCE_MUTEX_NAME)
    if not handle:
        # Couldn't create (permissions?) — don't block startup.
        return True
    if _ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        close_handle(handle)  # someone else owns it — give our handle back
        return False
    # Owned for the process lifetime; the OS releases it automatically on
    # exit or crash (no stale-lock cleanup needed).
    _single_instance_mutex = handle
    return True

def _acquire_single_instance_lock() -> bool:
    """Returns True if this is the only running instance. If another
    instance already holds the lock, pings it (so it can raise its
    window) and returns False so the caller can exit immediately."""
    global _single_instance_socket
    if _os.environ.get("LITE_ALLOW_MULTI_INSTANCE"):
        return True  # escape hatch for tests / deliberate parallel runs

    # 1) Windows mutex — the authoritative duplicate check.
    if _sys.platform == "win32" and not _try_acquire_windows_mutex():
        _ping_raising_instance()
        return False

    # 2) Loopback socket — the "raise window" channel (and, on non-Windows
    #    systems, the single-instance lock itself).
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        # Deliberately NO SO_REUSEADDR: on Windows it would let a second
        # socket steal this port from the running instance (the bug that
        # let duplicates launch). A plain bind fails if the port is held.
        s.bind(("127.0.0.1", _SINGLE_INSTANCE_PORT))
        s.listen(1)
    except OSError:
        s.close()
        if _sys.platform == "win32":
            # The mutex says we're first, so the port is held by an
            # unrelated app (or TIME_WAIT leftovers from a previous crash).
            # Start anyway without the raise channel — duplicates still
            # can't launch because the mutex blocks them.
            _single_instance_socket = None
            return True
        # Non-Windows: the socket WAS the lock.
        _ping_raising_instance()
        return False

    _single_instance_socket = s  # kept open for the process lifetime
    return True

def _release_single_instance_lock() -> None:
    """Drops both lock primitives. Only needed by tests — normal exits rely
    on the OS releasing the socket and mutex handle automatically."""
    global _single_instance_socket, _single_instance_mutex
    if _single_instance_socket is not None:
        try:
            _single_instance_socket.close()
        except OSError:
            pass
        _single_instance_socket = None
    if _single_instance_mutex is not None and _sys.platform == "win32":
        try:
            import ctypes as _ctypes
            _ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(
                _single_instance_mutex
            )
        except Exception:
            pass
        _single_instance_mutex = None

if not _acquire_single_instance_lock():
    print("[LITE] Another instance is already running — raising it and exiting.")
    _sys.exit(0)
# ─────────────────────────────────────────────────────────────────────────

import platform as _platform
import subprocess as _subprocess

# ── Nuclear: force CREATE_NO_WINDOW on EVERY subprocess call on Windows ───────
# This patches Popen itself, so no per-file flag is needed anywhere.
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)   # drop any stale/shared STARTUPINFO
            super().__init__(args, **kw)

    _subprocess.Popen = _Popen
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import base64
import re
import threading
import time
import json
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import sounddevice as sd
import numpy as np
from google import genai
from google.genai import types
from ui import LiteUI
from core.audio_devices import (
    input_channel_index,
    input_device_name,
    output_device_name,
    resample_audio,
    resolve_input_stream,
    resolve_output_device,
)
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
    save_session_summary, pop_last_session,
)

from actions.file_processor import file_processor
from actions.flight_finder     import flight_finder
from actions.open_app          import open_app
from actions.weather_report    import weather_action
from actions.weather_widget    import weather_widget
from actions.send_message      import send_message
from actions.reminder          import reminder
from actions.computer_settings import computer_settings
from actions.screen_processor  import _capture_camera, _capture_screen
from actions.youtube_video     import youtube_video
from actions.desktop           import desktop_control
from actions.browser_control   import browser_control
from actions.file_controller   import file_controller
from actions.code_agent         import code_agent
from agents.rics_agent import rics_agent
from agents.data_analytics_agent import data_analytics_agent
from agents.fta_agent import fta_agent
from agents.compliance_legal_agent import compliance_legal_agent
from agents.scheduling_docs_agent import scheduling_docs_agent
from agents.opportunity_pipeline import opportunity_pipeline_agent, get_cached_or_refresh as _get_opportunity_or_refresh
from actions.self_update        import self_update
from actions.affiliate_growth_agent import affiliate_growth_agent
from actions.web_search        import web_search as web_search_action
from actions.web_media_fetch   import web_media_fetch

# Executive Directory — maps each delegated tool to the roster member who
# actually owns it, so the HUD's Active Operative badge can show who is
# really doing the work instead of a generic "LITE is thinking". Roster
# ids/photos/bios live in hologram/index.html's AGENT_ROSTER (JS side) —
# this dict is the Python-side half of that same mapping and must use the
# same ids. Any tool NOT listed here is LITE's own direct action and stays
# on "lite". Kept as one dict rather than scattered per-branch so adding a
# new specialist agent later is a one-line change, not a find-and-replace.
AGENT_FOR_TOOL = {
    "code_agent":                "mike",     # Mike   — Code Agent (Programmer & CTO)
    "self_update":                "mike",     # Mike   — self-maintenance is CTO territory too
    "rics_agent":                 "ava",      # Ava    — RICS (Research, Intelligence & CRM/Sales)
    "data_analytics_agent":       "chidinma", # Chidinma — Data Analytics Agent
    "fta_agent":                  "elias",    # Elias  — CFO (Finance, FP&A & Treasury)
    "compliance_legal_agent":     "wale",     # Wale   — Compliance & Legal Agent
    "scheduling_docs_agent":      "adeola",   # Adeola — Scheduling, Document & Presentation Agent
    "affiliate_growth_agent":     "priya",    # Priya  — Affiliate Growth / GAS
    "opportunity_pipeline_agent": "nova",     # Nova   — Opportunity Pipeline Director
}

EXECUTIVE_DIRECTORY_CONTEXT = (
    "[EXECUTIVE DIRECTORY]\n"
    "LITE is the Executive Orchestrator and Chief Operating Officer. "
    "Every specialist below reports to LITE and owns a distinct area of the business. "
    "When the user asks who is handling or assigned to a task, answer with the correct personality and role.\n\n"
    "- LITE: Executive Orchestrator / COO — runs the whole operation, delegates work, and owns the final handoff back to the user.\n"
    "- Mike: Code Agent / Programmer & CTO — responsible for coding work, project fixes, automation, and LITE's self-update pipeline.\n"
    "- Ava: RICS Agent / Research, Intelligence & CRM/Sales — handles research, CRM, pipeline work, and outbound sales intelligence.\n"
    "- Chidinma: Data Analytics Agent — data analysis, metrics, and business KPI interpretation.\n"
    "- Elias: FTA Agent / CFO — financial analysis, FP&A, treasury logic, ratios, and investment appraisal.\n"
    "- Wale: Compliance & Legal Agent — contracts, compliance, regulatory review, and legal analysis.\n"
    "- Adeola: Scheduling, Document & Presentation Agent — scheduling, doc generation, presentations, and work product creation.\n"
    "- Priya: Affiliate Growth Agent / GAS — affiliate marketing strategy, content, audience research, and growth operations.\n"
    "- Nova: Opportunity Pipeline Director — opportunity validation, pipeline prioritization, and deal flow management.\n\n"
    "Use this roster precisely when referring to the executive directory, the active operative, or who is handling a delegated task.\n\n"
)
from actions.computer_control  import computer_control
from actions.game_updater      import game_updater
from actions.obsidian          import obsidian_notes
from actions.system_monitor    import SystemMonitor, get_system_status
from actions.proactive         import ProactiveEngine
from actions.background_monitor import (
    add_monitor, remove_monitor, list_monitors, check_all as monitor_check_all,
)
from memory.config_manager     import get_brief_enabled


def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"
LIVE_MODEL          = "models/gemini-2.5-flash-native-audio-preview-12-2025"
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000
RECEIVE_SAMPLE_RATE = 24000
# 2048 frames @ 16 kHz = 128 ms per mic chunk. The Live API expects ~100 ms
# audio frames; going smaller (e.g. 512/1024) doesn't reduce perceived
# latency — it just adds more Python↔PortAudio round-trips and more
# call_soon_threadsafe hops per second, all of which add *to* latency.
# 2048 is the sweet spot for first-byte response on a real-time audio stream.
CHUNK_SIZE          = 2048
INPUT_AUDIO_MIME    = "audio/pcm;rate=16000"
# Speaker echo guard: after LITE stops speaking, keep ignoring mic input for
# this long. Two delays stack after the last chunk is *handed to* the sound
# system: the output buffer itself still has to drain (latency="high" keeps
# a few hundred ms queued) and the room then needs time to decay — so the
# tail must be anchored well past the moment playback "ends" on the software
# side. The old 0.35 s tail let LITE's final words leak back into the mic,
# Gemini's VAD heard them as "the user", and LITE started responding to its
# own voice. Barge-in is unaffected — the stop button / global hotkey /
# remote all go through interrupt(), which reopens the mic instantly.
ECHO_GUARD_S        = 1.0

def _get_api_key() -> str:
    """
    Returns the configured Gemini key, or "" if none is set.
    Never raises — a missing/unset key is a normal, supported state
    (voice conversation is simply unavailable until one is added).
    """
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            return (json.load(f).get("gemini_api_key") or "").strip()
    except Exception:
        return ""


def _echo_cancellation_enabled() -> bool:
    """Whether the mic should stay open while LITE speaks using real echo
    cancellation (voice barge-in). OFF by default: how badly LITE's own voice
    leaks into the mic depends entirely on the room, speaker volume and the
    mic array — and on hardware where the canceller under-performs the leak,
    the leaked echo is worse than the bug it fixes (LITE hears itself and
    keeps interrupting). Set "echo_cancellation": true in api_keys.json to
    opt in."""
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            return bool(json.load(f).get("echo_cancellation", False))
    except Exception:
        return False


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are LITE, Tony Stark's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

def _clean_transcript(text: str) -> str:    
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()


# ── Language watchdog ─────────────────────────────────────────────────────
# System-prompt instructions alone aren't fully reliable for native-audio
# Gemini Live models — Google's own docs note these models "automatically
# choose the appropriate language" and can drift mid-conversation regardless
# of instructions. This is a code-level backstop: it actually inspects what
# the model said (not what we told it to do) and self-corrects on the spot
# if it drifted into any non-English language. Language switching itself is
# disabled entirely (see identity_ctx's LANGUAGE rule) — there is no
# authorized-language exception anymore, so this fires on any drift, always.

_FOREIGN_SCRIPT_RE = re.compile(
    r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7a3\u0400-\u04ff\u0600-\u06ff\u0590-\u05ff\u0e00-\u0e7f]"
)

def _foreign_script_ratio(text: str) -> float:
    """Fraction of non-space characters that fall in a non-Latin script block
    (CJK, Hiragana/Katakana, Hangul, Cyrillic, Arabic, Hebrew, Thai). English
    text scores ~0.0; a reply that's switched to Chinese/Japanese/Korean/etc.
    scores high regardless of exact language."""
    stripped = text.replace(" ", "")
    if not stripped:
        return 0.0
    foreign = len(_FOREIGN_SCRIPT_RE.findall(stripped))
    return foreign / len(stripped)

TOOL_DECLARATIONS = [
    {
        "name": "open_app",
        "description": (
            "Opens any application on the computer. "
            "Use this whenever the user asks to open, launch, or start any app, "
            "website, or program. Always call this tool — never just say you opened it."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {
                    "type": "STRING",
                    "description": "Exact name of the application (e.g. 'WhatsApp', 'Chrome', 'Spotify')"
                }
            },
            "required": ["app_name"]
        }
    },
    {
        "name": "web_media_fetch",
        "description": (
            "Fetches an ACTUAL image (downloads it) or finds a playable video from the web "
            "and displays it directly on LITE's HUD panel — this NEVER opens a browser window, "
            "Chrome, or any other app; the image/video appears right on LITE's own screen. Use "
            "this whenever the user wants to actually SEE a picture or video ('show me a picture of "
            "X', 'find a video of Y', 'download an image of Z and display it') — NEVER "
            "web_search (text-only, describes the topic instead of showing it) or browser_control "
            "(opens an actual browser window, which is not what 'display it' means here) for this. "
            "Images are downloaded and shown directly; "
            "videos are embedded (platform-hosted videos aren't raw downloadable files, so "
            "these play via embed rather than a literal download — still opens right on the "
            "HUD, not a browser)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "What to find, in the user's words, e.g. 'a red Ferrari' or 'the Eiffel Tower at night'. Leave empty if a direct url is given instead."},
                "url":   {"type": "STRING", "description": "A specific image or video URL the user already gave, if any. Leave empty to search instead."},
                "kind":  {"type": "STRING", "description": "image (default) | video"},
            },
            "required": []
        }
    },
    {
        "name": "web_search",
        "description": (
            "Searches the web. Use for ANY question about current facts, events, prices, "
            "or topics — always prefer this over guessing. NOT for 'show me a picture/video "
            "of X' — that's web_media_fetch, which actually displays the image/video rather "
            "than describing it in text. "
            "Modes: 'opportunities' (business/income ideas, side hustles, international/remote "
            "gigs, arbitrage — LITE's primary mode, use this whenever the user asks what's out "
            "there or how to make money), 'search' (default general lookup), "
            "'news' (latest headlines on a topic), "
            "'research' (deep comprehensive answer), 'price' (product cost lookup), "
            "'compare' (side-by-side comparison of items)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query":  {"type": "STRING", "description": "Search query or topic"},
                "mode":   {"type": "STRING", "description": "opportunities | search | news | research | price | compare"},
                "items":  {"type": "ARRAY",  "items": {"type": "STRING"}, "description": "Items to compare (compare mode)"},
                "aspect": {"type": "STRING", "description": "Comparison aspect: price | specs | reviews | features"},
            },
            "required": ["query"]
        }
    },
    {
        "name": "system_status",
        "description": (
            "Returns real-time system metrics: CPU usage, RAM, GPU load, CPU temperature, "
            "uptime, and process count. Use when the user asks about computer performance, "
            "temperature, memory, or resource usage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "weather_report",
        "description": "Gives the weather report to user",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "city": {"type": "STRING", "description": "City name"}
            },
            "required": ["city"]
        }
    },
    {
        "name": "weather_widget",
        "description": (
            "Updates or collapses the weather section that's permanently built "
            "into LITE's own sidebar interface (current conditions + 5-day "
            "forecast for a city) — it sits alongside the activity log and file "
            "upload sections, not a separate popup. Use this whenever the user "
            "asks to see the weather for a different city on screen, or to "
            "collapse/hide that section. weather_report is different — it just "
            "speaks/opens a browser search and shows nothing persistent."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "'show' (default) to open the widget, or 'hide' to close it"},
                "city":   {"type": "STRING", "description": "City to display when showing. Optional — if omitted, reopens showing whatever city was last used."}
            },
            "required": []
        }
    },
    {
        "name": "ui_control",
        "description": (
            "Operates LITE's OWN interface directly — scrolling the content panel in any "
            "direction (up/down/left/right), zooming the whole HUD in/out, clicking LITE's "
            "own known controls (mute, interrupt, settings, the executive directory, and the "
            "content panel's prev/next/minimize/close — 'next'/'previous' are the button at "
            "the top-right corner of the content panel for switching between what's been "
            "shown), and controlling whatever audio or video is currently displayed (a GAS "
            "draft's narration, a video preview) with play/pause/stop/restart. For 'click' "
            "targets NOT in that known list, it searches every currently-relevant panel — the "
            "content panel, the setup panel's sub-menus and toggles (including the fullscreen "
            "toggle), and the executive directory — for a text/description match, and "
            "honestly reports if nothing matched, so always relay that back rather than "
            "assuming success. This is NOT for web pages or other apps — that's "
            "browser_control (real websites) or computer_settings' screen_click (any other "
            "app/window on screen). Use this whenever the user wants LITE to interact with "
            "its own HUD, its setup panel, or something it's currently showing on screen."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "scroll | zoom | click | media"},
                "direction":   {"type": "STRING", "description": "For scroll: up | down | left | right (default down)."},
                "amount":      {"type": "INTEGER", "description": "For scroll: pixels to scroll (default 400)."},
                "zoom_action": {"type": "STRING", "description": "For zoom: in | out | reset."},
                "target":      {"type": "STRING", "description": "For click: mute | interrupt | settings | directory | close directory | next | previous | minimize | close panel, OR a description of anything else currently visible (including setup sub-menu items and toggles like 'fullscreen') to click."},
                "media_action": {"type": "STRING", "description": "For media: play | pause | stop | restart."},
            },
            "required": ["action"]
        }
    },
    {
        "name": "show_executive_directory",
        "description": (
            "Opens LITE's Executive Directory overlay so the user can see the full "
            "roster of specialist agents and the current active operative. Use this "
            "when the user asks to show, open, reveal, or view the executive directory, "
            "org chart, or roster cards on screen."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "hide_executive_directory",
        "description": (
            "Closes LITE's Executive Directory overlay. Use this when the user says "
            "close, hide, dismiss, or exit the executive directory."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "send_message",
        "description": "Sends a text message via WhatsApp, Telegram, or other messaging platform.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "receiver":     {"type": "STRING", "description": "Recipient contact name"},
                "message_text": {"type": "STRING", "description": "The message to send"},
                "platform":     {"type": "STRING", "description": "Platform: WhatsApp, Telegram, etc."}
            },
            "required": ["receiver", "message_text", "platform"]
        }
    },
    {
        "name": "reminder",
        "description": "Sets a timed reminder using Task Scheduler.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "date":    {"type": "STRING", "description": "Date in YYYY-MM-DD format"},
                "time":    {"type": "STRING", "description": "Time in HH:MM format (24h)"},
                "message": {"type": "STRING", "description": "Reminder message text"}
            },
            "required": ["date", "time", "message"]
        }
    },
    {
        "name": "youtube_video",
        "description": (
            "Controls YouTube. Use for: playing videos, summarizing a video's content, "
            "getting video info, or showing trending videos."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "play | summarize | get_info | trending (default: play)"},
                "query":  {"type": "STRING", "description": "Search query for play action"},
                "save":   {"type": "BOOLEAN", "description": "Save summary to Notepad (summarize only)"},
                "region": {"type": "STRING", "description": "Country code for trending e.g. TR, US"},
                "url":    {"type": "STRING", "description": "Video URL for get_info action"},
            },
            "required": []
        }
    },
    {
        "name": "screen_process",
        "description": (
            "Captures the screen or webcam image and lets you analyze it. "
            "MUST be called when user asks what is on screen, what you see, "
            "look at camera, analyze my screen, etc. "
            "You have NO visual ability without this tool. "
            "After the image is captured it is sent directly to you — describe what you see and answer the user's question. "
            "When using camera: the live view stays open until user says close it or calls close_camera."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "'screen' to capture display, 'camera' for webcam. Default: 'screen'"},
                "text":  {"type": "STRING", "description": "The question or instruction about the captured image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "close_camera",
        "description": (
            "Closes the live camera view shown on screen. "
            "Call when user says: close camera, stop camera, turn off camera, "
            "kamerayı kapat, kapat, creepy, etc."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "computer_settings",
        "description": (
            "Controls the computer: volume, brightness, window management, keyboard shortcuts, "
            "typing text on screen, closing apps, fullscreen, dark mode, WiFi, restart, shutdown, "
            "scrolling, tab management, zoom, screenshots, lock screen, refresh/reload page, "
            "and conservative process cleanup to free memory. Cleanup only closes approved "
            "user applications after the user confirms a named process. "
            "Use for ANY single computer control command."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "The action to perform"},
                "description": {"type": "STRING", "description": "Natural language description of what to do"},
                "value":       {"type": "STRING", "description": "Optional value: volume level, text to type, etc."},
                "process_name": {"type": "STRING", "description": "Exact approved user process to close, such as chrome or spotify"},
                "min_memory_mb": {"type": "INTEGER", "description": "Minimum resident memory for candidate discovery (default 500 MB)"},
                "max_processes": {"type": "INTEGER", "description": "Maximum candidates to report (1-5, default 3)"},
                "confirmed": {"type": "STRING", "description": "Must be yes to close the named process; omit for report-only discovery"}
            },
            "required": []
        }
    },
    {
        "name": "browser_control",
        "description": (
            "Controls any web browser. Use for: opening websites, searching the web, "
            "clicking elements, filling forms, scrolling, screenshots, navigation, any web-based task. "
            "NOT for 'find/download/show me a picture or video of X' — that's web_media_fetch, "
            "which displays the actual image/video directly on LITE's own HUD without opening a "
            "browser at all. Only use browser_control when the user wants an actual browser window "
            "open (browsing, logging into a site, filling a form, etc.). "
            "Simple open/search requests launch the user's own browser normally (their real profile "
            "and logged-in accounts); interactive actions (click, type, fill_form...) attach an "
            "automation browser. "
            "Always pass the 'browser' parameter when the user specifies a browser (e.g. 'open in Edge', "
            "'use Firefox', 'open Chrome'). Multiple browsers can run simultaneously."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "go_to | search | click | type | scroll | zoom | fill_form | smart_click | smart_type | get_text | get_url | press | new_tab | close_tab | screenshot | back | forward | reload | list_tabs | deep_dive | switch | list_browsers | close | close_all"},
                "browser":     {"type": "STRING", "description": "Target browser: chrome | edge | firefox | opera | operagx | brave | vivaldi | safari. Omit to use the currently active browser."},
                "url":         {"type": "STRING", "description": "URL for go_to / new_tab action"},
                "query":       {"type": "STRING", "description": "Search query for search action"},
                "engine":      {"type": "STRING", "description": "Search engine: google | bing | duckduckgo | yandex (default: google)"},
                "selector":    {"type": "STRING", "description": "CSS selector for click/type"},
                "text":        {"type": "STRING", "description": "Text to click or type"},
                "description": {"type": "STRING", "description": "Element description for smart_click/smart_type"},
                "direction":   {"type": "STRING", "description": "up | down for scroll"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount in pixels (default: 500)"},
                "max_pages":  {"type": "INTEGER", "description": "For deep_dive: maximum pages to inspect, 1-5 (default 3)"},
                "fields":     {"type": "OBJECT", "description": "For fill_form: map CSS selectors or field labels to values"},
                "key":         {"type": "STRING", "description": "Key name for press action (e.g. Enter, Escape, F5)"},
                "path":        {"type": "STRING", "description": "Save path for screenshot"},
                "incognito":   {"type": "BOOLEAN", "description": "Open in private/incognito mode"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "file_controller",
        "description": (
            "Manages files and folders: list, create, delete, move, copy, rename, "
            "read, write, find, disk usage. For create_file/write/create_folder: if "
            "`path` is omitted, this now defaults to the user's configured Obsidian "
            "vault (if one is set up) rather than guessing a folder — so leave `path` "
            "unset for a generic 'create/save a file' request unless the user named a "
            "specific location. `path` also accepts \"obsidian\"/\"vault\" explicitly."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "list | create_file | create_folder | delete | move | copy | rename | read | write | find | largest | disk_usage | organize_desktop | info"},
                "path":        {"type": "STRING", "description": "File/folder path or shortcut: desktop, downloads, documents, home, obsidian/vault (the user's configured Obsidian vault, if any)"},
                "destination": {"type": "STRING", "description": "Destination path for move/copy"},
                "new_name":    {"type": "STRING", "description": "New name for rename"},
                "content":     {"type": "STRING", "description": "Content for create_file/write"},
                "name":        {"type": "STRING", "description": "File name to search for"},
                "extension":   {"type": "STRING", "description": "File extension to search (e.g. .pdf)"},
                "count":       {"type": "INTEGER", "description": "Number of results for largest"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "desktop_control",
        "description": "Controls the desktop: wallpaper, organize, clean, list, stats.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "wallpaper | wallpaper_url | organize | clean | list | stats | task"},
                "path":   {"type": "STRING", "description": "Image path for wallpaper"},
                "url":    {"type": "STRING", "description": "Image URL for wallpaper_url"},
                "mode":   {"type": "STRING", "description": "by_type or by_date for organize"},
                "task":   {"type": "STRING", "description": "Natural language desktop task"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "code_agent",
        "description": (
            "Delegates any coding, automation, self-improvement, or full-project task "
            "to the Cline VS Code extension running headless — it reads files, edits, "
            "runs commands, and loops until the task is actually done, rather than a "
            "single-shot guess. Covers what code_helper, dev_agent, and the old "
            "automation_coding_agent used to do: fixing a bug, building a new feature "
            "into LITE itself, scaffolding a whole new project, or ad-hoc 'write me a "
            "script for X' help — just describe the task in plain language. Every run "
            "is git-checkpointed first and independently verified (every changed .py "
            "file must still compile) before being kept; anything that fails "
            "verification is automatically rolled back, so this never leaves LITE or "
            "another project in a broken state."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "task":        {"type": "STRING", "description": "The task, in the user's words — a fix, a feature, a whole project, or ad-hoc code help."},
                "scope":       {"type": "STRING", "description": "self (default — LITE's own codebase) | external (any other project Felix names; requires target)"},
                "target":      {"type": "STRING", "description": "For scope=external: absolute or home-relative path to the project folder. Not needed for scope=self."},
                "timeout":     {"type": "INTEGER", "description": "Max seconds to let the Cline session run before stopping it and verifying whatever it left behind (default 900)."},
            },
            "required": ["task"]
        }
    },
    {
        "name": "self_update",
        "description": (
            "Checks LITE's own GitHub repo for new commits, or pulls them down. Only works "
            "if the app folder is a git checkout (has a .git folder) with a GitHub remote. "
            "Call with mode='check' when the user asks if updates are available, or "
            "mode='update' when they ask to update/pull the latest version of LITE itself."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "mode": {"type": "STRING", "description": "check (default, read-only — reports how many commits behind) | update (pulls the latest commits)"},
            },
            "required": []
        }
    },
    {
        "name": "affiliate_growth_agent",
        "description": (
            "Autonomous Affiliate Marketing Social Media Growth subagent "
            "reporting to LITE. Ranks affiliate offers, researches "
            "audiences, builds social growth strategies and content "
            "calendars, writes posts/reels/video scripts/email sequences/"
            "ad copy/landing page copy, and reviews performance metrics. "
            "Every response ends with a structured LITE executive summary "
            "(opportunity, recommended action, expected ROI, risk level, "
            "priority, next actions). assign_job hands it a standing job "
            "that keeps running via GAS (the cloud service) even while "
            "the laptop is off; get_report pulls the latest overnight "
            "results (drafts written, leads, conversions, earnings). "
            "generate_ads is a one-off, on-demand generation from a "
            "free-text idea/brief — separate from the standing auto-"
            "generator, fires once, no job/cadence created. Call "
            "this whenever the user asks about affiliate marketing, "
            "content strategy, social growth, wants marketing content "
            "written, or asks the growth agent for a report."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": (
                        "rank_offers | research_audience | growth_strategy | "
                        "content_calendar | post_ideas | video_script | "
                        "email_sequence | landing_page | ad_copy | "
                        "performance_review | assign_job | get_report | "
                        "generate_ads | list_queue | edit_draft | "
                        "approve_draft | reject_draft | delete_draft | "
                        "download_draft | custom (default: custom). get_report "
                        "also pulls pending drafts into LITE's own review queue "
                        "UI — it never means 'go check the affiliate dashboard'. "
                        "list_queue re-shows the current pending drafts "
                        "(optionally filter by status). edit_draft changes a "
                        "draft's title/body/tracking_subid/platform/content_type "
                        "before approval. approve_draft/reject_draft decide a "
                        "specific draft's fate — posting itself stays manual by "
                        "design, this only marks the draft. delete_draft "
                        "permanently removes a draft from the database (not a "
                        "status flag — irreversible, unlike reject_draft). "
                        "download_draft saves a draft's text (plus any "
                        "rendered video/images/narration audio it has) to a "
                        "local folder. generate_ads takes a free-text "
                        "'description' of the idea and generates content for "
                        "it right now, independent of any standing job."
                    ),
                },
                "item_id":       {"type": "STRING",  "description": "The draft's id, for edit_draft/approve_draft/reject_draft/delete_draft/download_draft (shown in list_queue/get_report results)."},
                "status":        {"type": "STRING",  "description": "For list_queue: filter by status (default pending_approval)."},
                "body":          {"type": "STRING",  "description": "For edit_draft: new body text, if changing it."},
                "title":         {"type": "STRING",  "description": "For edit_draft: new title, if changing it."},
                "tracking_subid":{"type": "STRING",  "description": "For edit_draft: new tracking sub-id, if changing it."},
                "content_type":  {"type": "STRING",  "description": "For edit_draft: new content type, if changing it. For generate_ads: force this content_type for every piece (e.g. 'video', 'ad_copy')."},
                "description":   {"type": "STRING",  "description": "For generate_ads: the one-off idea/brief to generate content for, e.g. 'a launch ad for our new budgeting app aimed at Gen Z'."},
                "save_dir":      {"type": "STRING",  "description": "For download_draft: folder to save into (default: the user's Downloads folder)."},
                "task":          {"type": "STRING",  "description": "Free-form instruction, used when action=custom."},
                "niche":         {"type": "STRING",  "description": "The affiliate niche, e.g. 'personal finance apps'."},
                "offers":        {"type": "STRING",  "description": "Affiliate offers to rank (plain text or JSON list)."},
                "platforms":     {"type": "STRING",  "description": "Comma-separated platforms to prioritise. Also used by generate_ads to target specific platforms for the one-off pieces."},
                "platform":      {"type": "STRING",  "description": "Single platform for post_ideas/video_script/ad_copy."},
                "budget_notes":  {"type": "STRING",  "description": "Budget/resourcing constraints for growth_strategy."},
                "weeks":         {"type": "INTEGER", "description": "Length of content_calendar in weeks (default 4)."},
                "count":         {"type": "INTEGER", "description": "Number of post_ideas to generate (default 10). Also used by generate_ads for number of pieces (default 3)."},
                "angle":         {"type": "STRING",  "description": "Creative angle for video_script."},
                "goal":          {"type": "STRING",  "description": "email_sequence goal, or assign_job's standing goal."},
                "num_emails":    {"type": "INTEGER", "description": "Number of emails in email_sequence (default 5)."},
                "offer":         {"type": "STRING",  "description": "The specific offer for landing_page/ad_copy."},
                "metrics":       {"type": "STRING",  "description": "Performance metrics to review (plain text or JSON)."},
                "cadence_hours": {"type": "INTEGER", "description": "assign_job: how often GAS runs a content pass (default 6)."},
                "posts_per_run": {"type": "INTEGER", "description": "assign_job: pieces drafted per pass (default 3)."},
            },
            "required": []
        }
    },
    {
        "name": "mute_self",
        "description": (
            "Mutes or unmutes the microphone. Call this when the user says something like "
            "'mute yourself', 'stop listening', 'go silent', or the reverse ('unmute', "
            "'start listening again'). This is the LAST thing you should process before "
            "going silent if muting — don't keep talking after muting."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "state": {"type": "STRING", "description": "'mute' or 'unmute'. If omitted, toggles the current state."},
            },
            "required": []
        }
    },
    {
        "name": "obsidian_notes",
        "description": (
            "Reads, searches, or writes to the user's Obsidian vault. Every conversation is "
            "already auto-journaled to today's dated note, and every business-opportunities "
            "brief is already auto-saved to its own dated note in a separate 'Business "
            "Opportunities' folder — neither needs to be requested. This tool is mainly for "
            "RECALLING past discussions ('what did we talk about last week regarding X', "
            "'what did I decide about the SaaS idea') and for taking notes on demand. Use "
            "mode='search' with scope='vault' to search across both the conversation journal "
            "and past opportunity briefs together, mode='read' for a specific day's full log, "
            "mode='append' to log something specific right now, mode='create' for a new "
            "standalone note (e.g. a business plan doc), mode='list' to see recent journal days."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "mode":   {"type": "STRING", "description": "search (default) | read | append | create | list"},
                "query":  {"type": "STRING", "description": "For mode=search — keyword(s) to look for."},
                "date":   {"type": "STRING", "description": "For mode=read — 'YYYY-MM-DD', 'today', or 'yesterday' (default: today)."},
                "text":   {"type": "STRING", "description": "For mode=append — what to log. For mode=create — the note's content."},
                "name":   {"type": "STRING", "description": "For mode=create — the note's filename (without .md)."},
                "folder": {"type": "STRING", "description": "For mode=create — optional subfolder within the vault."},
                "scope":  {"type": "STRING", "description": "For mode=search — 'journal' (default, LITE's own entries only) or 'vault' (search everything)."},
            },
            "required": []
        }
    },
    {
        "name": "rics_agent",
        "description": (
            "Delegates a research, prospecting, or CRM/sales task to RICS (Research, "
            "Intelligence & CRM/Sales) — a specialist sub-agent (reporting to LITE) that "
            "owns the whole research-to-pipeline motion: looking up a company or person, "
            "logging companies/contacts/deals/interactions/tasks to the CRM, summarizing "
            "the sales pipeline, and delegating to the existing overnight affiliate-growth "
            "agent (GAS) for growth-job assignment and reports. Hand it a plain-language "
            "task — it routes itself to the right capability; you don't need to pre-decide "
            "action unless the user was specific. Call when the user asks LITE to research "
            "someone/some company, log a contact/company/deal/call/email, check the "
            "pipeline or follow-up tasks, or work with the overnight affiliate agent."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "Optional — leave empty and the agent infers it. research | log_company | log_contact | log_interaction | update_deal | list_pipeline | list_tasks | growth_job | growth_report"},
                "description": {"type": "STRING", "description": "The task or note, in the user's words — used both to infer the action and to actually do the work."},
                "target":      {"type": "STRING", "description": "For research: the company or person name to look up."},
                "name":        {"type": "STRING", "description": "For log_company/log_contact/update_deal: the name/title."},
                "id":          {"type": "STRING", "description": "CRM record id, when updating an existing company/contact/deal/task rather than creating a new one."},
                "company_id":  {"type": "STRING", "description": "Links a contact, deal, or interaction to an existing company."},
                "contact_id":  {"type": "STRING", "description": "Links a deal or interaction to an existing contact."},
                "stage":       {"type": "STRING", "description": "For update_deal/list_pipeline: prospecting | contacted | qualified | proposal | won | lost"},
                "type":        {"type": "STRING", "description": "For log_interaction: research | outreach_sent | call | email_reply | meeting | note"},
            },
            "required": []
        }
    },
    {
        "name": "data_analytics_agent",
        "description": (
            "Delegates a business-data question or file-analysis task to the Data "
            "Analytics Agent — a general Business Intelligence specialist sub-agent "
            "(reporting to LITE) covering data across LITE's other agents/data sources "
            "(currently: sales pipeline, affiliate earnings, and CRM activity from GAS/"
            "RICS, via fixed, safe queries — never open-ended SQL), plus analyzing CSV/"
            "Excel files the user provides. For an uploaded financial statement "
            "specifically (income statement, balance sheet, cash flow statement, or a "
            "request for ratio analysis), prefer fta_agent directly — it does real "
            "ratio computation (agents/financial_ratios.py), not just an AI summary; "
            "this agent will also auto-detect and hand those off to fta_agent itself "
            "as a safety net, but calling fta_agent directly is the more precise route. "
            "Hand it a plain-language question — it routes itself to the right query; "
            "you don't need to pre-decide action unless the user was specific. Call "
            "when the user asks how the business/pipeline/earnings are doing, wants a "
            "data snapshot, or wants a non-financial-statement spreadsheet/CSV file "
            "analyzed. Does NOT do charting/visualization yet."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "Optional — leave empty and the agent infers it. pipeline_summary | earnings_trend | interaction_volume | tasks_overview | report | analyze_file"},
                "description": {"type": "STRING", "description": "The question, in the user's words — used both to infer the action and to help interpret an analyzed file."},
                "days":        {"type": "INTEGER", "description": "For earnings_trend/interaction_volume: how many days back to look (default 30, or 7 within a combined 'report')."},
                "file_path":   {"type": "STRING", "description": "For analyze_file: path to the CSV/Excel file to analyze (usually the last-uploaded file)."},
                "file_action": {"type": "STRING", "description": "For analyze_file: analyze (default, AI insights) | stats | info | filter"},
            },
            "required": []
        }
    },
    {
        "name": "fta_agent",
        "description": (
            "Delegates a finance, investment-appraisal, statement-interpretation, market-"
            "research, M&A, tax/regulatory, or opportunity-appraisal task to FTA (Finance, "
            "FP&A & Treasury/Cashflow) — a specialist sub-agent (reporting to LITE). SIX "
            "capabilities: (1) investment appraisal — NPV, IRR, MIRR, payback "
            "period (simple + discounted), ARR, and Profitability Index from a caller-"
            "supplied cashflow list, computed by real deterministic code (never LLM "
            "arithmetic); (2) opportunity appraisal — same real math, but for a DESCRIBED "
            "opportunity with no cashflows given yet: an LLM estimates a conservative "
            "cashflow projection (with stated assumptions) from the description, then "
            "that projection is run through the same deterministic appraise() as "
            "investment_appraisal — use this one when the user describes an opportunity "
            "in words rather than already having numbers; (3) statement interpretation — "
            "reads an income statement/cash flow/balance sheet (uploaded file or pasted "
            "text), extracts the figures, computes financial ratios with real code, and "
            "returns analysis PLUS options, never a single prescriptive recommendation; "
            "(4) market research — ALWAYS live-searches (never answers from training "
            "data) for current rates/instruments: MPR, T-bill rates, money market rates, "
            "FGN bonds, capital/debt market, funds and the organizations offering them; "
            "(5) M&A advisory — valuation approaches and deal-structure considerations "
            "for a specific scenario, blending framework guidance with a live search pass "
            "for current precedent; (6) tax & regulatory — ALWAYS live-searches (never "
            "static), flags when a situation needs a licensed professional rather than "
            "just this analysis. Market research, M&A advisory, and tax/regulatory are "
            "all Nigerian-primary by default but NOT Nigeria-only — pass a different "
            "market/jurisdiction and it searches that instead. Statement interpretation, "
            "opportunity appraisal, and M&A advisory all accept a `context` string of "
            "pre-computed findings from another agent (e.g. RICS's research, Data "
            "Analytics's summary) when LITE is chaining agents on one task. Every "
            "capability presents analysis and options — never a single prescriptive "
            "directive; the decision stays with the user."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":              {"type": "STRING", "description": "investment_appraisal | opportunity_appraisal | statement_interpretation | market_research | ma_advisory | tax_regulatory — safe to omit: inferred automatically (a file_path or statement_text always infers statement_interpretation, so any uploaded/pasted financial report routes correctly without setting this)."},
                "cashflows":           {"type": "ARRAY", "items": {"type": "NUMBER"}, "description": "For investment_appraisal: the initial outlay as a NEGATIVE number, followed by each period's expected return, e.g. [-10000, 3000, 3000, 3000, 3000, 3000]."},
                "rate":                {"type": "NUMBER", "description": "For investment_appraisal/opportunity_appraisal: discount rate — accepts either form, e.g. 10 or 0.10 for 10%. Defaults to 15% for opportunity_appraisal if omitted."},
                "finance_rate":        {"type": "NUMBER", "description": "For MIRR only — the rate paid on financing outflows."},
                "reinvest_rate":       {"type": "NUMBER", "description": "For MIRR only — the rate earned reinvesting inflows."},
                "avg_annual_profit":   {"type": "NUMBER", "description": "For ARR only — average annual accounting profit (not cashflow)."},
                "initial_investment":  {"type": "NUMBER", "description": "For ARR only — the initial investment amount."},
                "periods":             {"type": "INTEGER", "description": "For opportunity_appraisal: how many periods (months, by default) to project — default 12."},
                "file_path":           {"type": "STRING", "description": "For statement_interpretation: path to an uploaded statement file (usually the last-uploaded file)."},
                "statement_text":      {"type": "STRING", "description": "For statement_interpretation: statement figures pasted directly, if no file was uploaded."},
                "context":             {"type": "STRING", "description": "For statement_interpretation, opportunity_appraisal, or ma_advisory: pre-computed findings from another agent (RICS/Data Analytics/Compliance) to factor into the analysis, when LITE is chaining agents on one task."},
                "description":         {"type": "STRING", "description": "The question/scenario/opportunity, in the user's words — used by statement_interpretation, opportunity_appraisal, market_research, ma_advisory, and tax_regulatory alike."},
                "market":              {"type": "STRING", "description": "For market_research: the jurisdiction/market to search, e.g. 'Nigeria' (default) or 'United States'."},
                "jurisdiction":        {"type": "STRING", "description": "For tax_regulatory: the jurisdiction to search, e.g. 'Nigeria' (default) or 'United Kingdom'."},
            },
            "required": []
        }
    },
    {
        "name": "compliance_legal_agent",
        "description": (
            "Delegates a contract review, compliance, regulatory, or legal risk task to "
            "the Compliance & Legal Agent — a specialist sub-agent (reporting to LITE). "
            "FOUR capabilities: (1) contract_review — reads a contract (uploaded file or "
            "pasted text), flags non-standard/risky clauses and missing provisions, "
            "presents analysis PLUS options, never a single 'sign/don't sign' directive; "
            "(2) compliance_check — ALWAYS live-searches (never training data) whether a "
            "described activity/practice is compliant with current regulation; "
            "(3) regulatory_research — ALWAYS live-searches current requirements/"
            "licensing/registration for a business activity; (4) risk_assessment — legal/"
            "compliance risk areas for a described scenario. All regulation-facing "
            "capabilities are Nigerian-primary by default but NOT Nigeria-only — pass a "
            "different jurisdiction and it searches that instead. Every capability "
            "explicitly flags when a situation needs a licensed lawyer rather than just "
            "this analysis. contract_review and risk_assessment both accept a `context` "
            "string of pre-computed findings from another agent when LITE is chaining "
            "agents on one task."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":          {"type": "STRING", "description": "contract_review | compliance_check | regulatory_research | risk_assessment (inferred from context if omitted)"},
                "file_path":       {"type": "STRING", "description": "For contract_review: path to an uploaded contract file (usually the last-uploaded file)."},
                "contract_text":   {"type": "STRING", "description": "For contract_review: contract text pasted directly, if no file was uploaded."},
                "description":     {"type": "STRING", "description": "The question/scenario, in the user's words — used by compliance_check, regulatory_research, and risk_assessment."},
                "jurisdiction":    {"type": "STRING", "description": "The jurisdiction to search, e.g. 'Nigeria' (default) or another country/region."},
                "context":         {"type": "STRING", "description": "Pre-computed findings from another agent (e.g. RICS/Data Analytics/FTA) to factor into the analysis, when LITE is chaining agents on one task."},
            },
            "required": []
        }
    },
    {
        "name": "scheduling_docs_agent",
        "description": (
            "Delegates a scheduling, document-creation, presentation-creation, Gmail, or "
            "Calendar task to the Scheduling & Docs Agent — a specialist sub-agent "
            "(reporting to LITE). Capabilities: (1) schedule_reminder — a one-off OS-level "
            "reminder; (2) create_document / (3) create_presentation — generates a .docx/"
            ".pptx from a description or outline; (4) check_inbox — lists recent unread "
            "Gmail with sender/subject/snippet ONLY (not the full body — use read_email for "
            "that); (5) read_email — opens a specific email (by email_id from check_inbox, "
            "or a from/subject hint) and shows its FULL body on screen; call this whenever "
            "Felix asks what an email actually says, to open/read one, or anything beyond "
            "the snippet check_inbox already showed; (6) draft_reply — drafts a reply to a "
            "specific email (same id/hint matching as read_email) per "
            "Felix's instructions, saved to Gmail Drafts for his own review and send — "
            "this NEVER sends an email itself, only drafts; (7) schedule_event — creates a "
            "calendar event; if it has attendees, this does NOT create it immediately (an "
            "invite email fires the instant it's created) — it instead describes exactly "
            "what would be created and returns a pending event_id, and you must relay that "
            "to Felix and wait for him to explicitly say to confirm or cancel before "
            "calling (8)/(9); (8) confirm_event(event_id) — actually creates a pending "
            "event and sends the invites, only after Felix explicitly confirms; "
            "(9) cancel_event(event_id) — discards a pending event, no invites sent; "
            "(10) list_events — upcoming calendar items, for context or conflict-checking. "
            "A background pass also periodically scans unread Gmail on its own and drafts "
            "replies for anything that looks like it genuinely needs one (skipping "
            "newsletters/notifications), surfacing them here without being asked — "
            "check_inbox/read_email/draft_reply are for when Felix explicitly asks about email."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":       {"type": "STRING", "description": "schedule_reminder | create_document | create_presentation | check_inbox | read_email | draft_reply | schedule_event | confirm_event | cancel_event | list_events (inferred from context only for the first three; required for the rest)"},
                "date":         {"type": "STRING", "description": "For schedule_reminder: YYYY-MM-DD."},
                "time":         {"type": "STRING", "description": "For schedule_reminder: HH:MM (24-hour)."},
                "message":      {"type": "STRING", "description": "For schedule_reminder: what the reminder should say."},
                "description":  {"type": "STRING", "description": "For create_document/create_presentation: what it should be about. For draft_reply: alias for instructions."},
                "outline":      {"type": "OBJECT", "description": "For create_document/create_presentation: a pre-structured outline to render directly, skipping LLM generation."},
                "max_results":  {"type": "INTEGER", "description": "For check_inbox/list_events: how many to return (default 10)."},
                "unread_only":  {"type": "BOOLEAN", "description": "For check_inbox: default true."},
                "email_id":     {"type": "STRING", "description": "For draft_reply: the Gmail message id, from a prior check_inbox result."},
                "from":         {"type": "STRING", "description": "For draft_reply, if email_id isn't known: match against the sender."},
                "subject":      {"type": "STRING", "description": "For draft_reply, if email_id isn't known: match against the subject."},
                "instructions": {"type": "STRING", "description": "For draft_reply: what the reply should say, in Felix's words."},
                "title":        {"type": "STRING", "description": "For schedule_event: the event title."},
                "start":        {"type": "STRING", "description": "For schedule_event: ISO datetime, e.g. 2026-09-05T14:00:00."},
                "end":          {"type": "STRING", "description": "For schedule_event: ISO datetime."},
                "attendees":    {"type": "ARRAY", "items": {"type": "STRING"}, "description": "For schedule_event: attendee email addresses, if any — triggers the confirm-before-inviting gate."},
                "location":     {"type": "STRING", "description": "For schedule_event: optional location."},
                "event_id":     {"type": "STRING", "description": "For confirm_event/cancel_event: the pending id returned by schedule_event."},
            },
            "required": []
        }
    },
    {
        "name": "opportunity_pipeline_agent",
        "description": (
            "Delegates to the Opportunity Pipeline — a specialist sub-agent (reporting to "
            "LITE) that chains RICS-style research, achievability/viability scoring, "
            "compliance_legal_agent's legal/compliance review, and fta_agent's real "
            "financial appraisal to produce ONE thoroughly vetted business/income "
            "opportunity, instead of a raw pile of unvetted ideas. This replaces the old "
            "'fetch fresh opportunities every launch' behavior — results are cached and "
            "reused for up to a couple of days rather than re-run constantly, and nothing "
            "below the achievability bar or that fails compliance ever reaches the user. "
            "THREE actions: (1) get (default) — instant if a fresh-enough cached result "
            "exists, otherwise kicks off a full background run and says so immediately "
            "(never blocks); call this for 'what's today's opportunity', 'give me a solid "
            "plan', or anything similar; (2) run_now — forces a brand new run even if the "
            "cache is fresh, for when the user explicitly wants a fresh look (e.g. 'run it "
            "again', 'try a different focus'); runs in the background unless wait=true is "
            "explicitly requested, since a full run (research + verification + scoring + "
            "compliance + financial appraisal) genuinely takes a while; (3) status — "
            "reports whether a run is in progress and how old the cached result is, "
            "without triggering anything."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":  {"type": "STRING", "description": "get (default) | run_now | status"},
                "focus":   {"type": "STRING", "description": "Optional — narrows the research focus, e.g. 'fintech consulting' or 'e-commerce arbitrage'. Leave empty for a broad international scan tailored to the user's own skill profile."},
                "wait":    {"type": "BOOLEAN", "description": "For run_now only: if true, blocks and returns the full result instead of running in the background. Only set this if the user explicitly said to wait."},
            },
            "required": []
        }
    },
    {
        "name": "computer_control",
            "description": "Direct computer control: type, click buttons/icons, hotkeys, scroll, zoom, back/forward navigation, move mouse, screenshots, and find elements on screen.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "type | smart_type | click | double_click | right_click | hotkey | press | scroll | zoom | back | forward | move | copy | paste | screenshot | wait | clear_field | focus_window | screen_find | screen_click | random_data | user_data"},
                "text":        {"type": "STRING", "description": "Text to type or paste"},
                "x":           {"type": "INTEGER", "description": "X coordinate"},
                "y":           {"type": "INTEGER", "description": "Y coordinate"},
                "keys":        {"type": "STRING", "description": "Key combination e.g. 'ctrl+c'"},
                "key":         {"type": "STRING", "description": "Single key e.g. 'enter'"},
                "direction":   {"type": "STRING", "description": "up | down | left | right"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount (default: 3)"},
                "seconds":     {"type": "NUMBER",  "description": "Seconds to wait"},
                "title":       {"type": "STRING",  "description": "Window title for focus_window"},
                "description": {"type": "STRING",  "description": "Element description for screen_find/screen_click"},
                "type":        {"type": "STRING",  "description": "Data type for random_data"},
                "field":       {"type": "STRING",  "description": "Field for user_data: name|email|city"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
                "path":        {"type": "STRING",  "description": "Save path for screenshot"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "game_updater",
        "description": (
            "THE ONLY tool for ANY Steam or Epic Games request. "
            "Use for: installing, downloading, updating games, listing installed games, "
            "checking download status, scheduling updates. "
            "ALWAYS call directly for any Steam/Epic/game request. "
            "NEVER use browser_control or web_search for Steam/Epic."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":    {"type": "STRING",  "description": "update | install | list | download_status | schedule | cancel_schedule | schedule_status (default: update)"},
                "platform":  {"type": "STRING",  "description": "steam | epic | both (default: both)"},
                "game_name": {"type": "STRING",  "description": "Game name (partial match supported)"},
                "app_id":    {"type": "STRING",  "description": "Steam AppID for install (optional)"},
                "hour":      {"type": "INTEGER", "description": "Hour for scheduled update 0-23 (default: 3)"},
                "minute":    {"type": "INTEGER", "description": "Minute for scheduled update 0-59 (default: 0)"},
                "shutdown_when_done": {"type": "BOOLEAN", "description": "Shut down PC when download finishes"},
            },
            "required": []
        }
    },
    {
        "name": "flight_finder",
        "description": "Searches Google Flights and speaks the best options.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "origin":      {"type": "STRING",  "description": "Departure city or airport code"},
                "destination": {"type": "STRING",  "description": "Arrival city or airport code"},
                "date":        {"type": "STRING",  "description": "Departure date (any format)"},
                "return_date": {"type": "STRING",  "description": "Return date for round trips"},
                "passengers":  {"type": "INTEGER", "description": "Number of passengers (default: 1)"},
                "cabin":       {"type": "STRING",  "description": "economy | premium | business | first"},
                "save":        {"type": "BOOLEAN", "description": "Save results to Notepad"},
            },
            "required": ["origin", "destination", "date"]
        }
    },
    {
        "name": "manage_monitor",
        "description": (
            "Add, remove, or list background monitoring topics. "
            "LITE checks these topics once a day and alerts the user when there is a new development. "
            "Use 'add' when the user says 'monitor X', 'track X', 'follow X'. "
            "Use 'remove' when the user says 'stop monitoring X'. "
            "Use 'list' when the user asks what is being monitored. "
            "Finance, crypto, market, and business topics are fully in scope — encourage "
            "monitoring them, since tracking developments in the user's ventures and markets "
            "is core to LITE's mission."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type":        "STRING",
                    "description": "add | remove | list",
                },
                "topic": {
                    "type":        "STRING",
                    "description": "Topic to monitor or stop monitoring (e.g. 'space exploration', 'AI news')",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "shutdown_lite",
        "description": (
            "Shuts down the assistant completely. "
            "Call this when the user expresses intent to end the conversation, "
            "close the assistant, say goodbye, or stop Lite. "
            "The user can say this in ANY language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
    "name": "file_processor",
    "description": (
        "Processes any file that the user has uploaded or dropped onto the interface. "
        "Use this when the user refers to an uploaded file and wants an action on it. "
        "Supports: images (describe/ocr/resize/compress/convert), "
        "PDFs (summarize/extract_text/to_word), "
        "Word docs & text files (summarize/fix/reformat/translate), "
        "CSV/Excel (analyze/stats/filter/sort/convert), "
        "JSON/XML (validate/format/analyze), "
        "code files (explain/review/fix/optimize/run/document/test), "
        "audio (transcribe/trim/convert/info), "
        "video (trim/extract_audio/extract_frame/compress/transcribe/summarize/info), "
        "archives (list/extract), "
        "presentations (summarize/extract_text). "
        "ALWAYS call this tool when a file has been uploaded and the user gives a command about it. "
        "If the user's command is ambiguous, pick the most logical action for that file type."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "file_path": {
                "type": "STRING",
                "description": "Full path to the uploaded file. Leave empty to use the currently uploaded file."
            },
            "action": {
                "type": "STRING",
                "description": (
                    "What to do with the file. Examples by type:\n"
                    "image: describe | ocr | resize | compress | convert | info\n"
                    "pdf: summarize | read | extract_text | to_word | info\n"
                    "docx/txt: summarize | read | fix | reformat | translate_hint | word_count | to_bullet\n"
                    "csv/excel: analyze | stats | filter | sort | convert | info\n"
                    "json: validate | format | analyze | to_csv\n"
                    "code: explain | review | fix | optimize | run | document | test\n"
                    "audio: transcribe | trim | convert | info\n"
                    "video: trim | extract_audio | extract_frame | compress | transcribe | summarize | info | convert\n"
                    "  (video 'transcribe' = speech-to-text only; video 'summarize' = full transcript "
                    "AND a summary in one pass, using native video+audio understanding — use 'summarize' "
                    "whenever the user wants both, or just wants to know what a video covers)\n"
                    "archive: list | extract\n"
                    "pptx: summarize | read | extract_text | analyze"
                )
            },
            "instruction": {
                "type": "STRING",
                "description": "Free-form instruction if action doesn't cover it. E.g. 'translate this to Turkish', 'find all email addresses'"
            },
            "format": {
                "type": "STRING",
                "description": "Target format for conversion. E.g. 'mp3', 'pdf', 'csv', 'png'"
            },
            "width":     {"type": "INTEGER", "description": "Target width for image resize"},
            "height":    {"type": "INTEGER", "description": "Target height for image resize"},
            "scale":     {"type": "NUMBER",  "description": "Scale factor for image resize (e.g. 0.5)"},
            "quality":   {"type": "INTEGER", "description": "Quality 1-100 for image/video compress"},
            "start":     {"type": "STRING",  "description": "Start time for trim: seconds or HH:MM:SS"},
            "end":       {"type": "STRING",  "description": "End time for trim: seconds or HH:MM:SS"},
            "timestamp": {"type": "STRING",  "description": "Timestamp for video frame extraction HH:MM:SS"},
            "column":    {"type": "STRING",  "description": "Column name for CSV filter/sort"},
            "value":     {"type": "STRING",  "description": "Filter value for CSV filter"},
            "condition": {"type": "STRING",  "description": "Filter condition: equals|contains|gt|lt"},
            "ascending": {"type": "BOOLEAN", "description": "Sort order for CSV sort (default: true)"},
            "save":      {"type": "BOOLEAN", "description": "Save result to file (default: true)"},
            "destination": {"type": "STRING", "description": "Output folder for archive extract"},
        },
        "required": []
    }
},
    {
        "name": "save_memory",
        "description": (
            "Save an important personal fact about the user to long-term memory. "
            "Call this silently whenever the user reveals something worth remembering: "
            "name, age, city, job, preferences, hobbies, relationships, projects, or future plans. "
            "Do NOT call for: weather, reminders, searches, or one-time commands. "
            "Do NOT announce that you are saving — just call it silently. "
            "Values must be in English regardless of the conversation language. "
            "NEVER save identity.language — language switching is disabled entirely, "
            "so there is no standing language preference to persist, even if the user "
            "asks you to always speak another language. Acknowledge such a request "
            "verbally if asked, but do not save it to memory and do not act on it."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity — name, age, birthday, city, job, language, nationality | "
                        "preferences — favorite food/color/music/film/game/sport, hobbies | "
                        "projects — active technical/personal projects, goals, things being built | "
                        "business — income ventures being pursued, opportunities evaluated "
                        "(accepted/rejected), revenue targets, deadlines, capital committed — "
                        "LITE's core mission category, update this actively | "
                        "relationships — friends, family, partner, colleagues | "
                        "wishes — future plans, things to buy, travel dreams | "
                        "notes — habits, schedule, anything else worth remembering"
                    )
                },
                "key":   {"type": "STRING", "description": "Short snake_case key (e.g. name, favorite_food, sister_name)"},
                "value": {"type": "STRING", "description": "Concise value in English (e.g. Fatih, pizza, older sister)"},
            },
            "required": ["category", "key", "value"]
        }
    },
]

# --- Plugin system ---


class LiteLive:

    def __init__(self, ui: LiteUI):
        self.ui             = ui
        self._asst_name     = "LITE"   # updated each session from config
        self.session              = None
        self.audio_in_queue       = None
        self.out_queue            = None
        # Dedicated, small thread pool exclusively for the real-time output-
        # stream write in _play_audio(). Every tool call (open_app,
        # browser_control, code_agent, screen_process, etc.) runs via
        # loop.run_in_executor(None, ...) — Python's DEFAULT executor —
        # and asyncio.to_thread() used to draw from that exact same shared
        # pool for the audio write. A long-running tool call (code_agent in
        # particular can run for minutes) occupying a worker could delay the
        # time-critical stream.write() call queued right behind it, causing
        # an audible dropout/"break" mid-sentence. Isolating audio output
        # onto its own pool removes that contention entirely regardless of
        # what else is running.
        self._audio_out_executor  = ThreadPoolExecutor(max_workers=2, thread_name_prefix="LiteAudioOut")
        self._loop                = None
        self._is_speaking         = False
        self._speaking_lock       = threading.Lock()
        self._phone_active        = False   # True while phone mic is streaming; pauses PC mic
        self._phone_stt           = None    # lazily-loaded WhisperSTT, shared across fallback-mode phone utterances
        self._pending_vision       = None    # (img_bytes, mime_type, question, angle) to inject after tool response
        self._pending_file_notice  = None   # uploaded before a Live session is ready
        self._vision_cam_active    = False   # True if camera was opened for vision → auto-close after response
        self._vision_close_pending = False   # True after vision injected; next turn_complete closes camera
        self._vision_last_time     = 0.0     # monotonic time of last screen_process call (cooldown guard)
        self._vision_busy          = False   # True while a vision capture/inject cycle is in flight
        self._interrupted          = False   # True while draining audio after user interrupt
        self._speech_ended_at      = 0.0     # monotonic time LITE last stopped speaking (echo guard)
        self._aec                  = None    # EchoCanceller (voice barge-in); created lazily in _listen_audio
        self._aec_notice_sent      = False   # the barge-in capability is logged only once
        self.ui.on_text_command   = self._on_text_command
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_interrupt      = self.interrupt
        self._turn_done_event: asyncio.Event | None = None
        self._dashboard     = None
        self._briefing_sent    = False          # morning briefing fires once per process
        self._sys_monitor      = SystemMonitor()  # persistent cooldown state
        self._proactive        = ProactiveEngine()
        self._last_user_speech = time.monotonic()  # updated on every user utterance
        self._session_log: list[str] = []          # conversation turns for end-of-session summary
        self._authorized_language: str | None = None  # permanently None — language switching is disabled entirely (nothing sets this anymore); kept only because proactive.py's build_prompt() still takes it as a param
        self.ui.on_file_uploaded = self._on_file_uploaded

    def _make_remote_key(self):
        """Called from Qt main thread when user presses Remote Control."""
        if self._dashboard is None:
            self.ui.write_log(
                "SYS: Dashboard unavailable. "
                "Run: pip install fastapi \"uvicorn[standard]\" cryptography"
            )
            return None
        key    = self._dashboard.new_key()
        url    = self._dashboard.get_url()
        manual = self._dashboard.get_manual_url()
        return url, key, f"{url}/auto-login?key={key}", manual

    def _on_text_command(self, text: str):
        self._send_text_to_session(text)

    def _send_text_to_session(self, text: str) -> bool:
        if not self._loop or not self.session:
            return False
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )
        return True

    def _on_file_uploaded(self, path: str):
        """Make every upload source visible to the next file-processing turn."""
        path = str(Path(path).expanduser().resolve())
        self.ui.set_current_file(path)
        notice = (
            f"A file named {Path(path).name} has just been uploaded and is ready at "
            f"{path}. Ask the user what they would like you to do with it. "
            "Do not process it until they give an instruction."
        )
        if not self._send_text_to_session(notice):
            self._pending_file_notice = notice

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
            if not value:
                # Stamp when speech ended — the echo guard (_mic_capture_allowed)
                # keeps mic frames out of the session for a short tail window
                # after this, so speaker echo can't trigger Gemini's VAD.
                self._speech_ended_at = time.monotonic()
        if value:
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")

    def _echo_tail_passed(self) -> bool:
        """True once the echo-guard tail window after LITE's last spoken word
        has elapsed (and False while LITE is speaking). Shared by the PC mic
        gate and the phone relay — both feed audio into the same Live
        session, so both must stay quiet through the tail."""
        with self._speaking_lock:
            speaking = self._is_speaking
            ended_at = self._speech_ended_at
        if speaking:
            # Opt-in echo cancellation (config "echo_cancellation": true)
            # keeps capture open during playback — the canceller subtracts
            # LITE's own voice, which is what enables voice barge-in.
            return getattr(self, "_aec", None) is not None
        return (time.monotonic() - ended_at) >= ECHO_GUARD_S

    def _mic_capture_allowed(self) -> bool:
        """True when mic frames should stream to Gemini right now.

        Two modes:
        - DEFAULT (no echo canceller): capture is gated while LITE speaks
          (plus a short tail window — ECHO_GUARD_S). Without it, speaker
          echo re-enters the mic, Gemini's VAD hears "the user" mid-sentence
          and LITE interrupts itself — the flip-flopping "broken
          conversation" bug. Barge-in happens through the stop button, the
          global mute hotkey, and the remote dashboard, which all call
          interrupt() → set_speaking(False) and reopen the mic instantly.
        - Echo cancellation active (self._aec, opt-in via "echo_cancellation":
          true): the mic stays OPEN even while LITE speaks — the canceller
          subtracts LITE's own voice from the capture, so Gemini's VAD only
          ever sees the real user (true voice barge-in). Only enable this
          where the canceller provably beats the speaker leak.
        """
        if self.ui.muted or self._phone_active:
            return False
        return self._echo_tail_passed()

    def interrupt(self) -> None:
        """Stop LITE mid-speech: drain queued audio and open mic immediately."""
        self._interrupted = True
        q = self.audio_in_queue
        if q:
            drained = 0
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except Exception:
                    break
            if drained:
                print(f"[LITE] ✋ Interrupted — {drained} audio chunks discarded")
        self.set_speaking(False)
        if self._turn_done_event:
            self._turn_done_event.clear()
        self.ui.write_log("SYS: Interrupted — listening...")

    def speak(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"Sir, {tool_name} encountered an error. {short}")

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        # Load customization from config
        try:
            with open(API_CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                _cfg = json.load(f)
            self._asst_name = (_cfg.get("assistant_name") or "LITE").strip()
            _user_name = (_cfg.get("user_name") or "").strip()
        except Exception:
            self._asst_name = "LITE"
            _user_name = ""

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        # Identity injection — overrides any hardcoded name in prompt.txt
        _addr = (f"ADDRESS: Always call the user '{_user_name}'."
                 if _user_name
                 else "ADDRESS: Always call the user \"sir\" unless the user has told you "
                      "to address them differently.")
        identity_ctx = (
            f"[IDENTITY]\n"
            f"Your name is {self._asst_name}. "
            f"Always refer to yourself as {self._asst_name}.\n"
            f"{_addr}\n\n"
            f"LANGUAGE: English is the default and the language you open every conversation "
            f"in. You do NOT switch language on your own initiative — not because of the "
            f"user's name, accent, location, time of day, system locale, or any 'Language' "
            f"field in memory below. You DO switch language when the user explicitly asks "
            f"you to (e.g. 'speak French from now on'), and you stay in that language for "
            f"the rest of the conversation unless the user asks you to switch again. The "
            f"'Language' value in memory is background context only and is never a switch "
            f"trigger by itself. You may still translate text or discuss other languages "
            f"as a content task when asked; that is not the same as switching your "
            f"conversational language, which is governed by the rules above.\n\n"
        )

        parts = [time_ctx, identity_ctx, EXECUTIVE_DIRECTORY_CONTEXT]
        if mem_str:
            parts.append(mem_str)
        parts.append(sys_prompt)

        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": TOOL_DECLARATIONS}],
            session_resumption=types.SessionResumptionConfig(),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name="Charon"
                    )
                )
            ),
        )

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})

        print(f"[LITE] 🔧 {name}  {args}")
        self.ui.set_state("THINKING")
        # Executive Directory HUD: swap the Active Operative badge to
        # whichever roster member actually owns this tool (see
        # AGENT_FOR_TOOL below); anything not in the map is LITE's own
        # direct action, so it stays on LITE. Reset back to LITE happens
        # once, at the bottom of this function, so every branch below —
        # success or failure — hands control back automatically.
        self.ui.set_active_agent(AGENT_FOR_TOOL.get(name, "lite"))

        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        loop   = asyncio.get_event_loop()
        result = "Done."

        try:
            if name == "open_app":
                r = await loop.run_in_executor(None, lambda: open_app(parameters=args, response=None, player=self.ui))
                result = r or f"Opened {args.get('app_name')}."

            elif name == "weather_report":
                r = await loop.run_in_executor(None, lambda: weather_action(parameters=args, player=self.ui))
                result = r or "Weather delivered."

            elif name == "weather_widget":
                r = await loop.run_in_executor(None, lambda: weather_widget(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "ui_control":
                ui_action = (args.get("action") or "").strip().lower()
                if ui_action == "scroll":
                    result = self.ui.ui_scroll(args.get("direction", "down"), int(args.get("amount") or 400))
                elif ui_action == "zoom":
                    result = self.ui.ui_zoom(args.get("zoom_action", "in"))
                elif ui_action == "click":
                    result = self.ui.ui_click(args.get("target", ""))
                elif ui_action == "media":
                    result = self.ui.ui_media(args.get("media_action", "play"))
                else:
                    result = f"Unknown ui_control action '{ui_action}' — use scroll, zoom, click, or media."

            elif name == "show_executive_directory":
                self.ui.show_executive_directory(True)
                result = "Executive directory opened."

            elif name == "hide_executive_directory":
                self.ui.show_executive_directory(False)
                result = "Executive directory hidden."

            elif name == "browser_control":
                r = await loop.run_in_executor(None, lambda: browser_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "file_controller":
                r = await loop.run_in_executor(None, lambda: file_controller(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "send_message":
                r = await loop.run_in_executor(None, lambda: send_message(parameters=args, response=None, player=self.ui, session_memory=None))
                result = r or f"Message sent to {args.get('receiver')}."

            elif name == "reminder":
                r = await loop.run_in_executor(None, lambda: reminder(parameters=args, response=None, player=self.ui))
                result = r or "Reminder set."

            elif name == "youtube_video":
                r = await loop.run_in_executor(None, lambda: youtube_video(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "screen_process":
                import time as _t_mod
                _now = _t_mod.monotonic()
                _cooldown = 4.0  # seconds — covers echo window after speaking ends
                if self._vision_busy or (_now - self._vision_last_time) < _cooldown:
                    _wait = max(0, _cooldown - (_now - self._vision_last_time))
                    print(f"[Vision] ⏳ Cooldown active ({_wait:.1f}s remaining) — ignoring duplicate call")
                    result = "Vision is still processing the previous request. I will not call this again."
                else:
                    self._vision_busy      = True
                    self._vision_last_time = _now
                    angle     = args.get("angle", "screen").lower()
                    user_text = args.get("text", "What do you see?")
                    if angle == "camera":
                        img_b, mime_t = await loop.run_in_executor(None, _capture_camera)
                        self.ui.start_camera_stream()
                        self._vision_cam_active = True
                        print(f"[Vision] 📷 Camera: {len(img_b):,} bytes")
                        _stall = "camera"
                    else:
                        img_b, mime_t = await loop.run_in_executor(None, _capture_screen)
                        print(f"[Vision] 🖥️  Screen: {len(img_b):,} bytes")
                        _stall = "screen"
                    self._pending_vision = (img_b, mime_t, user_text, angle)
                    result = (
                        f"[VISION_ACTIVE] {_stall.capitalize()} captured. "
                        f"Immediately say ONE short natural sentence in the user's own language, "
                        f"telling them you are looking at their {_stall} right now. "
                        f"Do NOT describe or guess content — the actual image arrives in the NEXT message."
                    )

            elif name == "close_camera":
                self.ui.stop_camera_stream()
                result = "Camera closed."

            elif name == "computer_settings":
                r = await loop.run_in_executor(None, lambda: computer_settings(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "desktop_control":
                r = await loop.run_in_executor(None, lambda: desktop_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "code_agent":
                r = await loop.run_in_executor(None, lambda: code_agent(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."
                # No show_content call here — code_agent pushes its own HUD
                # content (a single plain-text run report).

            elif name == "rics_agent":
                r = await loop.run_in_executor(None, lambda: rics_agent(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."
                if r:
                    self.ui.show_content("RICS", r)

            elif name == "data_analytics_agent":
                r = await loop.run_in_executor(None, lambda: data_analytics_agent(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."
                if r:
                    self.ui.show_content("DATA ANALYTICS", r)

            elif name == "fta_agent":
                r = await loop.run_in_executor(None, lambda: fta_agent(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."
                if r:
                    self.ui.show_content("FTA", r)

            elif name == "compliance_legal_agent":
                r = await loop.run_in_executor(None, lambda: compliance_legal_agent(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."
                if r:
                    self.ui.show_content("COMPLIANCE & LEGAL", r)

            elif name == "scheduling_docs_agent":
                r = await loop.run_in_executor(None, lambda: scheduling_docs_agent(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."
                if r:
                    self.ui.show_content("SCHEDULING & DOCS", r)

            elif name == "opportunity_pipeline_agent":
                r = await loop.run_in_executor(None, lambda: opportunity_pipeline_agent(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."
                if r:
                    self.ui.show_content("OPPORTUNITY PIPELINE", r)

            elif name == "self_update":
                r = await loop.run_in_executor(None, lambda: self_update(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."
                if r:
                    self.ui.show_content("SELF-UPDATE", r)

            elif name == "affiliate_growth_agent":
                r = await loop.run_in_executor(
                    None, lambda: affiliate_growth_agent(parameters=args, player=self.ui, speak=self.speak)
                )
                result = r or "Done."

            elif name == "mute_self":
                state = (args.get("state") or "").strip().lower()
                if state == "mute":
                    target = True
                elif state == "unmute":
                    target = False
                else:
                    target = not self.ui.muted  # read current state before toggling
                self.ui.set_muted(target)
                result = f"Microphone {'muted' if target else 'active'}."

            elif name == "obsidian_notes":
                r = await loop.run_in_executor(None, lambda: obsidian_notes(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "web_media_fetch":
                r = await loop.run_in_executor(None, lambda: web_media_fetch(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."
                # No show_content call here — web_media_fetch pushes the
                # actual image/video to the HUD itself once it has one.

            elif name == "web_search":
                r = await loop.run_in_executor(None, lambda: web_search_action(parameters=args, player=self.ui))
                result = r or "Done."
                # Mirror results to the on-screen content panel
                _mode = args.get("mode", "search")
                if r and not r.startswith("No results") and not r.startswith("Search failed"):
                    _query = args.get("query") or ", ".join(args.get("items", []))
                    _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                    self.ui.show_content(_label, r)
            elif name == "file_processor":
                if not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                r = await loop.run_in_executor(
                    None,
                    lambda: file_processor(parameters=args, player=self.ui, speak=self.speak)
                )
                result = r or "Done."

            elif name == "computer_control":
                r = await loop.run_in_executor(None, lambda: computer_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "game_updater":
                r = await loop.run_in_executor(None, lambda: game_updater(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "flight_finder":
                r = await loop.run_in_executor(None, lambda: flight_finder(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "system_status":
                r = await loop.run_in_executor(None, get_system_status)
                result = str(r)

            elif name == "manage_monitor":
                action = args.get("action", "").lower().strip()
                topic  = args.get("topic", "").strip()
                if action == "add" and topic:
                    result = await asyncio.to_thread(add_monitor, topic)
                elif action == "remove" and topic:
                    result = await asyncio.to_thread(remove_monitor, topic)
                elif action == "list":
                    topics = await asyncio.to_thread(list_monitors)
                    result = ("Monitoring: " + ", ".join(topics)) if topics else "No topics are being monitored."
                else:
                    result = "Specify action (add/remove/list) and a topic."

            elif name == "shutdown_lite":
                self.ui.write_log("SYS: Shutdown requested.")
                async def _do_shutdown():
                    await self._save_session_summary()
                    if self.session:
                        try:
                            await self.session.send_client_content(
                                turns={"parts": [{"text": "Say a brief natural goodbye to the user."}]},
                                turn_complete=True,
                            )
                        except Exception:
                            pass
                    await asyncio.sleep(1.5)
                    import os as _os
                    _os._exit(0)
                asyncio.create_task(_do_shutdown())

            else:
                result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)

        self.ui.set_active_agent("lite")  # hand-off complete — reporting back to LITE

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        print(f"[LITE] 📤 {name} → {str(result)[:80]}")
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result}
        )

    def _queue_live_audio(self, data: bytes):
        """Keep the freshest mic frames; drop stale ones when the live queue is full."""
        if self.out_queue is None:
            return
        item = {"data": data, "mime_type": INPUT_AUDIO_MIME}
        try:
            self.out_queue.put_nowait(item)
        except asyncio.QueueFull:
            try:
                self.out_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.out_queue.put_nowait(item)

    async def _send_realtime(self):
        consecutive_failures = 0
        last_logged = 0.0
        while True:
            msg = await self.out_queue.get()
            try:
                await self.session.send_realtime_input(media=msg)
                consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # A transient blip (one bad websocket write, a hiccup while a
                # tool call is mid-flight) must NOT tear the whole Live
                # session down — that used to turn a single dropped frame
                # into a full mid-conversation reconnect. Drop the frame and
                # carry on; _receive_audio raises separately if the session
                # is genuinely dead. Only sustained failure escalates.
                consecutive_failures += 1
                now = time.monotonic()
                if now - last_logged > 5.0:
                    print(f"[LITE] ⚠️ send_realtime failed ({e}) — frame dropped")
                    last_logged = now
                if consecutive_failures >= 10:
                    raise  # session is dead — let the reconnect logic take over

    async def _listen_audio(self):
        print("[LITE] 🎤 Mic started")
        loop = asyncio.get_event_loop()

        # Echo cancellation (voice barge-in) — OPT-IN, off by default: with
        # echo cancellation active the mic stays open while LITE speaks, so
        # cancellation quality directly decides whether LITE hears itself.
        # Created once per process, before the mic stream opens.
        if self._aec is None and not self._aec_notice_sent:
            self._aec_notice_sent = True
            if _echo_cancellation_enabled():
                try:
                    from core.echo_canceller import EchoCanceller, aec_available
                    if aec_available():
                        self._aec = EchoCanceller()
                        self.ui.write_log(
                            "SYS: Echo cancellation active — you can interrupt me by voice."
                        )
                    else:
                        self.ui.write_log(
                            "SYS: Voice barge-in unavailable (install 'pyaec') — use the stop button to interrupt."
                        )
                except Exception as e:
                    print(f"[LITE] ⚠️ Echo canceller init failed: {e}")
                    self.ui.write_log(
                        "SYS: Voice barge-in unavailable — use the stop button to interrupt."
                    )
            else:
                self.ui.write_log(
                    "SYS: Mic pauses while I speak — use the stop button or Ctrl+Alt+M to interrupt."
                )

        def enqueue_mic_audio(data):
            """Keep the freshest mic input and discard stale packets when the queue is full."""
            self._queue_live_audio(data)

        def callback(indata, frames, time_info, status):
            if status:
                print(f"[LITE] ⚠️ Mic status: {status}")
            # Gating: open whenever allowed — with echo cancellation that
            # includes while LITE is speaking (true barge-in); without it,
            # frames are dropped while speaking (see _mic_capture_allowed).
            if not self._mic_capture_allowed():
                return
            data = resample_audio(
                indata[:, input_channel], input_rate, SEND_SAMPLE_RATE
            )
            if self._aec is not None:
                try:
                    cleaned = self._aec.process_mic(data.tobytes())
                    data = np.frombuffer(cleaned, dtype=np.int16)
                except Exception as e:
                    print(f"[LITE] ⚠️ Echo cancellation skipped a frame: {e}")
            loop.call_soon_threadsafe(enqueue_mic_audio, data.tobytes())

        # Log mic state only on TRANSITIONS, not on every 1-second retry: an
        # unstable device (Windows audio engine restart, another app grabbing
        # the array, a duplicate instance) used to spam "unavailable" into
        # the activity log every second while it recovered on its own.
        mic_was_up = None  # None = unknown — log the first state unconditionally
        while True:
            try:
                input_device, input_rate = resolve_input_stream(SEND_SAMPLE_RATE)
                input_channel = input_channel_index(input_device)
                with sd.InputStream(
                    device=input_device,
                    samplerate=input_rate,
                    channels=max(CHANNELS, input_channel + 1),
                    dtype="int16",
                    blocksize=CHUNK_SIZE,
                    latency="high",   # see _play_audio()'s latency="high" comment — same jitter-tolerance reasoning applies to capture
                    callback=callback,
                ):
                    print(f"[LITE] 🎤 Input device: {input_device_name(input_device)}")
                    print("[LITE] 🎤 Mic stream open")
                    if mic_was_up is not True:
                        mic_was_up = True
                        self.ui.write_log("SYS: Laptop microphone active.")
                    while True:
                        await asyncio.sleep(0.1)
            except Exception as e:
                print(f"[LITE] ❌ Mic stream lost: {e}; retrying")
                if mic_was_up is not False:
                    mic_was_up = False
                    self.ui.write_log("ERR: Laptop microphone unavailable — retrying.")
                await asyncio.sleep(1)

    async def _receive_audio(self):
        print("[LITE] 👂 Recv started")
        out_buf, in_buf = [], []

        try:
            while True:
                async for response in self.session.receive():

                    if response.data:
                        if self._interrupted:
                            pass  # discard: interrupted
                        else:
                            if self._turn_done_event and self._turn_done_event.is_set():
                                self._turn_done_event.clear()
                            # Split into ~50 ms chunks so interrupt() stops audio within 50 ms
                            # (24000 Hz × 2 bytes/sample × 0.05 s = 2400 bytes per slice)
                            _audio_data = response.data
                            _SLICE = 2400
                            for _i in range(0, len(_audio_data), _SLICE):
                                chunk = _audio_data[_i : _i + _SLICE]
                                try:
                                    self.audio_in_queue.put_nowait(chunk)
                                except asyncio.QueueFull:
                                    try:
                                        self.audio_in_queue.get_nowait()
                                    except asyncio.QueueEmpty:
                                        pass
                                    self.audio_in_queue.put_nowait(chunk)

                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            if txt and txt != (out_buf[-1] if out_buf else ""):
                                out_buf.append(txt)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                in_buf.append(txt)
                                self._last_user_speech = time.monotonic()

                        if sc.turn_complete:
                            if self._turn_done_event:
                                self._turn_done_event.set()

                            # If this turn_complete ends an interrupted response, clear the
                            # flag and skip all further processing for that turn.
                            if self._interrupted:
                                self._interrupted = False
                                in_buf  = []
                                out_buf = []
                                continue

                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                self.ui.write_log(f"You: {full_in}")
                                self._session_log.append(f"User: {full_in}")
                                # Language switching is disabled entirely — see the
                                # LANGUAGE rule in identity_ctx above. self._authorized_language
                                # is intentionally left permanently None; nothing sets it
                                # anymore, so the watchdog below always fires on drift.
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "user",
                                        "text": full_in,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            in_buf = []

                            full_out = " ".join(out_buf).strip()
                            if full_out:
                                self.ui.write_log(f"{self._asst_name}: {full_out}")
                                self._session_log.append(f"{self._asst_name}: {full_out}")
                                # Watchdog: the model just drifted into a non-Latin script
                                # (Chinese/Japanese/Korean/Russian/Arabic/Hebrew/Thai).
                                # Language switching is disabled entirely now, so this fires
                                # unconditionally on any drift — there is no authorized
                                # exception to check anymore. System-prompt instructions
                                # alone don't reliably stop this on native-audio models, so
                                # force a correction now rather than let it continue.
                                if self.session and _foreign_script_ratio(full_out) > 0.15:
                                    print("[Language] ⚠️ Language drift detected — forcing correction to English")
                                    try:
                                        await self.session.send_client_content(
                                            turns={"role": "user", "parts": [{"text": (
                                                "[SYSTEM] You just replied in a language other than English. "
                                                "Language switching is disabled. Continue your NEXT reply "
                                                "entirely in English — do not apologize, explain, or comment "
                                                "on the correction, just continue naturally in English."
                                            )}]},
                                            turn_complete=True,
                                        )
                                    except Exception as e:
                                        print(f"[Language] ⚠️ Correction turn failed to send: {e}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "lite",
                                        "text": full_out,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            out_buf = []

                            # Vision injection: model finished tool-response turn → now send the image
                            if self._pending_vision and self.session:
                                import base64 as _b64
                                img_b, mime_t, question, angle = self._pending_vision
                                self._pending_vision = None
                                b64 = _b64.b64encode(img_b).decode("ascii")
                                print(f"[Vision] 📤 {len(img_b):,} bytes (angle={angle}) → main session")
                                await self.session.send_client_content(
                                    turns={"parts": [
                                        {"inline_data": {"mime_type": mime_t, "data": b64}},
                                        {"text": question},
                                    ]},
                                    turn_complete=True,
                                )
                                # Mark next turn_complete behaviour depending on angle
                                if self._vision_cam_active:
                                    # Camera: keep busy until LITE finishes speaking the answer
                                    self._vision_cam_active    = False
                                    self._vision_close_pending = True
                                else:
                                    # Screen-only: no camera to close; release busy flag now
                                    self._vision_busy = False
                            elif self._vision_close_pending:
                                # This turn_complete IS the vision answer — close camera + release busy flag
                                self._vision_close_pending = False
                                self._vision_busy = False
                                async def _cam_close():
                                    await asyncio.sleep(2.0)
                                    self.ui.stop_camera_stream()
                                asyncio.create_task(_cam_close())

                    if response.tool_call:
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            print(f"[LITE] 📞 {fc.name}")
                            fr = await self._execute_tool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses
                        )
        except Exception as e:
            print(f"[LITE] ❌ Recv: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[LITE] 🔊 Play started")
        output_device = resolve_output_device(RECEIVE_SAMPLE_RATE)
        print(f"[LITE] 🔊 Output device: {output_device_name(output_device)}")

        stream = sd.RawOutputStream(
            device=output_device,
            samplerate=RECEIVE_SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=CHUNK_SIZE,
            # 'high' asks PortAudio for a larger internal buffer than the
            # driver's low-latency default — trades a small amount of extra
            # end-to-end delay (typically well under 200ms) for much more
            # tolerance of scheduling jitter (GC pauses, other threads,
            # HUD rendering), which is what was producing audible
            # dropouts/"breaks" mid-sentence under any load.
            latency="high",
        )
        stream.start()

        loop = asyncio.get_event_loop()
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=0.1
                    )
                except asyncio.TimeoutError:
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        # Give late-arriving audio chunks a beat to land
                        # before declaring the turn over — reopening the mic
                        # between stragglers is exactly how LITE ends up
                        # hearing its own tail.
                        await asyncio.sleep(0.25)
                        if self.audio_in_queue.empty():
                            self.set_speaking(False)
                            self._turn_done_event.clear()
                    continue

                self.set_speaking(True)

                # Batch all immediately-available chunks into one write to reduce
                # thread-pool round-trips (was one asyncio.to_thread per 50ms slice).
                # Cap at ~200 ms so interrupt() still stops audio within ~200 ms.
                batch = bytearray(chunk)
                while len(batch) < 9600:   # 9600 bytes ≈ 200 ms at 24 kHz / 16-bit mono
                    try:
                        batch.extend(self.audio_in_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                try:
                    # Runs on _audio_out_executor (see __init__), NOT the
                    # shared default executor every tool call also uses —
                    # see the comment there for why that separation matters.
                    _out_bytes = bytes(batch)
                    await loop.run_in_executor(self._audio_out_executor, stream.write, _out_bytes)
                except (RuntimeError, asyncio.CancelledError):
                    break   # executor shutting down — exit cleanly
                if self._aec is not None:
                    try:
                        # Feed the canceller what actually went to the
                        # speakers (downsampled to the mic's 16 kHz) — that
                        # reference is what lets it subtract LITE's own
                        # voice from the mic capture (voice barge-in).
                        self._aec.push_reference(
                            resample_audio(
                                np.frombuffer(_out_bytes, dtype=np.int16),
                                RECEIVE_SAMPLE_RATE,
                                SEND_SAMPLE_RATE,
                            ).tobytes()
                        )
                    except Exception:
                        pass  # a dropped reference frame just weakens cancellation briefly
        except Exception as e:
            print(f"[LITE] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            stream.stop()
            stream.close()

    # ── Morning briefing ────────────────────────────────────────────────────────

    async def _send_startup_briefing(self) -> None:
        """
        Two-phase briefing:
          Phase 1 — instant greeting (no tools) → speech starts in <1s
          Phase 2 — today's vetted opportunity, served from
                    agents/opportunity_pipeline.py's cache-or-refresh: if a
                    result from within the last couple of days already
                    exists, it's delivered instantly here; otherwise a full
                    background pipeline run (research → viability scoring
                    → legal/compliance → financial appraisal) is kicked off
                    and this phase says so honestly rather than pretending
                    to have a vetted opportunity in hand. No timeout is
                    needed here anymore — get_cached_or_refresh() never
                    blocks on the pipeline itself (that was the old 4-
                    second race that made this unreliable); the background
                    run finishes on its own time and shows up on the HUD
                    panel when it's actually ready.
        """
        memory   = load_memory()
        identity = memory.get("identity", {})
        business = memory.get("business", {})

        def _val(k: str) -> str:
            e = identity.get(k, {})
            return (e.get("value", "") if isinstance(e, dict) else str(e)).strip()

        # English is ALWAYS the startup language — the assistant speaks first
        # here, before the user has said anything this session, so there is
        # no live signal to justify anything else. A stored identity.language
        # value is deliberately NOT consulted: it can only have gotten there
        # from a past detected phrase or a stale save, and using it here is
        # exactly the "opens in a language I don't understand" bug. If the
        # user wants LITE to always greet them in another language, that
        # should be a deliberate, separate setting — not silently inferred.
        lang = "English"
        name = _val("name")
        time_str = datetime.now().strftime("%H:%M")

        # Focus the opportunity scan on whatever active ventures are stored,
        # if any — otherwise it defaults to a broad international scan.
        biz_focus = ""
        if business:
            biz_terms = [
                (e.get("value") if isinstance(e, dict) else str(e))
                for e in list(business.values())[:3]
            ]
            biz_focus = ", ".join(t for t in biz_terms if t)

        # Cache check (+ possible background-run kickoff) is fast either
        # way — a cache hit returns immediately, and a cache miss just
        # starts a background thread and returns immediately too. No
        # forced timeout needed anymore.
        loop = asyncio.get_event_loop()
        news_future = loop.run_in_executor(
            None, lambda: _get_opportunity_or_refresh(biz_focus, 2.0, self.ui)
        )

        await asyncio.sleep(0.3)
        if not self.session:
            return

        # ── Phase 1: instant greeting ─────────────────────────────────────────
        lang_clause = f" Respond in {lang}."
        name_clause = f" Address the user as {name}." if name else ""

        # Inject last session context if available — pop removes it so it's never repeated
        last = await asyncio.to_thread(pop_last_session)
        session_clause = ""
        if last:
            try:
                _delta = (datetime.now() - datetime.strptime(last["date"], "%Y-%m-%d")).days
                _when  = "earlier today" if _delta == 0 else ("yesterday" if _delta == 1 else f"{_delta} days ago")
            except Exception:
                _when = "last time"
            session_clause = (
                f" Also briefly and naturally mention that {_when}: {last['summary']}"
            )

        p1 = (
            f"Greet the user warmly and mention it is {time_str}.{session_clause} "
            f"Keep it to 2 short sentences max. Do not call any tools.{lang_clause}{name_clause}"
        )

        # Clear the turn-done event so we can wait for Phase 1 to finish
        if self._turn_done_event:
            self._turn_done_event.clear()

        await self.session.send_client_content(
            turns={"parts": [{"text": p1}]},
            turn_complete=True,
        )
        self.ui.write_log("SYS: Briefing phase 1 (greeting) sent.")

        # ── Phase 2: fire as soon as Phase 1 audio is done ───────────────────
        async def _deliver_news():
            try:
                lang_str = f" Respond in {lang}."

                # Wait for the (fast) cache-or-refresh call and Phase 1
                # turn-complete in parallel — whichever takes longer
                # determines the wait time. No hard timeout on the fetch
                # itself anymore since it's no longer doing the slow work
                # inline (that now happens in the background thread the
                # call may have kicked off).
                news_done   = asyncio.wrap_future(news_future)
                turn_waited = False
                if self._turn_done_event:
                    try:
                        await asyncio.wait_for(self._turn_done_event.wait(), timeout=6.0)
                        turn_waited = True
                    except asyncio.TimeoutError:
                        pass

                # Extra buffer: turn_complete fires when Gemini finishes *generating*
                # Phase 1, but audio may still be playing.  Waiting a beat here
                # prevents Phase 2 audio from arriving while Phase 1 is mid-sentence
                # (which sounds like a "repeated first response" to the user).
                if turn_waited:
                    await asyncio.sleep(0.8)
                else:
                    await asyncio.sleep(1.0)

                try:
                    news_text = await asyncio.wait_for(news_done, timeout=4.0)
                except Exception:
                    news_text = ""

                if not self.session:
                    return

                if news_text:
                    # opportunity_pipeline_agent's cache-or-refresh already
                    # pushed the full text to the HUD panel via player when
                    # it's a cache hit (see get_cached_or_refresh's caller
                    # convention below); for a background-kicked-off run,
                    # the panel updates itself once the pipeline finishes —
                    # this spoken summary is honest about which case it is.
                    self.ui.show_content("TODAY'S OPPORTUNITY", news_text)
                    p2 = (
                        f"[BRIEFING] Opportunity pipeline status:\n{news_text}\n\n"
                        "If this is a completed, vetted opportunity (has a title, "
                        "achievability score, legal/compliance section, and financial "
                        "appraisal), summarise the single strongest point in one or two "
                        "sentences including the next concrete action, then say the full "
                        "breakdown is on screen. If this instead says a pipeline run is "
                        "still in progress or being kicked off, say so plainly in one "
                        "sentence — do not invent opportunity details that aren't in the "
                        f"text above. Do not call any tools.{lang_str}"
                    )
                else:
                    p2 = (
                        "The opportunity pipeline couldn't be reached right now. "
                        f"Let the user know briefly.{lang_str}"
                    )

                await self.session.send_client_content(
                    turns={"parts": [{"text": p2}]},
                    turn_complete=True,
                )
                self.ui.write_log("SYS: Briefing phase 2 (opportunity pipeline) sent.")
            except Exception as e:
                print(f"[Briefing] Phase 2 error: {e}")
                self.ui.write_log(f"SYS: Briefing phase 2 failed: {e}")

        asyncio.create_task(_deliver_news())

    # ── Session memory ──────────────────────────────────────────────────────────

    async def _save_session_summary(self) -> None:
        """Summarise the current session in 1-2 sentences and save to long_term.json."""
        log = self._session_log
        if len(log) < 3:          # need at least one exchange to be worth saving
            return
        self._session_log = []    # reset immediately so the next session starts clean

        # Summaries are written by LITE, not spoken live to the user, and are
        # fed back into future system-prompt memory context — so a bad stored
        # language here would re-poison every future session. Always English.
        lang = "English"

        convo = "\n".join(log[-40:])   # cap at last 40 turns to stay within token budget
        prompt = (
            f"Summarize this conversation in 1-2 sentences in {lang}. "
            "Focus on what the user accomplished or discussed. "
            "Output ONLY the summary text, nothing else:\n\n" + convo
        )
        try:
            from core.ai_client import generate_content as _ai_generate
            resp = await asyncio.to_thread(_ai_generate, prompt, "gemini-3.5-flash")
            summary = (resp.text or "").strip()
            if summary:
                save_session_summary(summary, lang)
        except Exception as e:
            print(f"[Memory] ⚠️ Session summary failed: {e}")

        # Auto-journal the full conversation to today's Obsidian daily note
        # (if a vault is configured) — the quick summary above is for LITE's
        # own memory; this is the full-fidelity, human-searchable record the
        # user actually reads back later. Never lets a journaling hiccup
        # affect the session-save flow.
        try:
            from actions.obsidian import append_to_daily_note, has_vault_configured
            if has_vault_configured():
                journal_text = "\n".join(f"- {line}" for line in log[-60:])
                msg = await asyncio.to_thread(append_to_daily_note, journal_text)
                self.ui.write_log(f"SYS: {msg}")
        except Exception as e:
            print(f"[Obsidian] ⚠️ Auto-journal failed: {e}")

    async def _supervise(self, label: str, coro) -> None:
        """Shield for helper tasks that share the Live TaskGroup with the
        audio pipeline. If any of them raises, the TaskGroup cancels
        mic/_listen_audio/_receive_audio/_play_audio and forces a
        mid-conversation reconnect — which sounded like LITE 'breaking'
        while speaking. Their loops now swallow per-iteration errors; this
        catches whatever still escapes so the worst case is a stopped
        helper, never a broken voice session."""
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[{label}] ⚠️ Background task stopped: {e}")

    # ── System monitor ──────────────────────────────────────────────────────────

    async def _run_system_monitor(self) -> None:
        """Background task: voice alerts when metrics exceed thresholds.

        Errors are swallowed per-cycle: this loop shares the Live TaskGroup
        with the mic/receive/play tasks, so anything raised here used to tear
        the whole voice session down mid-conversation (seen in the console
        log as an ExceptionGroup + surprise "Reconnecting in 3s...")."""
        while True:
            await asyncio.sleep(10)
            try:
                alert = await asyncio.to_thread(self._sys_monitor.check)
            except asyncio.CancelledError:
                raise
            except RuntimeError as e:
                if "cannot schedule new futures after shutdown" in str(e):
                    return  # event loop shutting down (window closed) — exit quietly
                print(f"[Monitor] ⚠️ System check failed: {e}")
                continue
            except Exception as e:
                print(f"[Monitor] ⚠️ System check failed: {e}")
                continue
            if not alert or not self.session:
                continue
            # Don't interrupt an active conversation
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking or (time.monotonic() - self._last_user_speech) < 10:
                continue
            try:
                await self.session.send_client_content(
                    turns={"parts": [{"text": alert}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[Monitor] ⚠️ Could not send alert: {e}")

    # ── Background monitor ──────────────────────────────────────────────────────

    async def _run_background_monitor(self) -> None:
        """Check user-configured topics once per day; speak alerts when new headlines appear."""
        await asyncio.sleep(300)          # wait 5 min after startup before first check
        while True:
            if self.session:
                # Don't interrupt if user spoke recently or LITE is mid-sentence
                with self._speaking_lock:
                    speaking = self._is_speaking
                recent_speech = (time.monotonic() - self._last_user_speech) < 30
                if not speaking and not recent_speech:
                    try:
                        alerts = await asyncio.to_thread(monitor_check_all)
                        # Background alerts are LITE-initiated, same as the startup
                        # briefing — no live user speech to justify anything but English.
                        lang = "English"
                        for alert in alerts:
                            msg = (
                                f"{alert}\n\n"
                                f"Inform the user about this development naturally in {lang}. "
                                "One brief sentence only."
                            )
                            await self.session.send_client_content(
                                turns={"parts": [{"text": msg}]},
                                turn_complete=True,
                            )
                            self.ui.write_log("SYS: Monitor alert sent.")
                            await asyncio.sleep(6)   # gap between consecutive alerts
                    except Exception as e:
                        print(f"[Monitor] ⚠️ Background check error: {e}")
            await asyncio.sleep(1800)     # check every 30 minutes

    # ── Proactive mode ──────────────────────────────────────────────────────────

    async def _run_proactive_mode(self) -> None:
        """
        Background task: periodically checks if the user has been silent long enough,
        then hands time + memory context to Gemini so it can decide what (if anything)
        to say proactively. No hardcoded rules — Gemini makes the call.
        """
        while True:
            await asyncio.sleep(60)   # evaluate once per minute

            if not self.session:
                continue

            # Per-cycle error swallowing — same reasoning as
            # _run_system_monitor: a transient failure here must never cancel
            # the mic/receive/play tasks that share this TaskGroup.
            try:
                with self._speaking_lock:
                    speaking = self._is_speaking
                if speaking:
                    continue

                if not self._proactive.should_trigger(self._last_user_speech):
                    continue

                self._proactive.mark_triggered()

                memory       = await asyncio.to_thread(load_memory)
                monitors     = await asyncio.to_thread(list_monitors)
                recent_turns = self._session_log[-8:] if self._session_log else []
                prompt = self._proactive.build_prompt(
                    memory              = memory,
                    monitors            = monitors or None,
                    recent_turns        = recent_turns or None,
                    authorized_language = self._authorized_language,
                )
                await self.session.send_client_content(
                    turns={"parts": [{"text": prompt}]},
                    turn_complete=True,
                )
                self.ui.write_log("SYS: Proactive check-in.")
            except asyncio.CancelledError:
                raise
            except RuntimeError as e:
                if "cannot schedule new futures after shutdown" in str(e):
                    return  # event loop shutting down (window closed) — exit quietly
                print(f"[Proactive] ⚠️ {e}")
            except Exception as e:
                print(f"[Proactive] ⚠️ {e}")

    # ── Phone audio relay ────────────────────────────────────────────────────────

    def _get_phone_stt(self):
        """Lazily load a Whisper STT instance shared across fallback-mode phone
        utterances. Separate from FallbackVoice's own instance (which handles
        the local mic) — some duplicate memory if both are active at once,
        but keeps this change isolated rather than threading a shared
        instance through two independently-lifecycled subsystems."""
        if self._phone_stt is None:
            from core.stt import WhisperSTT
            try:
                with open(API_CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}
            self._phone_stt = WhisperSTT(model_name=cfg.get("fallback_stt_model", "base"))
        return self._phone_stt

    async def _synthesize_reply_audio_mp3(self, text: str) -> bytes | None:
        """Best-effort TTS for phone replies — EdgeTTS returns MP3 bytes
        directly and is trivially playable via an HTML5 <audio> element,
        so it's used here regardless of the desktop's configured TTS engine
        (Kokoro/ElevenLabs return formats that need extra handling this
        path doesn't need). Returns None on any failure — a missing audio
        reply should never block delivering the text reply."""
        if not text.strip():
            return None
        try:
            from core.tts import EdgeTTSEngine
            return await EdgeTTSEngine()._synth(text)
        except Exception as e:
            print(f"[PhoneVoice] TTS synth failed (reply stays text-only): {e}")
            return None

    async def _relay_phone_audio(self) -> None:
        """Consume phone-mic PCM16 chunks from the dashboard queue.

        Always running (started once in run(), independent of whether a
        Gemini Live session is up) — previously this task only existed
        inside the Live-session TaskGroup, so phone audio silently went
        nowhere the moment Live wasn't connected (fallback voice mode, no
        Gemini key, quota exhausted, or simply before the first connect).
        Text commands never had this problem because
        _process_dashboard_commands has always been a top-level task; this
        gives phone voice the same guarantee.

        Two modes, decided per-chunk:
          - Gemini Live session active → forward raw PCM straight through,
            exactly as before (zero behaviour change for the working case).
          - No Live session → buffer the utterance locally, transcribe with
            Whisper on a silence gap, and push the text into the same
            dashboard command queue text commands already use — so it gets
            a real reply via the existing Gemini-or-fallback text path,
            visible in the phone's chat log. A best-effort spoken reply is
            also synthesized and sent back to the phone over the chat
            websocket, since "nothing audible came back" was as much the
            complaint as "nothing happened at all".
        """
        q = self._dashboard._phone_audio_queue
        SILENCE_GAP_S     = 0.9   # matches core.fallback_voice.SILENCE_MS
        MAX_UTTERANCE_S   = 20
        SAMPLE_RATE       = 16000
        BYTES_PER_SEC     = SAMPLE_RATE * 2   # int16 mono

        buffer = bytearray()
        buffer_started_at = None

        async def _flush_buffer():
            nonlocal buffer, buffer_started_at
            if not buffer:
                return
            pcm_bytes = bytes(buffer)
            buffer = bytearray()
            buffer_started_at = None

            audio_f32 = (
                np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            )
            if len(audio_f32) < SAMPLE_RATE * 0.3:
                return  # too short to be real speech — drop rather than waste a whisper call

            try:
                stt = await asyncio.to_thread(self._get_phone_stt)
                text = await asyncio.to_thread(stt.transcribe, audio_f32)
            except Exception as e:
                print(f"[PhoneVoice] Transcription failed: {e}")
                if self._dashboard:
                    await self._dashboard.broadcast({
                        "type": "log", "speaker": "lite",
                        "text": "Sir, I couldn't transcribe that — offline speech "
                                 "recognition may not be installed (faster-whisper).",
                        "ts": datetime.now().isoformat(),
                    })
                return

            text = text.strip()
            if not text:
                return

            self.ui.write_log(f"[Phone voice]: {text}")
            # Reuses the exact path text commands already use — Gemini Live if
            # available (won't be, in this branch, since we only got here
            # because self.session was None), otherwise the fallback text
            # provider. Also handles the user-message broadcast for us.
            self._dashboard._command_queue.put_nowait({"text": text})

        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=SILENCE_GAP_S)
            except asyncio.TimeoutError:
                self._phone_active = False
                await _flush_buffer()
                continue

            self._phone_active = True

            if self.session:
                # Gemini Live is up — unchanged behaviour, straight passthrough.
                # The echo tail applies here too: the phone's mic sits in the
                # same room as LITE's speakers, so phone audio must also stay
                # quiet until LITE's own voice has fully decayed.
                if not self.ui.muted and self._echo_tail_passed():
                    try:
                        self.out_queue.put_nowait(item)
                    except asyncio.QueueFull:
                        pass
                continue

            # No Live session — accumulate for local transcription instead.
            if buffer_started_at is None:
                buffer_started_at = time.time()
            buffer.extend(item["data"])
            if (len(buffer) / BYTES_PER_SEC) >= MAX_UTTERANCE_S:
                await _flush_buffer()

    def _on_phone_connected(self) -> None:
        self.ui.write_log("SYS: Phone connected via Remote Dashboard.")
        self.ui.notify_phone_connected()

    # ── dashboard command relay ─────────────────────────────────────────────

    async def _process_dashboard_commands(self) -> None:
        while True:
            try:
                item = await asyncio.wait_for(
                    self._dashboard._command_queue.get(), timeout=0.5
                )
                if not item:
                    continue

                # Typed dashboard commands are plain strings; phone-voice
                # utterances (transcribed in _relay_phone_audio) are tagged
                # dicts so we know to also speak the reply back to the phone.
                if isinstance(item, dict):
                    text, from_voice = item.get("text", ""), True
                else:
                    text, from_voice = item, False
                if not text:
                    continue

                if self._dashboard:
                    await self._dashboard.broadcast({
                        "type": "log", "speaker": "user", "text": text,
                        "ts": datetime.now().isoformat(),
                    })

                # Wait up to 8s for a Gemini Live session to become ready after a wake
                for _ in range(80):
                    if self.session:
                        break
                    await asyncio.sleep(0.1)

                if self.session:
                    await self.session.send_client_content(
                        turns={"parts": [{"text": text}]},
                        turn_complete=True,
                    )
                    self.ui.write_log(f"[Web]: {text}")
                else:
                    # No Gemini Live session — most commonly because we're running
                    # in fallback voice mode (or no voice engine configured at all).
                    # This used to just print "Dropped command" and go nowhere,
                    # silently failing every phone command in that state. Route it
                    # through the same text fallback chain fallback_voice uses
                    # instead, so a phone command always gets an actual answer.
                    self.ui.write_log(f"[Web]: {text} (no live session — using fallback)")
                    try:
                        from core.ai_client import generate_content
                        reply = await asyncio.to_thread(
                            generate_content,
                            f"You are {self._asst_name}, an efficient, professional AI "
                            f"assistant. Address the user as 'sir' unless told otherwise. "
                            f"Keep the reply concise.\n\nUser: {text}\n{self._asst_name}:",
                        )
                        reply_text = reply.text.strip()
                    except Exception as e:
                        reply_text = f"Sir, I couldn't reach any configured AI provider right now ({e})."

                    self.ui.write_log(f"{self._asst_name}: {reply_text}")
                    if self._dashboard:
                        await self._dashboard.broadcast({
                            "type": "log", "speaker": "lite", "text": reply_text,
                            "ts": datetime.now().isoformat(),
                        })
                        if from_voice:
                            # Best-effort spoken reply for phone voice, so a
                            # fallback-mode voice conversation is actually a
                            # conversation and not just a one-way transcript.
                            audio_bytes = await self._synthesize_reply_audio_mp3(reply_text)
                            if audio_bytes:
                                await self._dashboard.broadcast({
                                    "type": "tts_audio",
                                    "mime": "audio/mpeg",
                                    "audio_b64": base64.b64encode(audio_bytes).decode("ascii"),
                                    "ts": datetime.now().isoformat(),
                                })
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                print(f"[Dashboard] Command error: {e}")
                await asyncio.sleep(0.5)

    # ── main loop ───────────────────────────────────────────────────────────

    async def run(self):
        self._loop = asyncio.get_event_loop()

        # Start dashboard (optional — needs: pip install fastapi "uvicorn[standard]" cryptography)
        try:
            from dashboard.server import DashboardServer
            from core.step_bus    import register as _register_step_bus
            self._dashboard = DashboardServer()
            self._dashboard.set_connect_callback(self._on_phone_connected)
            self._dashboard.set_file_callback(self._on_file_uploaded)
            # Wires actions/code_agent.py's live step events (Checkpoint ->
            # Delegate -> Verify -> Rollback/Commit) through to the Remote
            # Dashboard's "Steps" tab. code_agent runs in a worker thread
            # (run_in_executor), so step_bus needs this loop reference to
            # broadcast back onto it safely.
            _register_step_bus(self._dashboard, self._loop)
            asyncio.create_task(self._dashboard.serve())
            # Both run for the whole app lifetime, not just inside an active
            # Gemini Live session — _relay_phone_audio used to only exist
            # inside the Live-session TaskGroup below, so phone voice input
            # silently went nowhere the moment Live wasn't connected. It now
            # decides per-chunk whether to forward to Live or transcribe
            # locally via fallback, so it needs to always be running, exactly
            # like _process_dashboard_commands already was.
            asyncio.create_task(self._process_dashboard_commands())
            asyncio.create_task(self._relay_phone_audio())
        except Exception as e:
            print(f"[Dashboard] Disabled: {e}")
            self._dashboard = None

        # Quiet startup update check — read-only, logs a note if something's
        # available, never speaks or interrupts. Does nothing if the app
        # folder isn't a git checkout (e.g. a plain zip download).
        def _startup_update_check():
            try:
                from actions.self_update import check_for_updates
                status = check_for_updates()
                if status.get("ok") and status.get("behind"):
                    n = status["behind"]
                    self.ui.write_log(
                        f"SYS: {n} update{'s' if n != 1 else ''} available on GitHub — "
                        f"say 'check for updates' to review, or 'update yourself' to pull."
                    )
            except Exception:
                pass  # silent — this is a nicety, never worth surfacing an error for
        threading.Thread(target=_startup_update_check, daemon=True).start()

        _voice_unconfigured_notified = False
        self._fallback_thread     = None
        self._fallback_stop_event = None

        def _stop_fallback_voice(timeout: float = 2.0):
            """Signals the fallback voice thread to stop and gives it a
            moment to release the microphone before Gemini claims it."""
            if self._fallback_thread and self._fallback_thread.is_alive():
                self._fallback_stop_event.set()
                self._fallback_thread.join(timeout=timeout)
            self._fallback_thread = None

        def _start_fallback_voice_if_idle():
            """Starts the degraded fallback voice loop (Claude/custom-backed,
            no tool use) if it isn't already running and a fallback text
            provider is configured. No-op otherwise — caller keeps idling
            normally in that case."""
            if self._fallback_thread and self._fallback_thread.is_alive():
                return
            from core.fallback_voice import FallbackVoice, _has_fallback_text_provider
            if not _has_fallback_text_provider():
                return
            self._fallback_stop_event = threading.Event()
            fv = FallbackVoice(self.ui, assistant_name=self._asst_name)
            self._fallback_thread = threading.Thread(
                target=fv.run, args=(self._fallback_stop_event,), daemon=True
            )
            self._fallback_thread.start()

        while True:
            # Voice conversation needs a Gemini key. Rather than hammering a
            # connection attempt that's guaranteed to fail (and spamming
            # reconnect/backoff every few seconds), idle quietly — dashboard,
            # search, news, and text-based tools all work fine without this.
            # If a fallback text provider (Claude / custom) is configured,
            # run a degraded conversational voice loop instead of pure text.
            _key = _get_api_key()
            if not _key:
                if not _voice_unconfigured_notified:
                    _voice_unconfigured_notified = True
                    self.ui.write_log(
                        "SYS: No Gemini key configured — voice conversation is "
                        "unavailable. Everything else (search, news, dashboard, "
                        "text commands) still works. Add a key anytime in Settings "
                        "to enable voice."
                    )
                    self.ui.set_state("SLEEPING")
                _start_fallback_voice_if_idle()
                await asyncio.sleep(5)
                continue
            _voice_unconfigured_notified = False
            _stop_fallback_voice()

            try:
                print("[LITE] Connecting...")
                self.ui.set_state("THINKING")
                config = self._build_config()

                # Fresh client on every reconnect — avoids stale HTTP session state
                client = genai.Client(
                    api_key=_key,
                    http_options={"api_version": "v1beta"}
                )

                async with (
                    client.aio.live.connect(model=LIVE_MODEL, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session          = session
                    # Keep buffered audio bounded so stale speech does not pile up and
                    # cause the next command to sound intermittent while the network
                    # catches up. Sized for ~3 s of jitter at ~128 ms/frame: a normal
                    # network blip absorbs without dropping anything; only sustained
                    # backpressure triggers a drop, and _queue_live_audio drops the
                    # oldest queued item so the freshest audio always wins.
                    self.audio_in_queue   = asyncio.Queue(maxsize=200)
                    self.out_queue        = asyncio.Queue(maxsize=150)
                    self._turn_done_event = asyncio.Event()

                    # Reset transient state that must not carry over from a previous session
                    self._pending_vision       = None
                    self._vision_cam_active    = False
                    self._vision_close_pending = False
                    self._vision_busy          = False
                    self._vision_last_time     = 0.0
                    self._interrupted          = False

                    if self._pending_file_notice:
                        self._send_text_to_session(self._pending_file_notice)
                        self._pending_file_notice = None

                    print("[LITE] Connected.")
                    _stop_fallback_voice()
                    self.ui.set_state("LISTENING")
                    self.ui.write_log("SYS: LITE online.")

                    if self._dashboard:
                        await self._dashboard.broadcast({"type": "status", "state": "active"})

                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    # Helper loops run shielded — see _supervise(). A hiccup in
                    # any of them must never cancel the audio tasks above
                    # (that was the mid-speech "breaks and reconnects" bug).
                    tg.create_task(self._supervise("SystemMonitor", self._run_system_monitor()))
                    tg.create_task(self._supervise("BackgroundMonitor", self._run_background_monitor()))
                    tg.create_task(self._supervise("Proactive", self._run_proactive_mode()))
                    # _relay_phone_audio is NOT started here — it's a
                    # top-level task started once in run(), independent of
                    # this Live session's lifetime (see comment there).

                    # Morning briefing — fires once per process launch (if enabled)
                    if not self._briefing_sent and get_brief_enabled():
                        self._briefing_sent = True
                        tg.create_task(self._supervise("Briefing", self._send_startup_briefing()))

            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except BaseException as e:
                # Catches both Exception and BaseExceptionGroup (Python 3.11+
                # TaskGroup raises BaseExceptionGroup when tasks are cancelled
                # externally, which `except Exception` would miss, letting the
                # exception escape the while-loop and causing asyncio.run() to
                # start shutdown — resulting in "executor after shutdown" errors).
                err_str = str(e)
                print(f"[LITE] Error ({type(e).__name__}): {e}")
                traceback.print_exc()

                # Invalid API key — stop hammering the API, prompt re-configuration
                if "API key not valid" in err_str or "1007" in err_str:
                    self.ui.write_log("ERR: API key invalid — please re-enter your key.")
                    self.ui.set_state("SLEEPING")
                    self.ui.prompt_reconfig()
                    while not self.ui._win._ready:
                        await asyncio.sleep(1)
                    print("[LITE] New API key saved — reconnecting...")
                    _conn_backoff = 3
                    continue

                # Quota exhausted / rate-limited — Gemini itself is fine, just
                # temporarily unavailable. Drop into the degraded fallback
                # voice loop (if a fallback provider is configured) while
                # continuing to retry Gemini in the background at normal
                # backoff cadence — first successful reconnect stops it.
                is_quota_err = any(k in err_str for k in (
                    "RESOURCE_EXHAUSTED", "429", "quota", "rate limit", "Quota exceeded",
                ))
                if is_quota_err:
                    self.ui.write_log(
                        "ERR: Gemini quota exhausted — falling back to degraded voice "
                        "(if configured) while retrying in the background."
                    )
                    _start_fallback_voice_if_idle()
                    _conn_backoff = min(getattr(self, "_conn_backoff", 3) * 2, 60)
                    self._conn_backoff = _conn_backoff

                # Network / timeout errors — log clearly and back off
                is_net_err = any(k in err_str for k in (
                    "TimeoutError", "timed out", "getaddrinfo", "CancelledError",
                    "ConnectionRefusedError", "OSError", "Cannot connect",
                ))
                if is_net_err:
                    _conn_backoff = min(getattr(self, "_conn_backoff", 3) * 2, 60)
                    self._conn_backoff = _conn_backoff
                    self.ui.write_log(
                        f"NET: Could not connect — retrying in {_conn_backoff}s. "
                        "(A VPN may be required)"
                    )
                elif not is_quota_err:
                    self._conn_backoff = 3
            finally:
                self.session = None
                # Only save if there was a real conversation (≥3 turns)
                if len(self._session_log) >= 3:
                    asyncio.create_task(self._save_session_summary())

            self.set_speaking(False)
            self.ui.set_state("SLEEPING")

            if self._dashboard:
                await self._dashboard.broadcast({"type": "status", "state": "sleeping"})

            delay = getattr(self, "_conn_backoff", 3)
            print(f"[LITE] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)

def _start_single_instance_listener(raise_callback):
    """Background thread: wakes up and invokes raise_callback() whenever a
    second launch attempt pings the lock socket held by this instance."""
    if _single_instance_socket is None:
        # Raise channel unavailable (an unrelated app holds the port). The
        # Windows mutex still blocks duplicate launches — later clicks just
        # can't raise this window.
        return
    def _serve():
        while True:
            try:
                conn, _ = _single_instance_socket.accept()
            except OSError:
                return  # socket closed — process is shutting down
            try:
                conn.settimeout(1)
                conn.recv(16)
            except OSError:
                pass
            finally:
                conn.close()
            raise_callback()

    threading.Thread(target=_serve, daemon=True).start()


def _raise_lite_window(ui):
    """Bring LITE's window to the foreground when a duplicate launch pings
    us. Runs on the Qt main thread — the listener schedules it via
    ui.root.after (see _RootShim.after), and the actual window work lives in
    LiteUI.raise_to_front()."""
    try:
        ui.raise_to_front()
    except Exception:
        pass  # never let a raise hiccup take the listener thread down


def main():
    ui = LiteUI("face.png")

    # Raise our window instead of doing nothing when a second launch
    # attempt (e.g. an accidental double-click) pings our lock socket.
    _start_single_instance_listener(lambda: ui.root.after(0, _raise_lite_window, ui))

    # Global mute hotkey — works system-wide, not just while LITE's window is
    # focused, so it's useful for the actual use case (muting to have an
    # unrelated conversation elsewhere). No-ops cleanly if pynput isn't
    # installed; the in-app mute button and F4 still work regardless.
    from core.global_hotkey import start_global_mute_hotkey
    start_global_mute_hotkey(ui)

    # The one genuinely proactive background loop in the app — checks Gmail
    # on its own at 10am and 4pm every day and drafts replies for anything
    # that needs one, independent of user activity (see
    # agents/scheduling_docs_agent.py). Silently does nothing per cycle
    # until config/google_client_secret.json exists (the one-time Google
    # Cloud setup that can't be automated).
    from agents.scheduling_docs_agent import start_gmail_scan_scheduler
    start_gmail_scan_scheduler(ui)

    def runner():
        ui.wait_for_api_key()
        lite = LiteLive(ui)
        ui.set_live_session(lite)  # lets the window's closeEvent trigger a
                                    # graceful session save on normal window close
        try:
            asyncio.run(lite.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()

if __name__ == "__main__":
    main()