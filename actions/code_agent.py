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
  6. Fallback    — if the primary executor fails at the *infrastructure*
                   layer (CLI not on PATH, hits the timeout, exits with a
                   non-zero code, or returns an auth/credential error),
                   hand the same task to the Cline VS Code extension
                   running headless and re-run the verify+commit pass on
                   whatever Cline produces. If the Cline extension isn't
                   available, fall through to the FCC Cline adapter for
                   users without the extension installed. Result-state
                   failures (no edits made, files don't compile, git
                   commit itself fails) are NOT Cline candidates —
                   re-running with a different model would just reproduce
                   them.

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
  - for fallback only: the Cline VS Code extension installed and
    authenticated, or the FCC Cline adapter at
    %USERPROFILE%\.local\bin\fcc-cline.exe (overridable via the
    `cline_adapter_path` config key or CLINE_ADAPTER_PATH env var).
  - the FCC Cline adapter at the configured path (default
    `~/.local/bin/fcc-cline.exe`) to enable the Cline fallback. If the
    adapter isn't there, the fallback is silently skipped and the user is
    told how to enable it.

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

DEFAULT_CLAUDE_CLI     = "claude.cmd" if os.name == "nt" else "claude"
DEFAULT_FCC_URL        = "http://127.0.0.1:8082"
DEFAULT_TIMEOUT_S      = 900          # 15 min — an agentic session can genuinely take a while
DEFAULT_COMMAND_TPL    = [
    DEFAULT_CLAUDE_CLI, "-p", "{prompt}", "--dangerously-skip-permissions",
    "--no-session-persistence", "--autocompact", "auto",
]
_BOUNDED_TOOLS = "Read,Edit,Write,Bash,Glob,Grep"
# Use a Claude Code-recognized alias; FCC routes it to the configured free
# provider while avoiding the CLI's unrecognized synthetic-model path.
_FCC_MODEL = "sonnet"
_FCC_SYSTEM_PROMPT = (
    "You are LITE's coding agent. Work only in the supplied project directory. "
    "Use the available coding tools, make the requested changes, and verify them."
)
DEFAULT_FCC_SERVER     = Path.home() / ".local" / "bin" / "fcc-server.exe"
DEFAULT_CLINE_ADAPTER  = Path.home() / ".local" / "bin" / "fcc-cline.exe"

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
        command = list(tpl)
    else:
        cmd = os.environ.get("CLAUDE_CODE_CMD") or cfg.get("claude_code_cli") or DEFAULT_CLAUDE_CLI
        command = [
            cmd, "-p", "{prompt}", "--dangerously-skip-permissions",
            "--no-session-persistence", "--autocompact", "auto",
        ]

    # FCC rejects the current Claude Code default tool/system payload at about
    # 32 MB. Keep the agentic coding tools, but omit image-capable and optional
    # integrations that can accumulate attachments before the first turn.
    if "--tools" not in command:
        command.extend(["--tools", _BOUNDED_TOOLS])
    if "--setting-sources" not in command:
        command.extend(["--setting-sources", "project,local"])
    if "--model" not in command:
        command.extend(["--model", _FCC_MODEL])
    if "--system-prompt" not in command and "--system-prompt-file" not in command:
        command.extend(["--system-prompt", _FCC_SYSTEM_PROMPT])
    if "--strict-mcp-config" not in command:
        command.append("--strict-mcp-config")
    if "--disable-slash-commands" not in command:
        command.append("--disable-slash-commands")
    return command


def _minimal_command_template(command: list) -> list:
    """Build a last-resort request with no optional Claude context."""
    minimal = []
    skip_next = False
    for part in command:
        if skip_next:
            skip_next = False
            continue
        if part in ("--tools", "--setting-sources", "--model", "--system-prompt"):
            skip_next = True
            continue
        if part in ("--strict-mcp-config", "--disable-slash-commands", "--autocompact", "auto"):
            continue
        minimal.append(part)
    minimal.extend(["--tools", "", "--setting-sources", "project,local", "--model", "sonnet"])
    return minimal


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
    base_url = cfg.get("anthropic_base_url") or cfg.get("fcc_claude_url")
    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
    if cfg.get("anthropic_auth_token"):
        env["ANTHROPIC_AUTH_TOKEN"] = cfg["anthropic_auth_token"]
    if cfg.get("fcc_claude_api_key"):
        env["ANTHROPIC_API_KEY"] = cfg["fcc_claude_api_key"]
    return env


