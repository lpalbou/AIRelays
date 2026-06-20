from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class AppStore:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._uploads_dir = data_dir / "uploads"
        self._db_path = data_dir / "state.sqlite3"
        self._lock = threading.Lock()
        self._uploads_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    storage_path TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    metadata_json TEXT NOT NULL,
                    seed_items_json TEXT,
                    latest_response_id TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )
            self._conn.commit()

    def create_file(
        self,
        filename: str,
        purpose: str,
        content_type: str,
        data: bytes,
        sha256: str,
    ) -> dict[str, Any]:
        storage_path = self._uploads_dir / f"file_{uuid.uuid4().hex}"
        storage_path.write_bytes(data)
        return self.create_file_from_path(
            filename=filename,
            purpose=purpose,
            content_type=content_type,
            storage_path=storage_path,
            size_bytes=len(data),
            sha256=sha256,
        )

    def create_file_from_path(
        self,
        filename: str,
        purpose: str,
        content_type: str,
        storage_path: Path,
        size_bytes: int,
        sha256: str,
    ) -> dict[str, Any]:
        created_at = int(time.time())
        file_id = f"file_{uuid.uuid4().hex}"
        final_path = self._uploads_dir / file_id
        if storage_path != final_path:
            os.replace(storage_path, final_path)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO files (id, filename, purpose, content_type, bytes, sha256, created_at, storage_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    filename,
                    purpose,
                    content_type,
                    size_bytes,
                    sha256,
                    created_at,
                    str(final_path),
                ),
            )
            self._conn.commit()
        return self.get_file(file_id)

    def file_usage(self) -> dict[str, int]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS file_count, COALESCE(SUM(bytes), 0) AS total_bytes
                FROM files
                """
            ).fetchone()
        return {
            "file_count": int(row["file_count"]),
            "total_bytes": int(row["total_bytes"]),
        }

    def conversation_usage(self) -> dict[str, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS conversation_count FROM conversations"
            ).fetchone()
        return {"conversation_count": int(row["conversation_count"])}

    def list_files(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM files ORDER BY created_at DESC").fetchall()
        return [self._row_to_file(row) for row in rows]

    def get_file(self, file_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        if row is None:
            raise KeyError(file_id)
        return self._row_to_file(row)

    def get_file_bytes(self, file_id: str) -> tuple[dict[str, Any], bytes]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        if row is None:
            raise KeyError(file_id)
        file_record = self._row_to_file(row)
        data = Path(row["storage_path"]).read_bytes()
        return file_record, data

    def delete_file(self, file_id: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT storage_path FROM files WHERE id = ?", (file_id,)).fetchone()
            if row is None:
                return False
            self._conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
            self._conn.commit()
        Path(row["storage_path"]).unlink(missing_ok=True)
        return True

    def create_conversation(
        self,
        metadata: dict[str, Any] | None = None,
        seed_items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        created_at = int(time.time())
        conversation_id = f"conv_{uuid.uuid4().hex}"
        payload = json.dumps(metadata or {}, ensure_ascii=True)
        seed_payload = json.dumps(seed_items or [], ensure_ascii=True)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO conversations (id, metadata_json, seed_items_json, latest_response_id, created_at, updated_at)
                VALUES (?, ?, ?, NULL, ?, ?)
                """,
                (conversation_id, payload, seed_payload, created_at, created_at),
            )
            self._conn.commit()
        return self.get_conversation(conversation_id)

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        if row is None:
            raise KeyError(conversation_id)
        return self._row_to_conversation(row)

    def update_conversation(
        self, conversation_id: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        current = self.get_conversation(conversation_id)
        merged = current["metadata"].copy()
        if metadata:
            merged.update(metadata)
        updated_at = int(time.time())
        with self._lock:
            self._conn.execute(
                "UPDATE conversations SET metadata_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(merged, ensure_ascii=True), updated_at, conversation_id),
            )
            self._conn.commit()
        return self.get_conversation(conversation_id)

    def touch_conversation(self, conversation_id: str, latest_response_id: str | None) -> None:
        updated_at = int(time.time())
        with self._lock:
            self._conn.execute(
                """
                UPDATE conversations
                SET latest_response_id = COALESCE(?, latest_response_id), updated_at = ?
                WHERE id = ?
                """,
                (latest_response_id, updated_at, conversation_id),
            )
            self._conn.commit()

    def delete_conversation(self, conversation_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM conversations WHERE id = ?", (conversation_id,)
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def _row_to_file(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "object": "file",
            "bytes": row["bytes"],
            "created_at": row["created_at"],
            "filename": row["filename"],
            "purpose": row["purpose"],
            "status": "processed",
            "content_type": row["content_type"],
            "sha256": row["sha256"],
        }

    def _row_to_conversation(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "object": "conversation",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "latest_response_id": row["latest_response_id"],
            "metadata": json.loads(row["metadata_json"]),
            "seed_items": json.loads(row["seed_items_json"] or "[]"),
        }
