#!/usr/bin/env python3
"""Keep source edits on the Edit/Write path, where the safety net lives.

Every other hook in this directory is registered on `Edit|Write`. That is
the whole safety net — the encoding check, the test run, the front-end
guard, the risk radar — and a file changed through Bash slips past all of
it silently. That is not hypothetical: the update-existing-playlist work
(PR #127, Sept 2026) was edited almost entirely through shell heredocs and
inline Python, none of those checks ran, and CodeQL found 22 problems the
checks would have pointed at.

Two halves, one script:

  pre   PreToolUse on Bash. Refuses a command that WRITES into the
        repo's source — a shell redirect, `tee`, `sed -i`/`perl -i`,
        `cp`/`mv`/`dd` into the tree, or an inline interpreter script that
        writes to a repo path. The refusal says to use Edit or Write
        instead, which is all it takes to put the checks back.

  post  PostToolUse on Bash. Whatever the pre-check could not see (a
        script file that writes, a tool with its own output flag),
        this finds by looking at what actually changed on disk, then runs
        the Edit|Write hooks over those files so nothing goes unchecked.

Deliberately NOT refused: git (checkout, restore, stash, pull, merge —
moving between committed states is not authoring), package managers,
the test run, and anything that writes OUTSIDE the repo (a scratchpad,
/tmp, the venv). Reading is never refused.

The pre-check is a pattern match on a shell command and cannot be
complete; it aims to catch every way this repo's source has actually been
written through Bash. The post-check is the backstop that does not need
to guess.
"""
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

# Inside the repo but not authored source: generated, vendored, or local.
_EXEMPT_PARTS = {".git", ".venv", "venv", "node_modules", "__pycache__",
                 ".pytest_cache", "build", "dist"}
_EXEMPT_PREFIXES = ("graphify-out", "static_analysis_")

# The hooks that make up the Edit|Write safety net, replayed by `post`.
_NET = ("check_encoding.py", "frontend_guard.py", "risk_radar.py")
_TESTS = "run_tests.py"

# More changed files than this is a branch switch or a merge, not an
# edit — say so, run the suite once, don't dump a hundred advisories.
_BULK = 25

_DENY = (
    "bash-guard — this command writes into the repo's source through the "
    "shell ({why}).\n\nUse the Edit or Write tool instead. This repo's "
    "safety net (encoding check, tests, front-end guard, risk radar) only "
    "runs on Edit|Write; a shell write skips all of it silently.\n\n"
    "Writing outside the repo (a scratchpad, /tmp) is fine, and so is git.")


# `NAME=value` assignments made earlier in the command being checked, so
# `SP=/tmp/x; … > "$SP/out.json"` resolves to /tmp/x/out.json instead of
# a literal "$SP" directory inside the repo. One command per hook process,
# so a module-level table is enough.
_CMD_VARS: dict = {}
_ASSIGN_VAR = re.compile(r"(?:^|[\s;&|(])([A-Za-z_]\w*)=(\"[^\"]*\"|'[^']*'|[^\s;&|)]*)")
_VAR_REF = re.compile(r"\$\{(\w+)\}|\$(\w+)")


def _collect_vars(cmd: str) -> dict:
    return {name: value.strip("'\"")
            for name, value in _ASSIGN_VAR.findall(cmd or "")}


def _expand_vars(s: str) -> str:
    def sub(m):
        name = m.group(1) or m.group(2)
        return _CMD_VARS.get(name, os.environ.get(name, m.group(0)))
    return _VAR_REF.sub(sub, s)


def _in_repo_source(path_str: str, cwd: str) -> bool:
    """True when `path_str` names a file this repo authors."""
    s = _expand_vars((path_str or "").strip().strip("'\""))
    if not s or s.startswith(("-", "&", "$(")) or s in ("/dev/null", "-"):
        return False
    if s.startswith("/dev/"):
        return False
    if "$" in s or "`" in s:
        # A variable we could not resolve. Refusing on a guess would block
        # ordinary scratch-file work; the post-check still catches a real
        # write into the repo by looking at what actually changed.
        return False
    p = Path(os.path.expanduser(s))
    if not p.is_absolute():
        p = Path(cwd or REPO) / p
    try:
        rel = Path(os.path.realpath(p)).relative_to(os.path.realpath(REPO))
    except ValueError:
        return False                           # outside the repo: fine
    parts = rel.parts
    if not parts:
        return False
    if any(part in _EXEMPT_PARTS for part in parts):
        return False
    return not parts[0].startswith(_EXEMPT_PREFIXES)


# ── pre: refuse obvious shell writes into source ─────────────────────────

