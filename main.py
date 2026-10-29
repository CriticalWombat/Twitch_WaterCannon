"""
water_cannon.py
---------------
Listens for a Twitch channel point redemption named "waterCannon" via
EventSub WebSocket and pulses a GPIO pin HIGH for 5 seconds.

Placeholders you must fill in:
  - TWITCH_CLIENT_ID      : Your Twitch application client ID
  - TWITCH_CLIENT_SECRET  : Your Twitch application client secret
  - TWITCH_REFRESH_TOKEN  : A user refresh token with channel:read:redemptions scope
  - TWITCH_BROADCASTER_ID : The numeric user ID of the channel to monitor
  - GPIO_PIN              : BCM pin number wired to your relay/cannon trigger
"""

import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import websockets
from dotenv import load_dotenv

import reveal
import scoring
import vip
import web

# Load .env file if present – works when running directly with Python.
# Docker Compose injects env vars on its own, so this is a no-op there.
load_dotenv()

# ---------------------------------------------------------------------------
# Configuration  (override via environment variables or edit directly)
# ---------------------------------------------------------------------------
TWITCH_CLIENT_ID      = os.environ.get("TWITCH_CLIENT_ID",      "PLACEHOLDER_CLIENT_ID")
TWITCH_CLIENT_SECRET  = os.environ.get("TWITCH_CLIENT_SECRET",  "PLACEHOLDER_CLIENT_SECRET")
TWITCH_REFRESH_TOKEN  = os.environ.get("TWITCH_REFRESH_TOKEN",  "PLACEHOLDER_REFRESH_TOKEN")
TWITCH_BROADCASTER_ID = os.environ.get("TWITCH_BROADCASTER_ID", "PLACEHOLDER_BROADCASTER_ID")

REWARD_NAME           = os.environ.get("REWARD_NAME",  "Water Cannon (test)")   # must match exactly (case-sensitive)
GPIO_PIN              = int(os.environ.get("GPIO_PIN", "4"))            # BCM numbering
PULSE_DURATION_SEC    = float(os.environ.get("PULSE_DURATION_SEC", "0.25"))

# Second redeem: identical behaviour, just a longer pulse.
REWARD_NAME_5SHOT        = os.environ.get("REWARD_NAME_5SHOT", "Water Cannon 5-shot")   # must match exactly (case-sensitive)
PULSE_DURATION_5SHOT_SEC = float(os.environ.get("PULSE_DURATION_5SHOT_SEC", "1.25"))

# Redeem title -> pulse duration in seconds.
REWARD_PULSES = {
    REWARD_NAME:       PULSE_DURATION_SEC,
    REWARD_NAME_5SHOT: PULSE_DURATION_5SHOT_SEC,
}

# Redeem title -> shots scored on the soak leaderboard (150 Bits = 1, 450 Bits = 5).
REWARD_SHOTS = {
    REWARD_NAME:       int(os.environ.get("SHOTS_REWARD", "1")),
    REWARD_NAME_5SHOT: int(os.environ.get("SHOTS_5SHOT",  "5")),
}
SCORE_DB_PATH         = os.environ.get("SCORE_DB_PATH", "/data/soak.db")
FIRST_BLOOD_BONUS     = int(os.environ.get("FIRST_BLOOD_BONUS", "1"))
SESSION_ONLINE_GRACE_MIN = float(os.environ.get("SESSION_ONLINE_GRACE_MIN", "180"))  # late stream.online keeps a session active this recently
SESSION_IDLE_HOURS    = float(os.environ.get("SESSION_IDLE_HOURS", "3"))            # a session idle this long is closed as stale

STREAM_STATUS_TYPES   = ("stream.online", "stream.offline")

