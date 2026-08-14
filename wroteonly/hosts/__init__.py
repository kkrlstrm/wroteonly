"""Host adapters — the only part of wroteonly that knows which agent it is inside.

The engine ports unchanged; only the wire format differs. Same split as
agent-guard/codex-guard, for the same reason: the interesting logic is host-neutral
and the host-specific part is thirty lines of JSON shape.

Two hosts today: Claude Code and the OpenAI Codex CLI. Both deliver a hook payload
on stdin and signal back with an exit code plus optional JSON on stdout. The fields
wroteonly needs — `session_id`, `cwd`, `tool_name`, `tool_input`, `tool_use_id`,
`permission_mode`, `stop_hook_active` — are spelled identically on both, which is
what makes one normaliser sufficient.

WHERE THEY GENUINELY DIVERGE (verified against each host's docs, 2026-08-14)

    capability                     Claude Code            Codex
    ------------------------------------------------------------------------
    PreToolUse hard block          exit 2                 exit 2
    PreToolUse soft deny           permissionDecision     permissionDecision
                                   "deny"                 "deny" (+ legacy
                                                          {"decision":"block"})
    PostToolUse can block          NO — "tool already     YES — accepts
                                   ran", exit 2 has no    {"decision":"block"}
                                   blocking effect
    Stop forces continuation       exit 2                 {"decision":"block",
                                                           "reason": ...}
    Rewrite a tool's input         not available          updatedInput

    The PostToolUse row is why the Stop gate carries enforcement on both hosts
    rather than the post-write check: it is the only lever that exists on both.

INVARIANT
    A host adapter never decides anything. It converts a payload in and a verdict
    out, and nothing else — so a new host is a new file here and no change anywhere
    else in the package.
"""

from __future__ import annotations

import json
import os

PRE_TOOL_USE = "PreToolUse"
POST_TOOL_USE = "PostToolUse"
STOP = "Stop"
SESSION_START = "SessionStart"


class Host:
    """Base adapter. Subclasses override only what actually differs."""

    name = "generic"
    #: Can this host block at PostToolUse? Claude Code cannot.
    post_can_block = False

    # -- input --------------------------------------------------------------

    def parse(self, payload: dict) -> dict:
        """Normalise a raw hook payload into the fields wroteonly uses."""
        tool_input = payload.get("tool_input")
        return {
            "host": self.name,
            "event": payload.get("hook_event_name") or "",
            "run_id": payload.get("session_id") or "",
            "cwd": payload.get("cwd") or os.getcwd(),
            "tool_name": payload.get("tool_name") or "",
            "tool_input": tool_input if isinstance(tool_input, dict) else {},
            "tool_use_id": payload.get("tool_use_id") or "",
            "permission_mode": payload.get("permission_mode") or "",
            "stop_hook_active": bool(payload.get("stop_hook_active")),
            "transcript_path": payload.get("transcript_path") or "",
        }

    # -- output -------------------------------------------------------------
    # Every emitter returns (stdout_text, stderr_text, exit_code) so the runner
    # stays a pure function of the verdict and testing needs no subprocess.

    def noop(self):
        return "", "", 0

    def advise(self, message: str, event: str):
        """Non-blocking context injection. Identical on both hosts."""
        return json.dumps({
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": message,
            }
        }), "", 0

    def deny_tool(self, message: str, event: str = PRE_TOOL_USE):
        """Soft deny: the tool does not run and the model is told why."""
        return json.dumps({
            "hookSpecificOutput": {
                "hookEventName": event,
                "permissionDecision": "deny",
                "permissionDecisionReason": message,
            }
        }), "", 0

    def block_tool(self, message: str):
        """Hard block. exit 2 on both hosts; survives bypassPermissions."""
        return "", "wroteonly: %s\n" % message, 2

    def keep_going(self, message: str):
        """Refuse to let the agent stop, so it can fix what it broke.

        This is wroteonly's enforcement lever. Overridden per host.
        """
        raise NotImplementedError


class ClaudeCode(Host):
    """Claude Code.

    Stop: exit 2 prevents Claude from stopping and shows the message.
    PostToolUse: cannot block — exit 2 "has no blocking effect on this event".
    """

    name = "claude-code"
    post_can_block = False

    def keep_going(self, message: str):
        return "", message + "\n", 2


class Codex(Host):
    """OpenAI Codex CLI.

    Stop: `{"decision": "block", "reason": ...}` tells Codex to continue and build
    a continuation prompt from the reason text.
    PostToolUse: accepts the same block shape, so a violation can be surfaced one
    tool call earlier than on Claude Code.

    Note the trust-on-first-use constraint: Codex records a `trusted_hash` of the
    hook command in `config.toml`. Keep the entry script byte-stable and put churn
    in the declaration JSON, which is not hashed, or every edit re-prompts.
    """

    name = "codex"
    post_can_block = True

    def keep_going(self, message: str):
        return json.dumps({"decision": "block", "reason": message}), "", 0

    def block_after_tool(self, message: str):
        return json.dumps({"decision": "block", "reason": message}), "", 0


HOSTS = {ClaudeCode.name: ClaudeCode, Codex.name: Codex}


def detect(payload: dict | None = None) -> Host:
    """Work out which host we are running inside.

    Order: explicit override, then host-specific environment markers, then a
    payload-shape tell, then Claude Code as the default. `WROTEONLY_HOST` exists so
    an unattended job can be unambiguous rather than relying on inference.
    """
    forced = (os.environ.get("WROTEONLY_HOST") or "").strip().lower()
    if forced in HOSTS:
        return HOSTS[forced]()

    if os.environ.get("CODEX_HOME") or os.environ.get("CODEX_SANDBOX"):
        return Codex()
    if os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("CLAUDECODE"):
        return ClaudeCode()

    if payload:
        # Codex sends `turn_id` and `model` on every event; Claude Code sends
        # `prompt_id` and does not send `turn_id`.
        if payload.get("turn_id") and "prompt_id" not in payload:
            return Codex()
        if payload.get("prompt_id"):
            return ClaudeCode()

    return ClaudeCode()
