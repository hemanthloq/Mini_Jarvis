"""Pronoun resolution for 'open it' — pure helpers, no state.

When JARVIS itself names something openable ("Would you like me to open WhatsApp
for you?") and the user answers "open it", the word "it" must resolve to that
named thing — never get fuzzy-matched as a search term (which once opened the
'iSCSI Initiator' admin tool). main.py holds the one-slot memory; these two
functions are the pure logic around it.
"""
import re

# "open it" / "yes, open it" / "go ahead and open that" — a pronoun with NO noun
# after it. If a real noun follows ("open that whatsapp photo") this must NOT
# fire; the actual name should be resolved normally instead.
_PRONOUN_OPEN = re.compile(
    r"^(?:(?:yes|yeah|yep|sure|ok|okay|alright|please|go ahead|do it)[,.\s]+)*"
    r"(?:go ahead(?: and)?\s+)?(?:can you\s+|could you\s+|would you\s+|please\s+)?"
    r"(?:open|launch|start|pull up|bring up)\s+"
    r"(?:it|that one|that|this|them|those)(?:\s+(?:again|now|please|up))?[.!?]*$",
    re.I)

# What JARVIS offered/claimed to open, pulled from its own reply
# ("...open WhatsApp for you?" -> "WhatsApp").
_OPEN_REF = re.compile(r"\bopen(?:ing)?\s+(.+?)(?:\s+for you)?\s*(?:[?.!,]|$)", re.I)

_PRONOUNS = {"it", "that", "this", "them", "those", "that one"}


def is_pronoun_open(text: str) -> bool:
    """True for 'open it' / 'yes, open that' with no noun of its own."""
    return bool(_PRONOUN_OPEN.match((text or "").strip()))


# Self-correction within one utterance: "open chrome, actually no, open notepad
# instead" -> "open notepad". Only fires when the utterance STARTS with a command
# verb (so "what actually happened in 1969" is left alone) AND a STRONG
# correction marker is present ("actually no", "never mind", "i meant" — not a
# bare "actually", which is far too common in ordinary speech).
_CMD_START = re.compile(
    r"^\s*(?:open|launch|start|play|set|change|put|turn|increase|raise|lower|"
    r"decrease|mute|unmute|close|remind|note|search|google|show|read|volume|"
    r"brightness|send|message|call|pause|resume|skip)\b", re.I)
_CORRECTION = re.compile(
    r"\b(?:actually\s+no|no\s+wait|wait\s+no|never\s?mind|scratch that|"
    r"forget (?:that|it)|i meant|on second thought|make it)\b[,\s]*(?:no[,\s]+)?",
    re.I)


def apply_self_correction(text: str) -> str:
    """Collapse a self-corrected command to just the corrected intent."""
    t = (text or "").strip()
    start = _CMD_START.match(t)
    if not start:
        return t
    marks = list(_CORRECTION.finditer(t))
    if not marks:
        return t
    verb = start.group().strip()
    tail = re.sub(r"\s+instead\b\.?$", "", t[marks[-1].end():].strip(" ,."),
                  flags=re.I).strip()
    if not tail:
        return "never mind"                       # they cancelled outright
    if not _CMD_START.match(tail):                # "...I meant notepad" -> re-verb
        tail = f"{verb} {tail}"
    return tail


def extract_referent(reply: str) -> str | None:
    """The openable thing named in JARVIS's reply, for a later 'open it'. None if
    the reply named nothing (so an old referent isn't reused staleley)."""
    refs = _OPEN_REF.findall(reply or "")
    if not refs:
        return None
    ref = refs[-1].strip(" .,!?'\"")
    ref = re.sub(r"^(?:the|my|your|a|an|up)\s+", "", ref, flags=re.I).strip()
    if not ref or ref.lower() in _PRONOUNS:
        return None
    return ref
