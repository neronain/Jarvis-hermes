"""Tests for the parts of the sidecars that don't need a GPU.

Everything here runs on CPU with no model loaded: sentence splitting, PCM
conversion, resampling, WAV framing, and voice-config validation. These are
the pieces most likely to be silently wrong — a bad resample sounds like a
stretched tape, and a bad splitter produces long pauses — so they get covered
even though the model itself can't be tested here.
"""
import importlib.util
import sys
import wave
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    """Import a sidecar module without executing its FastAPI startup hooks."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tts = _load("tts_server", ROOT / "gpu-node" / "tts_server.py")
provider = _load("f5_tts_provider", ROOT / "host" / "adapters" / "f5_tts_provider.py")


# --------------------------------------------------------------------------
# sentence splitting
# --------------------------------------------------------------------------

class TestSplitSentences:
    def test_splits_on_thai_and_latin_terminators(self):
        out = tts.split_sentences("สวัสดีครับ. วันนี้อากาศดี! คุณสบายดีไหม?")
        assert len(out) == 3
        assert out[0].startswith("สวัสดี")

    def test_no_terminator_is_still_one_chunk(self):
        assert tts.split_sentences("ข้อความไม่มีจุด") == ["ข้อความไม่มีจุด"]

    def test_empty_input_yields_nothing(self):
        assert tts.split_sentences("") == []
        assert tts.split_sentences("   \n  ") == []

    def test_long_sentence_is_broken_under_the_cap(self):
        long_text = "word " * 200          # ~1000 chars, no terminator
        chunks = tts.split_sentences(long_text, max_chars=100)
        assert len(chunks) > 1
        assert all(len(c) <= 100 for c in chunks)

    def test_no_content_is_lost_when_breaking_long_text(self):
        text = " ".join(f"w{i}" for i in range(300))
        joined = "".join(tts.split_sentences(text, max_chars=80)).replace(" ", "")
        assert joined == text.replace(" ", "")

    def test_host_and_node_splitters_agree(self):
        """The two implementations are duplicated on purpose — they must not drift."""
        for text in [
            "สวัสดีครับ. วันนี้อากาศดี! คุณสบายดีไหม?",
            "ประโยคเดียวไม่มีเครื่องหมาย",
            "a" * 500,
            "หนึ่ง. สอง. สาม.",
        ]:
            assert tts.split_sentences(text) == provider.split_sentences(text), text


# --------------------------------------------------------------------------
# PCM conversion
# --------------------------------------------------------------------------

class TestToInt16:
    def test_full_scale_maps_to_int16_range(self):
        out = tts._to_int16(np.array([1.0, -1.0, 0.0], dtype=np.float32))
        assert out.dtype == np.dtype("<i2")
        assert out[0] == 32767
        assert out[1] == -32767
        assert out[2] == 0

    def test_overshoot_is_normalised_not_wrapped(self):
        """A value above 1.0 must scale down, never wrap to a loud negative."""
        out = tts._to_int16(np.array([2.0, -1.0, 0.5], dtype=np.float32))
        assert out.max() <= 32767
        assert out.min() >= -32768
        assert out[0] > 0          # the +2.0 sample stays positive
        assert out[0] == 32767     # and becomes the new peak

    def test_empty_array_is_safe(self):
        assert tts._to_int16(np.array([], dtype=np.float32)).size == 0

    def test_multidimensional_input_is_flattened(self):
        out = tts._to_int16(np.zeros((2, 8), dtype=np.float32))
        assert out.shape == (16,)


# --------------------------------------------------------------------------
# resampling
# --------------------------------------------------------------------------

class TestResample:
    def test_same_rate_is_a_passthrough(self):
        src = np.linspace(-1, 1, 100, dtype=np.float32)
        assert np.array_equal(tts._resample(src, 16000, 16000), src)

    def test_24k_to_16k_shortens_by_two_thirds(self):
        src = np.zeros(2400, dtype=np.float32)     # 100 ms at 24 kHz
        out = tts._resample(src, 24000, 16000)
        assert out.size == 1600                    # 100 ms at 16 kHz

    def test_duration_is_preserved(self):
        """The whole point: 1 second in must stay 1 second out."""
        for dst in (8000, 16000, 22050, 44100):
            out = tts._resample(np.zeros(24000, dtype=np.float32), 24000, dst)
            assert abs(out.size / dst - 1.0) < 0.001, dst

    def test_a_sine_keeps_its_frequency(self):
        """Catches an off-by-one in the index mapping, which pitch-shifts audio."""
        rate_in, rate_out, freq = 24000, 16000, 440.0
        t = np.arange(rate_in, dtype=np.float32) / rate_in
        src = np.sin(2 * np.pi * freq * t).astype(np.float32)
        out = tts._resample(src, rate_in, rate_out)
        peak_bin = int(np.argmax(np.abs(np.fft.rfft(out))))
        detected = peak_bin * rate_out / out.size
        assert abs(detected - freq) < 5.0, f"got {detected} Hz"

    def test_empty_input_is_safe(self):
        assert tts._resample(np.array([], dtype=np.float32), 24000, 16000).size == 0


# --------------------------------------------------------------------------
# WAV framing
# --------------------------------------------------------------------------

class TestWavBytes:
    def test_header_matches_the_data(self):
        raw = tts._to_wav_bytes(np.zeros(2400, dtype=np.float32), 24000)
        with wave.open(BytesIO(raw), "rb") as w:
            assert w.getnchannels() == 1
            assert w.getsampwidth() == 2
            assert w.getframerate() == 24000
            assert w.getnframes() == 2400

    def test_output_is_a_riff_file(self):
        raw = tts._to_wav_bytes(np.zeros(16, dtype=np.float32), 16000)
        assert raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"


# --------------------------------------------------------------------------
# voice config validation
# --------------------------------------------------------------------------

class TestVoiceLoading:
    def _write(self, tmp_path, body: str, make_wav=True):
        if make_wav:
            with wave.open(str(tmp_path / "ref.wav"), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
                w.writeframes(b"\x00\x00" * 24000)
        cfg = tmp_path / "voices.yaml"
        cfg.write_text(body, encoding="utf-8")
        tts.VOICES_FILE = cfg
        return cfg

    def test_valid_config_loads(self, tmp_path):
        self._write(tmp_path, """
