"""Read-only access to SQLite files living inside archived exports.

Archived exports are immutable. Nothing here may ever open a database for
writing, and nothing may modify the archive: a database packed inside a zip is
extracted to a scratch directory, read, and discarded.
"""

from __future__ import annotations

import lzma
import shutil
import sqlite3
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def _lossy_text(raw: bytes) -> str:
    """Decode a TEXT column, replacing bytes that are not valid UTF-8.

    sqlite3's default factory raises `OperationalError` on such a column, and it
    does so from the cursor, so one bad byte anywhere aborts the entire table read
    rather than spoiling one field.

    Archives contain such bytes. A Firefox `places.sqlite` here has one page
    description ending in `\\xed\\xa0\\xbe` — a lone UTF-16 high surrogate, left
    behind when Firefox truncated the description to 256 characters and cut an
    emoji's surrogate pair in half. Its other half is already gone, so there is
    nothing to recover; the choice is between U+FFFD in one field and losing 13783
    pages.

    `replace` rather than `surrogateescape` on purpose: a surrogate survives in a
    `str` but then raises again on `print`, `json.dumps` or any encode, which moves
    the failure somewhere far from here. U+FFFD is visibly wrong and inert.
    """
    return raw.decode("utf-8", "replace")


@contextmanager
def sqlite_ro(path: Path) -> Iterator[sqlite3.Connection]:
    """Open a plain SQLite file read-only.

    `immutable=1` also stops sqlite from touching the file at all (no -wal/-shm,
    no recovery), which is what we want for an archive. It is only safe because
    the file is guaranteed not to be written concurrently.
    """
    uri = f"file:{path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.text_factory = _lossy_text
        yield conn
    finally:
        conn.close()


@contextmanager
def sqlite_ro_in_zip(archive: Path, member: str) -> Iterator[sqlite3.Connection]:
    """Open a SQLite file stored inside a zip archive, read-only.

    sqlite cannot read from a stream, so the member is extracted to a temporary
    directory that is removed on exit. The archive itself is never modified.
    """
    with tempfile.TemporaryDirectory(prefix="cpi-") as tmp:
        target = Path(tmp) / Path(member).name
        with (
            zipfile.ZipFile(archive) as zf,
            zf.open(member) as src,
            target.open("wb") as dst,
        ):
            shutil.copyfileobj(src, dst)
        with sqlite_ro(target) as conn:
            yield conn


@contextmanager
def sqlite_ro_xz(archive: Path) -> Iterator[sqlite3.Connection]:
    """Open an xz-compressed SQLite file, read-only.

    Same constraint as the zip case: sqlite cannot read from a stream, so the
    database is decompressed to a temporary directory that is removed on exit. The
    archive itself is never modified.

    Decompressed in a chunked copy rather than via `lzma.open(...).read()`: a
    Firefox `places.sqlite` is ~10 MB uncompressed and there is no reason to hold a
    whole database in memory to hand sqlite a path.
    """
    with tempfile.TemporaryDirectory(prefix="cpi-") as tmp:
        target = Path(tmp) / archive.with_suffix("").name
        with lzma.open(archive) as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        with sqlite_ro(target) as conn:
            yield conn


def zip_members(archive: Path) -> tuple[str, ...]:
    """Names of the files inside an export zip. Useful when probing a new export."""
    with zipfile.ZipFile(archive) as zf:
        return tuple(i.filename for i in zf.infolist() if not i.is_dir())
