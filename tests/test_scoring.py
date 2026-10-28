import os
import tempfile
import unittest

from scoring import Scoreboard


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "soak.db")
        self.sb = Scoreboard(self.path)
        self._n = 0

    def tearDown(self):
        self.sb.close()
        self._dir.cleanup()

    def shot(self, uid, shots=1, name=None):
        self._n += 1
        return self.sb.record(f"evt-{self._n}", uid, name or uid.title(), shots)

    def test_first_redemption_claims_first_blood_and_king(self):
        r = self.shot("a", 1)
        self.assertTrue(r.first_blood)
        self.assertEqual(r.points, 2)           # 1 shot + 1 bonus
        self.assertEqual(r.score, 2)
        self.assertEqual(r.king_id, "a")
        self.assertIsNone(r.dethrone)

    def test_first_blood_only_once_per_session(self):
        self.shot("a")
        r = self.shot("b")
        self.assertFalse(r.first_blood)
        self.assertEqual(r.points, 1)

    def test_five_shot_redeem_scores_five(self):
        self.shot("a")                          # a = 2 (with bonus)
        r = self.shot("b", 5)
        self.assertEqual(r.score, 5)

    def test_tie_does_not_dethrone(self):
        self.shot("a")                          # a = 2, king
        r = self.shot("b", 2)                   # b = 2, ties
        self.assertEqual(r.king_id, "a")
        self.assertIsNone(r.dethrone)

    def test_strictly_higher_dethrones_with_margin(self):
        self.shot("a")                          # a = 2
        r = self.shot("b", 5)                   # b = 5
        self.assertEqual(r.king_id, "b")
        self.assertEqual(r.dethrone.old_id, "a")
        self.assertEqual(r.dethrone.new_id, "b")
        self.assertEqual(r.dethrone.margin, 3)
        self.assertEqual(r.lead, 3)

    def test_king_extending_lead_is_not_a_dethrone(self):
        self.shot("a")
        r = self.shot("a", 5)
        self.assertEqual(r.king_id, "a")
        self.assertIsNone(r.dethrone)

    def test_tie_after_challenger_catches_up_keeps_first_to_reach(self):
        self.shot("a", 3)                       # a = 4 (bonus), reached first
        self.shot("b", 3)                       # b = 3
        r = self.shot("b", 1)                   # b = 4, ties a
        self.assertEqual(r.king_id, "a")
        self.assertIsNone(r.dethrone)
        r = self.shot("b", 1)                   # b = 5, strictly higher
        self.assertEqual(r.king_id, "b")
        self.assertEqual(r.dethrone.margin, 1)

    def test_duplicate_event_ignored(self):
        self.assertIsNotNone(self.sb.record("same", "a", "A", 1))
        self.assertIsNone(self.sb.record("same", "a", "A", 1))
        self.assertEqual(self.sb.standings(), [("a", "A", 2)])

    def test_missing_event_id_is_still_scored(self):
        self.assertIsNotNone(self.sb.record(None, "a", "A", 1))
        self.assertIsNotNone(self.sb.record(None, "a", "A", 1))
        self.assertEqual(self.sb.standings()[0][2], 3)

    def test_viewers_keyed_by_id_and_latest_name_wins(self):
        self.shot("42", 1, name="OldName")
        self.shot("42", 1, name="NewName")
        self.assertEqual(self.sb.standings(), [("42", "NewName", 3)])

    def test_standings_order_and_limit(self):
        self.shot("a", 1)
        self.shot("b", 5)
        self.shot("c", 3)
        self.assertEqual([u for u, _, _ in self.sb.standings()], ["b", "c", "a"])
        self.assertEqual(len(self.sb.standings(limit=2)), 2)

    def test_state_survives_restart(self):
        self.shot("a", 1)
        self.shot("b", 5)
        self.sb.close()
        self.sb = Scoreboard(self.path)
        self.assertEqual(self.sb.standings(), [("b", "B", 5), ("a", "A", 2)])
        r = self.shot("a", 1)                   # same session, no new First Blood
        self.assertFalse(r.first_blood)
        self.assertEqual(r.score, 3)
        self.assertEqual(r.king_id, "b")
        # a redelivered event after restart is still deduped
        self.assertIsNone(self.sb.record("evt-1", "a", "A", 1))

    def test_empty_board(self):
        self.assertEqual(self.sb.standings(), [])


if __name__ == "__main__":
    unittest.main()
