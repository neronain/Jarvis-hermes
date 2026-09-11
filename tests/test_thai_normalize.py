"""Tests for the Thai text normaliser.

Every case here comes from a real mispronunciation measured on the deployed
model — the text was synthesised, transcribed back with Whisper, and came out
wrong. The assertions describe what the model must be handed instead.
"""
import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gpu-node"))
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


class TestTransliteration:
    """The phonetic fallback, and why it is not on by default."""

    def test_a_known_word_gets_a_pronunciation(self):
        t = importlib.import_module("en_to_thai")
        assert t.transliterate("container")
        assert t.source("container") == "cmudict"

    def test_an_unknown_word_returns_nothing(self):
        """Spelling guesses produced เวอร์เอะช็อรก for Wireshark — worse than
        leaving it in English, since the model at least attempts what it sees."""
        t = importlib.import_module("en_to_thai")
        assert t.transliterate("wireshark") is None
        assert t.transliterate("wireshark", allow_spelling=True)

    def test_it_is_off_unless_asked_for(self):
        import thai_normalize
        assert thai_normalize.AUTO_TRANSLITERATE is False

    def test_the_lexicon_still_wins(self):
        n = tn.ThaiNormalizer(lexicon={
            "abbreviations": {}, "symbols": {}, "words": {"container": "คอนเทนเนอร์"}})
        assert "คอนเทนเนอร์" in n.normalize("ระบบ container ทำงาน")


class TestAbbreviationBoundaries:
    """A Thai abbreviation ends at its period, and only there."""

    @pytest.fixture
    def ab(self):
        return tn.ThaiNormalizer(lexicon={
            "abbreviations": {"น.": "นาฬิกา", "กม.": "กิโลเมตร"},
            "symbols": {}, "words": {}})

    def test_it_does_not_fire_mid_word(self, ab):
        """`ชื่อต้น.นามสกุล` became `ชื่อต้นาฬิกานามสกุล` — silent corruption."""
        assert "นาฬิกา" not in ab.normalize("รูปแบบ ชื่อต้น.นามสกุล ครับ")

    def test_it_still_fires_where_it_should(self, ab):
        assert "นาฬิกา" in ab.normalize("ตอนนี้เวลา 23:45 น.")
        assert "กิโลเมตร" in ab.normalize("ระยะทาง 12 กม. ใช้เวลา")


class TestLatex:
    """Assistants emit LaTeX for arrows; spoken it is currency plus a word."""

    @pytest.fixture
    def tex(self):
        return tn.ThaiNormalizer(lexicon={
            "abbreviations": {}, "symbols": {"$": "ดอลลาร์"}, "words": {}})

    def test_an_arrow_becomes_a_word(self, tex):
        out = tex.normalize(r"อาจจะเป็น a $\rightarrow$ b")
        assert "ถึง" in out
        assert "ดอลลาร์" not in out and "rightarrow" not in out

    def test_leftover_commands_are_removed(self, tex):
        assert "\\" not in tex.normalize(r"สมการ $\alpha + \beta$ ครับ")

    def test_a_real_dollar_still_speaks(self, tex):
        assert "ดอลลาร์" in tex.normalize("ราคา $50")


class TestLeadingZero:
    """A number that starts with a zero is an identifier, not a quantity."""

    @pytest.fixture
    def z(self):
        return tn.ThaiNormalizer(lexicon={
            "abbreviations": {}, "symbols": {}, "words": {}})

    def test_a_lottery_number_keeps_its_zero(self, z):
        """`041` came out สี่สิบเอ็ด — a different number, in a reply about
        which number it was."""
        assert "ศูนย์สี่หนึ่ง" in z.normalize("ที่แท้คือเลข 041 ใช่ไหมครับ")

    def test_all_zeros(self, z):
        assert "ศูนย์ศูนย์ศูนย์" in z.normalize("เริ่มจาก 000 ครับ")

    def test_a_decimal_below_one_is_still_a_quantity(self, z):
        assert z.normalize("อัตรา 0.1 ครับ") == "อัตรา ศูนย์จุดหนึ่ง ครับ"

    def test_a_plain_zero_is_a_quantity(self, z):
        assert z.normalize("เหลือ 0 ครับ") == "เหลือ ศูนย์ ครับ"