def _fcc_server_path(cfg: dict) -> Path:
    configured = cfg.get("fcc_server_path") or os.environ.get("FCC_SERVER_PATH")
    return Path(configured).expanduser() if configured else DEFAULT_FCC_SERVER


def _cline_adapter_path(cfg: dict) -> Path:
    configured = cfg.get("cline_adapter_path") or os.environ.get("CLINE_ADAPTER_PATH")
    return Path(configured).expanduser() if configured else DEFAULT_CLINE_ADAPTER


# Cline VS Code extension — the user asked for Cline in VS Code to be the
# fallback when the default configuration fails. When the extension is
# installed in VS Code and the `cline` CLI is on PATH, we invoke it in
# headless/print mode with the same prompt we gave Claude Code. If neither
# is available, we fall through to the FCC Cline adapter below so the
# fallback still works for users without the VS Code extension.
DEFAULT_CLINE_VSCODE_BIN  = "cline.cmd" if os.name == "nt" else "cline"
DEFAULT_CLINE_VSCODE_TPL  = [
    DEFAULT_CLINE_VSCODE_BIN, "-p", "{prompt}", "--no-session", "--auto-approve",
]


def _cline_vscode_enabled(cfg: dict) -> bool:
    """True when the Cline-in-VS-Code fallback should be attempted.

    Disabled via cline_disable_vscode=true or the CLINE_DISABLE_VSCODE env
    var. On by default — if `cline` is on PATH, we'll use it; if not, the
    code falls through to the FCC adapter.
    """
    if os.environ.get("CLINE_DISABLE_VSCODE", "").lower() in ("1", "true", "yes"):
        return False
    return not cfg.get("cline_disable_vscode", False)


def _cline_vscode_command(cfg: dict) -> list | None:
    """Return the headless Cline command template, or None if the CLI
    isn't available. Resolution order:
      1. cline_vscode_command in config (list, "{prompt}" placeholder).
      2. CLINE_VSCODE_CMD env var (list form via ';'-separated string).
      3. DEFAULT_CLINE_VSCODE_TPL.
    Returns None only when none of the above resolves to an executable
    `cline` binary on PATH (or .cmd/.bat on Windows).
    """
    tpl = cfg.get("cline_vscode_command")
    if not tpl:
        env = os.environ.get("CLINE_VSCODE_CMD")
        if env:
            tpl = [s.strip() for s in env.split(";") if s.strip()]
    if not tpl:
        tpl = list(DEFAULT_CLINE_VSCODE_TPL)

    if not tpl:
        return None
    first = tpl[0]
    if Path(first).exists():
        return tpl
    if shutil.which(first) is not None:
        return tpl
    return None


def _cline_vscode_env(cfg: dict) -> dict:
    """Env for the Cline VS Code headless run. Mirrors _extra_env plus
    Cline-specific keys so the extension's model route can also be
    pointed at fcc-claude.
    """
    env = {**os.environ, **_extra_env(cfg)}
    if cfg.get("cline_api_key"):
        env["CLINE_API_KEY"] = cfg["cline_api_key"]
    if cfg.get("cline_base_url"):
        env["CLINE_BASE_URL"] = cfg["cline_base_url"]
    return env


