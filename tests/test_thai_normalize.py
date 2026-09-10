"""Tests for the Thai text normaliser.

Every case here comes from a real mispronunciation measured on the deployed
model — the text was synthesised, transcribed back with Whisper, and came out
wrong. The assertions describe what the model must be handed instead.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("thai_normalize", ROOT / "gpu-node" / "thai_normalize.py")
tn = importlib.util.module_from_spec(spec)
sys.modules["thai_normalize"] = tn
spec.loader.exec_module(tn)


class TestNumbers:
    @pytest.mark.parametrize("n,expected", [
        (0, "ศูนย์"), (1, "หนึ่ง"), (9, "เก้า"),
        (10, "สิบ"),          # not หนึ่งสิบ
        (11, "สิบเอ็ด"),      # trailing one is เอ็ด
        (20, "ยี่สิบ"),        # not สองสิบ
        (21, "ยี่สิบเอ็ด"),
        (100, "หนึ่งร้อย"),
        (101, "หนึ่งร้อยเอ็ด"),
        (1000, "หนึ่งพัน"),
        (1250, "หนึ่งพันสองร้อยห้าสิบ"),
        (10000, "หนึ่งหมื่น"),
        (100000, "หนึ่งแสน"),
        (1000000, "หนึ่งล้าน"),
        (2026, "สองพันยี่สิบหก"),
    ])
    def test_cardinals(self, n, expected):
        assert tn.num_to_thai(n) == expected

    def test_millions_stack(self):
        """ล้าน is a real place value, so it repeats rather than overflowing."""
        assert tn.num_to_thai(12_000_000).startswith("สิบสองล้าน")

    def test_negative(self):
        assert tn.num_to_thai(-5) == "ลบห้า"


@pytest.fixture
def n():
    return tn.ThaiNormalizer(lexicon={
        "abbreviations": {"กม.": "กิโลเมตร", "น.": "นาฬิกา"},
        "words": {"GPU": "จีพียู", "node": "โหนด", "docker": "ด็อกเกอร์"},
        "symbols": {"$": "ดอลลาร์"},
    })


class TestMeasuredFailures:
    """Each name is what the model said before the normaliser existed."""

    def test_time_was_read_as_one_five_digit_number(self, n):
        out = n.normalize("ตอนนี้เวลา 23:45 น.")
        assert "ยี่สิบสามนาฬิกาสี่สิบห้านาที" in out

    def test_time_does_not_say_the_clock_twice(self, n):
        """The trailing น. must be consumed by the time, not expanded again."""
        assert n.normalize("เวลา 23:45 น.").count("นาฬิกา") == 1

    def test_decimal_point_was_dropped(self, n):
        assert "สามสิบหกจุดห้า" in n.normalize("อุณหภูมิ 36.5 องศา")

    def test_percent_sign_was_dropped(self, n):
        assert "แปดสิบห้าเปอร์เซ็นต์" in n.normalize("เหลือ 85%")

    def test_abbreviation_was_read_as_a_word(self, n):
        """12 กม. came out as สิบสองกลม — 'round', not 'kilometres'."""
        out = n.normalize("ระยะทาง 12 กม.")
        assert "กิโลเมตร" in out and "กม." not in out

    def test_date_slashes_were_dropped(self, n):
        out = n.normalize("วันที่ 10/09/2026")
        assert "กันยายน" in out and "สองพันยี่สิบหก" in out

    def test_date_does_not_repeat_the_word_date(self, n):
        assert n.normalize("วันที่ 10/09/2026").count("วันที่") == 1

    def test_date_supplies_the_word_when_absent(self, n):
        assert n.normalize("10/09/2026").startswith("วันที่")

    def test_english_terms_get_thai_spellings(self, n):
        out = n.normalize("รัน docker บน GPU node")
        assert "ด็อกเกอร์" in out and "จีพียู" in out and "โหนด" in out


class TestIdentifiersVsQuantities:
    def test_a_port_is_read_digit_by_digit(self, n):
        """Nobody says 'port eight thousand seven hundred and sixty-six'."""
        assert n.normalize("พอร์ต 8766") == "พอร์ต แปดเจ็ดหกหก"

    def test_a_price_is_read_as_a_quantity(self, n):
        assert "หนึ่งพันสองร้อยห้าสิบ" in n.normalize("ราคา 1,250 บาท")

    def test_long_runs_are_identifiers_whatever_precedes(self, n):
        assert n.normalize("0812345678") == "ศูนย์แปดหนึ่งสองสามสี่ห้าหกเจ็ดแปด"


class TestWordBoundaries:
    def test_ascii_entries_do_not_match_inside_words(self):
        """A naive replace turns 'nodes' into 'โหนดs' and 'anode' into 'aโหนด'."""
        norm = tn.ThaiNormalizer(lexicon={
            "abbreviations": {}, "symbols": {}, "words": {"node": "โหนด"}})
        assert norm.normalize("anode") == "anode"
        assert norm.normalize("node") == "โหนด"

    def test_longest_match_wins(self):
        norm = tn.ThaiNormalizer(lexicon={
            "abbreviations": {}, "symbols": {},
            "words": {"GPU": "จีพียู", "GPU node": "จีพียูโหนด"}})
        assert norm.normalize("GPU node") == "จีพียูโหนด"

    def test_matching_is_case_insensitive(self, n):
        assert "จีพียู" in n.normalize("gpu")


class TestRobustness:
    def test_empty_input(self, n):
        assert n.normalize("") == ""

    def test_plain_thai_is_untouched(self, n):
        assert n.normalize("สวัสดีครับ") == "สวัสดีครับ"

    def test_thai_digits_are_converted(self, n):
        assert "สิบสอง" in n.normalize("๑๒ ชิ้น")

    def test_invalid_time_is_left_alone(self, n):
        """25:99 is not a clock; it must not become one."""
        assert "นาฬิกา" not in n.normalize("อัตราส่วน 25:99")

    def test_invalid_date_is_left_alone(self, n):
        assert "กันยายน" not in n.normalize("99/99/2026")

    def test_missing_lexicon_file_is_not_fatal(self, tmp_path):
        lex = tn.load_lexicon(tmp_path / "nope.yaml")
        assert lex == {"abbreviations": {}, "words": {}, "symbols": {}}
        assert tn.ThaiNormalizer(lexicon=lex).normalize("ทดสอบ") == "ทดสอบ"
