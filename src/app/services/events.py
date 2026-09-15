"""Structured event stream — the log you read when something breaks.

Format (one event per line, never wrapped):

    2026-09-01 01:52:57 INFO ConversationService  turn.finished     status=ok duration=84.0s tools=6

  timestamp  · level · component · event · fields

Why this shape:
  • component tells you WHERE it happened — the class that actually emitted the
    event, not the module the logger happens to live in;
  • event is a dotted name, so `grep deploy.` gives the whole deploy story and
    `grep turn.failed` gives only the bad turns;
  • fields are logfmt key=value pairs: greppable by eye, parseable by machine,
    and they never turn into prose that has to be re-read to find a number;
  • fixed-width columns keep the left edge aligned, which is what makes a long
    log scannable at all.

Everything lives in one module on purpose: it is the complete vocabulary of what
this bot can say about itself. Adding an event here is a deliberate act, and it
is immediately obvious when two events describe the same thing differently.
"""
from __future__ import annotations

import logging

from ..infrastructure.logs.setup import EVENT_LOGGER

log = logging.getLogger(EVENT_LOGGER)

COMPONENT_WIDTH = 19
EVENT_WIDTH = 17


def _value(value: object) -> str:
    """Render one field value. Strings with spaces are quoted, as logfmt wants."""
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        text = f"{value:.3f}".rstrip("0").rstrip(".")
        return text or "0"
    text = str(value)
    if text == "" or any(ch.isspace() for ch in text) or '"' in text:
        return '"' + text.replace('"', "'").replace("\n", " ") + '"'
    return text


def _emit(component: str, event: str, level: int = logging.INFO, **fields: object) -> None:
    pairs = " ".join(f"{key}={_value(value)}" for key, value in fields.items()
                     if value is not None and value != "")
    log.log(level, "%-*s %-*s %s", COMPONENT_WIDTH, component, EVENT_WIDTH, event, pairs)


def _secs(seconds: float) -> str:
    return f"{seconds:.1f}s"


# ---- application lifecycle ------------------------------------------------- #
def started(version: str, session: str | None, model: str) -> None:
    _emit("Application", "app.started", version=version[:7], model=model,
          session=(session or "new")[:8])


def stopping() -> None:
    _emit("Application", "app.stopping")


def claude_restarted(reason: str, session: str | None) -> None:
    _emit("ClaudeProcess", "claude.restarted", reason=reason, session=(session or "?")[:8])


def claude_died(detail: str) -> None:
    _emit("ClaudeProcess", "claude.died", logging.WARNING, detail=detail[:200])


# ---- conversation ---------------------------------------------------------- #
def turn_queued(source: str, chars: int, waiting: int) -> None:
    _emit("ConversationService", "turn.queued", source=source, chars=chars,
          queued=waiting if waiting > 1 else None)


def turn_started(source: str, preview: str) -> None:
    _emit("ConversationService", "turn.started", source=source, text=preview)


def turn_finished(status: str, seconds: float, tools: int, tokens: int, cost: float) -> None:
    _emit("ConversationService", "turn.finished", status=status, duration=_secs(seconds),
          tools=tools, tokens=tokens, cost_usd=cost)


def turn_failed(status: str, error: str) -> None:
    _emit("ConversationService", "turn.failed", logging.WARNING, status=status,
          error=error[:200])


def turn_interrupted() -> None:
    _emit("ConversationService", "turn.interrupted")


def queue_cleared(dropped: int) -> None:
    _emit("ConversationService", "queue.cleared", dropped=dropped)


# ---- speech ---------------------------------------------------------------- #
def voice_received(seconds: float) -> None:
    _emit("SpeechService", "stt.received", audio=_secs(seconds))


def voice_recognized(chars: int, seconds: float, replacements: int) -> None:
    _emit("SpeechService", "stt.recognized", chars=chars, duration=_secs(seconds),
          fixed=replacements or None)


def voice_empty() -> None:
    _emit("SpeechService", "stt.empty", logging.WARNING, reason="no speech detected")


def terms_applied(count: int, sample: str) -> None:
    _emit("TermsService", "terms.applied", count=count, sample=sample)


def term_learned(canonical: str, variants: list[str]) -> None:
    _emit("TermsService", "terms.learned", canonical=canonical, variants=", ".join(variants))


# ---- files ----------------------------------------------------------------- #
def file_received(name: str, size: int) -> None:
    _emit("FilesRouter", "file.received", name=name, bytes=size)


def file_sent(path: str, size: int) -> None:
    _emit("FilesRouter", "file.sent", path=path, bytes=size)


# ---- self-update ------------------------------------------------------------ #
def deploy_started(note: str, prev: str, new: str, files: int) -> None:
    _emit("SelfUpdateService", "deploy.started", **{"from": prev[:7]}, to=new[:7],
          files=files, note=note)


def deploy_blocked(reason: str) -> None:
    _emit("SelfUpdateService", "deploy.blocked", logging.WARNING, reason=reason)


def deploy_verdict(phase: str, note: str, reason: str | None) -> None:
    _emit("SelfUpdateService", "deploy.verdict",
          logging.INFO if phase == "ok" else logging.WARNING,
          phase=phase, note=note, reason=reason)


def rollback_started(target: str, subject: str) -> None:
    _emit("SelfUpdateService", "deploy.rollback", logging.WARNING,
          target=target[:7], subject=subject)


def stable_marked(sha: str) -> None:
    _emit("SelfUpdateService", "version.stable", commit=sha[:7])


# ---- goals -------------------------------------------------------------------- #
def goal_created(goal_id: int, text: str, budget: float) -> None:
    _emit("GoalService", "goal.created", id=goal_id, budget_usd=budget, text=text)


def goal_woke(goal_id: int, turn_today: int, budget_left: float) -> None:
    _emit("GoalService", "goal.woke", id=goal_id, turn=turn_today, budget_left=budget_left)


def goal_step(goal_id: int, step: int, cost: float, status: str) -> None:
    _emit("GoalService", "goal.step", id=goal_id, step=step, cost_usd=cost, status=status)


def goal_status(goal_id: int, status: str) -> None:
    _emit("GoalService", "goal.status", id=goal_id, status=status)


# ---- proactivity ------------------------------------------------------------- #
def reminder_scheduled(reminder_id: int, kind: str, due: str) -> None:
    _emit("ProactiveService", "reminder.scheduled", id=reminder_id, kind=kind, due=due)


def reminder_fired(reminder_id: int, attempt: int, kind: str) -> None:
    _emit("ProactiveService", "reminder.fired", id=reminder_id, attempt=attempt, kind=kind)


def reminder_snoozed(reminder_id: int, minutes: int) -> None:
    _emit("ProactiveService", "reminder.snoozed", id=reminder_id, minutes=minutes)


def reminder_closed(reminder_id: int, status: str) -> None:
    _emit("ProactiveService", "reminder.closed", id=reminder_id, status=status)


# ---- health ------------------------------------------------------------------ #
def health_changed(what: str, ok: bool) -> None:
    _emit("HealthService", "health.changed", logging.INFO if ok else logging.WARNING,
          dependency=what, reachable=ok)


# ---- access ------------------------------------------------------------------- #
def access_denied(user_id: int, username: str | None) -> None:
    _emit("AuthMiddleware", "access.denied", logging.WARNING,
          user_id=user_id, username=username or "?")
