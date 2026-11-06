"""stovemaster/voice.py: sentences (as Siri dictates them) -> stove commands."""

import unittest

import helpers  # noqa: F401  (puts the project on the path)

from stovemaster import voice


def parse(text, setpoint=21.0):
    return voice.parse(text, setpoint)


class Sentences(unittest.TestCase):
    def test_what_the_user_says(self):
        cases = {
            "Turn on stove and set temperature to 23 degrees.": [("setpoint", 23.0), ("on", None)],
            "turn on the stove and set the temperature to 23 degrees": [("setpoint", 23.0), ("on", None)],
            "Set it to 22.5°, fan auto and power 4": [("setpoint", 22.5), ("power", 4), ("fan", 7)],
            "turn on, set temperature to 23,5 and then fan high": [("setpoint", 23.5), ("fan", 6), ("on", None)],
            "turn on stove, fan three, temperature twenty-two": [("setpoint", 22.0), ("fan", 3), ("on", None)],
            "Please turn it on at 21": [("setpoint", 21.0), ("on", None)],
            "start at level 2": [("power", 2), ("on", None)],
            "turn off": [("off", None)],
            "Stop the stove.": [("off", None)],
            "shut it down": [("off", None)],
            "put the fire out": [("off", None)],
            "switch off the fan and stop": [("fan", 0), ("off", None)],
            "set the temperature to twenty three point five": [("setpoint", 23.5)],
            "make it twenty two and a half": [("setpoint", 22.5)],
            "fan speed two": [("fan", 2)],
            "turn the fan on": [("fan", 7)],
            "power level three": [("power", 3)],
            "put the fire on maximum": [("power", 5)],
        }
        for text, commands in cases.items():
            self.assertEqual(parse(text), (commands, False), text)

    def test_warmer_and_colder_start_from_the_current_target(self):
        self.assertEqual(parse("two degrees warmer")[0], [("setpoint", 23.0)])
        self.assertEqual(parse("a bit colder please")[0], [("setpoint", 20.5)])
        self.assertEqual(parse("turn the heat down")[0], [("setpoint", 20.0)])
        self.assertEqual(parse("set it to 22 and then one degree warmer")[0], [("setpoint", 23.0)])
        read = []
        self.assertEqual(voice.parse("turn it up", lambda: read.append(1) or 19.5)[0], [("setpoint", 20.5)])
        self.assertEqual(voice.parse("turn on", lambda: read.append(1))[0], [("on", None)])
        self.assertEqual(read, [1])                         # the stove is asked only when needed
        with self.assertRaises(voice.NotUnderstood):
            voice.parse("warmer", None)                     # unknown target: do not guess

    def test_questions(self):
        for text in ("how is the stove", "What's the temperature?", "Is the stove on?", "status"):
            self.assertEqual(parse(text), ([], True), text)
        self.assertEqual(parse("turn on and tell me the temperature"), ([("on", None)], True))

    def test_anything_unclear_does_nothing(self):
        for text in ("turn on and banana", "", "   ", "please", "set to 80", "fan 2.5"):
            with self.assertRaises(voice.NotUnderstood, msg=text):
                parse(text)
        self.assertIn("banana", voice.sorry("x", voice.NotUnderstood("banana")))
        self.assertIn("Nothing was changed", voice.sorry("x", voice.NotUnderstood("banana")))

    def test_the_last_word_counts(self):
        self.assertEqual(parse("turn on and off")[0], [("off", None)])
        self.assertEqual(parse("fan 2, no fan 4")[0], [("fan", 4)])


if __name__ == "__main__":
    unittest.main()
