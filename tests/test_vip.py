import asyncio
import os
import sqlite3
import tempfile
import unittest

import scoring
import vip

BROADCASTER = "999"


class Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeHttp:
    """Scripted Twitch. `script` maps (METHOD, user_id) to a response or a list of them."""

    def __init__(self, script=None, default=None):
        self.script = script or {}
        self.default = default
        self.calls = []

    async def request(self, method, url, params=None, headers=None):
        key = (method, (params or {}).get("user_id"))
        self.calls.append(key)
        if key not in self.script:
            if self.default is not None:
                return self.default
            raise AssertionError(f"unexpected call {key}")
        r = self.script[key]
        if isinstance(r, list):
            r = r.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeTokens:
    def __init__(self, fail_refresh=False):
        self.refreshed = 0
        self.fail_refresh = fail_refresh

    def headers(self):
        return {"Authorization": "Bearer x"}

    async def refresh(self, http):
        self.refreshed += 1
        if self.fail_refresh:
            raise RuntimeError("refresh down")


class MemoryStore:
    def __init__(self, holders=()):
        self.holders = list(holders)
        self.writes = []

    def vip_holders(self):
        return [(u, f"name-{u}") for u in self.holders]

    def add_vip_holder(self, uid):
        self.writes.append(("add", uid))
        if uid not in self.holders:
            self.holders.append(uid)

    def remove_vip_holder(self, uid):
        self.writes.append(("remove", uid))
        if uid in self.holders:
            self.holders.remove(uid)


def run(coro):
    return asyncio.run(coro)


class ClientTests(unittest.TestCase):
    def client(self, http, tokens=None):
        return vip.VipClient(http, tokens or FakeTokens(), BROADCASTER)

    def test_add_success(self):
        http = FakeHttp({("POST", "1"): Resp(204)})
        self.assertEqual(run(self.client(http).add("1")), "added")

    def test_add_422_for_an_existing_vip_is_already_vip(self):
        http = FakeHttp({("POST", "1"): Resp(422, {"message": "x"}),
                         ("GET", "1"): Resp(200, {"data": [{"user_id": "1"}]})})
        self.assertEqual(run(self.client(http).add("1")), "already_vip")

    def test_add_422_for_a_non_vip_is_not_allowed(self):
        http = FakeHttp({("POST", "1"): Resp(422, {"message": "user is a moderator"}),
                         ("GET", "1"): Resp(200, {"data": []})})
        self.assertEqual(run(self.client(http).add("1")), "not_allowed")

    def test_permanent_failures(self):
        for status, hint in [(409, "slots"), (425, "Build a Community"), (403, "not allowed"), (400, "")]:
            with self.subTest(status=status):
                http = FakeHttp({("POST", "1"): Resp(status, {"message": "nope"})})
                with self.assertRaises(vip.VipError) as cm:
                    run(self.client(http).add("1"))
                self.assertFalse(cm.exception.retryable)
                self.assertIn(hint, str(cm.exception))

    def test_retryable_failures(self):
        for resp in (Resp(429, {"message": "slow down"}), Resp(500), Resp(503)):
            with self.subTest(status=resp.status_code):
                http = FakeHttp({("POST", "1"): resp})
                with self.assertRaises(vip.VipError) as cm:
                    run(self.client(http).add("1"))
                self.assertTrue(cm.exception.retryable)

    def test_network_error_is_retryable(self):
        http = FakeHttp({("POST", "1"): ConnectionError("down")})
        with self.assertRaises(vip.VipError) as cm:
            run(self.client(http).add("1"))
        self.assertTrue(cm.exception.retryable)

    def test_401_refreshes_the_token_once_then_succeeds(self):
        tokens = FakeTokens()
        http = FakeHttp({("POST", "1"): [Resp(401, {"message": "expired"}), Resp(204)]})
        self.assertEqual(run(self.client(http, tokens).add("1")), "added")
        self.assertEqual(tokens.refreshed, 1)

    def test_persistent_401_names_the_missing_scope(self):
        http = FakeHttp({("POST", "1"): [Resp(401, {"message": "Missing scope"}), Resp(401, {"message": "Missing scope"})]})
        with self.assertRaises(vip.VipError) as cm:
            run(self.client(http).add("1"))
        self.assertFalse(cm.exception.retryable)
        self.assertIn("channel:manage:vips", str(cm.exception))

    def test_failed_token_refresh_is_retryable(self):
        http = FakeHttp({("POST", "1"): Resp(401)})
        with self.assertRaises(vip.VipError) as cm:
            run(self.client(http, FakeTokens(fail_refresh=True)).add("1"))
        self.assertTrue(cm.exception.retryable)

    def test_remove(self):
        http = FakeHttp({("DELETE", "1"): Resp(204), ("DELETE", "2"): Resp(422)})
        c = self.client(http)
        self.assertEqual(run(c.remove("1")), "removed")
        self.assertEqual(run(c.remove("2")), "not_vip")

    def test_remove_forbidden_is_permanent(self):
        http = FakeHttp({("DELETE", "1"): Resp(403)})
        with self.assertRaises(vip.VipError) as cm:
            run(self.client(http).remove("1"))
        self.assertFalse(cm.exception.retryable)

    def test_preflight_counts_vips_and_reports_a_bad_token(self):
        http = FakeHttp(default=Resp(200, {"data": [{}, {}, {}]}))
        http.script = {("GET", None): Resp(200, {"data": [{}, {}, {}]})}
        self.assertEqual(run(self.client(http).preflight()), 3)
        http = FakeHttp({("GET", None): [Resp(401), Resp(401)]})
        with self.assertRaises(vip.VipError):
            run(self.client(http).preflight())


