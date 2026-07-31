"""Create, parse, verify, extract, and recover Rosetta Archive files."""

from __future__ import annotations

import binascii
import hashlib
import os
import stat
import tempfile
import unicodedata
import zlib
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable, Iterator

from .format import (
    CHUNK_SIZE,
    ENTRY,
    ENTRY_MAGIC,
    EXTENSION_FLAG_CRITICAL,
    EXTENSION_HEADER,
    FLAG_DETERMINISTIC,
    FLAG_PER_ENTRY_COMPRESSION,
    FLAG_RECOVERY_FOOTER,
    FOOTER,
    FOOTER_MAGIC,
    FORMAT_MAJOR,
    FORMAT_MINOR,
    HEADER,
    HEADER_MAGIC,
    INDEX_HEADER,
    INDEX_MAGIC,
    KNOWN_ARCHIVE_FLAGS,
    MAX_ENTRIES,
    MAX_INDEX_BYTES,
    MAX_PATH_BYTES,
    METHOD_STORE,
    METHOD_ZLIB,
    TYPE_DIRECTORY,
    TYPE_FILE,
    method_name,
    type_name,
)


class ArchiveError(Exception):
    """Raised when an archive or an input tree violates the RTA rules."""


@dataclass(frozen=True)
class HeaderInfo:
    major: int
    minor: int
    flags: int
    entry_count: int
    index_offset: int
    index_size: int
    data_offset: int
    archive_size: int
    created_ns: int
    index_sha256: bytes


@dataclass(frozen=True)
class EntryInfo:
    path: str
    entry_type: int
    flags: int
    method: int
    mode: int
    uid: int
    gid: int
    mtime_ns: int
    original_size: int
    stored_size: int
    data_offset: int
    data_crc32: int
    stored_crc32: int
    sha256: bytes
    extension: bytes = b""

    @property
    def type_label(self) -> str:
        return type_name(self.entry_type)

    @property
    def method_label(self) -> str:
        return method_name(self.method)


@dataclass(frozen=True)
class SourceEntry:
    path: str
    entry_type: int
    mode: int
    uid: int
    gid: int
    mtime_ns: int
    data: bytes = b""


@dataclass(frozen=True)
class RecoveryResult:
    recovered: int
    skipped: int


def _crc32(data: bytes, value: int = 0) -> int:
    return binascii.crc32(data, value) & 0xFFFFFFFF


def _read_exact(handle: BinaryIO, size: int, context: str) -> bytes:
    data = handle.read(size)
    if len(data) != size:
        raise ArchiveError(f"truncated archive while reading {context}")
    return data


def _normal_path(path: str) -> tuple[str, bytes]:
    if not isinstance(path, str):
        raise ArchiveError("archive path is not text")
    value = unicodedata.normalize("NFC", path.replace("\\", "/"))
    if not value or value.startswith("/") or "\x00" in value:
        raise ArchiveError(f"unsafe archive path: {path!r}")
    pure = PurePosixPath(value)
    parts = pure.parts
    if any(part in ("", ".", "..") for part in parts):
        raise ArchiveError(f"unsafe archive path: {path!r}")
    if parts and len(parts[0]) >= 2 and parts[0][0].isalpha() and parts[0][1] == ":":
        raise ArchiveError(f"drive-qualified archive path: {path!r}")
    normalized = "/".join(parts)
    encoded = normalized.encode("utf-8", "strict")
    if len(encoded) > MAX_PATH_BYTES:
        raise ArchiveError(f"archive path exceeds {MAX_PATH_BYTES} bytes")
    return normalized, encoded


def _source_metadata(path: Path, archive_path: str, entry_type: int, data: bytes = b"") -> SourceEntry:
    info = path.lstat()
    return SourceEntry(
        path=archive_path,
        entry_type=entry_type,
        mode=stat.S_IMODE(info.st_mode),
        uid=int(getattr(info, "st_uid", 0)) & 0xFFFFFFFF,
        gid=int(getattr(info, "st_gid", 0)) & 0xFFFFFFFF,
        mtime_ns=int(info.st_mtime_ns),
        data=data,
    )


