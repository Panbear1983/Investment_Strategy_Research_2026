import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
from weekly_sector_report import _rolling_window, archive_weekly_report


class WeeklyReportArchiveTests(unittest.TestCase):
    def test_appends_to_year_file_once_per_sector_and_week(self):
        with tempfile.TemporaryDirectory() as temp:
            pushed_at = datetime(2026, 8, 30, 15, 0, tzinfo=ZoneInfo('Asia/Taipei'))
            first = archive_weekly_report(
                '# 半導體週報\n\n內容', 'Semiconductor', '2026-08-23 15:00 至 2026-08-30 15:00（台灣時間）',
                pushed_at=pushed_at, archive_dir=temp,
            )
            second = archive_weekly_report(
                '# 半導體週報\n\n內容', 'Semiconductor', '2026-08-23 15:00 至 2026-08-30 15:00（台灣時間）',
                pushed_at=pushed_at, archive_dir=temp,
            )
            archive = Path(temp) / '2026.md'
            text = archive.read_text(encoding='utf-8')

            self.assertEqual(first['status'], 'archived')
            self.assertEqual(second['status'], 'already_archived')
            self.assertEqual(text.count('<!-- isr-weekly-report sector=Semiconductor period=2026-08-23 15:00 至 2026-08-30 15:00（台灣時間） -->'), 1)
            self.assertIn('推送日期：2026-08-30', text)
            self.assertIn('內容', text)
    def test_rolling_window_is_exactly_seven_days_to_the_push_time(self):
        end = datetime(2026, 8, 30, 15, 0, tzinfo=ZoneInfo('Asia/Taipei'))
        start, resolved_end, label = _rolling_window(end)

        self.assertEqual(start, datetime(2026, 8, 23, 15, 0, tzinfo=ZoneInfo('Asia/Taipei')))
        self.assertEqual(resolved_end, end)
        self.assertEqual(label, '2026-08-23 15:00 至 2026-08-30 15:00（台灣時間）')


if __name__ == '__main__':
    unittest.main()
