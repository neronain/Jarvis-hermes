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


class TestEmoji:
    """Assistants emit emoji constantly; the model has no reading for them."""

    @pytest.fixture
    def e(self):
        return tn.ThaiNormalizer(lexicon={"abbreviations": {}, "symbols": {}, "words": {}})

    @pytest.mark.parametrize("text", [
        "พร้อมลุยงาน 🚀",
        "ยินดีครับ 😊",
        "ดีมาก 👍🏽",              # with a skin-tone modifier
        "ทีม 👨‍👩‍👧‍👦 พร้อม",       # ZWJ sequence
        "เสร็จแล้ว ✅",
        "ขึ้น ↑ ลง ↓",
    ])
    def test_emoji_are_removed(self, e, text):
        out = e.normalize(text)
        assert not any(ord(c) > 0x2000 and not (0x0E00 <= ord(c) <= 0x0E7F) for c in out), out

    def test_thai_text_survives_alongside_emoji(self, e):
        assert e.normalize("พร้อมลุยงาน 🚀 ครับ") == "พร้อมลุยงาน ครับ"

    def test_the_thai_repetition_mark_is_not_an_emoji(self, e):
        """ๆ sits near the symbol ranges; stripping it would break real Thai."""
        assert "ๆ" in e.normalize("ปกติ ๆ ไม่มีอะไร")

    def test_percent_still_works_next_to_an_arrow(self, e):
        assert "เปอร์เซ็นต์" in e.normalize("ราคา 50% ↑")


class TestThaiSpacing:
    """Thai has no inter-word spaces; a space is a phrase break, read as a pause."""

    @pytest.fixture
    def th(self):
        return tn.ThaiNormalizer(lexicon={"abbreviations": {}, "symbols": {}, "words": {}})

    def test_emphasis_inside_a_phrase_does_not_leave_a_pause(self, th):
        """`ผม "นึก" ว่า` must not become `ผม นึก ว่า` — that is spoken with pauses."""
        assert th.normalize('ขออภัยที่ผม "นึก" ว่าคุณจะเริ่มใหม่') == "ขออภัยที่ผมนึกว่าคุณจะเริ่มใหม่"

    def test_bold_and_quotes_together(self, th):
        assert th.normalize('คุณกำลัง **"แกง"** หรือทดสอบ') == "คุณกำลังแกงหรือทดสอบ"

    def test_ordinary_thai_spacing_survives(self, th):
        """The rule must only close gaps a marker made, never real phrase breaks."""
        assert th.normalize("ประโยคแรก วรรคปกติ ต้องไม่หาย") == "ประโยคแรก วรรคปกติ ต้องไม่หาย"

    def test_emphasis_at_a_real_boundary_keeps_the_boundary(self, th):
        """A marker between Thai and non-Thai has no space to reclaim."""
        out = th.normalize('เข้าใจ **จังหวะ** ครับ')
        assert "เข้าใจจังหวะครับ" == out

    def test_a_quoted_english_word_is_still_separated(self, th):
        out = th.normalize('แบบ "Robot" ที่ตั้งใจ')
        assert "Robot" in out

    def test_a_long_emphasised_span_keeps_its_spacing(self, th):
        """A marker opening a sentence looks like one wrapping a word.

        Closing every gap ran sentences together: `เกินเหตุ **เอาล่ะครับ`
        became `เกินเหตุเอาล่ะครับ`. Length is what separates emphasis inside a
        phrase from a clause of its own.
        """
        out = th.normalize("ที่ดูตั้งใจเกินเหตุ **เอาล่ะครับ ผมกลับมา**")
        assert "เกินเหตุ เอาล่ะครับ" in out

    def test_a_short_emphasis_still_closes_up(self, th):
        assert th.normalize('ผม "นึก" ว่าคุณ') == "ผมนึกว่าคุณ"

    def test_slash_between_thai_takes_no_spaces(self, th):
        """`การฟัง/การรับรู้` is one phrase; spaces would pause inside it."""
        assert th.normalize("ความสามารถในการฟัง/การรับรู้") == "ความสามารถในการฟังหรือการรับรู้"


class TestVersions:
    @pytest.fixture
    def v(self):
        return tn.ThaiNormalizer(lexicon={
            "abbreviations": {}, "symbols": {},
            "words": {"VMware": "วีเอ็มแวร์", "vSphere": "วีสเฟียร์"}})

    def test_a_version_number_is_spoken_as_one(self, v):
        assert "เวอร์ชันเก้า" in v.normalize("ที่ใช้เลข v9 ครับ")

    def test_dotted_versions(self, v):
        assert "เวอร์ชันสองจุดหนึ่ง" in v.normalize("v2.1")

    def test_the_word_is_not_repeated(self, v):
        """`เวอร์ชัน v2.1` already says it once."""
        assert v.normalize("เวอร์ชัน v2.1").count("เวอร์ชัน") == 1

    def test_a_name_starting_with_v_is_not_a_version(self, v):
        """vSphere must survive: the digit is what makes it a version."""
        assert "วีสเฟียร์" in v.normalize("vSphere")
        assert "เวอร์ชัน" not in v.normalize("vSphere")

    def test_space_before_punctuation_is_removed(self, v):
        """Stripping a marker can leave a pause in front of a comma."""
        assert " ," not in v.normalize("vSphere, **ESXi** , หรือ")