# `>`/`>>` redirect targets, skipping fd duplication (2>&1, >&2).
_REDIRECT = re.compile(r"(?<![<>&\d])(?:\d)?>{1,2}(?!&)\s*(\"[^\"]+\"|'[^']+'|[^\s;&|()<>]+)")
# In-place editors and copy-into commands, by leading word.
_INPLACE = re.compile(r"(?:^|[;&|(]\s*|\bxargs\s+)(sed|perl|gsed)\b([^;&|]*)")
_COPYLIKE = re.compile(r"(?:^|[;&|(]\s*)(cp|mv|install|rsync|ln)\b([^;&|]*)")
_TEE = re.compile(r"\btee\b([^;&|]*)")
_DD = re.compile(r"\bdd\b[^;&|]*\bof=(\S+)")
# Inline interpreter scripts: python -c / python - <<EOF / node -e ...
_INTERP = re.compile(r"\b(python3?|\S*/python3?|node|ruby|perl)\b")
_WRITE_API = re.compile(
    r"write_text\(|write_bytes\(|\.write\(|writelines\(|"
    r"open\([^)]*['\"][wax]\+?b?['\"]|"
    r"shutil\.(?:copy\w*|move)\(|os\.(?:replace|rename)\(|\.replace\(\s*\w+\s*\)|"
    r"writeFileSync|fs\.write|File\.write")
# A quoted string that looks like a file path: has a separator, or ends in
# a source extension. "1.5" and "utf-8" are not paths.
_PATH_LITERAL = re.compile(
    r"['\"]([^'\"\s]*/[^'\"\s]*|[^'\"\s]+\.(?:py|js|html|css|json|md|txt"
    r"|sh|bat|ya?ml|toml|cfg|ini|svg|spec))['\"]")
# `name = Path("x")` / `name = "x"` — so `name.write_text(...)` on a later
# line is traced back to the path it was built from.
_ASSIGN = re.compile(r"(\w+)\s*=\s*(?:Path\()?\s*['\"]([^'\"]+)['\"]")


_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _strip_heredocs(cmd: str) -> str:
    """The command with heredoc BODIES removed — what the shell parses.

    A heredoc body is data. Scanning it as shell syntax made a commit
    message that mentioned "tee" or "> file" read as a write and refused
    an ordinary `git commit -F - <<EOF`. The line that OPENS the heredoc
    is kept, so `cat <<EOF > src.py` is still seen. Inline interpreter
    scripts, whose body IS code, are checked separately on the full
    text."""
    lines = cmd.split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for m in _HEREDOC.finditer(line):
            delim = m.group(2)
            while i < len(lines) and lines[i].strip() != delim:
                i += 1
            if i < len(lines):
                out.append(lines[i])
                i += 1
    return "\n".join(out)


def _tokens(fragment: str):
    try:
        return shlex.split(fragment, posix=True)
    except ValueError:
        return fragment.split()


def _script_writes_source(cmd: str, cwd: str) -> bool:
    """An inline interpreter script that writes to a repo path."""
    if not _INTERP.search(cmd) or not _WRITE_API.search(cmd):
        return False
    bound = {}
    for name, lit in _ASSIGN.findall(cmd):
        bound[name] = lit
    for line in cmd.splitlines():
        if not _WRITE_API.search(line):
            continue
        # A repo path literal on the writing line itself.
        if any(_in_repo_source(lit, cwd) for lit in _PATH_LITERAL.findall(line)):
            return True
        # Or a variable bound to one: `p.write_text(...)`, `open(p, "w")`.
        for name, lit in bound.items():
            if re.search(rf"\b{re.escape(name)}\b", line) and _in_repo_source(lit, cwd):
                return True
    return False


def check_command(cmd: str, cwd: str):
    """The reason `cmd` is refused, or None to let it run."""
    full = cmd or ""
    _CMD_VARS.clear()
    _CMD_VARS.update(_collect_vars(full))
    cmd = _strip_heredocs(full)            # shell syntax only
    for m in _REDIRECT.finditer(cmd):
        if _in_repo_source(m.group(1), cwd):
            return f"a redirect into {m.group(1).strip(chr(39) + chr(34))}"
    for m in _TEE.finditer(cmd):
        for tok in _tokens(m.group(1)):
            if not tok.startswith("-") and _in_repo_source(tok, cwd):
                return f"tee into {tok}"
    for m in _INPLACE.finditer(cmd):
        tool, rest = m.group(1), m.group(2)
        toks = _tokens(rest)
        if not any(t == "-i" or t.startswith("-i") or t in ("-pi", "-p", "-ni")
                   for t in toks if t.startswith("-")):
            continue
        if tool == "perl" and not any(t.startswith("-") and "i" in t for t in toks):
            continue
        for tok in toks:
            if not tok.startswith("-") and _in_repo_source(tok, cwd):
                return f"{tool} -i on {tok}"
    for m in _COPYLIKE.finditer(cmd):
        toks = [t for t in _tokens(m.group(2)) if not t.startswith("-")]
        if len(toks) >= 2 and _in_repo_source(toks[-1], cwd):
            return f"{m.group(1)} into {toks[-1]}"
    for m in _DD.finditer(cmd):
        if _in_repo_source(m.group(1), cwd):
            return f"dd into {m.group(1)}"
    if _script_writes_source(full, cwd):   # heredoc body is code here
        return "an inline script that writes to a repo file"
    return None


