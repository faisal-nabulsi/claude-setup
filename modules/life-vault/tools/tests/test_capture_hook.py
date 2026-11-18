# tools/tests/test_capture_hook.py
import os
import re
import stat
import subprocess
import time
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "bin" / "lv-capture"
SID = "2bb362c8-4f1a-4b2c-9d3e-1a2b3c4d5e6f"

# Anything matching this turns the hook into a process launcher, which is the
# entire recursion trap. The test is the enforcement mechanism.
#
# It is a WORD-BOUNDARY match, not a substring match. Substring matching does
# not work here and was measured failing against the shipped script: "at "
# matches inside `cat >/dev/null` and "exec " matches `exec 2>/dev/null`, so the
# test reported two spawn primitives in a script that has none — and because
# lv-install refuses to register the hook when this file fails, the substring
# version made Task 15 unreachable.
FORBIDDEN = re.compile(
    r"(?<![\w/.-])"
    r"(claude|python[0-9.]*|node|deno|bun|ruby|perl|curl|wget|nohup|osascript"
    r"|at|batch|screen|tmux|eval|source)"
    r"(?![\w-])"
)

# The property being enforced is not "starts no process" — `cat` is /bin/cat and
# is a real process. It is "cannot start a MODEL or an INTERPRETER, and cannot
# background, substitute or evaluate anything". These are the only command words
# the script is allowed to use.
ALLOWED_COMMAND_WORDS = {"exec", "cat", "if", "case", "exit", "printf", "mv", "["}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def hook_body() -> str:
    text = HOOK.read_text(encoding="utf-8")
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def run(cwd, env_extra=None, stdin=b"", shell="/bin/sh"):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(cwd)}
    env.update(env_extra or {})
    return subprocess.run([shell, str(HOOK)], input=stdin, cwd=str(cwd),
                          env=env, capture_output=True)


@pytest.fixture()
def vault(tmp_path):
    (tmp_path / "_queue" / "pending").mkdir(parents=True)
    return tmp_path


def test_the_hook_contains_no_spawn_primitive():
    found = FORBIDDEN.findall(hook_body())
    assert found == [], f"lv-capture must not be able to start a process: {found}"


def test_the_hook_cannot_background_substitute_or_evaluate():
    body = hook_body()
    assert not re.search(r"[&`]|\$\(", body), "backgrounding or command substitution"


def test_exec_appears_only_as_a_redirection():
    """`exec 2>/dev/null` is a builtin redirection. `exec /some/binary` is a
    process launch wearing the same word, so the two are distinguished by shape
    rather than by trusting the word."""
    assert re.findall(r"^\s*exec\b.*$", hook_body(), re.M) == ["exec 2>/dev/null"]


def test_every_command_word_in_the_hook_is_on_the_allowlist():
    """A denylist only catches the primitives somebody thought of. This is the
    positive half: any NEW command word at all fails the build, and lv-install
    then refuses to register the hook."""
    words = set()
    for line in hook_body().splitlines():
        line = line.strip()
        if not line or ASSIGNMENT.match(line):
            continue
        words.add(line.split()[0])
    assert words == ALLOWED_COMMAND_WORDS, f"unexpected command words: {words ^ ALLOWED_COMMAND_WORDS}"


def test_the_hook_stays_short_enough_to_audit_at_a_glance():
    lines = [l for l in HOOK.read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.lstrip().startswith("#")]
    assert len(lines) <= 15, f"{len(lines)} executable lines; keep it auditable"


def test_the_hook_is_executable():
    assert HOOK.stat().st_mode & stat.S_IXUSR


def test_writes_one_pointer_named_for_the_session(vault):
    r = run(vault, {"LIFE_VAULT": str(vault), "CLAUDE_CODE_SESSION_ID": SID})
    assert r.returncode == 0
    assert (vault / "_queue" / "pending" / SID).read_text().strip() == str(vault)


def test_the_pointer_content_is_the_cwd_and_nothing_else(vault):
    work = vault / "somewhere"
    work.mkdir()
    run(work, {"LIFE_VAULT": str(vault), "CLAUDE_CODE_SESSION_ID": SID})
    assert (vault / "_queue" / "pending" / SID).read_text().strip() == str(work)


