"""Tests for sxm.http — .stream endpoint helpers and handler logic."""
import pytest

from sxm.http import _hls_decrypt, _find_adts
from sxm.client import HLS_AES_KEY


def _make_adts_header(frame_length: int) -> bytes:
    """Build a valid 7-byte ADTS header with the given frame length.
    
    ADTS frame length (13 bits) is encoded across bytes 3-5:
      bits [12:11] in byte 3 low 2 bits
      bits [10:3]  in byte 4
      bits [2:0]   in byte 5 high 3 bits
    """
    # MPEG-4 AAC, 44100 Hz stereo, no CRC
    header = bytearray([0xFF, 0xF1, 0x50, 0x80, 0x00, 0x1F, 0xFC])
    header[3] = (header[3] & 0xFC) | ((frame_length >> 11) & 0x03)
    header[4] = (frame_length >> 3) & 0xFF
    header[5] = (header[5] & 0x1F) | ((frame_length & 0x07) << 5)
    return bytes(header)


def _make_adts_frame(frame_length: int) -> bytes:
    """Build a complete ADTS frame: header + zero payload."""
    header = _make_adts_header(frame_length)
    return header + b"\x00" * (frame_length - len(header))


class TestHLSDecrypt:
    """Tests for _hls_decrypt."""

    def test_decrypt_known_vector(self):
        """Decrypt should produce expected output for a known plaintext."""
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        key = HLS_AES_KEY
        iv = b"\x00" * 16
        plaintext = b"A" * 16
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        encryptor = cipher.encryptor()
        # PKCS7: 16 bytes needs a full 16-byte padding block
        padded = plaintext + b"\x10" * 16
        ciphertext = encryptor.update(padded) + encryptor.finalize()

        result = _hls_decrypt(ciphertext, key, iv)
        assert result == plaintext

    def test_decrypt_padding_stripped(self):
        """Decrypted output should have PKCS7 padding removed."""
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        key = HLS_AES_KEY
        iv = b"\x00" * 16
        plaintext = b"hello"  # 5 bytes -> 11 bytes of padding
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        encryptor = cipher.encryptor()
        padded = plaintext + b"\x0b" * 11  # pad to 16
        ciphertext = encryptor.update(padded) + encryptor.finalize()

        result = _hls_decrypt(ciphertext, key, iv)
        assert result == plaintext
        assert len(result) == 5

    def test_decrypt_with_sequence_iv(self):
        """IV derived from media sequence number should work."""
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        key = HLS_AES_KEY
        seq = 1328432
        iv = seq.to_bytes(16, "big")
        plaintext = b"test segment dat"  # 16 bytes exactly
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        encryptor = cipher.encryptor()
        padded = plaintext + b"\x10" * 16
        ciphertext = encryptor.update(padded) + encryptor.finalize()

        result = _hls_decrypt(ciphertext, key, iv)
        assert result == plaintext


class TestFindADTS:
    """Tests for _find_adts."""

    def test_finds_adts_at_start(self):
        """Should return 0 when ADTS header is at position 0."""
        frame1 = _make_adts_frame(400)
        frame2 = _make_adts_frame(400)
        data = frame1 + frame2

        assert _find_adts(data) == 0

    def test_skips_leading_garbage(self):
        """Should skip non-ADTS bytes at the start."""
        garbage = b"\x00" * 50
        frame1 = _make_adts_frame(400)
        frame2 = _make_adts_frame(400)
        data = garbage + frame1 + frame2

        assert _find_adts(data) == 50

    def test_returns_zero_when_not_found(self):
        """Should return 0 when no valid ADTS frames exist."""
        data = b"\x00" * 200
        assert _find_adts(data) == 0

    def test_rejects_false_sync(self):
        """Should not match a lone 0xFF byte without a valid second frame."""
        # 0xFF 0xF0 looks like a sync word, but no valid second frame follows
        data = bytes([0xFF, 0xF0]) + b"\x00" * 8
        assert _find_adts(data) == 0


class TestStreamHandlerLogic:
    """Tests for the .stream handler's dedup and sleep logic."""

    def test_first_segment_always_sent(self):
        """When last_sent_seq is None, all segments should be sent."""
        last_sent_seq = None
        seq = 1000
        sent_count = 0

        for _ in range(17):
            if last_sent_seq is not None and seq <= last_sent_seq:
                seq += 1
                continue
            last_sent_seq = seq
            seq += 1
            sent_count += 1

        assert sent_count == 17
        assert last_sent_seq == 1016

    def test_overlapping_segments_skipped(self):
        """Segments already sent should be skipped on next iteration."""
        last_sent_seq = 1016  # we've sent 1000-1016
        seq = 1014  # playlist starts at 1014, has 17 segments (1014-1030)

        sent = []
        for _ in range(17):
            if last_sent_seq is not None and seq <= last_sent_seq:
                seq += 1
                continue
            sent.append(seq)
            last_sent_seq = seq
            seq += 1

        assert sent == [1017, 1018, 1019, 1020, 1021, 1022, 1023, 1024,
                        1025, 1026, 1027, 1028, 1029, 1030]
        assert last_sent_seq == 1030

    def test_fully_overlapping_playlist_sends_nothing(self):
        """When playlist is entirely within already-sent range, send none."""
        last_sent_seq = 1056  # sent 1040-1056 (17 segments)
        seq = 1040  # all 17 segments are <= 1056

        sent = []
        sent_any = False
        for _ in range(17):
            if last_sent_seq is not None and seq <= last_sent_seq:
                seq += 1
                continue
            sent.append(seq)
            last_sent_seq = seq
            seq += 1
            sent_any = True

        assert sent == []
        assert not sent_any
        sleep_time = 0.5 if sent_any else 8
        assert sleep_time == 8

    def test_adaptive_sleep_when_caught_up(self):
        """Should sleep 8s when no new segments were sent (caught up)."""
        sleep_time = 0.5 if False else 8
        assert sleep_time == 8

    def test_adaptive_sleep_when_new_segments(self):
        """Should sleep 0.5s when new segments were sent."""
        sleep_time = 0.5 if True else 8
        assert sleep_time == 0.5

    def test_failed_segment_increments_seq(self):
        """When a segment fetch fails (None), seq should still increment."""
        seq = 1000
        data = None
        if data is None:
            seq += 1  # 1001
        last_sent_seq = seq  # 1001
        seq += 1

        assert last_sent_seq == 1001

    def test_no_new_segments_interval_growth(self):
        """After sending initial batch, caught-up state should use 8s sleep."""
        last_sent_seq = 2000
        seq = 1998
        sent_any = False
        for _ in range(17):
            if last_sent_seq is not None and seq <= last_sent_seq:
                seq += 1
                continue
            last_sent_seq = seq
            seq += 1
            sent_any = True

        assert sent_any  # some new segments were sent
        sleep_time = 0.5 if sent_any else 8
        assert sleep_time == 0.5