# ── post: whatever changed anyway gets the full safety net ───────────────

def _state_file(payload: dict) -> Path:
    key = (payload.get("tool_use_id") or payload.get("session_id")
           or "default")
    key = re.sub(r"[^A-Za-z0-9_-]", "_", str(key))[:80]
    return Path(tempfile.gettempdir()) / f"rp-bash-guard-{key}.json"


def _source_files():
    """Tracked plus untracked-but-not-ignored files, authored ones only."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard"],
            cwd=REPO, capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    return [f for f in out.splitlines()
            if f and _in_repo_source(f, str(REPO))]


def _mtimes() -> dict:
    snap = {}
    for rel in _source_files():
        try:
            snap[rel] = os.stat(REPO / rel).st_mtime_ns
        except OSError:
            pass
    return snap


def _record(payload: dict) -> None:
    try:
        data = json.dumps({"t": time.time(), "m": _mtimes()})
        _state_file(payload).write_text(data, encoding="utf-8")
    except Exception:
        pass            # a missing snapshot only weakens the backstop


def _changed_since(payload: dict) -> list:
    """Repo source files a Bash command created or modified."""
    state = _state_file(payload)
    try:
        before = json.loads(state.read_text(encoding="utf-8")).get("m") or {}
    except Exception:
        before = None
    finally:
        try:
            state.unlink()
        except Exception:
            pass
    after = _mtimes()
    if before is None:
        # No snapshot (the pre half did not run): fall back to what the
        # harness reports it changed, when it reports anything.
        resp = payload.get("tool_response") or {}
        listed = []
        if isinstance(resp, dict):
            for k, v in resp.items():
                if "file" in k.lower() and isinstance(v, list):
                    listed += [str(x.get("path") if isinstance(x, dict) else x)
                               for x in v]
        return [f for f in listed if _in_repo_source(f, str(REPO))]
    return sorted(f for f, m in after.items() if before.get(f) != m)


def _replay(rel: str, script: str) -> str:
    path = str(REPO / rel)
    try:
        body = (REPO / rel).read_text(encoding="utf-8", errors="replace")
    except Exception:
        body = ""
    payload = json.dumps({"tool_name": "Edit", "tool_input": {
        "file_path": path, "content": body}})
    try:
        proc = subprocess.run(
            [sys.executable, str(REPO / ".claude" / "hooks" / script)],
            input=payload, capture_output=True, text=True, timeout=120,
            env=dict(os.environ, CLAUDE_PROJECT_DIR=str(REPO)))
    except Exception:
        return ""
    out = (proc.stdout or "").strip()
    if not out:
        return ""
    try:
        return json.loads(out)["hookSpecificOutput"]["additionalContext"]
    except Exception:
        return out


def post(payload: dict) -> str:
    changed = _changed_since(payload)
    if not changed:
        return ""
    cmd = ((payload.get("tool_input") or {}).get("command") or "").lstrip()
    # git moves the tree between committed states; that is not an edit.
    # Skip any leading `cd … &&` and VAR=value prefixes before deciding.
    head_cmd = re.sub(r"^(?:cd\s+\S+\s*&&\s*)+", "", cmd)
    if re.match(r"(?:\S+=\S+\s+)*git\b", head_cmd):
        return ""
    notes = []
    if len(changed) > _BULK:
        notes.append(f"{len(changed)} source files changed — too many to "
                     "check one by one; the test suite was run once.")
    else:
        for rel in changed:
            for script in _NET:
                msg = _replay(rel, script)
                if msg:
                    notes.append(msg)
    py = next((f for f in changed if f.endswith(".py")), None)
    if py:
        msg = _replay(py, _TESTS)
        if msg:
            notes.append(msg)
    head = ("bash-guard — this Bash command changed repo source without "
            "Edit/Write:\n  " + "\n  ".join(changed[:_BULK])
            + "\n\nThe Edit|Write checks have now been run over those files."
            " Use Edit or Write for source changes so they run as you go.")
    return head + ("\n\n" + "\n\n".join(notes) if notes else
                   "\nNothing to report from them.")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0
    if os.environ.get("PYTEST_CURRENT_TEST") and mode == "post":
        # The post half runs the suite; never from inside the suite.
        return 0
    cmd = ((payload.get("tool_input") or {}).get("command") or "")
    cwd = payload.get("cwd") or os.getcwd()

    if mode == "pre":
        why = check_command(cmd, cwd)
        if why:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": _DENY.format(why=why),
            }}))
            return 0
        _record(payload)
        return 0

    if mode == "post":
        text = post(payload)
        if text:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": text,
            }}))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