def _collect_input(path: Path, archive_path: str) -> Iterator[SourceEntry]:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise ArchiveError(f"symbolic links are not supported: {path}")
    if stat.S_ISREG(info.st_mode):
        yield _source_metadata(path, archive_path, TYPE_FILE, path.read_bytes())
        return
    if not stat.S_ISDIR(info.st_mode):
        raise ArchiveError(f"unsupported input type: {path}")

    yield _source_metadata(path, archive_path, TYPE_DIRECTORY)
    children = sorted(
        path.iterdir(),
        key=lambda child: unicodedata.normalize("NFC", child.name).encode("utf-8"),
    )
    for child in children:
        child_name = unicodedata.normalize("NFC", child.name)
        child_archive_path, _ = _normal_path(f"{archive_path}/{child_name}")
        yield from _collect_input(child, child_archive_path)


def _gather_inputs(inputs: Iterable[os.PathLike[str] | str]) -> list[SourceEntry]:
    gathered: list[SourceEntry] = []
    seen: set[str] = set()
    for raw in inputs:
        path = Path(raw)
        if not path.exists() and not path.is_symlink():
            raise ArchiveError(f"input does not exist: {path}")
        resolved_name = path.name
        if not resolved_name:
            raise ArchiveError(f"cannot archive a filesystem root directly: {path}")
        root_name, _ = _normal_path(resolved_name)
        for item in _collect_input(path, root_name):
            if item.path in seen:
                raise ArchiveError(f"duplicate archive path: {item.path}")
            seen.add(item.path)
            gathered.append(item)
    if not gathered:
        raise ArchiveError("create requires at least one input")
    gathered.sort(key=lambda item: item.path.encode("utf-8"))
    return gathered


def _encode_source(source: SourceEntry, compression_level: int, force_store: bool) -> tuple[EntryInfo, bytes]:
    path, _ = _normal_path(source.path)
    if source.entry_type == TYPE_DIRECTORY:
        empty_hash = hashlib.sha256(b"").digest()
        return (
            EntryInfo(
                path=path,
                entry_type=TYPE_DIRECTORY,
                flags=0,
                method=METHOD_STORE,
                mode=source.mode & 0o7777,
                uid=source.uid,
                gid=source.gid,
                mtime_ns=source.mtime_ns,
                original_size=0,
                stored_size=0,
                data_offset=0,
                data_crc32=0,
                stored_crc32=0,
                sha256=empty_hash,
            ),
            b"",
        )
    if source.entry_type != TYPE_FILE:
        raise ArchiveError(f"unsupported source entry type for {path}")

    original = source.data
    method = METHOD_STORE
    stored = original
    if not force_store and original:
        candidate = zlib.compress(original, compression_level)
        if len(candidate) < len(original):
            method = METHOD_ZLIB
            stored = candidate
    return (
        EntryInfo(
            path=path,
            entry_type=TYPE_FILE,
            flags=0,
            method=method,
            mode=source.mode & 0o7777,
            uid=source.uid,
            gid=source.gid,
            mtime_ns=source.mtime_ns,
            original_size=len(original),
            stored_size=len(stored),
            data_offset=0,
            data_crc32=_crc32(original),
            stored_crc32=_crc32(stored),
            sha256=hashlib.sha256(original).digest(),
        ),
        stored,
    )


def _pack_entry(entry: EntryInfo) -> bytes:
    path, path_bytes = _normal_path(entry.path)
    if path != entry.path:
        raise ArchiveError(f"entry path is not canonical NFC: {entry.path!r}")
    record_size = ENTRY.size + len(path_bytes) + len(entry.extension)
    fixed_zero = ENTRY.pack(
        ENTRY_MAGIC,
        record_size,
        entry.entry_type,
        entry.flags,
        entry.method,
        0,
        len(path_bytes),
        entry.mode,
        entry.uid,
        entry.gid,
        entry.mtime_ns,
        entry.original_size,
        entry.stored_size,
        entry.data_offset,
        entry.data_crc32,
        entry.stored_crc32,
        entry.sha256,
        0,
        b"\x00" * 4,
    )
    record_crc = _crc32(fixed_zero + path_bytes + entry.extension)
    fixed = fixed_zero[:100] + record_crc.to_bytes(4, "little") + fixed_zero[104:]
    return fixed + path_bytes + entry.extension


