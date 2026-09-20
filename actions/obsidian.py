#obsidian.py
"""
Journals conversations into an Obsidian vault — one markdown file per day,
so past discussions are easy to find and read later (in Obsidian itself,
or via LITE's own search_notes). Also supports general note operations
(create/append/read/search) so the vault can be used as working memory,
not just a conversation log.

Configure the vault in config/api_keys.json:
  "obsidian_vault_path": "C:/Users/you/Documents/MyVault"

Journal entries are written under a dedicated "LITE Journal/" subfolder
inside the vault — never mixed into your own notes or your own Daily Notes
folder, so this can never clobber or clutter what you already keep there.
One file per calendar day: LITE Journal/2026-08-19.md

Daily business-opportunity briefs get their own dedicated subfolder,
"Business Opportunities/", separate from the conversation journal — this
runs automatically from code every time a brief is generated (see
save_opportunities_brief), so it never depends on the model remembering a
standing instruction, and it never has to guess a filename or a location.
One file per calendar day: Business Opportunities/2026-08-19.md
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR             = get_base_dir()
API_CONFIG_PATH       = BASE_DIR / "config" / "api_keys.json"
JOURNAL_SUBDIR        = "LITE Journal"
OPPORTUNITIES_SUBDIR  = "Business Opportunities"


def _load_config() -> dict:
    try:
        return json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _vault_path() -> Path | None:
    raw = (_load_config().get("obsidian_vault_path") or "").strip()
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_dir() else None


def has_vault_configured() -> bool:
    return _vault_path() is not None


def get_vault_path() -> Path | None:
    """Public accessor for the configured vault root — used by
    actions/file_controller.py so generic "create a file"/"write" requests
    default to the vault instead of guessing a folder. Returns None if no
    vault is configured (matches has_vault_configured())."""
    return _vault_path()


def _journal_dir() -> Path | None:
    vault = _vault_path()
    if not vault:
        return None
    d = vault / JOURNAL_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _opportunities_dir() -> Path | None:
    vault = _vault_path()
    if not vault:
        return None
    d = vault / OPPORTUNITIES_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _resolve_date(date_str: str = "") -> datetime:
    date_str = (date_str or "").strip().lower()
    if not date_str or date_str == "today":
        return datetime.now()
    if date_str == "yesterday":
        return datetime.now() - timedelta(days=1)
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        return datetime.now()


def _daily_note_path(date: datetime | None = None) -> Path | None:
    jdir = _journal_dir()
    if not jdir:
        return None
    date = date or datetime.now()
    return jdir / f"{date.strftime('%Y-%m-%d')}.md"


# ── Journal operations ──────────────────────────────────────────────────────

def append_to_daily_note(text: str, heading: str | None = None) -> str:
    """
    Appends a timestamped entry to today's journal file, creating it (with
    a title) if it doesn't exist yet. Returns a status message — never
    raises, since this gets called automatically after every session and
    a journaling hiccup should never interrupt the app.
    """
    if not text.strip():
        return "Nothing to log."
    path = _daily_note_path()
    if not path:
        return "No Obsidian vault configured."

    try:
        now = datetime.now()
        if not path.exists():
            path.write_text(f"# {now.strftime('%A, %B %d, %Y')}\n\n", encoding="utf-8")

        section = heading or f"## {now.strftime('%H:%M')}"
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n{section}\n{text.strip()}\n")
        return f"Logged to {path.name}."
    except Exception as e:
        return f"Journal write failed: {e}"


def save_opportunities_brief(text: str, focus: str = "") -> str:
    """
    Writes today's business-opportunities brief to its own dated file under
    Business Opportunities/YYYY-MM-DD.md — deterministic path and filename,
    called directly from code right after a brief is generated. Unlike the
    conversation journal, this OVERWRITES rather than appends: a same-day
    rerun means a fresher brief, not a second copy to disconnect and confuse
    later search results. Never raises — a save failure should never break
    the briefing flow itself.
    """
    if not text.strip():
        return "Nothing to save."
    odir = _opportunities_dir()
    if not odir:
        return "No Obsidian vault configured."

    try:
        now = datetime.now()
        path = odir / f"{now.strftime('%Y-%m-%d')}.md"
        title = f"# {now.strftime('%A, %B %d, %Y')} — Business & Income Opportunities\n"
        if focus:
            title += f"*Focus: {focus}*\n"
        path.write_text(f"{title}\n{text.strip()}\n", encoding="utf-8")
        return f"Saved to {path.name}."
    except Exception as e:
        return f"Opportunities brief save failed: {e}"


def read_daily_note(date_str: str = "") -> str:
    date = _resolve_date(date_str)
    path = _daily_note_path(date)
    if not path:
        return "No Obsidian vault configured."
    if not path.exists():
        return f"No journal entry for {date.strftime('%Y-%m-%d')}."
    try:
        return path.read_text(encoding="utf-8")
    except Exception as e:
        return f"Couldn't read that entry: {e}"


def list_recent_notes(days: int = 14) -> list[str]:
    jdir = _journal_dir()
    if not jdir:
        return []
    out = []
    for i in range(days):
        d = datetime.now() - timedelta(days=i)
        p = jdir / f"{d.strftime('%Y-%m-%d')}.md"
        if p.exists():
            out.append(p.stem)
    return out


def search_notes(query: str, max_results: int = 8, scope: str = "journal") -> list[dict]:
    """
    scope='journal' (default) searches only LITE's own journal subfolder —
    fast, and won't dredge up unrelated personal notes you keep elsewhere
    in the vault. scope='vault' searches everything.
    """
    vault = _vault_path()
    if not vault:
        return []
    root = _journal_dir() if scope == "journal" else vault
    if not root:
        return []

    query_lower = query.lower()
    results = []
    for path in sorted(root.rglob("*.md"), reverse=True):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        idx = text.lower().find(query_lower)
        if idx == -1:
            continue
        start = max(0, idx - 80)
        end   = min(len(text), idx + len(query) + 80)
        snippet = " ".join(text[start:end].split())
        results.append({"file": path.stem, "snippet": snippet})
        if len(results) >= max_results:
            break
    return results


def create_note(name: str, content: str = "", folder: str = "") -> str:
    vault = _vault_path()
    if not vault:
        return "No Obsidian vault configured."
    name = name.strip()
    if not name.endswith(".md"):
        name += ".md"
    target_dir = (vault / folder) if folder else vault
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / name
        if path.exists():
            return f"A note named {name} already exists — nothing overwritten."
        path.write_text(content, encoding="utf-8")
        return f"Created {name}."
    except Exception as e:
        return f"Couldn't create that note: {e}"


# ── Public entry point ───────────────────────────────────────────────────────

def obsidian_notes(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    p      = parameters or {}
    mode   = (p.get("mode") or "search").strip().lower()
    query  = (p.get("query") or "").strip()
    date   = (p.get("date") or "").strip()
    text   = (p.get("text") or "").strip()
    name   = (p.get("name") or "").strip()
    folder = (p.get("folder") or "").strip()
    scope  = (p.get("scope") or "journal").strip().lower()

    if not has_vault_configured():
        return (
            "No Obsidian vault configured yet, sir — set \"obsidian_vault_path\" "
            "to your vault's folder path in config/api_keys.json."
        )

    try:
        if mode == "read":
            return read_daily_note(date)

        if mode == "append":
            if not text:
                return "Please tell me what to log, sir."
            return append_to_daily_note(text)

        if mode == "create":
            if not name:
                return "Please give the note a name, sir."
            return create_note(name, text, folder)

        if mode == "list":
            recent = list_recent_notes(14)
            return ("Recent journal entries: " + ", ".join(recent)) if recent else "No journal entries yet."

        # mode == "search" (default)
        if not query:
            return "What should I search your notes for, sir?"
        results = search_notes(query, scope=scope)
        if not results:
            return f"No notes found mentioning '{query}'."
        lines = [f"Found {len(results)} note(s) mentioning '{query}':"]
        for r in results:
            lines.append(f"  • {r['file']}: ...{r['snippet']}...")
        return "\n".join(lines)

    except Exception as e:
        return f"Obsidian operation failed, sir: {e}"
