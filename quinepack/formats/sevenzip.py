"""7z writer that embeds a byte-for-byte copy of the produced archive.

Two variants are implemented:

``compressed=True``
    The validated double-LZMA2 folder: the inner stream decompresses to
    ``content | seed | archive`` and the outer stream stores the compressed
    content only once by referencing the seed.

``compressed=False``
    Store-only single LZMA2 stream: the archive is carried by uncompressed
    chunks plus a small mechanical match gadget.

Entry order is always: optional seed parent directories, seed file, payload
entries in ``Payload.entries`` order, self-copy file.
"""

from __future__ import annotations

import binascii
import lzma
import struct
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..buffers import Buffer, SPOOL_THRESHOLD, SpoolHandle
from ..lzma2 import (
    CHUNK,
    LZ2_MAX_UNPACKED,
    MAX_MATCH,
    dict_prop_for,
    lzma2_raw_buffer,
    lzma2_raw_reader,
    lzma_chunk,
    matches_for,
    seed_chunks,
    split_len,
    store_hdr,
)
from ..model import Payload
from ..parallel import open_pool
from ..progress import report

SIG = b"7z\xbc\xaf'\x1c"
VER = b"\x00\x04"
# Difference between the FILETIME epoch (1601) and the Unix epoch in 100 ns.
FILETIME_EPOCH_OFFSET = 116444736000000000
SEED_NOTE = b"\nQuinePack seed"


@dataclass(frozen=True, slots=True)
class BuildResult:
    """In-memory 7z quine archive plus the metadata needed to verify it."""

    data: Buffer
    entries: tuple[tuple[str, bool], ...]
    files: tuple[tuple[str, Buffer], ...]
    seed: Buffer
    layout: dict[str, object]
    compressed: bool


@dataclass(frozen=True, slots=True)
class _Entry:
    name: str
    is_dir: bool
    data: Buffer
    mtime_ns: int
    attributes: int = 0


class _StoreHeaderTooLarge(RuntimeError):
    """The 7z footer cannot fit in one LZMA2 store chunk."""


# ---------------------------------------------------------------------------
# Small 7z helpers
# ---------------------------------------------------------------------------


def _varint(value: int) -> bytes:
    """7z UINT64 variable-length encoding."""
    if value < 0x80:
        return bytes([value])
    for n in range(1, 9):
        if value < (1 << (8 * n + 7 - n)):
            break
    else:
        raise ValueError(f"varint too big: {value}")
    first = ((0xFF << (8 - n)) & 0xFF) | ((value >> (8 * n)) & ((1 << (7 - n)) - 1))
    return bytes([first]) + (value & ((1 << (8 * n)) - 1)).to_bytes(n, "little")


def _crc32(data, value: int = 0) -> int:
    return binascii.crc32(data, value) & 0xFFFFFFFF


def _filetime(mtime_ns: int) -> int:
    return mtime_ns // 100 + FILETIME_EPOCH_OFFSET


def _prepare_entries(
    payload: Payload, quine_name: str, seed_name: str
) -> tuple[_Entry, ...]:
    generated_mtime = payload.entries[0].mtime_ns if payload.entries else 0
    existing = {entry.path for entry in payload.entries}
    entries: list[_Entry] = []
    parts = seed_name.split("/")
    for index in range(1, len(parts)):
        parent = "/".join(parts[:index])
        if parent and parent not in existing:
            entries.append(_Entry(parent, True, b"", generated_mtime, 0x10))
            existing.add(parent)
    entries.append(_Entry(seed_name, False, b"", generated_mtime, 0x20))
    for entry in payload.entries:
        entries.append(
            _Entry(
                entry.path,
                entry.is_dir,
                b"",
                entry.mtime_ns,
                entry.attributes,
            )
        )
    entries.append(_Entry(quine_name, False, b"", generated_mtime, 0x20))
    return tuple(entries)


