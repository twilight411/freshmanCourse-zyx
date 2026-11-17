#!/usr/bin/env python3
"""Auditable BIRD Mini-Dev 2026 SQLite engineering pilot.

This is a deliberately small, standard-library-only harness.  It separates
three things that must not be conflated:

1. inference inputs: question, evidence, and schema context only;
2. generated-SQL execution: read-only SQLite with progress/time limits;
3. grading: gold SQL is looked up only after the model response exists.

No paid API request is made by the implementation or self-check commands.
"""

from __future__ import annotations

import argparse
import collections
import csv
import dataclasses
import hashlib
import io
import json
import os
import pathlib
import re
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable, Sequence


WORKSPACE = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_DATASET = WORKSPACE / "data" / "mini_dev_sqlite_2026.jsonl"
DEFAULT_DB_ROOT = WORKSPACE / "data" / "bird_mini_dev_db" / "dev_databases"
DEFAULT_SELECTION = WORKSPACE / "experiments" / "pilot" / "selection.json"

DATASET_SHA256 = "926af2a0d1b47e5d0d15a7aa8a34d4721007f4550a881555b6a89496b54a2606"
DATABASE_ARCHIVE_MD5 = "7beb6dab11e65f0fde563e644a1ea319"
EXPECTED_DATASET_ROWS = 500
PILOT_SEED = 20261104
PILOT_SIZE = 20
SELECTION_FORMAT_VERSION = 1

DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic"
API_VERSION = "2023-06-01"
MAX_API_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_DESCRIPTION_FIELD_CHARS = 800
MAX_ERROR_CHARS = 2000
MAX_DESCRIPTION_CSV_BYTES = 5 * 1024 * 1024

DB_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")
FENCE_RE = re.compile(r"```(.*?)```", re.DOTALL)

ALLOWED_DIFFICULTIES = {"simple", "moderate", "challenging"}
VARIANTS = ("baseline", "structured")


class PilotError(RuntimeError):
    """Expected operational error, suitable for a concise CLI message."""


@dataclasses.dataclass(frozen=True)
class DatasetRow:
    question_id: int
    db_id: str
    question: str
    evidence: str
    sql: str
    difficulty: str


@dataclasses.dataclass(frozen=True)
class InferenceQuestion:
    """The only data allowed to enter inference prompt construction."""

    question_id: int
    db_id: str
    question: str
    evidence: str


@dataclasses.dataclass(frozen=True)
class SelectionCandidate:
    question_id: int
    db_id: str
    difficulty: str
    rank: str


# ---------------------------------------------------------------------------
# Generic utilities
# ---------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def display_path(path: pathlib.Path) -> str:
    try:
        return str(path.resolve().relative_to(WORKSPACE))
    except ValueError:
        return str(path.resolve())


def clip_text(value: Any, limit: int = MAX_ERROR_CHARS) -> str:
    text = " ".join(str(value).split())
    if len(text) > limit:
        return text[:limit].rstrip() + "... [truncated]"
    return text


def redact_secret(text: Any, secret: str) -> str:
    result = str(text)
    if secret:
        result = result.replace(secret, "[REDACTED]")
    return clip_text(result)


def infer_question(row: DatasetRow) -> InferenceQuestion:
    """Drop SQL and all grading-only metadata at the inference boundary."""

    return InferenceQuestion(
        question_id=row.question_id,
        db_id=row.db_id,
        question=row.question,
        evidence=row.evidence,
    )


# ---------------------------------------------------------------------------
# Dataset and deterministic pilot selection
# ---------------------------------------------------------------------------


def load_dataset(path: pathlib.Path) -> tuple[list[DatasetRow], str]:
    path = path.resolve()
    if not path.is_file():
        raise PilotError(f"dataset not found: {path}")
    actual_sha256 = sha256_file(path)
    if actual_sha256 != DATASET_SHA256:
        raise PilotError(
            f"dataset SHA256 mismatch: expected {DATASET_SHA256}, got {actual_sha256}"
        )

    rows: list[DatasetRow] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PilotError(f"invalid JSON on dataset line {line_number}: {exc.msg}") from None
            if not isinstance(raw, dict):
                raise PilotError(f"dataset line {line_number} is not a JSON object")

            question_id = raw.get("question_id")
            if type(question_id) is not int:
                raise PilotError(f"dataset line {line_number}: question_id must be an integer")
            db_id = raw.get("db_id")
            if not isinstance(db_id, str) or not DB_ID_RE.fullmatch(db_id):
                raise PilotError(f"dataset line {line_number}: invalid db_id")
            question = raw.get("question")
            evidence = raw.get("evidence", "")
            gold_sql = raw.get("SQL")
            difficulty = raw.get("difficulty")
            if not isinstance(question, str) or not question.strip():
                raise PilotError(f"dataset line {line_number}: invalid question")
            if not isinstance(evidence, str):
                raise PilotError(f"dataset line {line_number}: invalid evidence")
            if not isinstance(gold_sql, str) or not gold_sql.strip():
                raise PilotError(f"dataset line {line_number}: invalid SQL")
            if difficulty not in ALLOWED_DIFFICULTIES:
                raise PilotError(f"dataset line {line_number}: invalid difficulty")

            rows.append(
                DatasetRow(
                    question_id=question_id,
                    db_id=db_id,
                    question=question,
                    evidence=evidence,
                    sql=gold_sql,
                    difficulty=difficulty,
                )
            )

    if len(rows) != EXPECTED_DATASET_ROWS:
        raise PilotError(
            f"dataset row count mismatch: expected {EXPECTED_DATASET_ROWS}, got {len(rows)}"
        )
    question_ids = [row.question_id for row in rows]
    if len(set(question_ids)) != len(question_ids):
        raise PilotError("dataset contains duplicate question_id values")
    return rows, actual_sha256


def selection_rank(seed: int, question_id: int) -> str:
    return sha256_text(f"{seed}:{question_id}")


def select_diverse(
    rows: Sequence[DatasetRow], seed: int = PILOT_SEED, size: int = PILOT_SIZE
) -> list[SelectionCandidate]:
    """Pick a deterministic, database-covering, difficulty-balanced pilot.

    Candidates are ordered by a SHA256(seed:question_id) rank.  Greedily choose
    the minimum key below until ``size`` items are selected:

        (db_still_missing, selected_in_db, selected_at_difficulty,
         sha256_rank, question_id)

    The first term forces coverage of every database before any database gets a
    second question.  The next two terms spread additional questions across
    databases and then difficulty levels.  Gold SQL is never consulted.
    """

    if size <= 0:
        raise PilotError("selection size must be positive")
    if size > len(rows):
        raise PilotError("selection size exceeds dataset size")

    candidates = [
        SelectionCandidate(
            question_id=row.question_id,
            db_id=row.db_id,
            difficulty=row.difficulty,
            rank=selection_rank(seed, row.question_id),
        )
        for row in rows
    ]
    chosen_ids: set[int] = set()
    chosen: list[SelectionCandidate] = []
    db_counts: collections.Counter[str] = collections.Counter()
    difficulty_counts: collections.Counter[str] = collections.Counter()

    while len(chosen) < size:
        best_key: tuple[Any, ...] | None = None
        best: SelectionCandidate | None = None
        for candidate in candidates:
            if candidate.question_id in chosen_ids:
                continue
            key = (
                0 if db_counts[candidate.db_id] == 0 else 1,
                db_counts[candidate.db_id],
                difficulty_counts[candidate.difficulty],
                candidate.rank,
                candidate.question_id,
            )
            if best_key is None or key < best_key:
                best_key = key
                best = candidate
        if best is None:
            raise PilotError("selection failed to find a candidate")
        chosen.append(best)
        chosen_ids.add(best.question_id)
        db_counts[best.db_id] += 1
        difficulty_counts[best.difficulty] += 1

    return chosen


def selection_fingerprint(seed: int, dataset_sha256: str, question_ids: Sequence[int]) -> str:
    payload = {
        "selection_seed": seed,
        "dataset_sha256": dataset_sha256,
        "question_ids": list(question_ids),
    }
    return sha256_text(canonical_json(payload))