class TestRanges:
    """A hyphen between numbers is silent, so the range ran together."""

    @pytest.fixture
    def r(self):
        return tn.ThaiNormalizer(lexicon={
            "abbreviations": {}, "symbols": {}, "words": {}})

    def test_a_temperature_range(self, r):
        """`28-33 องศาเซลเซียส` gave ยี่สิบแปด-สามสิบสาม, read as two numbers."""
        out = r.normalize("อุณหภูมิประมาณ 28-33 องศาเซลเซียส")
        assert "ยี่สิบแปด ถึง สามสิบสาม" in out
        assert "-" not in out

    def test_an_em_dash_is_a_range_too(self, r):
        assert "ยี่สิบเจ็ด ถึง แปดสิบสี่" in r.normalize("ผมให้ไปเป็น 27 — 84 แล้ว")

    def test_a_year_range(self, r):
        assert "สองพันยี่สิบ ถึง สองพันยี่สิบสี่" in r.normalize("ช่วง 2020-2024 ครับ")

    def test_a_phone_number_is_not_a_range(self, r):
        out = r.normalize("โทร 02-437-1210")
        assert "ถึง" not in out
        assert "ศูนย์สองสี่สามเจ็ดหนึ่งสองหนึ่งศูนย์" in out


class TestIsoDates:
    """Assistants write 2026-09-11; the day-first rule could not match it."""

    @pytest.fixture
    def d(self):
        return tn.ThaiNormalizer(lexicon={
            "abbreviations": {}, "symbols": {}, "words": {}})

    def test_iso_order(self, d):
        assert d.normalize("อัปเดต 2026-09-11 แล้ว") == \
            "อัปเดต วันที่สิบเอ็ด กันยายน สองพันยี่สิบหก แล้ว"

    def test_it_does_not_repeat_a_supplied_prefix(self, d):
        assert d.normalize("วันที่ 2026-09-11").count("วันที่") == 1

    def test_day_first_still_works(self, d):
        assert "วันที่สิบ กันยายน" in d.normalize("ประชุม 10-09-2026 นะ")

    def test_an_impossible_date_is_left_alone(self, d):
        assert "กันยายน" not in d.normalize("รหัส 2026-13-45 ครับ")


class TestSentenceBreaks:
    """Emphasis is a marker PAIR. Two different markers are two sentences."""

    @pytest.fixture
    def e(self):
        return tn.ThaiNormalizer(lexicon={
            "abbreviations": {}, "symbols": {}, "words": {}})

    def test_a_new_sentence_is_not_swallowed(self, e):
        """`...นะครับ 😉 **ถ้าจะให้ผม "จัดเต็ม"` ran together as
        นะครับถ้าจะให้ผม, losing the break between two sentences."""
        out = e.normalize('ไม่ให้เดือดร้อนนะครับ 😉 **ถ้าจะให้ผม "จัดเต็ม" กว่านี้')
        assert "นะครับถ้าจะให้ผม" not in out
        assert "นะครับ ถ้าจะให้ผม" in out

    def test_a_matching_pair_still_closes_up(self, e):
        assert "การถูกหวยคือ" in e.normalize('กว่าการ "ถูกหวย" คือการเล่น')

    def test_mixed_but_symmetric_markers_close_up(self, e):
        assert "ผมนึกว่า" in e.normalize('ผม **"นึก"** ว่าครับ')


class TestAddresses:
    """Company details: an address, a postcode, an English gloss, a website."""

    @pytest.fixture
    def a(self):
        return tn.ThaiNormalizer(lexicon={
            "abbreviations": {}, "symbols": {"/": " ทับ "},
            "words": {"shop": "ช็อป", "Veerasiam": "วีรสยาม"}})

    def test_a_postcode_is_not_a_sum_of_money(self, a):
        """`กรุงเทพมหานคร 10110` came out หนึ่งหมื่นหนึ่งร้อยสิบ."""
        assert "หนึ่งศูนย์หนึ่งหนึ่งศูนย์" in a.normalize(
            "เขตวัฒนา กรุงเทพมหานคร 10110")

    def test_five_digits_without_an_address_stay_a_quantity(self, a):
        assert "หนึ่งหมื่นห้าพัน" in a.normalize("ราคา 15000 บาท")

    def test_a_gloss_with_a_comma_is_dropped(self, a):
        """`(Veerasiam Group Co., Ltd.)` survived — the comma was not in the
        gloss character class — and half of it was then translated, leaving
        `Veerasiam กรุ๊ป Co., Ltd.`"""
        assert a.normalize("บริษัท วีรสยาม (Veerasiam Group Co., Ltd.) ครับ") == \
            "บริษัท วีรสยาม ครับ"

    def test_a_host_name_speaks_its_own_separators(self, a):
        """`shop.veerasiam-g.com` became ช็อป.veerasiam-g — the head was looked
        up whole, came back unchanged, and the lexicon then hit `shop` inside
        it, leaving a literal dot and hyphen."""
        out = a.normalize("เว็บ shop.veerasiam-g.com นะ")
        assert out == "เว็บ ช็อป ดอท วีรสยาม จี ดอทคอม นะ"

    def test_a_house_number_keeps_its_slash(self, a):
        assert "ทับ" in a.normalize("ที่อยู่ 1015/2 ถนนสุขุมวิท")


