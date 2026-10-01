from __future__ import annotations

import json
import re
import sqlite3
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .db import (
    CURRENT_TABLE,
    HISTORY_ROWS_TABLE,
    HISTORY_TABLE,
    INT64_MAX,
    INT64_MIN,
    LEGACY_TABLE,
    MIGRATIONS_TABLE,
    OPS_TABLE,
    PREVIEW_META_TABLE,
    RESTORE_PREVIEW_TABLE,
    TARGET_COLUMNS,
    ServiceError,
    advance_formal_generation,
    db_session,
    get_formal_generation,
    get_revision,
)

LEGACY_COLUMNS = {"legacy_id", "code", "raw_name", "note"}
DECIMAL_INT_RE = re.compile(r"^-?[0-9]+$")
MAX_TEXT_LENGTH = 1_000


class FieldMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(..., pattern="^(copy|trim|decimal_int|constant)$")
    source_column: str | None = None
    value: Any = None

    @field_validator("source_column")
    @classmethod
    def validate_source_column(cls, value: str | None) -> str | None:
        if value is not None and value not in LEGACY_COLUMNS:
            raise ValueError(f"unknown source column: {value}")
        return value


class MappingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: FieldMapping
    code: FieldMapping
    label: FieldMapping

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return self.model_dump()


class LegacyRowPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    legacy_id: int
    code: str
    raw_name: str | None = None
    note: str | None = None


class CommitPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_id: str = Field(..., min_length=32, max_length=64, pattern="^[0-9a-f]+$")
    source_revision: int
    # Optional, kept compatible with old clients; the server-stored value is
    # authoritative.  When supplied it must match the rehearsal's generation.
    formal_generation: int | None = None


class RestorePreviewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: int


class RestoreCommitPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_id: str = Field(..., min_length=32, max_length=64, pattern="^[0-9a-f]+$")
    # Optional mirror of the base generation returned by the rehearsal.
    base_generation: int | None = None


def _mapping_source(mapping: dict[str, Any]) -> str:
    if mapping["type"] == "constant":
        return f"constant:{json.dumps(mapping.get('value'), ensure_ascii=False, separators=(',', ':'))}"
    return f"{mapping['type']}:{mapping.get('source_column')}"


def _require_source(mapping: dict[str, Any], field_name: str) -> str:
    source = mapping.get("source_column")
    if not source:
        raise ServiceError(
            422,
            "mapping_without_source",
            f"Mapping for {field_name} requires source_column",
        )
    return source


def _apply_mapping(value: Any, mapping: dict[str, Any]) -> Any:
    mapping_type = mapping["type"]
    if mapping_type == "copy":
        return value
    if mapping_type == "trim":
        if value is None:
            return None
        if not isinstance(value, str):
            raise ServiceError(422, "trim_requires_text", "trim can only be applied to text")
        return value.strip()
    if mapping_type == "decimal_int":
        if value is None:
            return None
        if not isinstance(value, str) or not DECIMAL_INT_RE.fullmatch(value):
            raise ServiceError(422, "not_decimal_integer", "value is not a decimal integer")
        try:
            parsed = int(value, 10)
        except ValueError as exc:
            raise ServiceError(422, "not_decimal_integer", "value is not a decimal integer") from exc
        if not INT64_MIN <= parsed <= INT64_MAX:
            raise ServiceError(422, "integer_out_of_range", "integer is outside SQLite signed 64-bit range")
        return parsed
    if mapping_type == "constant":
        constant = mapping.get("value")
        if isinstance(constant, str) and len(constant) > MAX_TEXT_LENGTH:
            raise ServiceError(422, "constant_too_long", "constant text is too long")
        return constant
    raise ServiceError(422, "unknown_mapping", f"unsupported mapping type: {mapping_type}")


def _validate_output(field_name: str, value: Any) -> str | None:
    if value is None:
        return "required"
    if field_name == "id":
        if isinstance(value, bool) or not isinstance(value, int):
            return "integer_required"
        if not INT64_MIN <= value <= INT64_MAX:
            return "integer_out_of_range"
    else:
        if not isinstance(value, str):
            return "text_required"
        if value == "":
            return "required"
        if len(value) > MAX_TEXT_LENGTH:
            return "text_too_long"
    return None


