import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import reveal

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 3, 21, 0, 0, tzinfo=ET)


class ParseTestSecondsTests(unittest.TestCase):
    def test_blank_means_the_default_fifteen(self):
        for text in ("", "  ", "\n"):
            self.assertEqual(reveal.parse_test_seconds(text), 15)

    def test_a_number_of_seconds(self):
        self.assertEqual(reveal.parse_test_seconds("40\n"), 40)
        self.assertEqual(reveal.parse_test_seconds("  7 "), 7)
        self.assertEqual(reveal.parse_test_seconds("12.5"), 12.5)

    def test_junk_zero_negative_and_non_finite_fall_back_to_the_default(self):
        for text in ("abc", "15 seconds", "0", "-5", "nan", "inf", "-inf"):
            self.assertEqual(reveal.parse_test_seconds(text), 15, text)

    def test_a_typo_cannot_start_a_long_countdown(self):
        self.assertEqual(reveal.parse_test_seconds("99999"), 600)
        self.assertEqual(reveal.parse_test_seconds("1e9"), 600)

    def test_none_is_treated_as_blank(self):
        self.assertEqual(reveal.parse_test_seconds(None), 15)


class ManualCountdownTests(unittest.TestCase):
    def test_idle_until_started(self):
        c = reveal.ManualCountdown()
        self.assertIsNone(c.due)
        self.assertFalse(c.is_due(NOW))

    def test_runs_out_exactly_after_the_seconds(self):
        c = reveal.ManualCountdown()
        due = c.start(NOW, 15)
        self.assertEqual(due, NOW + timedelta(seconds=15))
        self.assertEqual(c.due, due)
        self.assertFalse(c.is_due(NOW + timedelta(seconds=14, milliseconds=999)))
        self.assertTrue(c.is_due(NOW + timedelta(seconds=15)))
        self.assertTrue(c.is_due(NOW + timedelta(minutes=5)))

    def test_clear_stops_it(self):
        c = reveal.ManualCountdown()
        c.start(NOW, 15)
        c.clear()
        self.assertIsNone(c.due)
        self.assertFalse(c.is_due(NOW + timedelta(hours=1)))

    def test_starting_again_restarts_it(self):
        c = reveal.ManualCountdown()
        c.start(NOW, 15)
        later = NOW + timedelta(seconds=10)
        c.start(later, 15)
        self.assertEqual(c.due, later + timedelta(seconds=15))
        self.assertFalse(c.is_due(NOW + timedelta(seconds=20)))

    def test_it_never_changes_the_real_schedule(self):
        before = reveal.next_start(NOW, (21, 30), 10)
        c = reveal.ManualCountdown()
        c.start(NOW, 15)
        self.assertEqual(reveal.next_start(NOW, (21, 30), 10), before)


if __name__ == "__main__":
    unittest.main()
