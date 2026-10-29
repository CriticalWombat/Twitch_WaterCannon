import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import reveal

ET = ZoneInfo("America/New_York")


def at(h, m, day=3, s=0):
    return datetime(2026, 11, day, h, m, s, tzinfo=ET)


class WindowTests(unittest.TestCase):
    def window(self, now, end=(21, 30)):
        return reveal.window_start(now, end, 10, 10)

    def test_before_the_window(self):
        self.assertIsNone(self.window(at(21, 19, s=59)))

    def test_window_opens_ten_minutes_before_end(self):
        self.assertEqual(self.window(at(21, 20)), at(21, 20))

    def test_still_inside_the_window(self):
        self.assertEqual(self.window(at(21, 29, s=59)), at(21, 20))

    def test_after_the_window(self):
        self.assertIsNone(self.window(at(21, 30)))

    def test_same_start_for_every_tick_in_the_window(self):
        starts = {self.window(at(21, m, s=s)) for m in range(20, 30) for s in (0, 30)}
        self.assertEqual(starts, {at(21, 20)})

    def test_stream_ending_after_midnight(self):
        end = (0, 30)
        self.assertIsNone(self.window(at(23, 0), end))
        self.assertEqual(self.window(at(0, 20, day=4), end), at(0, 20, day=4))

    def test_end_just_after_midnight_opens_the_night_before(self):
        self.assertEqual(self.window(at(23, 56), (0, 5)), at(23, 55))

    def test_parse(self):
        self.assertEqual(reveal.parse_hhmm(" 21:30 "), (21, 30))
        for bad in ("2130", "25:00", "21:60", "", "ab:cd"):
            with self.assertRaises(ValueError):
                reveal.parse_hhmm(bad)


if __name__ == "__main__":
    unittest.main()
