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


@pytest.fixture
def biz():
    """A lexicon shaped like the contact-details case that exposed these bugs."""
    return tn.ThaiNormalizer(lexicon={
        "abbreviations": {},
        "symbols": {"@": " แอท "},
        "words": {
            "cyn": "ซีวายเอ็น", "group": "กรุ๊ป", "sale": "เซล",
            "support": "ซัพพอร์ต", "info": "อินโฟ", "line": "ไลน์",
            "communication": "คอมมิวนิเคชั่น", "admin": "แอดมิน",
            "GPU": "จีพียู",
        },
    })


class TestMarkdown:
    """Agents emit markdown however firmly the prompt says not to."""

    def test_bold_markers_are_removed(self, biz):
        assert "*" not in biz.normalize("**เบอร์โทรศัพท์:** 02-437-1210")

    def test_bullets_are_removed(self, biz):
        assert biz.normalize("* รายการหนึ่ง").startswith("รายการหนึ่ง")

    def test_unmatched_markers_are_swept(self, biz):
        """A stray ** survives pair-matching and gets voiced as noise."""
        assert "*" not in biz.normalize("ไลน์ ** ทดสอบ")

    def test_link_text_survives_the_url(self, biz):
        assert biz.normalize("[เว็บไซต์](https://x.com)") == "เว็บไซต์"

    def test_colons_and_brackets_become_pauses(self, biz):
        out = biz.normalize("อีเมล (Email): ทดสอบ")
        assert ":" not in out and "(" not in out


class TestPhoneNumbers:
    def test_thai_landline_is_read_digit_by_digit(self, biz):
        """02-437-1210 was read as three separate quantities."""
        assert "ศูนย์สองสี่สามเจ็ดหนึ่งสองหนึ่งศูนย์" in biz.normalize("โทร 02-437-1210")

    def test_the_leading_zero_survives(self, biz):
        assert biz.normalize("081-234-5678").startswith("ศูนย์แปดหนึ่ง")

    def test_unseparated_numbers_work_too(self, biz):
        assert "ศูนย์แปดหนึ่ง" in biz.normalize("0812345678")

    def test_a_price_is_not_mistaken_for_a_phone(self, biz):
        assert "หนึ่งพันสองร้อยห้าสิบ" in biz.normalize("ราคา 1,250 บาท")


class TestEmailsAndHandles:
    def test_address_is_spelled_out(self, biz):
        out = biz.normalize("info@cyn.co.th")
        assert "อินโฟ" in out and "ซีวายเอ็น" in out and "ดอทซีโอดอททีเอช" in out

    def test_a_bare_handle_is_not_treated_as_an_email(self, biz):
        assert biz.normalize("@cyngroup") == "แอท ซีวายเอ็นกรุ๊ป"

    def test_compound_handles_are_segmented(self, biz):
        """Greedy matching takes 'sales' and then cannot place 'upport'."""
        assert biz.normalize("@salesupport") == "แอท เซลซัพพอร์ต"

    def test_unknown_short_token_is_spelled(self, biz):
        assert biz.normalize("@abc") == "แอท เอบีซี"

    def test_unknown_long_token_is_left_for_the_model(self, biz):
        """example spelled out is อีเอ็กซ์เอเอ็มพีแอลอี — worse than a rough attempt."""
        assert "example" in biz.normalize("admin@example.com")


class TestAcronyms:
    def test_unknown_capitals_are_spelled(self, biz):
        assert "ซีวายเอ็น" in biz.normalize("บริษัท CYN")

    def test_known_acronyms_use_the_lexicon(self, biz):
        assert biz.normalize("GPU") == "จีพียู"

    def test_lowercase_words_are_not_treated_as_acronyms(self, biz):
        assert biz.normalize("cat") == "cat"


