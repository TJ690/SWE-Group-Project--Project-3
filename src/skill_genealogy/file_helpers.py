"""Small helpers shared by every skill_genealogy stage.

What: command-line flags, input checks, parquet read and write, a DuckDB
connection, and the short summary each stage prints.
Inputs: paths and frames the caller already has. This module does not
choose stage paths; those live in ``settings.py``.
Outputs: parquet files the caller names, plus lines on standard output.
"""

from __future__ import annotations

import argparse
import glob
from pathlib import Path

import duckdb
import pandas as pd

_CELL_WIDTH = 80


def build_arg_parser(description: str) -> argparse.ArgumentParser:
    """Build the parser every stage uses.

    The parser has two flags: ``--dev`` and ``--force``. The stage calls
    ``parse_args()`` on the parser this returns.

    Inputs: ``description`` — one line shown in ``--help``.
    Outputs: an ``ArgumentParser``. Nothing is parsed yet.

    Example:
    >>> parser = build_arg_parser("check the raw tables")
    >>> parser.parse_args(["--dev"]).dev
    True
    """
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--dev",
        action="store_true",
        help="use the 30-registry sample and write to the _dev folders",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="rebuild outputs even when the output files already exist",
    )
    return parser


def check_inputs_exist(paths: dict[str, Path | str], builders: dict[str, str]) -> None:
    """Raise ``FileNotFoundError`` when any input is missing.

    A glob (a path that contains ``*``, ``?`` or ``[``) counts as present
    when it matches at least one file. A normal path counts when the file
    or folder exists. The error names every missing input and the command
    from ``builders`` that creates it.

    Inputs: ``paths`` maps a short name to a file, folder or glob.
    ``builders`` maps that same name to the command that builds it.
    Outputs: none. Returns when every input is present.
    """
    missing: list[str] = []
    for name, path in paths.items():
        if _input_exists(path):
            continue
        command = builders.get(name, "unknown command")
        missing.append(f"{name}: {path} (build with: {command})")
    if missing:
        detail = "\n".join(missing)
        raise FileNotFoundError(f"Missing inputs:\n{detail}")


def outputs_exist(paths: list[Path]) -> bool:
    """Return True when every output file is already on disk.

    A missing file, or a path that is a folder, makes this False.
    An empty list is True: there is nothing left to write.

    Inputs: ``paths`` — output files for one stage.
    Outputs: True or False. Does not create files.
    """
    return all(path.is_file() for path in paths)


def read_parquet(path: Path | str) -> pd.DataFrame:
    """Read one parquet file into a DataFrame.

    Inputs: ``path`` — a parquet file.
    Outputs: the table. Row order is the order stored in the file.
    """
    return pd.read_parquet(path)


def write_parquet(frame: pd.DataFrame, path: Path, sort_by: list[str] | None = None) -> None:
    """Write a DataFrame to parquet with zstd compression.

    When ``sort_by`` is given, rows are sorted by those columns first
    (stable sort). The parent folder is created if it is missing.
    The caller's frame is left unchanged.

    Inputs: ``frame`` — rows to store. ``path`` — destination file.
    ``sort_by`` — column names, or None to keep the current order.
    Outputs: the parquet file at ``path``.
    """
    to_write = frame
    if sort_by:
        to_write = frame.sort_values(list(sort_by), kind="mergesort").reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    to_write.to_parquet(path, compression="zstd", index=False)


def connect_duckdb() -> duckdb.DuckDBPyConnection:
    """Open an in-memory DuckDB connection.

    ``threads`` stays at DuckDB's default. The caller closes the connection.

    Inputs: none.
    Outputs: a connection with no tables loaded.

    Example:
    >>> connect_duckdb().execute("select 1").fetchone()
    (1,)
    """
    return duckdb.connect(database=":memory:")


def print_sample_records(frame: pd.DataFrame, title: str, count: int = 2) -> None:
    """Print ``title``, then up to ``count`` rows as ``column: value`` lines.

    Strings longer than 80 characters are cut to 80. Newlines become spaces
    so each column stays on one line. A blank line separates records.

    Inputs: ``frame`` — the output table. ``title`` — a short heading.
    ``count`` — how many rows to show (default 2).
    Outputs: lines on standard output.
    """
    print(title)
    rows = list(frame.head(count).iterrows())
    for index, (_, row) in enumerate(rows):
        if index:
            print()
        for column, value in row.items():
            print(f"{column}: {_cut_cell(value)}")


def print_summary(title: str, numbers: dict[str, object]) -> None:
    """Print a one-line heading and one ``name: value`` line per count.

    Inputs: ``title`` — what the numbers describe.
    ``numbers`` — names and counts, in the order to print.
    Outputs: lines on standard output.
    """
    print(title)
    for name, value in numbers.items():
        print(f"{name}: {value}")


def _input_exists(path: Path | str) -> bool:
    """Return True when ``path`` is on disk, or a glob matches one file."""
    text = str(path)
    if glob.has_magic(text):
        return any(Path(match).is_file() for match in glob.glob(text))
    return Path(text).exists()


def _cut_cell(value: object) -> str:
    """Turn one cell into one short line of at most 80 characters."""
    text = str(value).replace("\r", " ").replace("\n", " ")
    if len(text) <= _CELL_WIDTH:
        return text
    return text[:_CELL_WIDTH]
