from __future__ import annotations

import unittest

from datetime import datetime
from zoneinfo import ZoneInfo

from ten_u.market_calendar import recent_us_market_non_workdays, is_us_market_non_workday, us_eastern_date


NY = ZoneInfo("America/New_York")


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class MarketCalendarTests(unittest.TestCase):
    def test_us_eastern_weekend_is_non_workday(self) -> None:
        ts = ms(datetime(2026, 5, 9, 12, tzinfo=NY))
        self.assertTrue(is_us_market_non_workday(ts))
        self.assertEqual(us_eastern_date(ts).isoformat(), "2026-05-09")

    def test_nyse_full_holiday_is_non_workday(self) -> None:
        self.assertTrue(is_us_market_non_workday(ms(datetime(2026, 1, 1, 12, tzinfo=NY))))

    def test_nyse_early_close_is_still_workday(self) -> None:
        self.assertFalse(is_us_market_non_workday(ms(datetime(2026, 11, 27, 12, tzinfo=NY))))

    def test_recent_us_market_non_workdays_returns_requested_count(self) -> None:
        days = recent_us_market_non_workdays(3, ms(datetime(2026, 5, 8, 12, tzinfo=NY)))
        self.assertEqual([day.isoformat() for day in days], ["2026-04-26", "2026-05-02", "2026-05-03"])


if __name__ == "__main__":
    unittest.main()