def _run_cline_vscode_fallback(cfg: dict, work_dir: Path, task: str, timeout: int) -> str:
    """Run the Cline VS Code extension in headless mode. Returns the
    CLI's combined stdout/stderr. Caller is responsible for verifying
    whatever the Cline run produced (same pipeline as Claude Code)."""
    tpl = _cline_vscode_command(cfg)
    if not tpl:
        return "Cline VS Code extension not on PATH (install the Cline extension in VS Code, or set cline_vscode_command in config)."

    prompt = (
        f"You are working inside the project at {work_dir}. Complete the "
        f"following task, making whatever file changes are needed. Verify "
        f"your own work (run/compile/test as appropriate) before finishing. "
        f"Task:\n\n{task}"
    )
    cmd = [part.format(prompt=prompt) if isinstance(part, str) else part for part in tpl]
    env = _cline_vscode_env(cfg)
    try:
        result = subprocess.run(
            cmd, cwd=str(work_dir), env=env,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        return ((e.stdout or "") if isinstance(e.stdout, str) else "") + \
               ((e.stderr or "") if isinstance(e.stderr, str) else "") + \
               f"\n[Cline VS Code fallback timed out after {timeout}s]"
    except FileNotFoundError:
        return f"Cline VS Code fallback failed: `{tpl[0]}` not on PATH."
    except Exception as e:
        return f"Cline VS Code fallback failed to start: {e}"
    out = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
    return out.strip() or f"Cline VS Code fallback exited with code {result.returncode}."


def _run_cline_fallback(cfg: dict, work_dir: Path, task: str, timeout: int) -> str:
    """Run Cline after Claude Code fails at the infrastructure layer.

    Resolution order (per the user's request to prefer Cline in VS Code):
      1. Cline VS Code extension (headless) if enabled and on PATH.
      2. FCC Cline adapter at cline_adapter_path (legacy).
    Either returns the captured CLI output (which _verify_and_commit will
    then judge) or a human-readable 'unavailable' message.
    """
    if _cline_vscode_enabled(cfg):
        tpl = _cline_vscode_command(cfg)
        if tpl is not None:
            return _run_cline_vscode_fallback(cfg, work_dir, task, timeout)

    adapter = _cline_adapter_path(cfg)
    if not adapter.exists():
        return f"Cline fallback unavailable: {adapter} was not found."

    env = {**os.environ, **_extra_env(cfg)}
    npm_bin = Path.home() / "AppData" / "Roaming" / "npm"
    if npm_bin.exists():
        env["PATH"] = str(npm_bin) + os.pathsep + env.get("PATH", "")
    command = [
        str(adapter),
        f"Work in {work_dir}. Complete this task, verify the result, and make the changes directly: {task}",
        "--cwd", str(work_dir), "--auto-approve", "true", "--compaction", "off",
        "--timeout", str(timeout),
    ]
    try:
        result = subprocess.run(command, cwd=str(work_dir), env=env,
                                capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return f"Cline fallback failed to start: {exc}"
    output = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
    return output.strip() or f"Cline fallback exited with code {result.returncode}."


def _verify_and_commit(
    work_dir, checkpoint, task, cli_output, elapsed, timed_out, scope, log, report,
):
    """Run the verify (py_compile) + commit pass and return a 4-tuple.

    Returns
    -------
    (changed, broken, commit_failed, final_message):
        changed         — list of .py files the session touched.
        broken          — list of "<file>: <error>" for files that don't
                          compile (post-rollback, may be empty).
        commit_failed   — True when the changes compiled but the git
                          commit itself didn't land.
        final_message   — the user-facing message that *would* be reported
                          for the primary agent, before the orchestrator
                          decides whether to try the Cline fallback. Used
                          in the final report either as the success line
                          or as a quoted context line under the Cline run.
    """
    changed = _changed_py_files(work_dir, checkpoint)
    status  = _git(["status", "--porcelain"], work_dir, check=False).stdout

    note          = " (session hit the time limit — verify the result before relying on it)" if timed_out else ""
    restart_note  = " Restart LITE for changes to its own code to take effect." if scope != "external" else ""
    files_txt     = "\n".join(f"  • {f}" for f in changed) if changed else "  (no .py files — see git status)"

    if not status.strip():
        tail = "\n".join(cli_output.strip().splitlines()[-15:])
        return (
            [],
            [],
            False,
            f"Claude Code ran ({elapsed:.0f}s) but left no changes.\n"
            f"Last output:\n{tail}",
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
        return (
            changed,
            broken,
            False,
            "Claude Code's changes didn't compile — rolled back to the pre-run "
            "checkpoint, nothing was left broken.\n"
            + "\n".join(f"  • {b}" for b in broken),
        )

    # ── Commit the verified-good change ─────────────────────────────────────
    _git(["add", "-A"], work_dir, check=False)
    summary = task[:72] + ("..." if len(task) > 72 else "")
    commit  = _git(["commit", "-m", f"feat(code_agent): {summary}"], work_dir, check=False)
    still_dirty = _git(["status", "--porcelain"], work_dir, check=False).stdout.strip()

    if still_dirty:
        reason = (commit.stderr or commit.stdout or "unknown reason").strip().splitlines()[-1:] or ["unknown reason"]
        return (
            changed,
            [],
            True,
            f"Done{note} in {elapsed:.0f}s. Changed:\n{files_txt}\n"
            f"Verified compiling — but the commit itself failed ({reason[0]}). "
            f"The change is on disk and staged, not committed — commit it "
            f"manually once that's sorted.{restart_note}",
        )

    return (
        changed,
        [],
        False,
        f"Done{note} in {elapsed:.0f}s. Changed:\n{files_txt}\n"
        f"Verified compiling and committed.{restart_note}",
    )


def _maybe_fallback_to_cline(
    cfg, work_dir, task, timeout, log, report,
    *, checkpoint, cli_output, kind, primary_message, scope, started,
):
    """Run Cline when the primary (Claude Code) run didn't produce a
    verified + committed result. Returns the user-facing report string.

    The Cline run uses the *same* checkpoint that the Claude Code run
    started from, so a Cline success lands on a tree that already has
    whatever the Claude Code session left behind (rolled back on a
    compile failure, untouched otherwise). The same verify + commit
    pass runs on Cline's output, so a Cline success commits on top of
    the original worktree just like a Claude Code success would have.
    """
    adapter = _cline_adapter_path(cfg)
    if not adapter.exists():
        log(f"Cline fallback unavailable ({adapter} not found); returning the primary failure.")
        return report(primary_message)

    log(f"Primary run failed ({kind}); handing the task to the Cline fallback...")
    cline_output = _run_cline_fallback(cfg, work_dir, task, timeout)
    log(f"Cline fallback finished. Re-running verify + commit on its output...")

    cline_elapsed = time.monotonic() - started
    cline_changed, cline_broken, cline_commit_failed, cline_message = _verify_and_commit(
        work_dir, checkpoint, task, cline_output, cline_elapsed,
        timed_out=False, scope=scope, log=log, report=lambda _t: _t,
    )

    cline_kind = _detect_failure_kind(
        cline_output,
        changed_files=cline_changed,
        broken_files=cline_broken,
        commit_failed=cline_commit_failed,
    )

    if cline_kind == _FAILURE_OK:
        return report(
            f"Cline completed what Claude Code couldn't.\n"
            f"Primary agent said: {primary_message}\n"
            f"\nCline result:\n{cline_message}"
        )

    log("Cline fallback also did not produce a verified, committed change.")
    return report(
        f"Both agents failed to produce a verified change.\n"
        f"\nPrimary agent ({kind}):\n{primary_message}\n"
        f"\nCline fallback ({cline_kind}):\n{cline_message}\n"
        f"\nLast 20 lines of Cline output:\n"
        + "\n".join(cline_output.strip().splitlines()[-20:])
    )


# Failure kinds the orchestrator uses to decide whether to invoke the
# Cline fallback. Kept as a small enum-style set of strings so the
# detector and the trigger site stay trivially in sync.
_FAILURE_OK              = "ok"               # verified + committed
_FAILURE_PAYLOAD_TOO_BIG = "payload_too_big"  # FCC's 32 MB rejection
_FAILURE_CLI_ERROR       = "cli_error"        # non-zero exit, FileNotFoundError, etc.
_FAILURE_TIMEOUT         = "timeout"          # session hit the wall clock
_FAILURE_EMPTY           = "empty"            # session ran but left no changes
_FAILURE_COMPILE         = "compile_failed"   # changes broke py_compile
_FAILURE_COMMIT          = "commit_failed"    # changes good, git commit itself failed
_FAILURE_STARTUP         = "startup_failed"   # subprocess.run never produced a session


def _detect_failure_kind(
    cli_output: str,
    *,
    returncode: int | None = None,
    timed_out: bool = False,
    changed_files: list | None = None,
    broken_files: list | None = None,
    commit_failed: bool = False,
) -> str:
    """Classify a finished Claude Code session into a single failure kind.

    The caller is the orchestrator just after the verify/commit pass; it
    passes the original CLI output, the subprocess return code, and the
    structural facts gathered by the verify pass (which files changed,
    which of those don't compile, and whether the final commit itself
    succeeded). Returns one of the ``_FAILURE_*`` constants above. The
    orchestrator treats anything other than ``_FAILURE_OK`` as a reason
    to consider the Cline fallback.
    """
    text = (cli_output or "").lower()

    if "request too large" in text and "32mb" in text:
        return _FAILURE_PAYLOAD_TOO_BIG
    if timed_out:
        return _FAILURE_TIMEOUT
    if returncode not in (None, 0):
        return _FAILURE_CLI_ERROR
    if broken_files:
        return _FAILURE_COMPILE
    if commit_failed:
        return _FAILURE_COMMIT
    if not changed_files:
        return _FAILURE_EMPTY
    return _FAILURE_OK


def _fcc_reachable(url: str) -> bool:
    try:
        import requests
        requests.get(url, timeout=2)
        return True
    except Exception:
        return False


def start_fcc_server(cfg: dict | None = None) -> str:
    """Start fcc-server when needed and wait until its proxy responds."""
    cfg = cfg or _load_config()
    url = _fcc_url(cfg)
    if _fcc_reachable(url):
        return f"fcc server is already running at {url}."

    server = _fcc_server_path(cfg)
    if not server.exists():
        return f"Couldn't start fcc server: {server} was not found."

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            [str(server)], cwd=str(server.parent),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except Exception as exc:
        return f"Couldn't start fcc server: {exc}"

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _fcc_reachable(url):
            return f"fcc server started at {url}."
        time.sleep(0.25)
    return f"fcc server process was launched, but {url} did not respond within 10 seconds."


def stop_fcc_server(cfg: dict | None = None) -> str:
    """Stop the local fcc-server process without stopping LITE."""
    cfg = cfg or _load_config()
    url = _fcc_url(cfg)
    if os.name == "nt":
        command = ["taskkill", "/IM", "fcc-server.exe", "/T", "/F"]
    else:
        command = ["pkill", "-f", "fcc-server"]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return "fcc server stopped. LITE is still running."
    if not _fcc_reachable(url):
        return "fcc server is already stopped."
    return f"Couldn't stop fcc server: {(result.stderr or result.stdout).strip()}"


def fcc_server_control(parameters: dict | None = None) -> str:
    action = str((parameters or {}).get("action", "status")).strip().lower()
    cfg = _load_config()
    url = _fcc_url(cfg)
    if action == "start":
        return start_fcc_server(cfg)
    if action == "stop":
        return stop_fcc_server(cfg)
    if action == "status":
        return f"fcc server is {'running' if _fcc_reachable(url) else 'stopped'} at {url}."
    return "Specify fcc server action: start, stop, or status."


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


def _rollback(work_dir: Path, checkpoint: str, log):
    log(f"Rolling back to checkpoint {checkpoint[:10]}...")
    _git(["reset", "--hard", checkpoint], work_dir, check=False)
    _git(["clean", "-fd"], work_dir, check=False)


# ── Failure classification & Cline fallback policy ─────────────────────────
# The primary executor is Claude Code via FCC. When the *infrastructure* of
# that pipeline is the problem (the CLI isn't on PATH, it can't authenticate,
# it crashes, it hangs past the timeout), we want a safety net so the user's
# request is not silently dropped. Cline is that safety net: when present
# and configured, it gets one attempt with the same task, same checkpoint
# safety net, and same verify+commit pass. Result-state failures (no
# changes, files don't compile, git commit itself failed) are NOT Cline
# candidates — those are real outcomes, and re-running the same model
# against the same project would just reproduce them.

# Failure categories returned by _detect_failure_kind().
_FAILURE_OK            = "ok"               # success
_FAILURE_STARTUP       = "startup_failed"   # subprocess.run never produced a session
_FAILURE_TIMEOUT       = "timeout"          # subprocess.run hit the timeout
_FAILURE_EXIT_NONZERO  = "exit_nonzero"     # CLI returned a non-zero exit code
_FAILURE_AUTH          = "auth_error"       # FCC/auth failure in stdout/stderr
_FAIL_NO_CHANGES       = "no_changes"       # CLI succeeded but made no edits
_FAILURE_BROKEN        = "broken_files"     # edits don't compile
_FAILURE_COMMIT        = "commit_failed"    # edits good but git commit itself failed
_FAIL_HARD = {                            # categories that warrant a Cline retry
    _FAILURE_STARTUP,
    _FAILURE_TIMEOUT,
    _FAILURE_EXIT_NONZERO,
    _FAILURE_AUTH,
}


# Strings that indicate an infrastructure-level auth/proxy failure in the
# CLI's combined output, not a code problem in the project. Treated as
# _FAILURE_AUTH so Cline gets a turn before we report back to the user.
_AUTH_SIGNATURES = (
    "401", "403", "unauthorized", "unauthenticated",
    "api key", "apikey", "invalid token", "expired token",
    "authentication failed", "auth failed",
    "rate limit", "rate_limit", "quota exceeded",
    "fcc server", "fcc rejected", "fcc-claude",
    "billing", "payment required", "402",
)


def _detect_failure_kind(
    cli_output: str,
    returncode: int | None,
    timed_out: bool,
    changed_files: list,
    broken_files: list,
    commit_failed: bool,
) -> str:
    """Classify what happened so the orchestrator can pick the right next step.

    Order matters: infrastructure-level failures (startup/timeout/non-zero
    exit/auth) are reported BEFORE result-level failures (no changes,
    broken files, commit failed), because if the CLI itself didn't run to
    completion we don't know if a result would have been good.
    """
    if timed_out:
        return _FAILURE_TIMEOUT
    if returncode is not None and returncode != 0:
        return _FAILURE_EXIT_NONZERO
    out_lc = (cli_output or "").lower()
    if any(sig in out_lc for sig in _AUTH_SIGNATURES):
        return _FAILURE_AUTH
    if commit_failed:
        return _FAILURE_COMMIT
    if broken_files:
        return _FAILURE_BROKEN
    if not changed_files:
        return _FAIL_NO_CHANGES
    return _FAILURE_OK


def _verify_and_commit(
    work_dir: Path,
    checkpoint: str,
    task: str,
    cli_output: str,
    elapsed: float,
    timed_out: bool,
    scope: str,
    log,
    report,
) -> tuple[list, list, bool, str]:
    """Run the post-run verify + commit pass. Pure function over the
    working tree; returns the same shape the orchestrator unpacks:
        (changed_py_files, broken_py_files, commit_failed, final_message)

    This is the same logic the original orchestrator had inline; pulling
    it out makes the orchestrator readable and lets _maybe_fallback_to_cline
    re-use it after the Cline attempt.
    """
    changed = _changed_py_files(work_dir, checkpoint)
    status  = _git(["status", "--porcelain"], work_dir, check=False).stdout

    if not status.strip():
        tail = "\n".join(cli_output.strip().splitlines()[-15:])
        return changed, [], False, (
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
        return changed, broken, False, (
            f"Claude Code's changes didn't compile — rolled back to the pre-run "
            f"checkpoint, nothing was left broken.\n"
            + "\n".join(f"  • {b}" for b in broken)
        )

    _git(["add", "-A"], work_dir, check=False)
    summary = task[:72] + ("..." if len(task) > 72 else "")
    commit = _git(
        ["commit", "-m", f"feat(code_agent): {summary}"],
        work_dir,
        check=False,
    )
    still_dirty = _git(["status", "--porcelain"], work_dir, check=False).stdout

    note = " (session hit the time limit — verify the result before relying on it)" if timed_out else ""
    files_txt = (
        "\n".join(f"  • {f}" for f in changed) if changed else "  (no .py files — see git status)"
    )
    restart_note = " Restart LITE for changes to its own code to take effect." if scope != "external" else ""

    if still_dirty.strip():
        reason = (commit.stderr or commit.stdout or "unknown reason").strip().splitlines()[-1:] or ["unknown reason"]
        return changed, [], True, (
            f"Done{note} in {elapsed:.0f}s. Changed:\n{files_txt}\n"
            f"Verified compiling — but the commit itself failed ({reason[0]}). "
            f"The change is on disk and staged, not committed — commit it "
            f"manually once that's sorted.{restart_note}"
        )

    return changed, [], False, (
        f"Done{note} in {elapsed:.0f}s. Changed:\n{files_txt}\n"
        f"Verified compiling and committed.{restart_note}"
    )


def _maybe_fallback_to_cline(
    cfg: dict,
    work_dir: Path,
    task: str,
    timeout: int,
    log,
    report,
    *,
    checkpoint: str,
    cli_output: str,
    kind: str,
    primary_message: str,
    scope: str,
    started: float,
) -> str:
    """Run Cline once when the primary executor failed at the infrastructure
    layer. Returns the user-facing report — either the Cline result or the
    original primary message + a 'Cline also tried' note.

    Only fires for HARD failures (startup/timeout/non-zero exit/auth). Result
    failures (no changes / broken / commit-failed) are real outcomes and
    won't get better by re-running with a different model.
    """
    if kind not in _FAIL_HARD:
        return report(primary_message)

    # Cline can be reached two ways:
    #   1. The Cline VS Code extension's headless `cline` CLI on PATH
    #      (what the user asked for in their message).
    #   2. The legacy FCC Cline adapter at cline_adapter_path.
    # If either is available, _run_cline_fallback will pick the VS Code
    # one first. Only when *neither* resolves do we tell the user the
    # fallback isn't configured — and the message points at the VS Code
    # extension install path first, since that's the preferred one.
    vscode_tpl = _cline_vscode_command(cfg) if _cline_vscode_enabled(cfg) else None
    adapter    = _cline_adapter_path(cfg)
    if vscode_tpl is None and not adapter.exists():
        log(
            f"Primary executor failed ({kind}); Cline fallback skipped — "
            f"no Cline CLI on PATH and no FCC adapter at {adapter}."
        )
        return report(
            f"{primary_message}\n\n"
            f"[Cline fallback not configured — install the Cline extension "
            f"in VS Code so the `cline` CLI is on PATH, or set "
            f"'cline_adapter_path' in config/api_keys.json to "
            f"{adapter}.]"
        )

    via = f"the Cline VS Code CLI ({vscode_tpl[0]})" if vscode_tpl is not None else f"the FCC Cline adapter at {adapter}"
    log(
        f"Primary executor failed ({kind}); delegating the same task to "
        f"Cline via {via} for one attempt..."
    )
    cline_output = _run_cline_fallback(cfg, work_dir, task, timeout)
    cline_elapsed = time.monotonic() - started

    # Same verify+commit pass as the primary executor — a Cline output is
    # only useful if the resulting tree is good, just like Claude Code.
    cline_changed, cline_broken, cline_commit_failed, cline_message = (
        _verify_and_commit(
            work_dir, checkpoint, task, cline_output, cline_elapsed,
            timed_out=False, scope=scope, log=log, report=lambda t: t,
        )
    )

    cline_kind = _detect_failure_kind(
        cline_output,
        returncode=None,           # _run_cline_fallback already returned
                                   # its own human-readable summary, and
                                   # the verify+commit outcome is what
                                   # matters from here on.
        timed_out=False,
        changed_files=cline_changed,
        broken_files=cline_broken,
        commit_failed=cline_commit_failed,
    )

    if cline_kind == _FAILURE_OK:
        log("Cline fallback completed the task successfully.")
        return report(f"[Cline fallback after {kind} via {via}]\n{cline_message}")

    log(f"Cline fallback also failed ({cline_kind}); reporting both attempts.")
    return report(
        f"[Cline fallback after {kind} also did not produce a verified result]\n\n"
        f"Primary (Claude Code) result:\n{primary_message}\n\n"
        f"Cline result:\n{cline_message}\n\n"
        f"Last 20 lines of Cline output:\n"
        + "\n".join(cline_output.strip().splitlines()[-20:])
    )


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
    cfg = _load_config()
    timeout = int(p.get("timeout") or cfg.get("claude_code_timeout") or DEFAULT_TIMEOUT_S)

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

    # ── Preflight: start the fcc-claude proxy before Claude Code ─────────────
    fcc_url = _fcc_url(cfg)
    fcc_result = start_fcc_server(cfg)
    log(fcc_result)
    if not _fcc_reachable(fcc_url):
        return report(f"fcc-claude is unavailable at {fcc_url}; Claude Code was not launched.")

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

    env = {
        **os.environ,
        **_extra_env(cfg),
        # The FCC model is synthetic and not in Claude Code's built-in model
        # catalog; without this, the CLI applies its own window assumptions.
        "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT": "1",
    }

    started = time.monotonic()
    returncode = 0
    try:
        proc = subprocess.run(
            cmd, cwd=str(work_dir), env=env,
            capture_output=True, text=True, timeout=timeout,
        )
        cli_output   = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        returncode   = proc.returncode
        timed_out    = False
    except subprocess.TimeoutExpired as e:
        cli_output = ((e.stdout or "") if isinstance(e.stdout, str) else "") + \
                     ((e.stderr or "") if isinstance(e.stderr, str) else "")
        timed_out  = True
        returncode = 124   # conventional shell "timeout" code
        log(f"Claude Code session hit the {timeout}s timeout — stopping and verifying "
            f"whatever state it left behind.")
    except FileNotFoundError:
        # Don't bail out — Cline might still be available, and the user
        # asked us to fall back to it when the default config fails.
        cli_output   = f"FileNotFoundError: `{cli_name}` not on PATH"
        returncode   = 127
        timed_out    = False
    except Exception as e:
        return report(f"Failed to start the Claude Code session: {e}")

    # Older Claude Code builds can still attach their startup/session-title
    # payload despite the bounded command flags. Retry once with all optional
    # tools disabled; this is safe because no project mutation happened yet.
    # Cline is invoked later (after the verify pass) by _maybe_fallback_to_cline
    # for every failure kind, not just the 32 MB one — so this block is just
    # an in-band retry that might let Claude Code succeed without burning the
    # Cline turn at all.
    if "Request too large" in cli_output and "32MB" in cli_output:
        log("FCC rejected Claude's startup payload at 32 MB; retrying with minimal context...")
        minimal_cmd = [
            part.format(prompt=prompt) if isinstance(part, str) else part
            for part in _minimal_command_template(command_tpl)
        ]
        try:
            retry = subprocess.run(
                minimal_cmd, cwd=str(work_dir), env=env,
                capture_output=True, text=True, timeout=timeout,
            )
            cli_output = (retry.stdout or "") + (("\n" + retry.stderr) if retry.stderr else "")
        except Exception as e:
            cli_output += f"\nMinimal FCC retry failed: {e}"

    elapsed = time.monotonic() - started

    # ── Verify ───────────────────────────────────────────────────────────────
    changed, broken, commit_failed, final_message = _verify_and_commit(
        work_dir, checkpoint, task, cli_output, elapsed, timed_out, scope, log, report,
    )

    kind = _detect_failure_kind(
        cli_output,
        returncode=returncode if "returncode" in locals() else None,
        timed_out=timed_out,
        changed_files=changed,
        broken_files=broken,
        commit_failed=commit_failed,
    )

    if kind != _FAILURE_OK:
        return _maybe_fallback_to_cline(
            cfg, work_dir, task, timeout, log, report,
            checkpoint=checkpoint, cli_output=cli_output, kind=kind,
            primary_message=final_message, scope=scope, started=started,
        )

    return report(final_message)
