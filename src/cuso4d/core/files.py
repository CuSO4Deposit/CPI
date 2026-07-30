"""Locating archived export files.

The only place in CPI that turns configuration into concrete paths. Parsers below
this layer always take an explicit path, so they stay pure and trivially testable.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol


class Ordered[T](Protocol):
    """Anything `sorted` can compare. Contravariant because `str.__lt__` and
    friends only accept their own type, not `object`."""

    def __lt__(self, other: T, /) -> bool: ...


type PathIsh = str | os.PathLike[str]
type Paths = PathIsh | Sequence[PathIsh]
type SortKey = Callable[[Path], Ordered[Any]]


def get_files(
    paths: Paths,
    glob: str = "*",
    key: SortKey | None = None,
) -> tuple[Path, ...]:
    """Resolve archived exports to a sorted tuple of existing files.

    Accepts a single path or a sequence of them. Each may be a literal file, a
    directory (searched with `glob`), or a path containing shell wildcards.

    Ordered by filename, which for a given exporter is chronological order —
    accumulation downstream depends on that. This holds only while the names come
    from one exporter with one convention; a source whose exports are named
    otherwise should pass its own `key`, since only that source knows how to read
    its app's filenames.
    """
    if isinstance(paths, str | os.PathLike):
        paths = [paths]

    found: list[Path] = []
    for p in paths:
        path = Path(p).expanduser()
        if any(c in str(path) for c in "*?["):
            # Path.glob needs a base; split at the first wildcard component.
            parts = path.parts
            first_magic = next(
                i for i, part in enumerate(parts) if any(c in part for c in "*?[")
            )
            base = Path(*parts[:first_magic]) if first_magic else Path()
            pattern = str(Path(*parts[first_magic:]))
            found.extend(x for x in base.glob(pattern) if x.is_file())
        elif path.is_dir():
            found.extend(x for x in path.glob(glob) if x.is_file())
        elif path.is_file():
            found.append(path)
        else:
            raise FileNotFoundError(f"no such export path: {path}")

    if not found:
        raise FileNotFoundError(f"no export files matched: {paths!r}")

    # Deduplicate while keeping a deterministic order.
    return tuple(sorted(set(found), key=key or (lambda p: (p.name, str(p)))))
