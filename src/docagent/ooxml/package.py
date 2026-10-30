"""Minimal ZIP reader/writer for OOXML packages.

Untouched entries are copied as raw records (local header, compressed bytes and
data descriptor), so their bytes and CRCs in the output are identical to the
template. Only replaced parts are recompressed. ZIP64, encryption, multi-disk
archives and duplicate entry names are refused rather than handled.
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from dataclasses import dataclass, field

from docagent.errors import PackageError

_LOCAL_SIG = 0x04034B50
_CENTRAL_SIG = 0x02014B50
_EOCD_SIG = 0x06054B50
_DESCRIPTOR_SIG = 0x08074B50

_LOCAL_FMT = "<IHHHHHIIIHH"  # 30 bytes
_CENTRAL_FMT = "<IHHHHHHIIIHHHHHII"  # 46 bytes
_EOCD_FMT = "<IHHHHIIH"  # 22 bytes

MAX_ENTRIES = 2000
MAX_TOTAL_UNCOMPRESSED = 256 * 1024 * 1024


@dataclass
class Entry:
    name: str
    raw_record: bytes  # local header + compressed data (+ data descriptor), verbatim
    central: bytes  # central directory record, verbatim
    method: int
    flags: int
    crc: int
    compress_size: int
    file_size: int
    mod_time: int
    mod_date: int
    _data: bytes | None = field(default=None, repr=False)

    def data(self) -> bytes:
        if self._data is None:
            name_len, extra_len = struct.unpack_from("<HH", self.raw_record, 26)
            header_len = 30 + name_len + extra_len
            compressed = self.raw_record[header_len : header_len + self.compress_size]
            if self.method == 0:
                out = compressed
            elif self.method == 8:
                try:
                    out = zlib.decompress(compressed, -15)
                except zlib.error as exc:
                    raise PackageError(f"{self.name}: corrupt deflate stream") from exc
            else:
                raise PackageError(f"{self.name}: unsupported compression method {self.method}")
            if len(out) != self.file_size or zlib.crc32(out) != self.crc:
                raise PackageError(f"{self.name}: size or CRC mismatch")
            self._data = out
        return self._data


class Package:
    """An OOXML package read from bytes, with entries in archive order."""

    def __init__(self, raw: bytes, entries: list[Entry], comment: bytes):
        self.raw = raw
        self.entries = entries
        self.comment = comment
        self._by_name = {e.name: e for e in entries}

    @classmethod
    def from_bytes(cls, raw: bytes) -> Package:
        eocd_at = raw.rfind(struct.pack("<I", _EOCD_SIG), max(0, len(raw) - 65557))
        if eocd_at < 0 or eocd_at + 22 > len(raw):
            raise PackageError("not a ZIP archive (no end-of-central-directory record)")
        _, disk, cd_disk, n_disk, n_total, cd_size, cd_offset, comment_len = struct.unpack_from(
            _EOCD_FMT, raw, eocd_at
        )
        if disk != 0 or cd_disk != 0 or n_disk != n_total:
            raise PackageError("multi-disk archives are not supported")
        if n_total == 0xFFFF or cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
            raise PackageError("ZIP64 archives are not supported")
        if n_total > MAX_ENTRIES:
            raise PackageError(f"too many entries ({n_total})")
        comment = raw[eocd_at + 22 : eocd_at + 22 + comment_len]

        entries: list[Entry] = []
        seen: set[str] = set()
        total = 0
        pos = cd_offset
        for _ in range(n_total):
            if struct.unpack_from("<I", raw, pos)[0] != _CENTRAL_SIG:
                raise PackageError("corrupt central directory")
            (
                _sig, _made, _need, flags, method, mtime, mdate, crc, csize, usize,
                name_len, extra_len, cmt_len, _disk, _iattr, _eattr, local_offset,
            ) = struct.unpack_from(_CENTRAL_FMT, raw, pos)
            central = raw[pos : pos + 46 + name_len + extra_len + cmt_len]
            name_bytes = raw[pos + 46 : pos + 46 + name_len]
            pos += len(central)

            if flags & 0x1:
                raise PackageError("encrypted entries are not supported")
            if csize == 0xFFFFFFFF or usize == 0xFFFFFFFF or local_offset == 0xFFFFFFFF:
                raise PackageError("ZIP64 entries are not supported")
            name = name_bytes.decode("utf-8" if flags & 0x800 else "cp437")
            if name in seen:
                raise PackageError(f"duplicate entry name {name!r}")
            seen.add(name)
            total += usize
            if total > MAX_TOTAL_UNCOMPRESSED:
                raise PackageError("package exceeds the uncompressed size limit")

            if struct.unpack_from("<I", raw, local_offset)[0] != _LOCAL_SIG:
                raise PackageError(f"{name}: missing local header")
            l_name_len, l_extra_len = struct.unpack_from("<HH", raw, local_offset + 26)
            if raw[local_offset + 30 : local_offset + 30 + l_name_len] != name_bytes:
                raise PackageError(f"{name}: local and central names differ")
            data_end = local_offset + 30 + l_name_len + l_extra_len + csize
            if flags & 0x8:
                has_sig = struct.unpack_from("<I", raw, data_end)[0] == _DESCRIPTOR_SIG
                data_end += 16 if has_sig else 12
            entries.append(
                Entry(
                    name=name,
                    raw_record=raw[local_offset:data_end],
                    central=central,
                    method=method,
                    flags=flags,
                    crc=crc,
                    compress_size=csize,
                    file_size=usize,
                    mod_time=mtime,
                    mod_date=mdate,
                )
            )
        return cls(raw, entries, comment)

    @property
    def names(self) -> list[str]:
        return [e.name for e in self.entries]

    def has(self, name: str) -> bool:
        return name in self._by_name

    def entry(self, name: str) -> Entry:
        try:
            return self._by_name[name]
        except KeyError:
            raise PackageError(f"package has no part {name!r}") from None

    def part(self, name: str) -> bytes:
        return self.entry(name).data()

    def fingerprint(self) -> dict[str, str]:
        """SHA-256 of every part's uncompressed bytes, in archive order."""
        return {e.name: hashlib.sha256(e.data()).hexdigest() for e in self.entries}

    def rebuild(self, replacements: dict[str, bytes]) -> bytes:
        """Return a new archive with the given parts replaced and all others copied raw."""
        unknown = set(replacements) - set(self._by_name)
        if unknown:
            raise PackageError(f"cannot replace missing parts: {sorted(unknown)}")
        out = bytearray()
        central = bytearray()
        for e in self.entries:
            offset = len(out)
            if e.name not in replacements:
                out += e.raw_record
                central += e.central[:42] + struct.pack("<I", offset) + e.central[46:]
                continue
            data = replacements[e.name]
            compressor = zlib.compressobj(6, zlib.DEFLATED, -15)
            compressed = compressor.compress(data) + compressor.flush()
            crc = zlib.crc32(data)
            flags = e.flags & 0x800  # keep the UTF-8 name flag only; sizes are now known
            name_bytes = e.central[46 : 46 + struct.unpack_from("<H", e.central, 28)[0]]
            out += struct.pack(
                _LOCAL_FMT, _LOCAL_SIG, 20, flags, 8, e.mod_time, e.mod_date,
                crc, len(compressed), len(data), len(name_bytes), 0,
            )
            out += name_bytes + compressed
            made_by, = struct.unpack_from("<H", e.central, 4)
            cmt_len, = struct.unpack_from("<H", e.central, 32)
            iattr, eattr = struct.unpack_from("<HI", e.central, 36)
            comment = e.central[len(e.central) - cmt_len :] if cmt_len else b""
            central += struct.pack(
                _CENTRAL_FMT, _CENTRAL_SIG, made_by, 20, flags, 8, e.mod_time, e.mod_date,
                crc, len(compressed), len(data), len(name_bytes), 0, cmt_len, 0,
                iattr, eattr, offset,
            )
            central += name_bytes + comment
        cd_offset = len(out)
        out += central
        out += struct.pack(
            _EOCD_FMT, _EOCD_SIG, 0, 0, len(self.entries), len(self.entries),
            len(central), cd_offset, len(self.comment),
        )
        out += self.comment
        return bytes(out)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
