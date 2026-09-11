#!/usr/bin/env python3
"""Transliterate English into Thai script, phonetically.

The curated lexicon in ``thai_lexicon.yaml`` is authoritative and always wins:
convention beats phonetics for terms people already say a particular way. This
is what runs when a word is not in it, so an unknown English word is spoken as
Thai rather than left for the model to guess at.

Two sources of pronunciation, tried in order:

1. **CMUdict** — 126,000 English words with real phoneme sequences. Correct by
   construction for anything in it.
2. **Spelling rules** — for what is not, which is most of the technical
   vocabulary that matters here: kubernetes, proxmox and virtualization are all
   absent from CMUdict. Less accurate, and better than nothing.

The hard part is not the phoneme table but Thai orthography: vowels are written
before, after, above or below their consonant, so a syllable has to be
assembled rather than concatenated. Each vowel therefore carries a prefix and a
suffix, and the consonant goes between them.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import List, Optional, Tuple

# --- ARPAbet consonants -----------------------------------------------------
# Thai has no /v/, /z/, /th/ or /sh/; these are the substitutions Thai speakers
# actually make, not the closest articulatory match.
# Tuned against the curated lexicon rather than chosen from a chart: comparing
# generated output with 257 hand-checked words showed /p/ written as พ 16 times
# where convention uses ป, /t/ as ท 11 times where it should be ต, and /s/ as ส
# 20 times where a loanword takes ซ.
_ONSET = {
    "B": "บ", "CH": "ช", "D": "ด", "DH": "ด", "F": "ฟ", "G": "ก", "HH": "ฮ",
    "JH": "จ", "K": "ค", "L": "ล", "M": "ม", "N": "น", "NG": "ง", "P": "ป",
    "R": "ร", "S": "ซ", "SH": "ช", "T": "ต", "TH": "ธ", "V": "ว", "W": "ว",
    "Y": "ย", "Z": "ซ", "ZH": "ช",
}
# ...except in a cluster, where /s/ is ส: เซิร์ฟเวอร์ but สตอเรจ, สต็อป.
_ONSET_CLUSTER = dict(_ONSET, S="ส")

# Thai allows only these eight final consonants. Everything else is mapped to
# the one it is heard as, which is why "tech" ends in ค and "cache" in ช→ด.
_CODA = {
    "B": "บ", "CH": "ช", "D": "ด", "DH": "ด", "F": "ฟ", "G": "ก", "JH": "จ",
    "K": "ก", "L": "ล", "M": "ม", "N": "น", "NG": "ง", "P": "ป", "R": "ร",
    "S": "ส", "SH": "ช", "T": "ต", "TH": "ธ", "V": "ฟ", "Z": "ส", "ZH": "ช",
    "HH": "", "W": "ว", "Y": "ย",
}

# (before the consonant, after it) — Thai writes vowels in four positions.
_VOWEL: dict[str, Tuple[str, str]] = {
    "AA": ("", "า"),   "AE": ("แ", ""),   "AH": ("", "ั"),  "AO": ("", "อ"),
    "AW": ("เ", "า"),  "AY": ("ไ", ""),   "EH": ("เ", "็"),  "ER": ("เ", "อร์"),
    "EY": ("เ", ""),   "IH": ("", "ิ"),   "IY": ("", "ี"),   "OW": ("โ", ""),
    "OY": ("", "อย"),  "UH": ("", "ุ"),   "UW": ("", "ู"),
}
# An open syllable takes the long form; ั needs a final consonant to sit on.
_VOWEL_OPEN = {"AH": ("", "ะ"), "EH": ("เ", "ะ")}
# English /ɑ/ before a final consonant is heard as Thai "็อ", not า:
# stop -> สต็อป, shop -> ช็อป. Open, it stays า: data -> ดาต้า.
_VOWEL_CLOSED = {"AA": ("", "็อ")}

_VOWELS = set(_VOWEL)


def _syllabify(phones: List[str]) -> List[Tuple[List[str], str, List[str]]]:
    """Split a phoneme run into (onset, vowel, coda) groups.

    Consonants attach forward to the next vowel; whatever is left before the
    following vowel closes the current syllable. Crude next to real English
    syllabification, and enough for transliteration, where the goal is a
    pronounceable Thai approximation rather than a linguistic analysis.
    """
    out: List[Tuple[List[str], str, List[str]]] = []
    onset: List[str] = []
    i = 0
    while i < len(phones):
        p = phones[i]
        if p in _VOWELS:
            coda: List[str] = []
            j = i + 1
            # Consonants up to the last one before the next vowel are this
            # syllable's coda; the last becomes the next syllable's onset.
            run = []
            while j < len(phones) and phones[j] not in _VOWELS:
                run.append(phones[j]); j += 1
            if j < len(phones):          # another vowel follows
                coda, onset_next = run[:-1], run[-1:]
            else:                         # end of word: all of it is coda
                coda, onset_next = run, []
            out.append((onset, p, coda))
            onset = onset_next
            i = j
        else:
            onset.append(p)
            i += 1
    if onset:                             # trailing consonants with no vowel
        out.append((onset, "", []))
    return out


def _render(onset: List[str], vowel: str, coda: List[str]) -> str:
    table = _ONSET_CLUSTER if len(onset) > 1 else _ONSET
    cons = "".join(table.get(p, "") for p in onset) or "อ"
    if not vowel:
        # A consonant with no vowel: Thai needs a carrier, and อะ is the
        # neutral one speakers insert.
        return "".join(_ONSET.get(p, "") for p in onset) + "ะ"
    if coda and vowel in _VOWEL_CLOSED:
        pre, post = _VOWEL_CLOSED[vowel]
    elif not coda and vowel in _VOWEL_OPEN:
        pre, post = _VOWEL_OPEN[vowel]
    else:
        pre, post = _VOWEL[vowel]
    tail = "".join(_CODA.get(p, "") for p in coda)
    return pre + cons + post + tail


def from_phonemes(phones: List[str]) -> str:
    stripped = [re.sub(r"\d", "", p) for p in phones]
    return "".join(_render(o, v, c) for o, v, c in _syllabify(stripped))


# --- spelling fallback ------------------------------------------------------
# Ordered longest-first; the first match at each position wins.
_SPELL = [
    ("tion", ["SH", "AH", "N"]), ("sion", ["ZH", "AH", "N"]),
    ("ture", ["CH", "ER"]),      ("ough", ["AH", "F"]),
    ("ing",  ["IH", "NG"]),      ("ck",   ["K"]),    ("ch", ["CH"]),
    ("sh",   ["SH"]),            ("th",   ["TH"]),   ("ph", ["F"]),
    ("qu",   ["K", "W"]),        ("ee",   ["IY"]),   ("ea", ["IY"]),
    ("oo",   ["UW"]),            ("ou",   ["AW"]),   ("ow", ["AW"]),
    ("ai",   ["EY"]),            ("ay",   ["EY"]),   ("oa", ["OW"]),
    ("er",   ["ER"]),            ("or",   ["AO", "R"]), ("ar", ["AA", "R"]),
    ("ur",   ["ER"]),            ("ir",   ["ER"]),
    ("a", ["AE"]), ("b", ["B"]), ("c", ["K"]), ("d", ["D"]), ("e", ["EH"]),
    ("f", ["F"]), ("g", ["G"]), ("h", ["HH"]), ("i", ["IH"]), ("j", ["JH"]),
    ("k", ["K"]), ("l", ["L"]), ("m", ["M"]), ("n", ["N"]), ("o", ["AA"]),
    ("p", ["P"]), ("q", ["K"]), ("r", ["R"]), ("s", ["S"]), ("t", ["T"]),
    ("u", ["AH"]), ("v", ["V"]), ("w", ["W"]), ("x", ["K", "S"]),
    ("y", ["IY"]), ("z", ["Z"]),
]


def _phones_from_spelling(word: str) -> List[str]:
    w = word.lower()
    # A silent final e lengthens the vowel before it rather than sounding.
    if len(w) > 3 and w.endswith("e") and w[-2] not in "aeiou":
        w = w[:-1]
    out: List[str] = []
    i = 0
    while i < len(w):
        for pat, ph in _SPELL:
            if w.startswith(pat, i):
                out += ph
                i += len(pat)
                break
        else:
            i += 1
    return out


@lru_cache(maxsize=1)
def _cmudict() -> dict:
    try:
        import cmudict
        return cmudict.dict()
    except Exception:
        return {}


@lru_cache(maxsize=8192)
def transliterate(word: str, allow_spelling: bool = False) -> Optional[str]:
    """English word -> Thai script, or None when there is no pronunciation.

    The spelling fallback is off by default, and that is the whole judgement
    here. Measured against the curated lexicon, CMUdict-backed output averages
    a recognisable approximation; spelling-guessed output does not — Wireshark
    became เวอร์เอะช็อรก, Grafana แกรแฟแน. Those are worse than leaving the
    English in place, because the model makes a passable attempt at a word it
    can see, and none at all at Thai that spells something else.

    So an unknown proper noun stays English rather than becoming wrong Thai.
    The curated lexicon is how such a word gets said properly; this only covers
    ordinary English vocabulary, where a pronunciation is actually known.
    """
    w = re.sub(r"[^A-Za-z]", "", word).lower()
    if not w:
        return None
    prons = _cmudict().get(w)
    if not prons:
        if not allow_spelling:
            return None
        return from_phonemes(_phones_from_spelling(w)) or None
    return from_phonemes(prons[0]) or None


def source(word: str) -> str:
    """Which path a word takes — for measuring how much rests on the guesses."""
    w = re.sub(r"[^A-Za-z]", "", word).lower()
    return "cmudict" if _cmudict().get(w) else "spelling"


if __name__ == "__main__":
    import sys
    words = sys.argv[1:] or [
        "docker", "container", "kubernetes", "proxmox", "virtualization",
        "server", "storage", "network", "security", "performance",
    ]
    for w in words:
        print(f"  {w:18s} {source(w):9s} → {transliterate(w)}")
