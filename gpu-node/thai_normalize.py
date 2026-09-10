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

import re
from pathlib import Path
from typing import Dict, List, Tuple

# ---------------------------------------------------------------- numbers

_DIGITS = ["ศูนย์", "หนึ่ง", "สอง", "สาม", "สี่", "ห้า", "หก", "เจ็ด", "แปด", "เก้า"]
_PLACES = ["", "สิบ", "ร้อย", "พัน", "หมื่น", "แสน"]
_THAI_DIGIT_MAP = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")


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
        # ASCII entries get word boundaries so "no" doesn't fire inside "node".
        parts = []
        for k in keys:
            esc = re.escape(k)
            parts.append(rf"\b{esc}\b" if k.isascii() else esc)
        return re.compile("|".join(parts), re.IGNORECASE)

    # -- individual passes ------------------------------------------------

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
        r"(?i)((?:พอร์ต|port|ห้อง|รหัส|เบอร์|หมายเลข|version|เวอร์ชัน)\s*)(\d+)")

    def _numbers(self, text: str) -> str:
        text = self._DIGITWISE_AFTER.sub(
            lambda m: m.group(1) + _digits_to_thai(m.group(2)), text)
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
        text = text.translate(_THAI_DIGIT_MAP)
        # Abbreviations before numbers: "12 กม." must not lose its unit to the
        # number pass, and "น." must not survive to be read as a letter.
        # Times and dates run before abbreviations so "23:45 น." is one clock
        # reading rather than a clock plus a stray "นาฬิกา".
        text = self._dates(text)          # before times: both eat digit groups
        text = self._times(text)
        text = self._apply(self._abbr_re, self.abbr, text)
        text = self._percent(text)        # before numbers, or the % is orphaned
        text = self._apply(self._word_re, self.words, text)
        for sym, spoken in self.symbols.items():
            text = text.replace(sym, spoken)
        text = self._numbers(text)
        return re.sub(r"\s{2,}", " ", text).strip()


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
