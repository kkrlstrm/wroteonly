#!/usr/bin/env python3
"""Wire wroteonly into Claude Code and/or the OpenAI Codex CLI.

Both hosts want the same three events; only the file and the nesting differ.

    Claude Code   ~/.claude/settings.json    hooks.PreToolUse[].hooks[]
    Codex         ~/.codex/hooks.json        hooks.PreToolUse[].hooks[]

Merge-aware (never clobbers another tool's hooks), idempotent (re-running updates
in place), and it backs up whatever was there first.

    python3 install.py                    # both hosts, whichever are present
    python3 install.py --host codex       # just one
    python3 install.py --dry-run          # print what would be written
    python3 install.py --uninstall        # remove only wroteonly's entries

A note specific to Codex: it trust-pins the hook command by hash (`trusted_hash` in
config.toml), so it may prompt once. `bin/wroteonly-hook.py` is deliberately a
three-line shim that never needs editing — keep churn in the declaration JSON, which
is not hashed, and you will not be re-prompted.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

REPO = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(REPO, "bin", "wroteonly-hook.py")
MARKER = "wroteonly-hook.py"

#: PreToolUse gates the writes a tool names outright; Stop is the verification gate
#: and the only lever that can force a continuation on both hosts; PostToolUse is
#: attribution only. Matchers are regex over the tool name.
EVENTS = {
    "PreToolUse": "Edit|MultiEdit|Write|NotebookEdit|Bash|apply_patch",
    "PostToolUse": "Edit|MultiEdit|Write|NotebookEdit",
    "Stop": ".*",
}

TARGETS = {
    "claude-code": os.path.join(os.path.expanduser("~"), ".claude", "settings.json"),
    "codex": os.path.join(
        os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex"),
        "hooks.json"),
}


def _entry(host: str) -> dict:
    return {
        "type": "command",
        "command": '%s "%s"' % (json.dumps(sys.executable)[1:-1], HOOK),
        "statusMessage": "wroteonly verifying the declared write set",
        "timeout": 120,
    }


def _load(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _strip(groups: list) -> list:
    """Remove only wroteonly's hooks, preserving every other tool's."""
    kept = []
    for group in groups or []:
        hooks = [h for h in group.get("hooks", [])
                 if MARKER not in (h.get("command") or "")]
        if hooks:
            group = dict(group, hooks=hooks)
            kept.append(group)
        elif not group.get("hooks"):
            kept.append(group)
    return kept


def wire(host: str, doc: dict, uninstall: bool) -> dict:
    hooks = doc.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        doc["hooks"] = hooks

    for event, matcher in EVENTS.items():
        groups = _strip(hooks.get(event, []))
        if not uninstall:
            groups.append({"matcher": matcher, "hooks": [_entry(host)]})
        if groups:
            hooks[event] = groups
        elif event in hooks:
            del hooks[event]

    if not hooks:
        doc.pop("hooks", None)
    return doc


def apply_to(host: str, path: str, uninstall: bool, dry_run: bool) -> bool:
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        print("·  %-12s skipped — %s does not exist" % (host, parent))
        return False

    doc = wire(host, _load(path), uninstall)
    rendered = json.dumps(doc, indent=2) + "\n"

    if dry_run:
        action = "uninstall" if uninstall else "install"
        print("# --dry-run (%s) — would write %s:\n%s" % (action, path, rendered))
        return True

    if os.path.exists(path):
        backup = "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(path, backup)
        print("·  backed up %s -> %s" % (path, backup))

    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(rendered)

    verb = "removed from" if uninstall else "wired into"
    print("✓  %-12s %s %s" % (host, verb, path))
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Install wroteonly's hooks.")
    ap.add_argument("--host", choices=sorted(TARGETS) + ["all"], default="all")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(HOOK):
        sys.stderr.write("✗ %s is missing — run from a complete checkout.\n" % HOOK)
        return 3

    hosts = sorted(TARGETS) if args.host == "all" else [args.host]
    touched = sum(apply_to(h, TARGETS[h], args.uninstall, args.dry_run)
                  for h in hosts)

    if not touched:
        sys.stderr.write(
            "\n✗ Nothing to do — neither ~/.claude nor ~/.codex was found.\n"
            "  wroteonly also works with no hooks at all:\n"
            "      bin/wroteonly declare --create 'docs/**/*.md' --run-id job1\n"
            "      <run your agent>\n"
            "      bin/wroteonly verify --run-id job1\n")
        return 3

    if not args.dry_run and not args.uninstall:
        print("\n·  Start a new session so the hooks load.")
        print("·  wroteonly stays inert until a declaration exists — write one to")
        print("   .wroteonly.json in the project, or set $WROTEONLY_DECLARATION.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