def build_selection_payload(
    rows: Sequence[DatasetRow],
    chosen: Sequence[SelectionCandidate],
    dataset_path: pathlib.Path,
    dataset_sha256: str,
) -> dict[str, Any]:
    rows_by_id = {row.question_id: row for row in rows}
    items = []
    for candidate in chosen:
        row = rows_by_id[candidate.question_id]
        if row.db_id != candidate.db_id or row.difficulty != candidate.difficulty:
            raise PilotError("internal selection candidate does not match dataset")
        items.append(
            {
                "question_id": row.question_id,
                "db_id": row.db_id,
                "difficulty": row.difficulty,
            }
        )
    question_ids = [item["question_id"] for item in items]
    return {
        "format_version": SELECTION_FORMAT_VERSION,
        "purpose": (
            "20-question engineering pilot only; not a representative or "
            "official benchmark sample"
        ),
        "selection_seed": PILOT_SEED,
        "selection_size": PILOT_SIZE,
        "selection_algorithm": (
            "coverage-first greedy minimum of "
            "(db_still_missing, selected_in_db, selected_at_difficulty, "
            "sha256(seed:question_id), question_id); gold SQL not consulted"
        ),
        "dataset": {
            "path": display_path(dataset_path),
            "sha256": dataset_sha256,
            "rows": len(rows),
        },
        "database_archive": {
            "provided_md5": DATABASE_ARCHIVE_MD5,
            "verification": (
                "official archive was downloaded and its MD5 independently "
                "verified before extracting the SQLite databases; archive then removed"
            ),
        },
        "question_ids": question_ids,
        "items": items,
        "fingerprint_sha256": selection_fingerprint(
            PILOT_SEED, dataset_sha256, question_ids
        ),
    }


