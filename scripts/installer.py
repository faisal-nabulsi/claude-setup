#!/usr/bin/env python3
"""claude-setup installer (called by ../install.sh).

Promises:
  - Never overwrites an existing file. If a destination exists it is skipped and reported
    (CLAUDE.md is the one exception: the template is saved next to it as CLAUDE.md.claude-setup).
  - Backs up ~/.claude/settings.json before merging, and merges with JSON (never text edits).
  - Idempotent: a second run changes nothing that the first run already did.
  - --dry-run prints every action and touches nothing.
  - Never loads launchd jobs or registers MCP servers that need secrets; it prints the next
    command instead, because those steps need you (or Claude, with you watching).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
M = REPO / "modules"
HOME = Path(os.environ.get("HOME", str(Path.home()))).resolve()  # resolved: lv-install compares real paths
CLAUDE = HOME / ".claude"
SETTINGS = CLAUDE / "settings.json"

MODULES = [
    ("core", "CLAUDE.md template, connectors-first map, memory example"),
    ("hooks", "connectors-first + secrets guard hooks"),
    ("icloud-guard", "macOS: warn when a git repo sits in iCloud-synced Desktop/Documents"),
    ("life-vault", "~/life vault skeleton + lv tools + /remember + capture hook"),
    ("semester-tracker", "~/semester-tracker + ~/semester-heartbeat + today/deadlines hooks"),
    ("mods", "status band, /todo panel, model router (Claude Code plugins)"),
    ("mcp", "MCP bridge, Keychain launchers, custom MCP servers (copied, not registered)"),
    ("skills", "/hw, /verify-date, lock-in"),
    ("ntfy", "create ~/semester-tracker/.ntfy.env with a random topic"),
]

TEXT_SUFFIXES = {".py", ".sh", ".md", ".json", ".plist", ".js", ".ts", ".txt", ".tsv", ".yml",
                 ".yaml", ".jsonc", ".toml", ".ini", ".swift", ".template", ""}

DRY = False
LOG: list[str] = []
VALUES: dict[str, str] = {}


def say(msg: str) -> None:
    print(msg)
    LOG.append(msg)


def ask(prompt: str, default: str = "", yes: bool = False) -> str:
    if yes or not sys.stdin.isatty():
        return default
    v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return v or default


def confirm(prompt: str, yes: bool, default: bool = True) -> bool:
    if yes or not sys.stdin.isatty():
        return default
    v = input(f"{prompt} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return default if not v else v.startswith("y")


def fill(text: str) -> str:
    for k, v in VALUES.items():
        text = text.replace("{{" + k + "}}", v)
    return text


def is_text(p: Path) -> bool:
    return p.suffix in TEXT_SUFFIXES or p.name.startswith(".")


SKIP_PARTS = {"node_modules", ".venv", "__pycache__", ".git", ".pytest_cache", ".wrangler", "dist"}


def place(src: Path, dst: Path, mode: int | None = None) -> None:
    """Copy one file (or a whole tree) without overwriting anything, filling placeholders."""
    if src.is_dir():
        for p in sorted(src.rglob("*")):
            if p.is_file() and not (set(p.relative_to(src).parts) & SKIP_PARTS):
                place(p, dst / p.relative_to(src), mode)
        return
    if dst.exists():
        say(f"  skip (exists)  {dst}")
        return
    say(f"  {'would write' if DRY else 'write'}  {dst}")
    if DRY:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if is_text(src):
        try:
            dst.write_text(fill(src.read_text()))
        except UnicodeDecodeError:
            shutil.copy2(src, dst)
    else:
        shutil.copy2(src, dst)
    shutil.copymode(src, dst)
    if mode is not None:
        dst.chmod(mode)


def run(cmd: list[str], cwd: Path | None = None) -> None:
    say(f"  {'would run' if DRY else 'run'}  {' '.join(cmd)}" + (f"   (in {cwd})" if cwd else ""))
    if not DRY:
        subprocess.run(cmd, cwd=cwd, check=False, capture_output=True)


# ---------- settings.json merge ----------------------------------------------------------

def _strip_meta(obj):
    if isinstance(obj, dict):
        return {k: _strip_meta(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_meta(x) for x in obj]
    return obj


def merge_settings(chosen: set[str]) -> None:
    snippet = json.loads(fill((M / "core/settings-hooks.json").read_text()))
    try:
        data = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    except ValueError:
        say(f"  ! {SETTINGS} is not valid JSON; not touching it. Merge modules/core/settings-hooks.json by hand.")
        return
    before = json.dumps(data, sort_keys=True)
    hooks = data.setdefault("hooks", {})
    for event, blocks in snippet["hooks"].items():
        for block in blocks:
            if block.get("_module") not in chosen:
                continue
            clean = _strip_meta(block)
            cmds = {h["command"] for h in clean["hooks"]}
            existing = hooks.setdefault(event, [])
            have = {h.get("command") for b in existing for h in b.get("hooks", [])}
            if cmds & have:
                continue  # already wired (idempotent)
            existing.append(clean)
            say(f"  + hook {event}: {next(iter(cmds))}")
    if "life-vault" in chosen:
        perm = data.setdefault("permissions", {})
        for rule in snippet["permissions"]["allow"]:
            if rule not in perm.setdefault("allow", []):
                perm["allow"].append(rule)
                say(f"  + permission allow {rule}")
        for d in snippet["permissions"]["additionalDirectories"]:
            if d not in perm.setdefault("additionalDirectories", []):
                perm["additionalDirectories"].append(d)
                say(f"  + additional directory {d}")
    if "mods" in chosen:
        mk = data.setdefault("extraKnownMarketplaces", {})
        if "my-mods" not in mk:
            mk["my-mods"] = {"source": {"source": "directory", "path": str(HOME / "code/claude-mods")}}
            say("  + marketplace my-mods -> ~/code/claude-mods")
        ep = data.setdefault("enabledPlugins", {})
        for name in ("status-band", "todo-panel", "model-router"):
            key = f"{name}@my-mods"
            if key not in ep:
                ep[key] = True
                say(f"  + plugin {key}")
    if json.dumps(data, sort_keys=True) == before:
        say("  settings.json already has everything (no change)")
        return
    if DRY:
        say(f"  would back up and update {SETTINGS}")
        return
    CLAUDE.mkdir(parents=True, exist_ok=True)
    if SETTINGS.exists():
        backup = SETTINGS.with_name(f"settings.json.bak-claude-setup-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(SETTINGS, backup)
        say(f"  backed up settings.json -> {backup.name}")
    tmp = SETTINGS.with_name(".settings.json.claude-setup.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, SETTINGS)
    say(f"  updated {SETTINGS}")


# ---------- modules ----------------------------------------------------------------------

def mod_core(yes: bool) -> None:
    dst = CLAUDE / "CLAUDE.md"
    if dst.exists() and dst.read_text() == fill((M / "core/CLAUDE.md.template").read_text()):
        say(f"  skip (already installed)  {dst}")
    elif dst.exists():
        place(M / "core/CLAUDE.md.template", CLAUDE / "CLAUDE.md.claude-setup")
        say("  you already have ~/.claude/CLAUDE.md: the template is saved as CLAUDE.md.claude-setup; merge the sections you want")
    else:
        place(M / "core/CLAUDE.md.template", dst)
    place(M / "core/connectors-first.txt", CLAUDE / "connectors-first.txt")
    say("  memory format example: modules/core/memory-example/ (read it; nothing installed)")


def mod_hooks(yes: bool) -> None:
    for f in ("connectors_first.py", "secrets_guard.py"):
        place(M / "hooks" / f, CLAUDE / "hooks" / f)


def mod_icloud(yes: bool) -> None:
    place(M / "hooks/icloud_guard.py", CLAUDE / "hooks/icloud_guard.py")


def mod_vault(yes: bool) -> None:
    vault = HOME / "life"
    if vault.exists() and any(vault.iterdir()):
        say(f"  {vault} already exists: skeleton files are added only where missing")
    place(M / "life-vault/vault-skeleton", vault)
    place(M / "life-vault/tools", vault / "tools")
    say("  plists for opt-in feeds (iMessage, mail): modules/life-vault/launchd-optin/ (not installed)")
    lv_install = vault / "tools/bin/lv-install"
    if DRY:
        say(f"  would run  python3 {lv_install} --skill-only   and   --plists-only")
    elif lv_install.exists():
        for flag in ("--skill-only", "--plists-only"):
            r = subprocess.run(["/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable,
                                str(lv_install), flag, "--settings", str(SETTINGS),
                                "--agents", str(HOME / "Library/LaunchAgents"),
                                "--skill", str(CLAUDE / "skills/remember")],
                               capture_output=True, text=True)
            say("  lv-install " + flag + ": " + (r.stdout.strip().splitlines() or [r.stderr.strip()[:200]])[-1])
    say("  next: load the harvest + sweep jobs when you're ready (see modules/life-vault/README.md)")


def mod_tracker(yes: bool) -> None:
    tr, hb = HOME / "semester-tracker", HOME / "semester-heartbeat"
    st = M / "semester-tracker"
    place(st / "tracker", tr)
    for d in ("gradescope", "pollev", "assignment-watch"):
        place(st / d, tr / d)
    place(st / "tools/ocr-pdf.swift", tr / "tools/ocr-pdf.swift")
    if not (tr / "config.json").exists():
        cfg = json.loads(fill((st / "tracker/config.example.json").read_text()))
        cfg["timezone"] = VALUES["TIMEZONE"]
        cfg["canvas_host"] = VALUES["CANVAS_HOST"]
        say(f"  {'would write' if DRY else 'write'}  {tr / 'config.json'} (from config.example.json)")
        if not DRY:
            tr.mkdir(parents=True, exist_ok=True)
            (tr / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    place(st / "heartbeat", hb)
    for f in ("today.py",):
        place(M / "hooks" / f, CLAUDE / "hooks" / f)
    place(st / "hooks/deadlines_guard.py", CLAUDE / "hooks/deadlines_guard.py")
    place(st / "scheduled-tasks/morning-brief/SKILL.md", tr / "scheduled-tasks/morning-brief/SKILL.md")
    for repo in (tr, hb):
        if not (repo / ".git").exists():
            run(["git", "init", "-q", "-b", "main"], cwd=repo)
    say("  next: edit ~/semester-tracker/config.json, schedule.md, tags.json; replace the example rows in deadlines.json")


def mod_mods(yes: bool) -> None:
    place(M / "mods", HOME / "code/claude-mods")


def mod_mcp(yes: bool) -> None:
    place(M / "mcp/bridge/mcp_bridge.py", HOME / "code/scripts/mcp_bridge.py")
    for f in ("canvas.sh", "google-workspace.sh", "setup-secrets.sh"):
        place(M / "mcp/launchers" / f, CLAUDE / "mcp-wrappers" / f, mode=0o700)
    for d in ("coursework-mcp", "crawl4ai-mcp", "autohub"):
        place(M / "mcp" / d, HOME / "code" / d)
    for d, why in (("calcentral-mcp", "Berkeley students only"),
                   ("lemonsqueezy-mcp", "only if you run a Lemon Squeezy store"),
                   ("tracker-mcp", "remote to-do MCP on Cloudflare, optional")):
        if confirm(f"  also copy {d} ({why})?", yes, default=False):
            place(M / "mcp" / d, HOME / "code" / d)
    place(M / "mcp/espn-fantasy", HOME / "code/espn-fantasy-mcp-extras")
    say("  next: register servers one by one (modules/mcp/README.md); secrets go in with ~/.claude/mcp-wrappers/setup-secrets.sh, which YOU run")


def mod_skills(yes: bool) -> None:
    for s in ("hw", "verify-date", "lock-in"):
        place(M / "skills" / s, CLAUDE / "skills" / s)


def mod_ntfy(yes: bool) -> None:
    env = HOME / "semester-tracker/.ntfy.env"
    if env.exists():
        say(f"  skip (exists)  {env}")
        return
    topic = "cs-" + secrets.token_urlsafe(18).replace("_", "x").replace("-", "y")
    say(f"  {'would write' if DRY else 'write'}  {env} (random topic, chmod 600)")
    if DRY:
        return
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text(f"NTFY_TOPIC={topic}\n")
    env.chmod(stat.S_IRUSR | stat.S_IWUSR)
    say("  your topic is in that file. Read it yourself (cat it in your own terminal) and subscribe in the ntfy app.")


RUNNERS = {"core": mod_core, "hooks": mod_hooks, "icloud-guard": mod_icloud, "life-vault": mod_vault,
           "semester-tracker": mod_tracker, "mods": mod_mods, "mcp": mod_mcp, "skills": mod_skills,
           "ntfy": mod_ntfy}


def main() -> int:
    global DRY
    ap = argparse.ArgumentParser(description="Install claude-setup modules (never overwrites files).")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen; change nothing")
    ap.add_argument("--modules", help="comma-separated list (default: ask for each)")
    ap.add_argument("--yes", action="store_true", help="accept defaults without asking")
    a = ap.parse_args()
    DRY = a.dry_run

    print(f"claude-setup installer{'  (DRY RUN: nothing will change)' if DRY else ''}")
    print(f"HOME = {HOME}\n")
    user = re.sub(r"[^a-z0-9]", "", os.environ.get("USER", "me").lower()) or "me"
    VALUES.update({
        "HOME": str(HOME),
        "VAULT": str(HOME / "life"),
        "NAME": os.environ.get("CS_NAME") or ask("Your first name (used in CLAUDE.md)", "the user", a.yes),
        "EMAILS": os.environ.get("CS_EMAILS") or ask("Your email addresses, comma-separated", "(fill in)", a.yes),
        "LABEL_PREFIX": os.environ.get("CS_LABEL_PREFIX") or ask("launchd label prefix", f"local.{user}", a.yes),
        "TIMEZONE": os.environ.get("CS_TIMEZONE") or ask("Your timezone (IANA name)", "America/Los_Angeles", a.yes),
        "CANVAS_HOST": os.environ.get("CS_CANVAS_HOST") or ask("Your school's Canvas host (blank if none)", "canvas.instructure.com", a.yes),
    })
    VALUES["MAIL_ACCOUNTS"] = VALUES["EMAILS"].replace(" ", "")
    VALUES["BRIEF_EMAIL"] = VALUES["EMAILS"].split(",")[0].strip()

    if a.modules:
        chosen = [m.strip() for m in a.modules.split(",") if m.strip()]
        unknown = [m for m in chosen if m not in RUNNERS]
        if unknown:
            print(f"unknown module(s): {', '.join(unknown)}. Known: {', '.join(RUNNERS)}")
            return 2
    else:
        chosen = [name for name, desc in MODULES
                  if confirm(f"Install {name}: {desc}?", a.yes, default=name not in ("icloud-guard",))]
    for name in chosen:
        print(f"\n== {name}")
        RUNNERS[name](a.yes)
    print("\n== settings.json")
    merge_settings(set(chosen))
    print(f"\nDone{' (dry run)' if DRY else ''}. Next: open Claude Code and paste BOOTSTRAP.md to finish setup with Claude.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
