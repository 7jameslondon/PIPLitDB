from __future__ import annotations

import unittest
from datetime import date

from scripts.discovery.dates import benchmark_windows, resolve_range, subtract_calendar_months


class DateTests(unittest.TestCase):
    def test_calendar_month_subtraction_clamps_month_end(self) -> None:
        self.assertEqual(subtract_calendar_months(date(2026, 8, 31), 6), date(2026, 2, 28))

    def test_calendar_month_subtraction_handles_leap_day(self) -> None:
        self.assertEqual(subtract_calendar_months(date(2024, 8, 31), 6), date(2024, 2, 29))

    def test_default_range_resolves_today_once(self) -> None:
        start, end, today = resolve_range(None, None, months=6, today="2026-07-31")
        self.assertEqual((start, end, today), (date(2026, 1, 31), date(2026, 7, 31), date(2026, 7, 31)))

    def test_explicit_range_is_inclusive(self) -> None:
        start, end, _ = resolve_range("2025-01-01", "2025-06-30", today="2026-01-01")
        self.assertEqual(start, date(2025, 1, 1))
        self.assertEqual(end, date(2025, 6, 30))

    def test_partial_explicit_range_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "supplied together"):
            resolve_range("2025-01-01", None)

    def test_benchmark_windows_add_separate_current_window(self) -> None:
        windows = benchmark_windows(date(2018, 1, 1), date(2019, 2, 15))
        self.assertEqual([(item.start, item.end, item.kind) for item in windows[:2]], [
            (date(2018, 1, 1), date(2018, 6, 30), "completed"),
            (date(2018, 7, 1), date(2018, 12, 31), "completed"),
        ])
        self.assertEqual(windows[-1].kind, "current")
        self.assertEqual((windows[-1].start, windows[-1].end), (date(2018, 8, 15), date(2019, 2, 15)))


if __name__ == "__main__":
    unittest.main()