# Scheduled winner reveal: STREAM_END_TIME minus REVEAL_MINUTES_BEFORE locks the results.
REVEAL_ENABLED = os.environ.get("REVEAL_ENABLED", "1") == "1"
STREAM_END_TIME = os.environ.get("STREAM_END_TIME", "21:30")          # HH:MM, 24h, in STREAM_TZ
STREAM_TZ = os.environ.get("STREAM_TZ", "America/New_York")
REVEAL_MINUTES_BEFORE = float(os.environ.get("REVEAL_MINUTES_BEFORE", "10"))
REVEAL_WINDOW_MIN = float(os.environ.get("REVEAL_WINDOW_MIN", "10"))   # a restart inside this window still reveals
REVEAL_TRIGGER_PATH = os.environ.get("REVEAL_TRIGGER_PATH", "/data/reveal-now")
REVEAL_POLL_SEC = 5

# VIP badge for the night's winner. Off by default; VIP_DRY_RUN=1 only logs what it would do.
# Needs a broadcaster token with the channel:manage:vips scope.
VIP_ENABLED = os.environ.get("VIP_ENABLED", "0") == "1"
VIP_DRY_RUN = os.environ.get("VIP_DRY_RUN", "1") == "1"
VIP_ON_OFFLINE_FALLBACK = os.environ.get("VIP_ON_OFFLINE_FALLBACK", "0") == "1"  # also grant it if stream.offline crowns the winner

# Overlay web server (read-only; OBS loads http://<pi>:<port>/overlay as a browser source).
WEB_ENABLED           = os.environ.get("WEB_ENABLED", "1") == "1"
WEB_HOST              = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT              = int(os.environ.get("WEB_PORT", "5000"))
OVERLAY_TOP_N         = int(os.environ.get("OVERLAY_TOP_N", "5"))
OVERLAY_PATH          = os.path.join(os.path.dirname(os.path.abspath(__file__)), "overlay.html")
SOUNDS_DIR            = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")  # alert sounds, mounted by compose

EVENTSUB_WS_URL       = "wss://eventsub.wss.twitch.tv/ws"
TWITCH_EVENTSUB_URL   = "https://api.twitch.tv/helix/eventsub/subscriptions"
TWITCH_TOKEN_URL      = "https://id.twitch.tv/oauth2/token"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("water_cannon")

# ---------------------------------------------------------------------------
# GPIO setup  (falls back to a no-op stub when RPi.GPIO isn't available,
#              e.g. during local development/testing on non-Pi hardware)
# ---------------------------------------------------------------------------
try:
    import RPi.GPIO as GPIO  # type: ignore
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(GPIO_PIN, GPIO.OUT, initial=GPIO.LOW)
    HAS_GPIO = True
    log.info("GPIO initialised on BCM pin %d", GPIO_PIN)
except (ImportError, RuntimeError) as exc:
    HAS_GPIO = False
    log.warning("RPi.GPIO not available (%s) – running in DRY-RUN mode", exc)

# ---------------------------------------------------------------------------
# Scoring  (optional: if the database can't be opened the cannon still works)
# ---------------------------------------------------------------------------
try:
    scoreboard = scoring.Scoreboard(SCORE_DB_PATH, FIRST_BLOOD_BONUS, SESSION_IDLE_HOURS)
    log.info("Scoreboard ready at %s", SCORE_DB_PATH)
except Exception:
    scoreboard = None
    log.exception("Scoreboard unavailable – cannon will fire but scores will not be kept")

hub = web.Hub()   # fans overlay messages out to every connected OBS browser source


def reveal_info() -> dict:
    """When the next winner reveal is, for the overlay's countdown. Sent as epoch
    milliseconds together with this machine's clock, so the overlay never depends on the
    OBS computer's clock or timezone. Empty if the reveal schedule is off or invalid."""
    if not REVEAL_ENABLED:
        return {}
    try:
        now = datetime.now(ZoneInfo(STREAM_TZ))
        start = reveal.next_start(now, reveal.parse_hhmm(STREAM_END_TIME), REVEAL_MINUTES_BEFORE)
        return {"reveal_at": int(start.timestamp() * 1000), "server_now": int(now.timestamp() * 1000)}
    except Exception:
        return {}


