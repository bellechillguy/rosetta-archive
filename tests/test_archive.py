from __future__ import annotations

import binascii
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from rosetta_archive.archive import (
    ArchiveError,
    ArchiveReader,
    create_archive,
    extract_archive,
    recover_archive,
)
from rosetta_archive.cli import main
from rosetta_archive.format import ENTRY, FOOTER, HEADER, INDEX_HEADER


def crc32(data: bytes) -> int:
    return binascii.crc32(data) & 0xFFFFFFFF


def repair_outer_integrity(raw: bytearray) -> None:
    header_values = list(HEADER.unpack(raw[: HEADER.size]))
    index_offset = header_values[6]
    index_size = header_values[7]
    index = bytes(raw[index_offset : index_offset + index_size])
    header_values[11] = __import__("hashlib").sha256(index).digest()
    header_values[12] = 0
    header_zero = HEADER.pack(*header_values)
    header_values[12] = crc32(header_zero)
    raw[: HEADER.size] = HEADER.pack(*header_values)

    footer_offset = len(raw) - FOOTER.size
    footer_values = list(FOOTER.unpack(raw[footer_offset:]))
    footer_values[8] = crc32(index)
    footer_values[9] = 0
    footer_zero = FOOTER.pack(*footer_values)
    footer_values[9] = crc32(footer_zero)
    raw[footer_offset:] = FOOTER.pack(*footer_values)


def repair_record(raw: bytearray, record_offset: int) -> None:
    values = list(ENTRY.unpack(raw[record_offset : record_offset + ENTRY.size]))
    record_size = values[1]
    values[17] = 0
    fixed_zero = ENTRY.pack(*values)
    variable = bytes(raw[record_offset + ENTRY.size : record_offset + record_size])
    values[17] = crc32(fixed_zero + variable)
    raw[record_offset : record_offset + ENTRY.size] = ENTRY.pack(*values)


class ArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inputs = self.root / "sample"
        (self.inputs / "nested" / "empty").mkdir(parents=True)
        (self.inputs / "hello.txt").write_text("Rosetta\n" * 100, encoding="utf-8")
        (self.inputs / "nested" / "binary.bin").write_bytes(
            bytes(range(256)) + b"\x00\xff\x00"
        )
        os.chmod(self.inputs / "hello.txt", 0o640)
        self.archive = self.root / "sample.rta"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_round_trip_and_metadata(self) -> None:
        create_archive(self.archive, [self.inputs])
        reader = ArchiveReader(self.archive)
        reader.verify()
        self.assertEqual(len(reader.entries), 5)
        by_path = {entry.path: entry for entry in reader.entries}
        self.assertEqual(by_path["sample/hello.txt"].mode, 0o640)
        self.assertEqual(by_path["sample/hello.txt"].original_size, 800)
        self.assertIn("sample/nested/empty", by_path)

        output = self.root / "out"
        extract_archive(self.archive, output)
        self.assertEqual(
            (output / "sample" / "hello.txt").read_bytes(),
            (self.inputs / "hello.txt").read_bytes(),
        )
        self.assertEqual(
            (output / "sample" / "nested" / "binary.bin").read_bytes(),
            (self.inputs / "nested" / "binary.bin").read_bytes(),
        )
        self.assertTrue((output / "sample" / "nested" / "empty").is_dir())
        self.assertEqual(
            (output / "sample" / "hello.txt").stat().st_mode & 0o777,
            0o640,
        )

    def test_deterministic_output(self) -> None:
        first = self.root / "one.rta"
        second = self.root / "two.rta"
        create_archive(first, [self.inputs])
        create_archive(second, [self.inputs])
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_changed_payload_is_rejected_before_extraction(self) -> None:
        create_archive(self.archive, [self.inputs])
        reader = ArchiveReader(self.archive)
        file_entry = next(entry for entry in reader.entries if entry.stored_size)
        damaged = bytearray(self.archive.read_bytes())
        damaged[file_entry.data_offset] ^= 0x40
        broken = self.root / "broken.rta"
        broken.write_bytes(damaged)

        parsed = ArchiveReader(broken)
        with self.assertRaisesRegex(ArchiveError, "CRC32|zlib"):
            parsed.verify()
        output = self.root / "should-not-exist"
        with self.assertRaises(ArchiveError):
            extract_archive(broken, output)
        self.assertFalse(output.exists())

    def test_truncated_archive_is_rejected(self) -> None:
        create_archive(self.archive, [self.inputs])
        truncated = self.root / "truncated.rta"
        truncated.write_bytes(self.archive.read_bytes()[:-11])
        with self.assertRaises(ArchiveError):
            ArchiveReader(truncated)

    def test_unsupported_major_version_is_rejected(self) -> None:
        create_archive(self.archive, [self.inputs])
        raw = bytearray(self.archive.read_bytes())
        fields = list(HEADER.unpack(raw[: HEADER.size]))
        fields[1] = 99
        fields[12] = 0
        header = HEADER.pack(*fields)
        fields[12] = crc32(header)
        raw[: HEADER.size] = HEADER.pack(*fields)
        changed = self.root / "future.rta"
        changed.write_bytes(raw)
        with self.assertRaisesRegex(ArchiveError, "unsupported RTA version"):
            ArchiveReader(changed)

    def test_path_traversal_record_is_rejected(self) -> None:
        source = self.root / "safe.txt"
        source.write_text("safe", encoding="utf-8")
        archive = self.root / "traversal.rta"
        create_archive(archive, [source], force_store=True)
        raw = bytearray(archive.read_bytes())
        header_values = list(HEADER.unpack(raw[: HEADER.size]))
        index_offset = header_values[6]
        index_size = header_values[7]
        record_offset = index_offset + INDEX_HEADER.size
        fixed = bytearray(raw[record_offset : record_offset + ENTRY.size])
        entry_values = list(ENTRY.unpack(fixed))
        self.assertEqual(entry_values[6], 8)
        path_offset = record_offset + ENTRY.size
        raw[path_offset : path_offset + 8] = b"../x.txt"

        repair_record(raw, record_offset)
        repair_outer_integrity(raw)
        archive.write_bytes(raw)

        with self.assertRaisesRegex(ArchiveError, "unsafe archive path"):
            ArchiveReader(archive)

    def test_duplicate_entry_is_rejected(self) -> None:
        first = self.root / "a"
        second = self.root / "b"
        first.write_bytes(b"A")
        second.write_bytes(b"B")
        archive = self.root / "duplicate.rta"
        create_archive(archive, [first, second], force_store=True)
        raw = bytearray(archive.read_bytes())
        header_values = HEADER.unpack(raw[: HEADER.size])
        first_record = header_values[6] + INDEX_HEADER.size
        first_size = ENTRY.unpack(raw[first_record : first_record + ENTRY.size])[1]
        second_record = first_record + first_size
        second_path = second_record + ENTRY.size
        raw[second_path : second_path + 1] = b"a"
        repair_record(raw, second_record)
        repair_outer_integrity(raw)
        archive.write_bytes(raw)

        with self.assertRaisesRegex(ArchiveError, "duplicate entry path"):
            ArchiveReader(archive)

    def test_out_of_bounds_data_offset_is_rejected(self) -> None:
        source = self.root / "offset.bin"
        source.write_bytes(b"offset")
        archive = self.root / "offset.rta"
        create_archive(archive, [source], force_store=True)
        raw = bytearray(archive.read_bytes())
        header_values = HEADER.unpack(raw[: HEADER.size])
        record_offset = header_values[6] + INDEX_HEADER.size
        values = list(ENTRY.unpack(raw[record_offset : record_offset + ENTRY.size]))
        values[13] = header_values[6] + 1
        raw[record_offset : record_offset + ENTRY.size] = ENTRY.pack(*values)
        repair_record(raw, record_offset)
        repair_outer_integrity(raw)
        archive.write_bytes(raw)

        with self.assertRaisesRegex(ArchiveError, "out of bounds"):
            ArchiveReader(archive)

    def test_recovery_skips_one_corrupt_file(self) -> None:
        create_archive(self.archive, [self.inputs])
        reader = ArchiveReader(self.archive)
        victim = next(
            entry for entry in reader.entries if entry.path.endswith("hello.txt")
        )
        damaged = bytearray(self.archive.read_bytes())
        damaged[victim.data_offset] ^= 0x01
        broken = self.root / "partial.rta"
        broken.write_bytes(damaged)
        recovered = self.root / "recovered.rta"

        result = recover_archive(broken, recovered)
        self.assertGreaterEqual(result.skipped, 1)
        recovered_reader = ArchiveReader(recovered)
        recovered_reader.verify()
        paths = {entry.path for entry in recovered_reader.entries}
        self.assertNotIn(victim.path, paths)
        self.assertIn("sample/nested/binary.bin", paths)

    def test_required_cli_commands_return_success(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["create", str(self.archive), str(self.inputs)]), 0)
            self.assertEqual(main(["list", str(self.archive)]), 0)
            self.assertEqual(main(["info", str(self.archive)]), 0)
            self.assertEqual(main(["inspect", str(self.archive)]), 0)
            self.assertEqual(main(["verify", str(self.archive)]), 0)
            self.assertEqual(
                main(["extract", str(self.archive), str(self.root / "cli-out")]), 0
            )


if __name__ == "__main__":
    unittest.main()
