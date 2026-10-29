import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import reveal

ET = ZoneInfo("America/New_York")


def at(h, m, day=3, s=0):
    return datetime(2026, 11, day, h, m, s, tzinfo=ET)


class NextStartTests(unittest.TestCase):
    def next(self, now, end=(21, 30)):
        return reveal.next_start(now, end, 10)

    def test_earlier_the_same_evening_aims_at_tonights_reveal(self):
        self.assertEqual(self.next(at(18, 0)), at(21, 20))

    def test_one_second_before_the_reveal(self):
        self.assertEqual(self.next(at(21, 19, s=59)), at(21, 20))

    def test_at_the_reveal_moment_it_is_still_tonights(self):
        self.assertEqual(self.next(at(21, 20)), at(21, 20))

    def test_first_minute_after_the_reveal_is_still_tonights(self):
        self.assertEqual(self.next(at(21, 20, s=59)), at(21, 20))

    def test_after_the_grace_minute_it_aims_at_tomorrow(self):
        self.assertEqual(self.next(at(21, 21, s=1)), at(21, 20, day=4))

    def test_late_evening_aims_at_tomorrow(self):
        self.assertEqual(self.next(at(23, 59)), at(21, 20, day=4))

    def test_early_morning_aims_at_that_evening(self):
        self.assertEqual(self.next(at(0, 30, day=4)), at(21, 20, day=4))

    def test_stream_ending_after_midnight(self):
        end = (0, 30)                                   # reveal at 00:20
        self.assertEqual(self.next(at(23, 0), end), at(0, 20, day=4))
        self.assertEqual(self.next(at(0, 20, s=30, day=4), end), at(0, 20, day=4))
        self.assertEqual(self.next(at(0, 25, day=4), end), at(0, 20, day=5))

    def test_end_just_after_midnight_reveals_the_night_before(self):
        end = (0, 5)                                    # reveal at 23:55
        self.assertEqual(self.next(at(23, 55, s=30), end), at(23, 55))      # inside the grace minute
        self.assertEqual(self.next(at(23, 56), end), at(23, 55, day=4))      # past it: tomorrow's (was None)
        self.assertEqual(self.next(at(0, 0, s=30, day=4), end), at(23, 55, day=4))

    def test_there_is_always_a_next_reveal_for_any_end_time_and_moment(self):
        for end in [(21, 30), (0, 0), (0, 5), (0, 10), (0, 30), (23, 59), (9, 0)]:
            for h in range(24):
                for m in (0, 1, 5, 15, 29, 30, 45, 56, 59):
                    for s in (0, 30, 59):
                        now = at(h, m, s=s)
                        start = self.next(now, end)
                        self.assertIsNotNone(start, (end, now))
                        delta = (start - now).total_seconds()
                        self.assertGreater(delta, -60, (end, now))              # at most the grace minute ago
                        self.assertLessEqual(delta, 24 * 3600 + 60, (end, now))  # within a day


if __name__ == "__main__":
    unittest.main()
