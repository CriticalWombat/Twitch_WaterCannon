"""
vip.py
------
Gives the night's winner the VIP badge, and takes it from the previous winner.

Rules
  - Called only after the results lock (the reveal), never before.
  - The bot only removes a badge it granted itself (tracked in the database), so
    VIPs you gave by hand are never touched. If the winner is already a VIP the
    bot did not grant, it leaves them as they are and does not track them.
  - The previous winner is un-VIPed at the moment of reassignment, before the
    new winner is added, which also frees a VIP slot.
  - A moderator, or the broadcaster, cannot be made VIP: skipped with a log line.
  - Failures never reach the cannon. Retryable ones (network, rate limit,
    Twitch 5xx) are retried a few times; the rest are logged loudly.

Pure asyncio + duck-typed HTTP: the client only needs `request(method, url,
params=, headers=)` returning an object with `status_code`, `json()` and `text`
(an httpx.AsyncClient does), and `tokens` needs `headers()` and `refresh(http)`.
"""

import asyncio
import logging
from typing import Awaitable, Callable, Iterable, List, Optional, Protocol, Tuple

log = logging.getLogger("water_cannon.vip")

VIPS_URL = "https://api.twitch.tv/helix/channels/vips"
RETRY_DELAYS_SEC = (0, 30, 120, 600)   # first try now, then three retries


class VipError(Exception):
    """A VIP call failed. `retryable` says whether trying again later could help."""

    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


class Store(Protocol):
    def vip_holders(self) -> List[Tuple[str, str]]: ...
    def add_vip_holder(self, user_id: str) -> None: ...
    def remove_vip_holder(self, user_id: str) -> None: ...


def _message(response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict) and body.get("message"):
            return str(body["message"])
    except Exception:
        pass
    return (getattr(response, "text", "") or "")[:200]


class VipClient:
    def __init__(self, http, tokens, broadcaster_id: str):
        self._http = http
        self._tokens = tokens
        self._broadcaster = broadcaster_id

    async def _call(self, method: str, params: dict):
        """One Twitch call. Refreshes the token once on a 401."""
        for attempt in (1, 2):
            try:
                response = await self._http.request(
                    method, VIPS_URL, params=params, headers=self._tokens.headers())
            except Exception as exc:                      # network trouble: worth retrying
                raise VipError(f"{method} VIP request failed: {exc!r}", retryable=True)
            if response.status_code == 401 and attempt == 1:
                log.warning("VIP call got 401 – refreshing the token and retrying once")
                try:
                    await self._tokens.refresh(self._http)
                except Exception as exc:
                    raise VipError(f"Token refresh failed: {exc!r}", retryable=True)
                continue
            return response

    def _fail(self, what: str, response) -> VipError:
        status = response.status_code
        msg = _message(response)
        if status == 401:
            return VipError(
                f"{what}: 401 {msg} – the token probably lacks the channel:manage:vips scope",
                retryable=False)
        retryable = status == 429 or status >= 500
        hint = {
            403: " (the bot is not allowed to change this VIP)",
            409: " (no free VIP slots?)",
            425: " (the channel has not met Twitch's Build a Community requirement)",
        }.get(status, "")
        return VipError(f"{what}: {status} {msg}{hint}", retryable=retryable)

    async def is_vip(self, user_id: str) -> bool:
        response = await self._call("GET", {"broadcaster_id": self._broadcaster, "user_id": user_id})
        if response.status_code != 200:
            raise self._fail("Checking VIP status", response)
        return bool(response.json().get("data"))

    async def preflight(self) -> int:
        """Read-only check that the token can see the VIP list. Returns the VIP count."""
        response = await self._call("GET", {"broadcaster_id": self._broadcaster, "first": 100})
        if response.status_code != 200:
            raise self._fail("VIP preflight", response)
        return len(response.json().get("data", []))

    async def add(self, user_id: str) -> str:
        """'added', 'already_vip', or 'not_allowed' (e.g. a moderator). Raises VipError."""
        response = await self._call("POST", {"broadcaster_id": self._broadcaster, "user_id": user_id})
        if response.status_code == 204:
            return "added"
        if response.status_code == 422:
            # Twitch uses 422 both for "already a VIP" and "is a moderator"; ask which.
            if await self.is_vip(user_id):
                return "already_vip"
            log.warning("Twitch refused to make %s a VIP: %s", user_id, _message(response))
            return "not_allowed"
        raise self._fail("Adding VIP", response)

    async def remove(self, user_id: str) -> str:
        """'removed' or 'not_vip'. Raises VipError."""
        response = await self._call("DELETE", {"broadcaster_id": self._broadcaster, "user_id": user_id})
        if response.status_code == 204:
            return "removed"
        if response.status_code == 422:
            return "not_vip"
        raise self._fail("Removing VIP", response)