def _build_archive(sources: list[SourceEntry], compression_level: int, force_store: bool) -> bytes:
    if not 0 <= compression_level <= 9:
        raise ArchiveError("compression level must be between 0 and 9")
    if len(sources) > MAX_ENTRIES:
        raise ArchiveError(f"entry count exceeds {MAX_ENTRIES}")

    normalized: list[SourceEntry] = []
    seen: set[str] = set()
    for source in sources:
        path, _ = _normal_path(source.path)
        if path in seen:
            raise ArchiveError(f"duplicate archive path: {path}")
        seen.add(path)
        normalized.append(replace(source, path=path))
    normalized.sort(key=lambda item: item.path.encode("utf-8"))

    encoded = [_encode_source(item, compression_level, force_store) for item in normalized]
    data_cursor = HEADER.size
    entries: list[EntryInfo] = []
    data_parts: list[bytes] = []
    for entry, stored in encoded:
        if entry.entry_type == TYPE_FILE:
            entry = replace(entry, data_offset=data_cursor)
            data_cursor += len(stored)
            data_parts.append(stored)
        entries.append(entry)

    index_records = b"".join(_pack_entry(entry) for entry in entries)
    index_size = INDEX_HEADER.size + len(index_records)
    index = INDEX_HEADER.pack(
        INDEX_MAGIC, FORMAT_MAJOR, FORMAT_MINOR, len(entries), index_size
    ) + index_records
    if len(index) > MAX_INDEX_BYTES:
        raise ArchiveError(f"index exceeds {MAX_INDEX_BYTES} bytes")

    index_offset = data_cursor
    archive_size = HEADER.size + sum(len(part) for part in data_parts) + len(index) + FOOTER.size
    flags = FLAG_DETERMINISTIC | FLAG_PER_ENTRY_COMPRESSION | FLAG_RECOVERY_FOOTER
    index_sha = hashlib.sha256(index).digest()
    header_zero = HEADER.pack(
        HEADER_MAGIC,
        FORMAT_MAJOR,
        FORMAT_MINOR,
        HEADER.size,
        flags,
        len(entries),
        index_offset,
        len(index),
        HEADER.size,
        archive_size,
        0,
        index_sha,
        0,
        b"\x00" * 12,
    )
    header_crc = _crc32(header_zero)
    header = header_zero[:96] + header_crc.to_bytes(4, "little") + header_zero[100:]

    footer_zero = FOOTER.pack(
        FOOTER_MAGIC,
        FORMAT_MAJOR,
        FORMAT_MINOR,
        flags,
        index_offset,
        len(index),
        archive_size,
        0,
        _crc32(index),
        0,
        b"\x00" * 8,
    )
    footer_crc = _crc32(footer_zero)
    footer = footer_zero[:52] + footer_crc.to_bytes(4, "little") + footer_zero[56:]
    return header + b"".join(data_parts) + index + footer


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def create_archive(
    archive: os.PathLike[str] | str,
    inputs: Iterable[os.PathLike[str] | str],
    *,
    compression_level: int = 6,
    force_store: bool = False,
) -> None:
    sources = _gather_inputs(inputs)
    _atomic_write(Path(archive), _build_archive(sources, compression_level, force_store))


