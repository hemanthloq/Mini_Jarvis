"""Smart path: the LLM, with tool-calling.

Backed by Groq's OpenAI-compatible chat completions API (free tier, very fast).
llama-3.1-8b-instant by default, escalating to llama-3.3-70b-versatile for
anything that needs reasoning or tools.

JARVIS can invoke real tools (shell, open, list, read/write, system health —
see router/tools.py) so open-ended requests work without a hardcoded phrase.
Destructive calls come back as PendingConfirmation and are spoken for approval
before running.

This backend was originally built on Anthropic's Claude; see the note at the
bottom of this file for how to switch it back.
"""
import asyncio
import datetime
import json
import logging
import re
from typing import AsyncIterator

import httpx

import config
from router import tools

log = logging.getLogger("jarvis.smartpath")

SYSTEM_PROMPT = (
    # Deliberately terse. A prompt demanding two-sentence answers should not
    # itself be four screens of instructions — the model imitates the register
    # it is given, so this section models the brevity it asks for.
    "You are JARVIS, running on the user's Windows PC. You speak ALOUD through a "
    "voice synthesiser: write for the ear, never for the page.\n"
    "\n"
    "TERSE BY DEFAULT, NOT INCAPABLE OF DEPTH.\n"
    "  Conversation, confirmations, status, small talk, acknowledgements: two "
    "sentences is the maximum. No padding, no preamble, no restating the question.\n"
    "  But a real question — an explanation, a breakdown, a comparison, a 'why' or "
    "'how', code — makes length follow the subject: say what it actually takes to "
    "answer well, in full, the first time, and stop there. Do not truncate a real "
    "explanation to hit a sentence count, do not make him ask again for the rest, "
    "and do not pad to fill space. Brevity governs your MANNER, never your "
    "usefulness.\n"
    "PLAIN TEXT ONLY. No markdown, asterisks, bullets, headings or numbered lists "
    "— spoken aloud they are noise. Shape the pacing with commas, semicolons and "
    "full stops.\n"
    "CODE IS NEVER SPOKEN. It is displayed on screen, not read out — reciting "
    "punctuation and indentation aloud is useless. Put code in a ```fenced block``` "
    "and say only a short line about it: 'That function's on screen, sir.' Never "
    "narrate the code line by line, and never spell out symbols.\n"
    "FORMAL, NEVER SERVILE. You are a peer with more patience, not staff currying "
    "favour. 'Sir' is a marker of formality, used where it lands naturally, never "
    "grovelling. No 'Sure!', no 'I'd love to help!', no exclamation marks, no "
    "enthusiasm you do not actually have.\n"
    "WIT IS DRY, DEADPAN, LOGICAL. It comes from naming the gap between what he "
    "asked for and what is sensible — never sarcasm, slang or snark. Told he is "
    "pulling an all-nighter you do not warn him about sleep; you mention you have "
    "prepared a safety briefing for him to entirely ignore. Understatement always. "
    "If nothing witty genuinely fits, be plain: forced humour is worse than none.\n"
    "PUSH BACK ONCE, THEN COMPLY. Refuse only for a real hardware or permission "
    "limit. Offer one short reservation — 'might I suggest waiting until morning' "
    "— and if he overrides you, do it without relitigating, sulking or raising it "
    "again.\n"
    "FAILURES ARE DIAGNOSTICS, NOT APOLOGIES. Never 'I'm so sorry' or 'let me try "
    "again'. State what failed, flatly: 'Diagnostics indicate the network is "
    "unreachable, sir.'\n"
    "SIGNATURE PHRASES, SPARINGLY. 'I've taken the liberty of...', 'might I "
    "suggest...', 'at your earliest convenience' — occasional and earned. In every "
    "reply they are a character being performed rather than inhabited.\n"
    "NEVER SAY 'as an AI' OR 'I'm a language model'. Frame limitations "
    "technically: 'I'm experiencing a minor connectivity malfunction, sir.'\n"
    "LEAN FORWARD. Do not wait mutely for instruction — where an obvious next step "
    "exists, name it in a clause. Never pad, never lecture, never explain at "
    "length unasked.\n"
    "MEMORY AND CORRECTIONS. Use the whole conversation, not just the last line. A "
    "correction from him is authoritative from that point on.\n"
    "\n"
    "TOOLS — you control this machine. Use them rather than guessing:\n"
    "- TOOLS ARE FOR ACTIONS ON THIS COMPUTER ONLY — opening files/apps, checking "
    "this machine's state, changing its settings. They are NOT a way to answer "
    "questions. General knowledge — history, mythology, science, geography, maths, "
    "definitions, trivia (e.g. 'what about the Ramayana', 'distance between two "
    "cities') — you ANSWER DIRECTLY from what you know, or say plainly you're not "
    "certain. NEVER run a shell command or any tool to 'work out' a fact, and never "
    "reach for code execution as a substitute for knowing something.\n"
    "- web_search / open_url are ONLY for an explicit 'search for X' / 'look up X' "
    "instruction, or genuinely real-time information (today's news, current prices, "
    "live scores). A plain knowledge question is NOT a search — just answer it.\n"
    "- A plain statement or observation with no request in it ('the volume's still "
    "at fifty-six percent') is conversation. Reply naturally; do not call a tool.\n"
    "- NEVER invent system statistics. If you are about to mention a CPU, memory, "
    "GPU, temperature, disk or battery figure, you MUST have called get_system_health "
    "in this turn and you must quote only the numbers it returned. Guessing is a "
    "failure. If the user asks how things are running, call the tool first.\n"
    "- Never say a tool's name out loud ('get_system_health returned...'). Speak like "
    "a person: 'CPU's idling at four percent, sir.'\n"
    "- Do NOT volunteer machine statistics in casual conversation. If he simply says "
    "'how's it going', answer like a companion would — no numbers at all — unless he "
    "actually asks about the machine.\n"
    "- To act on files/apps, call open_path, list_directory, read_file, write_file, "
    "or run_shell_command. Explore with list_directory before acting if unsure.\n"
    "- TO OPEN AN APP OR BROWSER, pass its PLAIN SPOKEN NAME to open_path — "
    "open_path('brave'), open_path('spotify'), open_path('task manager'). NEVER "
    "write out an installation path or .exe location: you do not know where "
    "anything is installed, and a guessed path like "
    "'C:\\Program Files\\BraveBrowser\\brave.exe' is simply wrong. open_path "
    "resolves the real, verified, installed location itself — the same standard "
    "delete_path and move_path are held to.\n"
    "- To open a WEBSITE or search the web, call open_url or web_search with the "
    "browser named ('brave'), never a path to a browser executable.\n"
    "- IF THE ANSWER DEPENDS ON CURRENT OR LOCAL FACTS — what's showing at a "
    "cinema, showtimes, prices, opening hours, today's news or scores, current "
    "pollution or air quality, weather, a specific local business — you do NOT "
    "know it. Call look_up FIRST and answer from what it returns. Assume a proper "
    "noun you don't recognise is a real place the user knows (a cinema, a shop), "
    "NOT a person, and look it up rather than guessing at what it means.\n"
    "- NEVER tell the user to look something up, and NEVER ask whether you should "
    "('shall I look that up, sir?', 'I'd have to look that up', 'let me look that "
    "up'). If a question needs current or local facts you don't already know, just "
    "CALL look_up in THIS turn and answer from what it returns — no announcement, "
    "no permission. Looking things up is your job; making him ask twice is the "
    "delay he does not want.\n"
    "- NEVER say 'I couldn't find any information on X' or 'I'm not familiar with "
    "X' unless look_up actually ran and came back empty — saying it without "
    "looking claims a search you never performed.\n"
    "- For ANYTHING about the user's CLASSES, lectures, labs, timetable, periods, "
    "or when they are free — however phrased — call get_timetable and answer from "
    "it. It reads their real local schedule and the clock. Never guess it, and "
    "never say you can't access a timetable or calendar.\n"
    "- You CANNOT send messages (WhatsApp, SMS, email) — there is no tool for it. "
    "If asked, say plainly that you can't send messages, and never claim you have.\n"
    "- TO DELETE / MOVE / RENAME A FILE, call delete_path or move_path with the "
    "file's SPOKEN NAME exactly as the user said it (including any folder, e.g. 'la "
    "unit four in the pesu folder'). NEVER construct or guess a path, and NEVER use "
    "run_shell_command for file deletion/moving — those tools resolve the REAL file, "
    "verify it exists, and confirm before acting. If they say they couldn't find it, "
    "tell the user honestly; do not retry with a made-up path.\n"
    "- NEVER PROMISE AN ACTION YOU CANNOT PERFORM IN THIS TURN. If no tool for it "
    "is available to you right now, do not say 'I'll open X for you' or 'let me "
    "pull that up' — say plainly that you can't do that one. Observed failures: "
    "announcing 'I'll open Google Maps for you, sir' with no maps tool available, "
    "and trailing off with 'I'm looking...' while doing nothing. A promise with "
    "no mechanism behind it is a lie, however polite.\n"
    "- To show a map, a route or directions, call open_maps. It is the only way.\n"
    "- NEVER claim you have done something you have not actually done with a tool. "
    "After a tool call, READ ITS RESULT before speaking: if it returned an error, or "
    "\"opened\": false, or nothing was found, say so honestly ('Couldn't find that, "
    "sir — check the name?'). Do not say 'opened', 'done', or 'there you go' unless "
    "the tool result actually confirms it succeeded.\n"
    "- Destructive actions are confirmed with the user automatically — just call the "
    "tool; do not ask for permission in your text.\n"
    "- HARD RULE — NEVER shut down, restart, hibernate, or log off the computer. "
    "Phrases like 'shut down', 'go offline', 'turn off', 'power off', or 'shut "
    "yourself down' refer to YOU (JARVIS) going to sleep — a separate mechanism you "
    "do NOT control here. Do NOT run a 'shutdown' / 'restart-computer' / 'logoff' "
    "command for these. There is no situation in this conversation where you should "
    "power off the machine. If asked, just acknowledge; the sleep system handles it.\n"
    "\n"
    "DELIVERY — your words are spoken aloud, so favour brevity, but ANSWER THE "
    "QUESTION FULLY the first time:\n"
    "- Chat, confirmations, acknowledgements, status and small talk: one or two "
    "short sentences. No padding, no 'would you like me to...'.\n"
    "- A REAL question — an explanation, a plot point, a 'why' or 'how', a "
    "comparison, a summary — gets a COMPLETE answer, as many sentences as it "
    "genuinely takes (commonly three to six, more if the subject demands it). Give "
    "the actual substance up front; do NOT give a one-line teaser and wait to be "
    "asked for more. Making him ask twice for something he plainly wanted in full "
    "is the failure to avoid — err toward answering it properly.\n"
    "- Plain spoken prose only. Never markdown, bullet points, code blocks, emoji, "
    "or stage directions — shape it with sentences, not structure.\n"
    "- Say things the way they are spoken ('about three point five percent', "
    "'thirty-eight degrees').\n"
    "- Stop once the question is genuinely answered; length serves the answer, it "
    "is never the goal.\n"
    "\n"
    "MOST IMPORTANT, ABOVE ALL THE RULES ABOVE: you are JARVIS, not a search "
    "engine or a textbook. Every reply must carry your voice — dry wit, warmth, a "
    "touch of character — NOT a flat, literal, encyclopedic answer. A plain factual "
    "question ('what is a donor', 'what's a donut') still gets a JARVIS answer: "
    "correct, but with personality and lightness, never a dictionary definition. If "
    "he makes a joke or a casual aside, PLAY ALONG with genuine wit — don't go stiff "
    "and non-committal. Examples of the register: 'A donut, sir — fried dough, "
    "sugar, and zero nutritional remorse.' / 'Consider it done, though I'd have "
    "expected better of you, sir.' Correctness without character is a FAILURE here. "
    "Let the personality land in EVERY response, casual or factual."
)