class ReassignTests(unittest.TestCase):
    def go(self, http, store, winner="2", dry_run=False, tokens=None):
        client = vip.VipClient(http, tokens or FakeTokens(), BROADCASTER)
        return run(vip.reassign(client, store, BROADCASTER, winner, f"name-{winner}", dry_run))

    def test_first_winner_is_added_and_tracked(self):
        http, store = FakeHttp({("POST", "2"): Resp(204)}), MemoryStore()
        self.go(http, store)
        self.assertEqual(http.calls, [("POST", "2")])
        self.assertEqual(store.holders, ["2"])

    def test_previous_winner_is_removed_before_the_new_one_is_added(self):
        http = FakeHttp({("DELETE", "1"): Resp(204), ("POST", "2"): Resp(204)})
        store = MemoryStore(["1"])
        self.go(http, store)
        self.assertEqual(http.calls, [("DELETE", "1"), ("POST", "2")])
        self.assertEqual(store.holders, ["2"])

    def test_same_winner_twice_is_not_removed_and_stays_tracked(self):
        http = FakeHttp({("POST", "2"): Resp(422), ("GET", "2"): Resp(200, {"data": [{}]})})
        store = MemoryStore(["2"])
        self.go(http, store)
        self.assertNotIn(("DELETE", "2"), http.calls)
        self.assertEqual(store.holders, ["2"])

    def test_a_vip_the_bot_did_not_grant_is_left_alone_and_not_tracked(self):
        http = FakeHttp({("POST", "2"): Resp(422), ("GET", "2"): Resp(200, {"data": [{}]})})
        store = MemoryStore()
        self.go(http, store)
        self.assertEqual(store.holders, [])        # so a later reassignment can never remove them

    def test_only_tracked_holders_are_ever_removed(self):
        # Other VIPs exist on the channel, but the bot never lists or removes them.
        http = FakeHttp({("DELETE", "1"): Resp(204), ("POST", "2"): Resp(204)})
        self.go(http, MemoryStore(["1"]))
        self.assertEqual([c for c in http.calls if c[0] == "DELETE"], [("DELETE", "1")])
        self.assertNotIn("GET", [c[0] for c in http.calls])

    def test_broadcaster_is_skipped_without_any_call(self):
        http, store = FakeHttp(), MemoryStore(["1"])
        self.go(http, store, winner=BROADCASTER)
        self.assertEqual(http.calls, [])
        self.assertEqual(store.holders, ["1"])

    def test_moderator_winner_is_skipped_and_not_tracked(self):
        http = FakeHttp({("POST", "2"): Resp(422), ("GET", "2"): Resp(200, {"data": []})})
        store = MemoryStore()
        self.go(http, store)
        self.assertEqual(store.holders, [])

    def test_permanent_removal_failure_still_adds_the_winner_and_keeps_tracking_the_old_holder(self):
        http = FakeHttp({("DELETE", "1"): Resp(403), ("POST", "2"): Resp(204)})
        store = MemoryStore(["1"])
        self.go(http, store)
        self.assertEqual(sorted(store.holders), ["1", "2"])

    def test_retryable_removal_failure_raises_before_adding_anyone(self):
        http = FakeHttp({("DELETE", "1"): Resp(429)})
        with self.assertRaises(vip.VipError):
            self.go(http, MemoryStore(["1"]))
        self.assertEqual(http.calls, [("DELETE", "1")])

    def test_no_free_slot_is_logged_not_raised_and_not_tracked(self):
        http = FakeHttp({("POST", "2"): Resp(409)})
        store = MemoryStore()
        self.go(http, store)
        self.assertEqual(store.holders, [])

    def test_dry_run_makes_no_calls_and_no_writes(self):
        http, store = FakeHttp(), MemoryStore(["1"])
        self.go(http, store, dry_run=True)
        self.assertEqual(http.calls, [])
        self.assertEqual(store.writes, [])


