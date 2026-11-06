"""Measure how many AI calls each operation makes, how long they take, and how many tokens they use.

Nothing here changes behaviour. It only writes one JSON line per AI call and one per
operation to logs/llm_calls.jsonl (read it with:  python -m scripts.llm_report).

Usage:
    async with metrics.measure("chat"):        # one whole operation
        ... any AI calls inside are recorded and counted ...
"""
import contextvars
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from langchain_core.callbacks import AsyncCallbackHandler
from sqlalchemy import event

log = logging.getLogger(__name__)
LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "llm_calls.jsonl"

_current: contextvars.ContextVar = contextvars.ContextVar("llm_metrics", default=None)


def _write(record: dict) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        record["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:  # measuring must never break the app
        log.debug("could not write metrics: %s", e)


class _Recorder(AsyncCallbackHandler):
    """Attached to the model; sees the start and end of every AI call."""

    def __init__(self, operation: str):
        self.operation = operation
        self.calls = 0
        self.model_seconds = 0.0
        self.tokens_in = 0
        self.tokens_out = 0
        self._starts: dict = {}
        self.note = ""  # optional short result text, e.g. what happened to an email
        # database activity during this operation
        self.db_statements = 0   # SQL statements run
        self.db_seconds = 0.0    # time spent inside those statements
        self.db_checkouts = 0    # times a connection was borrowed from the pool (each may add a ping)
        self.db_commits = 0
        self.db_rollbacks = 0
        # emails sent during this operation
        self.emails_sent = 0
        self.email_seconds = 0.0

    async def _start(self, run_id) -> None:
        # the same call can be reported twice when callbacks are inherited; ignore the repeat
        self._starts.setdefault(run_id, time.perf_counter())

    async def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs) -> None:
        await self._start(run_id)

    async def on_llm_start(self, serialized, prompts, *, run_id, **kwargs) -> None:
        await self._start(run_id)

    async def on_llm_end(self, response, *, run_id, **kwargs) -> None:
        started = self._starts.pop(run_id, None)
        if started is None:
            return
        seconds = time.perf_counter() - started
        t_in = t_out = 0
        try:
            usage = getattr(response.generations[0][0].message, "usage_metadata", None) or {}
            t_in, t_out = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
        except Exception:
            pass
        self.calls += 1
        self.model_seconds += seconds
        self.tokens_in += t_in
        self.tokens_out += t_out
        _write({"type": "llm_call", "operation": self.operation, "seconds": round(seconds, 3),
                "tokens_in": t_in, "tokens_out": t_out, "ok": True})

    async def on_llm_error(self, error, *, run_id, **kwargs) -> None:
        started = self._starts.pop(run_id, None)
        if started is None:
            return
        self.calls += 1
        seconds = time.perf_counter() - started
        self.model_seconds += seconds
        _write({"type": "llm_call", "operation": self.operation, "seconds": round(seconds, 3),
                "ok": False, "error": type(error).__name__})


def config() -> dict:
    """Pass this as `config=` to any model call so it is measured. Empty if nothing is being measured."""
    rec: Optional[_Recorder] = _current.get()
    return {"callbacks": [rec]} if rec else {}


@asynccontextmanager
async def measure(operation: str):
    """Time one whole operation (AI + database + email) and count its AI calls."""
    rec = _Recorder(operation)
    token = _current.set(rec)
    started = time.perf_counter()
    try:
        yield rec
    finally:
        _current.reset(token)
        total = time.perf_counter() - started
        _write({"type": "operation", "operation": rec.operation, "total_seconds": round(total, 3),
                "model_seconds": round(rec.model_seconds, 3), "llm_calls": rec.calls,
                "tokens_in": rec.tokens_in, "tokens_out": rec.tokens_out, "note": rec.note[:120],
                "db_statements": rec.db_statements, "db_seconds": round(rec.db_seconds, 3),
                "db_checkouts": rec.db_checkouts, "db_commits": rec.db_commits,
                "db_rollbacks": rec.db_rollbacks,
                "emails_sent": rec.emails_sent, "email_seconds": round(rec.email_seconds, 3)})
        log.info("[metrics] %s: %.2fs total, %d AI call(s), %d DB statement(s), %d checkout(s)",
                 rec.operation, total, rec.calls, rec.db_statements, rec.db_checkouts)


def install_db_hooks(async_engine) -> None:
    """Count database activity per measured operation. Call once, right after the engine is created."""
    sync_engine = async_engine.sync_engine

    @event.listens_for(sync_engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        context._metrics_t0 = time.perf_counter()

    @event.listens_for(sync_engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        rec = _current.get()
        if rec is not None:
            rec.db_statements += 1
            rec.db_seconds += time.perf_counter() - getattr(context, "_metrics_t0", time.perf_counter())

    @event.listens_for(sync_engine, "commit")
    def _commit(conn):
        rec = _current.get()
        if rec is not None:
            rec.db_commits += 1

    @event.listens_for(sync_engine, "rollback")
    def _rollback(conn):
        rec = _current.get()
        if rec is not None:
            rec.db_rollbacks += 1

    @event.listens_for(sync_engine.pool, "checkout")
    def _checkout(dbapi_conn, record, proxy):
        rec = _current.get()
        if rec is not None:
            rec.db_checkouts += 1


def record_email(seconds: float) -> None:
    """Called by the email sender after each message, so the report can show email cost."""
    rec = _current.get()
    if rec is not None:
        rec.emails_sent += 1
        rec.email_seconds += seconds