class ArchiveReader:
    """A strict random-access reader for one RTA archive."""

    def __init__(self, path: os.PathLike[str] | str):
        self.path = Path(path)
        self._size = self.path.stat().st_size
        self.header: HeaderInfo
        self.entries: list[EntryInfo]
        with self.path.open("rb") as handle:
            self.header = self._read_header(handle)
            self._read_footer(handle, self.header)
            self.entries = self._read_index(handle, self.header)
        self._validate_organization()

    def _read_header(self, handle: BinaryIO) -> HeaderInfo:
        if self._size < HEADER.size + INDEX_HEADER.size + FOOTER.size:
            raise ArchiveError("archive is too small")
        handle.seek(0)
        raw = _read_exact(handle, HEADER.size, "header")
        values = HEADER.unpack(raw)
        (
            magic,
            major,
            minor,
            header_size,
            flags,
            entry_count,
            index_offset,
            index_size,
            data_offset,
            archive_size,
            created_ns,
            index_sha,
            header_crc,
            reserved,
        ) = values
        if magic != HEADER_MAGIC:
            raise ArchiveError("invalid archive identity (header magic)")
        if major != FORMAT_MAJOR or minor > FORMAT_MINOR:
            raise ArchiveError(f"unsupported RTA version {major}.{minor}")
        if header_size != HEADER.size:
            raise ArchiveError(f"unsupported header size {header_size}")
        if flags & ~KNOWN_ARCHIVE_FLAGS:
            raise ArchiveError(f"unsupported archive flags 0x{flags:x}")
        if reserved != b"\x00" * 12:
            raise ArchiveError("nonzero reserved header bytes")
        zeroed = raw[:96] + b"\x00" * 4 + raw[100:]
        if _crc32(zeroed) != header_crc:
            raise ArchiveError("header CRC32 mismatch")
        if archive_size != self._size:
            raise ArchiveError(
                f"archive size mismatch: header={archive_size}, actual={self._size}"
            )
        if entry_count > MAX_ENTRIES:
            raise ArchiveError(f"entry count exceeds {MAX_ENTRIES}")
        if index_size > MAX_INDEX_BYTES:
            raise ArchiveError(f"index exceeds {MAX_INDEX_BYTES} bytes")
        footer_offset = self._size - FOOTER.size
        if data_offset != HEADER.size:
            raise ArchiveError("invalid data region offset")
        if index_offset < data_offset or index_offset + index_size != footer_offset:
            raise ArchiveError("index region is out of bounds or noncanonical")
        return HeaderInfo(
            major=major,
            minor=minor,
            flags=flags,
            entry_count=entry_count,
            index_offset=index_offset,
            index_size=index_size,
            data_offset=data_offset,
            archive_size=archive_size,
            created_ns=created_ns,
            index_sha256=index_sha,
        )

    def _read_footer(self, handle: BinaryIO, header: HeaderInfo) -> None:
        handle.seek(self._size - FOOTER.size)
        raw = _read_exact(handle, FOOTER.size, "footer")
        (
            magic,
            major,
            minor,
            flags,
            index_offset,
            index_size,
            archive_size,
            header_offset,
            index_crc,
            footer_crc,
            reserved,
        ) = FOOTER.unpack(raw)
        if magic != FOOTER_MAGIC:
            raise ArchiveError("invalid recovery footer magic")
        zeroed = raw[:52] + b"\x00" * 4 + raw[56:]
        if _crc32(zeroed) != footer_crc:
            raise ArchiveError("footer CRC32 mismatch")
        if reserved != b"\x00" * 8 or header_offset != 0:
            raise ArchiveError("invalid recovery footer reserved fields")
        expected = (
            header.major,
            header.minor,
            header.flags,
            header.index_offset,
            header.index_size,
            header.archive_size,
        )
        observed = (major, minor, flags, index_offset, index_size, archive_size)
        if observed != expected:
            raise ArchiveError("header and footer disagree")
        handle.seek(index_offset)
        if _crc32(_read_exact(handle, index_size, "index for footer CRC")) != index_crc:
            raise ArchiveError("index CRC32 mismatch")

    def _read_index(self, handle: BinaryIO, header: HeaderInfo) -> list[EntryInfo]:
        handle.seek(header.index_offset)
        raw = _read_exact(handle, header.index_size, "index")
        if hashlib.sha256(raw).digest() != header.index_sha256:
            raise ArchiveError("index SHA-256 mismatch")
        if len(raw) < INDEX_HEADER.size:
            raise ArchiveError("truncated index header")
        magic, major, minor, count, total_size = INDEX_HEADER.unpack_from(raw)
        if magic != INDEX_MAGIC:
            raise ArchiveError("invalid index magic")
        if major != FORMAT_MAJOR or minor > FORMAT_MINOR:
            raise ArchiveError(f"unsupported index version {major}.{minor}")
        if count != header.entry_count or total_size != len(raw):
            raise ArchiveError("index header is inconsistent with the archive header")

        entries: list[EntryInfo] = []
        position = INDEX_HEADER.size
        while position < len(raw):
            entry, position = _parse_record(raw, position)
            entries.append(entry)
            if len(entries) > count:
                raise ArchiveError("index contains more records than declared")
        if position != len(raw) or len(entries) != count:
            raise ArchiveError("index record count or boundary mismatch")
        return entries

    def _validate_organization(self) -> None:
        previous: bytes | None = None
        by_path: dict[str, EntryInfo] = {}
        intervals: list[tuple[int, int, str]] = []
        for entry in self.entries:
            encoded = entry.path.encode("utf-8")
            if previous is not None and encoded <= previous:
                if encoded == previous:
                    raise ArchiveError(f"duplicate entry path: {entry.path}")
                raise ArchiveError("index entries are not sorted by UTF-8 path")
            previous = encoded
            by_path[entry.path] = entry
            if entry.flags != 0:
                raise ArchiveError(f"unsupported entry flags for {entry.path}")
            if entry.mode & ~0o7777:
                raise ArchiveError(f"invalid mode bits for {entry.path}")
            if entry.entry_type == TYPE_DIRECTORY:
                if (
                    entry.method != METHOD_STORE
                    or entry.original_size
                    or entry.stored_size
                    or entry.data_offset
                    or entry.data_crc32
                    or entry.stored_crc32
                    or entry.sha256 != hashlib.sha256(b"").digest()
                ):
                    raise ArchiveError(f"directory fields are inconsistent: {entry.path}")
            elif entry.entry_type == TYPE_FILE:
                if entry.method not in (METHOD_STORE, METHOD_ZLIB):
                    raise ArchiveError(f"unsupported method for {entry.path}")
                if entry.method == METHOD_STORE and entry.stored_size != entry.original_size:
                    raise ArchiveError(f"stored file size mismatch: {entry.path}")
                start = entry.data_offset
                end = start + entry.stored_size
                if start < self.header.data_offset or end > self.header.index_offset:
                    raise ArchiveError(f"file data region is out of bounds: {entry.path}")
                intervals.append((start, end, entry.path))
            else:
                raise ArchiveError(f"unsupported entry type for {entry.path}")

        for path, entry in by_path.items():
            parent = str(PurePosixPath(path).parent)
            if parent != ".":
                parent_entry = by_path.get(parent)
                if parent_entry is None or parent_entry.entry_type != TYPE_DIRECTORY:
                    raise ArchiveError(f"missing directory entry for parent of {path}")

        cursor = self.header.data_offset
        for start, end, path in sorted(intervals):
            if start != cursor:
                raise ArchiveError(f"noncanonical gap or overlap before {path}")
            cursor = end
        if cursor != self.header.index_offset:
            raise ArchiveError("data region has trailing gap or unindexed bytes")

    def verify(self) -> None:
        with self.path.open("rb") as handle:
            for entry in self.entries:
                if entry.entry_type == TYPE_FILE:
                    _verify_file_data(handle, entry)

    def summary(self) -> dict[str, int | str]:
        files = sum(entry.entry_type == TYPE_FILE for entry in self.entries)
        directories = len(self.entries) - files
        original = sum(entry.original_size for entry in self.entries)
        stored = sum(entry.stored_size for entry in self.entries)
        return {
            "format": "Rosetta Archive",
            "version": f"{self.header.major}.{self.header.minor}",
            "archive_size": self.header.archive_size,
            "entries": len(self.entries),
            "files": files,
            "directories": directories,
            "original_bytes": original,
            "stored_bytes": stored,
            "index_offset": self.header.index_offset,
            "index_size": self.header.index_size,
        }