def current_state() -> dict:
    """The leaderboard as the overlay sees it (empty if scoring is unavailable)."""
    if scoreboard is None:
        state = {"active": False, "session_id": None, "standings": [],
                 "king": None, "first_blood_id": None}
    else:
        state = scoreboard.snapshot(OVERLAY_TOP_N)
    state.update(reveal_info())
    return state


# ---------------------------------------------------------------------------
# Token manager – keeps a live access token, refreshes automatically
# ---------------------------------------------------------------------------
class TokenManager:
    """
    Holds the current Twitch user access token and refreshes it on demand.

    Twitch user access tokens expire after ~60 days but can be invalidated
    earlier. This class handles two refresh scenarios:

      1. Proactive: refresh once at startup so we never start with a stale token.
      2. Reactive:  if any API call returns 401, call ensure_fresh() to refresh
                    immediately and retry.

    The refresh token itself is long-lived and only changes when a refresh
    succeeds (Twitch rotates it). The new refresh token is stored in memory;
    if you restart the container after a rotation you will need the original
    refresh token to still be valid OR export the new one from the logs.
    """

    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self._client_id      = client_id
        self._client_secret  = client_secret
        self._refresh_token  = refresh_token
        self._access_token: str | None = None
        self._lock = asyncio.Lock()

    @property
    def access_token(self) -> str:
        if self._access_token is None:
            raise RuntimeError("TokenManager.refresh() has not been called yet")
        return self._access_token

    def headers(self) -> dict:
        """Return Twitch API auth headers using the current access token."""
        return {
            "Client-Id": self._client_id,
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    async def refresh(self, client: httpx.AsyncClient) -> None:
        """Exchange the refresh token for a new access token (+ possibly new refresh token)."""
        async with self._lock:   # only one refresh at a time
            log.info("Refreshing Twitch access token …")
            response = await client.post(
                TWITCH_TOKEN_URL,
                data={
                    "grant_type":    "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id":     self._client_id,
                    "client_secret": self._client_secret,
                },
            )
            if response.status_code != 200:
                log.error("Token refresh failed: %s %s", response.status_code, response.text)
                raise RuntimeError(f"Token refresh failed with status {response.status_code}")

            data = response.json()
            self._access_token = data["access_token"]

            # Twitch rotates the refresh token on each use – keep the new one.
            if "refresh_token" in data:
                old = self._refresh_token
                self._refresh_token = data["refresh_token"]
                if old != self._refresh_token:
                    log.info(
                        "Refresh token rotated – new value: %s … "
                        "(update TWITCH_REFRESH_TOKEN in .env if you restart the container)",
                        self._refresh_token[:8],
                    )

            expires_in = data.get("expires_in", "unknown")
            log.info("Access token refreshed (expires_in=%s seconds)", expires_in)
            # Scope names are not secret; logging them shows what this token is allowed to do.
            log.info("Token scopes: %s", ", ".join(data.get("scope") or []) or "not reported")

    async def ensure_fresh(self, client: httpx.AsyncClient) -> None:
        """
        Refresh only if no access token is held yet.
        Call this at startup; for reactive refreshes after a 401, call refresh() directly.
        """
        if self._access_token is None:
            await self.refresh(client)


# ---------------------------------------------------------------------------
# GPIO / cannon helpers
# ---------------------------------------------------------------------------
async def fire_cannon(duration: float):
    """Pulse the GPIO pin HIGH for `duration` seconds."""
    if HAS_GPIO:
        log.info("🔫  FIRING – pin %d HIGH for %.2fs", GPIO_PIN, duration)
        GPIO.output(GPIO_PIN, GPIO.HIGH)
        await asyncio.sleep(duration)
        GPIO.output(GPIO_PIN, GPIO.LOW)
        log.info("✅  Pin %d LOW – cannon cycle complete", GPIO_PIN)
    else:
        log.info("[DRY-RUN] Would fire GPIO pin %d HIGH for %.2fs", GPIO_PIN, duration)
        await asyncio.sleep(duration)
        log.info("[DRY-RUN] Cycle complete")


def record_score(event: dict, metadata: dict, user: str, shots: int) -> list:
    """Score a redemption and return the overlay messages to publish.

    Never raises: the shot is already queued.
    """
    if scoreboard is None:
        return []
    try:
        user_id = event.get("user_id")
        if not user_id:
            log.warning("Redemption has no user_id – not scored")
            return []
        event_id = event.get("id") or metadata.get("message_id")
        if not event_id:
            log.warning("Redemption has no event id – cannot dedupe")
        result = scoreboard.record(event_id, user_id, user, shots)
        if result is None:
            return []
        if result.locked:
            log.info("Board locked – %s's %d shot(s) fired, scores unchanged",
                     result.name, result.shots)
        else:
            log.info("Score: %s now has %d (+%d)%s | king %s, lead %d",
                     result.name, result.score, result.points,
                     " – FIRST BLOOD" if result.first_blood else "",
                     result.king_name, result.lead)
        if result.dethrone:
            d = result.dethrone
            log.info("👑  %s dethroned %s by %d", d.new_name, d.old_name, d.margin)
        if result.closed_stale:
            log_recap(result.closed_stale, "Stale session closed")
        return [web.alert_message(web.shot_alert(result, FIRST_BLOOD_BONUS), current_state())]
    except Exception:
        log.exception("Scoring failed – shot was already queued, continuing")
        return []


def log_recap(recap: scoring.Recap, label: str) -> None:
    """Write a session recap to the log (the overlay will show it in a later phase)."""
    log.info("🏆  %s – session %d: %s wins with %d (win #%d)",
             label, recap.session_id, recap.winner.name, recap.winner.score, recap.winner_wins)
    log.info("    Podium: %s",
             ", ".join(f"#{i} {s.name} {s.score}" for i, s in enumerate(recap.podium, 1)))
    log.info("    %d viewers, %d points, First Blood: %s",
             recap.participants, recap.total_points, recap.first_blood_name or "-")
    if recap.longest_reign:
        log.info("    Longest reign: %s, %.1f min",
                 recap.longest_reign.name, recap.longest_reign.seconds / 60)


def handle_stream_status(sub_type: str, metadata: dict) -> tuple:
    """Start or close the soak session on stream.online / stream.offline.

    Returns (overlay messages to publish, the recap if this call crowned a winner).
    Never raises.
    """
    if scoreboard is None:
        return [], None
    try:
        event_id = metadata.get("message_id")
        if sub_type == "stream.online":
            started = scoreboard.start_session(event_id, SESSION_ONLINE_GRACE_MIN)
            log.info("Stream online – %s session %d",
                     "started" if started.created else "kept open", started.session_id)
            if started.closed:
                log_recap(started.closed, "Stale session closed")
            return [web.snapshot_message(current_state())], None
        final_board = current_state()          # the board as it stood when the stream ended
        recap = scoreboard.end_session(event_id)
        if recap:
            log_recap(recap, "Stream offline")
            return [web.alert_message(web.winner_alert(recap), final_board, final=True)], recap
        log.info("Stream offline – winner already revealed" if final_board.get("locked")
                 else "Stream offline – nothing to recap")
        # Only tell the overlay to clear if an (empty) session was actually closed; a
        # redelivered offline event must not wipe the champion board that is still showing.
        return ([web.snapshot_message(current_state())] if final_board["active"] else []), None
    except Exception:
        log.exception("Handling %s failed – continuing", sub_type)
        return [], None


def handle_reveal(reason: str) -> tuple:
    """Reveal tonight's winner and lock the results. Returns (overlay messages to
    publish, the recap). Never raises. Does nothing if there is no open session,
    nobody has scored yet, or the winner was already revealed."""
    if scoreboard is None:
        return [], None
    try:
        recap = scoreboard.lock_session()
        if recap is None:
            log.info("Reveal (%s): nothing to reveal (no session, no scores, or already revealed)", reason)
            return [], None
        log_recap(recap, f"Winner revealed ({reason})")
        return [web.alert_message(web.winner_alert(recap), current_state(), final=True)], recap
    except Exception:
        log.exception("Reveal failed - continuing")
        return [], None


vip_client = None        # set in run() once the Twitch token is ready
_vip_tasks: set = set()  # keeps background badge tasks alive until they finish


def schedule_vip(winner: scoring.Standing, reason: str) -> None:
    """Hand the VIP badge to the winner in the background. Never raises, and only
    ever called after the results are locked."""
    if not VIP_ENABLED:
        return
    if vip_client is None or scoreboard is None:
        log.warning("VIP: not ready – the badge for %s was not assigned (%s)", winner.name, reason)
        return
    try:
        log.info("VIP: assigning the badge to %s (%s)%s", winner.name, reason,
                 " – DRY RUN" if VIP_DRY_RUN else "")
        task = asyncio.create_task(vip.assign_with_retries(
            vip_client, scoreboard, TWITCH_BROADCASTER_ID, winner.user_id, winner.name, VIP_DRY_RUN))
        _vip_tasks.add(task)
        task.add_done_callback(_vip_tasks.discard)
    except Exception:
        log.exception("VIP: could not start the badge task - continuing")


async def reveal_scheduler():
    """At STREAM_END_TIME minus REVEAL_MINUTES_BEFORE, reveal tonight's winner and
    lock the results. Creating the file REVEAL_TRIGGER_PATH does the same on demand.
    Never raises: the cannon must keep working whatever happens here."""
    try:
        end = reveal.parse_hhmm(STREAM_END_TIME)
        tz = ZoneInfo(STREAM_TZ)
    except Exception:
        log.exception("Reveal scheduler disabled - check STREAM_END_TIME and STREAM_TZ")
        return
    log.info("Winner reveal: %s %s minus %g min (window %g min); on demand: create %s",
             STREAM_END_TIME, STREAM_TZ, REVEAL_MINUTES_BEFORE, REVEAL_WINDOW_MIN,
             REVEAL_TRIGGER_PATH)
    last_start = None
    while True:
        await asyncio.sleep(REVEAL_POLL_SEC)
        try:
            reason = None
            if os.path.exists(REVEAL_TRIGGER_PATH):
                os.remove(REVEAL_TRIGGER_PATH)
                reason = "manual"
            else:
                start = reveal.window_start(datetime.now(tz), end,
                                            REVEAL_MINUTES_BEFORE, REVEAL_WINDOW_MIN)
                if start is not None and start != last_start:
                    last_start = start
                    reason = "scheduled"
            if reason:
                messages, recap = await asyncio.to_thread(handle_reveal, reason)
                for msg in messages:
                    hub.publish(msg)
                if recap:       # the lock is committed and the alert is out: now the badge
                    schedule_vip(recap.winner, f"reveal, {reason}")
        except Exception:
            log.exception("Reveal scheduler error - continuing")


# ---------------------------------------------------------------------------
# Cannon worker – drains the queue one shot at a time
# ---------------------------------------------------------------------------
async def cannon_worker(queue: asyncio.Queue):
    """
    Runs forever in the background, processing queued redemptions in order.
    Each item in the queue is a (username, pulse_duration_sec) tuple; the
    username is used only for logging.
    """
    while True:
        user, duration = await queue.get()
        log.info("🔫  Processing queued shot for %s (queue depth after this: %d)",
                 user, queue.qsize())
        await fire_cannon(duration)
        queue.task_done()


# ---------------------------------------------------------------------------
# Twitch EventSub helpers
# ---------------------------------------------------------------------------
async def _post_subscription(
    client: httpx.AsyncClient, tokens: TokenManager, payload: dict
) -> httpx.Response:
    """POST an EventSub subscription, refreshing the token once on a 401."""
    response = await client.post(
        TWITCH_EVENTSUB_URL, json=payload, headers=tokens.headers()
    )
    if response.status_code == 401:
        log.warning("Got 401 on subscription – refreshing token and retrying …")
        await tokens.refresh(client)
        response = await client.post(
            TWITCH_EVENTSUB_URL, json=payload, headers=tokens.headers()
        )
    return response


async def subscribe_to_redemptions(
    session_id: str,
    client: httpx.AsyncClient,
    tokens: TokenManager,
) -> None:
    """Register the EventSub subscription for custom Bits Power-up redemptions."""
    payload = {
        "type": "channel.custom_power_up_redemption.add",
        "version": "1",
        "condition": {
            "broadcaster_user_id": TWITCH_BROADCASTER_ID,
        },
        "transport": {
            "method": "websocket",
            "session_id": session_id,
        },
    }

    response = await _post_subscription(client, tokens, payload)

    if response.status_code in (200, 202):
        log.info("Subscribed to custom Power-up redemptions for broadcaster %s", TWITCH_BROADCASTER_ID)
    else:
        log.error("Failed to subscribe: %s %s", response.status_code, response.text)
        raise RuntimeError("EventSub subscription failed")


async def subscribe_to_stream_status(
    session_id: str,
    client: httpx.AsyncClient,
    tokens: TokenManager,
) -> None:
    """Subscribe to stream.online / stream.offline. Failures are logged, never raised:
    the cannon must keep working even if session tracking can't be set up."""
    for sub_type in STREAM_STATUS_TYPES:
        payload = {
            "type": sub_type,
            "version": "1",
            "condition": {"broadcaster_user_id": TWITCH_BROADCASTER_ID},
            "transport": {"method": "websocket", "session_id": session_id},
        }
        try:
            response = await _post_subscription(client, tokens, payload)
            if response.status_code in (200, 202):
                log.info("Subscribed to %s", sub_type)
            else:
                log.error("Failed to subscribe to %s: %s %s",
                          sub_type, response.status_code, response.text)
        except Exception:
            log.exception("Failed to subscribe to %s", sub_type)


# ---------------------------------------------------------------------------
# Main WebSocket loop
# ---------------------------------------------------------------------------
async def run():
    global vip_client
    tokens = TokenManager(TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TWITCH_REFRESH_TOKEN)

    # Unbounded queue – every redemption is honoured in arrival order.
    cannon_queue: asyncio.Queue = asyncio.Queue()

    # Start the single worker that serialises GPIO pulses.
    asyncio.create_task(cannon_worker(cannon_queue))
    if REVEAL_ENABLED:
        reveal_task = asyncio.create_task(reveal_scheduler())

    # Overlay server. If it can't start (port in use, missing file) the cannon is unaffected.
    if WEB_ENABLED:
        try:
            await web.start(WEB_HOST, WEB_PORT, hub, current_state, OVERLAY_PATH, SOUNDS_DIR)
        except Exception:
            log.exception("Overlay server could not start – the cannon is unaffected")

    async with httpx.AsyncClient() as http_client:
        # Always refresh on startup so we never run with a potentially stale token.
        await tokens.refresh(http_client)

        if VIP_ENABLED:
            vip_client = vip.VipClient(http_client, tokens, TWITCH_BROADCASTER_ID)
            try:       # read-only: proves the token has the VIP scope without changing anything
                count = await vip_client.preflight()
                log.info("VIP: token can manage VIPs (%d VIP(s) in the channel)%s", count,
                         " – DRY RUN, no changes will be made" if VIP_DRY_RUN else "")
            except Exception as exc:
                log.error("VIP: preflight failed – %s (the badge will not be assigned until fixed)", exc)

        while True:  # reconnect loop
            try:
                log.info("Connecting to Twitch EventSub WebSocket …")
                async with websockets.connect(EVENTSUB_WS_URL) as ws:
                    async for raw in ws:
                        msg = json.loads(raw)
                        metadata = msg.get("metadata", {})
                        msg_type  = metadata.get("message_type")
                        payload   = msg.get("payload", {})

                        # ── Welcome: grab session ID and subscribe ──────────
                        if msg_type == "session_welcome":
                            session_id = payload["session"]["id"]
                            log.info("Session ID: %s", session_id)
                            await subscribe_to_redemptions(session_id, http_client, tokens)
                            await subscribe_to_stream_status(session_id, http_client, tokens)

                        # ── Keepalive: nothing to do ────────────────────────
                        elif msg_type == "session_keepalive":
                            log.debug("Keepalive received")

                        # ── Stream went online / offline: start / close the session ──
                        elif msg_type == "notification" and (
                            metadata.get("subscription_type")
                            or payload.get("subscription", {}).get("type")
                        ) in STREAM_STATUS_TYPES:
                            sub_type = (metadata.get("subscription_type")
                                        or payload.get("subscription", {}).get("type"))
                            log.info("Received %s", sub_type)
                            messages, crowned = await asyncio.to_thread(
                                handle_stream_status, sub_type, metadata)
                            for msg in messages:
                                hub.publish(msg)
                            if crowned and VIP_ON_OFFLINE_FALLBACK:
                                schedule_vip(crowned.winner, "stream offline, no reveal ran")

                        # ── Notification: check for our reward ──────────────
                        elif msg_type == "notification":
                            event = payload.get("event", {})
                            # Log full event at DEBUG level so you can inspect the
                            # exact field names if reward matching isn't working.
                            log.debug("Raw event payload: %s", json.dumps(event, indent=2))

                            reward_title = event.get("custom_power_up", {}).get("title", "")
                            user = event.get("user_name", "unknown")

                            log.info("Power-up redemption received: '%s' by %s", reward_title, user)
                            # Field names only (no values) so we can confirm what Twitch sends.
                            log.info("Notification fields: metadata=%s event=%s power_up=%s",
                                     sorted(metadata), sorted(event),
                                     sorted(event.get("custom_power_up", {})))

                            duration = REWARD_PULSES.get(reward_title)
                            if duration is not None:
                                cannon_queue.put_nowait((user, duration))
                                log.info("Queued shot for %s (queue depth: %d)",
                                         user, cannon_queue.qsize())
                                # Fire first, score second. Scoring runs in a worker thread so a
                                # slow disk write can never delay the cannon.
                                messages = await asyncio.to_thread(
                                    record_score, event, metadata, user, REWARD_SHOTS[reward_title])
                                for msg in messages:
                                    hub.publish(msg)

                        # ── Reconnect requested by Twitch ───────────────────
                        elif msg_type == "session_reconnect":
                            new_url = payload["session"]["reconnect_url"]
                            log.warning("Twitch requested reconnect → %s", new_url)
                            break  # exits inner loop, outer loop reconnects

                        else:
                            log.debug("Unhandled message type: %s", msg_type)

            except (websockets.ConnectionClosed, ConnectionError) as exc:
                log.warning("WebSocket disconnected: %s – reconnecting in 5s …", exc)
                await asyncio.sleep(5)
            except Exception as exc:
                log.exception("Unexpected error: %s – reconnecting in 10s …", exc)
                await asyncio.sleep(10)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _cleanup(signum, frame):
    log.info("Signal %s received – cleaning up GPIO and exiting", signum)
    if HAS_GPIO:
        GPIO.output(GPIO_PIN, GPIO.LOW)
        GPIO.cleanup()
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT,  _cleanup)

    log.info("Water Cannon controller starting up")
    log.info("  Reward name : %s", REWARD_NAME)
    log.info("  5-shot name : %s", REWARD_NAME_5SHOT)
    log.info("  GPIO pin    : BCM %d", GPIO_PIN)
    log.info("  Pulse dur.  : %.2fs", PULSE_DURATION_SEC)
    log.info("  5-shot dur. : %.2fs", PULSE_DURATION_5SHOT_SEC)
    log.info("  VIP badge   : %s", "off" if not VIP_ENABLED else
             ("DRY RUN" if VIP_DRY_RUN else "on") + (", incl. offline fallback" if VIP_ON_OFFLINE_FALLBACK else ""))
    log.info("  Overlay     : %s", f"http://<this host>:{WEB_PORT}/overlay" if WEB_ENABLED else "disabled")

    asyncio.run(run())