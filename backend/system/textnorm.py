"""One canonical spelling for a name, used on BOTH sides of every fuzzy match
(the index alias AND the spoken query) so they line up.

Speech and filenames disagree in predictable ways:
  * "project two"  vs  a folder literally named "project2"
  * "summer grind" vs  "summer-grind-2026"
  * "chapter 3"    vs  "chapter3" / "chapter three"

So we: lowercase, turn separators (_ - .) into spaces, spell number words as
digits ("two" -> "2"), split letter/digit runs ("project2" -> "project 2"), and
drop stray punctuation. Normalising the index the same way it normalises the
query is what makes "project two" reach "project2".
"""
import re

# Spoken number words -> digits. 0-20 plus the tens covers realistic file and
# project numbering ("project two", "chapter fifteen", "week 3").
_NUM = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
    "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18",
    "nineteen": "19", "twenty": "20", "thirty": "30", "forty": "40", "fifty": "50",
    "sixty": "60", "seventy": "70", "eighty": "80", "ninety": "90",
}
_NUM_RE = re.compile(r"\b(" + "|".join(_NUM) + r")\b", re.I)
# The seam between letters and digits, in either order ("project2", "264h").
_LETTER_DIGIT = re.compile(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")


# camelCase / PascalCase boundary: 'SteamLibrary' -> 'Steam Library', so a
# no-separator folder name matches the multi-word spoken query.
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def normalize(text: str) -> str:
    t = _CAMEL.sub(" ", text or "")                      # split camelCase FIRST
    t = t.lower()
    t = re.sub(r"[_\-.]+", " ", t)                       # separators -> space
    t = _NUM_RE.sub(lambda m: _NUM[m.group(1).lower()], t)   # two -> 2
    t = _LETTER_DIGIT.sub(" ", t)                        # project2 -> project 2
    t = re.sub(r"[^\w\s]", " ", t)                       # drop stray punctuation
    return re.sub(r"\s+", " ", t).strip()


def tokens(text: str) -> list[str]:
    return normalize(text).split()


# Spoken-number parsing that COMBINES words: 'seventy five' -> 75 (the plain
# word map above would give '70 5'). Handles digits, single words, tens+unit,
# and 'hundred'. Enough for volume / brightness / counts (0-100+).
_ONES = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
         "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
         "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
         "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}


def parse_number(text: str) -> int | None:
    """First integer in `text`, spoken or written. 'set it to seventy five' -> 75,
    'forty percent' -> 40, '100' -> 100, 'a hundred' -> 100. None if there's no
    number at all."""
    if not text:
        return None
    t = re.sub(r"[-]", " ", text.lower())
    m = re.search(r"\d{1,4}", t)              # a written number wins outright
    if m:
        return int(m.group())
    total = cur = 0
    seen = False
    for w in re.findall(r"[a-z]+", t):
        if w in _ONES:
            cur += _ONES[w]; seen = True
        elif w in _TENS:
            cur += _TENS[w]; seen = True
        elif w == "hundred":
            cur = (cur or 1) * 100; seen = True
        elif w in ("and", "a"):
            continue
        elif seen:
            break                              # number phrase ended
    return (total + cur) if seen else None