def _parse_record(raw: bytes, position: int) -> tuple[EntryInfo, int]:
    if len(raw) - position < ENTRY.size:
        raise ArchiveError("truncated index entry")
    fixed = raw[position : position + ENTRY.size]
    values = ENTRY.unpack(fixed)
    (
        magic,
        record_size,
        entry_type,
        flags,
        method,
        reserved_byte,
        path_length,
        mode,
        uid,
        gid,
        mtime_ns,
        original_size,
        stored_size,
        data_offset,
        data_crc32,
        stored_crc32,
        sha256,
        record_crc,
        reserved,
    ) = values
    if magic != ENTRY_MAGIC:
        raise ArchiveError(f"invalid entry magic at index offset {position}")
    if reserved_byte != 0 or reserved != b"\x00" * 4:
        raise ArchiveError("nonzero reserved entry fields")
    if path_length > MAX_PATH_BYTES:
        raise ArchiveError("entry path length exceeds parser limit")
    if record_size < ENTRY.size + path_length or position + record_size > len(raw):
        raise ArchiveError("entry record size is invalid")
    path_start = position + ENTRY.size
    path_end = path_start + path_length
    path_bytes = raw[path_start:path_end]
    extension = raw[path_end : position + record_size]
    zeroed = fixed[:100] + b"\x00" * 4 + fixed[104:]
    if _crc32(zeroed + path_bytes + extension) != record_crc:
        raise ArchiveError("entry record CRC32 mismatch")
    try:
        path_text = path_bytes.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise ArchiveError("entry path is not valid UTF-8") from error
    path, canonical = _normal_path(path_text)
    if path != path_text or canonical != path_bytes:
        raise ArchiveError(f"entry path is not canonical NFC: {path_text!r}")
    _validate_extensions(extension)
    return (
        EntryInfo(
            path=path,
            entry_type=entry_type,
            flags=flags,
            method=method,
            mode=mode,
            uid=uid,
            gid=gid,
            mtime_ns=mtime_ns,
            original_size=original_size,
            stored_size=stored_size,
            data_offset=data_offset,
            data_crc32=data_crc32,
            stored_crc32=stored_crc32,
            sha256=sha256,
            extension=extension,
        ),
        position + record_size,
    )