def test_n_stops_in_one_session_produce_exactly_one_file(vault):
    for _ in range(20):
        run(vault, {"LIFE_VAULT": str(vault), "CLAUDE_CODE_SESSION_ID": SID})
    assert [p.name for p in (vault / "_queue" / "pending").iterdir()] == [SID]


def test_never_writes_to_stdout(vault):
    r = run(vault, {"LIFE_VAULT": str(vault), "CLAUDE_CODE_SESSION_ID": SID},
            stdin=b'{"session_id":"x","transcript_path":"/tmp/y"}')
    assert r.stdout == b""


def test_never_writes_to_stderr_even_when_everything_is_wrong(vault):
    r = run(vault, {"LIFE_VAULT": "/nonexistent/vault", "CLAUDE_CODE_SESSION_ID": SID})
    assert r.stderr == b""


def test_exits_zero_when_the_vault_does_not_exist(tmp_path):
    r = run(tmp_path, {"LIFE_VAULT": "/nonexistent/vault", "CLAUDE_CODE_SESSION_ID": SID})
    assert r.returncode == 0


def test_exits_zero_when_the_queue_directory_is_unwritable(vault):
    q = vault / "_queue" / "pending"
    q.chmod(0o500)
    try:
        r = run(vault, {"LIFE_VAULT": str(vault), "CLAUDE_CODE_SESSION_ID": SID})
        assert r.returncode == 0
        assert r.stderr == b""
    finally:
        q.chmod(0o700)


def test_exits_zero_with_no_session_id(vault):
    r = run(vault, {"LIFE_VAULT": str(vault)})
    assert r.returncode == 0
    assert list((vault / "_queue" / "pending").iterdir()) == []


def test_a_path_traversal_session_id_writes_nothing_anywhere(vault):
    r = run(vault, {"LIFE_VAULT": str(vault), "CLAUDE_CODE_SESSION_ID": "../../pwned"})
    assert r.returncode == 0
    assert list((vault / "_queue" / "pending").iterdir()) == []
    assert not (vault / "pwned").exists()
    assert not (vault.parent / "pwned").exists()


def test_a_shell_metacharacter_session_id_is_inert(vault):
    for hostile in ["a;touch /tmp/lvpwned", "$(id)", "`id`", "a b", "a/b"]:
        r = run(vault, {"LIFE_VAULT": str(vault), "CLAUDE_CODE_SESSION_ID": hostile})
        assert r.returncode == 0
    assert list((vault / "_queue" / "pending").iterdir()) == []
    assert not Path("/tmp/lvpwned").exists()


def test_the_recursion_sentinel_stops_it_before_any_write(vault):
    r = run(vault, {"LIFE_VAULT": str(vault), "CLAUDE_CODE_SESSION_ID": SID,
                    "LIFEVAULT_NO_CAPTURE": "1"})
    assert r.returncode == 0
    assert list((vault / "_queue" / "pending").iterdir()) == []


def test_a_large_stdin_payload_is_drained_without_error(vault):
    r = run(vault, {"LIFE_VAULT": str(vault), "CLAUDE_CODE_SESSION_ID": SID},
            stdin=b"x" * 5_000_000)
    assert r.returncode == 0
    assert (vault / "_queue" / "pending" / SID).exists()


@pytest.mark.parametrize("shell", ["/bin/sh", "/bin/bash", "/bin/zsh"])
def test_behaves_identically_under_every_shell_on_this_machine(vault, shell):
    r = run(vault, {"LIFE_VAULT": str(vault), "CLAUDE_CODE_SESSION_ID": SID}, shell=shell)
    assert (r.returncode, r.stdout, r.stderr) == (0, b"", b"")


def test_no_temp_file_is_left_behind(vault):
    run(vault, {"LIFE_VAULT": str(vault), "CLAUDE_CODE_SESSION_ID": SID})
    names = sorted(p.name for p in (vault / "_queue" / "pending").iterdir())
    assert names == [SID]


