"""Minimal LZMA2 token encoder used to assemble 7z quine archives.

The encoder only supports the tokens the quine builder needs: a normal match
and rep0 matches.  Match encoding bytes depend only on (distance, length,
output position), never on the literal history, which is what lets the builder
compute the archive layout before the payload bytes are known.

Port of the validated prototype in ``.omo-tmp/spike/sevenzip_double.py``.
"""

from __future__ import annotations

import lzma

# ---------------------------------------------------------------------------
# LZMA range coder constants
# ---------------------------------------------------------------------------

K_NUM_BIT_MODEL_TOTAL_BITS = 11
K_BIT_MODEL_TOTAL = 1 << K_NUM_BIT_MODEL_TOTAL_BITS
K_NUM_MOVE_BITS = 5
K_TOP_VALUE = 1 << 24

K_NUM_STATES = 12
K_NUM_LIT_STATES = 7
K_NUM_POS_BITS_MAX = 4
K_NUM_LEN_TO_POS_STATES = 4
K_NUM_ALIGN_BITS = 4
K_END_POS_MODEL_INDEX = 14
K_NUM_FULL_DISTANCES = 1 << (K_END_POS_MODEL_INDEX >> 1)
K_MATCH_MIN_LEN = 2
K_NUM_LOW_LEN_BITS = 3
K_NUM_MID_LEN_BITS = 3
K_NUM_HIGH_LEN_BITS = 8
K_NUM_LOW_LEN_SYMBOLS = 1 << K_NUM_LOW_LEN_BITS
K_NUM_MID_LEN_SYMBOLS = 1 << K_NUM_MID_LEN_BITS
K_NUM_POS_SLOT_BITS = 6

PROB_INIT = K_BIT_MODEL_TOTAL >> 1

# LZMA2 / 7z layout constants
PROPS_BYTE = 0x5D  # lc=3, lp=0, pb=2
CHUNK = 65536  # max LZMA2 uncompressed chunk payload
LZ2_MAX_UNPACKED = 1 << 21  # max output of a single LZMA chunk
MAX_MATCH = 273  # max LZMA1 match length


class RangeEncoder:
    """LZMA range encoder. ``low`` is 64 bits so it can hold a carry."""

    def __init__(self) -> None:
        self.low = 0
        self.range = 0xFFFFFFFF
        self.cache = 0
        self.cache_size = 1  # initial 1 -> first emitted byte is always 0x00
        self.buf = bytearray()

    def _shift_low(self) -> None:
        if (self.low & 0xFFFFFFFF) < 0xFF000000 or (self.low >> 32) != 0:
            temp = self.cache
            while True:
                self.buf.append((temp + (self.low >> 32)) & 0xFF)
                temp = 0xFF
                self.cache_size -= 1
                if self.cache_size == 0:
                    break
            self.cache = (self.low >> 24) & 0xFF
        self.cache_size += 1
        # The C implementation truncates (UInt32)low << 8 back to 32 bits.
        self.low = ((self.low & 0xFFFFFFFF) << 8) & 0xFFFFFFFF

    def encode_bit(self, probs: list[int], idx: int, bit: int) -> None:
        p = probs[idx]
        bound = (self.range >> K_NUM_BIT_MODEL_TOTAL_BITS) * p
        if bit == 0:
            self.range = bound
            probs[idx] = p + ((K_BIT_MODEL_TOTAL - p) >> K_NUM_MOVE_BITS)
        else:
            self.low += bound
            self.range -= bound
            probs[idx] = p - (p >> K_NUM_MOVE_BITS)
        while self.range < K_TOP_VALUE:
            self.range = (self.range << 8) & 0xFFFFFFFF
            self._shift_low()

    def encode_direct_bits(self, value: int, num_bits: int) -> None:
        for i in range(num_bits - 1, -1, -1):
            self.range >>= 1
            if (value >> i) & 1:
                self.low += self.range
            while self.range < K_TOP_VALUE:
                self.range = (self.range << 8) & 0xFFFFFFFF
                self._shift_low()

    def bittree_encode(
        self, probs: list[int], off: int, num_bits: int, symbol: int
    ) -> None:
        """Plain bit tree, MSB first, node indexes start at 1."""
        m = 1
        for i in range(num_bits - 1, -1, -1):
            bit = (symbol >> i) & 1
            self.encode_bit(probs, off + m, bit)
            m = (m << 1) | bit

    def bittree_reverse_encode(
        self, probs: list[int], off: int, num_bits: int, symbol: int
    ) -> None:
        """Reverse bit tree, LSB first, used by SpecPos / Align."""
        m = 1
        for _ in range(num_bits):
            bit = symbol & 1
            symbol >>= 1
            self.encode_bit(probs, off + m, bit)
            m = (m << 1) | bit

    def finish(self) -> bytes:
        for _ in range(5):
            self._shift_low()
        return bytes(self.buf)