def _validate_extensions(extension: bytes) -> None:
    position = 0
    seen: set[int] = set()
    while position < len(extension):
        if len(extension) - position < EXTENSION_HEADER.size:
            raise ArchiveError("truncated entry extension header")
        extension_type, flags, payload_size = EXTENSION_HEADER.unpack_from(
            extension, position
        )
        position += EXTENSION_HEADER.size
        if extension_type == 0 or extension_type in seen:
            raise ArchiveError("invalid or duplicate entry extension type")
        seen.add(extension_type)
        if flags & ~EXTENSION_FLAG_CRITICAL:
            raise ArchiveError("unsupported entry extension flags")
        if payload_size > len(extension) - position:
            raise ArchiveError("entry extension payload is out of bounds")
        # Version 1.0 defines no payload types. Optional unknown types are skipped.
        if flags & EXTENSION_FLAG_CRITICAL:
            raise ArchiveError(f"unsupported critical entry extension {extension_type}")
        position += payload_size


def _stream_original(handle: BinaryIO, entry: EntryInfo) -> Iterator[bytes]:
    handle.seek(entry.data_offset)
    remaining = entry.stored_size
    if entry.method == METHOD_STORE:
        while remaining:
            chunk = _read_exact(handle, min(CHUNK_SIZE, remaining), f"data for {entry.path}")
            remaining -= len(chunk)
            yield chunk
        return

    decompressor = zlib.decompressobj()
    produced = 0
    while remaining:
        compressed = _read_exact(
            handle, min(CHUNK_SIZE, remaining), f"compressed data for {entry.path}"
        )
        remaining -= len(compressed)
        pending = compressed
        while pending:
            limit = min(CHUNK_SIZE, entry.original_size - produced + 1)
            if limit <= 0:
                raise ArchiveError(f"decompressed size exceeds declaration: {entry.path}")
            try:
                output = decompressor.decompress(pending, limit)
            except zlib.error as error:
                raise ArchiveError(f"invalid zlib stream for {entry.path}: {error}") from error
            pending = decompressor.unconsumed_tail
            if output:
                produced += len(output)
                yield output
            if not output and pending and limit > 0:
                raise ArchiveError(f"zlib decoder made no progress for {entry.path}")
        if decompressor.unused_data:
            raise ArchiveError(f"trailing bytes in zlib stream for {entry.path}")
    try:
        tail = decompressor.flush()
    except zlib.error as error:
        raise ArchiveError(f"invalid zlib trailer for {entry.path}: {error}") from error
    if tail:
        produced += len(tail)
        if produced > entry.original_size:
            raise ArchiveError(f"decompressed size exceeds declaration: {entry.path}")
        yield tail
    if not decompressor.eof:
        raise ArchiveError(f"truncated zlib stream for {entry.path}")


