from __future__ import annotations

import unittest

from ten_u.cli import _poll_seconds, build_parser


class CLITests(unittest.TestCase):
    def test_okx_signal_loop_flags_parse(self) -> None:
        args = build_parser().parse_args(
            [
                "okx-signal",
                "--top",
                "5",
                "--strategy",
                "manuscript",
                "--loop",
                "--poll-seconds",
                "30",
            ]
        )
        self.assertTrue(args.loop)
        self.assertEqual(args.poll_seconds, 30)

    def test_okx_demo_loop_execute_flags_parse(self) -> None:
        args = build_parser().parse_args(
            [
                "okx-demo",
                "--top",
                "5",
                "--strategy",
                "manuscript",
                "--pos-mode",
                "long-short",
                "--loop",
                "--poll-seconds",
                "45",
                "--execute",
            ]
        )
        self.assertTrue(args.loop)
        self.assertTrue(args.execute)
        self.assertEqual(args.pos_mode, "long-short")
        self.assertEqual(args.poll_seconds, 45)

    def test_poll_seconds_has_safe_minimum(self) -> None:
        self.assertEqual(_poll_seconds(0), 1)


if __name__ == "__main__":
    unittest.main()
