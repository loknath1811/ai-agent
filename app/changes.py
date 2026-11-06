"""A tiny in-memory "something changed" counter, so the dashboard can update itself.

Every time a commit saves a change to tasks, people, teams or divisions, the counter
goes up by one. The dashboard asks "what is the number?" every couple of seconds
(a question answered from memory - no database work) and reloads only when it differs.

It is bumped AFTER the commit, so a browser that reloads on the new number always
sees the saved data. It lives in this process's memory, so it assumes ONE app process.
"""
from sqlalchemy import event
from sqlalchemy.orm import Session

_version = 0
_WATCHED_TABLES = {"tasks", "employees", "teams", "divisions"}
_installed = False


def current() -> int:
    return _version


def bump() -> None:
    global _version
    _version += 1


def install() -> None:
    """Start watching sessions. Safe to call more than once."""
    global _installed
    if _installed:
        return
    _installed = True

    @event.listens_for(Session, "after_flush")
    def _note_changes(session, flush_context):
        for obj in list(session.new) + list(session.dirty) + list(session.deleted):
            if getattr(obj, "__tablename__", None) in _WATCHED_TABLES:
                session.info["changed"] = True
                return

    @event.listens_for(Session, "after_commit")
    def _bump_after_commit(session):
        if session.info.pop("changed", False):
            bump()

    @event.listens_for(Session, "after_rollback")
    def _forget_on_rollback(session):
        session.info.pop("changed", None)
