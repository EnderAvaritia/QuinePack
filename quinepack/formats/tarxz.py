"""Store-only tar.xz quine writer.

The archive is a standard single-block xz stream (one LZMA2 filter, CRC64
check) whose decompressed bytes are a POSIX ustar archive.  The tar members
are, in order:

1. the seed, at ``seed_name``;
2. the payload entries in ``Payload.entries`` order;
3. the self copy, at ``quine_name``, holding the entire xz stream.

Payload data is carried by LZMA2 uncompressed ("store") chunks, so the result
is a compatibility edition: it is self-containing and standard tools can read
it, but it does not promise compression (ADR-0002).

The construction is a port of the validated ``TarXzQuine`` prototype.  Names
longer than ustar's 100-byte name field are supported through the 155-byte
prefix field; a name that still does not fit raises ``PayloadError`` instead of
being silently truncated.
"""

from __future__ import annotations

import binascii
import ctypes
import ctypes.util
import io
import lzma
import struct
import tarfile

from ..errors import PayloadError
from ..lzma2 import (
    CHUNK,
    MAX_MATCH,
    dict_prop_for,
    lzma_chunk,
    matches_for,
    store_hdr,
)
from ..model import Payload, PayloadEntry
from .sevenzip import BuildResult

# ---------------------------------------------------------------------------
# xz format constants
# ---------------------------------------------------------------------------

XZ_MAGIC = b"\xFD7zXZ\x00"
XZ_FOOTER_MAGIC = b"YZ"
XZ_CHECK_CRC64 = 0x04
LZMA2_FILTER_ID = 0x21
CHECK_SIZE = 8
_XZ_FLAGS = bytes([0x00, XZ_CHECK_CRC64])

TAR_BLOCK = 512
SEED_NOTE = b"\nQuinePack seed"

# Difference between the xz block header size and the first chunk offset.
# The block header is 12 bytes for the single LZMA2 filter used here.
_BLOCK_HEADER_SIZE = 12


def _round_up_512(value: int) -> int:
    return (value + TAR_BLOCK - 1) // TAR_BLOCK * TAR_BLOCK


def _crc32(data: bytes) -> int:
    return binascii.crc32(data) & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# CRC64 (ECMA-182, reflected), with an optional liblzma fast path
# ---------------------------------------------------------------------------

_CRC64_POLY = 0xC96C5795D7870F42  # reflected ECMA-182
_CRC64_INIT = 0xFFFFFFFFFFFFFFFF
_CRC64_XOROUT = 0xFFFFFFFFFFFFFFFF
_CRC64_NON_REFLECTED = 0x142F0E1EBA9EA3693
_CRC64_MOD_BIT = 1 << 64

_liblzma = None
try:
    _lzma_name = ctypes.util.find_library("lzma")
    if _lzma_name:
        _candidate = ctypes.CDLL(_lzma_name)
        _candidate.lzma_crc64.argtypes = [
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_uint64,
        ]
        _candidate.lzma_crc64.restype = ctypes.c_uint64
        if _candidate.lzma_crc64(b"123456789", 9, 0) == 0x995DC9BBDF1939FA:
            _liblzma = _candidate
except (OSError, AttributeError):
    _liblzma = None


def _make_crc64_table() -> list[int]:
    table: list[int] = []
    for index in range(256):
        crc = index
        for _ in range(8):
            crc = (crc >> 1) ^ _CRC64_POLY if (crc & 1) else crc >> 1
        table.append(crc)
    return table


_CRC64_TABLE = _make_crc64_table()


def _crc64_pure(data: bytes) -> int:
    crc = _CRC64_INIT
    for byte in data:
        crc = (crc >> 8) ^ _CRC64_TABLE[(crc ^ byte) & 0xFF]
    return crc ^ _CRC64_XOROUT


def _crc64_lib(data: bytes) -> int:
    return int(_liblzma.lzma_crc64(data, len(data), 0))  # type: ignore[union-attr]


def _crc64(data: bytes) -> int:
    return _crc64_lib(data) if _liblzma is not None else _crc64_pure(data)


# ---------------------------------------------------------------------------
# GF(2^64) polynomial helpers for the CRC64 self-reference fixpoint
# ---------------------------------------------------------------------------