def _fault_enabled(conn, name: str) -> bool:
    row = conn.execute("SELECT enabled FROM fault_injection_state WHERE name=?", (name,)).fetchone()
    return bool(row and row[0])


def _create_shadow(conn, shadow_name: str) -> None:
    # The shadow intentionally has no new-table constraints.  Constraints are
    # checked row by row so one failed row does not abort the complete report.
    conn.execute(
        f"""
        CREATE TABLE {shadow_name} (
            shadow_row_id INTEGER PRIMARY KEY,
            legacy_id INTEGER,
            id_value ANY,
            code_value ANY,
            label_value ANY
        )
        """
    )


def _create_candidate(conn, candidate_name: str) -> None:
    conn.execute(
        f"""
        CREATE TABLE {candidate_name} (
            id INTEGER PRIMARY KEY,
            code TEXT NOT NULL UNIQUE,
            label TEXT NOT NULL,
            legacy_id INTEGER NOT NULL UNIQUE
        )
        """
    )


def create_preview(mappings: MappingsPayload, db_path: str | None = None) -> dict[str, Any]:
    mapping_data = mappings.as_dict()
    preview_id = uuid.uuid4().hex
    shadow_name = f"migration_shadow_{preview_id}"

    with db_session(db_path) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            source_revision = get_revision(conn)
            # The rehearsal is bound to the formal table it saw: a migration
            # commit later must not be allowed to overwrite a restored table.
            formal_generation = get_formal_generation(conn)
            _create_shadow(conn, shadow_name)

            source_rows = conn.execute(
                f"SELECT rowid AS row_number, legacy_id, code, raw_name, note FROM {LEGACY_TABLE}"
            ).fetchall()

            transformed: list[dict[str, Any]] = []
            for source in source_rows:
                source_dict = dict(source)
                output: dict[str, Any] = {}
                transform_errors: list[dict[str, str]] = []
                for field_name in TARGET_COLUMNS:
                    mapping = mapping_data[field_name]
                    source_value = None
                    if mapping["type"] != "constant":
                        source_column = _require_source(mapping, field_name)
                        source_value = source_dict[source_column]
                    try:
                        output[field_name] = _apply_mapping(source_value, mapping)
                    except ServiceError as exc:
                        output[field_name] = None
                        transform_errors.append(
                            {
                                "field": field_name,
                                "code": exc.code,
                                "message": exc.message,
                                "mapping": _mapping_source(mapping),
                            }
                        )
                transformed.append(
                    {
                        "row_number": source_dict["row_number"],
                        "legacy_id": source_dict["legacy_id"],
                        "output": output,
                        "transform_errors": transform_errors,
                    }
                )

            conn.executemany(
                f"""
                INSERT INTO {shadow_name}
                    (shadow_row_id, legacy_id, id_value, code_value, label_value)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["row_number"],
                        row["legacy_id"],
                        row["output"].get("id"),
                        row["output"].get("code"),
                        row["output"].get("label"),
                    )
                    for row in transformed
                ],
            )

            if _fault_enabled(conn, "preview_copy"):
                raise RuntimeError("injected failure after shadow copy")

            failures = _validate_shadow_rows(conn, shadow_name, mapping_data, transformed)
            if failures:
                conn.rollback()
                return {
                    "preview_id": None,
                    "source_revision": source_revision,
                    "formal_generation": formal_generation,
                    "ok": False,
                    "row_count": len(source_rows),
                    "mappings": mapping_data,
                    "failures": failures,
                }

            conn.execute(
                f"""
                INSERT INTO {PREVIEW_META_TABLE}
                    (preview_id, source_revision, formal_generation, row_count, mappings_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    preview_id,
                    source_revision,
                    formal_generation,
                    len(source_rows),
                    json.dumps(mapping_data),
                ),
            )
            conn.commit()
            return {
                "preview_id": preview_id,
                "source_revision": source_revision,
                "formal_generation": formal_generation,
                "ok": True,
                "row_count": len(source_rows),
                "mappings": mapping_data,
                "failures": [],
            }
        except Exception:
            conn.rollback()
            raise


