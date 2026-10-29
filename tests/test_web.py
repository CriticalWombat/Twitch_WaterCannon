import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

import scoring
import web

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_result(**kw):
    base = dict(session_id=1, user_id="1", name="Alice", shots=1, points=1, score=1,
                first_blood=False, king_id="1", king_name="Alice", lead=1, dethrone=None)
    base.update(kw)
    return scoring.Result(**base)


class AlertTests(unittest.TestCase):
    def test_plain_shot_singular_and_plural(self):
        a = web.shot_alert(make_result(shots=1), 1)
        self.assertEqual((a["type"], a["h"], a["s"]), ("shot", "Alice", "FIRED 1 SHOT"))
        self.assertEqual(web.shot_alert(make_result(shots=2), 1)["s"], "FIRED 2 SHOTS")

    def test_five_shots_is_a_barrage(self):
        a = web.shot_alert(make_result(shots=5), 1)
        self.assertEqual((a["type"], a["h"], a["s"]), ("barrage", "BARRAGE", "Alice FIRED 5 SHOTS"))

    def test_first_blood_beats_barrage(self):
        a = web.shot_alert(make_result(shots=5, first_blood=True), 1)
        self.assertEqual(a["type"], "first")
        self.assertEqual(a["s"], "Alice draws first\n+1 bonus point")

    def test_first_blood_bonus_plural(self):
        a = web.shot_alert(make_result(first_blood=True), 2)
        self.assertEqual(a["s"], "Alice draws first\n+2 bonus points")

    def test_dethrone_beats_everything(self):
        d = scoring.Dethrone("2", "Bob", "1", "Alice", 3)
        a = web.shot_alert(make_result(shots=5, score=5, first_blood=True, dethrone=d), 1)
        self.assertEqual(a["type"], "dethrone")
        self.assertEqual(a["s"], "Alice takes the crown\nfrom Bob with 5 shots")

    def test_winner_alert(self):
        recap = scoring.Recap(1, "a", "b", scoring.Standing("1", "Alice", 7), 1,
                              [scoring.Standing("1", "Alice", 7)], 1, 7, "Alice", None)
        self.assertEqual(web.winner_alert(recap),
                         {"type": "winner", "h": "CHAMPION", "s": "Alice wins with 7 shots"})

    def test_message_shapes(self):
        self.assertEqual(web.snapshot_message({"x": 1}), {"kind": "snapshot", "state": {"x": 1}})
        self.assertNotIn("final", web.alert_message({"t": 1}, {}))
        self.assertTrue(web.alert_message({"t": 1}, {}, final=True)["final"])


