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

import httpx
import websockets
from dotenv import load_dotenv

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

    response = await client.post(
        TWITCH_EVENTSUB_URL, json=payload, headers=tokens.headers()
    )

    if response.status_code == 401:
        log.warning("Got 401 on subscription – refreshing token and retrying …")
        await tokens.refresh(client)
        response = await client.post(
            TWITCH_EVENTSUB_URL, json=payload, headers=tokens.headers()
        )

    if response.status_code in (200, 202):
        log.info("Subscribed to custom Power-up redemptions for broadcaster %s", TWITCH_BROADCASTER_ID)
    else:
        log.error("Failed to subscribe: %s %s", response.status_code, response.text)
        raise RuntimeError("EventSub subscription failed")


# ---------------------------------------------------------------------------
# Main WebSocket loop
# ---------------------------------------------------------------------------
async def run():
    tokens = TokenManager(TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TWITCH_REFRESH_TOKEN)

    # Unbounded queue – every redemption is honoured in arrival order.
    cannon_queue: asyncio.Queue = asyncio.Queue()

    # Start the single worker that serialises GPIO pulses.
    asyncio.create_task(cannon_worker(cannon_queue))

    async with httpx.AsyncClient() as http_client:
        # Always refresh on startup so we never run with a potentially stale token.
        await tokens.refresh(http_client)

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

                        # ── Keepalive: nothing to do ────────────────────────
                        elif msg_type == "session_keepalive":
                            log.debug("Keepalive received")

                        # ── Notification: check for our reward ──────────────
                        elif msg_type == "notification":
                            event = payload.get("event", {})
                            # Log full event at DEBUG level so you can inspect the
                            # exact field names if reward matching isn't working.
                            log.debug("Raw event payload: %s", json.dumps(event, indent=2))

                            reward_title = event.get("custom_power_up", {}).get("title", "")
                            user = event.get("user_name", "unknown")

                            log.info("Power-up redemption received: '%s' by %s", reward_title, user)

                            duration = REWARD_PULSES.get(reward_title)
                            if duration is not None:
                                cannon_queue.put_nowait((user, duration))
                                log.info("Queued shot for %s (queue depth: %d)",
                                         user, cannon_queue.qsize())

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

    asyncio.run(run())