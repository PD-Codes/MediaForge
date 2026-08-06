"""A tiny in-process publish/subscribe hub for Server-Sent Events.

MediaForge had exactly one SSE endpoint before this (Syncplay), with its own
subscriber list, its own queue handling and its own keep-alive loop. The
Operations view needed the same thing for worker status, and a third feature
would have needed it again -- so the mechanics live here once and each feature
brings only its topic name and its payload.

What this is, precisely: a fan-out of small JSON messages to whoever is
currently listening, and nothing else. It is not a queue and not a log. A
subscriber that cannot keep up loses the oldest messages rather than growing
without bound, and a subscriber that disconnects is simply forgotten. Anything
that must not be lost belongs in the database, with SSE used only to say "go
look" -- which is exactly how the worker topic is used: the payload is a hint,
:func:`worker_registry.snapshot` remains the source of truth.

Why bounded queues matter here: a publisher is a background worker thread. If a
browser tab is throttled (backgrounded, or on a slow link) and its queue grew
freely, a busy download queue would pin the difference in RAM for as long as
that tab stayed open. Dropping the oldest message is safe for a "go look" hint
by construction -- the newest one carries the same instruction.

Threading: publish() is called from worker threads and must never block or
raise into them. Subscriber registration happens on request threads. The lock
is held only for list mutation and the non-blocking put, never across a network
write.
"""

from __future__ import annotations

import json
import queue
import threading
import time

from ..logger import get_logger

logger = get_logger(__name__)

# Messages a single subscriber may fall behind by before the oldest are
# dropped. Small on purpose: see the module docstring -- these are hints, and
# the newest hint supersedes every older one.
MAX_PENDING = 32

# Upper bound on concurrent listeners per topic.
#
# Low on purpose, and the reason is deployment rather than memory: each open
# stream occupies a WSGI worker thread for as long as it lives. Under a sync
# worker model, N open streams means N threads that cannot serve anything else,
# so a generous limit here turns "an admin left some tabs open" into "the app
# stops responding". A browser opens one EventSource per tab, and the
# Operations page closes its stream when the tab is hidden or the user switches
# away, so a real deployment needs very few.
MAX_SUBSCRIBERS = 12

# Seconds between keep-alive comments on an otherwise silent stream. Proxies
# and load balancers commonly close an idle connection after 30-60s, and the
# browser's automatic reconnect would then hammer the endpoint.
KEEPALIVE_SECONDS = 15

_lock = threading.Lock()
_subscribers: dict[str, list[queue.Queue]] = {}


def publish(topic: str, payload: dict) -> None:
    """Hand *payload* to every current listener on *topic*. Never raises.

    Safe to call from any thread, including a worker mid-job: it does a
    non-blocking put per subscriber and returns. A full queue loses its oldest
    message instead of blocking the publisher.
    """
    try:
        with _lock:
            listeners = list(_subscribers.get(topic) or ())
        if not listeners:
            return
        message = {"topic": topic, "ts": time.time(), "data": payload}
        for q in listeners:
            try:
                q.put_nowait(message)
            except queue.Full:
                try:
                    q.get_nowait()      # drop the oldest
                    q.put_nowait(message)
                except Exception:
                    pass
    except Exception:
        logger.debug("[Events] publish on %r failed", topic, exc_info=True)


def subscribe(topic: str):
    """Register a listener, or return None if *topic* is at MAX_SUBSCRIBERS.

    The caller must pass the returned queue to :func:`unsubscribe` when done --
    :func:`stream` does that in a finally block.
    """
    q: queue.Queue = queue.Queue(maxsize=MAX_PENDING)
    with _lock:
        listeners = _subscribers.setdefault(topic, [])
        if len(listeners) >= MAX_SUBSCRIBERS:
            logger.warning("[Events] Refusing subscriber: topic %r is at %d listeners",
                           topic, MAX_SUBSCRIBERS)
            return None
        listeners.append(q)
    return q


def unsubscribe(topic: str, q) -> None:
    with _lock:
        listeners = _subscribers.get(topic)
        if not listeners:
            return
        try:
            listeners.remove(q)
        except ValueError:
            pass
        if not listeners:
            _subscribers.pop(topic, None)


def subscriber_count(topic: str) -> int:
    with _lock:
        return len(_subscribers.get(topic) or ())


def format_event(data, event: str | None = None) -> str:
    """One message in SSE wire format.

    json.dumps handles the escaping, and the result cannot contain a raw
    newline, so the payload can never break out of its ``data:`` line and
    inject a second event.
    """
    body = json.dumps(data, default=str)
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {body}\n\n"


def stream(topic: str, initial=None, keepalive: int = KEEPALIVE_SECONDS):
    """Generator yielding SSE frames for *topic* until the client goes away.

    *initial* (optional) is sent immediately, so a freshly-connected client has
    the current state without a separate fetch and without waiting for the next
    change.

    Yields a comment line every *keepalive* seconds while nothing is happening.
    That both keeps proxies from closing the connection and gives the generator
    a chance to notice a disconnected client -- Flask raises on the write, this
    generator unwinds, and the finally block drops the subscription.
    """
    q = subscribe(topic)
    if q is None:
        yield format_event({"error": "too_many_subscribers"}, event="error")
        return
    try:
        # Tells the browser's EventSource to wait this long before reconnecting
        # after a drop, instead of its default (often 3s) -- with a bounded
        # subscriber count, a reconnect storm is the failure mode to avoid.
        yield "retry: 5000\n\n"
        if initial is not None:
            yield format_event(initial, event="init")
        while True:
            try:
                message = q.get(timeout=keepalive)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            yield format_event(message["data"], event="update")
    except GeneratorExit:
        raise
    except Exception:
        logger.debug("[Events] stream on %r ended", topic, exc_info=True)
    finally:
        unsubscribe(topic, q)


def sse_headers() -> dict:
    """Response headers every SSE endpoint needs.

    ``X-Accel-Buffering: no`` is the one that is easy to forget: behind nginx
    the stream is buffered by default and the client sees nothing until the
    buffer fills, which for these small messages is approximately never.
    """
    return {
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
