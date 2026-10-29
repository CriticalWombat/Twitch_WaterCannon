import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import scoring


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 3, 23, 0, tzinfo=timezone.utc)

    def __call__(self):
        self.now += timedelta(seconds=10)
        return self.now


def make(path=":memory:"):
    return scoring.Scoreboard(path, clock=Clock())


def wins(sb, user_id):
    return sb._db.execute("SELECT wins FROM viewers WHERE user_id = ?", (user_id,)).fetchone()["wins"]


def lifetime(sb, user_id):
    return sb._db.execute(
        "SELECT lifetime_shots FROM viewers WHERE user_id = ?", (user_id,)
    ).fetchone()["lifetime_shots"]


class RevealLockTests(unittest.TestCase):
    def setUp(self):
        self.sb = make()
        self.sb.record("e1", "a", "Alice", 1)   # First Blood: 2 points
        self.sb.record("e2", "b", "Bob", 5)     # dethrones Alice: 5 points

    def test_lock_crowns_winner_and_counts_one_win(self):
        recap = self.sb.lock_session()
        self.assertEqual(recap.winner.name, "Bob")
        self.assertEqual(recap.winner.score, 5)
        self.assertEqual(recap.winner_wins, 1)
        self.assertTrue(self.sb.snapshot()["locked"])
        self.assertTrue(self.sb.snapshot()["active"])

    def test_lock_twice_does_nothing(self):
        self.sb.lock_session()
        self.assertIsNone(self.sb.lock_session())
        self.assertEqual(wins(self.sb, "b"), 1)

    def test_shots_after_lock_fire_but_do_not_change_scores(self):
        self.sb.lock_session()
        before = self.sb.snapshot()["standings"]
        result = self.sb.record("e3", "a", "Alice", 5)
        self.assertTrue(result.locked)
        self.assertEqual(result.points, 0)
        self.assertIsNone(result.dethrone)
        self.assertFalse(result.first_blood)
        self.assertEqual(result.king_name, "Bob")
        self.assertEqual(self.sb.snapshot()["standings"], before)
        self.assertEqual(lifetime(self.sb, "a"), 6)   # still counts toward lifetime shots

    def test_new_viewer_after_lock_is_not_added_to_the_board(self):
        self.sb.lock_session()
        result = self.sb.record("e3", "c", "Cara", 1)
        self.assertEqual(result.score, 0)
        names = [s["name"] for s in self.sb.snapshot()["standings"]]
        self.assertNotIn("Cara", names)

    def test_redelivered_event_after_lock_is_ignored(self):
        self.sb.lock_session()
        self.sb.record("e3", "a", "Alice", 1)
        self.assertIsNone(self.sb.record("e3", "a", "Alice", 1))

    def test_offline_after_lock_closes_without_a_second_win(self):
        self.sb.lock_session()
        self.assertIsNone(self.sb.end_session("off1"))
        self.assertFalse(self.sb.snapshot()["active"])
        self.assertEqual(wins(self.sb, "b"), 1)

    def test_offline_without_reveal_still_crowns_winner(self):
        recap = self.sb.end_session("off1")
        self.assertEqual(recap.winner.name, "Bob")
        self.assertEqual(wins(self.sb, "b"), 1)

    def test_next_stream_starts_unlocked(self):
        self.sb.lock_session()
        self.sb.end_session("off1")
        result = self.sb.record("e9", "c", "Cara", 1)
        self.assertFalse(result.locked)
        self.assertTrue(result.first_blood)
        self.assertFalse(self.sb.snapshot()["locked"])

    def test_stale_locked_session_is_closed_without_a_second_win(self):
        self.sb.lock_session()
        self.sb._clock.now += timedelta(hours=7)
        result = self.sb.record("e10", "c", "Cara", 1)
        self.assertFalse(result.locked)
        self.assertEqual(wins(self.sb, "b"), 1)


class EdgeCaseTests(unittest.TestCase):
    def test_lock_with_no_session_or_no_scores_does_nothing(self):
        sb = make()
        self.assertIsNone(sb.lock_session())
        sb.start_session("on1")
        self.assertIsNone(sb.lock_session())
        self.assertFalse(sb.snapshot()["locked"])
        result = sb.record("e1", "a", "Alice", 1)   # board still works afterwards
        self.assertFalse(result.locked)

    def test_inactive_snapshot_reports_unlocked(self):
        self.assertFalse(make().snapshot()["locked"])

    def test_old_database_gets_the_new_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.db")
            db = sqlite3.connect(path)
            db.execute(
                "CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, "
                "ended_at TEXT, seq INTEGER NOT NULL DEFAULT 0, first_blood_id TEXT, king_id TEXT, "
                "winner_id TEXT, last_event_at TEXT)"
            )
            db.commit()
            db.close()
            sb = make(path)
            cols = {r["name"] for r in sb._db.execute("PRAGMA table_info(sessions)")}
            self.assertIn("locked_at", cols)
            sb.record("e1", "a", "Alice", 1)
            self.assertIsNotNone(sb.lock_session())
            sb.close()


if __name__ == "__main__":
    unittest.main()