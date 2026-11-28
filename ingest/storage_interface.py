"""Pluggable file storage interface for Raseed.

Supports two storage backends controlled by STORAGE_BACKEND:
- "local": Local filesystem under data/uploads/<business_id>/<doc_type>/ (for development)
- "r2": Cloudflare R2 (S3-compatible API via boto3) with private bucket and presigned URLs.
"""
from abc import ABC, abstractmethod
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Optional, Tuple, Dict, Any

from config.settings import settings


def sanitize_filename(filename: str) -> str:
    """Sanitize filename to prevent path traversal or invalid characters."""
    base = Path(filename).name
    cleaned = re.sub(r"[^\w\.\-]", "_", base)
    return cleaned or "document"


def compute_sha256(data: bytes) -> str:
    """Compute hex SHA-256 hash of bytes."""
    return hashlib.sha256(data).hexdigest()


class BaseStorageBackend(ABC):
    """Abstract file storage backend interface."""

    @abstractmethod
    def store_file(
        self,
        file_bytes: bytes,
        business_id: int,
        document_type: str,
        filename: str,
        mime_type: str = "application/octet-stream",
    ) -> Tuple[str, str]:
        """Store bytes and return (storage_path, file_hash)."""
        pass

    @abstractmethod
    def read_file(self, storage_path: str) -> bytes:
        """Read and return raw file bytes."""
        pass

    @abstractmethod
    def store_extracted_text(self, storage_path: str, text: str) -> None:
        """Cache extracted OCR/text next to the document to prevent re-processing."""
        pass

    @abstractmethod
    def get_extracted_text(self, storage_path: str) -> Optional[str]:
        """Retrieve cached extracted text if available."""
        pass

    @abstractmethod
    def generate_presigned_url(self, storage_path: str, expires_in_seconds: int = 300) -> str:
        """Generate a short-lived presigned download URL."""
        pass

    @abstractmethod
    def delete_business_files(self, business_id: int) -> int:
        """Delete all files belonging to a business (Reset workspace). Returns count."""
        pass

    @abstractmethod
    def file_exists(self, storage_path: str) -> bool:
        """Check if file exists in storage."""
        pass


class LocalStorageBackend(BaseStorageBackend):
    """Local filesystem storage backend."""

    def __init__(self, root_dir: Optional[Path] = None):
        self.root_dir = Path(root_dir or settings.upload_dir).resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def _get_abs_path(self, storage_path: str) -> Path:
        p = Path(storage_path)
        if p.is_absolute():
            return p
        return (self.root_dir / storage_path).resolve()

    def store_file(
        self,
        file_bytes: bytes,
        business_id: int,
        document_type: str,
        filename: str,
        mime_type: str = "application/octet-stream",
    ) -> Tuple[str, str]:
        file_hash = compute_sha256(file_bytes)
        clean_name = sanitize_filename(filename)
        rel_path = f"{business_id}/{document_type}/{file_hash[:12]}_{clean_name}"
        abs_path = self.root_dir / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(file_bytes)
        return str(abs_path), file_hash

    def read_file(self, storage_path: str) -> bytes:
        abs_path = self._get_abs_path(storage_path)
        if not abs_path.exists():
            raise FileNotFoundError(f"File not found in local storage: {storage_path}")
        return abs_path.read_bytes()

    def store_extracted_text(self, storage_path: str, text: str) -> None:
        abs_path = self._get_abs_path(storage_path)
        ocr_path = abs_path.with_suffix(abs_path.suffix + ".ocr.txt")
        ocr_path.parent.mkdir(parents=True, exist_ok=True)
        ocr_path.write_text(text, encoding="utf-8")

    def get_extracted_text(self, storage_path: str) -> Optional[str]:
        abs_path = self._get_abs_path(storage_path)
        ocr_path = abs_path.with_suffix(abs_path.suffix + ".ocr.txt")
        if ocr_path.exists():
            try:
                return ocr_path.read_text(encoding="utf-8")
            except Exception:
                return None
        return None

    def generate_presigned_url(self, storage_path: str, expires_in_seconds: int = 300) -> str:
        # Local mock presigned URL: returns local file URI
        abs_path = self._get_abs_path(storage_path)
        return f"file:///{abs_path.as_posix()}"

    def delete_file(self, storage_path: str) -> bool:
        """Delete one stored file (and its cached extracted text)."""
        removed = False
        try:
            abs_path = self._get_abs_path(storage_path)
            for candidate in (abs_path, abs_path.with_suffix(abs_path.suffix + ".ocr.txt")):
                if candidate.exists():
                    candidate.unlink()
                    removed = True
        except Exception:
            pass
        return removed

    def delete_business_files(self, business_id: int) -> int:
        biz_dir = self.root_dir / str(business_id)
        count = 0
        if biz_dir.exists():
            for f in biz_dir.rglob("*"):
                if f.is_file():
                    count += 1
            shutil.rmtree(biz_dir, ignore_errors=True)
        return count

    def file_exists(self, storage_path: str) -> bool:
        abs_path = self._get_abs_path(storage_path)
        return abs_path.exists()