def _verify_file_data(handle: BinaryIO, entry: EntryInfo) -> None:
    handle.seek(entry.data_offset)
    remaining = entry.stored_size
    stored_crc = 0
    while remaining:
        chunk = _read_exact(handle, min(CHUNK_SIZE, remaining), f"stored data for {entry.path}")
        remaining -= len(chunk)
        stored_crc = _crc32(chunk, stored_crc)
    if stored_crc != entry.stored_crc32:
        raise ArchiveError(f"stored CRC32 mismatch for {entry.path}")

    original_crc = 0
    original_sha = hashlib.sha256()
    original_size = 0
    for chunk in _stream_original(handle, entry):
        original_size += len(chunk)
        if original_size > entry.original_size:
            raise ArchiveError(f"decompressed size exceeds declaration: {entry.path}")
        original_crc = _crc32(chunk, original_crc)
        original_sha.update(chunk)
    if original_size != entry.original_size:
        raise ArchiveError(f"original size mismatch for {entry.path}")
    if original_crc != entry.data_crc32:
        raise ArchiveError(f"data CRC32 mismatch for {entry.path}")
    if original_sha.digest() != entry.sha256:
        raise ArchiveError(f"SHA-256 mismatch for {entry.path}")


def _ensure_no_symlink_ancestors(root: Path, target: Path) -> None:
    current = root
    if current.exists() and current.is_symlink():
        raise ArchiveError(f"output root is a symbolic link: {root}")
    relative = target.relative_to(root)
    for part in relative.parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ArchiveError(f"symbolic link in extraction path: {current}")