def _validate_shadow_rows(
    conn,
    shadow_name: str,
    mapping_data: dict[str, dict[str, Any]],
    transformed: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    seen_values: dict[str, dict[Any, int]] = {"id": {}, "code": {}}
    shadow_rows = conn.execute(
        f"""
        SELECT shadow_row_id, legacy_id, id_value, code_value, label_value
          FROM {shadow_name}
         ORDER BY shadow_row_id
        """
    ).fetchall()

    for shadow, prepared in zip(shadow_rows, transformed):
        errors: list[dict[str, str]] = list(prepared["transform_errors"])
        values = {
            "id": shadow["id_value"],
            "code": shadow["code_value"],
            "label": shadow["label_value"],
        }
        for field_name, value in values.items():
            code = _validate_output(field_name, value)
            if code:
                errors.append(
                    {
                        "field": field_name,
                        "code": code,
                        "message": _validation_message(code),
                        "mapping": _mapping_source(mapping_data[field_name]),
                    }
                )
                continue
            if field_name not in seen_values:
                continue
            first_row = seen_values[field_name].get(value)
            if first_row is not None:
                errors.append(
                    {
                        "field": field_name,
                        "code": "duplicate",
                        "message": f"duplicate value already used by source row {first_row}",
                        "mapping": _mapping_source(mapping_data[field_name]),
                    }
                )
            else:
                seen_values[field_name][value] = shadow["shadow_row_id"]

        legacy_id = shadow["legacy_id"]
        if legacy_id is None or not isinstance(legacy_id, int) or isinstance(legacy_id, bool):
            errors.append(
                {
                    "field": "legacy_id",
                    "code": "integer_required",
                    "message": "source legacy_id must be an integer",
                    "mapping": "copy:legacy_id",
                }
            )

        if errors:
            failures.append(
                {
                    "row_number": shadow["shadow_row_id"],
                    "legacy_id": shadow["legacy_id"],
                    "values": values,
                    "errors": errors,
                }
            )
    return failures


def _discard_failed_migration_preview(
    conn: sqlite3.Connection, preview_id: str, pending_name: str
) -> None:
    """Remove artifacts from a failed commit without touching the formal table."""
    conn.execute(f"DROP TABLE IF EXISTS {pending_name}")
    conn.execute(f"DROP TABLE IF EXISTS migration_shadow_{preview_id}")
    conn.execute(f"DELETE FROM {PREVIEW_META_TABLE} WHERE preview_id=?", (preview_id,))


def _discard_failed_restore_preview(
    conn: sqlite3.Connection, preview_id: str, candidate_name: str
) -> None:
    """A rejected or failed restore confirmation is never replayable."""
    conn.execute(f"DROP TABLE IF EXISTS {candidate_name}")
    conn.execute(f"DELETE FROM {RESTORE_PREVIEW_TABLE} WHERE preview_id=?", (preview_id,))


def _validation_message(code: str) -> str:
    return {
        "required": "value is required",
        "integer_required": "value must be an integer",
        "integer_out_of_range": "integer is outside signed 64-bit range",
        "text_required": "value must be text",
        "text_too_long": "text is too long",
    }.get(code, code)


def _archive_current_formal(
    conn: sqlite3.Connection,
    *,
    op_id: int,
    kind: str,
    migration_id: int | None,
    source_version_id: int | None,
) -> dict[str, int]:
    """Seal the current formal table into a new locked history version.

    Runs inside the caller's switch transaction.  Returns the new version id
    and the number of rows preserved.
    """
    cur = conn.execute(
        f"""
        INSERT INTO {HISTORY_TABLE}
            (version_id, op_id, kind, migration_id, source_version_id,
             replaced_table, source_revision, row_count, locked)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            op_id,
            op_id,
            kind,
            migration_id,
            source_version_id,
            CURRENT_TABLE,
            get_revision(conn),
            conn.execute(f"SELECT COUNT(*) FROM {CURRENT_TABLE}").fetchone()[0],
        ),
    )
    version_id = int(cur.lastrowid)
    conn.execute(
        f"""
        INSERT INTO {HISTORY_ROWS_TABLE}
            (version_id, id, code, label, legacy_id)
        SELECT ?, id, code, label, legacy_id
          FROM {CURRENT_TABLE}
        """,
        (version_id,),
    )
    copied_count = conn.execute(
        f"SELECT COUNT(*) FROM {HISTORY_ROWS_TABLE} WHERE version_id=?",
        (version_id,),
    ).fetchone()[0]
    sealed_count = conn.execute(
        f"SELECT row_count FROM {HISTORY_TABLE} WHERE version_id=?",
        (version_id,),
    ).fetchone()[0]
    if copied_count != sealed_count:
        raise RuntimeError("copy mismatch while preserving the formal table")
    # Locking last turns the new version into read-only history; the readonly
    # triggers then reject every later insert/update/delete on its rows.
    conn.execute(f"UPDATE {HISTORY_TABLE} SET locked=1 WHERE version_id=?", (version_id,))
    return {"version_id": version_id, "row_count": int(sealed_count)}


def commit_preview(payload: CommitPayload, db_path: str | None = None) -> dict[str, Any]:
    with db_session(db_path) as conn:
        pending_name = f"records_pending_{payload.preview_id}"
        shadow_name = f"migration_shadow_{payload.preview_id}"
        try:
            conn.execute("BEGIN IMMEDIATE")
            current_revision = get_revision(conn)
            current_generation = get_formal_generation(conn)
            if current_revision != payload.source_revision:
                raise ServiceError(
                    409,
                    "stale_preview",
                    "source table changed after preview; recalculate and preview again",
                )

            preview = conn.execute(
                f"""
                SELECT preview_id, source_revision, formal_generation, row_count, mappings_json
                  FROM {PREVIEW_META_TABLE}
                 WHERE preview_id=?
                """,
                (payload.preview_id,),
            ).fetchone()
            if preview is None:
                raise ServiceError(404, "preview_not_found", "preview was not accepted or has expired")
            if preview["source_revision"] != payload.source_revision:
                raise ServiceError(
                    409,
                    "stale_preview",
                    "commit revision does not match the revision used by this preview",
                )
            # The rehearsal is only valid against the exact formal generation
            # it was built on.  A restore in between advances the generation,
            # so an old migration commit can never overwrite restored content.
            if preview["formal_generation"] != current_generation:
                raise ServiceError(
                    409,
                    "stale_generation",
                    "formal table generation changed after preview; recalculate and preview again",
                )
            if payload.formal_generation is not None and payload.formal_generation != current_generation:
                raise ServiceError(
                    409,
                    "stale_generation",
                    "submitted formal generation does not match the current formal table",
                )

            shadow_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (shadow_name,),
            ).fetchone()
            if not shadow_exists:
                raise ServiceError(409, "shadow_missing", "shadow table is missing; preview again")

            shadow_count = conn.execute(f"SELECT COUNT(*) FROM {shadow_name}").fetchone()[0]
            if shadow_count != preview["row_count"]:
                raise ServiceError(409, "shadow_changed", "shadow table changed; preview again")

            migration_cur = conn.execute(
                f"""
                INSERT INTO {MIGRATIONS_TABLE}
                    (preview_id, source_revision, committed_revision, formal_generation, row_count)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    payload.preview_id,
                    preview["source_revision"],
                    current_revision,
                    current_generation,
                    preview["row_count"],
                ),
            )
            migration_id = int(migration_cur.lastrowid)
            op_cur = conn.execute(
                f"""
                INSERT INTO {OPS_TABLE}
                    (kind, migration_id, source_version_id, sealed_version_id,
                     base_generation, new_generation)
                VALUES ('migration', ?, NULL, NULL, ?, NULL)
                """,
                (migration_id, current_generation),
            )
            op_id = int(op_cur.lastrowid)

            sealed = _archive_current_formal(
                conn,
                op_id=op_id,
                kind="migration",
                migration_id=migration_id,
                source_version_id=None,
            )
            version_id = sealed["version_id"]
            old_count = sealed["row_count"]

            conn.execute(f"DROP TABLE IF EXISTS {pending_name}")
            conn.execute(
                f"""
                CREATE TABLE {pending_name} (
                    id INTEGER PRIMARY KEY,
                    code TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL,
                    legacy_id INTEGER NOT NULL UNIQUE
                )
                """
            )
            conn.execute(
                f"""
                INSERT INTO {pending_name}(id, code, label, legacy_id)
                SELECT id_value, code_value, label_value, legacy_id
                  FROM {shadow_name}
                """
            )
            pending_count = conn.execute(f"SELECT COUNT(*) FROM {pending_name}").fetchone()[0]
            if pending_count != preview["row_count"]:
                raise RuntimeError("pending row count does not match accepted preview")

            if _fault_enabled(conn, "commit_switch"):
                raise RuntimeError("injected failure before table switch")

            new_generation = advance_formal_generation(conn, current_generation)
            conn.execute(
                f"UPDATE {OPS_TABLE} SET sealed_version_id=?, new_generation=? WHERE op_id=?",
                (version_id, new_generation, op_id),
            )

            # DDL is transactional in SQLite.  This drop/rename pair therefore
            # either replaces the formal table completely or rolls back to the
            # exact previous table.
            conn.execute(f"DROP TABLE {CURRENT_TABLE}")
            conn.execute(f"ALTER TABLE {pending_name} RENAME TO {CURRENT_TABLE}")
            conn.execute(f"DROP TABLE {shadow_name}")
            conn.execute(f"DELETE FROM {PREVIEW_META_TABLE} WHERE preview_id=?", (payload.preview_id,))
            conn.commit()

            return {
                "ok": True,
                "migration_id": migration_id,
                "preview_id": payload.preview_id,
                "source_revision": preview["source_revision"],
                "committed_revision": current_revision,
                "base_generation": current_generation,
                "formal_generation": new_generation,
                "row_count": pending_count,
                "old_version": {
                    "version_id": version_id,
                    "row_count": old_count,
                },
            }
        except ServiceError:
            conn.rollback()
            # A rejected commit is never replayable as-is; remove its preview
            # and shadow so the page can force the client to recalculate.
            _discard_failed_migration_preview(conn, payload.preview_id, pending_name)
            raise
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            _discard_failed_migration_preview(conn, payload.preview_id, pending_name)
            raise ServiceError(422, "constraint_failed", f"new-table constraint failed: {exc}") from exc
        except Exception:
            conn.rollback()
            _discard_failed_migration_preview(conn, payload.preview_id, pending_name)
            raise


