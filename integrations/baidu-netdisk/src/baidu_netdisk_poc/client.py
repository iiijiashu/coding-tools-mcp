from __future__ import annotations

import hashlib
import json
import os
import posixpath
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

PAN_FILE_API = "https://pan.baidu.com/rest/2.0/xpan/file"
PCS_UPLOAD_API = "https://d.pcs.baidu.com/rest/2.0/pcs/superfile2"
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024
DEFAULT_REMOTE_DIR = "/来自：ChatGPT"


class BaiduNetdiskError(RuntimeError):
    """Sanitized upload error safe to return through MCP."""


@dataclass(frozen=True)
class UploadResult:
    status: str
    local_path: str
    remote_path: str
    size: int
    chunks_total: int
    chunks_uploaded: int
    rapid_upload: bool
    fs_id: int | None = None
    md5: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "local_path": self.local_path,
            "remote_path": self.remote_path,
            "size": self.size,
            "chunks_total": self.chunks_total,
            "chunks_uploaded": self.chunks_uploaded,
            "rapid_upload": self.rapid_upload,
            "fs_id": self.fs_id,
            "md5": self.md5,
        }


class BaiduNetdiskClient:
    """Minimal, direct-to-Baidu uploader.

    The caller's machine is the data plane. No VPS relay or arbitrary URL fetch is
    involved. The access token is supplied out-of-band and is redacted from errors.
    """

    def __init__(
        self,
        access_token: str,
        *,
        session: requests.Session | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        timeout: float = 45.0,
        max_retries: int = 3,
        retry_backoff: float = 0.5,
    ) -> None:
        if not access_token:
            raise ValueError("access_token must not be empty")
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if max_retries < 1:
            raise ValueError("max_retries must be >= 1")
        self.access_token = access_token
        self.session = session or requests.Session()
        self.chunk_size = chunk_size
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff

    @classmethod
    def from_env(cls, **kwargs: Any) -> "BaiduNetdiskClient":
        token = os.environ.get("BAIDU_NETDISK_ACCESS_TOKEN", "").strip()
        if not token:
            raise BaiduNetdiskError(
                "BAIDU_NETDISK_ACCESS_TOKEN is not configured in the local process"
            )
        return cls(token, **kwargs)

    def upload_file(
        self,
        local_file_path: str | os.PathLike[str],
        remote_dir: str = DEFAULT_REMOTE_DIR,
    ) -> UploadResult:
        local = Path(local_file_path).expanduser()
        if not local.is_file():
            raise BaiduNetdiskError(f"local file does not exist: {local}")

        remote_path = self._remote_path(remote_dir, local.name)
        size = local.stat().st_size
        block_md5s = self._hash_blocks(local)
        if not block_md5s:
            # Baidu's precreate requires a non-empty block_list. MD5 of an empty
            # payload gives us one logical zero-byte block.
            block_md5s = [hashlib.md5(b"").hexdigest()]

        precreate = self._precreate(remote_path, size, block_md5s)
        return_type = int(precreate.get("return_type", 1) or 1)
        chunks_total = len(block_md5s)

        if return_type == 2:
            return UploadResult(
                status="success",
                local_path=str(local),
                remote_path=remote_path,
                size=size,
                chunks_total=chunks_total,
                chunks_uploaded=0,
                rapid_upload=True,
                fs_id=_optional_int(precreate.get("fs_id")),
                md5=precreate.get("md5") or None,
            )

        upload_id = str(precreate.get("uploadid") or "")
        if not upload_id:
            raise BaiduNetdiskError("precreate succeeded but uploadid is missing")

        missing = self._missing_parts(precreate, chunks_total)
        uploaded_slice_md5s: list[str] = []
        uploaded_count = 0
        with local.open("rb") as fh:
            for part_index in missing:
                fh.seek(part_index * self.chunk_size)
                chunk = fh.read(self.chunk_size)
                expected_md5 = block_md5s[part_index]
                response = self._upload_part(
                    remote_path,
                    upload_id,
                    part_index,
                    local.name,
                    chunk,
                )
                server_md5 = str(response.get("md5") or "")
                if server_md5 and server_md5.lower() != expected_md5.lower():
                    raise BaiduNetdiskError(
                        f"part {part_index} md5 mismatch after upload"
                    )
                uploaded_slice_md5s.append(server_md5 or expected_md5)
                uploaded_count += 1

        if not uploaded_slice_md5s:
            raise BaiduNetdiskError(
                "precreate requested upload but returned no missing parts"
            )
        created = self._create(remote_path, size, upload_id, uploaded_slice_md5s)
        return UploadResult(
            status="success",
            local_path=str(local),
            remote_path=str(created.get("path") or remote_path),
            size=int(created.get("size") or size),
            chunks_total=chunks_total,
            chunks_uploaded=uploaded_count,
            rapid_upload=False,
            fs_id=_optional_int(created.get("fs_id")),
            md5=created.get("md5") or None,
        )

    def _hash_blocks(self, local: Path) -> list[str]:
        hashes: list[str] = []
        with local.open("rb") as fh:
            while True:
                chunk = fh.read(self.chunk_size)
                if not chunk:
                    break
                hashes.append(hashlib.md5(chunk).hexdigest())
        return hashes

    @staticmethod
    def _remote_path(remote_dir: str, filename: str) -> str:
        remote_dir = (remote_dir or DEFAULT_REMOTE_DIR).strip()
        if not remote_dir.startswith("/"):
            raise BaiduNetdiskError("remote_dir must be an absolute path beginning with /")
        if "\x00" in remote_dir or "\x00" in filename:
            raise BaiduNetdiskError("path contains NUL")
        normalized = posixpath.normpath(remote_dir)
        if not normalized.startswith("/"):
            normalized = "/" + normalized
        return posixpath.join(normalized, filename)

    @staticmethod
    def _missing_parts(precreate: dict[str, Any], chunks_total: int) -> list[int]:
        raw = precreate.get("block_list")
        if raw is None:
            # Conservative compatibility fallback for older responses: upload all.
            return list(range(chunks_total))
        if not isinstance(raw, list):
            raise BaiduNetdiskError("precreate block_list has unexpected type")
        result: list[int] = []
        for value in raw:
            try:
                idx = int(value)
            except (TypeError, ValueError) as exc:
                raise BaiduNetdiskError("precreate block_list contains invalid index") from exc
            if idx < 0 or idx >= chunks_total:
                raise BaiduNetdiskError("precreate block_list contains out-of-range index")
            if idx not in result:
                result.append(idx)
        return result

    def _precreate(self, path: str, size: int, block_md5s: Iterable[str]) -> dict[str, Any]:
        return self._post_json(
            PAN_FILE_API,
            params={"method": "precreate", "access_token": self.access_token},
            data={
                "path": path,
                "size": str(size),
                "isdir": "0",
                "autoinit": "1",
                "block_list": json.dumps(list(block_md5s), separators=(",", ":")),
                # Match Baidu's SDK: path conflict -> automatic rename.
                "rtype": "3",
            },
            operation="precreate",
        )

    def _upload_part(
        self,
        path: str,
        upload_id: str,
        part_index: int,
        filename: str,
        chunk: bytes,
    ) -> dict[str, Any]:
        return self._post_json(
            PCS_UPLOAD_API,
            params={
                "method": "upload",
                "type": "tmpfile",
                "access_token": self.access_token,
                "path": path,
                "uploadid": upload_id,
                "partseq": str(part_index),
            },
            files={"file": (filename, chunk, "application/octet-stream")},
            operation=f"upload part {part_index}",
        )

    def _create(
        self,
        path: str,
        size: int,
        upload_id: str,
        block_md5s: Iterable[str],
    ) -> dict[str, Any]:
        return self._post_json(
            PAN_FILE_API,
            params={"method": "create", "access_token": self.access_token},
            data={
                "path": path,
                "size": str(size),
                "isdir": "0",
                "uploadid": upload_id,
                "block_list": json.dumps(list(block_md5s), separators=(",", ":")),
                "rtype": "3",
            },
            operation="create",
        )

    def _post_json(self, url: str, *, operation: str, **kwargs: Any) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = self.session.post(url, timeout=self.timeout, **kwargs)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise BaiduNetdiskError(f"{operation} returned a non-object response")
                errno = _optional_int(payload.get("errno")) or 0
                if errno != 0:
                    # This is an application error. Retrying blindly can waste data,
                    # so only transport/5xx errors are retried by this client.
                    raise BaiduNetdiskError(f"{operation} failed with errno={errno}")
                return payload
            except BaiduNetdiskError:
                raise
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt + 1 >= self.max_retries:
                    break
                time.sleep(self.retry_backoff * (2**attempt))

        safe = self._redact(str(last_error) if last_error else "unknown transport error")
        raise BaiduNetdiskError(f"{operation} request failed: {safe}")

    def _redact(self, text: str) -> str:
        if self.access_token:
            text = text.replace(self.access_token, "<redacted>")
        return text


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