class TestEnglishGloss:
    """Thai business writing glosses its terms in English for readers."""

    @pytest.fixture
    def g(self):
        return tn.ThaiNormalizer(lexicon={
            "abbreviations": {}, "symbols": {"&": " และ "}, "words": {}})

    def test_a_gloss_after_thai_is_dropped(self, g):
        """`ด้านราคาและความคุ้มค่า (Cost & Licensing)` says it twice, aloud."""
        out = g.normalize("ด้านราคาและความคุ้มค่า (Cost & Licensing) นี่คือจุดเปลี่ยน")
        assert "Cost" not in out and "Licensing" not in out
        assert "ด้านราคาและความคุ้มค่า" in out

    def test_thai_in_parentheses_survives(self, g):
        """Only an English gloss is redundant; Thai in brackets is content."""
        assert "เขียนโค้ด" in g.normalize("เริ่มงานใหม่ (เช่น เขียนโค้ด วางแผน)")

    def test_a_gloss_after_an_acronym_is_kept(self, g):
        """`HA (High Availability)` explains the acronym rather than repeating it."""
        assert "High Availability" in g.normalize("จุดแข็งเรื่อง HA (High Availability)")

    def test_numbers_in_parentheses_survive(self, g):
        assert "สอง" in g.normalize("ทั้งหมด (2) รายการ")

    def test_a_gloss_is_found_across_a_closing_quote(self, g):
        """`"เพ้อเจ้อ" (Verbose)` — a quote stands between the word and the bracket."""
        out = g.normalize('ผม "เพ้อเจ้อ" (Verbose) เกินไป')
        assert "Verbose" not in out
        assert "เพ้อเจ้อ" in out

    def test_a_phrase_beats_a_word_that_means_something_else(self):
        """"line" reads as ไลน์ for a Line ID, which is wrong inside Command Line."""
        n = tn.ThaiNormalizer(lexicon={
            "abbreviations": {}, "symbols": {},
            "words": {"line": "ไลน์", "Command Line": "คอมมานด์ไลน์"}})
        assert n.normalize("การใช้ Command Line") == "การใช้ คอมมานด์ไลน์"
        assert n.normalize("Line ID") == "ไลน์ ไอดี"


class TestLinksAndAddresses:
    @pytest.fixture
    def a(self):
        return tn.ThaiNormalizer(lexicon={
            "abbreviations": {}, "symbols": {"/": " ทับ "},
            "words": {"BTS": "บีทีเอส", "Google Maps": "กูเกิล แมปส์"}})

    def test_a_markdown_link_keeps_only_its_label(self, a):
        """A URL read aloud is unlistenable, and the label already says it."""
        out = a.normalize("ดูที่ [เปิดแผนที่](https://www.google.com/maps/search/?api=1&query=x+y) ครับ")
        assert "http" not in out and "query" not in out
        assert "เปิดแผนที่" in out

    def test_a_house_number_keeps_its_slash(self, a):
        assert "ทับ" in a.normalize("ที่อยู่ 230/51 ซอยกรุงธนบุรี")

    def test_transit_acronyms(self, a):
        assert "บีทีเอส" in a.normalize("ใกล้กับ BTS กรุงธนบุรี")


class TestNetworkText:
    """A network scan report: addresses, ports, and numbered headings."""

    @pytest.fixture
    def net(self):
        return tn.ThaiNormalizer(lexicon={
            "abbreviations": {}, "symbols": {"/": " ทับ "},
            "words": {"IP": "ไอพี", "Web": "เว็บ"}})

    def test_an_ip_address_is_read_whole(self, net):
        """Every other rule wants a piece of it and the result keeps a literal dot."""
        out = net.normalize("วงเครือข่าย 192.168.101.0 พบว่า")
        assert "." not in out
        assert "หนึ่งเก้าสองจุดหนึ่งหกแปด" in out

    def test_cidr_suffix(self, net):
        assert "ทับ ยี่สิบสี่" in net.normalize("192.168.101.0/24")

    def test_octets_are_read_digit_by_digit(self, net):
        """An address is an identifier, not four quantities."""
        assert "หนึ่งศูนย์จุดศูนย์จุดศูนย์จุดหนึ่ง" in net.normalize("10.0.0.1")

    def test_something_that_is_not_an_address_is_not_treated_as_one(self, net):
        """Octets above 255 mean it is not an address.

        Asserted on the rule rather than the pipeline: the decimal rule picks
        the string up afterwards, which is fine — what matters is that the
        address rule declined it.
        """
        assert net._ips("999.888.777.666") == "999.888.777.666"
        assert net._ips("192.168.1.1") != "192.168.1.1"

    def test_a_decimal_is_not_an_address(self, net):
        assert "สามสิบหกจุดห้า" in net.normalize("อุณหภูมิ 36.5 องศา")

    def test_a_numbered_heading_behind_markers_loses_its_period(self, net):
        """`### **1. หัวข้อ**` kept its period, and a period ends a sentence."""
        out = net.normalize("### **1. วิเคราะห์กลุ่ม** ต่อไป")
        assert out.startswith("ข้อหนึ่ง")
        assert "." not in out

    def test_a_port_pair_is_read_consistently(self, net):
        """80 digit-by-digit and 443 as a quantity is worse than either alone."""
        assert "แปดศูนย์ ทับ สี่สี่สาม" in net.normalize("Port 80/443")