def _row_signature(row: sqlite3.Row | dict[str, Any]) -> tuple[Any, ...]:
    return (row["id"], row["code"], row["label"], row["legacy_id"])


def _compute_row_diff(conn, current_name: str, candidate_name: str) -> dict[str, Any]:
    """Compare formal vs candidate by full row signature."""
    current_rows = [
        dict(row) for row in conn.execute(
            f"SELECT id, code, label, legacy_id FROM {current_name} ORDER BY id"
        )
    ]
    candidate_rows = [
        dict(row) for row in conn.execute(
            f"SELECT id, code, label, legacy_id FROM {candidate_name} ORDER BY id"
        )
    ]
    current_by_id = {row["id"]: row for row in current_rows}
    candidate_by_id = {row["id"]: row for row in candidate_rows}

    added = [row for row_id, row in candidate_by_id.items() if row_id not in current_by_id]
    removed = [row for row_id, row in current_by_id.items() if row_id not in candidate_by_id]
    changed = []
    for row_id, new_row in candidate_by_id.items():
        old_row = current_by_id.get(row_id)
        if old_row is not None and _row_signature(old_row) != _row_signature(new_row):
            changed.append({"before": old_row, "after": new_row})
    unchanged = [
        row for row_id, row in current_by_id.items()
        if row_id in candidate_by_id and _row_signature(row) == _row_signature(candidate_by_id[row_id])
    ]
    return {
        "current_row_count": len(current_rows),
        "candidate_row_count": len(candidate_rows),
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged_count": len(unchanged),
    }