class RetryTests(unittest.TestCase):
    def runner(self, http, store, delays=(0, 5, 10)):
        slept = []

        async def fake_sleep(s):
            slept.append(s)
        client = vip.VipClient(http, FakeTokens(), BROADCASTER)
        ok = run(vip.assign_with_retries(client, store, BROADCASTER, "2", "name-2",
                                         delays=delays, sleep=fake_sleep))
        return ok, slept

    def test_retries_a_transient_failure_then_succeeds(self):
        http = FakeHttp({("POST", "2"): [Resp(429), Resp(500), Resp(204)]})
        ok, slept = self.runner(http, MemoryStore())
        self.assertTrue(ok)
        self.assertEqual(slept, [5, 10])
        self.assertEqual(http.calls.count(("POST", "2")), 3)

    def test_gives_up_after_the_last_attempt(self):
        http = FakeHttp({("POST", "2"): Resp(503)})
        ok, _ = self.runner(http, MemoryStore())
        self.assertFalse(ok)
        self.assertEqual(http.calls.count(("POST", "2")), 3)

    def test_a_permanent_failure_is_not_retried(self):
        http = FakeHttp({("POST", "2"): Resp(425)})
        ok, slept = self.runner(http, MemoryStore())
        self.assertTrue(ok)                 # handled and logged; nothing to retry
        self.assertEqual(slept, [])
        self.assertEqual(http.calls.count(("POST", "2")), 1)

    def test_an_unexpected_error_never_escapes(self):
        class Boom(MemoryStore):
            def vip_holders(self):
                raise RuntimeError("db gone")
        ok, _ = self.runner(FakeHttp(), Boom())
        self.assertFalse(ok)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "soak.db")
        self.sb = scoring.Scoreboard(self.path)

    def tearDown(self):
        self.sb.close()
        self._dir.cleanup()

    def test_holders_add_remove_and_names(self):
        self.sb.record("e1", "1", "Alice", 1)
        self.sb.add_vip_holder("1")
        self.sb.add_vip_holder("1")                       # idempotent
        self.sb.add_vip_holder("77")                      # never seen a redemption: falls back to the id
        self.assertEqual(self.sb.vip_holders(), [("1", "Alice"), ("77", "77")])
        self.sb.remove_vip_holder("1")
        self.assertEqual(self.sb.vip_holders(), [("77", "77")])
        self.sb.remove_vip_holder("nobody")               # harmless

    def test_holders_survive_a_restart(self):
        self.sb.add_vip_holder("1")
        self.sb.close()
        self.sb = scoring.Scoreboard(self.path)
        self.assertEqual([u for u, _ in self.sb.vip_holders()], ["1"])

    def test_an_older_database_gets_the_table(self):
        old = os.path.join(self._dir.name, "old.db")
        db = sqlite3.connect(old)
        db.execute("CREATE TABLE viewers (user_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, "
                   "lifetime_shots INTEGER NOT NULL DEFAULT 0)")
        db.commit()
        db.close()
        sb = scoring.Scoreboard(old)
        try:
            sb.add_vip_holder("1")
            self.assertEqual([u for u, _ in sb.vip_holders()], ["1"])
        finally:
            sb.close()


if __name__ == "__main__":
    unittest.main()