default: jarvis
voices:
  jarvis:
    ref_audio: ref.wav
    ref_text: "สวัสดีครับ"
    speed: 1.1
""")
        tts._load_voices()
        assert tts._default_voice == "jarvis"
        assert tts._voices["jarvis"]["speed"] == 1.1
        assert Path(tts._voices["jarvis"]["ref_audio"]).is_absolute()

    def test_missing_ref_audio_fails_at_boot(self, tmp_path):
        self._write(tmp_path, """
voices:
  jarvis:
    ref_audio: nope.wav
    ref_text: "x"
""", make_wav=False)
        with pytest.raises(SystemExit, match="ref_audio not found"):
            tts._load_voices()

    def test_blank_ref_text_fails_at_boot(self, tmp_path):
        self._write(tmp_path, """
voices:
  jarvis:
    ref_audio: ref.wav
    ref_text: "   "
""")
        with pytest.raises(SystemExit, match="ref_text is required"):
            tts._load_voices()

    def test_default_pointing_at_an_undefined_voice_fails(self, tmp_path):
        self._write(tmp_path, """
default: ghost
voices:
  jarvis:
    ref_audio: ref.wav
    ref_text: "x"
""")
        with pytest.raises(SystemExit, match="default voice"):
            tts._load_voices()

    def test_empty_voices_fails(self, tmp_path):
        self._write(tmp_path, "voices: {}\n")
        with pytest.raises(SystemExit, match="no voices defined"):
            tts._load_voices()

    def test_first_voice_is_the_default_when_unspecified(self, tmp_path):
        self._write(tmp_path, """