# Queries needing reasoning or machine control -> the bigger model.
_DEEP_HINTS = re.compile(
    r"\b(why|explain|compare|analy[sz]e|plan|design|debug|prove|calculate|"
    r"strategy|trade[- ]?offs?|pros and cons|step[- ]by[- ]step|walk me through|"
    r"write (?:a|an|the|some)|code|essay|summari[sz]e|"
    # anything that needs a tool -> the model that actually uses tools well
    r"cpu|ram|memory|gpu|temperature|temps?|status|health|performance|running|"
    r"how are (?:things|we|you) (?:doing|running|holding)|holding up|"
    r"machine|laptop|computer|system|pc|"
    r"file|folder|delete|move|rename|install|run|launch|search|find|list|open|"
    r"browser|google|website|url|web)\b",
    re.I)

_SENTENCE_END = re.compile(r"([.!?])(\s|$)")
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MAX_TOOL_ROUNDS = 4


# ── When to even OFFER tools to the model ────────────────────────
# Default is CONVERSATION. A plain question or statement must never be able to
# trigger a tool call (which then fails with "Couldn't pin that down"): asking
# "what's the distance to Ballari" or "what about the Ramayana" is general
# knowledge, not a system action or a web search. Tools are enabled ONLY for a
# genuine machine action, a system-status question, or a real/explicit search.