class HubTests(unittest.IsolatedAsyncioTestCase):
    async def test_publish_reaches_every_client(self):
        hub = web.Hub()
        a, b = hub.subscribe(), hub.subscribe()
        hub.publish({"n": 1})
        self.assertEqual(a.get_nowait(), {"n": 1})
        self.assertEqual(b.get_nowait(), {"n": 1})

    async def test_client_limit(self):
        hub = web.Hub()
        with mock.patch.object(web, "MAX_SSE_CLIENTS", 1):
            self.assertIsNotNone(hub.subscribe())
            self.assertIsNone(hub.subscribe())

    async def test_slow_client_is_dropped(self):
        hub = web.Hub()
        with mock.patch.object(web, "CLIENT_QUEUE_SIZE", 2):
            hub.subscribe()
            for i in range(3):
                hub.publish({"n": i})
        self.assertEqual(hub.client_count, 0)


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = web.Hub()
        self.state = {"active": True, "standings": [], "king": None}
        self._dir = tempfile.TemporaryDirectory()
        self.overlay = os.path.join(self._dir.name, "overlay.html")
        with open(self.overlay, "w") as f:
            f.write("<html>overlay</html>")
        self.sounds = os.path.join(self._dir.name, "sounds")
        os.makedirs(os.path.join(self.sounds, "sub"))
        for name, data in {"shot.mp3": b"ID3-shot", "dethrone.mp3": b"ID3-dethrone",
                           "notes.txt": b"not audio"}.items():
            with open(os.path.join(self.sounds, name), "wb") as f:
                f.write(data)
        with open(os.path.join(self.sounds, "sub", "inner.mp3"), "wb") as f:
            f.write(b"inner")
        self.server = await web.start("127.0.0.1", 0, self.hub, lambda: self.state, self.overlay,
                                      self.sounds)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        self._dir.cleanup()

    async def request(self, raw: bytes) -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(raw)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        return data

    async def get(self, path: str) -> bytes:
        return await self.request(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())

    async def open_stream(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(b"GET /events HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        return reader, writer, head

    async def next_chunk(self, reader) -> str:
        return (await asyncio.wait_for(reader.readuntil(b"\n\n"), 5)).decode()

    async def next_message(self, reader) -> dict:
        while True:
            chunk = await self.next_chunk(reader)
            if chunk.startswith("data: "):
                return json.loads(chunk[len("data: "):])

    async def test_overlay_page(self):
        resp = await self.get("/overlay")
        self.assertTrue(resp.startswith(b"HTTP/1.1 200"))
        self.assertIn(b"text/html", resp)
        self.assertTrue(resp.endswith(b"<html>overlay</html>"))

    async def test_overlay_page_ignores_query_string(self):
        self.assertTrue((await self.get("/overlay?test=1")).startswith(b"HTTP/1.1 200"))

    async def test_state_and_healthz(self):
        resp = await self.get("/state")
        self.assertEqual(json.loads(resp.split(b"\r\n\r\n", 1)[1]), self.state)
        self.assertTrue((await self.get("/healthz")).endswith(b"ok\n"))

    async def test_unknown_path_is_404(self):
        self.assertTrue((await self.get("/nope")).startswith(b"HTTP/1.1 404"))

    async def test_only_get_is_allowed(self):
        resp = await self.request(b"POST /events HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
        self.assertTrue(resp.startswith(b"HTTP/1.1 405"))

    async def test_garbage_request_is_400(self):
        self.assertTrue((await self.request(b"hello\r\n\r\n")).startswith(b"HTTP/1.1 400"))

    async def test_events_sends_snapshot_then_live_messages(self):
        reader, writer, head = await self.open_stream()
        self.assertIn(b"text/event-stream", head)
        first = await self.next_message(reader)
        self.assertEqual(first, {"kind": "snapshot", "state": self.state})
        self.hub.publish(web.alert_message({"type": "shot", "h": "A", "s": "B"}, {"active": True}))
        second = await self.next_message(reader)
        self.assertEqual(second["alert"]["type"], "shot")
        writer.close()

    async def test_disconnect_frees_the_client_slot(self):
        reader, writer, _ = await self.open_stream()
        await self.next_message(reader)
        self.assertEqual(self.hub.client_count, 1)
        writer.close()
        for _ in range(50):
            if self.hub.client_count == 0:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(self.hub.client_count, 0)

    async def test_too_many_clients_gets_503(self):
        with mock.patch.object(web, "MAX_SSE_CLIENTS", 1):
            reader, writer, _ = await self.open_stream()
            await self.next_message(reader)
            resp = await self.get("/events")
            self.assertTrue(resp.startswith(b"HTTP/1.1 503"))
            writer.close()

    async def test_idle_stream_gets_pings(self):
        with mock.patch.object(web, "PING_EVERY_SEC", 0.1):
            reader, writer, _ = await self.open_stream()
            await self.next_message(reader)
            self.assertEqual(await self.next_chunk(reader), ": ping\n\n")
            writer.close()

    async def test_real_overlay_hides_test_panel_by_default(self):
        with open(os.path.join(REPO, "overlay.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertIn("#test{", html)
        self.assertIn("display:none;flex-wrap:wrap", html)         # hidden unless ?test=1
        self.assertIn("body.testmode #test{display:flex}", html)
        self.assertIn('new EventSource("/events")', html)

    async def test_real_overlay_shows_line_breaks_and_final_board(self):
        with open(os.path.join(REPO, "overlay.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertIn("#alert .s{white-space:pre-line;font-size:42px;", html)   # the Pi sends "\n" in alert text
        self.assertIn("FINAL STANDINGS", html)                     # a locked board is labelled FINAL
        self.assertNotIn(" | lead ", html)                         # old wording gone from the preview too

    async def test_real_overlay_has_the_new_visual_design(self):
        with open(os.path.join(REPO, "overlay.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertIn("transform:scale(1.6);transform-origin:top right", html)   # bigger leader panel
        self.assertIn("body.busy #list.open{opacity:0;visibility:hidden}", html)  # board hides during alerts
        self.assertIn("MASTER BLASTERS", html)
        self.assertNotIn("SOAK LEADERS", html)
        self.assertNotIn("FINAL RESULTS", html)
        self.assertNotIn("var FOOT", html)                         # header footprint icon removed
        self.assertIn('S.locked?"CHAMPION":"LEAD "', html)         # leader strip says CHAMPION once locked
        self.assertIn("Reveal in 1 min", html)
        self.assertIn("Reveal in 15 sec", html)                    # quick preview of the countdown

    async def test_real_overlay_plays_a_sound_per_alert_type(self):
        with open(os.path.join(REPO, "overlay.html"), encoding="utf-8") as f:
            html = f.read()
        for key, name in {"shot": "shot", "barrage": "barrage", "first": "firstblood",
                          "dethrone": "dethrone", "winner": "winner"}.items():
            self.assertIn(f'{key}:"sounds/{name}.mp3"', html)
        self.assertIn("playSound(a.type)", html)
        self.assertIn("volume:0.6", html)

    async def test_real_overlay_countdown_is_a_bare_timer_in_the_last_ten_minutes(self):
        with open(os.path.join(REPO, "overlay.html"), encoding="utf-8") as f:
            html = f.read()
        for needle in ("reveal_at", "server_now", "clockOffset", "countdownShowMs:600000",
                       "d>0&&d<=CFG.countdownShowMs"):
            self.assertIn(needle, html)
        for gone in ("WINNER IN", "REVEALING", "RESULTS LOCKED", "NO LEADER YET"):
            self.assertNotIn(gone, html)                           # just the time, no label
        self.assertIn("function fmtLeft(ms){var s=Math.max(0,Math.ceil(ms/1000));return Math.floor(s/60)", html)

    async def test_real_overlay_leader_card_is_one_line(self):
        with open(os.path.join(REPO, "overlay.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertIn("#strip{display:none;align-items:center;gap:10px;", html)
        self.assertNotIn("flex-direction:column", html)
        self.assertNotIn('"r1"', html)
        self.assertIn('strip.appendChild(el("span","tm off"))', html)   # the timer sits on the same line
        self.assertIn("if(S.king){", html)                         # the card is only up while someone leads

    async def test_real_overlay_crown_is_yellow_and_fb_tag_is_blue(self):
        with open(os.path.join(REPO, "overlay.html"), encoding="utf-8") as f:
            html = f.read()
        crown = html[html.index("var CROWN="):html.index("\n", html.index("var CROWN="))]
        self.assertIn('stroke="#ffd23f"', crown)
        self.assertNotIn("#ff2b2b", crown)                         # no red crown anywhere
        self.assertEqual(html.count("var CROWN="), 1)              # one crown, used in the card and the board
        self.assertIn(".fb{font-size:10px;color:var(--cyan);border:1px solid var(--cyan);", html)

    async def test_real_overlay_returns_to_the_small_card_after_the_champion_alert(self):
        with open(os.path.join(REPO, "overlay.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertNotIn("showBoard(0)", html.split("// ===== TEST PANEL")[0])   # the final board is never pinned
        self.assertIn("if(!pinned)boardT=setTimeout(", html)       # so it hides on the normal timer
        self.assertIn("showBoard(CFG.winnerBoardMs);", html)       # final standings for a few seconds
        self.assertIn("else if(!S.locked)showBoard(CFG.boardMs);", html)   # late shots only get their alert

    async def test_sound_is_served_with_audio_headers(self):
        resp = await self.get("/sounds/shot.mp3")
        self.assertTrue(resp.startswith(b"HTTP/1.1 200"))
        self.assertIn(b"Content-Type: audio/mpeg", resp)
        self.assertIn(b"max-age=3600", resp)
        self.assertTrue(resp.endswith(b"ID3-shot"))

    async def test_sound_paths_cannot_escape_or_serve_other_files(self):
        for path in ("/sounds/missing.mp3", "/sounds/notes.txt", "/sounds/", "/sounds/sub",
                     "/sounds/sub/inner.mp3", "/sounds/../overlay.html", "/sounds/..%2foverlay.html",
                     "/sounds/%2e%2e%2foverlay.html", "/sounds/shot.mp3%00.txt", "/sounds/.mp3"):
            with self.subTest(path=path):
                self.assertTrue((await self.get(path)).startswith(b"HTTP/1.1 404"), path)

    async def test_sounds_are_404_when_no_folder_is_configured(self):
        server = await web.start("127.0.0.1", 0, self.hub, lambda: self.state, self.overlay)
        try:
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /sounds/shot.mp3 HTTP/1.1\r\nHost: x\r\n\r\n")
            await writer.drain()
            self.assertTrue((await asyncio.wait_for(reader.read(), 5)).startswith(b"HTTP/1.1 404"))
            writer.close()
        finally:
            server.close()
            await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