voices:
  only:
    ref_audio: ref.wav
    ref_text: "x"
""")
        tts._load_voices()
        assert tts._default_voice == "only"


# --------------------------------------------------------------------------
# host provider config
# --------------------------------------------------------------------------

class TestProviderConfig:
    def test_from_config_reads_the_token_from_env(self, monkeypatch):
        monkeypatch.setenv("MY_TTS_TOKEN", "s3cret")
        p = provider.F5TTSProvider.from_config({
            "url": "http://node:8769/", "voice_name": "jarvis",
            "token_env": "MY_TTS_TOKEN", "speed": 1.2,
        })
        assert p.token == "s3cret"
        assert p.url == "http://node:8769"      # trailing slash stripped
        assert p.speed == 1.2

    def test_missing_token_env_is_not_fatal(self, monkeypatch):
        monkeypatch.delenv("ABSENT_TOKEN", raising=False)
        p = provider.F5TTSProvider.from_config(
            {"url": "http://node:8769", "token_env": "ABSENT_TOKEN"})
        assert p.token == ""
        assert "X-Jarvis-Token" not in p._headers()

    def test_token_is_sent_when_present(self):
        p = provider.F5TTSProvider(url="http://n", token="abc")
        assert p._headers()["X-Jarvis-Token"] == "abc"

    def test_a_failing_sentence_does_not_kill_the_stream(self, monkeypatch):
        """One bad sentence should cost one sentence, not the whole reply."""
        p = provider.F5TTSProvider(url="http://n", pause_ms=0)
        calls = []

        def fake(text):
            calls.append(text)
            if "boom" in text:
                raise RuntimeError("TTS 500")
            return b"\x01\x02"

        monkeypatch.setattr(p, "synthesize", fake)
        out = list(p.stream("one. boom. three."))
        assert len(calls) == 3        # all three attempted
        assert out == [b"\x01\x02", b"\x01\x02"]   # two survived

    def test_pause_goes_between_sentences_not_after(self, monkeypatch):
        """A trailing pause would delay the end of every turn for no reason."""
        p = provider.F5TTSProvider(url="http://n", pause_ms=100, sample_rate=16000)
        monkeypatch.setattr(p, "synthesize", lambda t: b"\x01\x02")
        out = list(p.stream("one. two. three."))
        assert len(out) == 5                       # audio, pause, audio, pause, audio
        assert out[0] == out[2] == out[4] == b"\x01\x02"
        assert out[1] == out[3] == b"\x00\x00" * 1600   # 100 ms at 16 kHz
        assert out[-1] != out[1], "must not end on a pause"

    def test_a_skipped_sentence_does_not_leave_a_double_pause(self, monkeypatch):
        p = provider.F5TTSProvider(url="http://n", pause_ms=100, sample_rate=16000)

        def fake(text):
            if "boom" in text:
                raise RuntimeError("TTS 500")
            return b"\x01\x02"

        monkeypatch.setattr(p, "synthesize", fake)
        out = list(p.stream("one. boom. three."))
        assert out == [b"\x01\x02", b"\x00\x00" * 1600, b"\x01\x02"]

    def test_pause_disabled_yields_only_audio(self, monkeypatch):
        p = provider.F5TTSProvider(url="http://n", pause_ms=0)
        monkeypatch.setattr(p, "synthesize", lambda t: b"\x01\x02")
        assert list(p.stream("one. two.")) == [b"\x01\x02", b"\x01\x02"]


# --------------------------------------------------------------------------
# silence trimming + level normalisation
# --------------------------------------------------------------------------

class TestPostProcess:
    """The model pads every utterance; concatenating sentences stacks the pads."""

    def _clip(self, sr=24000, lead=0.4, body=0.5, trail=0.3, amp=0.8):
        t = np.arange(int(body * sr)) / sr
        speech = (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        return np.concatenate([
            np.zeros(int(lead * sr), dtype=np.float32),
            speech,
            np.zeros(int(trail * sr), dtype=np.float32),
        ])

    def test_padding_is_removed(self):
        sr = 24000
        out = tts._trim_silence(self._clip(sr), sr)
        # 0.5s of speech plus the keep margin on each side, nothing like 1.2s
        assert 0.5 <= out.size / sr <= 0.5 + 2 * (tts.TRIM_KEEP_MS / 1000) + 0.05

    def test_speech_itself_survives(self):
        sr = 24000
        out = tts._trim_silence(self._clip(sr), sr)
        assert np.abs(out).max() > 0.7, "trimmed into the speech"

    def test_a_quiet_clip_is_not_trimmed_to_nothing(self):
        """The floor is relative to the clip's own peak, not an absolute level."""
        sr = 24000
        out = tts._trim_silence(self._clip(sr, amp=0.05), sr)
        assert out.size / sr > 0.4

    def test_silence_only_input_is_returned_untouched(self):
        sr = 24000
        arr = np.zeros(sr, dtype=np.float32)
        assert tts._trim_silence(arr, sr).size == arr.size

    def test_empty_input_is_safe(self):
        assert tts._trim_silence(np.array([], dtype=np.float32), 24000).size == 0

    def test_normalise_brings_sentences_to_one_level(self):
        loud = tts._normalize(np.array([0.95, -0.95], dtype=np.float32))
        quiet = tts._normalize(np.array([0.20, -0.20], dtype=np.float32))
        assert abs(float(np.abs(loud).max()) - float(np.abs(quiet).max())) < 1e-5

    def test_normalise_leaves_headroom(self):
        out = tts._normalize(np.array([1.0, -1.0], dtype=np.float32))
        assert float(np.abs(out).max()) <= 1.0