def _pos_slot_and_bits(distance: int) -> tuple[int, int, int]:
    """Split 0-based distance into (posSlot, numDirectBits, lowBits)."""
    if distance < 4:
        return distance, 0, 0
    for slot in range(4, 64):
        n = (slot >> 1) - 1
        base = (2 | (slot & 1)) << n
        if distance < base + (1 << n):
            return slot, n, distance - base
    raise ValueError(f"distance too large: {distance}")


class LzmaEncoder:
    """Encodes match / rep0 tokens without tracking output history."""

    def __init__(self, pb: int = 2) -> None:
        self.pb = pb
        self.pos_mask = (1 << pb) - 1
        self.rc = RangeEncoder()
        self.pos = 0
        self.state = 0
        n = PROB_INIT
        self.p_is_match = [n] * (K_NUM_STATES << K_NUM_POS_BITS_MAX)
        self.p_is_rep = [n] * K_NUM_STATES
        self.p_is_rep_g0 = [n] * K_NUM_STATES
        self.p_rep0_long = [n] * (K_NUM_STATES << K_NUM_POS_BITS_MAX)
        self.p_pos_slot = [n] * (K_NUM_LEN_TO_POS_STATES << K_NUM_POS_SLOT_BITS)
        self.p_spec_pos = [n] * (K_NUM_FULL_DISTANCES - K_END_POS_MODEL_INDEX)
        self.p_align = [n] * (1 << K_NUM_ALIGN_BITS)
        self.p_len_choice = [n] * 2
        self.p_len_low = [n] * (16 * K_NUM_LOW_LEN_SYMBOLS)
        self.p_len_mid = [n] * (16 * K_NUM_MID_LEN_SYMBOLS)
        self.p_len_high = [n] * (1 << K_NUM_HIGH_LEN_BITS)
        self.p_rep_len_choice = [n] * 2
        self.p_rep_len_low = [n] * (16 * K_NUM_LOW_LEN_SYMBOLS)
        self.p_rep_len_mid = [n] * (16 * K_NUM_MID_LEN_SYMBOLS)
        self.p_rep_len_high = [n] * (1 << K_NUM_HIGH_LEN_BITS)

    @property
    def pos_state(self) -> int:
        return self.pos & self.pos_mask

    def _encode_len(
        self,
        choice: list[int],
        low: list[int],
        mid: list[int],
        high: list[int],
        length: int,
    ) -> None:
        ps = self.pos_state
        remaining = length - K_MATCH_MIN_LEN
        if remaining < K_NUM_LOW_LEN_SYMBOLS:
            self.rc.encode_bit(choice, 0, 0)
            self.rc.bittree_encode(
                low, ps << K_NUM_LOW_LEN_BITS, K_NUM_LOW_LEN_BITS, remaining
            )
            return
        self.rc.encode_bit(choice, 0, 1)
        remaining -= K_NUM_LOW_LEN_SYMBOLS
        if remaining < K_NUM_MID_LEN_SYMBOLS:
            self.rc.encode_bit(choice, 1, 0)
            self.rc.bittree_encode(
                mid, ps << K_NUM_MID_LEN_BITS, K_NUM_MID_LEN_BITS, remaining
            )
            return
        self.rc.encode_bit(choice, 1, 1)
        self.rc.bittree_encode(
            high, 0, K_NUM_HIGH_LEN_BITS, remaining - K_NUM_MID_LEN_SYMBOLS
        )

    def _write_distance(self, distance: int, len_to_pos_state: int) -> None:
        d = distance - 1
        slot, n, low_bits = _pos_slot_and_bits(d)
        self.rc.bittree_encode(
            self.p_pos_slot,
            len_to_pos_state << K_NUM_POS_SLOT_BITS,
            K_NUM_POS_SLOT_BITS,
            slot,
        )
        if slot < 4:
            return
        if slot < K_END_POS_MODEL_INDEX:
            base = (2 | (slot & 1)) << n
            off = base - slot - 1
            self.rc.bittree_reverse_encode(self.p_spec_pos, off, n, low_bits)
            return
        self.rc.encode_direct_bits(low_bits >> K_NUM_ALIGN_BITS, n - K_NUM_ALIGN_BITS)
        self.rc.bittree_reverse_encode(
            self.p_align, 0, K_NUM_ALIGN_BITS, low_bits & ((1 << K_NUM_ALIGN_BITS) - 1)
        )

    def match(self, distance: int, length: int) -> None:
        assert K_MATCH_MIN_LEN <= length <= MAX_MATCH, length
        ps = self.pos_state
        self.rc.encode_bit(
            self.p_is_match, (self.state << K_NUM_POS_BITS_MAX) + ps, 1
        )
        self.rc.encode_bit(self.p_is_rep, self.state, 0)
        len_to_pos_state = min(length - K_MATCH_MIN_LEN, K_NUM_LEN_TO_POS_STATES - 1)
        self._encode_len(
            self.p_len_choice,
            self.p_len_low,
            self.p_len_mid,
            self.p_len_high,
            length,
        )
        self._write_distance(distance, len_to_pos_state)
        self.state = 7 if self.state < K_NUM_LIT_STATES else 10
        self.pos += length

    def rep_match(self, length: int) -> None:
        """rep0 match: reuses the most recent normal-match distance."""
        assert K_MATCH_MIN_LEN <= length <= MAX_MATCH, length
        ps = self.pos_state
        self.rc.encode_bit(
            self.p_is_match, (self.state << K_NUM_POS_BITS_MAX) + ps, 1
        )
        self.rc.encode_bit(self.p_is_rep, self.state, 1)
        self.rc.encode_bit(self.p_is_rep_g0, self.state, 0)
        self.rc.encode_bit(
            self.p_rep0_long, (self.state << K_NUM_POS_BITS_MAX) + ps, 1
        )
        self._encode_len(
            self.p_rep_len_choice,
            self.p_rep_len_low,
            self.p_rep_len_mid,
            self.p_rep_len_high,
            length,
        )
        self.state = 8 if self.state < K_NUM_LIT_STATES else 11
        self.pos += length

    def finish(self) -> bytes:
        return self.rc.finish()