class TestSilenceGate:
    """Whisper answers a clip of silence with a politeness particle, and the
    assistant then says "ครับ" to a user who has not spoken."""

    @staticmethod
    def _is_filler():
        import re as _re, os as _os
        src = (ROOT / "gpu-node" / "stt_server.py").read_text(encoding="utf-8")
        blk = src[src.index("NO_SPEECH_MAX"):src.index("_model = None")]
        ns = {"os": _os, "re": _re}
        exec(compile(blk, "stt_server", "exec"), ns, ns)
        return ns["_is_filler"]

    @pytest.mark.parametrize("text", [
        "ครับ", "ค่ะ", "อืม", "โอเค", "นะครับ", "ok",
        "ครับๆ", "ค่ะ.", "ครับ ครับ",
        "ขอบคุณครับ",     # Whisper's stock output for a silent Thai clip
        "", "   ",
    ])
    def test_nothing_was_said(self, text):
        assert self._is_filler()(text) is True

    @pytest.mark.parametrize("text", [
        "ครับ ผมเข้าใจแล้ว",        # a particle opening a real turn
        "สวัสดี พร้อมทำงานไหม",
        "เติมมาเตือน",              # gibberish — the acoustic gates catch this one
        "โอเค งั้นเริ่มเลยครับ",
    ])
    def test_something_was_said(self, text):
        assert self._is_filler()(text) is False


class TestFileNames:
    """A file name is not a domain and not a sentence."""

    @pytest.fixture
    def f(self):
        return tn.ThaiNormalizer(lexicon={
            "abbreviations": {}, "symbols": {}, "words": {"shop": "ช็อป"}})

    def test_an_extension_is_spoken(self, f):
        """`SOUL.md` came out เอสโอยูแอล.md — a literal dot and two letters
        with no reading."""
        out = f.normalize("กฎอยู่ใน SOUL.md ครับ")
        assert "ดอทเอ็มดี" in out
        assert ".md" not in out

    def test_a_domain_is_not_a_file(self, f):
        assert "ดอทคอม" in f.normalize("เว็บ shop.example.com นะ")

    def test_an_unknown_extension_is_left_alone(self, f):
        assert "ดอท" not in f.normalize("ไฟล์ report.qqq ครับ")


class TestGlossPunctuation:
    """`(And ready to go!)` survived — `!` was in neither character class."""

    @pytest.fixture
    def g(self):
        return tn.ThaiNormalizer(lexicon={
            "abbreviations": {}, "symbols": {}, "words": {}})

    def test_an_exclamation_does_not_save_the_gloss(self, g):
        assert g.normalize("พร้อมมากครับ! (And ready to go!) นะ") == "พร้อมมากครับ! นะ"

    def test_a_question_mark_too(self, g):
        assert "Really" not in g.normalize("จริงหรือ? (Really?) ครับ")


class TestLoneLatin:
    """A single Latin letter sitting in Thai has no reading — the voice skips
    it or guesses, and either way the sentence loses a word."""

    @pytest.fixture
    def l(self):
        return tn.ThaiNormalizer(lexicon={
            "abbreviations": {}, "symbols": {},
            "words": {"GPU": "จีพียู", "kg": "กิโลกรัม"}})

    def test_a_letter_used_as_a_name(self, l):
        """"ชั้น U" was read with the U simply absent."""
        assert l.normalize("ชั้น U ครับ") == "ชั้น ยู ครับ"

    def test_a_two_letter_fragment(self, l):
        """"รบก el" — the model's own typo for รบกวน — kept a bare "el"."""
        assert "อีแอล" in l.normalize("รบก el พี่")

    def test_three_letters_are_left_alone(self, l):
        """cat is a word; ซีเอที is worse than leaving it."""
        assert l.normalize("cat") == "cat"

    def test_the_lexicon_still_wins(self, l):
        assert "กิโลกรัม" in l.normalize("หนัก 5 kg")
        assert "จีพียู" in l.normalize("GPU ว่าง")

    def test_it_does_not_reach_into_an_address(self, l):
        out = l.normalize("อีเมล a@b.com")
        assert "@" not in out and "b.com" not in out


