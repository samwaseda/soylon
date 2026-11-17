import unittest
from pathlib import Path
from typing import Optional, Union

from soylon.helper import convert_input


class TestConvertInput(unittest.TestCase):
    def test_boolean_values(self):
        for value in ("1", "true", "t", "yes", "y", "on", "enable", "enabled"):
            with self.subTest(value=value):
                self.assertIs(convert_input(bool, f" {value.upper()} "), True)
        for value in ("0", "false", "f", "no", "n", "off", "disable", "disabled"):
            with self.subTest(value=value):
                self.assertIs(convert_input(bool, f" {value.upper()} "), False)

    def test_invalid_boolean_values(self):
        for value in ("", " ", "2", "-1", "maybe"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                convert_input(bool, value)

    def test_other_types_use_their_constructors(self):
        self.assertEqual(convert_input(int, "42"), 42)
        self.assertEqual(convert_input(float, "1.5"), 1.5)
        self.assertEqual(convert_input(str, "  text  "), "  text  ")
        self.assertEqual(convert_input(Path, "example.txt"), Path("example.txt"))
        with self.assertRaises(ValueError):
            convert_input(int, "not a number")

    def test_union_tries_members_in_order(self):
        self.assertEqual(convert_input(int | float, "42"), 42)
        self.assertIs(type(convert_input(int | float, "42")), int)
        self.assertEqual(convert_input(int | float, "1.5"), 1.5)
        self.assertEqual(convert_input(str | int, "42"), "42")
        self.assertIs(convert_input(int | bool, "yes"), True)
        self.assertEqual(convert_input(Union[int, str], "text"), "text")  # noqa: UP007
        self.assertEqual(convert_input(Optional[int], "42"), 42)  # noqa: UP045
        self.assertEqual(convert_input(type(None) | int, "42"), 42)

    def test_union_rejects_unconvertible_value(self):
        with self.assertRaisesRegex(ValueError, "Cannot convert"):
            convert_input(int | bool, "maybe")
