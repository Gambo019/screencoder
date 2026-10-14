"""Spot transcript lines with nothing to answer: hesitation sounds, acknowledgements, pleasantries, empty text."""
from __future__ import annotations

import re

# Hesitation sounds in any length: hmm, hmmmm, mm, mmhmm, uh, uhh, um, umm, er, erm, ah, oh, eh, huh, uhhuh, haha.
_HESITATION = re.compile(
    r"^(?:h+m+|m+(?:h+m+)*|u+h+|u+m+|e+r+m*|a+h+|o+h+|e+h+|h+u+h+|u+h+u+h+|a+h+a+|h+a+(?:h+a+)*|h+e+(?:h+e+)*)$"
)
# A line made only of these words is small talk, not a question. Deliberately excludes question words
# (what, why, how, is, can...) so short real questions like "Why?" or "Is it?" are kept.
_FILLER_WORDS = frozenset(
    """
    mhm hmph ooh oops whoa wow yikes ahem
    yeah yea ya yep yup yes no nope nah ok okay k kk right alright sure cool nice great good fine fair
    perfect awesome exactly indeed true totally absolutely definitely interesting gotcha understood noted
    agreed makes sense got it i see
    so well like and but or then anyway anyways actually basically just now also
    hi hello hey bye goodbye thanks thank you sorry please welcome cheers
    let me think hold on wait one second sec moment mean know
    the a an
    """.split()
)
_WORD = re.compile(r"[a-z0-9']+")


def _squeeze(word: str, keep: int) -> str:
    """Shorten letter runs: _squeeze("okaaay", 1) == "okay", _squeeze("hmmmm", 2) == "hmm"."""
    return re.sub(r"(.)\1+", lambda m: m.group(1) * min(len(m.group(0)), keep), word)


def _is_filler_word(word: str) -> bool:
    word = word.strip("'")
    if not word or _HESITATION.match(word):
        return True
    return word in _FILLER_WORDS or _squeeze(word, 2) in _FILLER_WORDS or _squeeze(word, 1) in _FILLER_WORDS


def is_filler(text: str) -> bool:
    """True when `text` has nothing to answer: empty, punctuation only, or only filler words."""
    return all(_is_filler_word(w) for w in _WORD.findall(text.lower().replace(chr(0x2019), "'")))