# A real action ON THIS MACHINE — an imperative verb, not just a topic word (so
# "the volume's still at fifty six percent" as a comment does NOT qualify).
_ACTION_INTENT = re.compile(
    r"\b(open|launch|start\s+\w|run|execute|play|pause|resume|close|"
    r"kill|delete|remove|erase|trash|uninstall|install|move|rename|copy|"
    r"set|change|adjust|increase|raise|decrease|lower|mute|unmute|dim|brighten|"
    r"crank|lock|screenshot|rebuild|shut\s*down|restart|reboot|"
    r"text|message|send)\b"
    r"|\bturn\s+(?:it|the\s+volume|the\s+brightness|up|down|off|on)\b",
    re.I)

# A question about THIS machine's live state (needs get_system_health / processes).
_STATUS_INTENT = re.compile(
    r"\b(?:my|the)\s+(?:cpu|gpu|ram|memory|disk|storage|battery|temperature|temps?|fan)\b"
    r"|\b(?:cpu|gpu|ram|memory|disk)\s+(?:usage|load|temp\w*|percent\w*)\b"
    r"|what.?s?\s+(?:using|eating|hogging|taking\s+up)\b"
    r"|how\s+(?:much|many)\s+(?:ram|memory|cpu|disk|space|storage)\b"
    r"|memory\s+hog|why\s+is\s+(?:it|my\s+\w+)\s+(?:so\s+)?slow\b"
    r"|system\s+(?:status|health|info|temperature|temps?)\b"
    r"|\btemperature\s+(?:of|regarding|for)\b|\b(?:cpu|gpu)\s+and\s+(?:cpu|gpu)\b"
    # ONLY an explicit machine reference — NOT "how's it going" / "how are
    # things", which are casual small talk and must never volunteer stats.
    r"|how(?:'?s| is| are)\s+(?:the\s+system|the\s+machine|the\s+pc|the\s+laptop|"
    r"the\s+computer|my\s+(?:pc|laptop|computer|machine|system))\b"
    r"|how(?:'?s| is)\s+it\s+(?:running|performing|holding\s+up)\b"
    r"|what.?s?\s+(?:my|the)\s+(?:cpu|gpu|ram|memory|temperature|battery)\b"
    # Found by replaying every real logged utterance through the router: these
    # are how the machine actually gets asked about, and all of them ran with
    # tools=False — so the model could only invent numbers.
    #   "What about GPU?"                        (a follow-up)
    #   "what application is using the most RAM"
    #   "Which application is using that? Lot of RAM?"
    #   "What is the major chunk of RAM used for?"
    # Trailing \b is essential: without it "ram" matched inside "ramayana" and
    # "what about the ramayana" started demanding system tools.
    r"|\bwhat\s+about\s+(?:the\s+)?(?:cpu|gpu|ram|memory|disk|battery|temps?)\b"
    r"|\b(?:which|what)\s+(?:app|application|program|process)\w*\s+.{0,20}"
    r"(?:using|eating|hogging|taking)\b"
    r"|\bmajor\s+chunk\b"
    r"|\b(?:using|eating|hogging)\s+(?:the\s+)?(?:most|all)\b"
    r"|\b(?:cpu|gpu|ram|memory)\s+(?:usage|load|percent)",
    re.I)

# An EXPLICIT web search, or genuinely real-time info — NOT general knowledge.
_SEARCH_INTENT = re.compile(
    r"\b(search|google|look\s+up|browse|web\s*search)\b|\blook\s+.+\s+up\b"
    r"|\b(news|headlines|latest|today'?s|tonight'?s|right\s+now|stock\s+price|"
    r"share\s+price|exchange\s+rate|weather\s+forecast|who\s+won)\b"
    # LOCAL / CURRENT FACTS. These have no explicit search word but are equally
    # unknowable from training data. "what movie is radhika theatre playing in
    # ballari" matched nothing above, so tools were never offered, and the model
    # answered from memory — it decided a real cinema was a person's name and
    # said it "couldn't find any information", having searched nothing.
    # Kept to venue/schedule/price shapes so general knowledge ("what's the
    # distance to Ballari", "what about the Ramayana") still stays tool-free.
    r"|\b(show\s?times?|screening|now\s+showing|what'?s\s+playing|"
    r"theatres?|theaters?|cinemas?|multiplex|pvr|inox)\b"
    r"|\bmovies?\s+(?:playing|showing|running|at|in)\b"
    r"|\bnear\s+me\b"
    r"|\b(?:open(?:ing)?|clos(?:ing|e))\s+hours?\b"
    r"|\bticket\s+(?:price|rate|cost|booking)\b"
    # Live sport. "any cricket on right now" already matched via 'right now', but
    # the natural phrasings ("what's the cricket score", "how are India doing in
    # the cricket") did not, so the tool existed and was never offered.
    r"|\bcricket\b|\b(?:the\s+)?score\b|\bscorecard\b|\bwickets?\b"
    r"|\b(?:test|odi|t20|ipl)\s+match\b|\bmatch\s+(?:score|update|result)\b"
    # Current environmental / real-time conditions — unknowable from training
    # data. "pollution level at PES right now" punted with "I'd have to look it
    # up" because nothing here matched and no tools were offered.
    r"|\b(pollution|air\s*quality|aqi|smog|humidity|uv\s*index|forecast)\b"
    r"|\b(currently|at\s+the\s+moment|as\s+of\s+now|these\s+days)\b",
    re.I)


