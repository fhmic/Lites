#code_agent.py
"""
Replaces self_maintain.py, code_diagnostics.py, dev_agent.py, code_helper.py,
and agents/automation_coding_agent.py — all five were single-shot
"prompt a model once, hope the diff is right" tools, and kept failing/
rolling back on anything non-trivial because a single prompt/response can't
actually run code, see the error, and retry.

This tool instead delegates the whole task to a real Claude Code agentic
session (the `claude` CLI), running locally against Felix's free-claude-code
("fcc") proxy at http://127.0.0.1:8082 — so it can read files, edit, run
commands, see failures, and loop until the task is actually done, the same
way Felix would if he sat down and did it by hand. LITE's job here is just
the safety net around that session, not the coding itself:

  1. Checkpoint  — commit any pre-existing dirty state, then record HEAD.
  2. Delegate    — run `claude` non-interactively in the target directory
                   with the task as its prompt.
  3. Verify      — every .py file the session touched must still compile
                   (py_compile). This is deliberately independent of
                   whether the CLI itself reported success — a session can
                   exit "successfully" and still leave broken Python.
  4. Rollback    — any compile failure -> `git reset --hard` + `git clean
                   -fd` back to the checkpoint. LITE never keeps a change
                   that doesn't at least compile, full stop.
  5. Commit      — a verified-good change is committed on its own, so it's
                   never mixed with whatever Felix had pending before.

Safety model:
- scope="self" (default) always targets LITE's own install directory
  (BASE_DIR) — never anywhere else.
- scope="external" targets any project Felix names (`target`), and
  `git init`s it first if it isn't already a repo, since the checkpoint/
  rollback mechanism requires one.
- Nothing here runs unattended in the background — only when explicitly
  asked, same as everything it replaces.
- Changes to LITE's own files take effect on next restart, same caveat
  self_maintain always carried.

Requires on the machine running LITE:
  - the `claude` CLI on PATH (or CLAUDE_CODE_CMD env var / claude_code_cli
    config key pointing at it)
  - the fcc-claude local proxy running at the configured URL (default
    http://127.0.0.1:8082), with the `claude` CLI already pointed at it
    (ANTHROPIC_BASE_URL / auth token) — that setup lives outside LITE
    entirely, this tool just shells out to whatever `claude` already does.

NOTE on CLI flags: Claude Code's exact headless-mode flags can change
between versions, and I can't verify Felix's installed version from here.
The command template below is the best-known invocation as of this
writing (`claude -p "<prompt>" --dangerously-skip-permissions`) but is
fully overridable via config/api_keys.json -> "claude_code_command" (a
list, with "{prompt}" as the placeholder) if it doesn't match what
`claude --help` shows on his machine.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import py_compile


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

DEFAULT_CLAUDE_CLI     = "claude"
DEFAULT_FCC_URL        = "http://127.0.0.1:8082"
DEFAULT_TIMEOUT_S      = 900          # 15 min — an agentic session can genuinely take a while
DEFAULT_COMMAND_TPL    = ["claude", "-p", "{prompt}", "--dangerously-skip-permissions"]

AGENT_NAME = "Code Agent"


# ── Config ───────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    try:
        return json.loads(API_CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _command_template(cfg: dict) -> list:
    tpl = cfg.get("claude_code_command")
    if isinstance(tpl, list) and tpl:
        return tpl
    cmd = os.environ.get("CLAUDE_CODE_CMD") or cfg.get("claude_code_cli") or DEFAULT_CLAUDE_CLI
    return [cmd, "-p", "{prompt}", "--dangerously-skip-permissions"]


def _fcc_url(cfg: dict) -> str:
    return (cfg.get("fcc_claude_url") or os.environ.get("FCC_CLAUDE_URL") or DEFAULT_FCC_URL).rstrip("/")


def _extra_env(cfg: dict) -> dict:
    """
    Only set if explicitly configured — LITE is often launched via a desktop
    shortcut (pythonw.exe), which does NOT inherit PowerShell-profile env
    vars the way a terminal session does. System-wide env vars (setx) are
    inherited fine either way; this is just a belt-and-suspenders override
    for anyone who configured fcc-claude at the shell-profile level instead.
    """
    env = {}
    if cfg.get("anthropic_base_url"):
        env["ANTHROPIC_BASE_URL"] = cfg["anthropic_base_url"]
    if cfg.get("anthropic_auth_token"):
        env["ANTHROPIC_AUTH_TOKEN"] = cfg["anthropic_auth_token"]
    return env


# ── Git helpers ──────────────────────────────────────────────────────────────

# Every commit code_agent makes is attributed to a fixed local identity,
# scoped to just these invocations via -c (never touches the user's global
# git config) — found in testing that a repo/machine with no git user.name/
# user.email configured makes every commit here fail with "please tell me
# who you are", and worse, the final commit was doing that failure
# *silently* and still reporting "committed". This closes that off at the
# source rather than just detecting it after the fact.
_GIT_IDENTITY = ["-c", "user.name=LITE Code Agent", "-c", "user.email=code-agent@lite.local"]


def _git(args: list, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *_GIT_IDENTITY, *args], cwd=str(cwd), capture_output=True, text=True, check=check,
    )


def _is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def _ensure_checkpoint(work_dir: Path, log):
    """Commits any pre-existing dirty state (so it's never lost or conflated
    with the agent's own changes) and returns the resulting HEAD hash — or
    None (with a reason logged) if the checkpoint itself couldn't be made,
    in which case the caller must NOT proceed: no checkpoint means no safety
    net to roll back to."""
    status = _git(["status", "--porcelain"], work_dir, check=False).stdout
    if status.strip():
        log("Working tree has pending changes — snapshotting them first...")
        _git(["add", "-A"], work_dir, check=False)
        commit = _git(["commit", "-m", "chore(code_agent): snapshot before agent run"], work_dir, check=False)
        still_dirty = _git(["status", "--porcelain"], work_dir, check=False).stdout
        if still_dirty.strip():
            reason = (commit.stderr or commit.stdout or "unknown reason").strip().splitlines()[-1:] or ["unknown reason"]
            log(f"Checkpoint commit failed: {reason[0]}")
            return None
    head = _git(["rev-parse", "HEAD"], work_dir, check=False).stdout.strip()
    return head or None


def _changed_py_files(work_dir: Path, since: str) -> list:
    diff = _git(["diff", "--name-only", since, "--", "*.py"], work_dir, check=False).stdout
    untracked = _git(["ls-files", "--others", "--exclude-standard", "*.py"], work_dir, check=False).stdout
    files = set(f for f in diff.splitlines() if f.strip()) | set(f for f in untracked.splitlines() if f.strip())
    return sorted(files)


def _changed_py_files(work_dir: Path, since: str) -> list:
    diff = _git(["diff", "--name-only", since, "--", "*.py"], work_dir, check=False).stdout
    untracked = _git(["ls-files", "--others", "--exclude-standard", "*.py"], work_dir, check=False).stdout
    files = set(f for f in diff.splitlines() if f.strip()) | set(f for f in untracked.splitlines() if f.strip())
    return sorted(files)


def _rollback(work_dir: Path, checkpoint: str, log):
    log(f"Rolling back to checkpoint {checkpoint[:10]}...")
    _git(["reset", "--hard", checkpoint], work_dir, check=False)
    _git(["clean", "-fd"], work_dir, check=False)


# ── Public entry point ───────────────────────────────────────────────────────

def code_agent(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    p       = parameters or {}
    task    = (p.get("task") or p.get("description") or p.get("issue") or "").strip()
    scope   = (p.get("scope") or "self").strip().lower()
    target  = (p.get("target") or p.get("file_path") or "").strip()
    timeout = int(p.get("timeout") or DEFAULT_TIMEOUT_S)

    def log(msg: str):
        print(f"[{AGENT_NAME}] {msg}")
        if player and hasattr(player, "write_log"):
            player.write_log(f"[{AGENT_NAME}] {msg}")

    def report(text: str) -> str:
        full = f"[{AGENT_NAME}] {text}"
        if player and hasattr(player, "show_content"):
            try:
                player.show_content("CODE AGENT", full)
            except Exception:
                pass
        return full

    if not task:
        return report(
            "Tell me what you'd like done — a fix, a feature, a whole project — "
            "and I'll hand it to Claude Code, sir."
        )

    # ── Resolve target directory ──────────────────────────────────────────
    if scope == "external":
        if not target:
            return report("Tell me which project/folder to work in for an external task, sir.")
        work_dir = Path(target).expanduser().resolve()
        if not work_dir.exists() or not work_dir.is_dir():
            return report(f"{work_dir} doesn't exist or isn't a folder, sir.")
    else:
        work_dir = BASE_DIR

    cfg = _load_config()

    # ── Preflight: git ─────────────────────────────────────────────────────
    if not _is_git_repo(work_dir):
        if scope == "external":
            log(f"{work_dir} isn't a git repo yet — initializing one so I can checkpoint safely...")
            _git(["init"], work_dir, check=False)
        else:
            return report(
                "LITE's own install folder isn't a git repo — I need that for the "
                "checkpoint/rollback safety net. Run `git init` there first, sir."
            )

    # ── Preflight: claude CLI ───────────────────────────────────────────────
    command_tpl = _command_template(cfg)
    cli_name    = command_tpl[0]
    if shutil.which(cli_name) is None and not Path(cli_name).exists():
        return report(
            f"Can't find the `{cli_name}` command on PATH, sir. Install Claude Code "
            f"(npm i -g @anthropic-ai/claude-code) or set claude_code_cli in "
            f"config/api_keys.json to its full path."
        )

    # ── Soft preflight: fcc-claude proxy reachable ──────────────────────────
    fcc_url = _fcc_url(cfg)
    try:
        import requests
        requests.get(fcc_url, timeout=2)
    except Exception:
        log(f"Couldn't reach fcc-claude at {fcc_url} — proceeding anyway; "
            f"the CLI may hang or fail if it's not running.")

    # ── Checkpoint ───────────────────────────────────────────────────────────
    checkpoint = _ensure_checkpoint(work_dir, log)
    if not checkpoint:
        return report(
            "Couldn't create the safety checkpoint git commit needs before I hand "
            "anything to Claude Code — nothing was touched. Check `git status` and "
            "`git log` in that folder for what's blocking a commit there (a hook, a "
            "lock file, etc.) and try again."
        )
    log(f"Checkpoint set at {checkpoint[:10]}. Delegating to Claude Code...")

    prompt = (
        f"You are working inside the project at {work_dir}. "
        f"Complete the following task, making whatever file changes are "
        f"needed. Verify your own work (run/compile/test as appropriate) "
        f"before finishing. Task:\n\n{task}"
    )
    cmd = [part.format(prompt=prompt) if isinstance(part, str) else part for part in command_tpl]

    env = {**os.environ, **_extra_env(cfg)}

    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=str(work_dir), env=env,
            capture_output=True, text=True, timeout=timeout,
        )
        cli_output   = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        timed_out    = False
    except subprocess.TimeoutExpired as e:
        cli_output = ((e.stdout or "") if isinstance(e.stdout, str) else "") + \
                     ((e.stderr or "") if isinstance(e.stderr, str) else "")
        timed_out  = True
        log(f"Claude Code session hit the {timeout}s timeout — stopping and verifying "
            f"whatever state it left behind.")
    except FileNotFoundError:
        return report(f"Couldn't launch `{cli_name}` — is it installed and on PATH?")
    except Exception as e:
        return report(f"Failed to start the Claude Code session: {e}")

    elapsed = time.monotonic() - started

    # ── Verify ───────────────────────────────────────────────────────────────
    changed = _changed_py_files(work_dir, checkpoint)
    status  = _git(["status", "--porcelain"], work_dir, check=False).stdout

    if not status.strip():
        tail = "\n".join(cli_output.strip().splitlines()[-15:])
        return report(
            f"Claude Code ran ({elapsed:.0f}s) but left no changes.\n"
            f"Last output:\n{tail}"
        )

    broken = []
    for rel in changed:
        full = work_dir / rel
        if not full.exists():
            continue
        try:
            py_compile.compile(str(full), doraise=True)
        except Exception as e:
            broken.append(f"{rel}: {e}")

    if broken:
        _rollback(work_dir, checkpoint, log)
        return report(
            f"Claude Code's changes didn't compile — rolled back to the pre-run "
            f"checkpoint, nothing was left broken.\n"
            + "\n".join(f"  • {b}" for b in broken)
        )

    # ── Commit the verified-good change ─────────────────────────────────────
    _git(["add", "-A"], work_dir, check=False)
    summary = task[:72] + ("..." if len(task) > 72 else "")
    commit = _git(["commit", "-m", f"feat(code_agent): {summary}"], work_dir, check=False)
    still_dirty = _git(["status", "--porcelain"], work_dir, check=False).stdout

    note = " (session hit the time limit — verify the result before relying on it)" if timed_out else ""
    files_txt = "\n".join(f"  • {f}" for f in changed) if changed else "  (no .py files — see git status)"
    restart_note = " Restart LITE for changes to its own code to take effect." if scope != "external" else ""

    if still_dirty.strip():
        reason = (commit.stderr or commit.stdout or "unknown reason").strip().splitlines()[-1:] or ["unknown reason"]
        return report(
            f"Done{note} in {elapsed:.0f}s. Changed:\n{files_txt}\n"
            f"Verified compiling — but the commit itself failed ({reason[0]}). "
            f"The change is on disk and staged, not committed — commit it "
            f"manually once that's sorted.{restart_note}"
        )

    return report(
        f"Done{note} in {elapsed:.0f}s. Changed:\n{files_txt}\n"
        f"Verified compiling and committed.{restart_note}"
    )
