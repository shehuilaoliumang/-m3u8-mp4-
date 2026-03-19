from __future__ import annotations

import unittest

from main import _should_use_tkinter_dnd


class MainTests(unittest.TestCase):
    def test_should_use_tkinter_dnd_when_enabled_and_available(self) -> None:
        self.assertTrue(_should_use_tkinter_dnd(True, object()))

    def test_should_not_use_tkinter_dnd_when_disabled(self) -> None:
        self.assertFalse(_should_use_tkinter_dnd(False, object()))

    def test_should_not_use_tkinter_dnd_when_module_missing(self) -> None:
        self.assertFalse(_should_use_tkinter_dnd(True, None))


if __name__ == "__main__":
    unittest.main()