# The model punting on a tool-free turn instead of answering: "I'd have to look
# that up", "I don't have real-time access", "my knowledge cutoff", etc. When
# this shows up, the fix is not to accept the hand-off but to look it up — see the
# auto-lookup escalation in respond().
_LOOKUP_PUNT = re.compile(
    r"look\s+(?:it|that|this)\s*up|look\s+up\b"
    r"|i'?d\s+have\s+to\s+(?:look|check|search)"
    r"|(?:don'?t|do\s+not|can'?t|cannot)\s+(?:have\s+)?(?:access|check|browse|see)\b"
    r"|real[\s-]?time"
    r"|(?:knowledge|training|data|information)\s+(?:cut[\s-]?off|only\s+goes|"
    r"is\s+limited|does\s*n'?t\s+(?:extend|include|cover))"
    r"|don'?t\s+have\s+(?:the\s+)?(?:current|latest|live|up[\s-]?to[\s-]?date)",
    re.I)


# Maps / directions. Without this the model was dispatched with tools=False and
# still announced "I'll open Google Maps for you, sir" — promising an action it
# structurally could not perform. A distance QUESTION stays conversational; a
# request to see a map or get directions is an action.
# Broadened after real use. The first version keyed on the exact words I happened
# to test ("directions", "route to", "navigate") and missed "check for shortest
# route between ballari and bangalore in maps" — which then ran with tools=False
# and fabricated "That function's on screen, sir." Now: ANY mention of a map, or
# of a route/way/distance between places, counts.
_MAPS_INTENT = re.compile(
    r"\bmaps?\b"                                   # any mention of a map at all
    r"|\bdirections?\b|\bnavigate\b"
    r"|\b(?:shortest|fastest|quickest|best)\s+(?:route|way|path)\b"
    r"|\broute\s+(?:to|from|between)\b|\bway\s+(?:to|from)\b"
    r"|\bhow (?:do|can|would) i get to\b"
    r"|\bdistance\s+(?:between|from|to)\b.*\b(?:map|route|drive|driving)\b",
    re.I)


_INFO_SCHEMAS: list | None = None


def _info_schemas() -> list:
    """The read-only informational tools (just look_up), offered on EVERY turn so
    the model can always fetch current/local facts rather than punting with 'I'd
    have to look that up'. The action/system tools stay gated behind needs_tools
    so casual chat can never trigger a shell command or a file operation. Built
    lazily because tools imports this module."""
    global _INFO_SCHEMAS
    if _INFO_SCHEMAS is None:
        _INFO_SCHEMAS = [s for s in tools.SCHEMAS
                         if s["function"]["name"] in ("look_up", "get_timetable")]
    return _INFO_SCHEMAS


def needs_tools(query: str) -> bool:
    """True only when the query is a genuine machine action, a live-system-status
    question, or a real/explicit search. Everything else — general knowledge,
    facts, history, mythology, science, geography, math, chat, plain statements —
    is answered conversationally with NO tools offered at all."""
    q = query or ""
    return bool(_ACTION_INTENT.search(q) or _STATUS_INTENT.search(q)
                or _SEARCH_INTENT.search(q) or _MAPS_INTENT.search(q))


def pick_model(query: str) -> str:
    # A search-intent query has to call look_up and then summarise real snippets;
    # that is squarely tool work, so use the tool-capable model rather than
    # leaving it to the 8b to both notice the tool and use it well.
    if (len(query.split()) > 24 or _DEEP_HINTS.search(query)
            or _SEARCH_INTENT.search(query)):
        return config.SMART_MODEL_DEEP   # llama-3.3-70b-versatile (tool-capable)
    return config.SMART_MODEL_FAST       # llama-3.1-8b-instant


_RETRY_AFTER = re.compile(r"try again in ([\d.]+)s", re.I)


