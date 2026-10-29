import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from scoring import Scoreboard


class FakeClock:
    def __init__(self):
        self.t = datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "soak.db")
        self.clock = FakeClock()
        self.sb = Scoreboard(self.path, clock=self.clock)
        self._n = 0

    def tearDown(self):
        self.sb.close()
        self._dir.cleanup()

    def shot(self, uid, shots=1, name=None):
        self._n += 1
        return self.sb.record(f"evt-{self._n}", uid, name or uid.title(), shots)

    # -- start ----------------------------------------------------------------
    def test_online_starts_a_session(self):
        r = self.sb.start_session("on-1")
        self.assertTrue(r.created)
        self.assertIsNone(r.closed)

    def test_online_redelivery_is_ignored(self):
        first = self.sb.start_session("on-1")
        self.shot("a")
        self.clock.advance(minutes=40)
        again = self.sb.start_session("on-1")           # same event ID
        self.assertFalse(again.created)
        self.assertEqual(again.session_id, first.session_id)
        self.assertEqual(self.sb.standings()[0][2], 2)  # board not wiped

    def test_late_online_keeps_session_started_by_first_redemption(self):
        r = self.shot("a")
        self.clock.advance(minutes=3)
        s = self.sb.start_session("on-1")
        self.assertFalse(s.created)
        self.assertEqual(s.session_id, r.session_id)
        self.assertEqual(self.sb.standings(), [("a", "A", 2)])

    def test_online_with_stale_session_closes_it_and_starts_fresh(self):
        old = self.shot("a")
        self.clock.advance(hours=4)                     # offline was missed
        s = self.sb.start_session("on-1")
        self.assertTrue(s.created)
        self.assertNotEqual(s.session_id, old.session_id)
        self.assertEqual(s.closed.winner.user_id, "a")
        self.assertEqual(self.sb.standings(), [])

    def test_online_reuses_empty_open_session(self):
        a = self.sb.start_session("on-1")
        self.clock.advance(hours=1)
        b = self.sb.start_session("on-2")
        self.assertFalse(b.created)
        self.assertEqual(a.session_id, b.session_id)

    def test_online_never_keeps_a_locked_session(self):
        # The night was revealed but the offline event was missed; a new stream starts
        # within the grace window and must get a fresh, unlocked board.
        self.shot("a")
        self.shot("b", 5)
        self.sb.lock_session()
        self.clock.advance(minutes=30)
        s = self.sb.start_session("on-2")
        self.assertTrue(s.created)
        self.assertIsNone(s.closed)                       # no second winner announced
        self.assertEqual(self.sb.standings(), [])
        r = self.shot("c")
        self.assertFalse(r.locked)
        self.assertTrue(r.first_blood)
        self.assertEqual(r.score, 2)

    # -- end ------------------------------------------------------------------
    def test_offline_crowns_king_and_builds_recap(self):
        self.sb.start_session("on-1")
        self.shot("a", 1)                               # a = 2 (First Blood)
        self.shot("b", 5)                               # b = 5, king
        self.shot("c", 3)                               # c = 3
        self.shot("d", 1)                               # d = 1
        recap = self.sb.end_session("off-1")
        self.assertEqual(recap.winner.user_id, "b")
        self.assertEqual(recap.winner.score, 5)
        self.assertEqual(recap.winner_wins, 1)
        self.assertEqual([p.user_id for p in recap.podium], ["b", "c", "a"])
        self.assertEqual(recap.participants, 4)
        self.assertEqual(recap.total_points, 2 + 5 + 3 + 1)
        self.assertEqual(recap.first_blood_name, "A")
        self.assertEqual(self.sb.standings(), [])       # board is closed

    def test_offline_redelivery_and_no_session_return_none(self):
        self.shot("a")
        self.assertIsNotNone(self.sb.end_session("off-1"))
        self.assertIsNone(self.sb.end_session("off-1"))   # redelivery
        self.assertIsNone(self.sb.end_session("off-2"))   # nothing open

    def test_offline_with_empty_session_has_no_recap(self):
        self.sb.start_session("on-1")
        self.assertIsNone(self.sb.end_session("off-1"))
        self.assertEqual(self.sb.standings(), [])

    def test_redemption_after_offline_starts_a_new_session_with_first_blood(self):
        self.shot("a")
        self.sb.end_session("off-1")
        r = self.shot("b")
        self.assertTrue(r.first_blood)
        self.assertEqual(r.score, 2)
        self.assertEqual(self.sb.standings(), [("b", "B", 2)])

    def test_wins_accumulate_across_sessions(self):
        self.shot("a", 5)
        self.assertEqual(self.sb.end_session("off-1").winner_wins, 1)
        self.shot("a", 5)
        self.assertEqual(self.sb.end_session("off-2").winner_wins, 2)
        self.shot("b", 5)
        self.assertEqual(self.sb.end_session("off-3").winner_wins, 1)

    def test_tie_at_close_goes_to_first_to_reach(self):
        self.shot("a", 3)                               # a = 4
        self.shot("b", 4)                               # b = 4, ties
        self.assertEqual(self.sb.end_session("off-1").winner.user_id, "a")

    # -- reigns ---------------------------------------------------------------
    def test_longest_reign(self):
        self.shot("a")                                  # a crowned at t=0
        self.clock.advance(minutes=10)
        self.shot("b", 5)                               # b dethrones a (a reigned 10m)
        self.clock.advance(minutes=30)
        self.shot("a", 5)                               # a retakes (b reigned 30m)
        self.clock.advance(minutes=5)
        recap = self.sb.end_session("off-1")            # a reigned 5m more
        self.assertEqual(recap.longest_reign.user_id, "b")
        self.assertAlmostEqual(recap.longest_reign.seconds, 30 * 60)

    def test_king_extending_lead_does_not_split_reign(self):
        self.shot("a")
        self.clock.advance(minutes=20)
        self.shot("a", 5)
        self.clock.advance(minutes=20)
        recap = self.sb.end_session("off-1")
        self.assertAlmostEqual(recap.longest_reign.seconds, 40 * 60)

    # -- idle / stale ---------------------------------------------------------
    def test_idle_session_is_closed_by_next_redemption(self):
        old = self.shot("a", 5)
        self.clock.advance(hours=4)                     # default idle limit is 3h
        r = self.shot("b")
        self.assertNotEqual(r.session_id, old.session_id)
        self.assertEqual(r.closed_stale.winner.user_id, "a")
        self.assertTrue(r.first_blood)
        self.assertEqual(self.sb.standings(), [("b", "B", 2)])

    def test_activity_within_idle_limit_keeps_session(self):
        old = self.shot("a")
        self.clock.advance(hours=2)
        r = self.shot("b")
        self.assertEqual(r.session_id, old.session_id)
        self.assertIsNone(r.closed_stale)

    # -- persistence / migration ---------------------------------------------
    def test_session_state_survives_restart(self):
        self.shot("a", 5)
        self.sb.close()
        self.sb = Scoreboard(self.path, clock=self.clock)
        self.clock.advance(minutes=10)
        recap = self.sb.end_session("off-1")
        self.assertEqual(recap.winner.user_id, "a")
        self.assertAlmostEqual(recap.longest_reign.seconds, 10 * 60)

    def test_opens_a_database_created_by_phase_1(self):
        old_path = os.path.join(self._dir.name, "old.db")
        db = sqlite3.connect(old_path)
        db.executescript("""
            CREATE TABLE viewers (user_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                lifetime_shots INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
                ended_at TEXT, seq INTEGER NOT NULL DEFAULT 0, first_blood_id TEXT, king_id TEXT);
            CREATE TABLE session_scores (session_id INTEGER NOT NULL, user_id TEXT NOT NULL,
                score INTEGER NOT NULL, reached_seq INTEGER NOT NULL, PRIMARY KEY (session_id, user_id));
            CREATE TABLE seen_events (event_id TEXT PRIMARY KEY, seen_at TEXT NOT NULL);
            INSERT INTO viewers VALUES ('a', 'A', 2);
            INSERT INTO sessions (started_at, seq, first_blood_id, king_id)
                VALUES ('2026-10-28T17:00:00+00:00', 1, 'a', 'a');
            INSERT INTO session_scores VALUES (1, 'a', 2, 1);
        """)
        db.commit()
        db.close()
        sb = Scoreboard(old_path, clock=self.clock)
        try:
            self.assertEqual(sb.standings(), [("a", "A", 2)])
            r = sb.record("evt-x", "b", "B", 5)         # session with NULL last_event_at still works
            self.assertEqual(r.king_id, "b")
            recap = sb.end_session("off-1")
            self.assertEqual(recap.winner.user_id, "b")
            self.assertEqual(recap.winner_wins, 1)
        finally:
            sb.close()


if __name__ == "__main__":
    unittest.main()