def _bit_reverse64(value: int) -> int:
    result = 0
    for _ in range(64):
        result = (result << 1) | (value & 1)
        value >>= 1
    return result


def _poly_mul_mod(left: int, right: int) -> int:
    result = 0
    while right:
        if right & 1:
            result ^= left
        right >>= 1
        left <<= 1
        if left & _CRC64_MOD_BIT:
            left ^= _CRC64_NON_REFLECTED
    return result


def _poly_pow(base: int, exponent: int) -> int:
    result = 1
    while exponent:
        if exponent & 1:
            result = _poly_mul_mod(result, base)
        base = _poly_mul_mod(base, base)
        exponent >>= 1
    return result


def _poly_minv(poly: int) -> int:
    """Inverse of ``poly`` modulo the CRC64 generator (extended Euclid)."""
    if poly == 0:
        return 0
    old_r, r = poly, _CRC64_NON_REFLECTED
    old_s, s = 1, 0
    while r:
        probe_r = r.bit_length() - 1
        remainder = old_r
        quotient = 0
        while remainder.bit_length() - 1 >= probe_r and remainder:
            shift = remainder.bit_length() - 1 - probe_r
            quotient |= 1 << shift
            remainder ^= r << shift
        old_r, r = r, remainder
        product = 0
        a, b = quotient, s
        while b:
            if b & 1:
                product ^= a
            b >>= 1
            a <<= 1
        old_s, s = s, old_s ^ product
    if old_r != 1:
        return 0
    probe_m = _CRC64_NON_REFLECTED.bit_length() - 1
    remainder = old_s
    while remainder.bit_length() - 1 >= probe_m and remainder:
        shift = remainder.bit_length() - 1 - probe_m
        remainder ^= _CRC64_NON_REFLECTED << shift
    return remainder


# ---------------------------------------------------------------------------
# tar primitives
# ---------------------------------------------------------------------------


def _split_ustar_name(name: bytes) -> tuple[bytes, bytes]:
    """Split a UTF-8 name into the ustar name and prefix fields."""
    if len(name) <= 100:
        return name, b""
    for index in range(len(name) - 1, -1, -1):
        if name[index : index + 1] != b"/":
            continue
        prefix, rest = name[:index], name[index + 1 :]
        if rest and len(rest) <= 100 and len(prefix) <= 155:
            return rest, prefix
    raise ValueError(
        "tar entry name is too long for POSIX ustar "
        f"(no path component fits the 100-byte name field): {name!r}"
    )


def _octal_field(value: int, digits: int, label: str) -> bytes:
    if value < 0 or value >= 8**digits:
        raise ValueError(f"{label} does not fit a {digits}-digit tar octal field: {value}")
    return b"%0*o\x00" % (digits, value)


def _tar_header(
    name: str,
    size: int,
    mtime: int,
    mode: int = 0o644,
    typeflag: bytes = b"0",
) -> bytes:
    """Build a 512-byte POSIX ustar header."""
    name_bytes = name.encode("utf-8")
    name_field, prefix_field = _split_ustar_name(name_bytes)

    header = bytearray(TAR_BLOCK)
    header[0 : len(name_field)] = name_field
    header[100:108] = _octal_field(mode & 0o7777, 7, "mode")
    header[108:116] = _octal_field(0, 7, "uid")
    header[116:124] = _octal_field(0, 7, "gid")
    header[124:136] = _octal_field(size, 11, "size")
    header[136:148] = _octal_field(mtime, 11, "mtime")
    header[148:156] = b"        "
    header[156:157] = typeflag
    if prefix_field:
        header[345 : 345 + len(prefix_field)] = prefix_field
    header[257:263] = b"ustar\x00"
    header[263:265] = b"00"

    checksum = sum(header) & 0o7777777
    header[148:156] = b"%06o\x00 " % checksum
    return bytes(header)


# ---------------------------------------------------------------------------
# xz primitives
# ---------------------------------------------------------------------------


def _xz_varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def _xz_stream_header() -> bytes:
    return XZ_MAGIC + _XZ_FLAGS + struct.pack("<I", _crc32(_XZ_FLAGS))


