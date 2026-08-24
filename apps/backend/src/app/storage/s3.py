import asyncio
import hashlib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import UploadFile


class UploadTooLargeError(RuntimeError):
    pass


class ObjectStorageError(RuntimeError):
    pass


class ObjectNotFoundError(RuntimeError):
    pass


class ObjectTooLargeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ObjectMetadata:
    size_bytes: int
    content_type: str


@dataclass(frozen=True)
class PresignedObject:
    url: str
    expires_at: datetime


@dataclass(frozen=True)
class BoundedObject:
    payload: bytes
    metadata: ObjectMetadata


class ResultObjectStorage(Protocol):
    bucket: str

    async def read_bounded(self, object_key: str, max_bytes: int) -> BoundedObject: ...

    async def head(self, object_key: str) -> ObjectMetadata: ...

    async def presign(self, object_key: str, ttl_seconds: int) -> PresignedObject: ...


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


class S3ResultObjectStorage:
    def __init__(
        self,
        *,
        internal_endpoint: str,
        external_endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str,
        addressing_style: str,
        internal_client: Any | None = None,
        signing_client: Any | None = None,
    ) -> None:
        self.bucket = bucket
        client_kwargs = {
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
            "region_name": region,
            "config": Config(
                signature_version="s3v4",
                s3={"addressing_style": addressing_style},
            ),
        }
        self._owns_internal_client = internal_client is None
        self._owns_signing_client = signing_client is None
        self._closed = False
        self._internal_client = internal_client or boto3.client(
            "s3", endpoint_url=internal_endpoint, **client_kwargs
        )
        try:
            self._signing_client = signing_client or boto3.client(
                "s3", endpoint_url=external_endpoint, **client_kwargs
            )
        except Exception:
            if self._owns_internal_client:
                try:
                    self._internal_client.close()
                except Exception:
                    pass
            raise

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        clients = []
        if self._owns_internal_client:
            clients.append(self._internal_client)
        if self._owns_signing_client:
            clients.append(self._signing_client)
        await asyncio.gather(*(asyncio.to_thread(client.close) for client in clients))

    async def head(self, object_key: str) -> ObjectMetadata:
        try:
            result = await asyncio.to_thread(
                self._internal_client.head_object,
                Bucket=self.bucket,
                Key=object_key,
            )
            return ObjectMetadata(
                size_bytes=int(result["ContentLength"]),
                content_type=str(result.get("ContentType") or ""),
            )
        except ClientError as error:
            if _is_not_found(error):
                raise ObjectNotFoundError from error
            raise ObjectStorageError("Object storage request failed") from error
        except Exception as error:
            raise ObjectStorageError("Object storage request failed") from error

    async def read_bounded(self, object_key: str, max_bytes: int) -> BoundedObject:
        metadata = await self.head(object_key)
        if metadata.size_bytes > max_bytes:
            raise ObjectTooLargeError
        body = None
        try:
            result = await asyncio.to_thread(
                self._internal_client.get_object,
                Bucket=self.bucket,
                Key=object_key,
            )
            body = result["Body"]
            content_length = int(result["ContentLength"])
            if content_length > max_bytes:
                raise ObjectTooLargeError
            if content_length != metadata.size_bytes:
                raise ObjectStorageError("Object storage request failed")
            payload = await asyncio.to_thread(body.read, max_bytes + 1)
            if len(payload) > max_bytes:
                raise ObjectTooLargeError
            if len(payload) != content_length:
                raise ObjectStorageError("Object storage request failed")
            return BoundedObject(payload=payload, metadata=metadata)
        except (ObjectStorageError, ObjectTooLargeError):
            raise
        except ClientError as error:
            if _is_not_found(error):
                raise ObjectNotFoundError from error
            raise ObjectStorageError("Object storage request failed") from error
        except Exception as error:
            raise ObjectStorageError("Object storage request failed") from error
        finally:
            if body is not None:
                try:
                    await asyncio.to_thread(body.close)
                except Exception:
                    pass

    async def presign(self, object_key: str, ttl_seconds: int) -> PresignedObject:
        signed_at = datetime.now(UTC)
        try:
            url = await asyncio.to_thread(
                self._signing_client.generate_presigned_url,
                "get_object",
                Params={"Bucket": self.bucket, "Key": object_key},
                ExpiresIn=ttl_seconds,
            )
        except Exception as error:
            raise ObjectStorageError("Object storage signing failed") from error
        return PresignedObject(
            url=url,
            expires_at=signed_at + timedelta(seconds=ttl_seconds),
        )


def _is_not_found(error: ClientError) -> bool:
    code = str(error.response.get("Error", {}).get("Code", ""))
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"404", "NoSuchKey", "NotFound"} or status == 404