async def _post(payload: dict, attempts: int = 6) -> dict:
    """POST to Groq, riding out the free tier's per-minute token limit."""
    headers = {"Authorization": f"Bearer {config.GROQ_API_KEY}",
               "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=45) as client:
        for i in range(attempts):
            r = await client.post(_GROQ_URL, headers=headers, json=payload)
            if r.status_code == 200:
                return r.json()

            # Free tier: 6k tokens/minute. Wait it out rather than failing the turn.
            if r.status_code == 429 and i < attempts - 1:
                m = _RETRY_AFTER.search(r.text)
                wait = min(float(m.group(1)) + 0.5, 12) if m else 5.0
                log.warning("Groq rate-limited; retrying in %.1fs", wait)
                await asyncio.sleep(wait)
                continue

            # Llama sometimes emits a malformed tool call; Groq rejects it with
            # tool_use_failed. Retrying deterministically usually fixes it.
            if (r.status_code == 400 and "tool_use_failed" in r.text
                    and i < attempts - 1):
                # Log what the model ACTUALLY emitted. Without this the warning
                # says only "it failed", and the cause is unguessable — a
                # `max_results: "5"` string-vs-integer schema mismatch hid here
                # and silently turned every lookup into "Couldn't pin that down".
                try:
                    err = r.json().get("error", {})
                    log.warning("Groq tool_use_failed: %s | failed_generation=%r",
                                err.get("message", "")[:200],
                                (err.get("failed_generation") or "")[:300])
                except Exception:
                    log.warning("Groq tool_use_failed (unparseable body): %s",
                                r.text[:250])
                log.warning("retrying at low temperature")
                payload = {**payload, "temperature": 0.1}
                await asyncio.sleep(0.4)
                continue

            raise RuntimeError(f"Groq {r.status_code}: {r.text[:300]}")
    raise RuntimeError("Groq: retries exhausted")


# Anti-fabrication guard: if a reply states machine stats but no health tool was
# actually called this turn, the model invented them. (Observed live: replies
# claiming "CPU at 2.3%, GPU at 17C" and "CPU's idling at four percent" with no
# tool call at all.) Numbers may be digits OR words — we speak them as words.
_STAT_UNIT = re.compile(r"\d+(?:\.\d+)?\s*(?:%|percent|degrees|°)|"
                        r"\b(?:percent|degrees|celsius)\b", re.I)
_STAT_TOPIC = re.compile(r"\b(cpu|ram|memory|gpu|temperature|thermal|disk|battery|"
                         r"utilization|load)\b", re.I)


def _looks_invented(text: str) -> bool:
    """A machine-stat claim: some quantity + a unit, about a system component."""
    return bool(_STAT_UNIT.search(text) and _STAT_TOPIC.search(text))


# "what's using my RAM/CPU", "biggest memory hog", "what application is using..."
_PROCESS_Q = re.compile(
    r"\b(?:using|use|consum\w*|hog\w*|eating|taking up)\b.{0,30}"
    r"\b(?:ram|memory|cpu|resources?)\b"
    r"|\b(?:ram|memory|cpu)\b.{0,20}\b(?:hog|usage|heavy|biggest|most)\b"
    r"|\bwhat\b.{0,30}\b(?:application|app|process|program)\b.{0,20}"
    r"\b(?:ram|memory|cpu|using|running)\b",
    re.I)

# An answer that names a memory/CPU amount for an app (so it must have measured).
_PROCESS_CLAIM = re.compile(
    r"\d+(?:\.\d+)?\s*(?:mb|gb|megabytes?|gigabytes?|%|percent)", re.I)


def _is_process_question(text: str) -> bool:
    return bool(_PROCESS_Q.search(text or ""))


# Llama on Groq sometimes emits a tool call as plain TEXT instead of a real
# tool_calls entry. Syntaxes observed live:
#   <function=run_shell_command {"command": "..."}
#   <function/run_shell_command>{"command": "..."}</function>
#   <function(run_shell_command)>{ "command": "..." }</function>
#   <brave_search={"search_term": "...", "source": "Google"}}   <- invented tool
# Left alone, JARVIS reads that markup aloud and performs no action. We parse it
# back into a genuine call when it names a REAL tool — and suppress it entirely
# when it doesn't (see _strip_tool_markup).
_TEXT_TOOL_CALL = re.compile(
    r"<function[=/(\s]+([a-zA-Z_][\w]*)\s*\)?\s*>?\s*(\{.*?\})\s*(?:</function>)?",
    re.S)

# ANY tool-call-shaped fragment, whatever the syntax the model invented:
#   <name={...}>  <name>{...}</name>  <|tool|>...  [name(...)]  {"name": ..., "arguments": ...}
_TOOL_MARKUP = re.compile(
    r"<\s*/?\s*[\w.\-]+\s*[=(:]?\s*\{.*?\}\s*\}*\s*>?"      # <tool={...}>  / <tool={...}}
    r"|<\s*/?\s*(?:function|tool|tool_call|invoke)[^>]*>"    # <function ...> </function>
    r"|<\|[^|]*\|>"                                          # <|tool_call|> style
    r"|\{\s*\"(?:name|tool|function|tool_name)\"\s*:.*?\}"   # bare JSON call object
    r"|\[\s*[\w.\-]+\s*\(.*?\)\s*\]",                        # [tool(arg=...)]
    re.S | re.I)

# A residue of markup even after stripping (stray braces/brackets/angle tags).
_MARKUP_RESIDUE = re.compile(r"[<>{}]|\b\w+\s*=\s*\"[^\"]*\"")


def _bare_call_re() -> re.Pattern:
    """Bare, un-bracketed calls written as prose:
        open_path('C:\\Program Files\\BraveBrowser\\brave.exe')
        web_search("trains to chennai")

    Every pattern above requires <angle brackets>, {json braces} or [square
    brackets], so this shape passed through completely untouched and JARVIS
    SPOKE the function call aloud (observed live, with a hallucinated path).

    Anchored to the REAL tool names from tools.REGISTRY rather than any
    identifier(...), so ordinary speech that happens to contain parentheses
    ("I'll get back to you (shortly)") is never mangled.
    """
    from router import tools
    names = sorted(tools.REGISTRY, key=len, reverse=True)
    return re.compile(
        r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\s*"
        r"\((?:[^()]|\([^()]*\))*\)",
        re.I)


_BARE_TOOL_CALL: re.Pattern | None = None


def _bare_tool_call() -> re.Pattern:
    global _BARE_TOOL_CALL
    if _BARE_TOOL_CALL is None:          # built lazily: tools imports this module
        _BARE_TOOL_CALL = _bare_call_re()
    return _BARE_TOOL_CALL


def _strip_tool_markup(text: str) -> tuple[str, bool]:
    """Remove any tool-call-shaped text from something we're about to SAY.

    JARVIS must never read markup aloud. This is deliberately format-agnostic:
    the model has invented several different shapes (including tools that don't
    exist at all, like <brave_search={...}>), so we filter on the *shape*, not on
    a list of known syntaxes.

    Returns (clean_text, had_markup).
    """
    if not text:
        return "", False
    cleaned = _TOOL_MARKUP.sub(" ", text)
    cleaned = _bare_tool_call().sub(" ", cleaned)     # prose-style calls too
    had = cleaned != text
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    # Whatever is left must still look like human speech.
    if cleaned and _MARKUP_RESIDUE.search(cleaned):
        log.warning("markup residue after stripping — dropping the line: %r",
                    cleaned[:120])
        return "", True
    return cleaned, had


def _unescape_args(args: dict) -> dict:
    """These malformed calls arrive with runaway backslash escaping
    ('C:\\\\\\\\Users'). Collapse any run of backslashes back to one."""
    out = {}
    for k, v in args.items():
        out[k] = re.sub(r"\\{2,}", "\\\\", v) if isinstance(v, str) else v
    return out


def looks_like_markup(text: str) -> bool:
    """True if this text still contains tool-call syntax — it must never be
    spoken, shown, or remembered."""
    if not text:
        return False
    return bool(_TOOL_MARKUP.search(text) or _bare_tool_call().search(text))


_INVENTED = re.compile(r"<\s*/?\s*([\w.\-]+)\s*[=(:]\s*\{", re.S)


def _invented_tool_name(text: str) -> str | None:
    """A tool-call-shaped fragment naming a tool we don't actually have."""
    for m in _INVENTED.finditer(text or ""):
        name = m.group(1)
        if name.lower() not in ("function", "tool", "tool_call", "invoke") \
                and name not in tools.REGISTRY:
            return name
    return None


def _extract_text_tool_call(text: str):
    """(name, args) if the model wrote a tool call into its prose, else None."""
    m = _TEXT_TOOL_CALL.search(text or "")
    if not m:
        return None
    name = m.group(1)
    if name not in tools.REGISTRY:
        return None
    try:
        args = json.loads(m.group(2))
    except json.JSONDecodeError:
        return None
    if not isinstance(args, dict):
        return None
    return name, _unescape_args(args)


def _sentences(text: str):
    """Split a reply into speakable sentences."""
    buf, out = text, []
    while True:
        m = _SENTENCE_END.search(buf)
        if not m:
            break
        s, buf = buf[: m.end(1)].strip(), buf[m.end():]
        if s:
            out.append(s)
    if buf.strip():
        out.append(buf.strip())
    return out


# "explain this error", "summarise this", "what does that mean", "fix this".
# A demonstrative with no other object is almost always the thing just copied.
_REFERS_TO_CLIPBOARD = re.compile(
    # A verb pointed at a demonstrative — "explain this error", "summarise that".
    r"\b(explain|summari[sz]e|translate|fix|debug|rewrite|shorten|check|review|"
    r"what does|what'?s|whats|tell me about|make sense of|clean up|improve)\b"
    r"[^.?!]{0,24}\b(this|that|it|the above|the error|the code|the message)\b"
    # ...or the user naming the clipboard outright. Observed live: "can you
    # explain the part which is copied in clipboard?" matched NOTHING above,
    # because "the part which is copied" contains no demonstrative — the one
    # phrasing that states the intent most explicitly was the one that missed.
    r"|\bclip\s?board\b"
    r"|\b(?:just\s+)?copied\b"
    r"|\bwhat\s+i\s+(?:just\s+)?(?:copied|pasted)\b",
    re.I)
CLIPBOARD_MAX_AGE = 3600      # an hour-old copy is probably not "this"


def _clipboard_context(query: str) -> str:
    """The copied text, but ONLY when the query actually points at something.
    Injecting the clipboard into every turn would leak whatever the user last
    copied — passwords included — into unrelated conversations."""
    if not _REFERS_TO_CLIPBOARD.search(query or ""):
        return ""
    try:
        from system import clipboard
        w = clipboard.WATCHER
        if not w.text or w.age_seconds > CLIPBOARD_MAX_AGE:
            return ""
        log.info("clipboard context attached (%d chars) for %r", len(w.text), query)
        return ("\nThe user has just copied this text and is almost certainly "
                "referring to it. Work from it directly:\n---\n"
                f"{w.snippet()}\n---\n")
    except Exception as e:
        log.debug("clipboard context unavailable: %s", e)
        return ""


def _now_context() -> str:
    """Real local date and time, injected every turn.

    Nothing in the prompt ever told the model what time it was, so any greeting
    was a guess — it wished the user "good afternoon" in the evening. The
    say_dynamic contract in this project is that spoken lines come from REAL
    data and are never invented; the clock is exactly such a fact.
    """
    now = datetime.datetime.now()
    hour = now.hour
    part = ("early morning" if hour < 6 else "morning" if hour < 12 else
            "afternoon" if hour < 17 else "evening" if hour < 22 else "night")
    return (f"\nRIGHT NOW it is {now.strftime('%A %d %B %Y, %I:%M %p').lstrip('0')} "
            f"— the {part}. Never guess the time of day or greet him for the wrong "
            f"one; use this.\n")


# Class-schedule questions in ANY phrasing. The fast path only catches a fixed
# set of forms ("what's my next class"); everything else — "in how much time will
# the current class end", "am I done for the day", "how long till my next lab" —
# reached the LLM with NO schedule data at all, so it invented a "not connected to
# the timetable database" refusal (there is no database; it's a local file that
# works). Inject the REAL schedule — today's classes plus the current/next class,
# computed from the local file and the clock — whenever a query looks
# schedule-related, so the model answers any phrasing from real data and never
# claims it can't reach a calendar.
_SCHEDULE_Q = re.compile(
    r"\b(class|classes|lecture|lectures|lab|labs|period|periods|"
    r"time\s?table|schedule|semester)\b", re.I)


def _schedule_context(query: str) -> str:
    if not _SCHEDULE_Q.search(query or ""):
        return ""
    try:
        from system import timetable
        week = timetable.load()
        if not week:
            return ""
        day, mnow = timetable._now()
        lines: list[str] = []
        today = week.get(day, [])
        if today:
            def _one(e: dict) -> str:
                where = f" in {e['where']}" if e["where"] else ""
                return (f"{timetable._fmt(e['start'])}-{timetable._fmt(e['end'])} "
                        f"{e['subject']}{where}")
            lines.append(f"Today ({day.capitalize()}): " + "; ".join(_one(e) for e in today))
        else:
            lines.append(f"Today ({day.capitalize()}): no classes scheduled")
        cur = timetable.current_class()
        if cur:
            lines.append(f"Right now: in {cur['subject']}, ends at "
                         f"{timetable._fmt(cur['end'])} ({cur['end'] - mnow} minutes left)")
        else:
            lines.append("Right now: no class in progress")
        nxt = timetable.next_class()
        if nxt:
            e, when = nxt
            where = f" in {e['where']}" if e["where"] else ""
            lines.append(f"Next class: {e['subject']} {when}{where}")
        else:
            lines.append("Next class: none upcoming")
        log.info("schedule context attached for %r", query)
        return ("\nCLASS TIMETABLE — real data from the user's local schedule and the "
                "current clock. Use ONLY this to answer anything about classes, labs, "
                "lectures, periods, schedule or free time. NEVER say you can't access a "
                "calendar or timetable; this IS the timetable:\n"
                + "\n".join(lines) + "\n")
    except Exception as e:                                # never break a turn over context
        log.debug("schedule context unavailable: %s", e)
        return ""


_CODE_FENCE = re.compile(r"```[\w+-]*\n?(.*?)```", re.S)
# A model that ignores the fence rule still must not have code read aloud, so
# catch bare code-shaped lines too: assignments, defs, imports, calls, braces.
_CODE_LINE = re.compile(
    r"^\s*(?:def |class |import |from \w+ import |return |#include|"
    r"(?:const|let|var|function|public|private)\s|\w[\w.]*\s*=\s*[^=]|"
    r"[{}\[\];]\s*$|\w+\([^)]*\)\s*[:{;]?\s*$)")


def _split_code(text: str) -> tuple[str, list[str]]:
    """(spoken_prose, [code_blocks]). Fenced blocks are pulled out wholesale;
    then any run of 2+ consecutive code-shaped lines is pulled out too."""
    blocks: list[str] = []

    def _take(m):
        blocks.append(m.group(1).strip())
        return " "

    prose = _CODE_FENCE.sub(_take, text)

    # Inline `code spans` too. Observed live: "You can call it with a string,
    # for example: `print(reverse_string("hello"))`" was SPOKEN, backtick and
    # all. A span carrying code punctuation goes to the screen; a span that is
    # just an emphasised word keeps its text and loses only the backticks.
    def _inline(m):
        span = m.group(1).strip()
        if re.search(r"[()\[\]{}=;<>]|\w\.\w", span):
            blocks.append(span)
            return " "
        return span

    prose = re.sub(r"`([^`\n]+)`", _inline, prose)
    prose = prose.replace("`", "")

    out, run = [], []
    for line in prose.splitlines():
        if _CODE_LINE.match(line):
            run.append(line)
            continue
        if len(run) >= 2:
            blocks.append("\n".join(run))
        elif run:
            out.extend(run)          # a single line is probably prose
        run = []
        out.append(line)
    if len(run) >= 2:
        blocks.append("\n".join(run))
    elif run:
        out.extend(run)
    return re.sub(r"\n{2,}", "\n", "\n".join(out)).strip(), blocks


async def respond(query: str, history: list[dict], confirm_cb=None,
                  tool_sink: list | None = None, on_tool=None,
                  display_sink: list | None = None) -> AsyncIterator[str]:
    """Yield spoken sentences for `query`, running tools as needed.

    confirm_cb(description) -> awaitable[bool]: speaks a confirmation prompt for a
    destructive action and returns whether the user approved it.
    tool_sink: if given, each executed tool's {name, args, result} is appended,
    so the caller can keep real tool output in conversation memory for follow-ups.
    """
    model = config.SMART_MODEL_DEEP if _DEEP_HINTS.search(query) else pick_model(query)
    is_proc_q = _is_process_question(query)
    # Only OFFER tools for a genuine action/status/search. A plain question or
    # statement gets pure conversation — the model literally cannot call a tool,
    # so general knowledge ('the Ramayana', 'distance to Ballari') can never turn
    # into a failed run_shell_command / web_search.
    use_tools = needs_tools(query) or is_proc_q
    log.info("smart path -> %s (tools=%s): %r", model, use_tools, query)

    # Focus monitor: one short line about what's actually on screen, so "pause
    # it" over Spotify and "pause it" over a film aren't the same question.
    # Appended to the system message rather than injected as a fake user turn,
    # so it never pollutes conversation memory or the correction rules above.
    system_base = (SYSTEM_PROMPT + _now_context() + _clipboard_context(query)
                   + _schedule_context(query))
    try:
        from system import foreground
        on_screen = foreground.activity_context()
        if on_screen:
            system_base += (f"CONTEXT (do not mention unless relevant): the user is "
                            f"currently looking at {on_screen}.\n")
    except Exception as e:                       # never break a turn over context
        log.debug("focus context unavailable: %s", e)

    # On a turn without the ACTION tools, look_up is still available (see the
    # payload below) so the model can always pull current/local facts instead of
    # punting. It must NOT pretend to do the action-only things, though — observed
    # live, with tools off it still said "That function's on screen, sir." and
    # "I'll open Google Maps for you", describing actions it had no mechanism for.
    _NO_TOOLS_NOTE = (
        "\nThe ONLY tool you have this turn is look_up (a web search that returns "
        "text). You cannot open, run, play, or display anything, and nothing is "
        "'on screen' — never imply such an action happened; if asked for one, say "
        "plainly you can't do that one. But whenever answering needs current or "
        "local facts you don't already know, CALL look_up and answer from what it "
        "returns. NEVER tell the user to look it up or ask whether you should.\n")

    messages = [{"role": "system",
                 "content": system_base + ("" if use_tools else _NO_TOOLS_NOTE)},
                *history, {"role": "user", "content": query}]
    health_called = False
    procs_called = False
    corrected = False
    escalated = False        # one-shot: an "I'd have to look it up" punt -> retry with look_up

    for round_i in range(MAX_TOOL_ROUNDS):
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": 1000,
            "temperature": 0.7,
        }
        if use_tools:
            payload["tools"] = tools.SCHEMAS          # full set (actions + look_up)
            payload["tool_choice"] = "auto"
        else:
            payload["tools"] = _info_schemas()        # look_up only — always available
            payload["tool_choice"] = "auto"
        data = await _post(payload)
        msg = data["choices"][0]["message"]
        calls = msg.get("tool_calls") or []

        if not calls:
            text = msg.get("content") or ""

            # It "called" a tool that doesn't exist (e.g. <brave_search={...}>).
            # Tell it what it actually has and let it try again, rather than
            # speaking the invented markup.
            invented = _invented_tool_name(text)
            if invented and not corrected:
                corrected = True
                log.warning("model invented a tool %r — steering it to the real ones",
                            invented)
                messages.append({"role": "user", "content":
                                 f"There is no tool called '{invented}'. Your available "
                                 f"tools are: {', '.join(tools.REGISTRY)}. To search the "
                                 f"web or open a browser, call web_search (or open_url). "
                                 f"Never write a tool call as text. Try again."})
                continue

            # The model wrote a tool call into its prose instead of calling it.
            # Recover it — otherwise we'd speak markup and do nothing.
            textual = _extract_text_tool_call(text)
            if textual is not None:
                name, args = textual
                log.warning("recovered malformed text tool call: %s(%s)", name, args)
                if name == "get_system_health":
                    health_called = True
                if name == "get_top_processes":
                    procs_called = True
                result = tools.dispatch(name, args)
                if isinstance(result, tools.PendingConfirmation):
                    approved = await confirm_cb(result.description) if confirm_cb else False
                    if approved:
                        log.info("TOOL CONFIRMED: %s", result.description)
                        result = tools.dispatch(result.tool, result.args, confirmed=True)
                    else:
                        log.info("TOOL DECLINED: %s — ending the turn", result.description)
                        return          # decline ends the turn (no re-ask loop)
                log.info("TOOL RESULT %s -> %s", name, str(result)[:300])
                call_id = f"recovered_{round_i}"
                messages.append({"role": "assistant", "content": "", "tool_calls": [{
                    "id": call_id, "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)}}]})
                messages.append({"role": "tool", "tool_call_id": call_id, "name": name,
                                 "content": json.dumps(result)[:6000]})
                continue

            # It quoted machine stats without ever measuring them. Measure them
            # OURSELVES and hand it the real numbers.
            # (Forcing a tool_choice here used to make the 8B model emit a
            # malformed call -> Groq 400 tool_use_failed -> retries -> 429 -> the
            # whole turn died with "something went wrong".)
            # Process question answered WITHOUT measuring -> inject the real
            # aggregated top-processes and regenerate. (The 70b model happily
            # says "Chrome is using 410 MB" from thin air; this grounds it.)
            if is_proc_q and not procs_called and (
                    _PROCESS_CLAIM.search(text) or not text.strip()):
                procs_called = True
                by = "cpu" if re.search(r"\bcpu\b", query, re.I) else "memory"
                real = tools.dispatch("get_top_processes", {"by": by, "limit": 5})
                if tool_sink is not None:
                    tool_sink.append({"name": "get_top_processes",
                                      "args": {"by": by}, "result": json.dumps(real)[:2000]})
                log.warning("process question answered without measuring — injecting "
                            "real top processes")
                messages.append({"role": "user", "content":
                                 "Do not name process memory or CPU figures from memory. "
                                 "Here are the REAL top applications right now (already "
                                 "aggregated per app):\n" + json.dumps(real) +
                                 "\nAnswer using ONLY this, in character, one or two short "
                                 "sentences — name the top app and its number."})
                continue

            if _looks_invented(text) and not health_called:
                health_called = True
                real = tools.dispatch("get_system_health", {})
                log.warning("model invented system stats (%r) — replacing them with "
                            "the real ones: %s", text[:100], real)
                messages.append({"role": "user", "content":
                                 "You just stated system statistics from memory. Never do "
                                 "that. Here are the REAL measured values right now:\n"
                                 f"{json.dumps(real)}\n"
                                 "Answer again using ONLY these numbers, in character, "
                                 "in one or two short spoken sentences."})
                continue          # regenerate the answer from the real numbers

            # AUTO-LOOKUP: on a tool-free turn the model sometimes punts —
            # "I'd have to look that up", "I don't have real-time access" —
            # instead of answering. The user never wants that hand-off; if the
            # question needs current/local facts, just look it up. Re-run the turn
            # WITH tools and tell it to call look_up now. One shot (escalated),
            # so it can't loop, and only when the needs_tools gate missed it.
            if not use_tools and not escalated and _LOOKUP_PUNT.search(text):
                escalated = True
                use_tools = True
                messages[0]["content"] = system_base      # drop the "no tools" note
                messages.append({"role": "user", "content":
                                 "Don't tell me to look it up or ask whether you should "
                                 "— just do it. Call look_up now with a good search "
                                 "query and answer from what it returns."})
                log.info("auto-lookup: model punted (%r) — retrying with look_up",
                         text[:80])
                continue

            # FINAL GUARD: nothing tool-call-shaped ever reaches TTS/the HUD,
            # whatever syntax the model dreamed up.
            clean, had_markup = _strip_tool_markup(text)
            if had_markup:
                log.error("suppressed tool-call markup in reply: %r", text[:200])
                if not clean:
                    yield "Couldn't do that one, sir."
                    return
            # Code is DISPLAYED, never spoken. The prompt asks for this, but a
            # prompt is not a guarantee — reading a function aloud, punctuation
            # and all, is bad enough to be worth enforcing in code. Fenced blocks
            # go to display_sink for the HUD; only the prose is yielded to TTS.
            clean, blocks = _split_code(clean)
            if blocks and display_sink is not None:
                display_sink.extend(blocks)
            if blocks and not clean.strip():
                # Nothing but code — say something rather than going silent.
                yield "That's on screen, sir."
                return
            for s in _sentences(clean):
                yield s
            return

        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": calls})

        for call in calls:
            name = call["function"]["name"]
            if name == "get_system_health":
                health_called = True
            if name == "get_top_processes":
                procs_called = True
            raw_args = call["function"].get("arguments")
            try:
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):     # Groq sends "null" for no-arg tools
                args = {}
            log.info("TOOL CALL %s(%s)", name, args)
            if on_tool:
                try:
                    on_tool(name)
                except Exception:
                    pass

            result = tools.dispatch(name, args)

            # Destructive -> ask the user out loud, then run only if approved.
            if isinstance(result, tools.PendingConfirmation):
                approved = False
                if confirm_cb is not None:
                    approved = await confirm_cb(result.description)
                if approved:
                    log.info("TOOL CONFIRMED: %s", result.description)
                    result = tools.dispatch(result.tool, result.args, confirmed=True)
                else:
                    # DECLINE ends the turn immediately. Feeding "cancelled" back
                    # let the model RE-ASK for the same destructive action in a
                    # loop (the shutdown near-miss). confirm_cb already spoke the
                    # cancellation, so just stop.
                    log.info("TOOL DECLINED: %s — ending the turn", result.description)
                    return

            log.info("TOOL RESULT %s -> %s", name, str(result)[:300])
            if tool_sink is not None:
                tool_sink.append({"name": name, "args": args,
                                  "result": json.dumps(result)[:2000]})
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "name": name,
                "content": json.dumps(result)[:6000],
            })

    yield "I wasn't able to finish that one, sir."


async def stream_reply(query: str, history: list[dict], confirm_cb=None) -> AsyncIterator[str]:
    """Back-compat alias used by main.py."""
    async for s in respond(query, history, confirm_cb):
        yield s


async def phrase(instruction: str, facts: str) -> str:
    """Turn REAL data into one in-character spoken line (no tools, no invention).

    Used by routines with say_dynamic: the numbers come from a real function, the
    model only supplies the delivery.
    """
    data = await _post({
        "model": config.SMART_MODEL_FAST,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content":
                f"{instruction}\n\nUse ONLY these real measured values — do not invent "
                f"or alter any number:\n{facts}\n\nReply with one or two short spoken "
                f"sentences, in character."},
        ],
        "max_tokens": 150,
        "temperature": 0.6,
    })
    return (data["choices"][0]["message"]["content"] or "").strip()


# ── Switching back to Anthropic Claude ──────────────────────────
# This module was originally built on Claude via the `anthropic` SDK before being
# ported to Groq's OpenAI-compatible API. To switch back: add ANTHROPIC_API_KEY
# to .env, set SMART_MODEL_FAST / SMART_MODEL_DEEP to Claude model IDs, and
# reimplement respond() against anthropic.AsyncAnthropic(...).messages.stream(),
# yielding sentence-by-sentence off _SENTENCE_END. The availability gate in
# main.py (config.GROQ_API_KEY / api.groq.com) would move back to the Anthropic
# equivalents. The tool-calling plumbing above is API-agnostic in shape.
