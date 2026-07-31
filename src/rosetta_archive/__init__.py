"""Rosetta Archive reference implementation."""

from .archive import (
    ArchiveError,
    ArchiveReader,
    create_archive,
    extract_archive,
    recover_archive,
)

__all__ = [
    "ArchiveError",
    "ArchiveReader",
    "create_archive",
    "extract_archive",
    "recover_archive",
]

__version__ = "1.0.0"

