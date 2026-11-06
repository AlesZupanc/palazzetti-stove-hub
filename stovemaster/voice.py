"""Spoken commands -> stove commands, for /api/command (a Siri shortcut sends what was dictated).

  "turn on the stove and set the temperature to 23 degrees"  -> setpoint 23, on
  "set it to 22.5°, fan auto and power 4"                     -> setpoint 22.5, fan 7, power 4
  "two degrees warmer" / "a bit colder"                       -> setpoint +2 / -1
  "turn off" / "stop the stove"                               -> off
  "how is the stove" / "what's the temperature"               -> state (spoken back)

English, the language Siri dictates in; numbers as digits ("23", "23.5", "23,5", "23°") or
words ("twenty three point five"). Several commands joined by "and", "then", commas.
Settings are sent before switching on, switching off comes last. When a part of the sentence
is not understood nothing is done at all (better than half of what was meant).
"""

import re

SMALL = {"zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
         "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
         "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "a": 1, "an": 1}
TENS = {"twenty": 20, "thirty": 30, "forty": 40}
NUMBER = r"(\d+(?:\.\d+)?)"
SWITCH_ON = re.compile(r"\b(?:turn|switch|power|fire|put) (?:\w+ ){0,2}?on\b|\b(?:start|light|ignite)\b")
SWITCH_OFF = re.compile(r"\b(?:turn|switch) (?:\w+ ){0,2}?off\b|\b(?:shut|power) (?:\w+ ){0,2}?(?:off|down)\b|"
                        r"\bput (?:\w+ ){0,2}?out\b|\b(?:stop|extinguish|off)\b|\bgo out\b")


class NotUnderstood(Exception):
    pass


def words_to_digits(text):
    """'twenty three point five' -> '23.5', 'twenty-one' -> '21', 'two' -> '2'."""
    tokens = text.replace("-", " ").split()
    out, i = [], 0
    while i < len(tokens):
        t = tokens[i]
        if t in TENS or (t in SMALL and t not in ("a", "an", "oh")):
            value = TENS.get(t, SMALL.get(t))
            i += 1
            if t in TENS and i < len(tokens) and tokens[i] in SMALL and 0 < SMALL[tokens[i]] < 10 \
                    and tokens[i] not in ("a", "an"):
                value += SMALL[tokens[i]]
                i += 1
            text_value = str(value)
            if i + 1 < len(tokens) and tokens[i] == "point" and tokens[i + 1] in SMALL:
                text_value += "." + str(SMALL[tokens[i + 1]])
                i += 2
            elif i + 2 < len(tokens) and tokens[i:i + 3] == ["and", "a", "half"]:
                text_value += ".5"
                i += 3
            out.append(text_value)
            continue
        out.append(t)
        i += 1
    return " ".join(out)


def normalise(text):
    t = text.lower().replace("°", " degrees ").replace("º", " degrees ")
    t = re.sub(r"(\d),(\d)", r"\1.\2", t)                     # 23,5 -> 23.5
    t = re.sub(r"[,;]", " , ", t)                              # kept: they separate commands
    t = re.sub(r"[^\w\s.,\-]", " ", t)                         # other punctuation of dictation
    t = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", t)                  # full stops, not decimal points
    t = words_to_digits(t)
    t = re.sub(r"(\d+) and a half", r"\1.5", t)
    t = re.sub(r"(\d+) point (\d)", r"\1.\2", t)
    return re.sub(r"\s+", " ", t).strip()


FILLER = re.compile(r"^(?:(?:please|ok|okay|hey|siri|now|can you|could you|would you|will you|i want you to|"
                    r"i d like you to|i would like you to|i want to|i d like to|go ahead and)\s+)+|"
                    r"(?:\s+(?:please|now|thanks|thank you))+$")
NOTHING = {"", "please", "thanks", "thank you", "ok", "okay", "the stove", "stove", "it", "now"}


def clauses(text):
    parts = re.split(r"\s*(?:,|;|\band then\b|\bthen\b|\band also\b|\balso\b|\band\b|\bplus\b)\s*", text)
    out = []
    for p in parts:
        p = FILLER.sub("", p.strip()).strip()
        if p not in NOTHING:
            out.append(p)
    return out


def _number(clause):
    m = re.search(NUMBER, clause)
    return float(m.group(1)) if m else None


def parse_clause(c, setpoint=None):
    """One clause -> list of (action, value), 'state' or raise NotUnderstood."""
    has = lambda *words: any(re.search(rf"\b{w}\b", c) for w in words)
    if (has("how", "what", "whats", "status", "state", "tell me", "report") or c.startswith(("is ", "are ", "does "))) \
            and not has("set", "turn", "switch", "make", "put"):
        return "state"
    n = _number(c)
    fanword = has("fan", "fans", "ventilation", "blower")
    if fanword:
        if has("auto", "automatic"):
            return [("fan", 7)]
        if has("max", "maximum", "high", "full"):
            return [("fan", 6)]
        if has("off", "stop") and n is None:
            return [("fan", 0)]
        if has("low", "lowest", "min", "minimum") and n is None:
            return [("fan", 1)]
        if has("medium", "middle") and n is None:
            return [("fan", 3)]
        if has("on", "start") and n is None:
            return [("fan", 7)]
        if n is not None and n == int(n):
            return [("fan", int(n))]
        raise NotUnderstood(c)
    if has("power", "level", "flame", "fire", "heat level", "intensity"):
        if has("max", "maximum", "full", "highest") and n is None:
            return [("power", 5)]
        if has("min", "minimum", "lowest") and n is None:
            return [("power", 1)]
        if n is not None and n == int(n) and 1 <= n <= 5:
            out = [("power", int(n))]
            if SWITCH_ON.search(c):
                out.append(("on", None))
            return out
        if n is None and SWITCH_OFF.search(c):
            return [("off", None)]
        if n is None and SWITCH_ON.search(c):
            return [("on", None)]
        raise NotUnderstood(c)
    if n is None and SWITCH_OFF.search(c):
        return [("off", None)]
    warmer, colder = has("warmer", "hotter", "increase", "raise", "up", "higher"), \
        has("colder", "cooler", "decrease", "lower", "reduce", "down")
    if (warmer or colder) and not has("to") and (has("warmer", "colder", "cooler", "hotter") or
                                                 has("temperature", "temp", "degrees", "degree", "it") or
                                                 has("turn") and has("heat")):
        if callable(setpoint):
            setpoint = setpoint()
        if setpoint is None:
            raise NotUnderstood(c)
        step = n if n is not None and n <= 10 else 1.0
        if has("bit", "little") and n is None:
            step = 0.5
        return [("setpoint", round(setpoint + (step if warmer else -step), 1))]
    if n is not None and (has("temperature", "temp", "degrees", "degree", "celsius", "warm", "heat", "setpoint",
                              "target") or (has("set", "to", "at", "make", "put") and 5 <= n <= 40)):
        out = [("setpoint", n)]
        if SWITCH_ON.search(c):
            out.append(("on", None))
        return out
    if n is None and (SWITCH_ON.search(c) or has("on", "fire up", "heat up", "warm up", "heat")):
        return [("on", None)]
    raise NotUnderstood(c)


def parse(text, setpoint=None):
    """Spoken text -> (commands in a safe order, wants the state spoken back); raises NotUnderstood.

    setpoint: the stove's current target (or a function that reads it), for "warmer" and "colder".
    """
    norm = normalise(text or "")
    commands, wants_state, unknown = [], False, []
    parts = clauses(norm)
    if not parts:
        raise NotUnderstood(text or "")
    for c in parts:
        try:
            r = parse_clause(c, setpoint)
        except NotUnderstood:
            unknown.append(c)
            continue
        if r == "state":
            wants_state = True
            continue
        for action, value in r:
            if action == "setpoint":
                setpoint = value
            commands.append((action, value))
    if unknown:
        raise NotUnderstood(", ".join(unknown))
    # the last word on each setting counts; settings first, then on, off last
    last = {}
    for action, value in commands:
        last["switch" if action in ("on", "off") else action] = (action, value)
    order = ["setpoint", "power", "fan", "switch"]
    return [last[k] for k in order if k in last], wants_state


def sorry(text, error):
    what = str(error).strip()
    if not what:
        return "Sorry, I heard nothing. Try: turn on and set the temperature to 22 degrees."
    return f"Sorry, I did not understand \u201c{what}\u201d. Nothing was changed. " \
           "Try: turn on and set the temperature to 22 degrees."
