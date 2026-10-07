"""Age of the latest / previous 1D cross-up (information columns; never a signal input)."""
import unittest

from scan_gc_radar import cross_up_ages


class TestCrossAge(unittest.TestCase):
    def test_fresh_cross_is_age_zero_and_previous_is_found(self):
        up = [10.0] * 10
        closes = [9, 11, 9, 9, 9, 11, 11, 9, 9, 11]       # cross-ups at bar 1, 5 and 9 (bar 6 is already above)
        last, age, prev = cross_up_ages(closes, up, 9)
        self.assertEqual((last, age, prev), (9, 0, 4))

    def test_old_cross(self):
        up = [10.0] * 8
        closes = [9, 11, 11, 11, 11, 11, 11, 11]
        self.assertEqual(cross_up_ages(closes, up, 7), (1, 6, None))

    def test_never_crossed(self):
        self.assertEqual(cross_up_ages([9, 9, 9], [10, 10, 10], 2), (None, None, None))


if __name__ == "__main__":
    unittest.main()
