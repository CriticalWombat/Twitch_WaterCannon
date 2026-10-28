"""
web.py
------
Tiny read-only web server for the OBS overlay, built on the standard library.

  GET /overlay   the overlay page (add ?test=1 for the hidden test panel)
  GET /events    Server-Sent Events: a snapshot on connect, then live messages
  GET /state     the current leaderboard as JSON (handy for debugging)
  GET /healthz   "ok"

There is no write endpoint and nothing here can reach the cannon. Messages
reach overlays through Hub.publish(); main.py builds them after scoring.

Message shape (JSON, one per SSE event):
  {"kind": "snapshot", "state": {...}}                       sync, no alert
  {"kind": "alert", "alert": {type, h, s}, "state": {...}}   play an alert
  ... plus "final": true on the winner alert (state = the closing board).
"""

import asyncio
import json
import logging
import urllib.parse
from pathlib import Path
from typing import Callable, List, Optional

import scoring

log = logging.getLogger("water_cannon.web")

BARRAGE_MIN_SHOTS = 5
MAX_SSE_CLIENTS = 10
PING_EVERY_SEC = 15
REQUEST_TIMEOUT_SEC = 10
CLIENT_QUEUE_SIZE = 50


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------
def shot_alert(result: scoring.Result, first_blood_bonus: int) -> dict:
    """The alert for a scored redemption. A dethrone beats First Blood beats a barrage."""
    if result.dethrone:
        d = result.dethrone
        return {"type": "dethrone", "h": "DETHRONED",
                "s": f"{d.new_name} takes the crown from {d.old_name} | lead {d.margin}"}
    if result.first_blood:
        return {"type": "first", "h": "FIRST BLOOD",
                "s": f"{result.name} draws first | +{first_blood_bonus} bonus"}
    if result.shots >= BARRAGE_MIN_SHOTS:
        return {"type": "barrage", "h": "BARRAGE", "s": f"{result.name} | {result.shots} SHOTS"}
    return {"type": "shot", "h": result.name,
            "s": f"FIRED {result.shots} SHOT{'S' if result.shots != 1 else ''}"}


def winner_alert(recap: scoring.Recap) -> dict:
    return {"type": "winner", "h": "CHAMPION",
            "s": f"{recap.winner.name} wins with {recap.winner.score} shots"}


def snapshot_message(state: dict) -> dict:
    return {"kind": "snapshot", "state": state}


def alert_message(alert: dict, state: dict, final: bool = False) -> dict:
    msg = {"kind": "alert", "alert": alert, "state": state}
    if final:
        msg["final"] = True
    return msg


# ---------------------------------------------------------------------------
# Hub: fans messages out to every connected overlay
# ---------------------------------------------------------------------------
class Hub:
    def __init__(self):
        self._clients: List[asyncio.Queue] = []

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def subscribe(self) -> Optional[asyncio.Queue]:
        """A queue for one overlay, or None if the client limit is reached."""
        if len(self._clients) >= MAX_SSE_CLIENTS:
            return None
        q: asyncio.Queue = asyncio.Queue(maxsize=CLIENT_QUEUE_SIZE)
        self._clients.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._clients:
            self._clients.remove(q)

    def publish(self, message: dict) -> None:
        """Send to every overlay. Call from the event loop thread."""
        for q in list(self._clients):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                log.warning("Dropping an overlay client that stopped reading")
                self.unsubscribe(q)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
_REASONS = {200: "OK", 404: "Not Found", 405: "Method Not Allowed",
            400: "Bad Request", 503: "Service Unavailable"}


def _response(status: int, content_type: str, body: bytes) -> bytes:
    head = (
        f"HTTP/1.1 {status} {_REASONS[status]}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "X-Content-Type-Options: nosniff\r\n"
        "Connection: close\r\n\r\n"
    )
    return head.encode("ascii") + body


def _sse(message: dict) -> bytes:
    return b"data: " + json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n\n"


async def _read_request(reader: asyncio.StreamReader):
    """Return (method, path, query) or None if the request is malformed."""
    line = await asyncio.wait_for(reader.readline(), REQUEST_TIMEOUT_SEC)
    parts = line.decode("latin-1").split()
    if len(parts) != 3 or not parts[2].startswith("HTTP/"):
        return None
    # Drain the headers; we use none of them.
    for _ in range(100):
        header = await asyncio.wait_for(reader.readline(), REQUEST_TIMEOUT_SEC)
        if header in (b"\r\n", b"\n", b""):
            break
    else:
        return None
    url = urllib.parse.urlsplit(parts[1])
    return parts[0], url.path, urllib.parse.parse_qs(url.query)


async def _stream_events(reader, writer, hub: Hub, snapshot: Callable[[], dict]):
    q = hub.subscribe()
    if q is None:
        writer.write(_response(503, "text/plain", b"too many overlay connections\n"))
        return
    try:
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Cache-Control: no-store\r\n"
            b"X-Accel-Buffering: no\r\n"
            b"Connection: keep-alive\r\n\r\n"
            b"retry: 3000\n\n"
        )
        writer.write(_sse(snapshot_message(snapshot())))
        await writer.drain()
        log.info("Overlay connected (%d client%s)", hub.client_count,
                 "" if hub.client_count == 1 else "s")

        disconnected = asyncio.ensure_future(reader.read(1))   # b"" once the client leaves
        try:
            while True:
                get = asyncio.ensure_future(q.get())
                done, _ = await asyncio.wait({get, disconnected}, timeout=PING_EVERY_SEC,
                                             return_when=asyncio.FIRST_COMPLETED)
                if disconnected in done:
                    get.cancel()
                    return
                if get in done:
                    writer.write(_sse(get.result()))
                else:
                    get.cancel()
                    writer.write(b": ping\n\n")
                await writer.drain()
        finally:
            disconnected.cancel()
    finally:
        hub.unsubscribe(q)
        log.info("Overlay disconnected (%d client%s)", hub.client_count,
                 "" if hub.client_count == 1 else "s")


async def start(host: str, port: int, hub: Hub, snapshot: Callable[[], dict],
                overlay_path: str) -> asyncio.AbstractServer:
    """Start serving. Raises OSError if the port can't be bound."""
    overlay_html = Path(overlay_path).read_bytes()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request = await _read_request(reader)
            if request is None:
                writer.write(_response(400, "text/plain", b"bad request\n"))
            else:
                method, path, _query = request
                if method != "GET":
                    writer.write(_response(405, "text/plain", b"GET only\n"))
                elif path == "/events":
                    await _stream_events(reader, writer, hub, snapshot)
                elif path == "/overlay":
                    writer.write(_response(200, "text/html; charset=utf-8", overlay_html))
                elif path == "/state":
                    body = json.dumps(snapshot()).encode("utf-8")
                    writer.write(_response(200, "application/json", body))
                elif path == "/healthz":
                    writer.write(_response(200, "text/plain", b"ok\n"))
                else:
                    writer.write(_response(404, "text/plain", b"not found\n"))
            await writer.drain()
        except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError, ValueError):
            pass
        except Exception:
            log.exception("Web request failed")
        finally:
            try:
                writer.close()
            except Exception:
                pass

    server = await asyncio.start_server(handle, host, port, limit=8192)
    log.info("Overlay server listening on %s:%d", host, server.sockets[0].getsockname()[1])
    return server