# ---------------------------------------------------------------------------
# LZMA2 chunk helpers
# ---------------------------------------------------------------------------


Token = tuple[str, int] | tuple[str, int, int]


def store_hdr(payload_len: int, first: bool = False) -> bytes:
    """Header for an LZMA2 uncompressed chunk."""
    assert 1 <= payload_len <= CHUNK, payload_len
    return bytes([0x01 if first else 0x02]) + (payload_len - 1).to_bytes(2, "big")


def matches_for(distance: int, total: int) -> list[Token]:
    """One regular match followed by rep0 matches covering ``total`` bytes."""
    assert total >= 2, total
    tokens: list[Token] = []
    first = min(MAX_MATCH, total)
    if total - first == 1:
        first -= 1
    tokens.append(("m", distance, first))
    total -= first
    while total > 0:
        length = min(MAX_MATCH, total)
        if total - length == 1:
            length -= 1
        tokens.append(("r0", length))
        total -= length
    return tokens


def dict_prop_for(max_distance: int) -> tuple[int, int]:
    """Smallest LZMA2 dictionary property whose dictionary covers max_distance."""
    for prop in range(41):
        size = (2 | (prop & 1)) << (prop // 2 + 11)
        if size >= max_distance:
            return prop, size
    raise ValueError(f"distance too large: {max_distance}")


def lzma_chunk(tokens: list[Token], out_pos: int) -> tuple[bytes, int]:
    """Encode one 0xC0 LZMA2 chunk (state+props reset, no dict reset)."""
    enc = LzmaEncoder()
    enc.pos = out_pos
    total = 0
    for token in tokens:
        if token[0] == "m":
            enc.match(token[1], token[2])
            total += token[2]
        else:
            enc.rep_match(token[1])
            total += token[1]
    data = enc.finish()
    unpacked = total - 1
    compressed = len(data) - 1
    assert 0 <= unpacked < LZ2_MAX_UNPACKED, total
    assert 0 <= compressed < CHUNK, len(data)
    header = (
        bytes([0xC0 | ((unpacked >> 16) & 0x1F)])
        + (unpacked & 0xFFFF).to_bytes(2, "big")
        + (compressed & 0xFFFF).to_bytes(2, "big")
        + bytes([PROPS_BYTE])
    )
    return header + data, total


def lzma2_raw(data: bytes, dict_size: int, preset: int = 6) -> bytearray:
    """Raw LZMA2 compression without the trailing 0x00 end marker.

    Input is fed in blocks so the compressor never materialises a second full
    copy of the payload.
    """
    compressor = lzma.LZMACompressor(
        format=lzma.FORMAT_RAW,
        filters=[
            {"id": lzma.FILTER_LZMA2, "dict_size": dict_size, "preset": preset}
        ],
    )
    view = memoryview(data)
    out = bytearray()
    block = 1 << 20
    for offset in range(0, len(view), block):
        out += compressor.compress(view[offset : offset + block])
    out += compressor.flush()
    assert out and out[-1] == 0x00, "LZMA2 stream must end with 0x00"
    del out[-1]
    return out


def clear_dict_reset(data: bytearray) -> bytearray:
    """Drop the leading dictionary reset so this stream can follow other chunks."""
    if not data:
        return data
    control = data[0]
    if control >= 0xE0:
        control ^= 0x20
    elif control == 0x01:
        control = 0x02
    data[0] = control
    return data


def split_len(total: int, cap: int) -> list[int]:
    """Split ``total`` into pieces of at most ``cap`` bytes, each at least 2."""
    pieces: list[int] = []
    remaining = total
    while remaining > cap:
        pieces.append(cap)
        remaining -= cap
    if remaining == 1 and pieces:
        pieces[-1] -= 1
        remaining = 2
    pieces.append(remaining)
    return pieces


def seed_chunks(total: int, content_region: int, cap: int) -> list[int]:
    """Chunk the seed so the final compressed-content byte is never alone.

    The outer stream reproduces the compressed content from the seed copy with
    fixed-distance matches, and a match cannot be one byte long.  Forcing a
    chunk boundary two bytes before ``content_region`` guarantees that the last
    content piece has length two and stays inside a single seed chunk.
    """
    if content_region <= 2:
        return split_len(total, cap)
    boundary = content_region - 2
    pieces: list[int] = []
    remaining = boundary
    while remaining > cap:
        pieces.append(cap)
        remaining -= cap
    if remaining:
        if pieces and remaining == 1:
            if pieces[-1] > 2:
                pieces[-1] -= 1
                remaining = 2
            else:
                pieces[-1] += 1
                remaining = 0
        if remaining:
            pieces.append(remaining)
    pieces.extend(split_len(total - boundary, cap))
    return pieces