def write_json_atomic(path: pathlib.Path, payload: Any, overwrite: bool = False) -> bool:
    """Write JSON atomically; an identical existing file is already current.

    Returns True when the file was written/replaced and False when the existing
    bytes were already identical.
    """

    path = path.resolve()
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if path.exists() and not overwrite:
        try:
            if path.read_text(encoding="utf-8") == text:
                return False
        except OSError:
            pass
        raise PilotError(f"refusing to overwrite existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, path)
    except BaseException:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return True


def load_selection(
    path: pathlib.Path, rows: Sequence[DatasetRow]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = path.resolve()
    if not path.is_file():
        raise PilotError(f"selection file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise PilotError(f"cannot read selection file: {exc}") from None
    if payload.get("format_version") != SELECTION_FORMAT_VERSION:
        raise PilotError("unsupported selection format version")
    if payload.get("selection_seed") != PILOT_SEED:
        raise PilotError(f"selection seed must be {PILOT_SEED}")
    if payload.get("selection_size") != PILOT_SIZE:
        raise PilotError(f"selection size must be {PILOT_SIZE}")
    dataset_meta = payload.get("dataset")
    if not isinstance(dataset_meta, dict):
        raise PilotError("selection is missing dataset metadata")
    if dataset_meta.get("sha256") != DATASET_SHA256:
        raise PilotError("selection dataset SHA256 does not match this pilot dataset")
    items = payload.get("items")
    question_ids = payload.get("question_ids")
    if not isinstance(items, list) or not isinstance(question_ids, list):
        raise PilotError("selection is missing items/question_ids")
    if len(items) != PILOT_SIZE or len(question_ids) != PILOT_SIZE:
        raise PilotError("selection does not contain exactly 20 items")
    if any(not isinstance(item, dict) for item in items):
        raise PilotError("selection items must be JSON objects")
    if question_ids != [item.get("question_id") for item in items]:
        raise PilotError("selection question_ids order does not match items")
    expected_fingerprint = selection_fingerprint(
        PILOT_SEED, DATASET_SHA256, question_ids
    )
    if payload.get("fingerprint_sha256") != expected_fingerprint:
        raise PilotError("selection fingerprint mismatch")

    rows_by_id = {row.question_id: row for row in rows}
    seen: set[int] = set()
    for item in items:
        if not isinstance(item, dict):
            raise PilotError("selection item is not an object")
        question_id = item.get("question_id")
        if type(question_id) is not int or question_id not in rows_by_id:
            raise PilotError("selection contains an unknown question_id")
        if question_id in seen:
            raise PilotError("selection contains a duplicate question_id")
        seen.add(question_id)
        row = rows_by_id[question_id]
        if item.get("db_id") != row.db_id or item.get("difficulty") != row.difficulty:
            raise PilotError("selection metadata does not match the dataset")
    selected_dbs = {item["db_id"] for item in items}
    dataset_dbs = {row.db_id for row in rows}
    if selected_dbs != dataset_dbs:
        raise PilotError("selection must cover every database in the dataset")
    return payload, items


# ---------------------------------------------------------------------------
# Schema context: sqlite_master + PRAGMA + allow-listed description CSVs
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ColumnSchema:
    name: str
    declared_type: str
    not_null: bool
    default_value: Any
    primary_key_order: int
    notes: tuple[tuple[str, str], ...]


@dataclasses.dataclass(frozen=True)
class ForeignKeySchema:
    columns: tuple[str, ...]
    referenced_table: str
    referenced_columns: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class TableSchema:
    name: str
    columns: tuple[ColumnSchema, ...]
    foreign_keys: tuple[ForeignKeySchema, ...]


@dataclasses.dataclass(frozen=True)
class CsvSource:
    filename: str
    sha256: str
    encoding: str
    rows: int


@dataclasses.dataclass(frozen=True)
class SchemaContext:
    db_id: str
    text: str
    sha256: str
    table_count: int
    column_count: int
    csv_sources: tuple[CsvSource, ...]


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def resolve_db_path(db_root: pathlib.Path, db_id: str) -> pathlib.Path:
    if not DB_ID_RE.fullmatch(db_id):
        raise PilotError(f"invalid db_id: {db_id!r}")
    root = db_root.resolve()
    path = (root / db_id / f"{db_id}.sqlite").resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise PilotError(f"database path escapes db root for {db_id}") from None
    if not path.is_file():
        raise PilotError(f"SQLite database not found for {db_id}: {path}")
    return path


def sqlite_read_only_uri(path: pathlib.Path) -> str:
    """Return an immutable read-only URI and refuse pending WAL/journal state.

    ``mode=ro`` alone can create derived ``-shm``/``-wal`` files for a WAL-mode
    database, which would modify the supplied data directory.  ``immutable=1``
    skips locking and those sidecars.  It also ignores WAL contents, so a
    non-empty WAL or rollback journal is rejected rather than silently read
    incorrectly.
    """

    path = path.resolve()
    for suffix in ("-wal", "-journal"):
        sidecar = pathlib.Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size > 0:
            raise PilotError(
                f"refusing immutable read with pending {suffix} state: {sidecar}"
            )
    quoted = urllib.parse.quote(str(path), safe="/")
    return f"file:{quoted}?mode=ro&immutable=1"


def decode_csv_bytes(data: bytes) -> tuple[str, str]:
    try:
        return data.decode("utf-8-sig"), "utf-8-sig"
    except UnicodeDecodeError:
        try:
            return data.decode("cp1252"), "cp1252"
        except UnicodeDecodeError:
            return data.decode("latin-1"), "latin-1"


def _normalize_control_characters(value: str) -> str:
    cleaned = []
    for character in value:
        codepoint = ord(character)
        if character in "\t\r\n":
            cleaned.append(" ")
        elif codepoint < 32 or 0x7F <= codepoint <= 0x9F:
            if 0x80 <= codepoint <= 0x9F:
                try:
                    cleaned.append(bytes([codepoint]).decode("cp1252"))
                except UnicodeDecodeError:
                    cleaned.append(" ")
            else:
                cleaned.append(" ")
        else:
            cleaned.append(character)
    return "".join(cleaned)


def clean_description_value(value: Any, limit: int = MAX_DESCRIPTION_FIELD_CHARS) -> str:
    if value is None:
        return ""
    text = " ".join(_normalize_control_characters(str(value)).split())
    if len(text) > limit:
        return text[:limit].rstrip() + "... [truncated]"
    return text


def csv_notes_for_row(row: dict[str, Any], declared_type: str) -> tuple[tuple[str, str], ...]:
    notes: list[tuple[str, str]] = []

    def add(label: str, value: Any) -> None:
        text = clean_description_value(value)
        if not text:
            return
        if any(text.casefold() == existing.casefold() for _, existing in notes):
            return
        notes.append((label, text))

    add("label", row.get("column_name"))
    add("description", row.get("column_description"))
    data_format = clean_description_value(row.get("data_format"))
    if data_format and data_format.casefold() != clean_description_value(declared_type).casefold():
        add("format", data_format)
    add("values", row.get("value_description"))
    return tuple(notes)


def load_description_csvs(
    db_id: str,
    db_path: pathlib.Path,
    column_types_by_table: dict[str, dict[str, str]],
) -> tuple[dict[str, dict[str, tuple[tuple[str, str], ...]]], tuple[CsvSource, ...]]:
    """Read only direct <db>/database_description/*.csv files for real tables.

    A CSV is allow-listed only when its stem case-insensitively names a table
    discovered in sqlite_master.  Symlinks resolving outside the description
    directory are ignored.  Column rows are matched case-insensitively to
    PRAGMA-derived column names.
    """

    description_dir = db_path.parent / "database_description"
    if not description_dir.is_dir():
        return {}, ()
    description_root = description_dir.resolve()
    table_names = list(column_types_by_table)
    table_by_fold = {name.casefold(): name for name in table_names}
    notes_by_table: dict[str, dict[str, tuple[tuple[str, str], ...]]] = {
        name: {} for name in table_names
    }
    sources: list[CsvSource] = []

    for entry in sorted(description_dir.iterdir(), key=lambda item: item.name.casefold()):
        if entry.suffix.casefold() != ".csv":
            continue
        try:
            resolved = entry.resolve(strict=True)
            resolved.relative_to(description_root)
        except (FileNotFoundError, ValueError, OSError):
            continue
        if not resolved.is_file():
            continue
        table_name = table_by_fold.get(entry.stem.casefold())
        if table_name is None:
            continue

        data = resolved.read_bytes()
        if len(data) > MAX_DESCRIPTION_CSV_BYTES:
            raise PilotError(f"{db_id}: description CSV exceeds size limit: {entry.name}")
        text, encoding = decode_csv_bytes(data)
        try:
            reader = csv.DictReader(io.StringIO(text, newline=""))
            if not reader.fieldnames or "original_column_name" not in reader.fieldnames:
                raise PilotError(
                    f"{db_id}: {entry.name} is missing original_column_name header"
                )
            raw_rows = list(reader)
        except csv.Error as exc:
            raise PilotError(f"{db_id}: cannot parse {entry.name}: {exc}") from None

        column_types = column_types_by_table[table_name]
        for row in raw_rows:
            original = clean_description_value(row.get("original_column_name"), 300)
            if not original:
                continue
            declared_type = column_types.get(original.casefold(), "")
            notes_by_table[table_name][original.casefold()] = csv_notes_for_row(
                row, declared_type
            )
        sources.append(
            CsvSource(
                filename=entry.name,
                sha256=sha256_bytes(data),
                encoding=encoding,
                rows=len(raw_rows),
            )
        )

    return notes_by_table, tuple(sources)


def build_schema_context(db_id: str, db_path: pathlib.Path) -> SchemaContext:
    """Build deterministic schema text from sqlite_master, PRAGMA, and CSVs."""

    db_path = db_path.resolve()
    if not db_path.is_file():
        raise PilotError(f"SQLite database not found: {db_path}")

    connection = sqlite3.connect(sqlite_read_only_uri(db_path), uri=True)
    try:
        table_rows = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        table_names = [str(row[0]) for row in table_rows]
        if not table_names:
            raise PilotError(f"no user tables found in {db_id}")

        pragma_by_table: dict[str, list[Any]] = {}
        column_types_by_table: dict[str, dict[str, str]] = {}
        for table_name in table_names:
            pragma_columns = connection.execute(
                f"PRAGMA table_info({quote_identifier(table_name)})"
            ).fetchall()
            if not pragma_columns:
                raise PilotError(f"table {table_name!r} has no columns in {db_id}")
            pragma_by_table[table_name] = pragma_columns
            column_types_by_table[table_name] = {
                str(row[1]).casefold(): str(row[2] or "") for row in pragma_columns
            }

        notes_by_table, csv_sources = load_description_csvs(
            db_id, db_path, column_types_by_table
        )

        tables: list[TableSchema] = []
        for table_name in table_names:
            columns = []
            for pragma_row in pragma_by_table[table_name]:
                _cid, name, declared_type, not_null, default_value, primary_key = pragma_row
                column_name = str(name)
                columns.append(
                    ColumnSchema(
                        name=column_name,
                        declared_type=str(declared_type or ""),
                        not_null=bool(not_null),
                        default_value=default_value,
                        primary_key_order=int(primary_key or 0),
                        notes=notes_by_table.get(table_name, {}).get(
                            column_name.casefold(), ()
                        ),
                    )
                )

            fk_groups: dict[int, list[Any]] = collections.defaultdict(list)
            for fk_row in connection.execute(
                f"PRAGMA foreign_key_list({quote_identifier(table_name)})"
            ).fetchall():
                fk_groups[int(fk_row[0])].append(fk_row)
            foreign_keys = []
            for fk_id in sorted(fk_groups):
                group = sorted(fk_groups[fk_id], key=lambda row: int(row[1]))
                foreign_keys.append(
                    ForeignKeySchema(
                        columns=tuple(str(row[3]) for row in group),
                        referenced_table=str(group[0][2]),
                        referenced_columns=tuple(
                            str(row[4]) for row in group if row[4] is not None
                        ),
                    )
                )
            tables.append(
                TableSchema(
                    name=table_name,
                    columns=tuple(columns),
                    foreign_keys=tuple(foreign_keys),
                )
            )
    finally:
        connection.close()

    lines = [
        f"SQLite schema for database {db_id!r}.",
        (
            "Derived from sqlite_master, PRAGMA table_info, "
            "PRAGMA foreign_key_list, and allow-listed database_description CSV files."
        ),
    ]
    column_count = 0
    for table in tables:
        lines.append("")
        lines.append(f"TABLE {quote_identifier(table.name)} ({len(table.columns)} columns)")
        for column in table.columns:
            column_count += 1
            attributes = [column.declared_type] if column.declared_type else []
            if column.primary_key_order:
                attributes.append("PRIMARY KEY")
            if column.not_null:
                attributes.append("NOT NULL")
            if column.default_value is not None:
                attributes.append(f"DEFAULT {column.default_value}")
            suffix = (" " + " ".join(attributes)) if attributes else ""
            lines.append(f"  {quote_identifier(column.name)}{suffix}")
            for label, note in column.notes:
                lines.append(f"    {label}: {note}")
        for foreign_key in table.foreign_keys:
            columns_text = ", ".join(quote_identifier(name) for name in foreign_key.columns)
            references = f"REFERENCES {quote_identifier(foreign_key.referenced_table)}"
            if foreign_key.referenced_columns:
                refs_text = ", ".join(
                    quote_identifier(name) for name in foreign_key.referenced_columns
                )
                references += f" ({refs_text})"
            lines.append(f"  FOREIGN KEY ({columns_text}) {references}")

    schema_text = "\n".join(lines).rstrip() + "\n"
    return SchemaContext(
        db_id=db_id,
        text=schema_text,
        sha256=sha256_text(schema_text),
        table_count=len(tables),
        column_count=column_count,
        csv_sources=csv_sources,
    )


# ---------------------------------------------------------------------------
# Prompt construction (inference-only inputs)
# ---------------------------------------------------------------------------


SYSTEM_PROMPT = (
    "You are a precise SQLite text-to-SQL analyst. Use only the supplied schema, "
    "question, and evidence. Produce one executable SQLite query and follow the "
    "output contract exactly."
)

BASELINE_GUIDANCE = (
    "Write the simplest correct SQLite query that answers the question exactly. "
    "Use the supplied schema and evidence; do not invent tables or columns."
)

STRUCTURED_GUIDANCE = """Schema-reading guidance:
1. Identify the entities, measures, filters, grouping level, and requested output columns before writing SQL.
2. Treat column labels, descriptions, formats, and value notes in the schema as part of the schema contract.
3. Prefer the declared primary-key/foreign-key relationships when joining tables.
4. Translate question terms and evidence into the exact stored values or the described derived measures.
5. Preserve the requested row grain and column order. Do not add DISTINCT, aggregation, ordering, or limits unless the question requires them.
6. Check nullable fields, division by zero, text/value matching, date handling, and SQLite syntax before returning.
7. Re-read the question after drafting and verify every requested output is present."""

OUTPUT_CONTRACT = """Output contract:
- Return exactly one SQLite SELECT or WITH query.
- Use only tables and columns present in the schema, with valid SQLite syntax.
- Do not modify data and do not emit a second SQL statement.
- Do not include markdown code fences, comments, or prose.
- The response must begin with SELECT or WITH."""


def build_prompt(
    question: InferenceQuestion, schema: SchemaContext, variant: str
) -> tuple[str, str]:
    if variant not in VARIANTS:
        raise PilotError(f"unknown prompt variant: {variant}")
    guidance = BASELINE_GUIDANCE if variant == "baseline" else STRUCTURED_GUIDANCE
    evidence = question.evidence.strip() or "(none provided)"
    user_prompt = "\n\n".join(
        [
            "## Database schema",
            schema.text.rstrip(),
            "## Question",
            question.question.strip(),
            "## External evidence",
            evidence,
            "## Task guidance",
            guidance,
            "## Output contract",
            OUTPUT_CONTRACT,
        ]
    )
    return SYSTEM_PROMPT, user_prompt


def prompt_hashes(system_prompt: str, user_prompt: str) -> dict[str, str]:
    return {
        "system_prompt_sha256": sha256_text(system_prompt),
        "user_prompt_sha256": sha256_text(user_prompt),
        "prompt_sha256": sha256_text(system_prompt + "\x00" + user_prompt),
    }


# ---------------------------------------------------------------------------
# DeepSeek Anthropic-compatible HTTP call
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ApiSettings:
    base_url: str
    model: str
    max_tokens: int
    temperature: float
    api_timeout: float
    auth_header: str
    allow_http: bool


@dataclasses.dataclass(frozen=True)
class ApiResponse:
    text: str
    prompt_tokens: int | None
    output_tokens: int | None
    response_id: str
    stop_reason: str | None


class ApiError(RuntimeError):
    pass


def messages_url(base_url: str, allow_http: bool) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.username or parsed.password:
        raise PilotError("base URL must not contain credentials")
    if not parsed.netloc:
        raise PilotError("base URL must include a host")
    if parsed.query or parsed.fragment:
        raise PilotError("base URL must not contain a query string or fragment")
    if parsed.scheme not in {"https", "http"}:
        raise PilotError("base URL must be http or https")
    if parsed.scheme == "http" and not allow_http:
        raise PilotError("plain HTTP is disabled; pass --allow-http only for a local test server")
    base = base_url.rstrip("/")
    if base.endswith("/v1/messages"):
        return base
    if base.endswith("/v1"):
        return base + "/messages"
    return base + "/v1/messages"


def _extract_api_error(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return clip_text(text)
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict) and error.get("message"):
            return clip_text(error["message"])
        if isinstance(error, str):
            return clip_text(error)
        if parsed.get("message"):
            return clip_text(parsed["message"])
    return clip_text(text)


def call_api(
    system_prompt: str,
    user_prompt: str,
    token: str,
    settings: ApiSettings,
) -> ApiResponse:
    url = messages_url(settings.base_url, settings.allow_http)
    body = {
        "model": settings.model,
        "max_tokens": settings.max_tokens,
        "temperature": settings.temperature,
        "thinking": {"type": "disabled"},
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Anthropic-Version": API_VERSION,
        "User-Agent": "bird-mini-dev-2026-pilot/1.0",
    }
    if settings.auth_header == "bearer":
        headers["Authorization"] = f"Bearer {token}"
    elif settings.auth_header == "x-api-key":
        headers["X-Api-Key"] = token
    else:
        raise PilotError(f"unsupported auth header mode: {settings.auth_header}")

    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=settings.api_timeout) as response:
            raw = response.read(MAX_API_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            error_body = exc.read(MAX_API_RESPONSE_BYTES + 1)
        except Exception:
            error_body = b""
        detail = redact_secret(_extract_api_error(error_body), token)
        raise ApiError(f"HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise ApiError(f"network error: {redact_secret(exc.reason, token)}") from None
    except TimeoutError:
        raise ApiError(f"API request timed out after {settings.api_timeout:g}s") from None
    except OSError as exc:
        raise ApiError(f"network error: {redact_secret(exc, token)}") from None

    if len(raw) > MAX_API_RESPONSE_BYTES:
        raise ApiError("API response exceeded local size limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError("API returned a non-JSON response") from None
    if not isinstance(payload, dict):
        raise ApiError("API response was not a JSON object")
    if payload.get("type") == "error" or "error" in payload:
        raise ApiError(f"API error: {redact_secret(_extract_api_error(raw), token)}")

    content = payload.get("content", [])
    text_parts: list[str] = []
    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in {None, "text"}:
                block_text = block.get("text")
                if isinstance(block_text, str):
                    text_parts.append(block_text)
    text = "".join(text_parts)

    usage = payload.get("usage")
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    if isinstance(usage, dict):
        raw_prompt = usage.get("input_tokens", usage.get("prompt_tokens"))
        raw_output = usage.get("output_tokens", usage.get("completion_tokens"))
        if type(raw_prompt) is int:
            prompt_tokens = raw_prompt
        if type(raw_output) is int:
            output_tokens = raw_output

    return ApiResponse(
        text=text,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        response_id=str(payload.get("id") or ""),
        stop_reason=payload.get("stop_reason") if isinstance(payload.get("stop_reason"), str) else None,
    )


# ---------------------------------------------------------------------------
# SQL extraction
# ---------------------------------------------------------------------------


def split_sql_statements(text: str) -> list[str]:
    """Split on top-level semicolons while respecting SQLite quoting/comments."""

    statements: list[str] = []
    start = 0
    index = 0
    state = "normal"
    length = len(text)

    while index < length:
        char = text[index]
        nxt = text[index + 1] if index + 1 < length else ""

        if state == "normal":
            if char == "'":
                state = "single"
                index += 1
            elif char == '"':
                state = "double"
                index += 1
            elif char == "`":
                state = "backtick"
                index += 1
            elif char == "[":
                state = "bracket"
                index += 1
            elif char == "-" and nxt == "-":
                state = "line_comment"
                index += 2
            elif char == "/" and nxt == "*":
                state = "block_comment"
                index += 2
            elif char == ";":
                statements.append(text[start:index])
                start = index + 1
                index += 1
            else:
                index += 1
        elif state == "single":
            if char == "'" and nxt == "'":
                index += 2
            elif char == "'":
                state = "normal"
                index += 1
            else:
                index += 1
        elif state == "double":
            if char == '"' and nxt == '"':
                index += 2
            elif char == '"':
                state = "normal"
                index += 1
            else:
                index += 1
        elif state == "backtick":
            if char == "`" and nxt == "`":
                index += 2
            elif char == "`":
                state = "normal"
                index += 1
            else:
                index += 1
        elif state == "bracket":
            if char == "]":
                state = "normal"
            index += 1
        elif state == "line_comment":
            if char in "\r\n":
                state = "normal"
            index += 1
        elif state == "block_comment":
            if char == "*" and nxt == "/":
                state = "normal"
                index += 2
            else:
                index += 1
        else:  # pragma: no cover - defensive
            index += 1

    statements.append(text[start:])
    return statements


def strip_leading_comments(statement: str) -> str:
    text = statement.lstrip()
    while True:
        if text.startswith("--"):
            newline = text.find("\n")
            if newline == -1:
                return ""
            text = text[newline + 1 :].lstrip()
        elif text.startswith("/*"):
            end = text.find("*/", 2)
            if end == -1:
                return ""
            text = text[end + 2 :].lstrip()
        else:
            return text


def sql_leading_keyword(statement: str) -> str | None:
    stripped = strip_leading_comments(statement)
    match = re.match(r"([A-Za-z_]+)", stripped)
    return match.group(1).casefold() if match else None


def select_statements(text: str) -> list[str]:
    found = []
    for statement in split_sql_statements(text):
        if not statement.strip():
            continue
        if sql_leading_keyword(statement) in {"select", "with"}:
            found.append(statement.strip())
    return found


def normalize_sql(statement: str) -> str:
    return statement.strip().rstrip(";").rstrip()


def _extract_single_statement(
    text: str, location: str
) -> tuple[str | None, str | None]:
    candidates = select_statements(text)
    if len(candidates) == 1:
        normalized = normalize_sql(candidates[0])
        if not normalized:
            return None, "empty SQL statement"
        return normalized, None
    if len(candidates) > 1:
        return None, f"multiple SQL statements found in {location}"
    return None, None


def extract_sql(answer: str) -> tuple[str | None, str | None]:
    """Extract exactly one SELECT/WITH statement from a model answer.

    A fenced block is preferred.  Non-SQL prose around a single SQL statement is
    tolerated; two SELECT/WITH statements are rejected as ambiguous.  The
    fallbacks cover common unfenced forms such as ``SQL: SELECT ...`` and a SQL
    line following a prose sentence.
    """

    if not isinstance(answer, str) or not answer.strip():
        return None, "empty model response"

    for match in FENCE_RE.finditer(answer):
        block = match.group(1).replace("\r\n", "\n").replace("\r", "\n")
        first_line, separator, remainder = block.partition("\n")
        if separator and re.fullmatch(r"[ \t]*[A-Za-z0-9_+.-]*[ \t]*", first_line):
            if re.match(r"\s*(select|with)\b", remainder, flags=re.IGNORECASE):
                block = remainder
        extracted, error = _extract_single_statement(block, "fenced block")
        if extracted is not None or error is not None:
            return extracted, error

    # If the whole response is one statement plus non-SQL prose, this catches it.
    extracted, error = _extract_single_statement(answer, "response")
    if extracted is not None or error is not None:
        return extracted, error

    # Unfenced SQL commonly starts a line after a prose sentence.
    for line_match in re.finditer(
        r"(?im)^[ \t]*(?=(?:select|with)\b)", answer
    ):
        extracted, error = _extract_single_statement(
            answer[line_match.start() :], "response"
        )
        if extracted is not None or error is not None:
            return extracted, error

    # Or follows an explicit label such as "SQL:" on the same line.
    for label_match in re.finditer(
        r"(?i)\b(?:sql(?:ite)?|query)\s*:\s*(?=(?:select|with)\b)", answer
    ):
        extracted, error = _extract_single_statement(
            answer[label_match.end() :], "response"
        )
        if extracted is not None or error is not None:
            return extracted, error

    return None, "no SELECT or WITH statement found"


# ---------------------------------------------------------------------------
# Read-only execution and BIRD EX comparison
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ExecConfig:
    timeout_s: float = 30.0
    progress_every: int = 10_000
    max_vm_steps: int = 100_000_000
    max_rows: int = 200_000


@dataclasses.dataclass
class ExecutionResult:
    rows: list[tuple[Any, ...]]
    error: str | None
    timed_out: bool
    row_limit_exceeded: bool
    vm_limit_exceeded: bool
    elapsed_ms: float

    @property
    def success(self) -> bool:
        return self.error is None


class _QueryAborted(Exception):
    pass


class _RowLimitExceeded(Exception):
    pass


def execute_sql(
    db_path: pathlib.Path,
    sql: str,
    config: ExecConfig,
) -> ExecutionResult:
    started = time.monotonic()
    deadline = started + config.timeout_s
    state: dict[str, Any] = {"abort": None, "steps": 0}
    connection: sqlite3.Connection | None = None
    rows: list[tuple[Any, ...]] = []
    error: str | None = None
    timed_out = False
    row_limit_exceeded = False
    vm_limit_exceeded = False

    def progress_handler() -> int:
        state["steps"] += config.progress_every
        if time.monotonic() >= deadline:
            state["abort"] = "timeout"
            return 1
        if config.max_vm_steps > 0 and state["steps"] > config.max_vm_steps:
            state["abort"] = "vm_limit"
            return 1
        return 0

    try:
        connection = sqlite3.connect(
            sqlite_read_only_uri(db_path),
            uri=True,
            timeout=min(max(config.timeout_s, 0.1), 10.0),
        )
        connection.execute("PRAGMA query_only = ON")
        connection.set_progress_handler(progress_handler, config.progress_every)
        cursor = connection.execute(sql)
        while True:
            if time.monotonic() >= deadline:
                state["abort"] = "timeout"
                raise _QueryAborted
            batch = cursor.fetchmany(10_000)
            if not batch:
                break
            rows.extend(batch)
            if len(rows) > config.max_rows:
                raise _RowLimitExceeded
    except _RowLimitExceeded:
        row_limit_exceeded = True
        error = f"row_limit_exceeded: more than {config.max_rows} rows"
        rows = []
    except _QueryAborted:
        timed_out = True
        error = f"query timed out after {config.timeout_s:g}s"
        rows = []
    except sqlite3.Error as exc:
        if state["abort"] == "timeout":
            timed_out = True
            error = f"query timed out after {config.timeout_s:g}s"
        elif state["abort"] == "vm_limit":
            vm_limit_exceeded = True
            error = f"vm_step_limit_exceeded: more than {config.max_vm_steps} steps"
        else:
            error = f"sqlite error: {clip_text(exc)}"
        rows = []
    except Exception as exc:  # keep one bad query from aborting the pilot
        error = f"execution error: {clip_text(exc)}"
        rows = []
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    return ExecutionResult(
        rows=rows,
        error=error,
        timed_out=timed_out,
        row_limit_exceeded=row_limit_exceeded,
        vm_limit_exceeded=vm_limit_exceeded,
        elapsed_ms=round((time.monotonic() - started) * 1000.0, 3),
    )


def dedupe_preserving_order(rows: Sequence[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    seen: set[tuple[Any, ...]] = set()
    unique: list[tuple[Any, ...]] = []
    for row in rows:
        if row not in seen:
            seen.add(row)
            unique.append(row)
    return unique


def rows_sha256(rows: Sequence[tuple[Any, ...]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(repr(row).encode("utf-8", errors="backslashreplace"))
        digest.update(b"\n")
    return digest.hexdigest()


def compare_row_sets(
    predicted_rows: Sequence[tuple[Any, ...]],
    gold_rows: Sequence[tuple[Any, ...]],
) -> dict[str, Any]:
    """Match official BIRD EX: set(predicted) == set(gold).

    Duplicate multiplicity and row order are measured separately.
    """

    predicted_set = set(predicted_rows)
    gold_set = set(gold_rows)
    set_equal = predicted_set == gold_set
    multiset_equal = collections.Counter(predicted_rows) == collections.Counter(gold_rows)
    order_matches = list(predicted_rows) == list(gold_rows)
    if set_equal:
        unique_order_differs = dedupe_preserving_order(predicted_rows) != dedupe_preserving_order(
            gold_rows
        )
        duplicates_differ = not multiset_equal
    else:
        unique_order_differs = None
        duplicates_differ = None

    return {
        "gold_match": set_equal,
        "set_equal": set_equal,
        "multiset_equal": multiset_equal,
        "order_matches": order_matches,
        "order_differs": unique_order_differs,
        "duplicates_differ": duplicates_differ,
        "pred_row_count": len(predicted_rows),
        "gold_row_count": len(gold_rows),
        "pred_duplicate_rows": len(predicted_rows) - len(predicted_set),
        "gold_duplicate_rows": len(gold_rows) - len(gold_set),
        "pred_result_sha256": rows_sha256(predicted_rows),
        "gold_result_sha256": rows_sha256(gold_rows),
    }


def empty_evaluation(
    *,
    success: bool,
    error: str | None,
) -> dict[str, Any]:
    return {
        "success": success,
        "error": error,
        "gold_match": False,
        "set_equal": None,
        "multiset_equal": None,
        "order_matches": None,
        "order_differs": None,
        "duplicates_differ": None,
        "pred_row_count": None,
        "gold_row_count": None,
        "pred_duplicate_rows": None,
        "gold_duplicate_rows": None,
        "pred_result_sha256": None,
        "gold_result_sha256": None,
        "gold_success": None,
        "gold_error": None,
        "sql_latency_ms": None,
        "gold_latency_ms": None,
    }


def evaluate_sql_pair(
    predicted_sql: str,
    db_path: pathlib.Path,
    config: ExecConfig,
    gold_result: ExecutionResult,
) -> dict[str, Any]:
    predicted_result = execute_sql(db_path, predicted_sql, config)
    fields = empty_evaluation(
        success=predicted_result.success,
        error=predicted_result.error,
    )
    fields["sql_latency_ms"] = predicted_result.elapsed_ms
    fields["gold_success"] = gold_result.success
    fields["gold_error"] = gold_result.error
    fields["gold_latency_ms"] = gold_result.elapsed_ms
    if predicted_result.success and gold_result.success:
        fields.update(compare_row_sets(predicted_result.rows, gold_result.rows))
    return fields


class GoldResultCache:
    def __init__(self, db_root: pathlib.Path, config: ExecConfig):
        self.db_root = db_root
        self.config = config
        self._cache: dict[int, ExecutionResult] = {}

    def get(self, row: DatasetRow) -> ExecutionResult:
        if row.question_id not in self._cache:
            db_path = resolve_db_path(self.db_root, row.db_id)
            self._cache[row.question_id] = execute_sql(db_path, row.sql, self.config)
        return self._cache[row.question_id]


# ---------------------------------------------------------------------------
# Run records and aggregate reporting
# ---------------------------------------------------------------------------


def base_record(
    job: "InferenceJob",
    schema: SchemaContext,
    api_settings: ApiSettings,
    selection: dict[str, Any],
) -> dict[str, Any]:
    prompt_hash_values = prompt_hashes(job.system_prompt, job.user_prompt)
    return {
        "question_id": job.question.question_id,
        "db_id": job.question.db_id,
        "variant": job.variant,
        "model": api_settings.model,
        "temperature": api_settings.temperature,
        "max_tokens": api_settings.max_tokens,
        "api_base_url": api_settings.base_url,
        "auth_header_mode": api_settings.auth_header,
        "dataset_sha256": DATASET_SHA256,
        "selection_fingerprint_sha256": selection["fingerprint_sha256"],
        "schema_sha256": schema.sha256,
        "schema_chars": len(schema.text),
        "schema_table_count": schema.table_count,
        "schema_column_count": schema.column_count,
        "schema_csv_sources": [dataclasses.asdict(source) for source in schema.csv_sources],
        "prompt_chars": len(job.system_prompt) + len(job.user_prompt),
        **prompt_hash_values,
        "latency_ms": None,
        "prompt_tokens": None,
        "output_tokens": None,
        "response_id": None,
        "stop_reason": None,
        "raw_response": None,
        "sql": None,
        "success": False,
        "error": None,
        "error_stage": None,
        "gold_match": False,
        "set_equal": None,
        "multiset_equal": None,
        "order_matches": None,
        "order_differs": None,
        "duplicates_differ": None,
        "pred_row_count": None,
        "gold_row_count": None,
        "pred_duplicate_rows": None,
        "gold_duplicate_rows": None,
        "pred_result_sha256": None,
        "gold_result_sha256": None,
        "gold_success": None,
        "gold_error": None,
        "sql_latency_ms": None,
        "gold_latency_ms": None,
    }


def open_jsonl_output(path: pathlib.Path, overwrite: bool):
    path = path.resolve()
    if path.exists() and not overwrite:
        raise PilotError(f"refusing to overwrite existing output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if overwrite else "x"
    return path.open(mode, encoding="utf-8", newline="\n")


def write_jsonl_record(handle, record: dict[str, Any]) -> None:
    handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
    handle.flush()


def read_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    path = path.resolve()
    if not path.is_file():
        raise PilotError(f"JSONL file not found: {path}")
    records = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PilotError(f"invalid JSON on {path.name} line {line_number}: {exc.msg}") from None
            if not isinstance(record, dict):
                raise PilotError(f"{path.name} line {line_number} is not an object")
            records.append(record)
    return records


def numeric_average(values: Iterable[Any]) -> float | None:
    numbers = [float(value) for value in values if isinstance(value, (int, float))]
    if not numbers:
        return None
    return sum(numbers) / len(numbers)


def numeric_sum(values: Iterable[Any]) -> int | None:
    numbers = [value for value in values if type(value) is int]
    if not numbers:
        return None
    return sum(numbers)


def print_aggregates(records: Sequence[dict[str, Any]]) -> None:
    if not records:
        print("No records.")
        return

    groups: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for record in records:
        variant = record.get("variant")
        if variant not in VARIANTS:
            raise PilotError(f"record has unknown variant: {variant!r}")
        groups[variant].append(record)

    print("Variant aggregates (EX is official set-of-rows equality)")
    header = (
        f"{'variant':<12} {'n':>4} {'sql_ok':>7} {'ex_match':>9} {'ex_rate':>8} "
        f"{'api_err':>8} {'extract_err':>12} {'gold_err':>9} "
        f"{'avg_latency_ms':>15} {'uncached_in':>11} {'output_tok':>10}"
    )
    print(header)
    for variant in sorted(groups):
        variant_records = groups[variant]
        count = len(variant_records)
        sql_ok = sum(1 for record in variant_records if record.get("success") is True)
        ex_match = sum(1 for record in variant_records if record.get("gold_match") is True)
        api_errors = sum(
            1 for record in variant_records if record.get("error_stage") == "api"
        )
        extract_errors = sum(
            1 for record in variant_records if record.get("error_stage") == "extraction"
        )
        gold_errors = sum(
            1 for record in variant_records if record.get("gold_success") is False
        )
        average_latency = numeric_average(
            record.get("latency_ms") for record in variant_records
        )
        prompt_tokens = numeric_sum(
            record.get("prompt_tokens") for record in variant_records
        )
        output_tokens = numeric_sum(
            record.get("output_tokens") for record in variant_records
        )
        ex_rate = ex_match / count if count else 0.0
        latency_display = (
            f"{average_latency:.2f}" if average_latency is not None else "-"
        )
        print(
            f"{variant:<12} {count:>4} {sql_ok:>7} {ex_match:>9} {ex_rate:>8.4f} "
            f"{api_errors:>8} {extract_errors:>12} {gold_errors:>9} "
            f"{latency_display:>15} "
            f"{(prompt_tokens if prompt_tokens is not None else '-'):>10} "
            f"{(output_tokens if output_tokens is not None else '-'):>10}"
        )

    by_question: dict[int, dict[str, dict[str, Any]]] = collections.defaultdict(dict)
    for record in records:
        question_id = record.get("question_id")
        if type(question_id) is not int:
            raise PilotError("record has invalid question_id")
        variant = record["variant"]
        if variant in by_question[question_id]:
            raise PilotError(
                f"duplicate record for question_id={question_id}, variant={variant}"
            )
        by_question[question_id][variant] = record

    variants_present = sorted(groups)
    paired_questions = [
        question_id
        for question_id, variant_records in by_question.items()
        if all(variant in variant_records for variant in variants_present)
    ]
    if len(variants_present) < 2:
        paired_questions = []
    print("")
    print("Paired counts (only questions present for every reported variant)")
    print(f"variant_set       {', '.join(variants_present)}")
    print(f"paired_questions  {len(paired_questions)}")
    if len(variants_present) == 2 and set(variants_present) == {"baseline", "structured"}:
        both = baseline_only = structured_only = neither = 0
        for question_id in paired_questions:
            baseline_match = by_question[question_id]["baseline"].get("gold_match") is True
            structured_match = by_question[question_id]["structured"].get("gold_match") is True
            if baseline_match and structured_match:
                both += 1
            elif baseline_match:
                baseline_only += 1
            elif structured_match:
                structured_only += 1
            else:
                neither += 1
        print(f"both_ex_match     {both}")
        print(f"baseline_only     {baseline_only}")
        print(f"structured_only   {structured_only}")
        print(f"neither_ex_match  {neither}")


# ---------------------------------------------------------------------------
# Inference jobs and commands
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class InferenceJob:
    question: InferenceQuestion
    variant: str
    system_prompt: str
    user_prompt: str
    schema: SchemaContext


def prepare_jobs(
    rows: Sequence[DatasetRow],
    items: Sequence[dict[str, Any]],
    db_root: pathlib.Path,
    variants: Sequence[str],
) -> list[InferenceJob]:
    rows_by_id = {row.question_id: row for row in rows}
    schema_cache: dict[str, SchemaContext] = {}
    jobs: list[InferenceJob] = []
    for item in items:
        row = rows_by_id[item["question_id"]]
        inference_question = infer_question(row)
        if row.db_id not in schema_cache:
            db_path = resolve_db_path(db_root, row.db_id)
            schema_cache[row.db_id] = build_schema_context(row.db_id, db_path)
        schema = schema_cache[row.db_id]
        for variant in variants:
            system_prompt, user_prompt = build_prompt(inference_question, schema, variant)
            jobs.append(
                InferenceJob(
                    question=inference_question,
                    variant=variant,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    schema=schema,
                )
            )
    return jobs


def command_select(args: argparse.Namespace) -> int:
    rows, dataset_sha256 = load_dataset(args.dataset)
    chosen = select_diverse(rows)
    payload = build_selection_payload(rows, chosen, args.dataset, dataset_sha256)
    written = write_json_atomic(args.out, payload, overwrite=args.force)
    if written:
        print(f"Wrote {len(chosen)} question IDs to {display_path(args.out)}")
    else:
        print(f"Selection is already current at {display_path(args.out)}")
    print(f"Dataset SHA256: {dataset_sha256}")
    print(f"Selection fingerprint: {payload['fingerprint_sha256']}")
    return 0


def validate_execution_args(args: argparse.Namespace) -> ExecConfig:
    if args.sql_timeout <= 0:
        raise PilotError("sql-timeout must be positive")
    if args.progress_every <= 0:
        raise PilotError("progress-every must be positive")
    if args.max_vm_steps < 0:
        raise PilotError("max-vm-steps must be non-negative (0 disables the budget)")
    if args.max_rows <= 0:
        raise PilotError("max-rows must be positive")
    return ExecConfig(
        timeout_s=args.sql_timeout,
        progress_every=args.progress_every,
        max_vm_steps=args.max_vm_steps,
        max_rows=args.max_rows,
    )


def command_dry_run(args: argparse.Namespace) -> int:
    rows, _ = load_dataset(args.dataset)
    _, items = load_selection(args.selection, rows)
    variants = parse_variants(args.variants)
    jobs = prepare_jobs(rows, items, args.db_root, variants)
    for job in jobs:
        # Deliberately print only identifiers and context lengths: no question,
        # evidence, prompt text, response text, or gold SQL.
        output = {
            "question_id": job.question.question_id,
            "db_id": job.question.db_id,
            "variant": job.variant,
            "schema_chars": len(job.schema.text),
            "system_chars": len(job.system_prompt),
            "user_chars": len(job.user_prompt),
            "prompt_chars": len(job.system_prompt) + len(job.user_prompt),
        }
        print(json.dumps(output, sort_keys=True, ensure_ascii=False))
    return 0


def command_run(args: argparse.Namespace) -> int:
    rows, _ = load_dataset(args.dataset)
    selection, items = load_selection(args.selection, rows)
    variants = parse_variants(args.variants)
    if args.temperature < 0:
        raise PilotError("temperature must be non-negative")
    if args.max_tokens <= 0:
        raise PilotError("max_tokens must be positive")
    if args.api_timeout <= 0:
        raise PilotError("api-timeout must be positive")
    if args.max_consecutive_api_errors < 0:
        raise PilotError("max_consecutive_api_errors must be non-negative")
    exec_config = validate_execution_args(args)

    token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    if not token:
        raise PilotError("ANTHROPIC_AUTH_TOKEN is not set in the process environment")
    if "\r" in token or "\n" in token:
        raise PilotError("ANTHROPIC_AUTH_TOKEN contains an invalid newline")

    if token in args.base_url:
        raise PilotError("base URL must not contain the API token")
    api_settings = ApiSettings(
        base_url=args.base_url,
        model=args.model,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        api_timeout=args.api_timeout,
        auth_header=args.auth_header,
        allow_http=args.allow_http,
    )
    # Validate URL/configuration before spending any API call.
    messages_url(api_settings.base_url, api_settings.allow_http)
    jobs = prepare_jobs(rows, items, args.db_root, variants)
    rows_by_id = {row.question_id: row for row in rows}
    gold_cache = GoldResultCache(args.db_root, exec_config)

    records: list[dict[str, Any]] = []
    consecutive_api_errors = 0
    stopped_early = False
    output_handle = open_jsonl_output(args.out, overwrite=args.overwrite)
    try:
        for index, job in enumerate(jobs, start=1):
            record = base_record(job, job.schema, api_settings, selection)
            started = time.monotonic()
            try:
                response = call_api(
                    job.system_prompt,
                    job.user_prompt,
                    token,
                    api_settings,
                )
            except ApiError as exc:
                record["latency_ms"] = round((time.monotonic() - started) * 1000.0, 3)
                record["error_stage"] = "api"
                record["error"] = redact_secret(exc, token)
                consecutive_api_errors += 1
                write_jsonl_record(output_handle, record)
                records.append(record)
                print(
                    f"[{index}/{len(jobs)}] qid={job.question.question_id} "
                    f"{job.variant}: API ERROR",
                    file=sys.stderr,
                )
                if (
                    args.max_consecutive_api_errors > 0
                    and consecutive_api_errors >= args.max_consecutive_api_errors
                ):
                    print(
                        "Stopping after consecutive API errors; partial JSONL retained.",
                        file=sys.stderr,
                    )
                    stopped_early = True
                    break
                continue
            record["latency_ms"] = round((time.monotonic() - started) * 1000.0, 3)
            record["prompt_tokens"] = response.prompt_tokens
            record["output_tokens"] = response.output_tokens
            record["response_id"] = response.response_id
            record["stop_reason"] = response.stop_reason
            record["raw_response"] = response.text
            consecutive_api_errors = 0

            sql, extraction_error = extract_sql(response.text)
            if extraction_error is not None:
                record["error_stage"] = "extraction"
                record["error"] = extraction_error
                write_jsonl_record(output_handle, record)
                records.append(record)
                print(
                    f"[{index}/{len(jobs)}] qid={job.question.question_id} "
                    f"{job.variant}: EXTRACTION ERROR",
                    file=sys.stderr,
                )
                continue

            record["sql"] = sql
            row = rows_by_id[job.question.question_id]
            # Gold SQL is touched only now, after prompt construction and the
            # corresponding model response are complete.
            gold_result = gold_cache.get(row)
            db_path = resolve_db_path(args.db_root, row.db_id)
            evaluation = evaluate_sql_pair(
                sql or "",
                db_path,
                exec_config,
                gold_result,
            )
            record.update(evaluation)
            if record["success"] is True:
                record["error_stage"] = None
            elif record["error"]:
                record["error_stage"] = "sql"
            if record["gold_success"] is False:
                record["error_stage"] = "gold" if record["success"] else record["error_stage"]
            write_jsonl_record(output_handle, record)
            records.append(record)
            status = "EX_MATCH" if record["gold_match"] else "NO_MATCH"
            if record["success"] is not True:
                status = "SQL_ERROR"
            elif record["gold_success"] is False:
                status = "GOLD_ERROR"
            print(
                f"[{index}/{len(jobs)}] qid={job.question.question_id} "
                f"{job.variant}: {status}",
                file=sys.stderr,
            )
    finally:
        output_handle.close()

    print("")
    print_aggregates(records)
    if stopped_early:
        print("")
        print("Run stopped early after repeated API errors; output is partial.")
        return 1
    return 0


def command_evaluate(args: argparse.Namespace) -> int:
    rows, _ = load_dataset(args.dataset)
    rows_by_id = {row.question_id: row for row in rows}
    predictions = read_jsonl(args.predictions)
    if not predictions:
        raise PilotError("prediction JSONL is empty")

    seen: set[tuple[int, str]] = set()
    for record in predictions:
        question_id = record.get("question_id")
        variant = record.get("variant")
        if type(question_id) is not int or question_id not in rows_by_id:
            raise PilotError("prediction has unknown question_id")
        if variant not in VARIANTS:
            raise PilotError("prediction has invalid variant")
        key = (question_id, variant)
        if key in seen:
            raise PilotError(f"duplicate prediction for question_id={question_id}, variant={variant}")
        seen.add(key)
        if record.get("db_id") != rows_by_id[question_id].db_id:
            raise PilotError(f"prediction db_id mismatch for question_id={question_id}")

    exec_config = validate_execution_args(args)
    gold_cache = GoldResultCache(args.db_root, exec_config)
    evaluated: list[dict[str, Any]] = []

    output_handle = open_jsonl_output(args.out, overwrite=args.overwrite)
    try:
        for record in predictions:
            row = rows_by_id[record["question_id"]]
            sql = record.get("sql")
            if not isinstance(sql, str) or not sql.strip():
                sql = record.get("SQL") if isinstance(record.get("SQL"), str) else None
            if not isinstance(sql, str) or not sql.strip():
                fields = empty_evaluation(
                    success=False,
                    error=record.get("error") or "missing SQL in prediction",
                )
                record.update(fields)
                if not record.get("error_stage"):
                    record["error_stage"] = "extraction"
            else:
                record["sql"] = sql
                db_path = resolve_db_path(args.db_root, row.db_id)
                gold_result = gold_cache.get(row)
                fields = evaluate_sql_pair(
                    sql,
                    db_path,
                    exec_config,
                    gold_result,
                )
                record.update(fields)
                if record["success"] is True:
                    record["error_stage"] = None
                elif record.get("error"):
                    record["error_stage"] = "sql"
                if record["gold_success"] is False:
                    record["error_stage"] = "gold" if record["success"] else record["error_stage"]
            write_jsonl_record(output_handle, record)
            evaluated.append(record)
    finally:
        output_handle.close()

    print("")
    print_aggregates(evaluated)
    return 0


def command_aggregate(args: argparse.Namespace) -> int:
    records = read_jsonl(args.input)
    print_aggregates(records)
    return 0


# ---------------------------------------------------------------------------
# Self-check: toy evaluator and extractor checks, no network
# ---------------------------------------------------------------------------


def command_self_check(args: argparse.Namespace) -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        checks.append((name, bool(condition), detail))

    with tempfile.TemporaryDirectory(prefix="bird-pilot-selfcheck-") as temporary:
        temporary_path = pathlib.Path(temporary)
        db_dir = temporary_path / "toy"
        db_dir.mkdir()
        db_path = db_dir / "toy.sqlite"
        connection = sqlite3.connect(db_path)
        try:
            connection.executescript(
                """
                CREATE TABLE items (id INTEGER PRIMARY KEY, label TEXT);
                INSERT INTO items VALUES (1, 'a'), (2, 'b'), (3, 'c');
                """
            )
        finally:
            connection.close()

        config = ExecConfig(timeout_s=5.0, progress_every=1000, max_vm_steps=50_000_000, max_rows=1000)
        gold = execute_sql(db_path, "SELECT id, label FROM items ORDER BY id", config)
        reverse = execute_sql(db_path, "SELECT id, label FROM items ORDER BY id DESC", config)
        check("toy gold query executes", gold.success, gold.error or "")
        check("toy reverse query executes", reverse.success, reverse.error or "")

        if gold.success and reverse.success:
            comparison = compare_row_sets(reverse.rows, gold.rows)
            check("EX ignores row order", comparison["gold_match"] is True)
            check("order difference is recorded", comparison["order_differs"] is True)
            check("exact sequence does not match", comparison["order_matches"] is False)

        duplicate = execute_sql(
            db_path,
            "SELECT 1 AS id, 'a' AS label UNION ALL SELECT 1, 'a'",
            config,
        )
        singleton = execute_sql(db_path, "SELECT 1 AS id, 'a' AS label", config)
        if duplicate.success and singleton.success:
            comparison = compare_row_sets(duplicate.rows, singleton.rows)
            check("EX ignores duplicate rows", comparison["gold_match"] is True)
            check("duplicate difference is recorded", comparison["duplicates_differ"] is True)
            check("duplicate row count is recorded", comparison["pred_duplicate_rows"] == 1)

        different = execute_sql(db_path, "SELECT id, label FROM items WHERE id = 1", config)
        if different.success and gold.success:
            comparison = compare_row_sets(different.rows, gold.rows)
            check("different row sets do not match", comparison["gold_match"] is False)

        swapped = execute_sql(db_path, "SELECT label, id FROM items ORDER BY id", config)
        if swapped.success and gold.success:
            comparison = compare_row_sets(swapped.rows, gold.rows)
            check("EX compares tuple column order", comparison["gold_match"] is False)
            check("order is not assessed when sets differ", comparison["order_differs"] is None)

        write_result = execute_sql(db_path, "INSERT INTO items VALUES (99, 'x')", config)
        check("read-only URI rejects writes", not write_result.success, write_result.error or "")
        check(
            "read-only error mentions readonly",
            write_result.error is not None and "readonly" in write_result.error.casefold(),
            write_result.error or "",
        )

        vm_config = ExecConfig(
            timeout_s=5.0,
            progress_every=1000,
            max_vm_steps=10_000,
            max_rows=1000,
        )
        vm_limited = execute_sql(
            db_path,
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 100000) SELECT count(*) FROM c",
            vm_config,
        )
        check("progress/VM limit aborts long query", vm_limited.vm_limit_exceeded, vm_limited.error or "")

        wal_db = db_dir / "wal_toy.sqlite"
        wal_connection = sqlite3.connect(wal_db)
        try:
            wal_connection.execute("PRAGMA journal_mode=WAL")
            wal_connection.execute("CREATE TABLE wal_items (value INTEGER)")
            wal_connection.execute("INSERT INTO wal_items VALUES (1)")
            wal_connection.commit()
        finally:
            wal_connection.close()
        for suffix in ("-wal", "-shm"):
            sidecar = pathlib.Path(str(wal_db) + suffix)
            if sidecar.exists():
                sidecar.unlink()
        wal_result = execute_sql(wal_db, "SELECT value FROM wal_items", config)
        check("immutable read of a WAL-mode database succeeds", wal_result.success, wal_result.error or "")
        check(
            "immutable read creates no WAL/SHM sidecars",
            not any(
                pathlib.Path(str(wal_db) + suffix).exists()
                for suffix in ("-wal", "-shm")
            ),
        )

        inside = extract_sql(
            "Here is the query:\n```sql\nSELECT id FROM items ORDER BY id;\n```\nIt is valid."
        )
        check("fenced SQL extraction", inside[0] == "SELECT id FROM items ORDER BY id", str(inside))
        raw = extract_sql("SELECT 1 AS value;")
        check("unfenced SQL extraction", raw[0] == "SELECT 1 AS value", str(raw))
        after_prose = extract_sql("Here is the query:\nSELECT 1 AS value;")
        check("SQL after prose is extracted", after_prose[0] == "SELECT 1 AS value", str(after_prose))
        after_label = extract_sql("SQL: SELECT 1 AS value;")
        check("SQL after label is extracted", after_label[0] == "SELECT 1 AS value", str(after_label))
        two = extract_sql("```sql\nSELECT 1;\nSELECT 2;\n```")
        check("two statements are rejected", two[0] is None and "multiple" in (two[1] or ""))
        none = extract_sql("I cannot answer that.")
        check("non-SQL response is rejected", none[0] is None)

        schema = build_schema_context("toy", db_path)
        prompt_question = InferenceQuestion(
            question_id=1,
            db_id="toy",
            question="List item labels.",
            evidence="",
        )
        system_prompt, user_prompt = build_prompt(prompt_question, schema, "baseline")
        gold_marker = "SELECT secret_gold_marker FROM forbidden_table"
        check(
            "gold SQL is absent from inference prompts",
            gold_marker not in system_prompt and gold_marker not in user_prompt,
        )

        toy_rows = [
            DatasetRow(
                question_id=index,
                db_id=f"db{index % 3}",
                question=f"q{index}",
                evidence="",
                sql="SELECT 1",
                difficulty=("simple", "moderate", "challenging")[index % 3],
            )
            for index in range(12)
        ]
        first_selection = select_diverse(toy_rows, seed=PILOT_SEED, size=4)
        second_selection = select_diverse(toy_rows, seed=PILOT_SEED, size=4)
        check(
            "selection is deterministic",
            [item.question_id for item in first_selection]
            == [item.question_id for item in second_selection],
        )
        check(
            "selection covers all toy databases",
            {item.db_id for item in first_selection} == {"db0", "db1", "db2"},
        )

    passed = sum(1 for _, condition, _ in checks if condition)
    for name, condition, detail in checks:
        if condition:
            print(f"PASS {name}")
        else:
            print(f"FAIL {name}" + (f": {detail}" if detail else ""))
    print(f"self-check: {passed}/{len(checks)} passed")
    return 0 if passed == len(checks) else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_variants(values: Sequence[str]) -> list[str]:
    parsed: list[str] = []
    for value in values:
        if value not in VARIANTS:
            raise PilotError(f"unknown variant: {value}")
        if value not in parsed:
            parsed.append(value)
    if not parsed:
        raise PilotError("at least one variant is required")
    return parsed


def add_dataset_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dataset",
        type=pathlib.Path,
        default=DEFAULT_DATASET,
        help="fixed 500-row Mini-Dev 2026 SQLite JSONL (SHA256 is enforced)",
    )


def add_db_root_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db-root",
        type=pathlib.Path,
        default=DEFAULT_DB_ROOT,
        help="dev_databases directory",
    )


def add_execution_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sql-timeout", type=float, default=30.0, help="per-query seconds")
    parser.add_argument(
        "--progress-every",
        type=int,
        default=10_000,
        help="SQLite progress-handler interval in VM instructions",
    )
    parser.add_argument(
        "--max-vm-steps",
        type=int,
        default=100_000_000,
        help="approximate VM step budget; 0 disables the step budget",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=200_000,
        help="abort with row_limit_exceeded above this many result rows",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Auditable BIRD Mini-Dev 2026 SQLite engineering pilot (stdlib only)"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    select_parser = subparsers.add_parser(
        "select", help="deterministically select the fixed 20-question pilot"
    )
    add_dataset_argument(select_parser)
    select_parser.add_argument(
        "--out",
        type=pathlib.Path,
        default=DEFAULT_SELECTION,
        help="selection JSON destination",
    )
    select_parser.add_argument("--force", action="store_true", help="overwrite selection JSON")
    select_parser.set_defaults(func=command_select)

    dry_parser = subparsers.add_parser(
        "dry-run", help="validate inputs and print selected IDs and context lengths only"
    )
    add_dataset_argument(dry_parser)
    add_db_root_argument(dry_parser)
    dry_parser.add_argument(
        "--selection", type=pathlib.Path, default=DEFAULT_SELECTION
    )
    dry_parser.add_argument(
        "--variants", nargs="+", choices=VARIANTS, default=list(VARIANTS)
    )
    dry_parser.set_defaults(func=command_dry_run)

    run_parser = subparsers.add_parser(
        "run", help="call the API, execute SQL read-only, evaluate, and write JSONL"
    )
    add_dataset_argument(run_parser)
    add_db_root_argument(run_parser)
    add_execution_arguments(run_parser)
    run_parser.add_argument(
        "--selection", type=pathlib.Path, default=DEFAULT_SELECTION
    )
    run_parser.add_argument("--out", type=pathlib.Path, required=True)
    run_parser.add_argument("--model", default=DEFAULT_MODEL)
    run_parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    run_parser.add_argument(
        "--auth-header",
        choices=("bearer", "x-api-key"),
        default="bearer",
        help="authentication header for the Anthropic-compatible endpoint",
    )
    run_parser.add_argument("--max-tokens", type=int, default=2048)
    run_parser.add_argument("--temperature", type=float, default=0.0)
    run_parser.add_argument(
        "--api-timeout", type=float, default=120.0, help="HTTP request seconds"
    )
    run_parser.add_argument(
        "--max-consecutive-api-errors",
        type=int,
        default=3,
        help="stop early after this many consecutive API errors; 0 disables",
    )
    run_parser.add_argument("--allow-http", action="store_true", help="allow plain HTTP for local tests")
    run_parser.add_argument(
        "--variants", nargs="+", choices=VARIANTS, default=list(VARIANTS)
    )
    run_parser.add_argument("--overwrite", action="store_true")
    run_parser.set_defaults(func=command_run)

    evaluate_parser = subparsers.add_parser(
        "evaluate", help="re-evaluate an existing prediction JSONL without API calls"
    )
    add_dataset_argument(evaluate_parser)
    add_db_root_argument(evaluate_parser)
    add_execution_arguments(evaluate_parser)
    evaluate_parser.add_argument("--predictions", type=pathlib.Path, required=True)
    evaluate_parser.add_argument("--out", type=pathlib.Path, required=True)
    evaluate_parser.add_argument("--overwrite", action="store_true")
    evaluate_parser.set_defaults(func=command_evaluate)

    aggregate_parser = subparsers.add_parser(
        "aggregate", help="print unpaired variant and paired counts from JSONL"
    )
    aggregate_parser.add_argument("--input", type=pathlib.Path, required=True)
    aggregate_parser.set_defaults(func=command_aggregate)

    self_check_parser = subparsers.add_parser(
        "self-check", help="run toy evaluator/extractor checks; no network"
    )
    self_check_parser.set_defaults(func=command_self_check)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except PilotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {clip_text(exc)}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