def _xz_block_header(dict_size: int) -> tuple[bytes, int]:
    prop, real_dict = dict_prop_for(dict_size)
    filter_flags = bytes([LZMA2_FILTER_ID, 1, prop])
    content = bytes([0x00]) + filter_flags
    total = 1 + len(content) + 4
    content += b"\x00" * ((4 - total % 4) % 4)
    header_size = 1 + len(content) + 4
    assert header_size % 4 == 0, header_size
    crc = _crc32(bytes([header_size // 4 - 1]) + content)
    return bytes([header_size // 4 - 1]) + content + struct.pack("<I", crc), real_dict


def _xz_index(records: list[tuple[int, int]]) -> bytes:
    data = bytearray([0x00])
    data += _xz_varint(len(records))
    for unpadded, uncompressed in records:
        data += _xz_varint(unpadded)
        data += _xz_varint(uncompressed)
    data += b"\x00" * ((4 - (len(data) + 4) % 4) % 4)
    return bytes(data) + struct.pack("<I", _crc32(bytes(data)))


def _xz_stream_footer(backward_size: int) -> bytes:
    crc_data = struct.pack("<I", backward_size) + _XZ_FLAGS
    return struct.pack("<I", _crc32(crc_data)) + crc_data + XZ_FOOTER_MAGIC


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------


def _add_gear(
    chunks: list[dict[str, object]], output_pos: int, file_pos: int, q: int
) -> tuple[int, int]:
    """Append a store chunk plus a matching LZMA chunk.

    The pair reduces the file/output slip to ``len(compressed_block)``.
    """
    length = min(file_pos - (output_pos - q) + 3, CHUNK)
    chunks.append(
        {
            "kind": "store",
            "foff": file_pos,
            "size": 3 + length,
            "ooff": output_pos,
            "olen": length,
        }
    )
    output_pos += length
    file_pos += 3 + length
    block, unpacked = lzma_chunk(matches_for(length, length), output_pos)
    chunks.append(
        {
            "kind": "lzma",
            "foff": file_pos,
            "size": len(block),
            "ooff": output_pos,
            "olen": unpacked,
            "bytes": block,
        }
    )
    return output_pos + unpacked, file_pos + len(block)


def _forward_vac(archive: bytearray, vac: dict[str, object], q: int) -> None:
    start = int(vac["ooff"]) - q
    length = int(vac["olen"])
    archive[start : start + length] = archive[start + length : start + 2 * length]


# ---------------------------------------------------------------------------
# tar.xz quine builder
# ---------------------------------------------------------------------------


class _TarXzBuilder:
    def __init__(
        self,
        payload: Payload,
        quine_name: str,
        seed_name: str,
    ) -> None:
        self.quine_name = quine_name
        self.seed_name = seed_name
        self.entries: tuple[PayloadEntry, ...] = payload.entries
        self.files: tuple[tuple[str, bytes], ...] = tuple(
            (entry.path, entry.data or b"") for entry in payload.files
        )
        self.file_map = dict(self.files)
        self.content = payload.content
        self.generated_mtime = (
            self.entries[0].mtime_ns // 1_000_000_000 if self.entries else 0
        )

    # -- tar model ---------------------------------------------------------

    def _entry_mtime(self, entry: PayloadEntry) -> int:
        return entry.mtime_ns // 1_000_000_000

    def tar_entries(self, d_seed: int, xz_size: int) -> list[dict[str, object]]:
        entries: list[dict[str, object]] = []
        seed_header = _tar_header(
            self.seed_name, d_seed, self.generated_mtime, mode=0o644
        )
        entries.append(
            {
                "name": self.seed_name,
                "hdr": seed_header,
                "data_size": d_seed,
                "is_dir": False,
            }
        )

        for entry in self.entries:
            mtime = self._entry_mtime(entry)
            if entry.is_dir:
                header = _tar_header(
                    entry.path,
                    0,
                    mtime,
                    mode=entry.mode,
                    typeflag=b"5",
                )
                entries.append(
                    {
                        "name": entry.path,
                        "hdr": header,
                        "data_size": 0,
                        "is_dir": True,
                    }
                )
            else:
                data = entry.data or b""
                header = _tar_header(
                    entry.path,
                    len(data),
                    mtime,
                    mode=entry.mode,
                )
                entries.append(
                    {
                        "name": entry.path,
                        "hdr": header,
                        "data_size": len(data),
                        "is_dir": False,
                    }
                )

        quine_header = _tar_header(
            self.quine_name, xz_size, self.generated_mtime, mode=0o644
        )
        entries.append(
            {
                "name": self.quine_name,
                "hdr": quine_header,
                "data_size": xz_size,
                "is_dir": False,
            }
        )
        return entries

    def _compute_q(self, d_seed: int) -> int:
        """Offset of the xz stream inside the tar stream."""
        q = TAR_BLOCK + _round_up_512(d_seed)
        for entry in self.entries:
            q += TAR_BLOCK
            if not entry.is_dir:
                q += _round_up_512(len(entry.data or b""))
        q += TAR_BLOCK
        return q

    # -- layout ------------------------------------------------------------

    def _trim_layouts(
        self,
        base: list[dict[str, object]],
        output_gear: int,
        file_gear: int,
        q: int,
        plant_offset: int,
        d_seed: int,
    ) -> list[tuple[list[dict[str, object]], int, int, int, bytes]]:
        """Enumerate phase-shifting trim configurations.

        Each candidate is ``(chunks, output_before_jump, file_before_jump,
        jump_length, jump_block)`` sorted by resulting archive size.
        """
        slip = file_gear - (output_gear - q)
        candidates: list[tuple[list[dict[str, object]], int, int, int, bytes]] = []
        seen: set[tuple[int, int]] = set()

        for bare in range(0, slip + 4):
            chunks = list(base)
            output_pos, file_pos = output_gear, file_gear
            if bare:
                chunks.append(
                    {
                        "kind": "store",
                        "foff": file_pos,
                        "size": 3 + bare,
                        "ooff": output_pos,
                        "olen": bare,
                    }
                )
                output_pos += bare
                file_pos += 3 + bare

            output_pos, file_pos = _add_gear(chunks, output_pos, file_pos, q)

            remaining = file_pos - (output_pos - q)
            jump_length = max(2, remaining + 17)
            jump_block = None
            for _ in range(64):
                try:
                    jump_block, unpacked = lzma_chunk(
                        [("m", output_pos - plant_offset, jump_length)], output_pos
                    )
                except AssertionError:
                    jump_block = None
                    break
                next_length = remaining + 3 + len(jump_block)
                if next_length == jump_length:
                    break
                if next_length < 2 or next_length > MAX_MATCH:
                    jump_block = None
                    break
                jump_length = next_length
            if jump_block is None or unpacked != jump_length:
                continue
            if jump_length + plant_offset - TAR_BLOCK > d_seed:
                continue

            key = (output_pos + jump_length, file_pos + len(jump_block))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                (chunks, output_pos, file_pos, jump_length, jump_block)
            )

        candidates.sort(key=lambda item: item[2] + len(item[4]))
        return candidates

    def _solve_gadget(
        self,
        chunks_before: list[dict[str, object]],
        output_before_jump: int,
        file_before_jump: int,
        jump_length: int,
        jump_block: bytes,
        q: int,
        block_header_size: int,
        few: int,
        block_header: bytes,
        dict_size: int,
        header_a: bytes,
        header_b: bytes,
        d_seed: int,
        chunk_count: int,
    ) -> dict[str, object] | None:
        """Search a one-dimensional family for a self-consistent tail gadget."""
        output_prefix = output_before_jump + jump_length
        file_prefix = file_before_jump + len(jump_block)
        if file_prefix != (output_prefix - q) - 3:
            return None

        chunks = list(chunks_before)
        chunks.append(
            {
                "kind": "lzma",
                "foff": file_before_jump,
                "size": len(jump_block),
                "ooff": output_before_jump,
                "olen": jump_length,
                "bytes": jump_block,
                "jump": True,
            }
        )

        best: dict[str, object] | None = None
        for combined in range(8, 512):
            low = file_prefix + 2 * combined + 2059
            high = file_prefix + 2 * combined + 3595
            for pad_blocks in range(
                max(1, (low - 512) // 512), (high + 512) // 512 + 2
            ):
                numerator = pad_blocks * 512 - (
                    output_prefix + 2 * combined + 8 - q - 1024
                )
                if numerator % 3:
                    continue
                trailing = numerator // 3
                if trailing < 1024:
                    continue
                total = combined + trailing + 4
                if total < 2:
                    continue
                gadget_block, gadget_unpacked = lzma_chunk(
                    matches_for(total, total), output_prefix + total
                )
                gadget_size = len(gadget_block)
                suffix = combined - gadget_size
                if suffix < 1:
                    continue

                file_final = (
                    file_prefix + 3 + total + gadget_size + 3 + trailing + 1
                )
                n = file_final - (_BLOCK_HEADER_SIZE + block_header_size)
                block_padding = (
                    4
                    - (_BLOCK_HEADER_SIZE + block_header_size + n) % 4
                ) % 4
                unpadded = block_header_size + n + CHECK_SIZE
                index = _xz_index(
                    [(unpadded, q + pad_blocks * 512 + 1024)]
                )
                index_size = len(index)
                suffix_actual = block_padding + CHECK_SIZE + index_size + 12
                if suffix_actual != suffix:
                    continue
                xz_size = (
                    _BLOCK_HEADER_SIZE
                    + block_header_size
                    + n
                    + block_padding
                    + CHECK_SIZE
                    + index_size
                    + 12
                )
                xz_pad = _round_up_512(xz_size)
                if xz_pad != pad_blocks * 512:
                    continue
                total_w = q + xz_pad + 1024
                output_final = output_prefix + 2 * total + trailing
                if output_final != total_w:
                    continue
                if best is not None and xz_size >= int(best["xz_size"]):
                    continue

                backward_size = index_size // 4 - 1
                final = list(chunks)
                final.append(
                    {
                        "kind": "store",
                        "foff": file_prefix,
                        "size": 3 + total,
                        "ooff": output_prefix,
                        "olen": total,
                        "vac": True,
                    }
                )
                final.append(
                    {
                        "kind": "lzma",
                        "foff": file_prefix + 3 + total,
                        "size": gadget_size,
                        "ooff": output_prefix + total,
                        "olen": gadget_unpacked,
                        "bytes": gadget_block,
                    }
                )
                final.append(
                    {
                        "kind": "store",
                        "foff": file_prefix + 3 + total + gadget_size,
                        "size": 3 + trailing,
                        "ooff": output_prefix + 2 * total,
                        "olen": trailing,
                        "trailing": True,
                    }
                )
                final.append(
                    {
                        "kind": "term",
                        "foff": file_prefix
                        + 3
                        + total
                        + gadget_size
                        + 3
                        + trailing,
                        "size": 1,
                        "ooff": output_final,
                        "olen": 0,
                        "bytes": b"\x00",
                    }
                )
                best = {
                    "q": q,
                    "d_seed": d_seed,
                    "k": chunk_count,
                    "chunks": final,
                    "block_hdr": block_header,
                    "bh": block_header_size,
                    "n": n,
                    "xz_size": xz_size,
                    "T": total,
                    "suffix": suffix_actual,
                    "bp": block_padding,
                    "ix": index_size,
                    "dict_size": dict_size,
                    "FEW": few,
                    "hdrA": header_a,
                    "hdrB": header_b,
                    "jump_y": jump_length,
                    "index": index,
                    "stream_hdr": _xz_stream_header(),
                    "footer": _xz_stream_footer(backward_size),
                    "entries": self.tar_entries(d_seed, xz_size),
                }
        return best

    def _layout_iterate(self, d_seed: int) -> dict[str, object]:
        # The prefix-echo matches reach back by roughly twice the tar prefix.
        # q grows with the number of tar headers, not just file content, so the
        # dictionary must be sized from q; using len(content) alone corrupts
        # archives with many tiny entries.
        q = self._compute_q(d_seed)
        distance_hint = 2 * (q + _BLOCK_HEADER_SIZE + 12 + 3)
        block_header, dict_size = _xz_block_header(distance_hint)
        block_header_size = len(block_header)
        few = _BLOCK_HEADER_SIZE + block_header_size + 3
        header_a_offset = TAR_BLOCK + _BLOCK_HEADER_SIZE + block_header_size
        header_b_offset = header_a_offset + 3
        plant_offset = TAR_BLOCK + _BLOCK_HEADER_SIZE + block_header_size + 6

        # Consume the tar prefix plus the first few xz bytes into store chunks.
        base: list[dict[str, object]] = []
        output_pos, file_pos = 0, _BLOCK_HEADER_SIZE + block_header_size
        lengths: list[int] = []
        remaining = q + few
        while remaining > CHUNK:
            lengths.append(CHUNK)
            remaining -= CHUNK
        lengths.append(remaining)
        assert lengths[-1] >= 2, f"last prefix chunk too small: {lengths[-1]}"
        chunk_count = len(lengths)
        header_a = store_hdr(CHUNK, first=False)
        header_b = store_hdr(lengths[-1], first=False)

        covered = 0
        for index, length in enumerate(lengths):
            base.append(
                {
                    "kind": "store",
                    "foff": file_pos,
                    "size": 3 + length,
                    "ooff": output_pos,
                    "olen": length,
                    "first": index == 0,
                    "c1": True,
                    "S": covered,
                }
            )
            output_pos += length
            file_pos += 3 + length
            covered += length
        assert output_pos == q + few

        covered = 0
        for index, length in enumerate(lengths):
            if index > 0:
                source = (
                    header_a_offset
                    if (index < chunk_count - 1 or length == CHUNK)
                    else header_b_offset
                )
                distance = (
                    q + few + covered + 3 * (index - 1)
                ) - source
                block, unpacked = lzma_chunk(
                    [("m", distance, 3)], output_pos
                )
                base.append(
                    {
                        "kind": "lzma",
                        "foff": file_pos,
                        "size": len(block),
                        "ooff": output_pos,
                        "olen": unpacked,
                        "bytes": block,
                    }
                )
                output_pos += unpacked
                file_pos += len(block)
            block, unpacked = lzma_chunk(
                matches_for(q + few + 3 * index, length), output_pos
            )
            base.append(
                {
                    "kind": "lzma",
                    "foff": file_pos,
                    "size": len(block),
                    "ooff": output_pos,
                    "olen": unpacked,
                    "bytes": block,
                }
            )
            output_pos += unpacked
            file_pos += len(block)
            covered += length

        while file_pos - (output_pos - q) > 16:
            output_pos, file_pos = _add_gear(
                base, output_pos, file_pos, q
            )

        output_gear, file_gear = output_pos, file_pos

        tried = 0
        best: dict[str, object] | None = None
        since_hit = 0
        for (
            chunks_before,
            output_before_jump,
            file_before_jump,
            jump_length,
            jump_block,
        ) in self._trim_layouts(
            base, output_gear, file_gear, q, plant_offset, d_seed
        ):
            layout = self._solve_gadget(
                chunks_before,
                output_before_jump,
                file_before_jump,
                jump_length,
                jump_block,
                q,
                block_header_size,
                few,
                block_header,
                dict_size,
                header_a,
                header_b,
                d_seed,
                chunk_count,
            )
            tried += 1
            if layout is not None and (
                best is None or int(layout["xz_size"]) < int(best["xz_size"])
            ):
                best = layout
            if best is not None:
                since_hit += 1
                if since_hit >= 10:
                    break
            elif tried >= 80:
                break
        if best is not None:
            return best
        raise RuntimeError(
            "tar.xz layout did not converge "
            f"(q={q}, file_gear={file_gear}, output_gear={output_gear})"
        )

    # -- assembly ----------------------------------------------------------

    def assemble(self, layout: dict[str, object]) -> tuple[bytes, bytes]:
        archive = bytearray(int(layout["xz_size"]))
        q = int(layout["q"])
        d_seed = int(layout["d_seed"])
        block_header_size = int(layout["bh"])
        few = int(layout["FEW"])

        archive[0:_BLOCK_HEADER_SIZE] = layout["stream_hdr"]
        archive[
            _BLOCK_HEADER_SIZE : _BLOCK_HEADER_SIZE + block_header_size
        ] = layout["block_hdr"]

        jump_chunk = None
        for chunk in layout["chunks"]:
            if chunk["kind"] == "store":
                archive[chunk["foff"] : chunk["foff"] + 3] = store_hdr(
                    chunk["olen"], first=chunk.get("first", False)
                )
            else:
                archive[
                    chunk["foff"] : chunk["foff"] + chunk["size"]
                ] = chunk["bytes"]
                if chunk.get("jump"):
                    jump_chunk = chunk

        padding_offset = _BLOCK_HEADER_SIZE + block_header_size + int(layout["n"])
        index_offset = padding_offset + int(layout["bp"]) + CHECK_SIZE
        archive[index_offset : index_offset + int(layout["ix"])] = layout["index"]
        footer_offset = index_offset + int(layout["ix"])
        archive[footer_offset : footer_offset + 12] = layout["footer"]

        assert jump_chunk is not None

        jump_length = int(layout["jump_y"])
        plant = bytes(
            archive[
                jump_chunk["ooff"] - q : jump_chunk["ooff"] - q + jump_length
            ]
        )
        seed = bytearray(d_seed)
        seed[0:_BLOCK_HEADER_SIZE] = layout["stream_hdr"]
        seed[
            _BLOCK_HEADER_SIZE : _BLOCK_HEADER_SIZE + block_header_size
        ] = layout["block_hdr"]
        seed[
            _BLOCK_HEADER_SIZE
            + block_header_size : _BLOCK_HEADER_SIZE
            + block_header_size
            + 3
        ] = layout["hdrA"]
        seed[
            _BLOCK_HEADER_SIZE
            + block_header_size
            + 3 : _BLOCK_HEADER_SIZE
            + block_header_size
            + 6
        ] = layout["hdrB"]
        seed[
            _BLOCK_HEADER_SIZE
            + block_header_size
            + 6 : _BLOCK_HEADER_SIZE
            + block_header_size
            + 6
            + jump_length
        ] = plant
        padding = (
            SEED_NOTE
            * (d_seed // len(SEED_NOTE) + 2)
        )[: d_seed - _BLOCK_HEADER_SIZE - block_header_size - 6 - jump_length]
        seed[
            _BLOCK_HEADER_SIZE + block_header_size + 6 + jump_length :
        ] = padding

        tar_prefix = self._build_tar_prefix(layout, seed)
        prefix = tar_prefix + bytes(archive[0:few])
        assert len(prefix) == q + few, (
            f"tar prefix length {len(prefix)} != {q + few}"
        )

        for chunk in layout["chunks"]:
            if chunk["kind"] != "store":
                continue
            output_offset = int(chunk["ooff"])
            length = int(chunk["olen"])
            if chunk.get("c1"):
                archive[
                    chunk["foff"] + 3 : chunk["foff"] + 3 + length
                ] = prefix[chunk["S"] : chunk["S"] + length]
            elif chunk.get("trailing"):
                # The payload is the zero tail after the archive and the
                # bytearray is already zero-initialized.
                continue
            elif chunk.get("vac"):
                _forward_vac(archive, chunk, q)
            else:
                start = output_offset - q
                archive[
                    chunk["foff"] + 3 : chunk["foff"] + 3 + length
                ] = archive[start : start + length]

        self._solve_crc64(archive, layout, tar_prefix)
        return bytes(archive), bytes(seed)

    def _build_tar_prefix(
        self, layout: dict[str, object], seed: bytes
    ) -> bytes:
        prefix = bytearray()
        entries = layout["entries"]
        for entry in entries[:-1]:
            prefix += entry["hdr"]
            if not entry["is_dir"] and entry["data_size"] > 0:
                data = (
                    seed
                    if entry["name"] == self.seed_name
                    else self.file_map[entry["name"]]
                )
                prefix += data
                prefix += b"\x00" * (
                    _round_up_512(entry["data_size"]) - entry["data_size"]
                )
        prefix += entries[-1]["hdr"]
        return bytes(prefix)

    def _solve_crc64(
        self,
        archive: bytearray,
        layout: dict[str, object],
        tar_prefix: bytes,
    ) -> None:
        q = int(layout["q"])
        block_header_size = int(layout["bh"])
        n = int(layout["n"])
        block_padding = int(layout["bp"])
        xz_size = int(layout["xz_size"])
        total = int(layout["T"])

        crc_offset = (
            _BLOCK_HEADER_SIZE + block_header_size + n + block_padding
        )
        crc_w1 = q + crc_offset
        crc_copy_offset = crc_offset - total
        crc_w2 = q + crc_copy_offset

        xz_pad = _round_up_512(xz_size)
        trailing = xz_pad - xz_size + 1024
        stream = bytearray(tar_prefix) + archive + b"\x00" * trailing
        stream[crc_w1 : crc_w1 + CHECK_SIZE] = b"\x00" * CHECK_SIZE
        stream[crc_w2 : crc_w2 + CHECK_SIZE] = b"\x00" * CHECK_SIZE

        crc_poly = _bit_reverse64(_crc64(bytes(stream)))
        trailing1 = (len(stream) - crc_w1 - 8) * 8
        p1 = _poly_pow(2, 64 + trailing1)
        trailing2 = (len(stream) - crc_w2 - 8) * 8
        p2 = _poly_pow(2, 64 + trailing2)

        inverse = _poly_minv(1 ^ p1 ^ p2)
        if inverse == 0:
            raise RuntimeError("CRC64 polynomial has no inverse")
        value = _bit_reverse64(_poly_mul_mod(crc_poly, inverse))

        struct.pack_into("<Q", archive, crc_offset, value)
        vac = next(
            chunk for chunk in layout["chunks"] if chunk.get("vac")
        )
        _forward_vac(archive, vac, q)

        check = bytearray(tar_prefix) + archive + b"\x00" * trailing
        assert _crc64(bytes(check)) == value, "CRC64 fixpoint verification failed"

    # -- public build ------------------------------------------------------

    def build(self) -> tuple[bytes, dict[str, object], bytes]:
        last_error: Exception | None = None
        seed_sizes = (
            64,
            96,
            128,
            160,
            192,
            256,
            320,
            384,
            448,
            576,
            704,
            832,
            960,
            1088,
            1600,
            2112,
            2624,
            3648,
        )
        for d_seed in seed_sizes:
            try:
                layout = self._layout_iterate(d_seed)
                data, seed = self.assemble(layout)
                return data, layout, seed
            except (RuntimeError, AssertionError, ValueError) as error:
                last_error = error
        raise RuntimeError(f"tar.xz build failed: {last_error}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _public_layout(layout: dict[str, object]) -> dict[str, object]:
    keys = ("q", "xz_size", "n", "T", "suffix", "dict_size", "d_seed", "k")
    return {key: layout[key] for key in keys}


def _validate_tar_names(
    payload: Payload, quine_name: str, seed_name: str
) -> None:
    """Reject names that POSIX ustar cannot represent before doing any work."""
    names = [seed_name]
    names.extend(entry.path for entry in payload.entries)
    names.append(quine_name)
    for name in names:
        try:
            _split_ustar_name(name.encode("utf-8"))
        except ValueError as exc:
            raise PayloadError(str(exc)) from exc


def build_archive(
    payload: Payload,
    *,
    quine_name: str,
    seed_name: str,
) -> BuildResult:
    """Build the store-only tar.xz quine in memory."""
    _validate_tar_names(payload, quine_name, seed_name)
    builder = _TarXzBuilder(payload, quine_name, seed_name)
    data, layout, seed = builder.build()
    return BuildResult(
        data=data,
        entries=tuple(
            (entry["name"], bool(entry["is_dir"]))
            for entry in layout["entries"]
        ),
        files=builder.files,
        seed=seed,
        layout=_public_layout(layout),
        compressed=False,
    )


def verify_built(result: BuildResult, payload: Payload) -> None:
    """Decode the built archive and assert seed, payload and self copy match."""
    decompressor = lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
    tar_data = decompressor.decompress(result.data)
    assert decompressor.eof, "xz stream is truncated"
    assert decompressor.unused_data == b"", "unexpected bytes after the xz stream"

    with tarfile.open(fileobj=io.BytesIO(tar_data)) as archive:
        seed_member = archive.extractfile(
            result.entries[0][0]
        )
        assert seed_member is not None, "seed member is missing"
        assert seed_member.read() == result.seed, "seed mismatch"

        for entry in payload.files:
            member = archive.extractfile(entry.path)
            assert member is not None, f"payload file missing: {entry.path}"
            assert member.read() == (entry.data or b""), (
                f"payload file mismatch: {entry.path}"
            )

        for entry in payload.dirs:
            member = archive.getmember(entry.path)
            assert member.isdir(), f"payload directory is not a directory: {entry.path}"

        quine_member = archive.extractfile(result.entries[-1][0])
        assert quine_member is not None, "self copy member is missing"
        assert quine_member.read() == result.data, "self copy mismatch"