class R2StorageBackend(BaseStorageBackend):
    """Cloudflare R2 storage backend (S3-compatible API via boto3)."""

    def __init__(
        self,
        endpoint_url: Optional[str] = None,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        bucket_name: Optional[str] = None,
        s3_client: Optional[Any] = None,
    ):
        import boto3
        from botocore.config import Config

        self.endpoint_url = endpoint_url or os.environ.get("R2_ENDPOINT") or settings.r2_endpoint
        self.access_key_id = access_key_id or os.environ.get("R2_ACCESS_KEY_ID") or settings.r2_access_key_id
        self.secret_access_key = secret_access_key or os.environ.get("R2_SECRET_ACCESS_KEY") or settings.r2_secret_access_key
        self.bucket = bucket_name or os.environ.get("R2_BUCKET") or settings.r2_bucket or "raseed-documents"

        if s3_client is not None:
            self.s3 = s3_client
        else:
            self.s3 = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                config=Config(signature_version="s3v4"),
                region_name="auto",
            )

    def store_file(
        self,
        file_bytes: bytes,
        business_id: int,
        document_type: str,
        filename: str,
        mime_type: str = "application/octet-stream",
    ) -> Tuple[str, str]:
        file_hash = compute_sha256(file_bytes)
        clean_name = sanitize_filename(filename)
        key = f"{business_id}/{document_type}/{file_hash[:12]}_{clean_name}"
        try:
            self.s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=file_bytes,
                ContentType=mime_type,
                Metadata={"sha256": file_hash, "business_id": str(business_id)},
            )
            return key, file_hash
        except Exception as e:
            # Fall back to local storage if R2 is unreachable (e.g. offline sandbox)
            import logging
            logging.getLogger(__name__).warning("R2 put_object failed (%s); falling back to local storage.", e)
            return LocalStorageBackend().store_file(file_bytes, business_id, document_type, filename, mime_type)

    def read_file(self, storage_path: str) -> bytes:
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=storage_path)
            return response["Body"].read()
        except Exception:
            local = LocalStorageBackend()
            if local.file_exists(storage_path):
                return local.read_file(storage_path)
            raise

    def store_extracted_text(self, storage_path: str, text: str) -> None:
        ocr_key = f"{storage_path}.ocr.txt"
        try:
            self.s3.put_object(
                Bucket=self.bucket,
                Key=ocr_key,
                Body=text.encode("utf-8"),
                ContentType="text/plain; charset=utf-8",
            )
        except Exception:
            LocalStorageBackend().store_extracted_text(storage_path, text)

    def get_extracted_text(self, storage_path: str) -> Optional[str]:
        ocr_key = f"{storage_path}.ocr.txt"
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=ocr_key)
            return response["Body"].read().decode("utf-8")
        except Exception:
            return LocalStorageBackend().get_extracted_text(storage_path)

    def generate_presigned_url(self, storage_path: str, expires_in_seconds: int = 300) -> str:
        try:
            return self.s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": storage_path},
                ExpiresIn=expires_in_seconds,
            )
        except Exception:
            return LocalStorageBackend().generate_presigned_url(storage_path, expires_in_seconds=expires_in_seconds)

    def delete_file(self, storage_path: str) -> bool:
        """Delete one object (and its cached extracted text) from the R2 bucket."""
        removed = False
        for key in (storage_path, f"{storage_path}.ocr.txt"):
            try:
                self.s3.delete_object(Bucket=self.bucket, Key=key)
                removed = True
            except Exception:
                pass
        LocalStorageBackend().delete_file(storage_path)
        return removed

    def delete_business_files(self, business_id: int) -> int:
        prefix = f"{business_id}/"
        count = 0
        try:
            paginator = self.s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                objects = page.get("Contents", [])
                if objects:
                    delete_keys = [{"Key": obj["Key"]} for obj in objects]
                    self.s3.delete_objects(Bucket=self.bucket, Delete={"Objects": delete_keys})
                    count += len(delete_keys)
        except Exception:
            count += LocalStorageBackend().delete_business_files(business_id)
        return count

    def file_exists(self, storage_path: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=storage_path)
            return True
        except Exception:
            return LocalStorageBackend().file_exists(storage_path)


# Singleton cache for backend
_GLOBAL_STORAGE_BACKEND: Optional[BaseStorageBackend] = None


def get_storage_backend(backend_type: Optional[str] = None) -> BaseStorageBackend:
    """Return configured storage backend ('local' or 'r2')."""
    global _GLOBAL_STORAGE_BACKEND
    if _GLOBAL_STORAGE_BACKEND is not None and backend_type is None:
        return _GLOBAL_STORAGE_BACKEND

    # During pytest runs without explicit backend or outside sandbox, default to local storage
    if "PYTEST_CURRENT_TEST" in os.environ and backend_type is None and "STORAGE_BACKEND" not in os.environ:
        return LocalStorageBackend()

    chosen = backend_type or os.environ.get("STORAGE_BACKEND") or settings.storage_backend

    if chosen == "r2":
        return R2StorageBackend()
    return LocalStorageBackend()


def set_global_storage_backend(backend: Optional[BaseStorageBackend]) -> None:
    """Override storage backend (e.g. for testing with moto)."""
    global _GLOBAL_STORAGE_BACKEND
    _GLOBAL_STORAGE_BACKEND = backend
