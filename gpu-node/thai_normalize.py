#!/usr/bin/env python3
"""Rewrite text into something a Thai TTS model reads correctly.

F5-TTS-TH is already trained on Thai; it does not need more training to say
"สิบสองกิโลเมตร". What it needs is to be handed Thai words instead of glyphs it
has never had to voice. Measured against the deployed model, feeding it raw
text produces:

    23:45 น.        -> "สองพันสามร้อยสี่สิบห้า"   (the colon vanishes)
    36.5 องศา       -> "สามร้อยหกสิบห้าองศา"      (the decimal point vanishes)
    85%             -> "แปดสิบห้า"                 (the percent sign vanishes)
    12 กม.          -> "สิบสองกลม"                 (the abbreviation is read as a word)
    10/09/2026      -> "สิบล้านเก้าพัน…"           (the slashes vanish)
    GPU node        -> "ดิวโน้ด"

Every one of those is a spelling problem, not a pronunciation problem, and
every one is fixed by writing the number or word out in Thai before synthesis.

Two layers:

- **Rules** (this file) handle the infinite cases — any number, time, date,
  decimal, percentage, currency. A dictionary cannot enumerate those.
- **Lexicon** (``thai_lexicon.yaml``) handles the finite ones — abbreviations
  and loanwords, where the only way to know is to be told. Add entries there,
  not here.

Order matters: the longest patterns run first, so ``23:45`` is read as a time
before ``23`` is read as a number.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Tuple

# ---------------------------------------------------------------- numbers

_DIGITS = ["ศูนย์", "หนึ่ง", "สอง", "สาม", "สี่", "ห้า", "หก", "เจ็ด", "แปด", "เก้า"]
_PLACES = ["", "สิบ", "ร้อย", "พัน", "หมื่น", "แสน"]
_THAI_DIGIT_MAP = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")

# Letter names, for spelling out acronyms. "CYN" has no pronunciation as a
# word; it is three letters and has to be said as three letters.
_LETTERS = {
    "a": "เอ", "b": "บี", "c": "ซี", "d": "ดี", "e": "อี", "f": "เอฟ",
    "g": "จี", "h": "เอช", "i": "ไอ", "j": "เจ", "k": "เค", "l": "แอล",
    "m": "เอ็ม", "n": "เอ็น", "o": "โอ", "p": "พี", "q": "คิว", "r": "อาร์",
    "s": "เอส", "t": "ที", "u": "ยู", "v": "วี", "w": "ดับเบิลยู",
    "x": "เอ็กซ์", "y": "วาย", "z": "แซด",
}


def spell_latin(s: str) -> str:
    """Letter by letter in Thai: CYN -> ซีวายเอ็น."""
    return "".join(_LETTERS.get(c.lower(), c) for c in s)


def _under_million(n: int) -> str:
    """0-999999 in Thai, with the two irregularities the language insists on."""
    if n == 0:
        return _DIGITS[0]
    out: List[str] = []
    s = str(n)
    length = len(s)
    for i, ch in enumerate(s):
        d = int(ch)
        place = length - i - 1
        if d == 0:
            continue
        # Ten is สิบ, not หนึ่งสิบ; twenty is ยี่สิบ, not สองสิบ.
        if place == 1:
            out.append("สิบ" if d == 1 else ("ยี่สิบ" if d == 2 else _DIGITS[d] + "สิบ"))
        # A trailing 1 above nine is เอ็ด: 11 is สิบเอ็ด, 21 is ยี่สิบเอ็ด.
        elif place == 0 and d == 1 and length > 1:
            out.append("เอ็ด")
        else:
            out.append(_DIGITS[d] + _PLACES[place])
    return "".join(out)


def num_to_thai(n: int) -> str:
    """Any non-negative integer in Thai. Millions stack: ล้าน is a real place."""
    if n < 0:
        return "ลบ" + num_to_thai(-n)
    if n < 10 ** 6:
        return _under_million(n)
    head, tail = divmod(n, 10 ** 6)
    out = num_to_thai(head) + "ล้าน"
    return out + (_under_million(tail) if tail else "")


def _decimal_to_thai(whole: str, frac: str) -> str:
    """Fractional digits are read one by one — 36.5 is สามสิบหกจุดห้า."""
    digits = "".join(_DIGITS[int(d)] for d in frac)
    return f"{num_to_thai(int(whole or 0))}จุด{digits}"


def _digits_to_thai(s: str) -> str:
    return "".join(_DIGITS[int(d)] for d in s if d.isdigit())


# ---------------------------------------------------------------- lexicon

_DEFAULT_LEXICON = Path(__file__).with_name("thai_lexicon.yaml")

# Sound out English words the lexicon does not know. Off by default, and the
# measurement is why: read back through the STT, leaving English scores 15%
# against the correct Thai and auto-transliteration 28% — better on average,
# but it makes some words worse rather than better (container went from 82% to
# 43%), and a curated entry scores far above either. An inconsistent
# improvement is not one you switch on for everybody.
AUTO_TRANSLITERATE = os.environ.get("JARVIS_TTS_AUTO_TRANSLIT", "0") == "1"


def load_lexicon(path: Path | None = None) -> Dict[str, Dict[str, str]]:
    """Read the abbreviation / loanword tables. Missing file is not fatal."""
    import yaml

    p = path or _DEFAULT_LEXICON
    if not p.exists():
        return {"abbreviations": {}, "words": {}, "symbols": {}}
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {
        "abbreviations": dict(raw.get("abbreviations") or {}),
        "words": dict(raw.get("words") or {}),
        "symbols": dict(raw.get("symbols") or {}),
    }


class ThaiNormalizer:
    """Text in, speakable Thai out."""

    def __init__(self, lexicon: Dict[str, Dict[str, str]] | None = None) -> None:
        lex = lexicon if lexicon is not None else load_lexicon()
        self.abbr = lex["abbreviations"]
        self.words = lex["words"]
        self.symbols = lex["symbols"]
        # Longest-first so "GPU node" wins over "node", and "กม." over "ก".
        self._word_re = self._compile(self.words)
        self._abbr_re = self._compile(self.abbr)

    @staticmethod
    def _compile(table: Dict[str, str]) -> re.Pattern | None:
        if not table:
            return None
        keys = sorted(table, key=len, reverse=True)
        parts = []
        for k in keys:
            esc = re.escape(k)
            if k.isascii():
                # Word boundaries so "no" doesn't fire inside "node".
                parts.append(rf"\b{esc}\b")
            elif k.endswith("."):
                # A Thai abbreviation ends at its period: `น.` is "นาฬิกา" in
                # "23:45 น." and is not there at all in "ชื่อต้น.นามสกุล", where
                # it matched mid-word and produced "ชื่อต้นาฬิกานามสกุล".
                parts.append(rf"{esc}(?![\u0E00-\u0E7F])")
            else:
                parts.append(esc)
        return re.compile("|".join(parts), re.IGNORECASE)

    # -- individual passes ------------------------------------------------

    # Agents keep emitting markdown even when told not to, and the model tries
    # to voice the punctuation. Strip it rather than rely on the prompt.
    _MD_BOLD = re.compile(r"\*{1,3}(.+?)\*{1,3}", re.S)
    _MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
    _MD_CODE = re.compile(r"`{1,3}([^`]*)`{1,3}", re.S)
    _MD_BULLET = re.compile(r"^[ \t]*[*\-+•]\s+", re.M)
    _MD_HEAD = re.compile(r"^#{1,6}\s*", re.M)
    _MD_RULE = re.compile(r"(?:^|\s)[-*_]{3,}(?=\s|$)", re.M)
    # Assistants emit LaTeX for arrows and symbols. Spoken, "$\rightarrow$"
    # becomes "ดอลลาร์ rightarrow ดอลลาร์": the dollars are read as currency and
    # the command as a word. The common relations get their Thai reading; the
    # rest of the markup goes.
    _TEX_WORDS = {
        r"\\rightarrow": " ถึง ", r"\\to": " ถึง ", r"\\leftarrow": " จาก ",
        r"\\times": " คูณ ", r"\\div": " หาร ", r"\\pm": " บวกลบ ",
        r"\\leq": " น้อยกว่าหรือเท่ากับ ", r"\\geq": " มากกว่าหรือเท่ากับ ",
        r"\\neq": " ไม่เท่ากับ ", r"\\approx": " ประมาณ ",
    }
    _TEX_MATH = re.compile(r"\$+([^$]*)\$+")
    _TEX_CMD = re.compile(r"\\[A-Za-z]+\s*")

    def _strip_latex(self, text: str) -> str:
        for pat, spoken in self._TEX_WORDS.items():
            text = re.sub(pat + r"\b", spoken, text)
        text = self._TEX_MATH.sub(r" \1 ", text)   # $x$ -> x
        return self._TEX_CMD.sub(" ", text)         # any command left over
    # Emoji reach the model as characters it has no reading for, and it either
    # voices something arbitrary or stumbles. Assistants emit them constantly.
    # Ranges rather than a list: new emoji are added to Unicode every year.
    _EMOJI = re.compile(
        "["
        "\U0001F000-\U0001FAFF"   # pictographs, emoticons, transport, symbols
        "\U00002600-\U000027BF"   # misc symbols + dingbats
        "\U0001F1E6-\U0001F1FF"   # regional indicators (flags)
        "\U0000FE00-\U0000FE0F"   # variation selectors
        "\U0000200D"               # zero-width joiner, for combined emoji
        "\U00002190-\U000021FF"   # arrows
        "\U00002B00-\U00002BFF"   # misc symbols and arrows
        "]+"
    )
    # "1." opening an item is a marker, not a number. Left alone it becomes
    # "หนึ่ง." and the trailing period splits the sentence there, so the reply
    # stops dead after every list number.
    # Markers can sit in front of the number — `### **1. หัวข้อ**` — so the
    # marker run is part of the pattern rather than something stripped first.
    # Without this the number keeps its period, and a period ends a sentence:
    # the voice stopped dead after every numbered heading.
    _MD_ORDERED = re.compile(r"(?:^|(?<=\s))([*_`#]*\s*)(\d{1,2})\.(?=\s)", re.M)

    # Thai does not put spaces between words; a space is a phrase break and is
    # read as a pause. Emphasis markers need room, so a writer types
    # `ผม "นึก" ว่า` — and stripping only the quotes leaves `ผม นึก ว่า`, spoken
    # with a pause on either side of the emphasised word.
    #
    # Only a SHORT span is closed up. A first attempt matched any marker between
    # Thai characters and ran sentences together: `เกินเหตุ **เอาล่ะครับ` became
    # `เกินเหตุเอาล่ะครับ`, because a marker opening a new sentence looks exactly
    # like one wrapping a word. Length is the signal that separates them — a
    # word or two is emphasis inside a phrase, a longer span is its own clause.
    _EMPH_SHORT = re.compile(
        r"(?<=[\u0E00-\u0E7F])\s*[*_`\"\u201c\u201d]+\s*"
        r"([\u0E00-\u0E7F]{1,12})"
        r"\s*[*_`\"\u201c\u201d]+\s*(?=[\u0E00-\u0E7F])")

    # A parenthesised English gloss after Thai — `ด้านราคาและความคุ้มค่า (Cost &
    # Licensing)` — exists for a reader scanning a page. Spoken, it says the
    # same thing twice in two languages and doubles the length of every heading.
    # Dropped entirely; the Thai beside it already carried the meaning.
    # Parentheses holding Thai, or numbers, are left alone.
    # Captures the Thai rather than looking behind it: a closing quote can sit
    # between the word and the bracket — `"เพ้อเจ้อ" (Verbose)` — and Python's
    # lookbehind is fixed-width, so it cannot step over one.
    _EN_GLOSS = re.compile(
        r"([\u0E00-\u0E7F])[\s\"'*`\u201c\u201d\u2018\u2019]*"
        r"\(\s*[A-Za-z][A-Za-z0-9 &/\-.'\u2019]*\)")

    def _strip_markdown(self, text: str) -> str:
        text = self._EMOJI.sub(" ", text)
        text = self._strip_latex(text)
        text = self._EMPH_SHORT.sub(r"\1", text)
        # After the markers are gone, not before: `"เพ้อเจ้อ" (Verbose)` has a
        # quote sitting between the Thai and the bracket, and the gloss rule
        # needs to see the Thai.
        text = self._EN_GLOSS.sub(r"\1", text)
        text = self._MD_RULE.sub(" ", text)
        text = self._MD_ORDERED.sub(lambda m: f"ข้อ{num_to_thai(int(m.group(2)))}", text)
        text = self._MD_LINK.sub(r"\1", text)
        text = self._MD_CODE.sub(r"\1", text)
        text = self._MD_BULLET.sub("", text)
        text = self._MD_HEAD.sub("", text)
        text = self._MD_BOLD.sub(r"\1", text)
        text = text.replace("_", " ")
        # Colons become pauses — except between digits, where the colon is a
        # clock and removing it here would leave _times nothing to match.
        text = re.sub(r"(?<!\d):(?!\d\d)", " ", text)
        text = re.sub(r"[\u2013\u2014]", " ", text)
        # An ellipsis is a pause in writing and a stumble when read aloud.
        text = re.sub(r"\.{2,}|\u2026", " ", text)
        text = re.sub(r"[()\[\]{}]", " ", text)
        # Unmatched markers survive the pair-matching passes above; a stray "**"
        # is voiced as noise, so sweep whatever is left.
        text = re.sub(r"[*`#]+", " ", text)
        # Quotation marks are typography, not speech.
        text = re.sub(r"[\"\u201c\u201d\u2018\u2019]", " ", text)
        # "A/B" is a choice; "10/09" is a date and "co/th" a path. Only the
        # letter-flanked case reads as "or".
        # "A/B" is a choice. Between Thai it takes no spaces — adding them
        # would put a pause on each side of a word that is read as one phrase.
        text = re.sub(r"(?<=[\u0E00-\u0E7F])\s*/\s*(?=[\u0E00-\u0E7F])", "หรือ", text)
        text = re.sub(r"(?<=[A-Za-z])\s*/\s*(?=[A-Za-z])", " หรือ ", text)
        return text

    # Thai numbers: 02-437-1210, 081-234-5678, 0812345678, +66 81 234 5678.
    # A phone number is an identifier — "สี่ร้อยสามสิบเจ็ด" for the middle
    # block is meaningless, and the leading zero disappears entirely.
    _PHONE = re.compile(r"(?<![\d])(?:\+66[\s-]?)?0?\d(?:[\s-]?\d){7,9}(?![\d])")

    def _phones(self, text: str) -> str:
        def repl(m: re.Match) -> str:
            raw = m.group(0)
            digits = re.sub(r"\D", "", raw)
            if not 9 <= len(digits) <= 12:
                return raw
            prefix = "บวกหกหก" if raw.strip().startswith("+66") else ""
            return prefix + _digits_to_thai(digits[2:] if prefix else digits)
        return self._PHONE.sub(repl, text)

    _TLD = {
        "co.th": "ดอทซีโอดอททีเอช", "ac.th": "ดอทเอซีดอททีเอช",
        "go.th": "ดอทจีโอดอททีเอช", "or.th": "ดอทโออาร์ดอททีเอช",
        "in.th": "ดอทไอเอ็นดอททีเอช",
        "com": "ดอทคอม", "net": "ดอทเน็ต", "org": "ดอทออร์ก",
        "io": "ดอทไอโอ", "ai": "ดอทเอไอ", "dev": "ดอทเดฟ", "th": "ดอททีเอช",
    }
    _EMAIL = re.compile(r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z.]{2,})\b")

    def _spell_domain(self, domain: str) -> str:
        """cyn.co.th -> ซีวายเอ็น ดอทซีโอดอททีเอช."""
        for tld, spoken in sorted(self._TLD.items(), key=lambda kv: -len(kv[0])):
            if domain.lower().endswith("." + tld):
                head = domain[: -(len(tld) + 1)]
                return self._spell_token(head) + " " + spoken
        return " ดอท ".join(self._spell_token(part) for part in domain.split("."))

    @staticmethod
    def _transliterate(token: str) -> Optional[str]:
        """Phonetic fallback for a word the lexicon has never seen."""
        try:
            from en_to_thai import transliterate
            return transliterate(token)
        except Exception:
            return None   # the module is optional; raw text still speaks

    def _spell_token(self, token: str) -> str:
        """A word if the lexicon knows it, else its parts, else letter by letter.

        Handles and local parts are routinely two words run together —
        ``cyngroup``, ``salesupport``. Spelling those out one letter at a time
        is technically correct and unlistenable, so try to segment first.
        """
        low = token.lower()
        table = {k.lower(): v for k, v in self.words.items()}
        if low in table:
            return table[low]
        parts = self._segment(low, table)
        if parts:
            return "".join(parts)
        # Letter-by-letter suits an acronym and ruins a word: "CYN" is three
        # letters, but "example" spelled out is อีเอ็กซ์เอเอ็มพีแอลอี. Past four
        # characters, hand it to the model — a rough attempt at a word beats a
        # perfect recitation of its spelling.
        if len(token) <= 4:
            return spell_latin(token)
        # Longer than an acronym and unknown. Sounding it out is opt-in for the
        # same reason it is everywhere else: measured against correct Thai it
        # helps on average and hurts on particular words.
        if AUTO_TRANSLITERATE:
            return self._transliterate(token) or token
        return token

    @staticmethod
    def _segment(token: str, table: Dict[str, str]) -> List[str] | None:
        """Split into known words, or None if the whole token isn't covered.

        Backtracks rather than committing to the longest first match: greedy
        takes "sales" out of "salesupport" and then cannot place "upport", even
        though "sale" + "support" covers it exactly.

        Only a full cover counts — a leftover fragment would have to be spelled
        out, which reads worse than spelling the whole token consistently.
        Pieces under three letters are ignored, or every handle dissolves into
        a stream of one-syllable matches.
        """
        keys = sorted((k for k in table if len(k) >= 3), key=len, reverse=True)

        def walk(i: int, depth: int) -> List[str] | None:
            if i == len(token):
                return []
            if depth > 6:          # a handle is not made of seven words
                return None
            for k in keys:
                if token.startswith(k, i):
                    rest = walk(i + len(k), depth + 1)
                    if rest is not None:
                        return [table[k]] + rest
            return None

        return walk(0, 0) or None

    # A bare domain outside an address — `spm-plape.com` quoted in prose — is
    # still spelled out, or the model reads it as an English word.
    _BARE_DOMAIN = re.compile(r"\b([A-Za-z0-9][A-Za-z0-9.-]*)\.([A-Za-z]{2,3}(?:\.[a-z]{2})?)\b")

    def _domains(self, text: str) -> str:
        def repl(m: re.Match) -> str:
            full = m.group(0)
            if "@" in full or full.lower().endswith((".v",)):
                return full
            tld = m.group(2).lower()
            if tld not in self._TLD and tld not in {"com", "net", "org", "th", "io"}:
                return full
            return self._spell_domain(full)
        return self._BARE_DOMAIN.sub(repl, text)

    def _emails(self, text: str) -> str:
        return self._EMAIL.sub(
            lambda m: f"{self._spell_token(m.group(1))} แอท {self._spell_domain(m.group(2))}",
            text)

    # A bare @handle is a Line ID or a social account, not an email.
    _HANDLE = re.compile(r"(?<![\w@])@([A-Za-z][A-Za-z0-9._-]{1,30})\b")

    def _handles(self, text: str) -> str:
        return self._HANDLE.sub(lambda m: "แอท " + self._spell_token(m.group(1)), text)

    # An IP address is four numbers and three dots, and every other rule here
    # wants a piece of it: the decimal rule claims "192.168", the number rule
    # claims each octet separately, and what comes out is
    # "หนึ่งร้อยเก้าสิบสองจุดหนึ่งหกแปด.หนึ่งร้อยเอ็ดจุดศูนย์" — with a literal dot
    # left standing. Handled whole, and first.
    #
    # Octets are read digit by digit, the way network people say them: an
    # address is an identifier, not four quantities.
    _IP = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?:/(\d{1,2}))?\b")

    def _ips(self, text: str) -> str:
        def repl(m: re.Match) -> str:
            octets = m.group(1, 2, 3, 4)
            if any(int(o) > 255 for o in octets):
                return m.group(0)          # not an address; leave it alone
            spoken = "จุด".join(_digits_to_thai(o) for o in octets)
            if m.group(5):
                spoken += " ทับ " + num_to_thai(int(m.group(5)))
            return spoken
        return self._IP.sub(repl, text)

    # "v9", "v2.1" — a version, not a word. A rule rather than lexicon entries
    # because version numbers are unbounded. The digit requirement keeps it off
    # names that merely start with v, like vSphere.
    _VERSION = re.compile(r"\b[vV](\d+(?:\.\d+)*)\b")

    def _versions(self, text: str) -> str:
        def repl(m: re.Match) -> str:
            n = m.group(1)
            spoken = (_decimal_to_thai(*n.split(".", 1)) if "." in n
                      else num_to_thai(int(n)))
            # "เวอร์ชัน v2.1" already says the word; adding it again reads as
            # "version version two point one".
            before = text[: m.start()].rstrip()
            if before.endswith(("เวอร์ชัน", "รุ่น", "version", "Version")):
                return spoken
            return "เวอร์ชัน" + spoken
        return self._VERSION.sub(repl, text)

    # Anything left in Latin after the lexicon and the acronym pass. Off by
    # default — see AUTO_TRANSLITERATE. Five characters or more, so acronyms and
    # short tokens keep their own handling.
    _LATIN_WORD = re.compile(r"\b[A-Za-z][A-Za-z'\u2019-]{4,}\b")

    def _latin_words(self, text: str) -> str:
        known = {k.lower() for k in self.words}
        def repl(m: re.Match) -> str:
            w = m.group(0)
            if w.lower() in known:
                return w          # the lexicon pass already had its chance
            return self._transliterate(w) or w
        return self._LATIN_WORD.sub(repl, text)

    # A run of capitals is an acronym, not a word: CYN, GPU, ID, API.
    _ACRONYM = re.compile(r"\b[A-Z]{2,6}\b")

    def _acronyms(self, text: str) -> str:
        known = {k.lower() for k in self.words}
        return self._ACRONYM.sub(
            lambda m: m.group(0) if m.group(0).lower() in known else spell_latin(m.group(0)),
            text)

    def _times(self, text: str) -> str:
        """23:45 -> ยี่สิบสามนาฬิกาสี่สิบห้านาที.

        Consumes a trailing "น." itself. Left to the abbreviation pass it would
        expand to a second "นาฬิกา" and the clock would be read twice.
        """
        def repl(m: re.Match) -> str:
            h, mi = int(m.group(1)), int(m.group(2))
            if not (0 <= h <= 23 and 0 <= mi <= 59):
                return m.group(0)
            if mi == 0:
                return f"{num_to_thai(h)}นาฬิกา"
            return f"{num_to_thai(h)}นาฬิกา{num_to_thai(mi)}นาที"
        return re.sub(r"\b(\d{1,2}):(\d{2})\b(?:\s*น\.)?", repl, text)

    def _dates(self, text: str) -> str:
        def repl(m: re.Match) -> str:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if not (1 <= d <= 31 and 1 <= mo <= 12):
                return m.group(0)
            months = ["", "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม",
                      "มิถุนายน", "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม",
                      "พฤศจิกายน", "ธันวาคม"]
            # The writer usually supplies "วันที่" already; adding another
            # produces "วันที่ วันที่สิบ".
            prefix = text[: m.start()].rstrip()
            lead = "" if prefix.endswith("วันที่") else "วันที่"
            return f"{lead}{num_to_thai(d)} {months[mo]} {num_to_thai(y)}"
        return re.sub(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b", repl, text)

    def _percent(self, text: str) -> str:
        return re.sub(r"(\d+(?:\.\d+)?)\s*%",
                      lambda m: self._number(m.group(1)) + "เปอร์เซ็นต์", text)

    def _number(self, s: str) -> str:
        s = s.replace(",", "")
        if "." in s:
            whole, frac = s.split(".", 1)
            return _decimal_to_thai(whole, frac)
        return num_to_thai(int(s))

    # Numbers after these are identifiers, not quantities: nobody says
    # "port eight thousand seven hundred and sixty-six".
    _DIGITWISE_AFTER = re.compile(
        r"(?i)((?:พอร์ต|port|ห้อง|รหัส|เบอร์|หมายเลข|version|เวอร์ชัน)\s*)"
        # The slash may already have become "ทับ": the symbol pass runs first.
        r"(\d+(?:\s*(?:/|ทับ)\s*\d+)*)")

    def _numbers(self, text: str) -> str:
        # "Port 80/443" is two ports, and both are identifiers — reading the
        # second as four hundred and forty-three when the first was eight-zero
        # is worse than either choice applied consistently.
        text = self._DIGITWISE_AFTER.sub(
            lambda m: m.group(1) + " ทับ ".join(
                _digits_to_thai(part.strip())
                for part in re.split(r"/|ทับ", m.group(2))),
            text)
        # Long runs (phone numbers, ids) are identifiers too, whatever precedes them.
        text = re.sub(r"\b\d{7,}\b", lambda m: _digits_to_thai(m.group(0)), text)
        return re.sub(r"\b\d[\d,]*(?:\.\d+)?\b",
                      lambda m: self._number(m.group(0)), text)

    def _apply(self, pattern: re.Pattern | None, table: Dict[str, str], text: str) -> str:
        if pattern is None:
            return text
        lower = {k.lower(): v for k, v in table.items()}
        return pattern.sub(lambda m: lower.get(m.group(0).lower(), m.group(0)), text)

    # -- the pipeline -----------------------------------------------------

    def normalize(self, text: str) -> str:
        if not text:
            return text
        text = self._strip_markdown(text)
        text = text.translate(_THAI_DIGIT_MAP)
        # Emails before the @ symbol rule, or the address is torn apart.
        text = self._emails(text)
        text = self._handles(text)    # after emails: user@host must not match first
        text = self._domains(text)    # after both: neither is a bare domain
        # Phones before general numbers, or each block becomes a quantity.
        text = self._phones(text)
        # Abbreviations before numbers: "12 กม." must not lose its unit to the
        # number pass, and "น." must not survive to be read as a letter.
        # Times and dates run before abbreviations so "23:45 น." is one clock
        # reading rather than a clock plus a stray "นาฬิกา".
        text = self._ips(text)            # before everything numeric
        text = self._dates(text)          # before times: both eat digit groups
        text = self._times(text)
        text = self._apply(self._abbr_re, self.abbr, text)
        text = self._percent(text)        # before numbers, or the % is orphaned
        text = self._versions(text)       # before numbers, or "v9" loses its v
        text = self._apply(self._word_re, self.words, text)
        text = self._acronyms(text)   # after the lexicon: GPU is known, CYN is not
        if AUTO_TRANSLITERATE:
            text = self._latin_words(text)
        for sym, spoken in self.symbols.items():
            text = text.replace(sym, spoken)
        text = self._numbers(text)
        # Stripping a marker can leave a space in front of punctuation, which
        # reads as a pause before a comma that should not be there.
        text = re.sub(r"\s+([,;?!])", r"\1", text)
        text = re.sub(r"\s{2,}", " ", text).strip()
        # "บริษัท CYN Communication (ซีวายเอ็น คอมมิวนิเคชั่น จำกัด)" normalises
        # to the same Thai phrase twice — the writer glossed the English for
        # readers, which is redundant once both halves are spoken Thai. Collapse
        # an adjacent exact repeat of up to four words; longer spans are more
        # likely to be deliberate.
        for n in (4, 3, 2, 1):
            pattern = r"(?<![^\s])((?:\S+)(?:\s+\S+){%d})(\s+\1)+(?![^\s])" % (n - 1)
            text = re.sub(pattern, r"\1", text)
        return text


_DEFAULT: ThaiNormalizer | None = None


def normalize(text: str) -> str:
    """Module-level convenience using the default lexicon."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = ThaiNormalizer()
    return _DEFAULT.normalize(text)


if __name__ == "__main__":
    import sys
    n = ThaiNormalizer()
    for line in (sys.argv[1:] or [
        "ตอนนี้เวลา 23:45 น.",
        "อุณหภูมิ 36.5 องศา",
        "แบตเตอรี่เหลือ 85%",
        "ระยะทาง 12 กม. ใช้เวลา 30 นาที",
        "วันที่ 10/09/2026",
        "รัน docker container บน GPU node",
        "เข้าที่ localhost พอร์ต 8766",
    ]):
        print(f"{line}\n  → {n.normalize(line)}\n")
