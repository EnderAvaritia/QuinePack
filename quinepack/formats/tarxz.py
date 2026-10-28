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
import sys
import tarfile
from collections.abc import Callable, Sequence
from multiprocessing import shared_memory

from ..buffers import Buffer, SPOOL_THRESHOLD, SpoolHandle, SpoolWriter
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
from ..parallel import open_pool, worker_count
from ..progress import report
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
PAX_HEADER_NAME = "PaxHeaders/QuinePack"

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
_CRC64_XOROUT = 0xFFFFFFFFFFFFFFFF
_CRC64_NON_REFLECTED = 0x142F0E1EBA9EA3693
_CRC64_MOD_BIT = 1 << 64

_liblzma = None
try:
    _lzma_name = ctypes.util.find_library("lzma")
    if _lzma_name:
        _candidate = ctypes.CDLL(_lzma_name)
        _candidate.lzma_crc64.argtypes = [
            ctypes.c_void_p,
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


def _make_crc64_slice_tables() -> list[list[int]]:
    """Eight byte-position tables for slice-by-8 CRC64 updates."""
    tables = [_CRC64_TABLE]
    for _ in range(7):
        previous = tables[-1]
        tables.append([(value >> 8) ^ _CRC64_TABLE[value & 0xFF] for value in previous])
    return tables


_CRC64_SLICE = _make_crc64_slice_tables()
_LITTLE_ENDIAN = sys.byteorder == "little"

# Below this many bytes a worker pool costs more than it saves.  Segments keep
# the work balanced without sending large slice descriptors through the pipe.
_CRC64_MP_MIN_BYTES = 32 << 20
_CRC64_MP_SEGMENT = 4 << 20


def _crc64_pure_update(state: int, data: Buffer) -> int:
    """Update a raw CRC64 state, eight bytes at a time."""
    view = memoryview(data)
    length = len(view)
    tables = _CRC64_SLICE
    offset = 0
    if _LITTLE_ENDIAN and length >= 8:
        words = view[: length & ~7].cast("Q")
        try:
            for value in words:
                value ^= state
                state = (
                    tables[7][value & 0xFF]
                    ^ tables[6][(value >> 8) & 0xFF]
                    ^ tables[5][(value >> 16) & 0xFF]
                    ^ tables[4][(value >> 24) & 0xFF]
                    ^ tables[3][(value >> 32) & 0xFF]
                    ^ tables[2][(value >> 40) & 0xFF]
                    ^ tables[1][(value >> 48) & 0xFF]
                    ^ tables[0][(value >> 56) & 0xFF]
                )
        finally:
            words.release()
        offset = length & ~7
    table = _CRC64_TABLE
    while offset < length:
        state = (state >> 8) ^ table[(state ^ view[offset]) & 0xFF]
        offset += 1
    view.release()
    return state


def _crc64_raw_update(state: int, data: Buffer) -> int:
    """Extend a raw (un-finalised) CRC64 state with more bytes."""
    if _liblzma is not None:
        return _crc64_update(state ^ _CRC64_XOROUT, data) ^ _CRC64_XOROUT
    return _crc64_pure_update(state, data)


def _crc64_combine(left: int, right: int, right_length: int) -> int:
    """Append ``right_length`` bytes of zeros-worth of transform to a state."""
    if right_length <= 0:
        return left ^ right
    factor = _poly_pow(2, 8 * right_length)
    return _bit_reverse64(_poly_mul_mod(_bit_reverse64(left), factor)) ^ right


def _crc64_update(value: int, data: Buffer) -> int:
    """Extend a finished CRC64 value with more bytes."""
    if _liblzma is not None:
        if isinstance(data, bytes):
            pointer = ctypes.c_char_p(data)
            return int(_liblzma.lzma_crc64(pointer, len(data), value))
        if isinstance(data, bytearray) or (
            isinstance(data, memoryview) and not data.readonly
        ):
            raw = (ctypes.c_char * len(data)).from_buffer(data)
            try:
                return int(_liblzma.lzma_crc64(raw, len(data), value))
            finally:
                del raw
        # Read-only buffers are rare on this path; copy rather than hand
        # liblzma a pointer the ctypes layer cannot pin.
        pointer = ctypes.c_char_p(bytes(data))
        return int(_liblzma.lzma_crc64(pointer, len(data), value))
    state = _crc64_pure_update(value ^ _CRC64_XOROUT, data)
    return state ^ _CRC64_XOROUT


def _crc64_parts(parts: Sequence[Buffer], pool=None) -> int:
    """CRC64 of the concatenation of ``parts``, spread over a worker pool.

    The stream is hashed as raw states from a zero register: the first slice
    starts from the CRC64 init value and the rest from zero, which lets the
    parent merge the pieces with ``_crc64_combine``.
    """
    parts = tuple(parts)
    total = sum(len(part) for part in parts)
    # A liblzma CRC64 runs at several GB/s; splitting it only adds copies.
    if (
        pool is None
        or not pool.parallel
        or total < _CRC64_MP_MIN_BYTES
        or _liblzma is not None
    ):
        return _crc64_parts_sequential(parts)
    try:
        return _crc64_parts_parallel(parts, pool)
    except Exception:  # noqa: BLE001 - parallelism must never break a build
        return _crc64_parts_sequential(parts)


def _crc64_parts_sequential(parts: Sequence[Buffer]) -> int:
    value = 0
    for part in parts:
        value = _crc64_update(value, part)
    return value


def _crc64_worker(task: tuple[int, list[tuple[str, int, int]]]) -> int:
    """Hash one contiguous slice of the logical stream inside a worker."""
    initial, ranges = task
    state = initial
    for name, offset, length in ranges:
        block = shared_memory.SharedMemory(name=name)
        try:
            view = memoryview(block.buf)[offset : offset + length]
            try:
                state = _crc64_raw_update(state, view)
            finally:
                view.release()
        finally:
            block.close()
    return state


def _crc64_tasks(
    segments: Sequence[tuple[str, int]], workers: int
) -> tuple[list[tuple[int, list[tuple[str, int, int]]]], list[int]]:
    """Cut the logical stream into contiguous, roughly equal worker slices."""
    total = sum(length for _, length in segments)
    target = max(1, total // workers)
    tasks: list[tuple[int, list[tuple[str, int, int]]]] = []
    lengths: list[int] = []
    initial = _CRC64_XOROUT
    ranges: list[tuple[str, int, int]] = []
    current = 0
    for name, size in segments:
        offset = 0
        while offset < size:
            take = min(size - offset, _CRC64_MP_SEGMENT)
            if ranges and ranges[-1][0] == name:
                previous = ranges[-1]
                ranges[-1] = (name, previous[1], previous[2] + take)
            else:
                ranges.append((name, offset, take))
            current += take
            offset += take
            if current >= target and len(tasks) < workers - 1:
                tasks.append((initial, ranges))
                lengths.append(current)
                initial = 0
                ranges = []
                current = 0
    if ranges:
        tasks.append((initial, ranges))
        lengths.append(current)
    return tasks, lengths


def _crc64_parts_parallel(parts: Sequence[Buffer], pool) -> int:
    filled = [(part, len(part)) for part in parts if part]
    if not filled:
        return _crc64_parts_sequential(parts)
    buffers = [
        shared_memory.SharedMemory(create=True, size=length) for _, length in filled
    ]
    try:
        for buffer, (part, length) in zip(buffers, filled):
            buffer.buf[:length] = part
        tasks, lengths = _crc64_tasks(
            tuple((buffer.name, length) for buffer, (_, length) in zip(buffers, filled)),
            worker_count(),
        )
        states = pool.map(tasks, chunksize=1)
    finally:
        for buffer in buffers:
            buffer.close()
            buffer.unlink()
    value = 0
    for length, state in zip(lengths, states):
        value = _crc64_combine(value, state, length)
    return value ^ _CRC64_XOROUT


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


def _fits_ustar(name: str) -> bool:
    try:
        _split_ustar_name(name.encode("utf-8"))
    except ValueError:
        return False
    return True


def _needs_pax(name: str) -> bool:
    """PAX is required for long names and to carry non-ASCII UTF-8 paths."""
    return not _fits_ustar(name) or not name.isascii()


def _truncate_utf8(value: bytes, limit: int) -> bytes:
    """Truncate UTF-8 bytes without splitting a multi-byte sequence."""
    if len(value) <= limit:
        return value
    cut = limit
    while cut > 0 and (value[cut] & 0xC0) == 0x80:
        cut -= 1
    return value[:cut]


def _header_name(name: str) -> tuple[bytes, bytes]:
    """Return ustar name/prefix fields; fall back to a placeholder for PAX."""
    raw = name.encode("utf-8")
    try:
        return _split_ustar_name(raw)
    except ValueError:
        base = raw.rsplit(b"/", 1)[-1] or raw
        return _truncate_utf8(base, 100), b""


def _pax_path_record(name: str) -> bytes:
    """Encode one ``path`` PAX record whose length includes its own digits."""
    body = b" path=" + name.encode("utf-8") + b"\n"
    length = len(body) + 1
    while True:
        digits = len(str(length))
        if digits + len(body) == length:
            return str(length).encode("ascii") + body
        length = digits + len(body)


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
    name_field, prefix_field = _header_name(name)

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
    chunks: list[dict[str, object]],
    output_pos: int,
    file_pos: int,
    q: int,
    header_a_offset: int,
) -> tuple[int, int]:
    """Append the store chunks and LZMA echoes that consume the current slip.

    A single store/LZMA pair is valid only while ``slip + 3 <= CHUNK``: the
    store must copy the whole source region ``[A, A + slip + 3)`` into a
    payload at ``F = A + slip + 3`` so the following LZMA chunk can reproduce
    that payload with one match at distance ``slip + 3``.  Larger regions are
    tiled into ``CHUNK``-sized stores and every payload piece is echoed
    separately; the three-byte LZMA2 headers between payload pieces are
    reproduced from the seed's ``hdrA`` copy at ``header_a_offset``.
    """
    slip = file_pos - (output_pos - q)
    length = slip + 3
    pieces = max(1, (length + CHUNK - 1) // CHUNK)
    first = length - (pieces - 1) * CHUNK
    if first < 2:
        # A one-byte piece cannot be encoded as an LZMA match.  A bare
        # one-byte store raises the slip by three and shifts the phase by one,
        # which makes the first piece three bytes longer.
        chunks.append(
            {
                "kind": "store",
                "foff": file_pos,
                "size": 4,
                "ooff": output_pos,
                "olen": 1,
            }
        )
        output_pos += 1
        file_pos += 4
        slip = file_pos - (output_pos - q)
        length = slip + 3
        pieces = max(1, (length + CHUNK - 1) // CHUNK)
        first = length - (pieces - 1) * CHUNK

    lengths = [first] + [CHUNK] * (pieces - 1)
    source = output_pos - q
    payload_start = file_pos + 3
    assert payload_start == source + length

    for piece in lengths:
        chunks.append(
            {
                "kind": "store",
                "foff": file_pos,
                "size": 3 + piece,
                "ooff": output_pos,
                "olen": piece,
            }
        )
        output_pos += piece
        file_pos += 3 + piece

    covered = 0
    for index, piece in enumerate(lengths):
        if index > 0:
            block, unpacked = lzma_chunk(
                [("m", output_pos - header_a_offset, 3)], output_pos
            )
            assert unpacked == 3
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
            output_pos += unpacked
            file_pos += len(block)
        distance = output_pos - (q + source + covered)
        block, unpacked = lzma_chunk(matches_for(distance, piece), output_pos)
        assert unpacked == piece
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
        output_pos += unpacked
        file_pos += len(block)
        covered += piece
    return output_pos, file_pos


def _forward_vac(archive: Buffer, vac: dict[str, object], q: int) -> None:
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
        release_payload: Callable[[], None] | None = None,
    ) -> None:
        self.quine_name = quine_name
        self.seed_name = seed_name
        self.entries: tuple[PayloadEntry, ...] = payload.entries
        self._release_payload = release_payload
        self.files: tuple[tuple[str, Buffer], ...] = tuple(
            (entry.path, entry.data or b"") for entry in payload.files
        )
        self.file_map = dict(self.files)
        self._handles: list[SpoolHandle] = []
        self.generated_mtime = (
            self.entries[0].mtime_ns // 1_000_000_000 if self.entries else 0
        )

    # -- tar model ---------------------------------------------------------

    def _entry_mtime(self, entry: PayloadEntry) -> int:
        return entry.mtime_ns // 1_000_000_000

    def tar_entries(self, d_seed: int, xz_size: int) -> list[dict[str, object]]:
        entries: list[dict[str, object]] = []
        entries.extend(
            self._tar_entry(
                self.seed_name,
                d_seed,
                self.generated_mtime,
                mode=0o644,
            )
        )

        for entry in self.entries:
            mtime = self._entry_mtime(entry)
            if entry.is_dir:
                entries.extend(
                    self._tar_entry(
                        entry.path,
                        0,
                        mtime,
                        mode=entry.mode,
                        typeflag=b"5",
                        is_dir=True,
                    )
                )
            else:
                data = entry.data or b""
                entries.extend(
                    self._tar_entry(
                        entry.path,
                        len(data),
                        mtime,
                        mode=entry.mode,
                    )
                )

        entries.extend(
            self._tar_entry(
                self.quine_name,
                xz_size,
                self.generated_mtime,
                mode=0o644,
            )
        )
        return entries

    def _tar_entry(
        self,
        name: str,
        data_size: int,
        mtime: int,
        *,
        mode: int,
        typeflag: bytes = b"0",
        is_dir: bool = False,
        data: bytes | None = None,
    ) -> list[dict[str, object]]:
        """Return the tar blocks for one member, plus a PAX header if needed."""
        result: list[dict[str, object]] = []
        if _needs_pax(name):
            record = _pax_path_record(name)
            result.append(
                {
                    "name": PAX_HEADER_NAME,
                    "hdr": _tar_header(
                        PAX_HEADER_NAME,
                        len(record),
                        mtime,
                        mode=0o644,
                        typeflag=b"x",
                    ),
                    "data_size": len(record),
                    "is_dir": False,
                    "data": record,
                    "pax": True,
                }
            )
        result.append(
            {
                "name": name,
                "hdr": _tar_header(
                    name,
                    data_size,
                    mtime,
                    mode=mode,
                    typeflag=typeflag,
                ),
                "data_size": data_size,
                "is_dir": is_dir,
                "data": data,
                "pax": False,
            }
        )
        return result

    def public_entries(
        self, entries: list[dict[str, object]]
    ) -> tuple[tuple[str, bool], ...]:
        """Archive members without the synthetic PAX header entries."""
        return tuple(
            (str(entry["name"]), bool(entry["is_dir"]))
            for entry in entries
            if not entry.get("pax")
        )

    def _seed_data_offset(self) -> int:
        """Tar offset where the seed data starts, after any PAX header."""
        offset = TAR_BLOCK
        if _needs_pax(self.seed_name):
            record = _pax_path_record(self.seed_name)
            offset += TAR_BLOCK + _round_up_512(len(record))
        return offset

    def _compute_q(self, d_seed: int) -> int:
        """Offset of the xz stream inside the tar stream."""
        entries = self.tar_entries(d_seed, 0)
        q = sum(
            TAR_BLOCK + _round_up_512(int(entry["data_size"]))
            for entry in entries[:-1]
        )
        return q + TAR_BLOCK

    # -- layout ------------------------------------------------------------

    def _trim_layouts(
        self,
        base: list[dict[str, object]],
        output_gear: int,
        file_gear: int,
        q: int,
        plant_offset: int,
        d_seed: int,
        seed_data_offset: int,
    ) -> list[tuple[list[dict[str, object]], int, int, int, bytes]]:
        """Enumerate phase-shifting trim configurations.

        Each candidate is ``(chunks, output_before_jump, file_before_jump,
        jump_length, jump_block)`` sorted by resulting archive size.
        """
        slip = file_gear - (output_gear - q)
        header_a_offset = plant_offset - 6
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

            output_pos, file_pos = _add_gear(
                chunks, output_pos, file_pos, q, header_a_offset
            )

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
            if jump_length + plant_offset - seed_data_offset > d_seed:
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
        seed_data_offset = self._seed_data_offset()
        distance_hint = 2 * (q + _BLOCK_HEADER_SIZE + 12 + 3)
        block_header, dict_size = _xz_block_header(distance_hint)
        block_header_size = len(block_header)
        few = _BLOCK_HEADER_SIZE + block_header_size + 3
        header_a_offset = (
            seed_data_offset + _BLOCK_HEADER_SIZE + block_header_size
        )
        header_b_offset = header_a_offset + 3
        plant_offset = (
            seed_data_offset + _BLOCK_HEADER_SIZE + block_header_size + 6
        )

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
                base, output_pos, file_pos, q, header_a_offset
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
            base, output_gear, file_gear, q, plant_offset, d_seed, seed_data_offset
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
        report("assembling")
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
        assert len(tar_prefix) == q, (
            f"tar prefix length {len(tar_prefix)} != {q}"
        )

        for chunk in layout["chunks"]:
            if chunk["kind"] != "store":
                continue
            output_offset = int(chunk["ooff"])
            length = int(chunk["olen"])
            if chunk.get("c1"):
                self._copy_prefix(
                    archive, chunk["foff"] + 3, int(chunk["S"]), length, tar_prefix
                )
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

        report("fixing CRCs")
        self._solve_crc64(archive, layout, tar_prefix)
        return archive, bytes(seed)

    def _copy_prefix(
        self,
        archive: bytearray,
        target: int,
        source: int,
        length: int,
        tar_prefix: Buffer,
    ) -> None:
        """Copy from ``tar_prefix + archive[0:few]`` without joining them."""
        prefix_length = len(tar_prefix)
        if source < prefix_length:
            piece = min(length, prefix_length - source)
            archive[target : target + piece] = tar_prefix[source : source + piece]
            target += piece
            length -= piece
            source += piece
        if length > 0:
            start = source - prefix_length
            archive[target : target + length] = archive[start : start + length]

    def _build_tar_prefix(
        self, layout: dict[str, object], seed: bytes
    ) -> bytes:
        prefix = bytearray()
        entries = layout["entries"]
        for entry in entries[:-1]:
            prefix += entry["hdr"]
            if not entry["is_dir"] and entry["data_size"] > 0:
                data = entry.get("data")
                if data is None:
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
        tar_prefix: Buffer,
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
        stream_length = q + xz_size + trailing
        crc_w1 = q + crc_offset
        crc_w2 = q + crc_offset - total
        zeros = b"\x00" * trailing
        slots = (bytes(archive[crc_offset : crc_offset + CHECK_SIZE]), bytes(
            archive[crc_offset - total : crc_offset - total + CHECK_SIZE]
        ))
        archive[crc_offset : crc_offset + CHECK_SIZE] = b"\x00" * CHECK_SIZE
        archive[
            crc_offset - total : crc_offset - total + CHECK_SIZE
        ] = b"\x00" * CHECK_SIZE
        stream_total = len(tar_prefix) + len(archive) + len(zeros)
        with open_pool(
            _crc64_worker, enabled=stream_total >= _CRC64_MP_MIN_BYTES
        ) as pool:
            crc_poly = _bit_reverse64(
                _crc64_parts((tar_prefix, archive, zeros), pool)
            )
            archive[crc_offset : crc_offset + CHECK_SIZE] = slots[0]
            archive[
                crc_offset - total : crc_offset - total + CHECK_SIZE
            ] = slots[1]

            trailing1 = (stream_length - crc_w1 - 8) * 8
            p1 = _poly_pow(2, 64 + trailing1)
            trailing2 = (stream_length - crc_w2 - 8) * 8
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

            assert (
                _crc64_parts((tar_prefix, archive, zeros), pool) == value
            ), "CRC64 fixpoint verification failed"

    # -- public build ------------------------------------------------------

    def release_payload(self) -> None:
        if self._release_payload is None:
            return
        release = self._release_payload
        self._release_payload = None
        self.files = tuple((name, b"") for name, _ in self.files)
        self.file_map = {}
        release()

    def close(self) -> None:
        for handle in self._handles:
            handle.close()
        self._handles.clear()

    def _fit_dictionary(self, layout: dict[str, object]) -> dict[str, object]:
        """Raise the dictionary property until it covers every match distance.

        Every match distance is bounded by the final output position, which is
        the tar length.  The xz block header keeps its 12-byte size when the
        dictionary property changes, so layout offsets and the CRC64 fixpoint
        are unaffected; only the header bytes and the reported size change.
        """
        q = int(layout["q"])
        output_final = q + _round_up_512(int(layout["xz_size"])) + 1024
        if output_final <= int(layout["dict_size"]):
            return layout
        block_header, dict_size = _xz_block_header(output_final)
        assert len(block_header) == int(layout["bh"]), len(block_header)
        fitted = dict(layout)
        fitted["block_hdr"] = block_header
        fitted["dict_size"] = dict_size
        return fitted

    def build(self) -> tuple[bytes, dict[str, object], bytes]:
        report("laying out")
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
                layout = self._fit_dictionary(self._layout_iterate(d_seed))
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
    """Reject names that tar or the PAX record format cannot carry."""
    names = [seed_name]
    names.extend(entry.path for entry in payload.entries)
    names.append(quine_name)
    for name in names:
        if "\x00" in name:
            raise PayloadError(f"tar entry name contains a NUL byte: {name!r}")
        if _needs_pax(name) and "\n" in name:
            raise PayloadError(
                f"tar entry name needs PAX but contains a newline: {name!r}"
            )


def build_archive(
    payload: Payload,
    *,
    quine_name: str,
    seed_name: str,
    release_payload: bool = False,
) -> BuildResult:
    """Build the store-only tar.xz quine, spooling large tar prefixes."""
    _validate_tar_names(payload, quine_name, seed_name)
    builder = _TarXzBuilder(
        payload,
        quine_name,
        seed_name,
        payload.release_content if release_payload else None,
    )
    try:
        data, layout, seed = builder.build()
        if release_payload:
            builder.release_payload()
    finally:
        builder.close()
    return BuildResult(
        data=data,
        entries=builder.public_entries(layout["entries"]),
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