def test_the_pointer_mtime_is_the_capture_heartbeat(vault):
    before = time.time()
    run(vault, {"LIFE_VAULT": str(vault), "CLAUDE_CODE_SESSION_ID": SID})
    mtime = (vault / "_queue" / "pending" / SID).stat().st_mtime
    assert before - 2 <= mtime <= time.time() + 2


def test_it_is_fast_enough_to_be_invisible(vault):
    start = time.monotonic()
    for _ in range(20):
        run(vault, {"LIFE_VAULT": str(vault), "CLAUDE_CODE_SESSION_ID": SID})
    per_call = (time.monotonic() - start) / 20
    assert per_call < 0.10, f"{per_call*1000:.1f}ms per call including process spawn"


# --- the exit code at the hook BOUNDARY, not inside the script ------------

BROKEN = {
    # A missing esac. /bin/sh exits 2 on a parse error, and Stop-hook exit 2
    # means "block this turn and feed stderr back to the model" — in every
    # project, every turn, up to the consecutive-block cap. `exec 2>/dev/null`
    # on line 1 also suppresses the diagnostic, so it would be undiagnosable.
    "syntax": 'exec 2>/dev/null\ncase "$X" in ""|*x*) exit 0 ;;\n',
    "ok": '#!/bin/sh\nexit 0\n',
}


@pytest.mark.parametrize("state", ["missing", "not-executable", "syntax-error"])
def test_the_registered_command_survives_a_broken_script(tmp_path, state):
    """The registration is `'<path>'; exit 0`, so no state of the script can
    produce a non-zero exit at the hook boundary. Verified: bare, these three
    states exit 127, 126 and 2; wrapped, all three exit 0."""
    script = tmp_path / "lv-capture"
    if state == "not-executable":
        script.write_text(BROKEN["ok"], encoding="utf-8")
        script.chmod(0o000)
    elif state == "syntax-error":
        script.write_text(BROKEN["syntax"], encoding="utf-8")
        script.chmod(0o755)
    command = f"'{script}'; exit 0"
    result = subprocess.run(command, shell=True, input=b"{}", capture_output=True)
    assert (result.returncode, result.stdout) == (0, b"")


def wrapper_env(vault):
    """os.environ minus the recursion sentinel, plus what the hook needs.

    Stripping LIFEVAULT_NO_CAPTURE is not defensive tidiness. Task 16's
    end-to-end suite imports `tools/bin/lv-sweep` as a module, and lv-sweep sets
    that variable in `os.environ` at module scope — inside the pytest process,
    for the remainder of the run. pytest collects alphabetically, so
    test_capture_end_to_end.py runs BEFORE test_capture_hook.py, and this test
    inherited a sentinel that makes the hook exit on line 3 having written
    nothing. Task 16 restores the variable around `exec_module` as well; this is
    the other half, because a test that only passes when it runs alone is not a
    test.
    """
    env = {k: v for k, v in os.environ.items() if k != "LIFEVAULT_NO_CAPTURE"}
    env.update(LIFE_VAULT=str(vault), CLAUDE_CODE_SESSION_ID=SID)
    return env


def test_the_healthy_script_also_exits_zero_through_the_wrapper(vault):
    command = f"'{HOOK}'; exit 0"
    result = subprocess.run(command, shell=True, input=b"{}", capture_output=True,
                            cwd=str(vault), env=wrapper_env(vault))
    assert (result.returncode, result.stdout) == (0, b"")
    assert (vault / "_queue" / "pending" / SID).exists()


def test_the_wrapper_test_is_not_affected_by_an_inherited_sentinel(vault, monkeypatch):
    """Pin the leak itself, not just its symptom.

    If a future edit goes back to `dict(os.environ, …)`, this fails
    deterministically instead of only when the full suite runs in alphabetical
    order — which is the difference between a five-minute fix and an afternoon
    spent bisecting test ordering.
    """
    monkeypatch.setenv("LIFEVAULT_NO_CAPTURE", "1")
    command = f"'{HOOK}'; exit 0"
    result = subprocess.run(command, shell=True, input=b"{}", capture_output=True,
                            cwd=str(vault), env=wrapper_env(vault))
    assert (result.returncode, result.stdout) == (0, b"")
    assert (vault / "_queue" / "pending" / SID).exists()