# --------------------------------------------------------------------------
# torchaudio decoder shim
# --------------------------------------------------------------------------

class TestAudioBackendShim:
    """The shim exists because torchaudio >= 2.9 needs FFmpeg it may not have.

    Getting the axis order wrong here is silent: the library indexes audio[0]
    for the first channel, so a transposed return feeds it one *sample* across
    all channels instead of one channel — which synthesises noise rather than
    raising.
    """

    def _stereo_wav(self, tmp_path):
        path = tmp_path / "ref.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(22050)
            # left = +0.5 full-scale, right = silence, so the axes are telling apart
            frames = b"".join(b"\x00\x40" + b"\x00\x00" for _ in range(1000))
            w.writeframes(frames)
        return path

    def test_shim_returns_channels_first(self, tmp_path, monkeypatch):
        pytest.importorskip("torch")
        pytest.importorskip("soundfile")
        import torchaudio

        monkeypatch.setitem(sys.modules, "torchcodec", None)   # force the fallback
        real_load = torchaudio.load
        try:
            tts._ensure_audio_backend()
            data, rate = torchaudio.load(str(self._stereo_wav(tmp_path)))
            assert rate == 22050
            assert data.shape == (2, 1000), "must be (channels, samples)"
            assert data[0].abs().max() > 0.1, "channel 0 should be the loud one"
            assert data[1].abs().max() == 0.0, "channel 1 should be silent"
        finally:
            torchaudio.load = real_load

    def test_shim_is_a_noop_when_torchcodec_exists(self, monkeypatch):
        pytest.importorskip("torch")
        import torchaudio

        monkeypatch.setitem(sys.modules, "torchcodec", object())
        sentinel = object()
        real_load = torchaudio.load
        try:
            torchaudio.load = sentinel
            tts._ensure_audio_backend()
            assert torchaudio.load is sentinel, "must not patch when torchcodec is present"
        finally:
            torchaudio.load = real_load