async def _in_thread(fn, *args):
    return await asyncio.to_thread(fn, *args)


async def reassign(client: VipClient, store: Store, broadcaster_id: str, winner_id: str,
                   winner_name: str, dry_run: bool = False) -> None:
    """Move the badge to the winner. Raises VipError only for retryable failures."""
    if winner_id == broadcaster_id:
        log.info("VIP: %s is the broadcaster and cannot be made VIP – skipped", winner_name)
        return

    previous = [(uid, name) for uid, name in await _in_thread(store.vip_holders)
                if uid != winner_id]

    if dry_run:
        for uid, name in previous:
            log.info("VIP (dry run): would remove the badge from %s (%s)", name, uid)
        log.info("VIP (dry run): would make %s (%s) VIP", winner_name, winner_id)
        return

    # 1) Take the badge from the previous winner(s) the bot granted it to.
    for uid, name in previous:
        try:
            outcome = await client.remove(uid)
        except VipError as exc:
            if exc.retryable:
                raise                       # nothing changed yet; the whole step is retried
            log.error("VIP: could not remove the badge from %s (%s): %s", name, uid, exc)
            continue                        # keep tracking them so a later run tries again
        await _in_thread(store.remove_vip_holder, uid)
        log.info("VIP: removed the badge from %s (%s) – %s", name, uid, outcome)

    # 2) Give it to the winner.
    try:
        outcome = await client.add(winner_id)
    except VipError as exc:
        if exc.retryable:
            raise
        log.error("VIP: could not make %s (%s) VIP: %s", winner_name, winner_id, exc)
        return

    if outcome == "added":
        await _in_thread(store.add_vip_holder, winner_id)
        log.info("👑  VIP: %s (%s) is now VIP", winner_name, winner_id)
    elif outcome == "already_vip":
        if any(uid == winner_id for uid, _ in await _in_thread(store.vip_holders)):
            log.info("VIP: %s already holds the badge the bot granted", winner_name)
        else:
            log.info("VIP: %s is already a VIP the bot did not grant – left as is", winner_name)
    else:
        log.warning("VIP: %s could not be made VIP (a moderator?) – skipped", winner_name)


async def assign_with_retries(client: VipClient, store: Store, broadcaster_id: str,
                              winner_id: str, winner_name: str, dry_run: bool = False,
                              delays: Iterable[float] = RETRY_DELAYS_SEC,
                              sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> bool:
    """reassign() with bounded retries. Never raises. True if it completed."""
    delays = list(delays)
    for i, delay in enumerate(delays):
        if delay:
            await sleep(delay)
        try:
            await reassign(client, store, broadcaster_id, winner_id, winner_name, dry_run)
            return True
        except VipError as exc:
            if i + 1 < len(delays):
                log.warning("VIP: %s – retrying in %gs", exc, delays[i + 1])
            else:
                log.error("VIP: giving up after %d attempts: %s", len(delays), exc)
        except Exception:
            log.exception("VIP: unexpected error – not retrying")
            return False
    return False