class TestRedundantGloss:
    def test_a_repeated_phrase_is_said_once(self, biz):
        """'CYN Communication (ซีวายเอ็น คอมมิวนิเคชั่น)' is one name, glossed."""
        out = biz.normalize("CYN Communication (ซีวายเอ็น คอมมิวนิเคชั่น) จำกัด")
        assert out.count("คอมมิวนิเคชั่น") == 1

    def test_a_repeated_word_is_said_once(self, biz):
        assert biz.normalize("อีเมล (Email)").count("อีเมล") == 1

    def test_distinct_neighbours_are_untouched(self, biz):
        assert biz.normalize("หนึ่ง สอง สาม") == "หนึ่ง สอง สาม"


class TestTheContactCase:
    """The message that prompted all of the above."""

    SRC = ("นี่คือรายละเอียดข้อมูลติดต่อของ **บริษัท CYN Communication "
           "(ซีวายเอ็น คอมมิวนิเคชั่น จำกัด)** ครับ: * **เบอร์โทรศัพท์:** "
           "02-437-1210 * **อีเมล (Email):** info@cyn.co.th * **Line ID:** @cyngroup")

    def test_nothing_unspeakable_survives(self, biz):
        out = biz.normalize(self.SRC)
        for ch in "*:()@":
            assert ch not in out, f"{ch!r} left in: {out}"

    def test_every_field_is_spoken(self, biz):
        out = biz.normalize(self.SRC)
        assert "ศูนย์สองสี่สามเจ็ดหนึ่งสองหนึ่งศูนย์" in out   # phone
        assert "อินโฟ แอท ซีวายเอ็น ดอทซีโอดอททีเอช" in out    # email
        assert "แอท ซีวายเอ็นกรุ๊ป" in out                      # line id

    def test_no_digits_are_left_unspoken(self, biz):
        assert not any(c.isdigit() for c in biz.normalize(self.SRC))


class TestStructuredProse:
    """A long explanatory reply: headings, numbered lists, glossed English."""

    @pytest.fixture
    def doc(self):
        return tn.ThaiNormalizer(lexicon={
            "abbreviations": {}, "symbols": {"/": " ทับ "},
            "words": {"PDPA": "พีดีพีเอ", "Data Subject": "ดาต้า ซับเจกต์",
                      "controller": "คอนโทรลเลอร์", "processor": "โพรเซสเซอร์"},
        })

    def test_a_list_number_does_not_end_the_sentence(self, doc):
        """"1." became "หนึ่ง." and the splitter stopped the reply there."""
        out = doc.normalize("1. การขอความยินยอม")
        assert out.startswith("ข้อหนึ่ง")
        assert "." not in out

    def test_list_numbers_count_up(self, doc):
        out = doc.normalize("1. หนึ่ง\n2. สอง\n3. สาม")
        assert "ข้อหนึ่ง" in out and "ข้อสอง" in out and "ข้อสาม" in out

    def test_a_decimal_is_not_a_list_marker(self, doc):
        assert "จุดห้า" in doc.normalize("อุณหภูมิ 36.5 องศา")

    def test_headings_lose_their_hashes(self, doc):
        assert "#" not in doc.normalize("### **PDPA คืออะไร?**")

    def test_horizontal_rules_are_removed(self, doc):
        assert "-" not in doc.normalize("ก่อนหน้า\n---\nถัดไป")

    def test_quotation_marks_are_removed(self, doc):
        out = doc.normalize('เรียกว่า "ข้อมูลส่วนบุคคล" ครับ')
        assert '"' not in out and "ข้อมูลส่วนบุคคล" in out

    def test_slash_between_words_is_a_choice(self, doc):
        """Controller/Processor is "or"; it is not a path."""
        assert "คอนโทรลเลอร์ หรือ โพรเซสเซอร์" in doc.normalize("Data Controller/Processor")

    def test_slash_between_numbers_stays_a_date(self, doc):
        assert "กันยายน" in doc.normalize("10/09/2026")

    def test_multiword_english_terms_beat_single_words(self, doc):
        assert doc.normalize("Data Subject") == "ดาต้า ซับเจกต์"

    def test_unknown_acronyms_are_spelled(self, doc):
        assert "พีทีพีเอ" in doc.normalize("PTPA")

    def test_a_name_is_left_for_the_model(self, doc):
        """Nero is five letters — spelling it out would be worse than an attempt."""
        assert "Nero" in doc.normalize("รบกวนคุณ Nero")
