"""Binary constants and small helpers for RTA version 1.0."""

from __future__ import annotations

import struct

FORMAT_MAJOR = 1
FORMAT_MINOR = 0

HEADER_MAGIC = b"RTA\x1a\r\n\x00\x00"
INDEX_MAGIC = b"RTAIDX\x00\x00"
ENTRY_MAGIC = b"REN1"
FOOTER_MAGIC = b"RTAEND\x00\x00"

# All integers are little endian. Struct sizes are part of the on-disk format.
HEADER = struct.Struct("<8sHHIIIQQQQq32sI12s")
INDEX_HEADER = struct.Struct("<8sHHIQ")
ENTRY = struct.Struct("<4sIBBBBIIIIqQQQII32sI4s")
FOOTER = struct.Struct("<8sHHIQQQQII8s")
EXTENSION_HEADER = struct.Struct("<HHI")

assert HEADER.size == 112
assert INDEX_HEADER.size == 24
assert ENTRY.size == 108
assert FOOTER.size == 64

FLAG_DETERMINISTIC = 1 << 0
FLAG_PER_ENTRY_COMPRESSION = 1 << 1
FLAG_RECOVERY_FOOTER = 1 << 2
KNOWN_ARCHIVE_FLAGS = (
    FLAG_DETERMINISTIC | FLAG_PER_ENTRY_COMPRESSION | FLAG_RECOVERY_FOOTER
)

TYPE_FILE = 1
TYPE_DIRECTORY = 2

METHOD_STORE = 0
METHOD_ZLIB = 1

EXTENSION_FLAG_CRITICAL = 1 << 0

MAX_PATH_BYTES = 1 << 20
MAX_INDEX_BYTES = 256 << 20
MAX_ENTRIES = 1_000_000
CHUNK_SIZE = 1 << 20

# Offsets of CRC fields inside their respective fixed structures.
HEADER_CRC_OFFSET = 96
ENTRY_CRC_OFFSET = 100
FOOTER_CRC_OFFSET = 52


def method_name(value: int) -> str:
    return {METHOD_STORE: "store", METHOD_ZLIB: "zlib"}.get(value, f"unknown({value})")


def type_name(value: int) -> str:
    return {TYPE_FILE: "file", TYPE_DIRECTORY: "directory"}.get(
        value, f"unknown({value})"
    )
