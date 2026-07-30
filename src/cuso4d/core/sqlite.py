"""Read-only access to SQLite files living inside archived exports.

Archived exports are immutable. Nothing here may ever open a database for
writing, and nothing may modify the archive: a database packed inside a zip is
extracted to a scratch directory, read, and discarded.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


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


def zip_members(archive: Path) -> tuple[str, ...]:
    """Names of the files inside an export zip. Useful when probing a new export."""
    with zipfile.ZipFile(archive) as zf:
        return tuple(i.filename for i in zf.infolist() if not i.is_dir())
