#code_agent.py
"""
Replaces self_maintain.py, code_diagnostics.py, dev_agent.py, code_helper.py,
and agents/automation_coding_agent.py — all five were single-shot
"prompt a model once, hope the diff is right" tools, and kept failing/
rolling back on anything non-trivial because a single prompt/response can't
actually run code, see the error, and retry.

This tool instead delegates the whole task to the Cline VS Code extension
running headless (the `cline` CLI) so it can read files, edit, run
commands, see failures, and loop until the task is actually done — the
same way Felix would if he sat down and did it by hand. LITE's job here
is just the safety net around that session, not the coding itself:

  1. Checkpoint  — commit any pre-existing dirty state, then record HEAD.
  2. Delegate    — run `cline` non-interactively in the target directory
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
  - the Cline VS Code extension installed and its `cline` CLI on PATH
    (or set `cline_cli` in config/api_keys.json, or the CLINE_CLI env
    var, to its full path). Cline itself manages model authentication
    through VS Code, so no proxy or fcc-style setup is required by
    LITE for this tool to work.

NOTE on CLI flags: Cline's exact headless-mode flags can change
between versions, and the version installed on Felix's machine can
differ from the one verified during development. The default template
below matches Cline 3.x (prompt is a positional argument, --auto-approve
takes a boolean value) and is fully overridable via
config/api_keys.json -> "cline_command" (a list, with "{prompt}" as the
placeholder) if `cline --help` on Felix's machine shows a different
flag set.
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

DEFAULT_CLINE_CLI      = "cline.cmd" if os.name == "nt" else "cline"
DEFAULT_TIMEOUT_S      = 900          # 15 min — an agentic session can genuinely take a while
# Cline 3.x headless invocation: the prompt is a *positional* argument
# (`cline [options] [command] [prompt]` per `cline --help`), and the
# auto-approve switch takes a boolean value. There is no --no-session flag
# in current Cline — sessions that finish are just done. Override via
# `cline_command` in config/api_keys.json if your installed Cline version
# expects different flags.
DEFAULT_COMMAND_TPL    = [
    DEFAULT_CLINE_CLI, "--auto-approve", "true", "{prompt}",
]

AGENT_NAME = "Code Agent"


# ── Config ───────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    try:
        return json.loads(API_CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


# Headless flags that the minimal retry template must keep so Cline still
# runs non-interactively in act mode. The executable and the {prompt}
# placeholder are also kept; everything else (--model, --system, --thinking,
# --provider, custom flags) gets dropped on retry.
_CLINE_KEEP_FLAGS = {"--auto-approve", "true", "{prompt}"}


def _command_template(cfg: dict) -> list:
    """Build the headless Cline command for a coding-agent run.

    Resolution order:
      1. `cline_command` in config (list, "{prompt}" placeholder).
      2. `cline_cli` in config (just the executable — wraps it with the
         default flags).
      3. `CLINE_CLI` env var (treated like `cline_cli`).
      4. DEFAULT_COMMAND_TPL (cline.cmd on Windows / cline elsewhere).

    The user can fully override either the executable or the full flag
    set, since Cline's exact headless-mode flags can change between
    versions.
    """
    tpl = cfg.get("cline_command")
    if isinstance(tpl, list) and tpl:
        return [str(part) for part in tpl]
    cmd = (
        cfg.get("cline_cli")
        or os.environ.get("CLINE_CLI")
        or DEFAULT_CLINE_CLI
    )
    return [str(cmd), "--auto-approve", "true", "{prompt}"]


def _minimal_command_template(command: list) -> list:
    """Build a minimal last-resort Cline command with optional flags dropped.

    Keeps the executable, the prompt placeholder, and the auto-approve
    switches (so Cline still runs non-interactively in act mode) but
    drops any extra model/system/provider overrides the user may have
    set in the full template. Used when the primary run trips an error
    that's known to be caused by one of those overrides.
    """
    keep = _CLINE_KEEP_FLAGS
    minimal = [part for part in command if part in keep]
    if "{prompt}" not in minimal:
        # Rebuild a safe default from the first positional (the executable)
        # if the user's template lost the placeholder.
        minimal = [command[0], "--auto-approve", "true", "{prompt}"]
    return minimal


# Cline VS Code extension — primary executor. Cline manages its own
# model authentication through VS Code, so LITE doesn't need to wire up
# any proxy or auth tokens; the env overrides below are opt-in knobs for
# users who want to point Cline at a different model endpoint.
DEFAULT_CLINE_VSCODE_BIN  = DEFAULT_CLINE_CLI
DEFAULT_CLINE_VSCODE_TPL  = [
    DEFAULT_CLINE_VSCODE_BIN, "--auto-approve", "true", "{prompt}",
]


def _cline_vscode_enabled(cfg: dict) -> bool:
    """True when the Cline CLI should be used. On by default."""
    if os.environ.get("CLINE_DISABLE_VSCODE", "").lower() in ("1", "true", "yes"):
        return False
    return not cfg.get("cline_disable_vscode", False)


def _cline_vscode_command(cfg: dict) -> list | None:
    """Return the headless Cline command template, or None if the CLI
    isn't available. Resolution order:
      1. cline_command in config (list, "{prompt}" placeholder).
      2. cline_cli in config / CLINE_CLI env var.
      3. DEFAULT_CLINE_VSCODE_TPL.
    Returns None only when none of the above resolves to an executable
    `cline` binary on PATH (or .cmd/.bat on Windows).
    """
    tpl = cfg.get("cline_command")
    if not tpl:
        cmd = cfg.get("cline_cli") or os.environ.get("CLINE_CLI")
        if cmd:
            tpl = [str(cmd), "--auto-approve", "true", "{prompt}"]
    if not tpl:
        tpl = list(DEFAULT_CLINE_VSCODE_TPL)

    if not tpl:
        return None
    first = tpl[0]
    if Path(first).exists():
        return [str(p) for p in tpl]
    if shutil.which(first) is not None:
        return [str(p) for p in tpl]
    return None


def _cline_env(cfg: dict) -> dict:
    """Optional env overrides for the Cline subprocess.

    Only set when explicitly configured — LITE is often launched via a
    desktop shortcut (pythonw.exe), which does NOT inherit shell-profile
    env vars the way a terminal session does. System-wide env vars (setx)
    are inherited fine either way; this is just a belt-and-suspenders
    override for anyone who configured Cline's model route at the
    shell-profile level instead.
    """
    env = {**os.environ}
    if cfg.get("cline_api_key"):
        env["CLINE_API_KEY"] = cfg["cline_api_key"]
    if cfg.get("cline_base_url"):
        env["CLINE_BASE_URL"] = cfg["cline_base_url"]
    return env


def _preflight_cline(cfg: dict, log) -> list[str]:
    """Run lightweight Cline diagnostics so failures later in the run have
    actionable context. Returns a list of human-readable notes (also sent
    to the log). Never raises — diagnostics must never block a real run.

    What it does:
      1. `cline --version`  — confirms the binary is on PATH and runnable.
         A half-broken install (binary present, crashes on launch) gets
         caught here instead of in the middle of the actual task.
      2. `cline doctor`     — checks the Cline hub daemon's health and
         reports its URL. The hub is the bit that listens for the CLI's
         API calls; an unhealthy hub is a strong signal that even if
         the binary runs, no LLM call will succeed.
      3. Resolves the model provider Cline is configured to use
         (CLINE_BASE_URL from cfg / env, else the user's default), so
         the user can see exactly which endpoint their session will hit.

    All output is captured — the Cline CLI on Windows is noisy and we
    only want the high-signal bits in the log.
    """
    notes: list[str] = []
    cli_name = (
        cfg.get("cline_cli")
        or os.environ.get("CLINE_CLI")
        or DEFAULT_CLINE_CLI
    )

    # ── 1. Version probe ─────────────────────────────────────────────────
    try:
        v = subprocess.run(
            [cli_name, "--version"],
            capture_output=True, text=True, timeout=10,
        )
        version_line = (v.stdout or v.stderr or "").strip().splitlines()
        version = version_line[0] if version_line else "(no output)"
        notes.append(f"cline --version -> {version}")
    except FileNotFoundError:
        notes.append(f"cline --version -> not found (`{cli_name}` not on PATH)")
    except subprocess.TimeoutExpired:
        notes.append("cline --version -> timed out after 10s")
    except Exception as e:
        notes.append(f"cline --version -> error: {e}")

    # ── 2. Doctor (hub health) ───────────────────────────────────────────
    try:
        d = subprocess.run(
            [cli_name, "doctor"],
            capture_output=True, text=True, timeout=15,
        )
        dlines = (d.stdout or "").strip().splitlines()
        # Pull just the high-signal lines; `doctor` prints ~12 of them.
        interesting = [
            ln for ln in dlines
            if any(k in ln for k in (
                "version", "hub url", "hub healthy", "hub uptime",
                "listeners", "cli processes", "sidecar processes",
                "error",
            ))
        ][:8]
        if interesting:
            notes.append("cline doctor -> " + " | ".join(
                ln.strip() for ln in interesting
            ))
        elif d.returncode != 0:
            err = (d.stderr or d.stdout or "").strip().splitlines()
            notes.append("cline doctor -> non-zero exit: " + (err[0] if err else "unknown"))
        else:
            notes.append("cline doctor -> no output (unexpected)")
    except FileNotFoundError:
        # Same as above; the version probe already reported it.
        pass
    except subprocess.TimeoutExpired:
        notes.append("cline doctor -> timed out after 15s")
    except Exception as e:
        notes.append(f"cline doctor -> error: {e}")

    # ── 3. Resolved API endpoint ─────────────────────────────────────────
    base_url = (
        cfg.get("cline_base_url")
        or os.environ.get("CLINE_BASE_URL")
        or "(unset — Cline uses whatever provider is configured in `cline auth`)"
    )
    notes.append(f"API endpoint   -> {base_url}")

    for line in notes:
        log(line)
    return notes


def _run_cline(cfg: dict, work_dir: Path, task: str, timeout: int, command_tpl: list | None = None) -> str:
    """Run Cline headless in the target project. Returns the CLI's combined
    stdout/stderr. Caller is responsible for verifying whatever the Cline
    run produced (same pipeline Claude Code used to use).

    If `command_tpl` is None, uses the full Cline command template. If the
    caller wants a last-resort minimal invocation (for transient errors),
    pass the result of _minimal_command_template.
    """
    tpl = command_tpl or _cline_vscode_command(cfg)
    if not tpl:
        return (
            "Cline CLI not on PATH. Install the Cline extension in VS Code "
            "and make sure its `cline` command is on PATH, or set "
            "cline_cli in config/api_keys.json to its full path."
        )

    prompt = (
        f"You are working inside the project at {work_dir}. Complete the "
        f"following task, making whatever file changes are needed. Verify "
        f"your own work (run/compile/test as appropriate) before finishing. "
        f"Task:\n\n{task}"
    )
    cmd = [part.format(prompt=prompt) if isinstance(part, str) else part for part in tpl]
    env = _cline_env(cfg)
    try:
        result = subprocess.run(
            cmd, cwd=str(work_dir), env=env,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        return ((e.stdout or "") if isinstance(e.stdout, str) else "") + \
               ((e.stderr or "") if isinstance(e.stderr, str) else "") + \
               f"\n[Cline timed out after {timeout}s]"
    except FileNotFoundError:
        return f"Cline failed: `{tpl[0]}` not on PATH."
    except Exception as e:
        return f"Cline failed to start: {e}"
    out = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
    return out.strip() or f"Cline exited with code {result.returncode}."


# Backward-compat aliases — the old name remains so anything that imported
# _run_cline_vscode_fallback still resolves while callers migrate.
def _run_cline_vscode_fallback(cfg: dict, work_dir: Path, task: str, timeout: int) -> str:
    return _run_cline(cfg, work_dir, task, timeout)


def _run_cline_fallback(cfg: dict, work_dir: Path, task: str, timeout: int) -> str:
    """Alias preserved for any external callers — Cline is now the only
    fallback, so this just delegates to _run_cline."""
    return _run_cline(cfg, work_dir, task, timeout)


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
        final_message   — the user-facing message that gets reported for
                          the Cline run.
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
            f"Cline ran ({elapsed:.0f}s) but left no changes.\n"
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
            "Cline's changes didn't compile — rolled back to the pre-run "
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


def _rollback(work_dir: Path, checkpoint: str, log):
    log(f"Rolling back to checkpoint {checkpoint[:10]}...")
    _git(["reset", "--hard", checkpoint], work_dir, check=False)
    _git(["clean", "-fd"], work_dir, check=False)


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


# ── Failure classification ──────────────────────────────────────────────────
# The primary executor is the Cline CLI. Result-state failures (no
# changes / broken files / commit failed) are real outcomes and are
# reported as-is to the user; infrastructure-level failures (CLI not
# on PATH, hits the timeout, exits with a non-zero code) are reported
# directly too — there is no longer a secondary fallback executor, so
# the user gets a clear "Cline didn't run" message instead of a silent
# drop.

# Failure categories returned by _detect_failure_kind().
_FAILURE_OK            = "ok"               # success
_FAILURE_STARTUP       = "startup_failed"   # subprocess.run never produced a session
_FAILURE_TIMEOUT       = "timeout"          # subprocess.run hit the timeout
_FAILURE_EXIT_NONZERO  = "exit_nonzero"     # CLI returned a non-zero exit code
_FAILURE_AUTH          = "auth_error"       # auth/model-route failure in stdout/stderr
_FAIL_NO_CHANGES       = "no_changes"       # CLI succeeded but made no edits
_FAILURE_BROKEN        = "broken_files"     # edits don't compile
_FAILURE_COMMIT        = "commit_failed"    # edits good but git commit itself failed


# Strings that indicate an infrastructure-level auth failure in the
# CLI's combined output, not a code problem in the project.
_AUTH_SIGNATURES = (
    "401", "403", "unauthorized", "unauthenticated",
    "api key", "apikey", "invalid token", "expired token",
    "authentication failed", "auth failed",
    "rate limit", "rate_limit", "quota exceeded",
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


def _maybe_fallback_to_cline(*args, **kwargs):
    """Backward-compat shim — retained so any external import still resolves.

    Cline is now the *primary* executor, so there is no longer a secondary
    fallback to delegate to. The orchestrator now just reports whatever
    the verify+commit pass produced. This shim returns the original
    primary message unchanged.
    """
    report = kwargs.get("report")
    if report is None and args:
        report = args[-1]
    primary = kwargs.get("primary_message", "")
    if report is None:
        return primary
    return report(primary)


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
    timeout = int(p.get("timeout") or cfg.get("cline_timeout") or cfg.get("claude_code_timeout") or DEFAULT_TIMEOUT_S)

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
            "and I'll hand it to Cline, sir."
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

    # ── Preflight: Cline CLI ────────────────────────────────────────────────
    if not _cline_vscode_enabled(cfg):
        return report(
            "Cline is disabled (cline_disable_vscode=true) — re-enable it in "
            "config/api_keys.json, or unset the CLINE_DISABLE_VSCODE env var."
        )
    command_tpl = _cline_vscode_command(cfg)
    if command_tpl is None:
        cli_name = (cfg.get("cline_cli") or os.environ.get("CLINE_CLI") or DEFAULT_CLINE_CLI)
        return report(
            f"Can't find the `{cli_name}` command on PATH, sir. Install the Cline "
            f"extension in VS Code and make sure its `cline` CLI is on PATH, or set "
            f"cline_cli in config/api_keys.json to its full path."
        )

    # ── Preflight: Cline diagnostics ───────────────────────────────────────
    # Probe `cline --version` + `cline doctor` and report the resolved API
    # endpoint, so that any failure later in the run has actionable context
    # the user can read in the log instead of an opaque "Cline ran but left
    # no changes".
    log("Cline preflight:")
    preflight = _preflight_cline(cfg, log)

    # ── Checkpoint ───────────────────────────────────────────────────────────
    checkpoint = _ensure_checkpoint(work_dir, log)
    if not checkpoint:
        return report(
            "Couldn't create the safety checkpoint git commit needs before I hand "
            "anything to Cline — nothing was touched. Check `git status` and "
            "`git log` in that folder for what's blocking a commit there (a hook, a "
            "lock file, etc.) and try again."
        )
    log(f"Checkpoint set at {checkpoint[:10]}. Delegating to Cline...")

    started = time.monotonic()
    cli_output = _run_cline(cfg, work_dir, task, timeout, command_tpl=command_tpl)
    returncode = 0
    timed_out  = False
    if cli_output.endswith("timed out]"):
        timed_out = True
        returncode = 124   # conventional shell "timeout" code

    # If Cline rejected the full command template (e.g. it has flags
    # that aren't supported on the installed Cline version), retry once
    # with the minimal template before giving up.
    if "unknown option" in cli_output.lower() or "unrecognized" in cli_output.lower():
        log("Cline rejected the full command template; retrying with minimal flags...")
        cli_output = _run_cline(
            cfg, work_dir, task, timeout,
            command_tpl=_minimal_command_template(command_tpl),
        )
        returncode = 0
        timed_out  = False
        if cli_output.endswith("timed out]"):
            timed_out = True
            returncode = 124

    elapsed = time.monotonic() - started

    # ── Interpret the most common Cline failure modes ─────────────────────
    # If Cline exited but the output reads as an upstream connectivity
    # problem rather than a "task done" or "task attempted" message,
    # surface an actionable explanation BEFORE the verify pipeline runs —
    # so the user can fix the right thing (re-run `cline auth`, restart
    # the provider proxy, etc.) instead of staring at a "left no changes"
    # verdict.
    lower = cli_output.lower()
    if (
        ("cannot connect to api" in lower or "unable to connect" in lower
         or "connection refused" in lower or "econnrefused" in lower
         or "etimedout" in lower or "network" in lower)
        and "left no changes" in final_message.lower()
    ):
        provider_hint = (
            (cfg.get("cline_base_url")
             or os.environ.get("CLINE_BASE_URL")
             or "Cline's currently-configured provider (set via `cline auth`)")
        )
        final_message = (
            final_message
            + "\n\n[Code Agent] The Cline CLI ran but couldn't reach its model API. "
              f"Resolved endpoint: {provider_hint}. "
              "If that's a custom proxy, it's likely down — restart it, or re-run "
              "`cline auth` to point Cline at a different provider. "
              "If the endpoint looks right, run `cline doctor` from a terminal for "
              "a fuller diagnostic."
        )
        log("Cline exit was an upstream API connectivity failure — not a code-task failure.")

    # ── Verify ───────────────────────────────────────────────────────────────
    changed, broken, commit_failed, final_message = _verify_and_commit(
        work_dir, checkpoint, task, cli_output, elapsed, timed_out, scope, log, report,
    )

    kind = _detect_failure_kind(
        cli_output,
        returncode=returncode,
        timed_out=timed_out,
        changed_files=changed,
        broken_files=broken,
        commit_failed=commit_failed,
    )

    if kind == _FAILURE_OK:
        return report(final_message)

    # Cline is now the only executor, so we just hand the result back. A
    # future iteration may reintroduce a real fallback here; the
    # _maybe_fallback_to_cline shim remains as a no-op import surface.
    return report(final_message)
