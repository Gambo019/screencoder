"""Heard speech that points at the screen: the interviewer pasted something, names a line, asks to scroll, or asks
for something to be done in their coding environment (change the language, press a button).

The AI developer sees the screen only through the screenshots someone takes, and cannot click anything there, so
these are the moments a person has to step in. main_window shows a notice for them on the remote page (and in
the status bar). Questions, coding and drawing requests are the developer's own business and are not cues.

Speech-to-text garbles the words ("I'll paste it on line" came through as "i'll based it on line" and "i'll
pace it on line borne"), so the patterns lean on the words that survive: "line" with a number, "on line" at the
end of a cut sentence, "scroll ... up", "language selector", "button there at the bottom".
"""
from __future__ import annotations

import re

_NUM = (r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
        r"sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fourty|fifty|sixty|seventy|eighty|ninety|"
        r"hundred)")

LINE, PASTED, SCREEN, ACTION = "line", "pasted", "screen", "action"
# What each kind of cue means for the person who can help (shown as the notice's title).
TITLES = {
    LINE: "The interviewer points at a line on the screen",
    PASTED: "The interviewer put something new on the screen",
    SCREEN: "The interviewer points at the screen",
    ACTION: "The interviewer asks for something to be done on the screen",
}

_CUES = (
    (LINE, re.compile(
        rf"\blines?\s+(?:number\s+|no\.?\s+|num\s+)?{_NUM}\b(?!\s+by\b)"   # "line forty five", "lines ten twelve"
        r"|\bline\s+number\b"                                               # "line number..."
        r"|\bon\s+lines?\s*$"                                               # "I'll paste it on line" (cut off)
        r"|\b(?:paste[ds]?|pace[ds]?|based|placed|put)\s+(?:it|this|that|them)\s+(?:on|at)\s+line\b", re.I)),
    (PASTED, re.compile(
        r"\b(?:paste|pasted|pasting)\b"
        r"|\b(?:i'?ve|i have|i'?ll|i will|i just|let me)\s+(?:just\s+)?(?:put|added|add|typed|type|written|write|"
        r"dropped|drop|shared|share|updated|update|uploaded|upload|changed|change)\b[^.?!]{0,50}?"
        r"\b(?:editor|screen|chat|pad|document|doc|tab|link|below|above|there)\b", re.I)),
    (SCREEN, re.compile(
        r"\bscroll(?:ing)?\s+(?:(?:the|a)\s+(?:bit|little)\s+|a\s+bit\s+|back\s+)?(?:up|down|to)\b"
        r"|\bscroll(?:ing)?\s+(?:the|a)\s+(?:bit|little)\b"
        r"|\b(?:on|at)\s+(?:your|the|my)\s+screen\b"
        r"|\bin\s+the\s+(?:editor|chat|code\s?pad|coding\s+environment)\b"
        r"|\b(?:look|see)\s+(?:at|on)\s+(?:the\s+)?(?:screen|editor)\b", re.I)),
    (ACTION, re.compile(
        r"\b(?:change|switch|select|set|pick)\s+the\s+language\b"
        r"|\blanguage\s+(?:selector|dropdown|drop-down|drop\s+down|menu)\b"
        r"|\b(?:click|press|hit)\b[^.?!]{0,30}?\b(?:button|icon|tab|link)\b"
        r"|\bbutton\s+(?:there|here|at|on)\b|\b(?:run|submit|reset)\s+button\b"
        r"|\b(?:top|bottom)\s+(?:right|left|cent(?:er|re))\b"
        r"|\bshare\s+your\s+screen\b|\brefresh\s+(?:the\s+)?page\b", re.I)),
)


def screen_cue(text: str) -> str | None:
    """The kind of cue (LINE, PASTED, SCREEN, ACTION) in a heard sentence, or None. The first kind that
    matches wins, in that order: a named line says most about where to look."""
    if not text:
        return None
    for kind, pattern in _CUES:
        if pattern.search(text):
            return kind
    return None