def _files_info(entries: tuple[_Entry, ...]) -> bytes:
    count = len(entries)
    out = bytearray()
    out += b"\x05" + _varint(count)  # kFilesInfo

    empty = bytearray((count + 7) // 8)
    for index, entry in enumerate(entries):
        if entry.is_dir:
            empty[index // 8] |= 1 << (7 - index % 8)
    out += b"\x0e" + _varint(len(empty)) + bytes(empty)  # kEmptyStream

    mtimes = b"\x01\x00" + b"".join(
        struct.pack("<Q", _filetime(entry.mtime_ns)) for entry in entries
    )
    out += b"\x14" + _varint(len(mtimes)) + mtimes  # kMTime, all defined, internal

    names = bytearray(b"\x00")  # kName, not external
    for entry in entries:
        names += entry.name.encode("utf-16-le") + b"\x00\x00"
    out += b"\x11" + _varint(len(names)) + bytes(names)

    attrs = b"".join(
        struct.pack("<I", entry.attributes or (0x10 if entry.is_dir else 0x20))
        for entry in entries
    )
    out += b"\x15" + _varint(2 + len(attrs)) + b"\x01\x00" + attrs  # kAttributes

    out += b"\x00\x00"  # end FilesInfo, end Header
    return bytes(out)


def _build_header(
    entries: tuple[_Entry, ...],
    *,
    pack_size: int,
    unpack_sizes: tuple[int, ...],
    sub_sizes: tuple[int, ...],
    dict_props: tuple[int, ...],
    bind_pairs: tuple[tuple[int, int], ...] = (),
) -> tuple[bytes, int]:
    """Build a 7z header and return it together with the CRC slot offset."""
    out = bytearray()
    out += b"\x01\x04"  # kHeader, kMainStreamsInfo

    out += b"\x06" + _varint(0) + _varint(1)  # kPackInfo, packPos=0
    out += b"\x09" + _varint(pack_size) + b"\x00"  # kSize, end PackInfo

    out += b"\x07"  # kUnpackInfo
    out += b"\x0b" + _varint(1) + b"\x00"  # kFolder, one folder, not external
    out += _varint(len(dict_props))  # NumCoders
    for prop in dict_props:
        out += b"\x21\x21" + _varint(1) + bytes([prop])  # LZMA2, one property
    for in_index, out_index in bind_pairs:
        out += _varint(in_index) + _varint(out_index)
    out += b"\x0c" + b"".join(_varint(size) for size in unpack_sizes)
    out += b"\x00"  # end UnpackInfo

    out += b"\x08"  # kSubStreamsInfo
    out += b"\x0d" + _varint(len(sub_sizes))  # kNumUnpackStream
    out += b"\x09" + b"".join(_varint(size) for size in sub_sizes[:-1])  # kSize
    out += b"\x0a\x01"  # kCRC, all defined
    crc_base = len(out)
    out += b"\x00" * (4 * len(sub_sizes))
    out += b"\x00\x00"  # end SubStreamsInfo, end MainStreamsInfo

    out += _files_info(entries)
    return bytes(out), crc_base


# Encoding tasks are cheap; below this payload size process start-up costs
# more than the parallel win.
_STORE_MP_MIN_BYTES = 8 << 20


def _encode_match_chunk(task: tuple[int, int, int]) -> bytes:
    """Encode one match-only LZMA2 chunk from a ``(distance, total, pos)`` plan.

    The result depends only on the task, so a batch of them is safe to run in
    worker processes and produces the same bytes in any order.
    """
    distance, total, out_pos = task
    block, unpacked = lzma_chunk(matches_for(distance, total), out_pos)
    assert unpacked == total
    return block


# ---------------------------------------------------------------------------
# Store-only builder (single-coder LZMA2 stream of uncompressed chunks)
# ---------------------------------------------------------------------------


class _StoreBuilder:
    def __init__(
        self,
        entries: tuple[_Entry, ...],
        files: tuple[tuple[str, Buffer], ...],
        content: Buffer,
    ):
        self.entries = entries
        self.files = files
        self.content = content

    def layout(
        self,
        d_seed: int,
        maxdist_hint: int,
        *,
        large: bool = False,
        pool=None,
    ) -> dict[str, object]:
        d = d_seed + len(self.content)
        lengths: list[int] = []
        remaining = d + 35
        while remaining > CHUNK:
            lengths.append(CHUNK)
            remaining -= CHUNK
        lengths.append(remaining)
        assert lengths[-1] >= 2, f"last chunk too small: {lengths[-1]}"
        chunk_count = len(lengths)
        header_a = store_hdr(CHUNK, first=False)
        header_b = store_hdr(lengths[-1], first=False)
        dict_prop, dict_size = dict_prop_for(maxdist_hint)
        file_sizes = [len(data) for _, data in self.files]

        gadget_size, header_size = 100, 100
        for _ in range(30):
            chunks: list[dict[str, object]] = []
            output_pos = 0
            file_pos = 32
            seed_covered = 0
            for index, length in enumerate(lengths):
                chunks.append(
                    {
                        "kind": "store",
                        "foff": file_pos,
                        "size": 3 + length,
                        "ooff": output_pos,
                        "olen": length,
                        "first": index == 0,
                        "c1": True,
                        "S": seed_covered,
                    }
                )
                output_pos += length
                file_pos += 3 + length
                seed_covered += length
            assert output_pos == d + 35

            seed_covered = 0
            plan: list[tuple[int, int, int]] = []
            for index, length in enumerate(lengths):
                if index > 0:
                    source_pos = (
                        0 if (index < chunk_count - 1 or length == CHUNK) else 3
                    )
                    distance = (d + 35 + seed_covered + 3 * (index - 1)) - source_pos
                    plan.append((distance, 3, output_pos))
                    output_pos += 3
                plan.append((d + 35 + 3 * index, length, output_pos))
                output_pos += length
                seed_covered += length

            # Every match chunk starts from a fresh range coder, so the batch
            # is a pure function of the plan above: encode it in parallel and
            # sum `file_pos` back here in plan order.
            blocks = (
                [_encode_match_chunk(task) for task in plan]
                if pool is None
                else pool.map(plan, chunksize=16)
            )
            for (_, total, chunk_pos), block in zip(plan, blocks):
                chunks.append(
                    {
                        "kind": "lzma",
                        "foff": file_pos,
                        "size": len(block),
                        "ooff": chunk_pos,
                        "olen": total,
                        "bytes": block,
                    }
                )
                file_pos += len(block)

            slip = file_pos - (output_pos - d)
            if slip + 3 <= CHUNK:
                # A store(s) + copy(s) pair is aligned only when s == slip+3:
                # the payload then occupies exactly the copy's target region.
                # While the whole slip fits in one LZMA2 store chunk, gears
                # shrink it to the small range the jump below can absorb.
                while file_pos - (output_pos - d) > 16:
                    slip = file_pos - (output_pos - d)
                    size = min(slip + 3, CHUNK)
                    chunks.append(
                        {
                            "kind": "store",
                            "foff": file_pos,
                            "size": 3 + size,
                            "ooff": output_pos,
                            "olen": size,
                        }
                    )
                    output_pos += size
                    file_pos += 3 + size
                    block, unpacked = lzma_chunk(
                        matches_for(size, size), output_pos
                    )
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

                slip = file_pos - (output_pos - d)
                jump_len = slip + 17
                for _ in range(10):
                    block, unpacked = lzma_chunk(
                        [("m", output_pos - 6, jump_len)], output_pos
                    )
                    next_len = slip + 3 + len(block)
                    if next_len == jump_len:
                        break
                    jump_len = next_len
                assert unpacked == jump_len and next_len == jump_len
                assert jump_len + 6 <= d_seed
                chunks.append(
                    {
                        "kind": "lzma",
                        "foff": file_pos,
                        "size": len(block),
                        "ooff": output_pos,
                        "olen": unpacked,
                        "bytes": block,
                        "jump": True,
                    }
                )
                output_pos += unpacked
                file_pos += len(block)
                need_ds = 0
            else:
                # The residual mechanical region is larger than one store
                # chunk, so no aligned gear pair exists.  Plant a byte image
                # of the whole region in the seed and copy it with matches
                # (chained rep0 for lengths above MAX_MATCH).
                jump_len = slip + 3
                for _ in range(30):
                    offset = 0
                    planted_pos = output_pos
                    encoded_total = 0
                    pieces: list[tuple[bytes, int]] = []
                    while offset < jump_len:
                        take = min(jump_len - offset, LZ2_MAX_UNPACKED)
                        distance = planted_pos - (6 + offset)
                        block, unpacked = lzma_chunk(
                            matches_for(distance, take), planted_pos
                        )
                        assert unpacked == take
                        pieces.append((block, take))
                        encoded_total += len(block)
                        planted_pos += take
                        offset += take
                    next_len = slip + 3 + encoded_total
                    if next_len == jump_len:
                        break
                    jump_len = next_len
                assert next_len == jump_len, "planted copy did not converge"

                need_ds = jump_len + 6
                for index, (block, take) in enumerate(pieces):
                    chunk = {
                        "kind": "lzma",
                        "foff": file_pos,
                        "size": len(block),
                        "ooff": output_pos,
                        "olen": take,
                        "bytes": block,
                    }
                    if index == 0:
                        chunk["jump"] = True
                    chunks.append(chunk)
                    output_pos += take
                    file_pos += len(block)

            assert file_pos == (output_pos - d) - 3
            if large:
                return self._finish_large(
                    chunks=chunks,
                    output_pos=output_pos,
                    file_pos=file_pos,
                    d=d,
                    d_seed=d_seed,
                    jump_len=jump_len,
                    need_ds=need_ds,
                    header_a=header_a,
                    header_b=header_b,
                    lengths=lengths,
                    chunk_count=chunk_count,
                    dict_prop=dict_prop,
                    dict_size=dict_size,
                    file_sizes=file_sizes,
                )
            chunks.append(
                {
                    "kind": "store",
                    "foff": file_pos,
                    "size": 3 + gadget_size,
                    "ooff": output_pos,
                    "olen": gadget_size,
                    "vac": True,
                }
            )
            file_pos += 3 + gadget_size
            output_pos += gadget_size
            block, unpacked = lzma_chunk(
                matches_for(gadget_size, gadget_size), output_pos
            )
            assert unpacked == gadget_size
            chunks.append(
                {
                    "kind": "lzma",
                    "foff": file_pos,
                    "size": len(block),
                    "ooff": output_pos,
                    "olen": unpacked,
                    "bytes": block,
                    "gmatch": True,
                }
            )
            file_pos += len(block)
            output_pos += unpacked

            chunks.append(
                {
                    "kind": "term",
                    "foff": file_pos,
                    "size": 1,
                    "ooff": output_pos,
                    "olen": 0,
                    "bytes": b"\x00",
                }
            )
            file_pos += 1

            n = file_pos - 32
            total = file_pos + header_size
            sub_sizes = (d_seed,) + tuple(file_sizes) + (total,)
            header, crc_base = _build_header(
                self.entries,
                pack_size=n,
                unpack_sizes=(d + total,),
                sub_sizes=sub_sizes,
                dict_props=(dict_prop,),
            )
            new_header_size = len(header)
            new_gadget_size = len(block) + 1 + new_header_size
            if new_gadget_size > CHUNK:
                raise _StoreHeaderTooLarge(
                    f"7z-store cannot encode {len(self.entries)} entries: "
                    f"footer is {new_gadget_size} bytes, above the "
                    f"{CHUNK}-byte LZMA2 store chunk limit; "
                    "use --format 7z or --format txz"
                )
            if (
                new_gadget_size == gadget_size
                and new_header_size == header_size
            ):
                assert output_pos == d + file_pos + header_size
                return {
                    "d": d,
                    "ds": d_seed,
                    "k": chunk_count,
                    "lengths": lengths,
                    "chunks": chunks,
                    "header": header,
                    "crc_base": crc_base,
                    "n": n,
                    "total": file_pos + header_size,
                    "T": gadget_size,
                    "h": header_size,
                    "hdrA": header_a,
                    "hdrB": header_b,
                    "jump_y": jump_len,
                    "need_ds": need_ds,
                    "n_sub": len(sub_sizes),
                    "sub_sizes": sub_sizes,
                    "dict_size": dict_size,
                }
            gadget_size, header_size = new_gadget_size, new_header_size
        raise RuntimeError("store layout did not converge")

    def _finish_large(
        self,
        *,
        chunks: list[dict[str, object]],
        output_pos: int,
        file_pos: int,
        d: int,
        d_seed: int,
        jump_len: int,
        need_ds: int,
        header_a: bytes,
        header_b: bytes,
        lengths: list[int],
        chunk_count: int,
        dict_prop: int,
        dict_size: int,
        file_sizes: list[int],
    ) -> dict[str, object]:
        """Emit the footer from a seed plant instead of a <=64 KiB store.

        The classic footer keeps the gmatch block, terminator and 7z header in
        one store chunk.  That caps the header at the LZMA2 store limit.  Here
        the same byte image is planted at the end of the seed and emitted with
        ordinary matches; the physical tail then only contains encoded match
        chunks, so the header may exceed 64 KiB.
        """
        s = output_pos - d
        assert file_pos == s - 3
        header_size = 100
        tail_len = 100
        for _ in range(80):
            distance = len(self.content) + s + tail_len
            assert distance <= dict_size, (
                f"tail distance {distance} exceeds dictionary {dict_size}"
            )
            tail_chunks: list[dict[str, object]] = []
            offset = 0
            encoded = 0
            tail_file = file_pos
            while offset < tail_len:
                take = min(tail_len - offset, LZ2_MAX_UNPACKED)
                block, unpacked = lzma_chunk(
                    matches_for(distance, take), d + s + offset
                )
                assert unpacked == take
                tail_chunks.append(
                    {
                        "kind": "lzma",
                        "foff": tail_file,
                        "size": len(block),
                        "ooff": d + s + offset,
                        "olen": take,
                        "bytes": block,
                        "tail": True,
                    }
                )
                encoded += len(block)
                tail_file += len(block)
                offset += take
            term = {
                "kind": "term",
                "foff": tail_file,
                "size": 1,
                "ooff": d + s + tail_len,
                "olen": 0,
                "bytes": b"\x00",
            }
            final_file = tail_file + 1
            n = final_file - 32
            assert n == s + encoded - 34
            total = 32 + n + header_size
            sub_sizes = (d_seed,) + tuple(file_sizes) + (total,)
            header, crc_base = _build_header(
                self.entries,
                pack_size=n,
                unpack_sizes=(d + total,),
                sub_sizes=sub_sizes,
                dict_props=(dict_prop,),
            )
            new_header_size = len(header)
            new_tail_len = total - s
            if new_tail_len == tail_len and new_header_size == header_size:
                plant_off = d_seed - tail_len
                need_ds = max(need_ds, 6 + jump_len + tail_len)
                assert output_pos + tail_len == d + total
                return {
                    "large": True,
                    "d": d,
                    "ds": d_seed,
                    "k": chunk_count,
                    "lengths": lengths,
                    "chunks": chunks + tail_chunks + [term],
                    "header": header,
                    "crc_base": crc_base,
                    "n": n,
                    "total": total,
                    "T": tail_len,
                    "h": new_header_size,
                    "hdrA": header_a,
                    "hdrB": header_b,
                    "jump_y": jump_len,
                    "need_ds": need_ds,
                    "n_sub": len(sub_sizes),
                    "sub_sizes": sub_sizes,
                    "dict_size": dict_size,
                    "s": s,
                    "L": tail_len,
                    "plant_off": plant_off,
                }
            header_size = new_header_size
            tail_len = new_tail_len
        raise RuntimeError("large store layout did not converge")

    def assemble(
        self, layout: dict[str, object]
    ) -> tuple[bytearray, bytes | bytearray]:
        if layout.get("large"):
            return self._assemble_large(layout)
        total = int(layout["total"])
        d = int(layout["d"])
        d_seed = int(layout["ds"])
        header_size = int(layout["h"])
        header_offset = total - header_size
        chunks = list(layout["chunks"])
        out = bytearray(total)

        out[0:6] = SIG
        out[6:8] = VER
        struct.pack_into("<Q", out, 12, int(layout["n"]))
        struct.pack_into("<Q", out, 20, header_size)

        jump_chunk = None
        vac_chunk = None
        for chunk in chunks:
            if chunk["kind"] == "store":
                out[chunk["foff"] : chunk["foff"] + 3] = store_hdr(
                    chunk["olen"], first=chunk.get("first", False)
                )
                if chunk.get("vac"):
                    vac_chunk = chunk
            else:
                out[chunk["foff"] : chunk["foff"] + chunk["size"]] = chunk["bytes"]
                if chunk.get("jump"):
                    jump_chunk = chunk
        out[header_offset : header_offset + header_size] = layout["header"]
        assert jump_chunk is not None and vac_chunk is not None

        jump_len = int(layout["jump_y"])
        plant = bytes(
            out[
                jump_chunk["ooff"] - d : jump_chunk["ooff"] - d + jump_len
            ]
        )
        seed = bytearray(d_seed)
        seed[0:3] = layout["hdrA"]
        seed[3:6] = layout["hdrB"]
        seed[6 : 6 + jump_len] = plant
        pad = (SEED_NOTE * (d_seed // len(SEED_NOTE) + 2))[: d_seed - 6 - jump_len]
        seed[6 + jump_len :] = pad

        for chunk in chunks:
            if chunk["kind"] != "store":
                continue
            offset = chunk["ooff"]
            length = chunk["olen"]
            if chunk.get("c1"):
                self._copy_store_source(
                    out, chunk["foff"] + 3, chunk["S"], length, seed
                )
            elif chunk.get("vac"):
                start = offset - d
                out[start : start + length] = out[start + length : start + 2 * length]
            else:
                start = offset - d
                out[chunk["foff"] + 3 : chunk["foff"] + 3 + length] = out[
                    start : start + length
                ]
        return out, seed

    def _assemble_large(
        self, layout: dict[str, object]
    ) -> tuple[bytearray, bytes | bytearray]:
        """Assemble a layout whose footer is carried by the seed plant."""
        total = int(layout["total"])
        d = int(layout["d"])
        d_seed = int(layout["ds"])
        header_size = int(layout["h"])
        header_offset = total - header_size
        chunks = list(layout["chunks"])
        out = bytearray(total)

        out[0:6] = SIG
        out[6:8] = VER
        struct.pack_into("<Q", out, 12, int(layout["n"]))
        struct.pack_into("<Q", out, 20, header_size)

        jump_chunk = None
        c1_end = 0
        for chunk in chunks:
            if chunk["kind"] == "store":
                out[chunk["foff"] : chunk["foff"] + 3] = store_hdr(
                    chunk["olen"], first=chunk.get("first", False)
                )
                if chunk.get("c1"):
                    c1_end = max(
                        c1_end, int(chunk["foff"]) + 3 + int(chunk["olen"])
                    )
            else:
                out[chunk["foff"] : chunk["foff"] + chunk["size"]] = chunk["bytes"]
                if chunk.get("jump"):
                    jump_chunk = chunk
        out[header_offset : header_offset + header_size] = layout["header"]
        assert jump_chunk is not None

        s = int(layout["s"])
        tail_len = int(layout["L"])
        plant_off = int(layout["plant_off"])
        assert s >= c1_end, f"tail plant overlaps seed region: {s} < {c1_end}"
        jump_len = int(layout["jump_y"])
        jump_plant = bytes(
            out[jump_chunk["ooff"] - d : jump_chunk["ooff"] - d + jump_len]
        )
        seed = bytearray(d_seed)
        seed[0:3] = layout["hdrA"]
        seed[3:6] = layout["hdrB"]
        seed[6 : 6 + jump_len] = jump_plant
        gap = plant_off - (6 + jump_len)
        assert gap >= 0, f"seed too small for tail plant: {d_seed}"
        pad = (SEED_NOTE * (gap // len(SEED_NOTE) + 2))[:gap]
        seed[6 + jump_len : plant_off] = pad
        tail = out[s:total]
        assert len(tail) == tail_len
        seed[plant_off : plant_off + tail_len] = tail

        for chunk in chunks:
            if chunk["kind"] != "store":
                continue
            offset = chunk["ooff"]
            length = chunk["olen"]
            if chunk.get("c1"):
                self._copy_store_source(
                    out, chunk["foff"] + 3, chunk["S"], length, seed
                )
            else:
                start = offset - d
                out[chunk["foff"] + 3 : chunk["foff"] + 3 + length] = out[
                    start : start + length
                ]
        return out, seed

    def _copy_store_source(
        self,
        out: bytearray,
        target: int,
        source: int,
        length: int,
        seed: bytearray,
    ) -> None:
        """Fill a store chunk from ``seed + content + out[0:35]`` in pieces."""
        seed_length = len(seed)
        if source < seed_length:
            piece = min(length, seed_length - source)
            out[target : target + piece] = seed[source : source + piece]
            target += piece
            source += piece
            length -= piece
        content_length = len(self.content)
        if length and source < seed_length + content_length:
            start = source - seed_length
            piece = min(length, content_length - start)
            out[target : target + piece] = self.content[start : start + piece]
            target += piece
            source += piece
            length -= piece
        if length:
            start = source - seed_length - content_length
            out[target : target + length] = out[start : start + length]

    def solve_crc(
        self, out: bytearray, layout: dict[str, object], seed: bytes | bytearray
    ) -> list[int]:
        if layout.get("large"):
            return self._solve_crc_large(out, layout, seed)
        gadget_size = int(layout["T"])
        header_size = int(layout["h"])
        d = int(layout["d"])
        chunk_count = int(layout["k"])
        n_sub = int(layout["n_sub"])
        crc_base = int(layout["crc_base"])
        header_offset = int(layout["total"]) - header_size
        header_copy = int(layout["total"]) - 2 * gadget_size + (gadget_size - header_size)

        known = [_crc32(seed)] + [_crc32(data) for _, data in self.files]
        assert len(known) == n_sub - 1
        for index, value in enumerate(known):
            slot = header_offset + crc_base + 4 * index
            struct.pack_into("<I", out, slot, value)
            struct.pack_into("<I", out, header_copy + crc_base + 4 * index, value)

        data_slot = crc_base + 4 * (n_sub - 1)
        next_copy = 32 + 3 * chunk_count + d
        groups = [
            [header_offset + data_slot, header_copy + data_slot],
            [28, next_copy + 28],
            [8, next_copy + 8],
        ]
        view = memoryview(out)

        def targets() -> tuple[int, int, int]:
            return (
                _crc32(view),
                _crc32(view[header_offset : header_offset + header_size]),
                _crc32(view[12:32]),
            )

        base = targets()
        base_vector = base[0] | (base[1] << 32) | (base[2] << 64)
        columns: list[int] = []
        for group in groups:
            for bit_index in range(32):
                positions = [position + bit_index // 8 for position in group]
                bit = bit_index % 8
                for position in positions:
                    out[position] ^= 1 << bit
                values = targets()
                for position in positions:
                    out[position] ^= 1 << bit
                columns.append(
                    (values[0] ^ base[0])
                    | ((values[1] ^ base[1]) << 32)
                    | ((values[2] ^ base[2]) << 64)
                )

        equations = []
        for row in range(96):
            coefficient = 0
            for column in range(96):
                if (columns[column] >> row) & 1:
                    coefficient |= 1 << column
            coefficient ^= 1 << row
            equations.append([coefficient, (base_vector >> row) & 1])

        for column in range(96):
            pivot = next(
                (
                    row
                    for row in range(column, 96)
                    if (equations[row][0] >> column) & 1
                ),
                None,
            )
            if pivot is None:
                raise RuntimeError(f"CRC system is singular at bit {column}")
            equations[column], equations[pivot] = equations[pivot], equations[column]
            for row in range(96):
                if row != column and ((equations[row][0] >> column) & 1):
                    equations[row][0] ^= equations[column][0]
                    equations[row][1] ^= equations[column][1]

        solution = 0
        for row in range(96):
            assert equations[row][0] == (1 << row)
            if equations[row][1]:
                solution |= 1 << row

        values = [
            solution & 0xFFFFFFFF,
            (solution >> 32) & 0xFFFFFFFF,
            (solution >> 64) & 0xFFFFFFFF,
        ]
        for group_index, group in enumerate(groups):
            for position in group:
                out[position : position + 4] = struct.pack("<I", values[group_index])
        assert targets() == tuple(values), "CRC fix-up failed"
        return values

    def _solve_crc_large(
        self, out: bytearray, layout: dict[str, object], seed: bytes | bytearray
    ) -> list[int]:
        """Solve archive, header, signature and seed CRCs in one linear system.

        The footer header exists twice physically (real tail and seed plant),
        and the seed CRC depends on that plant, so the large-footer layout needs
        a fourth equation over the classic 96-bit signature/header system.
        """
        header_size = int(layout["h"])
        d = int(layout["d"])
        d_seed = int(layout["ds"])
        chunk_count = int(layout["k"])
        n_sub = int(layout["n_sub"])
        crc_base = int(layout["crc_base"])
        total = int(layout["total"])
        header_offset = total - header_size
        header_seed = d_seed - header_size
        data_slot = crc_base + 4 * (n_sub - 1)
        next_copy = 32 + 3 * chunk_count + d
        assert isinstance(seed, bytearray)
        assert bytes(seed[header_seed:]) == layout["header"]

        seed_map = [-1] * d_seed
        for chunk in layout["chunks"]:
            if not chunk.get("c1"):
                continue
            source = int(chunk["S"])
            length = int(chunk["olen"])
            target = int(chunk["foff"]) + 3
            for index in range(length):
                position = source + index
                if position < d_seed:
                    seed_map[position] = target + index
        assert all(position >= 0 for position in seed_map), "seed is not fully mapped"

        def header_slot(offset: int) -> tuple[list[list[int]], list[list[int]]]:
            seed_slot = [header_seed + offset + index for index in range(4)]
            real_slot = [header_offset + offset + index for index in range(4)]
            mirror = [seed_map[position] for position in seed_slot]
            return [real_slot, mirror], [seed_slot]

        def signature_slot(offset: int) -> tuple[list[list[int]], list[list[int]]]:
            return [
                [offset + index for index in range(4)],
                [next_copy + offset + index for index in range(4)],
            ], []

        def patch(
            out_copies: list[list[int]],
            seed_copies: list[list[int]],
            value: int,
        ) -> None:
            raw = struct.pack("<I", value)
            for copy in out_copies:
                for index, position in enumerate(copy):
                    out[position] = raw[index]
            for copy in seed_copies:
                for index, position in enumerate(copy):
                    seed[position] = raw[index]

        known = [_crc32(seed)] + [_crc32(data) for _, data in self.files]
        assert len(known) == n_sub - 1
        for index, value in enumerate(known):
            # The seed CRC is itself one of the solver groups; leave its slot
            # at the zero placeholder so the linear system starts from a clean
            # value.  Payload/file CRCs are fixed and can be written now.
            if index == 0:
                continue
            out_copies, seed_copies = header_slot(crc_base + 4 * index)
            patch(out_copies, seed_copies, value)

        groups = [
            header_slot(data_slot),
            signature_slot(28),
            signature_slot(8),
            header_slot(crc_base),
        ]
        view = memoryview(out)

        def targets() -> tuple[int, int, int, int]:
            return (
                _crc32(view),
                _crc32(view[header_offset : header_offset + header_size]),
                _crc32(view[12:32]),
                _crc32(seed),
            )

        def flip(
            out_copies: list[list[int]],
            seed_copies: list[list[int]],
            bit_index: int,
        ) -> None:
            byte_index = bit_index // 8
            bit = 1 << (bit_index % 8)
            for copy in out_copies:
                out[copy[byte_index]] ^= bit
            for copy in seed_copies:
                seed[copy[byte_index]] ^= bit

        base = targets()
        size = 32 * len(groups)
        base_vector = 0
        for index, value in enumerate(base):
            base_vector |= value << (32 * index)

        columns: list[int] = []
        for out_copies, seed_copies in groups:
            for bit_index in range(32):
                flip(out_copies, seed_copies, bit_index)
                values = targets()
                flip(out_copies, seed_copies, bit_index)
                vector = 0
                for index, value in enumerate(values):
                    vector |= (value ^ base[index]) << (32 * index)
                columns.append(vector)

        equations = []
        for row in range(size):
            coefficient = 0
            for column in range(size):
                if (columns[column] >> row) & 1:
                    coefficient |= 1 << column
            coefficient ^= 1 << row
            equations.append([coefficient, (base_vector >> row) & 1])

        for column in range(size):
            pivot = next(
                (
                    row
                    for row in range(column, size)
                    if (equations[row][0] >> column) & 1
                ),
                None,
            )
            if pivot is None:
                raise RuntimeError(f"CRC system is singular at bit {column}")
            equations[column], equations[pivot] = equations[pivot], equations[column]
            for row in range(size):
                if row != column and ((equations[row][0] >> column) & 1):
                    equations[row][0] ^= equations[column][0]
                    equations[row][1] ^= equations[column][1]

        solution = 0
        for row in range(size):
            assert equations[row][0] == (1 << row)
            if equations[row][1]:
                solution |= 1 << row

        values = [
            (solution >> (32 * index)) & 0xFFFFFFFF for index in range(len(groups))
        ]
        for (out_copies, seed_copies), value in zip(groups, values):
            patch(out_copies, seed_copies, value)
        assert targets() == tuple(values), "CRC fix-up failed"
        return values

    def build(self) -> tuple[bytes | bytearray, dict[str, object], bytes | bytearray]:
        d_seed = 64
        last_error: Exception | None = None
        large = False
        report("laying out")
        with open_pool(
            _encode_match_chunk, enabled=len(self.content) >= _STORE_MP_MIN_BYTES
        ) as pool:
            for _ in range(80):
                d = d_seed + len(self.content)
                if (d + 35) % CHUNK == 1:
                    d_seed += 1
                    continue
                # The planted copy reads from output position 6, roughly 2*d
                # bytes behind the jump chunk, so the dictionary must cover it.
                max_distance = max(
                    2 * (len(self.content) + 128) + (1 << 21),
                    2 * d + (1 << 21),
                )
                try:
                    layout = self.layout(
                        d_seed, max_distance, large=large, pool=pool
                    )
                    need_ds = int(layout["need_ds"])
                    if d_seed < need_ds:
                        d_seed = need_ds
                        continue
                    report("assembling")
                    out, seed = self.assemble(layout)
                    report("fixing CRCs")
                    self.solve_crc(out, layout, seed)
                    return out, layout, seed
                except _StoreHeaderTooLarge:
                    if large:
                        raise
                    large = True
                    continue
                except (RuntimeError, AssertionError) as error:
                    last_error = error
                    d_seed += 1
        raise RuntimeError(f"store build failed: {last_error}")


# ---------------------------------------------------------------------------
# Double-LZMA2 builder
# ---------------------------------------------------------------------------


class _DoubleBuilder:
    def __init__(
        self,
        entries: tuple[_Entry, ...],
        files: tuple[tuple[str, Buffer], ...],
        content: Buffer,
        preset: int,
        release_payload: Callable[[], None] | None = None,
        *,
        content_len: int | None = None,
        content_reader: Callable[[], Iterable[Buffer]] | None = None,
        compact_seed: bool = False,
        file_sizes: tuple[int, ...] | None = None,
        file_crcs: tuple[int, ...] | None = None,
    ):
        self.entries = entries
        self.files = files
        self.preset = preset
        self.content = content
        self.content_len = len(content) if content_len is None else content_len
        self.file_sizes = (
            tuple(len(data) for _, data in files)
            if file_sizes is None
            else file_sizes
        )
        self.file_crcs = (
            tuple(_crc32(data) for _, data in files)
            if file_crcs is None
            else file_crcs
        )
        self._content_reader = content_reader
        self._compact_seed = compact_seed
        self._release_payload = release_payload
        self._compressed_content: Buffer = b""
        self._compressed_handle: SpoolHandle | None = None
        self._streams_cache: dict[str, object] | None = None
        self._mseg_cache: bytes | None = None

    # -- structure ---------------------------------------------------------

    def layout(self) -> dict[str, int]:
        content = self.content
        content_len = self.content_len
        dict_c = min(
            max(1 << 20, 1 << max(20, content_len.bit_length())), 64 << 20
        )
        if self._content_reader is None:
            compressed, handle = lzma2_raw_buffer(
                content,
                dict_c,
                self.preset,
                threshold=SPOOL_THRESHOLD,
            )
        else:
            compressed, handle = lzma2_raw_reader(
                self._content_reader(),
                dict_c,
                self.preset,
            )
        self._compressed_content = compressed
        self._compressed_handle = handle
        if self._release_payload is not None:
            # The remaining layout only needs sizes and CRCs.  Drop the
            # payload mapping before building token streams so it does not
            # stay resident alongside C, seed and F.
            release = self._release_payload
            self._release_payload = None
            self.files = tuple((name, b"") for name, _ in self.files)
            self.content = b""
            content = b""
            release()
        lc = len(compressed)

        # Seed the fixed point near the usual ratio of match-segment
        # bytes to archive bytes so the loop converges in a few rounds.
        plant_len = 96
        header_len = 256
        mseg_len = max(256, lc // 2000)
        for _ in range(1000):
            ds = lc + 32 + header_len + plant_len
            d = content_len + ds
            seed_lengths = seed_chunks(ds, lc, CHUNK)
            inner_prefix = 3 * len(seed_lengths) + ds
            m1 = mseg_len + 1
            mseg_lengths = split_len(m1, CHUNK)
            inner_len = inner_prefix + lc + m1
            dict_out = dict_prop_for(max(inner_len, 1 << 20))[0]

            streams = self._build_streams(lc, seed_lengths, inner_prefix, mseg_lengths)
            mseg = self._build_mseg(streams, d, lc, header_len)
            total = streams["n"] + 32 + header_len
            sub_sizes = (ds,) + self.file_sizes + (total,)
            dict_in = dict_prop_for(d + total + 8192)[0]
            header, _ = _build_header(
                self.entries,
                pack_size=streams["n"],
                unpack_sizes=(inner_len, d + total),
                sub_sizes=sub_sizes,
                dict_props=(dict_out, dict_in),
                bind_pairs=((1, 0),),
            )

            required_plant = (
                len(streams["hdr_bytes"]) + len(streams["mech"]) + len(mseg) + 2
            )
            new_header_len = len(header)
            new_mseg_len = len(mseg)
            if (
                new_header_len == header_len
                and new_mseg_len == mseg_len
                and required_plant <= plant_len
            ):
                self._streams_cache = streams
                self._mseg_cache = mseg
                return {"Y": plant_len, "H": header_len, "M": mseg_len}
            # Grow the plant instead of shrinking it: a larger seed can always
            # absorb the difference as deterministic zero padding, and this
            # avoids the two-cycle that an exact fixed point can fall into when
            # one extra seed byte changes the match-token split downstream.
            plant_len = max(plant_len, required_plant)
            header_len = new_header_len
            mseg_len = new_mseg_len
        raise RuntimeError("double layout did not converge")

    def finalize(self, plant_len: int, header_len: int, mseg_len: int) -> dict[str, object]:
        compressed = self._compressed_content
        lc = len(compressed)
        ds = lc + 32 + header_len + plant_len
        d = self.content_len + ds
        seed_lengths = seed_chunks(ds, lc, CHUNK)
        inner_prefix = 3 * len(seed_lengths) + ds
        m1 = mseg_len + 1
        mseg_lengths = split_len(m1, CHUNK)
        inner_len = inner_prefix + lc + m1
        dict_out, dict_out_size = dict_prop_for(max(inner_len, 1 << 20))

        if self._streams_cache is not None and self._mseg_cache is not None:
            streams = self._streams_cache
            mseg = self._mseg_cache
        else:
            streams = self._build_streams(
                lc, seed_lengths, inner_prefix, mseg_lengths
            )
            mseg = self._build_mseg(streams, d, lc, header_len)
        assert len(mseg) == mseg_len, f"Mseg {len(mseg)} != {mseg_len}"
        total = streams["n"] + 32 + header_len
        dict_in, dict_in_size = dict_prop_for(d + total + 8192)
        sub_sizes = (ds,) + self.file_sizes + (total,)
        header, crc_base = _build_header(
            self.entries,
            pack_size=streams["n"],
            unpack_sizes=(inner_len, d + total),
            sub_sizes=sub_sizes,
            dict_props=(dict_out, dict_in),
            bind_pairs=((1, 0),),
        )
        assert len(header) == header_len, f"header {len(header)} != {header_len}"
        plant_parts = (
            streams["hdr_bytes"],
            streams["mech"],
            mseg,
            b"\x00\x00",
        )
        plant_size = sum(len(part) for part in plant_parts)
        assert plant_size <= plant_len, f"plant {plant_size} > {plant_len}"
        return {
            "C": compressed,
            "lc": lc,
            "dc": self.content_len,
            "ds": ds,
            "d": d,
            "Li": inner_len,
            "ochunks": streams["ochunks"],
            "hdr_tab": streams["hdr_tab"],
            "hdr_bytes": streams["hdr_bytes"],
            "mech": streams["mech"],
            "mseg": mseg,
            "M": mseg_len,
            "H": header_len,
            "Y": plant_len,
            "n": streams["n"],
            "total": total,
            "header": header,
            "crc_base": crc_base,
            "plant_parts": plant_parts,
            "plant_pad": plant_len - plant_size,
            "dict_size_in": dict_in_size,
            "dict_size_out": dict_out_size,
            "n_sub": len(sub_sizes),
        }

    # -- outer stream ------------------------------------------------------

    def _build_streams(
        self,
        lc: int,
        seed_lengths: list[int],
        outer_content_offset: int,
        mseg_lengths: list[int],
    ) -> dict[str, object]:
        header_table: list[bytes] = []
        header_index: dict[bytes, int] = {}

        def header_id(block: bytes) -> int:
            assert len(block) == 3 and block[0] in (0x01, 0x02), block.hex()
            index = header_index.get(block)
            if index is None:
                index = len(header_table)
                header_index[block] = index
                header_table.append(block)
            return index

        chunks: list[dict[str, object]] = []
        mech = bytearray()
        file_pos = 32
        inner_pos = 0

        def put_lzma(tokens, out_pos: int) -> tuple[int, int]:
            nonlocal file_pos, inner_pos
            block, unpacked = lzma_chunk(tokens, out_pos)
            chunks.append(
                {
                    "kind": "lzma",
                    "foff": file_pos,
                    "size": len(block),
                    "ioff": out_pos,
                    "olen": unpacked,
                    "mech": len(mech),
                }
            )
            mech.extend(block)
            file_pos += len(block)
            inner_pos += unpacked
            return len(block), unpacked

        def put_store(
            out_pos: int, length: int, header_id_value: int, source: tuple[str, int]
        ) -> None:
            nonlocal file_pos, inner_pos
            chunks.append(
                {
                    "kind": "store",
                    "foff": file_pos,
                    "size": 3 + length,
                    "ioff": out_pos,
                    "olen": length,
                    "hdr": header_id_value,
                    "src": source,
                }
            )
            file_pos += 3 + length
            inner_pos += length

        # Segment 1: inner store chain carrying the seed (compressed content
        # copy, header copies, mechanical plant).
        seed_offset = 0
        chunk_offset = 0
        first_header_at: dict[bytes, int] = {}
        for index, length in enumerate(seed_lengths):
            inner_header = store_hdr(length, first=(index == 0))
            previous = first_header_at.get(inner_header)
            if previous is None:
                first_header_at[inner_header] = chunk_offset
                put_store(
                    chunk_offset,
                    3,
                    header_id(store_hdr(3, first=(index == 0))),
                    ("tab", header_id(inner_header)),
                )
            else:
                distance = chunk_offset - previous
                _, unpacked = put_lzma([("m", distance, 3)], chunk_offset)
                assert unpacked == 3
            put_store(
                chunk_offset + 3,
                length,
                header_id(store_hdr(length, first=False)),
                ("seed", seed_offset),
            )
            chunk_offset += 3 + length
            seed_offset += length
        assert chunk_offset == outer_content_offset
        assert inner_pos == outer_content_offset

        # Segment 2: compressed content, reproduced from the seed copy.
        content_offset = 0
        for index, length in enumerate(seed_lengths):
            if content_offset >= lc:
                break
            piece = min(length, lc - content_offset)
            assert piece >= 2, piece
            distance = outer_content_offset - 3 * (index + 1)
            assert distance > 0, distance
            _, unpacked = put_lzma(
                matches_for(distance, piece), outer_content_offset + content_offset
            )
            assert unpacked == piece
            content_offset += piece
        assert content_offset == lc

        # Segment 3: inner match chain for the archive bytes plus the inner
        # stream terminator.
        mseg_offset = 0
        for length in mseg_lengths:
            put_store(
                outer_content_offset + lc + mseg_offset,
                length,
                header_id(store_hdr(length, first=False)),
                ("mseg", mseg_offset),
            )
            mseg_offset += length

        chunks.append(
            {
                "kind": "term",
                "foff": file_pos,
                "size": 1,
                "ioff": inner_pos,
                "olen": 0,
                "bytes": b"\x00",
            }
        )
        file_pos += 1
        return {
            "ochunks": chunks,
            "mech": mech,
            "hdr_tab": header_table,
            "hdr_bytes": b"".join(header_table),
            "n": file_pos - 32,
        }

    # -- inner match chain -------------------------------------------------

    def _build_mseg(
        self, streams: dict[str, object], d: int, lc: int, header_len: int
    ) -> bytes:
        s_s32 = lc
        s_header = lc + 32
        s_table = lc + 32 + header_len
        s_mech = s_table + len(streams["hdr_bytes"])
        s_mseg = s_mech + len(streams["mech"])
        total = streams["n"] + 32 + header_len
        header_offset = 32 + streams["n"]

        segments: list[tuple[int, int, int]] = [(0, 32, s_s32)]
        for chunk in streams["ochunks"]:
            if chunk["kind"] == "store":
                segments.append((chunk["foff"], 3, s_table + 3 * chunk["hdr"]))
                source = chunk["src"]
                if source[0] == "seed":
                    source_pos = source[1]
                elif source[0] == "tab":
                    source_pos = s_table + 3 * source[1]
                else:
                    source_pos = s_mseg + source[1]
                segments.append((chunk["foff"] + 3, chunk["olen"], source_pos))
            elif chunk["kind"] == "lzma":
                segments.append(
                    (chunk["foff"], chunk["size"], s_mech + chunk["mech"])
                )
            else:
                previous_foff, previous_len, previous_pos = segments[-1]
                segments[-1] = (previous_foff, previous_len + 1, previous_pos)
        segments.append((header_offset, header_len, s_header))

        position = 0
        for foff, length, _ in segments:
            assert foff == position, f"segment gap {foff} != {position}"
            assert length >= 2, f"segment too short {length} at {foff}"
            position += length
        assert position == total, f"segments cover {position} != {total}"

        # Encode one LZMA2 chunk as soon as it is full so the whole token
        # stream is never resident at once.
        mseg = bytearray()
        out_pos = d
        tokens: list[tuple[str, int] | tuple[str, int, int]] = []
        token_len = 0

        def flush_tokens() -> None:
            nonlocal out_pos, tokens, token_len
            if not tokens:
                return
            block, unpacked = lzma_chunk(tokens, out_pos)
            assert unpacked == token_len
            mseg.extend(block)
            out_pos += token_len
            tokens, token_len = [], 0

        for foff, length, source_pos in segments:
            distance = (d + foff) - source_pos
            assert distance > 0, f"bad distance {distance} at {foff}"
            if token_len and token_len + length > LZ2_MAX_UNPACKED - MAX_MATCH:
                flush_tokens()
            tokens.extend(matches_for(distance, length))
            token_len += length
        flush_tokens()
        assert out_pos == d + total, f"Mseg output {out_pos} != {d + total}"
        return bytes(mseg)

    # -- assembly and CRC --------------------------------------------------

    def assemble(self, layout: dict[str, object]) -> tuple[bytearray, bytearray]:
        total = int(layout["total"])
        header_len = int(layout["H"])
        header_offset = total - header_len
        chunks = list(layout["ochunks"])
        out = bytearray(total)

        out[0:6] = SIG
        out[6:8] = VER
        struct.pack_into("<Q", out, 12, int(layout["n"]))
        struct.pack_into("<Q", out, 20, header_len)
        mech = layout["mech"]
        for chunk in chunks:
            if chunk["kind"] == "store":
                out[chunk["foff"] : chunk["foff"] + 3] = layout["hdr_tab"][chunk["hdr"]]
            elif chunk["kind"] == "term":
                out[chunk["foff"] : chunk["foff"] + 1] = chunk["bytes"]
            else:
                start = chunk["mech"]
                out[chunk["foff"] : chunk["foff"] + chunk["size"]] = mech[
                    start : start + chunk["size"]
                ]
        out[header_offset : header_offset + header_len] = layout["header"]

        compressed = layout.pop("C")
        lc = int(layout["lc"])
        seed = None
        if not self._compact_seed:
            seed = bytearray(int(layout["ds"]))
            seed[0:lc] = compressed
            seed[lc : lc + 32] = out[0:32]
            seed[lc + 32 : lc + 32 + header_len] = out[
                header_offset : header_offset + header_len
            ]
            position = lc + 32 + header_len
            for part in layout["plant_parts"]:
                seed[position : position + len(part)] = part
                position += len(part)
            pad = int(layout["plant_pad"])
            if pad:
                seed[position:] = b"\x00" * pad
            assert position + pad == layout["ds"], "seed size mismatch"

        mseg_tail = layout["mseg"] + b"\x00"
        for chunk in chunks:
            if chunk["kind"] != "store":
                continue
            start = chunk["foff"] + 3
            length = chunk["olen"]
            source = chunk["src"]
            if source[0] == "seed":
                if self._compact_seed:
                    self._copy_seed_source(
                        out, start, source[1], length, layout, compressed
                    )
                else:
                    assert seed is not None
                    out[start : start + length] = seed[
                        source[1] : source[1] + length
                    ]
            elif source[0] == "tab":
                out[start : start + length] = layout["hdr_tab"][source[1]]
            else:
                out[start : start + length] = mseg_tail[
                    source[1] : source[1] + length
                ]
        if isinstance(compressed, memoryview):
            compressed.release()
        self.close()
        return out, (b"" if seed is None else seed)

    def _copy_seed_source(
        self,
        out: bytearray,
        target: int,
        source: int,
        length: int,
        layout: dict[str, object],
        compressed: Buffer,
    ) -> None:
        """Copy a virtual seed range without materialising the whole seed."""
        lc = int(layout["lc"])
        header_len = int(layout["H"])
        header_offset = int(layout["total"]) - header_len
        ended = source + length
        position = source
        while position < ended:
            if position < lc:
                piece = min(ended, lc) - position
                out[target : target + piece] = compressed[position : position + piece]
            elif position < lc + 32:
                start = position - lc
                piece = min(ended, lc + 32) - position
                out[target : target + piece] = out[start : start + piece]
            elif position < lc + 32 + header_len:
                start = header_offset + (position - lc - 32)
                piece = min(ended, lc + 32 + header_len) - position
                out[target : target + piece] = out[start : start + piece]
            else:
                plant_start = lc + 32 + header_len
                plant_end = plant_start + sum(
                    len(part) for part in layout["plant_parts"]
                )
                if position < plant_end:
                    rel = position - plant_start
                    for part in layout["plant_parts"]:
                        if rel < len(part):
                            piece = min(ended - position, len(part) - rel)
                            out[target : target + piece] = part[rel : rel + piece]
                            break
                        rel -= len(part)
                else:
                    piece = ended - position
                    out[target : target + piece] = bytes(piece)
            target += piece
            position += piece

    def _seed_offsets(self, layout: dict[str, object], offset: int) -> tuple[int, int]:
        for chunk in layout["ochunks"]:
            if chunk["kind"] != "store" or chunk["src"][0] != "seed":
                continue
            start = chunk["src"][1]
            if start <= offset < start + chunk["olen"]:
                return chunk["foff"] + 3 + (offset - start), start + chunk["olen"] - offset
        raise RuntimeError(f"seed offset {offset} is not stored in the archive")

    def _seed_positions(
        self, layout: dict[str, object], offset: int, length: int
    ) -> list[int]:
        positions: list[int] = []
        while length > 0:
            position, available = self._seed_offsets(layout, offset)
            take = min(length, available)
            positions.extend(range(position, position + take))
            offset += take
            length -= take
        return positions

    def solve_crc(
        self,
        out: bytearray,
        layout: dict[str, object],
        compressed_crc: int,
    ) -> list[int]:
        header_len = int(layout["H"])
        header_offset = int(layout["total"]) - header_len
        n_sub = int(layout["n_sub"])
        crc_base = int(layout["crc_base"])
        lc = int(layout["lc"])
        s32_positions = self._seed_positions(layout, lc, 32)
        view = memoryview(out)

        pad = int(layout["plant_pad"])
        plant_parts = layout["plant_parts"]

        def seed_crc() -> int:
            value = compressed_crc
            value = _crc32(view[0:32], value)
            value = _crc32(view[header_offset : header_offset + header_len], value)
            for part in plant_parts:
                value = _crc32(part, value)
            if pad:
                value = _crc32(b"\x00" * pad, value)
            return value

        def targets() -> tuple[int, int, int, int]:
            return (
                seed_crc(),
                _crc32(view),
                _crc32(view[header_offset : header_offset + header_len]),
                _crc32(view[12:32]),
            )

        known = list(self.file_crcs)
        for index, value in enumerate(known):
            slot = crc_base + 4 * (1 + index)
            packed = struct.pack("<I", value)
            out[header_offset + slot : header_offset + slot + 4] = packed
            mirror = self._seed_positions(layout, lc + 32 + slot, 4)
            assert len(mirror) == 4
            for byte_index in range(4):
                out[mirror[byte_index]] = packed[byte_index]

        seed_slot = crc_base
        quine_slot = crc_base + 4 * (n_sub - 1)
        groups: list[list[list[int]]] = []
        for slot in (seed_slot, quine_slot):
            groups.append(
                [
                    list(range(header_offset + slot, header_offset + slot + 4)),
                    self._seed_positions(layout, lc + 32 + slot, 4),
                ]
            )
        groups.append([list(range(28, 32)), s32_positions[28:32]])
        groups.append([list(range(8, 12)), s32_positions[8:12]])

        bits = 32 * len(groups)
        base = targets()
        base_vector = 0
        for index, value in enumerate(base):
            base_vector |= value << (32 * index)
        columns: list[int] = []
        for own_positions, mirror_positions in groups:
            for bit_index in range(32):
                byte_index, bit = bit_index // 8, bit_index % 8
                positions = [own_positions[byte_index], mirror_positions[byte_index]]
                for position in positions:
                    out[position] ^= 1 << bit
                values = targets()
                for position in positions:
                    out[position] ^= 1 << bit
                delta = 0
                for index, value in enumerate(values):
                    delta |= (value ^ base[index]) << (32 * index)
                columns.append(delta)

        equations = []
        for row in range(bits):
            coefficient = 0
            for column in range(bits):
                if (columns[column] >> row) & 1:
                    coefficient |= 1 << column
            coefficient ^= 1 << row
            equations.append([coefficient, (base_vector >> row) & 1])

        for column in range(bits):
            pivot = next(
                (
                    row
                    for row in range(column, bits)
                    if (equations[row][0] >> column) & 1
                ),
                None,
            )
            if pivot is None:
                raise RuntimeError(f"CRC system is singular at bit {column}")
            equations[column], equations[pivot] = equations[pivot], equations[column]
            for row in range(bits):
                if row != column and ((equations[row][0] >> column) & 1):
                    equations[row][0] ^= equations[column][0]
                    equations[row][1] ^= equations[column][1]

        solution = 0
        for row in range(bits):
            assert equations[row][0] == (1 << row)
            if equations[row][1]:
                solution |= 1 << row

        values = [(solution >> (32 * index)) & 0xFFFFFFFF for index in range(len(groups))]
        for group_index, (own_positions, mirror_positions) in enumerate(groups):
            packed = struct.pack("<I", values[group_index])
            for byte_index in range(4):
                out[own_positions[byte_index]] = packed[byte_index]
                out[mirror_positions[byte_index]] = packed[byte_index]
        assert targets() == tuple(values), "CRC fix-up failed"
        return values

    def close(self) -> None:
        """Release the mapped compressed stream, if any."""
        self._compressed_content = b""
        if self._compressed_handle is not None:
            self._compressed_handle.close()
            self._compressed_handle = None

    def build(self) -> tuple[Buffer, dict[str, object], Buffer]:
        report("compressing")
        first = self.layout()
        report("laying out")
        layout = self.finalize(first["Y"], first["H"], first["M"])
        compressed_crc = _crc32(self._compressed_content)
        report("assembling")
        out, seed = self.assemble(layout)
        report("fixing CRCs")
        self.solve_crc(out, layout, compressed_crc)
        header_offset = int(layout["total"]) - int(layout["H"])
        lc = int(layout["lc"])
        header_len = int(layout["H"])
        if seed:
            seed[lc : lc + 32] = out[0:32]
            seed[lc + 32 : lc + 32 + header_len] = out[
                header_offset : header_offset + header_len
            ]
        return out, layout, seed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _public_layout(layout: dict[str, object], compressed: bool) -> dict[str, object]:
    keys = (
        ("lc", "dc", "ds", "d", "Li", "M", "H", "Y", "n", "total",
         "dict_size_in", "dict_size_out", "n_sub")
        if compressed
        else ("d", "ds", "k", "n", "total", "T", "h", "n_sub", "dict_size")
    )
    return {key: layout[key] for key in keys}


def build_archive(
    payload: Payload,
    *,
    quine_name: str,
    seed_name: str,
    compressed: bool,
    preset: int = 6,
    release_payload: bool = False,
) -> BuildResult:
    """Build the 7z archive, spooling large payloads to temp files."""
    entries = _prepare_entries(payload, quine_name, seed_name)
    files = tuple((entry.path, entry.data or b"") for entry in payload.files)
    compact = (
        compressed
        and release_payload
        and payload.total_bytes >= SPOOL_THRESHOLD
    )
    if compact:
        file_sizes = tuple(len(data) for _, data in files)
        file_crcs = tuple(_crc32(data) for _, data in files)
        files = tuple((name, b"") for name, _ in files)
        content = b""
        payload.release_views_keep_store()
        builder = _DoubleBuilder(
            entries,
            files,
            content,
            preset,
            payload.close,
            content_len=payload.total_bytes,
            content_reader=payload.iter_content,
            compact_seed=True,
            file_sizes=file_sizes,
            file_crcs=file_crcs,
        )
    else:
        content = payload.content_view
        release = payload.release_content if release_payload and compressed else None
        builder = (
            _DoubleBuilder(entries, files, content, preset, release)
            if compressed
            else _StoreBuilder(entries, files, content)
        )
    # The builder now owns these views.  Dropping the caller's references lets
    # a release callback unmap the payload while build() is still running.
    del content, files
    try:
        data, layout, seed = builder.build()
        # Drop the compressed-stream view before closing its mapping.
        layout.pop("C", None)
    finally:
        close = getattr(builder, "close", None)
        if close is not None:
            close()
    return BuildResult(
        data=data,
        entries=tuple((entry.name, entry.is_dir) for entry in entries),
        files=builder.files,
        seed=seed,
        layout=_public_layout(layout, compressed),
        compressed=compressed,
    )


def verify_built(result: BuildResult, payload: Payload) -> None:
    """Decode the built archive and assert seed, payload and self-copy match."""
    layout = result.layout
    data = result.data
    n = int(layout["n"])
    d = int(layout["d"])
    ds = int(layout["ds"])
    total = int(layout["total"])

    if result.compressed:
        outer = lzma.LZMADecompressor(
            format=lzma.FORMAT_RAW,
            filters=[
                {"id": lzma.FILTER_LZMA2, "dict_size": int(layout["dict_size_out"])}
            ],
        )
        inner_stream = outer.decompress(data[32 : 32 + n])
        assert len(inner_stream) == int(layout["Li"]), "outer LZMA2 size mismatch"
        inner = lzma.LZMADecompressor(
            format=lzma.FORMAT_RAW,
            filters=[
                {"id": lzma.FILTER_LZMA2, "dict_size": int(layout["dict_size_in"])}
            ],
        )
        unpacked = inner.decompress(inner_stream)
    else:
        decoder = lzma.LZMADecompressor(
            format=lzma.FORMAT_RAW,
            filters=[
                {"id": lzma.FILTER_LZMA2, "dict_size": int(layout["dict_size"])}
            ],
        )
        unpacked = decoder.decompress(data[32 : 32 + n])

    assert len(unpacked) == d + total, f"unpacked {len(unpacked)} != {d + total}"
    assert unpacked[:ds] == result.seed, "seed mismatch"
    position = ds
    for entry in payload.files:
        content = entry.data or b""
        assert unpacked[position : position + len(content)] == content, (
            f"payload file mismatch: {entry.path}"
        )
        position += len(content)
    assert position == d, f"payload size {position} != {d}"
    assert unpacked[d:] == data, "self-copy mismatch"