def _get_locked_version(conn, version_id: int) -> sqlite3.Row:
    version = conn.execute(
        f"SELECT * FROM {HISTORY_TABLE} WHERE version_id=?",
        (version_id,),
    ).fetchone()
    if version is None:
        raise ServiceError(404, "version_not_found", "history version does not exist")
    if not version["locked"]:
        raise ServiceError(409, "version_not_locked", "history version is not sealed; cannot restore")
    return version


def create_restore_preview(
    payload: RestorePreviewPayload, db_path: str | None = None
) -> dict[str, Any]:
    """Build a constrained candidate table from a read-only history version.

    No record of the current formal table is modified.  The candidate and the
    rehearsal metadata live in their own transaction and are consumed (or
    discarded) exactly once by the confirmation.
    """
    preview_id = uuid.uuid4().hex
    candidate_name = f"restore_pending_{preview_id}"

    with db_session(db_path) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            version = _get_locked_version(conn, payload.version_id)
            base_generation = get_formal_generation(conn)

            source_count = conn.execute(
                f"SELECT COUNT(*) FROM {HISTORY_ROWS_TABLE} WHERE version_id=?",
                (payload.version_id,),
            ).fetchone()[0]
            if source_count != version["row_count"]:
                raise RuntimeError("sealed history row count does not match its metadata")

            conn.execute(f"DROP TABLE IF EXISTS {candidate_name}")
            _create_candidate(conn, candidate_name)
            try:
                conn.execute(
                    f"""
                    INSERT INTO {candidate_name}(id, code, label, legacy_id)
                    SELECT id, code, label, legacy_id
                      FROM {HISTORY_ROWS_TABLE}
                     WHERE version_id=?
                    """,
                    (payload.version_id,),
                )
            except sqlite3.IntegrityError as exc:
                # A read-only history version cannot violate constraints: it
                # was itself a formal table.  Report rather than corrupt it.
                conn.rollback()
                return {
                    "ok": False,
                    "preview_id": None,
                    "version_id": payload.version_id,
                    "base_generation": base_generation,
                    "error": {"code": "constraint_failed", "message": str(exc)},
                }

            candidate_count = conn.execute(
                f"SELECT COUNT(*) FROM {candidate_name}"
            ).fetchone()[0]
            if candidate_count != source_count:
                raise RuntimeError("candidate row count does not match the source version")

            if _fault_enabled(conn, "restore_copy"):
                raise RuntimeError("injected failure after restore candidate copy")

            diff = _compute_row_diff(conn, CURRENT_TABLE, candidate_name)

            conn.execute(
                f"""
                INSERT INTO {RESTORE_PREVIEW_TABLE}
                    (preview_id, source_version_id, base_generation,
                     source_row_count, candidate_row_count, diff_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    preview_id,
                    payload.version_id,
                    base_generation,
                    source_count,
                    candidate_count,
                    json.dumps(diff),
                ),
            )
            conn.commit()

            return {
                "ok": True,
                "preview_id": preview_id,
                "version_id": payload.version_id,
                "source_kind": version["kind"],
                "base_generation": base_generation,
                "current_generation": base_generation,
                "source_row_count": source_count,
                "candidate_row_count": candidate_count,
                "diff": diff,
            }
        except Exception:
            conn.rollback()
            raise


def commit_restore(payload: RestoreCommitPayload, db_path: str | None = None) -> dict[str, Any]:
    with db_session(db_path) as conn:
        candidate_name = f"restore_pending_{payload.preview_id}"
        try:
            conn.execute("BEGIN IMMEDIATE")
            current_generation = get_formal_generation(conn)

            preview = conn.execute(
                f"""
                SELECT preview_id, source_version_id, base_generation,
                       source_row_count, candidate_row_count, diff_json
                  FROM {RESTORE_PREVIEW_TABLE}
                 WHERE preview_id=?
                """,
                (payload.preview_id,),
            ).fetchone()
            if preview is None:
                raise ServiceError(
                    404,
                    "restore_preview_not_found",
                    "restore rehearsal was not accepted or has already been consumed",
                )
            # Adjudicate against the exact formal-table generation seen by the
            # rehearsal.  A competing restore or migration wins only once; the
            # loser rolls back entirely and must rehearse again.
            if preview["base_generation"] != current_generation:
                raise ServiceError(
                    409,
                    "stale_generation",
                    "formal table generation changed since the restore rehearsal; preview again",
                )
            if payload.base_generation is not None and payload.base_generation != current_generation:
                raise ServiceError(
                    409,
                    "stale_generation",
                    "submitted base generation does not match the current formal table",
                )

            source_version = _get_locked_version(conn, preview["source_version_id"])

            candidate_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (candidate_name,),
            ).fetchone()
            if not candidate_exists:
                raise ServiceError(
                    409, "candidate_missing", "restore candidate table is missing; preview again"
                )
            candidate_count = conn.execute(
                f"SELECT COUNT(*) FROM {candidate_name}"
            ).fetchone()[0]
            if candidate_count != preview["candidate_row_count"]:
                raise ServiceError(
                    409, "candidate_changed", "candidate table changed; preview again"
                )
            # Re-verify the candidate still matches the read-only source rows
            # in full before it can become the formal table.
            source_count = conn.execute(
                f"SELECT COUNT(*) FROM {HISTORY_ROWS_TABLE} WHERE version_id=?",
                (preview["source_version_id"],),
            ).fetchone()[0]
            if source_count != preview["source_row_count"]:
                raise RuntimeError("sealed source version changed after restore rehearsal")

            op_cur = conn.execute(
                f"""
                INSERT INTO {OPS_TABLE}
                    (kind, migration_id, source_version_id, sealed_version_id,
                     base_generation, new_generation)
                VALUES ('restore', NULL, ?, NULL, ?, NULL)
                """,
                (preview["source_version_id"], current_generation),
            )
            op_id = int(op_cur.lastrowid)

            sealed = _archive_current_formal(
                conn,
                op_id=op_id,
                kind="restore",
                migration_id=None,
                source_version_id=preview["source_version_id"],
            )
            version_id = sealed["version_id"]
            old_count = sealed["row_count"]

            if _fault_enabled(conn, "restore_switch"):
                raise RuntimeError("injected failure before restore table switch")

            new_generation = advance_formal_generation(conn, current_generation)
            conn.execute(
                f"UPDATE {OPS_TABLE} SET sealed_version_id=?, new_generation=? WHERE op_id=?",
                (version_id, new_generation, op_id),
            )

            conn.execute(f"DROP TABLE {CURRENT_TABLE}")
            conn.execute(f"ALTER TABLE {candidate_name} RENAME TO {CURRENT_TABLE}")
            # Consume the rehearsal exactly once: a repeat commit must fail.
            conn.execute(
                f"DELETE FROM {RESTORE_PREVIEW_TABLE} WHERE preview_id=?",
                (payload.preview_id,),
            )
            conn.commit()

            return {
                "ok": True,
                "preview_id": payload.preview_id,
                "source_version_id": preview["source_version_id"],
                "source_kind": source_version["kind"],
                "sealed_version_id": version_id,
                "sealed_row_count": old_count,
                "base_generation": current_generation,
                "formal_generation": new_generation,
                "row_count": candidate_count,
            }
        except ServiceError:
            conn.rollback()
            _discard_failed_restore_preview(conn, payload.preview_id, candidate_name)
            raise
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            _discard_failed_restore_preview(conn, payload.preview_id, candidate_name)
            raise ServiceError(
                422, "constraint_failed", f"restore candidate constraint failed: {exc}"
            ) from exc
        except Exception:
            conn.rollback()
            _discard_failed_restore_preview(conn, payload.preview_id, candidate_name)
            raise
