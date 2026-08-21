import asyncio
import hashlib
from dataclasses import asdict, dataclass
from typing import Any, Protocol

import boto3
from botocore.config import Config
from fastapi import UploadFile


class UploadTooLargeError(RuntimeError):
    pass


class ObjectStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class S3ObjectReference:
    schema_version: int
    bucket: str
    object_key: str
    version_id: str | None
    etag: str | None
    size_bytes: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ObjectStorageUploader(Protocol):
    async def upload(self, file: UploadFile, object_key: str) -> S3ObjectReference: ...

    async def delete(self, reference: S3ObjectReference) -> None: ...


class S3MultipartUploader:
    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str,
        addressing_style: str,
        max_bytes: int,
        chunk_bytes: int,
        client: Any | None = None,
    ) -> None:
        self.bucket = bucket
        self.max_bytes = max_bytes
        self.chunk_bytes = chunk_bytes
        self.client = client or boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(s3={"addressing_style": addressing_style}),
        )

    async def upload(self, file: UploadFile, object_key: str) -> S3ObjectReference:
        upload_id: str | None = None
        try:
            created = await asyncio.to_thread(
                self.client.create_multipart_upload,
                Bucket=self.bucket,
                Key=object_key,
                ContentType=file.content_type or "application/octet-stream",
            )
            upload_id = created["UploadId"]
            parts: list[dict[str, Any]] = []
            digest = hashlib.sha256()
            size = 0
            part_number = 1
            while chunk := await file.read(self.chunk_bytes):
                size += len(chunk)
                if size > self.max_bytes:
                    raise UploadTooLargeError
                digest.update(chunk)
                uploaded = await asyncio.to_thread(
                    self.client.upload_part,
                    Bucket=self.bucket,
                    Key=object_key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=chunk,
                )
                parts.append({"PartNumber": part_number, "ETag": uploaded["ETag"]})
                part_number += 1
            if size == 0:
                raise ObjectStorageError("Uploaded file is empty")
            completed = await asyncio.to_thread(
                self.client.complete_multipart_upload,
                Bucket=self.bucket,
                Key=object_key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
            upload_id = None
            return S3ObjectReference(
                schema_version=1,
                bucket=self.bucket,
                object_key=object_key,
                version_id=completed.get("VersionId"),
                etag=completed.get("ETag"),
                size_bytes=size,
                sha256=digest.hexdigest(),
            )
        except (UploadTooLargeError, ObjectStorageError):
            raise
        except Exception as error:
            raise ObjectStorageError("Object storage upload failed") from error
        finally:
            if upload_id is not None:
                try:
                    await asyncio.to_thread(
                        self.client.abort_multipart_upload,
                        Bucket=self.bucket,
                        Key=object_key,
                        UploadId=upload_id,
                    )
                except Exception:
                    pass

    async def delete(self, reference: S3ObjectReference) -> None:
        try:
            await asyncio.to_thread(
                self.client.delete_object,
                Bucket=reference.bucket,
                Key=reference.object_key,
            )
        except Exception as error:
            raise ObjectStorageError("Object storage cleanup failed") from error
