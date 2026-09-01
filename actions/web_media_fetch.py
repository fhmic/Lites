#web_media_fetch.py
"""
Fetches an actual image (or finds an embeddable video) from the web and
displays it directly on LITE's HUD content panel — no browser window opens.

This closes a real gap: actions/web_search.py only ever returns synthesized
text/snippets (grounded search or raw DDG results) — nothing before this
module could put actual image bytes, or a playable video, on screen. "Find
a picture of X and show it to me" used to get answered with a text summary
about pictures of X, because no tool in LITE actually fetched one.

Images: searched via DuckDuckGo's image search (ddgs — the same dependency
web_search.py already uses, no new API key needed), downloaded locally into
hologram/media_cache/, and served through the hologram's own local HTTP
server (ui.py's _HologramHTTPServer) so the panel's <img> tag just points at
a same-origin relative path — no separate network/CORS concerns for the
webview itself.

Videos: DuckDuckGo's video search returns pointers into video *platforms*
(YouTube, Vimeo, etc.), not raw downloadable files — those platforms don't
serve hotlinkable video bytes, and re-hosting someone else's video would be
a copyright problem regardless. So video results are embedded (iframe, the
same approach the `map` content kind already uses for OpenStreetMap) rather
than downloaded — you get an actually-playing video without pretending to
"download" something that was never meant to be downloaded.
"""
import re
import sys
import time
import uuid
from pathlib import Path


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = _get_base_dir()
MEDIA_CACHE_DIR = BASE_DIR / "hologram" / "media_cache"

MAX_BYTES        = 25 * 1024 * 1024  # 25MB safety cap — a picture, not a movie file
MAX_CANDIDATES   = 6                 # how many search results we'll try before giving up
KEEP_CACHED      = 40                # oldest files beyond this are pruned after each fetch


def _safe_ext(url: str, default: str = ".jpg") -> str:
    m = re.search(r"\.(jpg|jpeg|png|gif|webp)(?:\?|#|$)", url, re.I)
    return f".{m.group(1).lower()}" if m else default


def _search_images(query: str) -> list[str]:
    """Direct image URLs via DuckDuckGo image search — result key is
    literally 'image' (verified against ddgs's own DuckduckgoImages engine),
    not 'url' or 'src'."""
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS
    with DDGS() as ddgs:
        results = ddgs.images(query, max_results=MAX_CANDIDATES)
    return [r["image"] for r in results if r.get("image")]


def _search_video_embed(query: str) -> tuple[str, str] | None:
    """Returns (embed_url, title) for the first video result, or None.
    DDG's video engine gives 'embed_url' directly — no need to construct one."""
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS
    with DDGS() as ddgs:
        results = ddgs.videos(query, max_results=MAX_CANDIDATES)
    for r in results:
        embed = r.get("embed_url")
        if embed:
            return embed, r.get("title", query)
    return None


def _download_image(url: str, dest: Path):
    import requests
    resp = requests.get(
        url, timeout=15, stream=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; LITE/1.0)"},
    )
    resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "")
    if ctype and not ctype.startswith("image/"):
        raise ValueError(f"URL didn't return an image (got {ctype or 'unknown type'})")
    size = 0
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(8192):
            size += len(chunk)
            if size > MAX_BYTES:
                dest.unlink(missing_ok=True)
                raise ValueError("Image exceeded the 25MB safety cap — skipped.")
            f.write(chunk)


def _prune_cache(keep: int = KEEP_CACHED):
    try:
        files = sorted(MEDIA_CACHE_DIR.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files[keep:]:
            f.unlink(missing_ok=True)
    except Exception:
        pass  # cache pruning is best-effort housekeeping, never worth failing the request over


def _push(player, title: str, kind: str, payload: dict):
    if player is None or not hasattr(player, "show_content"):
        return
    try:
        player.show_content(title, "", kind=kind, payload=payload)
    except TypeError:
        try:
            player.show_content(title, f"[{kind}] {payload}")
        except Exception:
            pass
    except Exception:
        pass


def web_media_fetch(parameters: dict, player=None, speak=None) -> str:
    p       = parameters or {}
    query   = (p.get("query") or "").strip()
    url     = (p.get("url") or "").strip()
    kind    = (p.get("kind") or "image").strip().lower()
    if kind not in ("image", "video"):
        kind = "image"

    if not query and not url:
        return "Tell me what to find — a description ('a red Ferrari'), or a direct URL, sir."

    MEDIA_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if kind == "video":
        if url:
            # A direct URL for video means "embed this page", same as a
            # search hit — we still don't download someone else's video file.
            _push(player, query or "VIDEO", "video", {"embed_url": url, "caption": query or url})
            return f"Showing that video on the HUD now, sir."
        try:
            found = _search_video_embed(query)
        except Exception as e:
            return f"Couldn't search for a video: {e}"
        if not found:
            return f"No embeddable video found for '{query}', sir."
        embed_url, title = found
        _push(player, title or query, "video", {"embed_url": embed_url, "caption": title})
        return f"Found a video for '{query}', sir — playing it on the HUD now."

    # kind == "image"
    candidates = [url] if url else []
    if not candidates:
        try:
            candidates = _search_images(query)
        except Exception as e:
            return f"Couldn't search for that image: {e}"
        if not candidates:
            return f"No image results found for '{query}', sir."

    last_err = None
    for candidate_url in candidates[:MAX_CANDIDATES]:
        dest = MEDIA_CACHE_DIR / f"image_{uuid.uuid4().hex[:10]}{_safe_ext(candidate_url)}"
        try:
            _download_image(candidate_url, dest)
        except Exception as e:
            last_err = e
            continue
        _prune_cache()
        local_path = f"media_cache/{dest.name}"
        _push(player, query or "IMAGE", "image", {"url": local_path, "caption": query or candidate_url})
        return f"Found an image for '{query or url}', sir — showing it on the HUD now."

    return f"Tried {len(candidates[:MAX_CANDIDATES])} result(s) but couldn't download a usable image: {last_err}"