class TestRepeatedShortWords:
    """Collapsing an adjacent repeat is right for a gloss said twice and wrong
    for a name that contains a repeat."""

    @pytest.fixture
    def r(self):
        return tn.ThaiNormalizer(lexicon={
            "abbreviations": {}, "symbols": {}, "words": {}})

    def test_a_name_keeps_its_repeat(self, r):
        """A shop called One To Two came back as วัน ทู — renamed, silently."""
        assert r.normalize("ร้าน วัน ทู ทู (One To Two) ครับ") == "ร้าน วัน ทู ทู ครับ"

    def test_a_repeated_syllable_survives(self, r):
        assert r.normalize("ชื่อ ปู ปู นะ") == "ชื่อ ปู ปู นะ"

    def test_a_long_repeated_word_is_still_collapsed(self, r):
        assert r.normalize("คอมมิวนิเคชั่น คอมมิวนิเคชั่น จำกัด") == "คอมมิวนิเคชั่น จำกัด"

    def test_a_repeated_phrase_is_still_collapsed(self, r):
        assert r.normalize("ซีวายเอ็น กรุ๊ป ซีวายเอ็น กรุ๊ป") == "ซีวายเอ็น กรุ๊ป"


class TestUrls:
    """A URL read aloud is fifteen seconds nobody can write down."""

    @pytest.fixture
    def u(self):
        return tn.ThaiNormalizer(lexicon={
            "abbreviations": {}, "symbols": {"/": " ทับ ", "+": " บวก "},
            "words": {"google": "กูเกิล", "github": "กิตฮับ"}})

    def test_only_the_host_is_spoken(self, u):
        """https://www.google.com/maps/search/One+To+Two+Cafe+Bangna came out as
        "เอชทีทีพีเอส ทับ ทับ ดับเบิลยูดับเบิลยูดับเบิลยู ดอท กูเกิล ดอทคอม
        หรือ แมปส์ หรือ search หรือ One บวก..." — by then nobody is listening."""
        out = u.normalize("ลองดูที่นี่ https://www.google.com/maps/search/One+To+Two+Cafe ครับ")
        assert out == "ลองดูที่นี่ กูเกิล ดอทคอม ครับ"

    def test_the_scheme_is_never_spelled(self, u):
        assert "เอชทีทีพี" not in u.normalize("ไปที่ http://github.com/x/y นะ")

    def test_a_markdown_link_speaks_its_text_only(self, u):
        assert u.normalize("[เว็บไซต์](https://github.com/a/b) ครับ") == "เว็บไซต์ ครับ"

    def test_a_bare_address_is_still_read_as_an_address(self, u):
        out = u.normalize("ไป http://192.168.1.1:8080/admin ครับ")
        assert "จุด" in out and "แอดมิน" not in out

    def test_a_plain_domain_is_untouched_by_this_rule(self, u):
        assert "ดอทคอม" in u.normalize("เว็บ example.com นะ")


class TestIdentifierNumbers:
    """A port is not a quantity. The model was writing these out itself and
    getting it wrong — "สองร้อยยี่สิบสาม" for port 223 — because the voice
    instructions told it to say numbers as words, which predates these rules."""

    @pytest.fixture
    def i(self):
        return tn.ThaiNormalizer(lexicon={
            "abbreviations": {}, "symbols": {}, "words": {}})

    def test_a_port_is_read_digit_by_digit(self, i):
        assert "สองสองสาม" in i.normalize("พอร์ต 223 เปิดอยู่")
        assert "สองร้อยยี่สิบสาม" not in i.normalize("พอร์ต 223 เปิดอยู่")

    def test_an_address_is_read_digit_by_digit(self, i):
        out = i.normalize("เครื่อง 103.212.182.45 ครับ")
        assert "หนึ่งศูนย์สามจุดสองหนึ่งสองจุดหนึ่งแปดสองจุดสี่ห้า" in out

    def test_a_quantity_is_still_a_quantity(self, i):
        assert "สองร้อยยี่สิบสาม" in i.normalize("ทั้งหมด 223 รายการ")
