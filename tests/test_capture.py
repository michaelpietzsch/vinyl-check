"""Capture: channel selection, sample decoding, listening in, errors as i18n messages."""
import numpy as np
import pytest

from vinylcheck import capture, i18n
from vinylcheck.config import _build, Config


def test_channel_pick_default_is_first_channels():
    assert capture.channel_pick(2) == (2, [0, 1])


def test_channel_pick_four_channel_interface():
    # e.g. a Soundcraft Notepad-12FX: only 4 channels at the hw: device, channels 1/2 = left/right
    assert capture.channel_pick(2, 4, (1, 2)) == (4, [0, 1])
    assert capture.channel_pick(2, 4, (3, 4)) == (4, [2, 3])


@pytest.mark.parametrize("cap,pick", [(4, (1,)), (4, (1, 5)), (2, (1, 3)), (4, (0, 1))])
def test_channel_pick_rejects_bad_mapping(cap, pick):
    with pytest.raises(RuntimeError):
        capture.channel_pick(2, cap, pick)


def test_capture_error_is_a_message_rendered_per_language():
    with pytest.raises(capture.CaptureError) as ei:
        capture.channel_pick(2, 4, (1, 5))
    assert ei.value.message["$t"] == "capture.channel_mismatch"
    assert str(ei.value) == "input_channels [1, 5] does not match channels = 2 and capture_channels = 4"
    with i18n.use_lang("de"):
        assert str(ei.value).startswith("input_channels [1, 5] passt nicht zu")


def test_decode_s32_four_channels_then_pick():
    frames = np.array([[1, 2, 3, 4], [-5, 6, -7, 8]], dtype="<i4") << 8   # 24 bit in a 32-bit container
    a = capture._decode(frames.tobytes(), "S32_LE", 4)
    _, idx = capture.channel_pick(2, 4, (1, 2))
    assert a[:, idx].tolist() == [[1 << 8, 2 << 8], [-5 << 8, 6 << 8]]


def test_config_reads_input_channels_as_tuple():
    cfg = _build(Config, {"capture_channels": 4, "input_channels": [1, 2]})
    assert cfg.capture_channels == 4 and cfg.input_channels == (1, 2)


class _Src:
    def __init__(self, chunks):
        self.chunks, self.xruns, self.errors = list(chunks), 0, []

    def read(self, frames):
        return self.chunks.pop(0) if self.chunks else None

    def close(self):
        pass


def test_tap_streams_16bit_pcm_and_ends_with_none():
    """Listening in: 24-bit values in a 32-bit container arrive as 16 bit, None at the end."""
    data = (np.arange(24, dtype=np.int64).reshape(-1, 2) - 12) << 16
    rec = capture.Recorder(lambda dev, rate, ch, fmt: _Src([data[:6], data[6:]]))
    q = rec.tap()
    rec.start(device="x", rate=48000, channels=2, fmt="S32_LE", path=None, max_s=10)
    got = []
    while (item := q.get(timeout=5)) is not None:
        got.append(item)
    rec.stop()
    assert b"".join(got) == (data >> 16).astype("<i2").tobytes()
    rec.untap(q)
    assert not rec._taps


def test_second_start_is_refused_while_running():
    rec = capture.Recorder(lambda dev, rate, ch, fmt: _Src([]))
    rec.state = {"active": True}                                     # as while a recording is running
    with pytest.raises(capture.CaptureError) as ei:
        rec.start(device="x", rate=48000, channels=2, fmt="S16_LE", path=None, max_s=10)
    assert ei.value.message == {"$t": "capture.busy"} and "already running" in str(ei.value)
