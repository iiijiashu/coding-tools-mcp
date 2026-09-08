from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import requests

from baidu_netdisk_poc.client import BaiduNetdiskClient, BaiduNetdiskError


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self.payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected HTTP call")
        return self.responses.pop(0)


def test_upload_only_missing_parts_and_keeps_chunks_bounded(tmp_path: Path) -> None:
    local = tmp_path / "payload.bin"
    local.write_bytes(b"AAAABBBBCCCC")
    hashes = [hashlib.md5(x).hexdigest() for x in (b"AAAA", b"BBBB", b"CCCC")]
    session = FakeSession(
        [
            FakeResponse({"errno": 0, "return_type": 1, "uploadid": "u1", "block_list": [1]}),
            FakeResponse({"errno": 0, "md5": hashes[1]}),
            FakeResponse({"errno": 0, "fs_id": 42, "path": "/来自：ChatGPT/payload.bin", "size": 12, "md5": "whole"}),
        ]
    )
    client = BaiduNetdiskClient("secret", session=session, chunk_size=4)

    result = client.upload_file(local)

    assert result.status == "success"
    assert result.chunks_total == 3
    assert result.chunks_uploaded == 1
    assert result.fs_id == 42
    assert len(session.calls) == 3
    upload_kwargs = session.calls[1][1]
    assert upload_kwargs["params"]["partseq"] == "1"
    assert upload_kwargs["files"]["file"][1] == b"BBBB"
    assert len(upload_kwargs["files"]["file"][1]) <= 4
    create_kwargs = session.calls[2][1]
    assert create_kwargs["data"]["block_list"] == f'["{hashes[1]}"]'


def test_rapid_upload_avoids_data_transfer_and_create(tmp_path: Path) -> None:
    local = tmp_path / "rapid.txt"
    local.write_text("already on server", encoding="utf-8")
    session = FakeSession([FakeResponse({"errno": 0, "return_type": 2})])
    client = BaiduNetdiskClient("secret", session=session, chunk_size=4)

    result = client.upload_file(local)

    assert result.rapid_upload is True
    assert result.chunks_uploaded == 0
    assert len(session.calls) == 1


def test_missing_block_list_falls_back_to_all_parts(tmp_path: Path) -> None:
    local = tmp_path / "all.bin"
    local.write_bytes(b"AAAABBBB")
    hashes = [hashlib.md5(x).hexdigest() for x in (b"AAAA", b"BBBB")]
    session = FakeSession(
        [
            FakeResponse({"errno": 0, "return_type": 1, "uploadid": "u1"}),
            FakeResponse({"errno": 0, "md5": hashes[0]}),
            FakeResponse({"errno": 0, "md5": hashes[1]}),
            FakeResponse({"errno": 0, "fs_id": 7, "path": "/来自：ChatGPT/all.bin", "size": 8}),
        ]
    )
    result = BaiduNetdiskClient("secret", session=session, chunk_size=4).upload_file(local)
    assert result.chunks_uploaded == 2


def test_remote_dir_must_be_absolute(tmp_path: Path) -> None:
    local = tmp_path / "x.txt"
    local.write_text("x", encoding="utf-8")
    client = BaiduNetdiskClient("secret", session=FakeSession([]))
    with pytest.raises(BaiduNetdiskError, match="absolute path"):
        client.upload_file(local, remote_dir="relative")


def test_token_is_redacted_from_transport_error(tmp_path: Path) -> None:
    local = tmp_path / "x.txt"
    local.write_text("x", encoding="utf-8")
    token = "very-secret-token"

    class FailingSession:
        def post(self, url: str, **kwargs):
            raise requests.ConnectionError(f"failed URL?access_token={token}")

    client = BaiduNetdiskClient(
        token,
        session=FailingSession(),
        chunk_size=4,
        max_retries=1,
    )
    with pytest.raises(BaiduNetdiskError) as exc:
        client.upload_file(local)
    assert token not in str(exc.value)
    assert "<redacted>" in str(exc.value)


def test_invalid_missing_part_index_is_rejected(tmp_path: Path) -> None:
    local = tmp_path / "x.bin"
    local.write_bytes(b"AAAA")
    session = FakeSession(
        [FakeResponse({"errno": 0, "return_type": 1, "uploadid": "u1", "block_list": [3]})]
    )
    client = BaiduNetdiskClient("secret", session=session, chunk_size=4)
    with pytest.raises(BaiduNetdiskError, match="out-of-range"):
        client.upload_file(local)