def extract_archive(
    archive: os.PathLike[str] | str,
    output: os.PathLike[str] | str,
    *,
    preserve_owner: bool = False,
) -> None:
    reader = ArchiveReader(archive)
    reader.verify()  # No filesystem writes occur until the complete archive passes.
    root = Path(output)
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise ArchiveError("output must be a directory and must not be a symbolic link")
    root.mkdir(parents=True, exist_ok=True)
    root_resolved = root.resolve()

    directory_entries: list[tuple[Path, EntryInfo]] = []
    with reader.path.open("rb") as handle:
        for entry in reader.entries:
            destination = root / Path(*PurePosixPath(entry.path).parts)
            _ensure_no_symlink_ancestors(root, destination)
            if destination.resolve(strict=False).is_relative_to(root_resolved) is False:
                raise ArchiveError(f"path escapes extraction root: {entry.path}")
            if entry.entry_type == TYPE_DIRECTORY:
                if destination.exists() and (destination.is_symlink() or not destination.is_dir()):
                    raise ArchiveError(f"cannot create directory over existing path: {destination}")
                destination.mkdir(exist_ok=True)
                directory_entries.append((destination, entry))
                continue

            destination.parent.mkdir(parents=True, exist_ok=True)
            _ensure_no_symlink_ancestors(root, destination)
            if destination.exists() or destination.is_symlink():
                raise ArchiveError(f"refusing to overwrite extracted path: {destination}")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(destination, flags, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as target:
                    for chunk in _stream_original(handle, entry):
                        target.write(chunk)
                os.chmod(destination, entry.mode & 0o7777, follow_symlinks=False)
                os.utime(
                    destination,
                    ns=(entry.mtime_ns, entry.mtime_ns),
                    follow_symlinks=False,
                )
                if preserve_owner:
                    os.chown(destination, entry.uid, entry.gid, follow_symlinks=False)
            except BaseException:
                try:
                    destination.unlink()
                except FileNotFoundError:
                    pass
                raise

    for destination, entry in sorted(
        directory_entries, key=lambda item: len(item[0].parts), reverse=True
    ):
        os.chmod(destination, entry.mode & 0o7777, follow_symlinks=False)
        os.utime(destination, ns=(entry.mtime_ns, entry.mtime_ns), follow_symlinks=False)
        if preserve_owner:
            os.chown(destination, entry.uid, entry.gid, follow_symlinks=False)


def _read_footer_for_recovery(handle: BinaryIO, size: int) -> tuple[int, int]:
    if size < FOOTER.size + INDEX_HEADER.size:
        raise ArchiveError("archive is too small for recovery")
    handle.seek(size - FOOTER.size)
    raw = _read_exact(handle, FOOTER.size, "recovery footer")
    values = FOOTER.unpack(raw)
    magic, major, minor, _flags, index_offset, index_size, archive_size, header_offset, _index_crc, footer_crc, reserved = values
    if magic != FOOTER_MAGIC:
        raise ArchiveError("recovery footer magic not found")
    if major != FORMAT_MAJOR or minor > FORMAT_MINOR:
        raise ArchiveError(f"unsupported recovery footer version {major}.{minor}")
    if archive_size != size or header_offset != 0 or reserved != b"\x00" * 8:
        raise ArchiveError("recovery footer fields are inconsistent")
    zeroed = raw[:52] + b"\x00" * 4 + raw[56:]
    if _crc32(zeroed) != footer_crc:
        raise ArchiveError("recovery footer CRC32 mismatch")
    if index_size > MAX_INDEX_BYTES or index_offset < HEADER.size or index_offset + index_size != size - FOOTER.size:
        raise ArchiveError("recovery index bounds are invalid")
    return index_offset, index_size


def _recover_records(handle: BinaryIO, index: bytes, index_offset: int) -> tuple[list[SourceEntry], int]:
    position = INDEX_HEADER.size if index.startswith(INDEX_MAGIC) else 0
    recovered: list[SourceEntry] = []
    skipped = 0
    seen: set[str] = set()
    while position < len(index):
        found = index.find(ENTRY_MAGIC, position)
        if found < 0:
            break
        try:
            entry, next_position = _parse_record(index, found)
            if entry.path in seen:
                raise ArchiveError("duplicate recovery path")
            if entry.entry_type == TYPE_FILE:
                if entry.data_offset < HEADER.size or entry.data_offset + entry.stored_size > index_offset:
                    raise ArchiveError("recovery data bounds are invalid")
                _verify_file_data(handle, entry)
                data = b"".join(_stream_original(handle, entry))
            elif entry.entry_type == TYPE_DIRECTORY:
                data = b""
            else:
                raise ArchiveError("unsupported recovery entry type")
            recovered.append(
                SourceEntry(
                    path=entry.path,
                    entry_type=entry.entry_type,
                    mode=entry.mode,
                    uid=entry.uid,
                    gid=entry.gid,
                    mtime_ns=entry.mtime_ns,
                    data=data,
                )
            )
            seen.add(entry.path)
            position = next_position
        except ArchiveError:
            skipped += 1
            position = found + len(ENTRY_MAGIC)

    # A surviving file may have lost a directory record. Synthesize safe parents.
    by_path = {item.path: item for item in recovered}
    for item in list(recovered):
        parent = PurePosixPath(item.path).parent
        while str(parent) != ".":
            parent_text = str(parent)
            existing = by_path.get(parent_text)
            if existing is not None and existing.entry_type != TYPE_DIRECTORY:
                recovered.remove(item)
                skipped += 1
                break
            if existing is None:
                synthetic = SourceEntry(
                    path=parent_text,
                    entry_type=TYPE_DIRECTORY,
                    mode=0o755,
                    uid=0,
                    gid=0,
                    mtime_ns=0,
                )
                by_path[parent_text] = synthetic
                recovered.append(synthetic)
            parent = parent.parent
    recovered.sort(key=lambda item: item.path.encode("utf-8"))
    return recovered, skipped


def recover_archive(
    archive: os.PathLike[str] | str,
    output: os.PathLike[str] | str,
    *,
    compression_level: int = 6,
) -> RecoveryResult:
    source = Path(archive)
    size = source.stat().st_size
    with source.open("rb") as handle:
        index_offset, index_size = _read_footer_for_recovery(handle, size)
        handle.seek(index_offset)
        index = _read_exact(handle, index_size, "recovery index")
        recovered, skipped = _recover_records(handle, index, index_offset)
    if not recovered:
        raise ArchiveError("no valid entries could be recovered")
    _atomic_write(Path(output), _build_archive(recovered, compression_level, False))
    return RecoveryResult(recovered=len(recovered), skipped=skipped)
