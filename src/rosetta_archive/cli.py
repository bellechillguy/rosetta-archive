"""Command-line interface for the RTA reference implementation."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from . import __version__
from .archive import (
    ArchiveError,
    ArchiveReader,
    create_archive,
    extract_archive,
    recover_archive,
)


def _size(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{value} B"
            return f"{amount:.2f} {unit}"
        amount /= 1024
    return f"{value} B"


def _mtime(value: int) -> str:
    try:
        return datetime.fromtimestamp(value / 1_000_000_000, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return f"{value} ns"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rta", description="Rosetta Archive reference implementation"
    )
    parser.add_argument("--version", action="version", version=f"rta {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="create a deterministic archive")
    create.add_argument("archive")
    create.add_argument("inputs", nargs="+")
    create.add_argument(
        "--compression-level", type=int, default=6, metavar="0..9"
    )
    create.add_argument(
        "--store", action="store_true", help="disable per-file zlib compression"
    )

    listing = commands.add_parser("list", help="list entries without extraction")
    listing.add_argument("archive")
    listing.add_argument("--json", action="store_true")

    info = commands.add_parser("info", help="show archive summary")
    info.add_argument("archive")
    info.add_argument("--json", action="store_true")

    inspect = commands.add_parser("inspect", help="show format and integrity metadata")
    inspect.add_argument("archive")
    inspect.add_argument("--json", action="store_true")

    verify = commands.add_parser("verify", help="verify structure and all file content")
    verify.add_argument("archive")

    extract = commands.add_parser("extract", help="safely extract a verified archive")
    extract.add_argument("archive")
    extract.add_argument("output")
    extract.add_argument(
        "--preserve-owner",
        action="store_true",
        help="apply stored uid/gid (normally requires elevated privileges)",
    )

    recover = commands.add_parser(
        "recover", help="salvage independently valid entries into a new archive"
    )
    recover.add_argument("archive")
    recover.add_argument("output")
    recover.add_argument(
        "--compression-level", type=int, default=6, metavar="0..9"
    )
    return parser


def _list(reader: ArchiveReader, as_json: bool) -> None:
    rows = [
        {
            "path": entry.path,
            "type": entry.type_label,
            "size": entry.original_size,
            "stored_size": entry.stored_size,
            "method": entry.method_label,
        }
        for entry in reader.entries
    ]
    if as_json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    print(f"{'TYPE':<10} {'SIZE':>12} {'STORED':>12} {'METHOD':<8} PATH")
    for row in rows:
        print(
            f"{row['type']:<10} {row['size']:>12} {row['stored_size']:>12} "
            f"{row['method']:<8} {row['path']}"
        )


def _info(reader: ArchiveReader, as_json: bool) -> None:
    summary = reader.summary()
    if as_json:
        print(json.dumps(summary, indent=2))
        return
    labels = (
        ("Format", summary["format"]),
        ("Version", summary["version"]),
        ("Archive size", _size(int(summary["archive_size"]))),
        ("Entries", summary["entries"]),
        ("Files", summary["files"]),
        ("Directories", summary["directories"]),
        ("Original data", _size(int(summary["original_bytes"]))),
        ("Stored data", _size(int(summary["stored_bytes"]))),
        ("Index", f"offset {summary['index_offset']}, {_size(int(summary['index_size']))}"),
        ("Deterministic", "yes" if reader.header.flags & 1 else "no"),
    )
    for label, value in labels:
        print(f"{label:<16}: {value}")


def _inspect(reader: ArchiveReader, as_json: bool) -> None:
    document = {
        "header": {
            "magic": "RTA\\x1a\\r\\n\\x00\\x00",
            "version": f"{reader.header.major}.{reader.header.minor}",
            "flags": f"0x{reader.header.flags:08x}",
            "data_offset": reader.header.data_offset,
            "index_offset": reader.header.index_offset,
            "index_size": reader.header.index_size,
            "archive_size": reader.header.archive_size,
            "created_ns": reader.header.created_ns,
            "index_sha256": reader.header.index_sha256.hex(),
        },
        "entries": [
            {
                "path": entry.path,
                "type": entry.type_label,
                "mode": f"{entry.mode:04o}",
                "uid": entry.uid,
                "gid": entry.gid,
                "mtime_ns": entry.mtime_ns,
                "mtime_utc": _mtime(entry.mtime_ns),
                "method": entry.method_label,
                "original_size": entry.original_size,
                "stored_size": entry.stored_size,
                "data_offset": entry.data_offset,
                "data_crc32": f"{entry.data_crc32:08x}",
                "stored_crc32": f"{entry.stored_crc32:08x}",
                "sha256": entry.sha256.hex(),
                "extension_bytes": len(entry.extension),
            }
            for entry in reader.entries
        ],
    }
    if as_json:
        print(json.dumps(document, indent=2, ensure_ascii=False))
        return
    header = document["header"]
    print("HEADER")
    for key, value in header.items():
        print(f"  {key:<18} {value}")
    print("\nENTRIES")
    for number, item in enumerate(document["entries"]):
        print(f"  [{number}] {item['path']} ({item['type']})")
        for key in (
            "mode",
            "uid",
            "gid",
            "mtime_ns",
            "method",
            "original_size",
            "stored_size",
            "data_offset",
            "data_crc32",
            "stored_crc32",
            "sha256",
            "extension_bytes",
        ):
            print(f"      {key:<17} {item[key]}")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            create_archive(
                args.archive,
                args.inputs,
                compression_level=args.compression_level,
                force_store=args.store,
            )
            reader = ArchiveReader(args.archive)
            print(
                f"created {args.archive}: {len(reader.entries)} entries, "
                f"{_size(reader.header.archive_size)}"
            )
        elif args.command == "list":
            _list(ArchiveReader(args.archive), args.json)
        elif args.command == "info":
            _info(ArchiveReader(args.archive), args.json)
        elif args.command == "inspect":
            _inspect(ArchiveReader(args.archive), args.json)
        elif args.command == "verify":
            reader = ArchiveReader(args.archive)
            reader.verify()
            print(f"OK: {args.archive} ({len(reader.entries)} entries verified)")
        elif args.command == "extract":
            extract_archive(
                args.archive, args.output, preserve_owner=args.preserve_owner
            )
            print(f"extracted {args.archive} to {args.output}")
        elif args.command == "recover":
            result = recover_archive(
                args.archive,
                args.output,
                compression_level=args.compression_level,
            )
            print(
                f"recovered {result.recovered} entries to {args.output}; "
                f"skipped {result.skipped} damaged records"
            )
        else:
            parser.error("unknown command")
    except (ArchiveError, OSError) as error:
        print(f"rta: error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
