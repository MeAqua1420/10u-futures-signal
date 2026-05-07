from __future__ import annotations

import unittest

from ten_u.indicators import ema, rolling_max_prior, rolling_median_prior, rolling_min_prior


class IndicatorTests(unittest.TestCase):
    def test_ema_seed_and_update(self) -> None:
        out = ema([1, 2, 3, 4], 3)
        self.assertEqual(out[:2], [None, None])
        self.assertAlmostEqual(out[2], 2.0)
        self.assertAlmostEqual(out[3], 3.0)

    def test_prior_rolls_do_not_include_current_bar(self) -> None:
        values = [1, 3, 2, 10]
        self.assertEqual(rolling_max_prior(values, 3), [None, None, None, 3])
        self.assertEqual(rolling_min_prior(values, 3), [None, None, None, 1])
        self.assertEqual(rolling_median_prior(values, 3), [None, None, None, 2])


if __name__ == "__main__":
    unittest.main()
