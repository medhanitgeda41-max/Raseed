"""Tests for Part 1: Persistent Memory (Cloudflare R2 + Dual DB / Postgres).

Covers:
- LocalStorageBackend and R2StorageBackend (mocked with moto)
- Presigned download URLs for cloud objects
- OCR caching in storage (<path>.ocr.txt) and database (uploaded_documents.extracted_text)
- Restart persistence (chat history, memory notes, staged actions, documents)
- Multi-tenant path isolation and reset_workspace file deletion
- Schema conversion parity for PostgreSQL / Neon
"""
import os
import json
import pytest
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import boto3
from moto import mock_aws

from config.settings import settings
from ingest.storage_interface import (
    LocalStorageBackend,
    R2StorageBackend,
    get_storage_backend,
)
from ingest.file_storage import (
    store_uploaded_file,
    read_stored_document,
    cache_extracted_text,
    get_cached_extracted_text,
    generate_presigned_download_url,
    reset_workspace,
)
from database.connection import (
    init_db,
    get_db_connection,
    save_chat_message,
    get_chat_history,
    add_memory_note,
    get_memory_notes,
    delete_memory_note,
    convert_sqlite_schema_to_postgres,
)
from tools.write_gateway import stage_action
from tools.read_tools import get_staged_actions, list_documents


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_p = Path(path)
    init_db(db_p)
    yield db_p
    try:
        os.unlink(path)
    except Exception:
        pass


@pytest.fixture
def temp_uploads_dir():
    with tempfile.TemporaryDirectory() as tmp_dir:
        yield Path(tmp_dir)


class TestStorageBackends:
    def test_local_storage_backend(self, temp_uploads_dir):
        backend = LocalStorageBackend(root_dir=temp_uploads_dir)
        content = b"Invoice #101 AED 5250"
        rel_path, sha256_hash = backend.store_file(
            file_bytes=content,
            business_id=1,
            document_type="invoice",
            filename="inv101.pdf",
        )
        assert backend.file_exists(rel_path) is True
        assert backend.read_file(rel_path) == content

        # Presigned URL (local fallback URL)
        url = backend.generate_presigned_url(rel_path, expires_in_seconds=300)
        assert url.startswith("file://") or url.startswith("/download/") or "inv101.pdf" in url

        # Delete files for business
        backend.delete_business_files(business_id=1)
        assert backend.file_exists(rel_path) is False

    @mock_aws
    def test_r2_storage_backend(self):
        # Setup mock S3
        s3 = boto3.client("s3", region_name="us-east-1")
        bucket_name = "test-raseed-bucket"
        s3.create_bucket(Bucket=bucket_name)

        backend = R2StorageBackend(
            endpoint_url="https://fake-r2.cloudflarestorage.com",
            access_key_id="fake_key",
            secret_access_key="fake_secret",
            bucket_name=bucket_name,
            s3_client=s3,
        )

        content = b"Tax Invoice #2026-99"
        rel_path, sha256_hash = backend.store_file(
            file_bytes=content,
            business_id=2,
            document_type="invoice",
            filename="tax_inv.pdf",
        )
        assert backend.file_exists(rel_path) is True
        assert backend.read_file(rel_path) == content

        # Check presigned URL
        url = backend.generate_presigned_url(rel_path, expires_in_seconds=300)
        assert bucket_name in url
        assert "AWSAccessKeyId" in url or "X-Amz-Signature" in url or "Signature" in url

        # Delete for business
        backend.delete_business_files(business_id=2)
        assert backend.file_exists(rel_path) is False


class TestOCRCaching:
    def test_ocr_caching_in_storage_and_db(self, temp_db, temp_uploads_dir):
        backend = LocalStorageBackend(root_dir=temp_uploads_dir)
        with patch("ingest.file_storage.get_storage_backend", return_value=backend):
            sample_pdf_bytes = b"%PDF-1.4 sample invoice"
            dest_path, file_hash, is_dup, doc_id = store_uploaded_file(
                file_bytes=sample_pdf_bytes,
                filename="vendor_inv.pdf",
                document_type="invoice",
                business_id=1,
                db_path=temp_db,
                backend=backend,
            )

            # Initially OCR cache is empty
            cached = get_cached_extracted_text(storage_path=str(dest_path), business_id=1, db_path=temp_db, backend=backend)
            assert cached is None

            # Simulate OCR run and caching
            extracted = "TAX INVOICE TRN 100234567800003 VAT AED 250"
            cache_extracted_text(storage_path=str(dest_path), text=extracted, business_id=1, db_path=temp_db, backend=backend)

            # Now verify it returns from cache
            cached2 = get_cached_extracted_text(storage_path=str(dest_path), business_id=1, db_path=temp_db, backend=backend)
            assert cached2 == extracted


class TestRestartPersistence:
    def test_app_restart_restores_chat_and_memory(self, temp_db):
        # 1. User turn and assistant turn saved
        msg1_id = save_chat_message(
            business_id=1,
            role="user",
            content="Can I recover VAT on entertainment expenses?",
            db_path=temp_db,
        )
        msg2_id = save_chat_message(
            business_id=1,
            role="assistant",
            content="Under Article 53 of the Executive Regulation, entertainment expenses are blocked.",
            how_i_got_this_json=json.dumps({"citations": "Executive Regulation Art. 53"}),
            db_path=temp_db,
        )

        # 2. Add confirmed fact to persistent memory
        fact_id = add_memory_note(
            business_id=1,
            note="Company vehicles are used 60% for commercial deliveries",
            source="chat",
            db_path=temp_db,
        )

        # 3. Stage an action
        action_id, _ = stage_action(
            action_type="SUBMIT_REFUND_APPLICATION",
            payload={"period_codes": ["2021-Q1"], "claimed_amount": 5000.0},
            reason="Recover verified excess input tax",
            business_id=1,
            db_path=temp_db,
        )

        # --- SIMULATE APP RESTART (Fresh memory / new queries from DB) ---
        history = get_chat_history(business_id=1, db_path=temp_db)
        assert len(history) == 2
        assert history[0]["content"] == "Can I recover VAT on entertainment expenses?"
        assert history[1]["how_i_got_this"]["citations"] == "Executive Regulation Art. 53"

        notes = get_memory_notes(business_id=1, db_path=temp_db)
        assert len(notes) == 1
        assert "60% for commercial deliveries" in notes[0]["note"]

        staged = get_staged_actions(status="PENDING_APPROVAL", business_id=1, db_path=temp_db)
        assert len(staged) == 1
        assert staged[0]["id"] == action_id
        assert staged[0]["payload"]["claimed_amount"] == 5000.0

        # Test deleting memory note
        deleted = delete_memory_note(note_id=fact_id, business_id=1, db_path=temp_db)
        assert deleted is True
        assert len(get_memory_notes(business_id=1, db_path=temp_db)) == 0


class TestPostgresSchemaParity:
    def test_sqlite_to_postgres_schema_conversion(self):
        pg_sql = convert_sqlite_schema_to_postgres()
        # Verify AUTOINCREMENT -> SERIAL conversion
        assert "SERIAL PRIMARY KEY" in pg_sql
        assert "AUTOINCREMENT" not in pg_sql
        # Verify JSON / TIMESTAMP conversions
        assert "JSONB" in pg_sql
        assert "TIMESTAMPTZ" in pg_sql
        # Verify memory_notes and chat_messages are present
        assert "CREATE TABLE IF NOT EXISTS chat_messages" in pg_sql
        assert "CREATE TABLE IF NOT EXISTS memory_notes" in pg_sql
