"""Tests for cloudflared binary update helper and checksum handling."""

import asyncio
import hashlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import cloudflared


def _assert_raises(exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type:
        return
    raise AssertionError(f"Expected {exc_type.__name__} from {fn.__name__}")


def _assert_raises_async(exc_type, coro_fn, *args, **kwargs):
    try:
        asyncio.run(coro_fn(*args, **kwargs))
    except exc_type as e:
        return e
    raise AssertionError(f"Expected {exc_type.__name__} from {coro_fn.__name__}")


def _run_with_temp_paths(fn):
    def wrapper():
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_home = Path(tmpdir)
            original_paths = (
                cloudflared.CLOUDFLARED_BINARY_PATH,
                cloudflared.CLOUDFLARED_TOKEN_PATH,
                cloudflared.CLOUDFLARED_UNIT_PATH,
            )
            cloudflared.CLOUDFLARED_BINARY_PATH = tmp_home / ".local" / "bin" / "cloudflared"
            cloudflared.CLOUDFLARED_TOKEN_PATH = tmp_home / ".config" / "inframatik" / "cf-tunnel-token"
            cloudflared.CLOUDFLARED_UNIT_PATH = (
                tmp_home / ".config" / "systemd" / "user" / "cloudflared.service"
            )
            try:
                fn(tmp_home)
            finally:
                cloudflared.CLOUDFLARED_BINARY_PATH = original_paths[0]
                cloudflared.CLOUDFLARED_TOKEN_PATH = original_paths[1]
                cloudflared.CLOUDFLARED_UNIT_PATH = original_paths[2]
    wrapper.__name__ = fn.__name__
    return wrapper


def test_normalize_version_accepts_valid_values():
    assert cloudflared._normalize_version("2025.2.1") == "2025.2.1"
    assert cloudflared._normalize_version("stable_1-rc") == "stable_1-rc"


def test_normalize_version_uses_default_for_none_or_empty():
    assert cloudflared._normalize_version(None) == cloudflared.DEFAULT_CLOUDFLARED_VERSION
    assert cloudflared._normalize_version("") == cloudflared.DEFAULT_CLOUDFLARED_VERSION


def test_normalize_version_rejects_blank_whitespace():
    _assert_raises(ValueError, cloudflared._normalize_version, "   ")


def test_normalize_version_rejects_invalid_chars():
    _assert_raises(ValueError, cloudflared._normalize_version, "../bad")
    _assert_raises(ValueError, cloudflared._normalize_version, "bad version")
    _assert_raises(ValueError, cloudflared._normalize_version, "$bad")


def test_extract_sha256_parses_hash():
    text = "abc\n6f4f6f8ff6f6f8ff6f4f6f8ff6f6f8ff6f4f6f8ff6f6f8ff6f4f6f8ff6f6f8ff cloudflared\n"
    parsed = cloudflared._extract_sha256(text)
    assert parsed == "6f4f6f8ff6f6f8ff6f4f6f8ff6f6f8ff6f4f6f8ff6f6f8ff6f4f6f8ff6f6f8ff"


def test_extract_sha256_rejects_missing_hash():
    _assert_raises(RuntimeError, cloudflared._extract_sha256, "no hash here")


def test_download_expected_sha_uses_release_metadata_digest():
    async def fake_download_json(url: str, max_bytes: int = cloudflared.MAX_GITHUB_RELEASE_JSON_BYTES) -> dict:
        assert "/releases/tags/2025.2.1" in url
        return {
            "assets": [
                {
                    "name": "cloudflared-linux-amd64",
                    "digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                }
            ]
        }

    async def fake_download(_url: str, max_bytes: int = cloudflared.MAX_CLOUDFLARED_DOWNLOAD_BYTES) -> bytes:
        raise AssertionError("sidecar checksum should not be used when digest exists")

    original_json = cloudflared._download_json
    original_bytes = cloudflared._download_bytes
    cloudflared._download_json = fake_download_json
    cloudflared._download_bytes = fake_download
    try:
        sha = asyncio.run(cloudflared._download_expected_sha("2025.2.1", "amd64"))
    finally:
        cloudflared._download_json = original_json
        cloudflared._download_bytes = original_bytes

    assert sha == "a" * 64


def test_download_expected_sha_falls_back_to_sidecar_checksum():
    seen = []

    async def fake_download_json(url: str, max_bytes: int = cloudflared.MAX_GITHUB_RELEASE_JSON_BYTES) -> dict:
        raise RuntimeError(f"metadata failed: {url}")

    async def fake_download(url: str, max_bytes: int = cloudflared.MAX_CLOUDFLARED_DOWNLOAD_BYTES) -> bytes:
        seen.append(url)
        if url.endswith(".sha256"):
            raise RuntimeError("primary missing")
        if url.endswith(".sha256sum"):
            return b"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb  cloudflared\n"
        raise AssertionError(f"Unexpected URL: {url}")

    original_json = cloudflared._download_json
    original_bytes = cloudflared._download_bytes
    cloudflared._download_json = fake_download_json
    cloudflared._download_bytes = fake_download
    try:
        sha = asyncio.run(cloudflared._download_expected_sha("2025.2.1", "amd64"))
    finally:
        cloudflared._download_json = original_json
        cloudflared._download_bytes = original_bytes

    assert seen[0].endswith(".sha256")
    assert seen[1].endswith(".sha256sum")
    assert sha == "b" * 64


def test_download_expected_sha_raises_when_all_sources_fail():
    async def fake_download_json(url: str, max_bytes: int = cloudflared.MAX_GITHUB_RELEASE_JSON_BYTES) -> dict:
        raise RuntimeError(f"metadata failed: {url}")

    async def fake_download(url: str, max_bytes: int = cloudflared.MAX_CLOUDFLARED_DOWNLOAD_BYTES) -> bytes:
        raise RuntimeError(f"failed: {url}")

    original_json = cloudflared._download_json
    original_bytes = cloudflared._download_bytes
    cloudflared._download_json = fake_download_json
    cloudflared._download_bytes = fake_download
    try:
        exc = _assert_raises_async(
            RuntimeError,
            cloudflared._download_expected_sha,
            "2025.2.1",
            "amd64",
        )
    finally:
        cloudflared._download_json = original_json
        cloudflared._download_bytes = original_bytes

    assert "failed:" in str(exc)
    assert "metadata failed:" in str(exc)


@_run_with_temp_paths
def test_update_cloudflared_writes_binary_without_restart(_tmp_home: Path):
    sample = b"binary-data-v1"
    expected_sha = hashlib.sha256(sample).hexdigest()

    async def fake_expected_sha(_version: str, _arch: str) -> str:
        return expected_sha

    async def fake_download(_url: str, max_bytes: int = cloudflared.MAX_CLOUDFLARED_DOWNLOAD_BYTES) -> bytes:
        return sample

    versions = iter(["2025.1.0", "2025.2.1"])

    async def fake_version() -> str:
        return next(versions, "2025.2.1")

    async def fake_run_checked(cmd: list[str], error_prefix: str):
        raise AssertionError(f"Should not restart when unit missing: {cmd} {error_prefix}")

    original_expected_sha = cloudflared._download_expected_sha
    original_download = cloudflared._download_bytes
    original_arch = cloudflared._cloudflared_arch
    original_version = cloudflared.get_cloudflared_binary_version
    original_run_checked = cloudflared._run_checked
    cloudflared._download_expected_sha = fake_expected_sha
    cloudflared._download_bytes = fake_download
    cloudflared._cloudflared_arch = lambda: "amd64"
    cloudflared.get_cloudflared_binary_version = fake_version
    cloudflared._run_checked = fake_run_checked
    try:
        result = asyncio.run(cloudflared.update_cloudflared_user_binary("2025.2.1"))
    finally:
        cloudflared._download_expected_sha = original_expected_sha
        cloudflared._download_bytes = original_download
        cloudflared._cloudflared_arch = original_arch
        cloudflared.get_cloudflared_binary_version = original_version
        cloudflared._run_checked = original_run_checked

    assert cloudflared.CLOUDFLARED_BINARY_PATH.read_bytes() == sample
    assert result["version_requested"] == "2025.2.1"
    assert result["version_before"] == "2025.1.0"
    assert result["version_after"] == "2025.2.1"
    assert result["sha256"] == expected_sha
    assert result["restarted"] is False


@_run_with_temp_paths
def test_update_cloudflared_restarts_when_unit_exists(_tmp_home: Path):
    cloudflared.CLOUDFLARED_UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    cloudflared.CLOUDFLARED_UNIT_PATH.write_text("[Unit]\n")
    sample = b"binary-data-v2"
    expected_sha = hashlib.sha256(sample).hexdigest()
    restart_calls = []

    async def fake_expected_sha(_version: str, _arch: str) -> str:
        return expected_sha

    async def fake_download(_url: str, max_bytes: int = cloudflared.MAX_CLOUDFLARED_DOWNLOAD_BYTES) -> bytes:
        return sample

    async def fake_version() -> str:
        return "2025.2.1"

    async def fake_run_checked(cmd: list[str], _error_prefix: str):
        restart_calls.append(cmd)

    original_expected_sha = cloudflared._download_expected_sha
    original_download = cloudflared._download_bytes
    original_arch = cloudflared._cloudflared_arch
    original_version = cloudflared.get_cloudflared_binary_version
    original_run_checked = cloudflared._run_checked
    cloudflared._download_expected_sha = fake_expected_sha
    cloudflared._download_bytes = fake_download
    cloudflared._cloudflared_arch = lambda: "amd64"
    cloudflared.get_cloudflared_binary_version = fake_version
    cloudflared._run_checked = fake_run_checked
    try:
        result = asyncio.run(cloudflared.update_cloudflared_user_binary("2025.2.1"))
    finally:
        cloudflared._download_expected_sha = original_expected_sha
        cloudflared._download_bytes = original_download
        cloudflared._cloudflared_arch = original_arch
        cloudflared.get_cloudflared_binary_version = original_version
        cloudflared._run_checked = original_run_checked

    assert restart_calls == [["systemctl", "--user", "restart", "cloudflared.service"]]
    assert result["restarted"] is True


@_run_with_temp_paths
def test_update_cloudflared_rejects_checksum_mismatch(_tmp_home: Path):
    async def fake_expected_sha(_version: str, _arch: str) -> str:
        return "0" * 64

    async def fake_download(_url: str, max_bytes: int = cloudflared.MAX_CLOUDFLARED_DOWNLOAD_BYTES) -> bytes:
        return b"actual-bytes"

    original_expected_sha = cloudflared._download_expected_sha
    original_download = cloudflared._download_bytes
    original_arch = cloudflared._cloudflared_arch
    cloudflared._download_expected_sha = fake_expected_sha
    cloudflared._download_bytes = fake_download
    cloudflared._cloudflared_arch = lambda: "amd64"
    try:
        exc = _assert_raises_async(RuntimeError, cloudflared.update_cloudflared_user_binary, "2025.2.1")
    finally:
        cloudflared._download_expected_sha = original_expected_sha
        cloudflared._download_bytes = original_download
        cloudflared._cloudflared_arch = original_arch

    assert "checksum verification failed" in str(exc)
    assert not cloudflared.CLOUDFLARED_BINARY_PATH.exists()


@_run_with_temp_paths
def test_update_cloudflared_rejects_missing_checksum_by_default(_tmp_home: Path):
    async def fake_expected_sha(_version: str, _arch: str) -> str:
        raise RuntimeError("checksum not published")

    async def fake_download(_url: str, max_bytes: int = cloudflared.MAX_CLOUDFLARED_DOWNLOAD_BYTES) -> bytes:
        return b"actual-bytes"

    original_expected_sha = cloudflared._download_expected_sha
    original_download = cloudflared._download_bytes
    original_arch = cloudflared._cloudflared_arch
    original_flag = os.environ.pop("INFRAMATIK_ALLOW_UNSIGNED_CLOUDFLARED", None)
    cloudflared._download_expected_sha = fake_expected_sha
    cloudflared._download_bytes = fake_download
    cloudflared._cloudflared_arch = lambda: "amd64"
    try:
        exc = _assert_raises_async(RuntimeError, cloudflared.update_cloudflared_user_binary, "2025.2.1")
    finally:
        cloudflared._download_expected_sha = original_expected_sha
        cloudflared._download_bytes = original_download
        cloudflared._cloudflared_arch = original_arch
        if original_flag is None:
            os.environ.pop("INFRAMATIK_ALLOW_UNSIGNED_CLOUDFLARED", None)
        else:
            os.environ["INFRAMATIK_ALLOW_UNSIGNED_CLOUDFLARED"] = original_flag

    assert "refusing unverified install" in str(exc)
    assert not cloudflared.CLOUDFLARED_BINARY_PATH.exists()


@_run_with_temp_paths
def test_update_cloudflared_allows_missing_checksum_with_opt_in(_tmp_home: Path):
    sample = b"binary-data-no-checksum"
    expected_sha = hashlib.sha256(sample).hexdigest()

    async def fake_expected_sha(_version: str, _arch: str) -> str:
        raise RuntimeError("checksum not published")

    async def fake_download(_url: str, max_bytes: int = cloudflared.MAX_CLOUDFLARED_DOWNLOAD_BYTES) -> bytes:
        return sample

    versions = iter(["2025.1.0", "2025.2.1"])

    async def fake_version() -> str:
        return next(versions, "2025.2.1")

    original_expected_sha = cloudflared._download_expected_sha
    original_download = cloudflared._download_bytes
    original_arch = cloudflared._cloudflared_arch
    original_version = cloudflared.get_cloudflared_binary_version
    original_flag = os.environ.get("INFRAMATIK_ALLOW_UNSIGNED_CLOUDFLARED")
    cloudflared._download_expected_sha = fake_expected_sha
    cloudflared._download_bytes = fake_download
    cloudflared._cloudflared_arch = lambda: "amd64"
    cloudflared.get_cloudflared_binary_version = fake_version
    os.environ["INFRAMATIK_ALLOW_UNSIGNED_CLOUDFLARED"] = "1"
    try:
        result = asyncio.run(cloudflared.update_cloudflared_user_binary("2025.2.1"))
    finally:
        cloudflared._download_expected_sha = original_expected_sha
        cloudflared._download_bytes = original_download
        cloudflared._cloudflared_arch = original_arch
        cloudflared.get_cloudflared_binary_version = original_version
        if original_flag is None:
            os.environ.pop("INFRAMATIK_ALLOW_UNSIGNED_CLOUDFLARED", None)
        else:
            os.environ["INFRAMATIK_ALLOW_UNSIGNED_CLOUDFLARED"] = original_flag

    assert cloudflared.CLOUDFLARED_BINARY_PATH.read_bytes() == sample
    assert result["sha256"] == expected_sha
    assert result["version_requested"] == "2025.2.1"


@_run_with_temp_paths
def test_update_cloudflared_propagates_download_errors(_tmp_home: Path):
    async def fake_expected_sha(_version: str, _arch: str) -> str:
        return "a" * 64

    async def fake_download(_url: str, max_bytes: int = cloudflared.MAX_CLOUDFLARED_DOWNLOAD_BYTES) -> bytes:
        raise RuntimeError("download failed")

    original_expected_sha = cloudflared._download_expected_sha
    original_download = cloudflared._download_bytes
    original_arch = cloudflared._cloudflared_arch
    cloudflared._download_expected_sha = fake_expected_sha
    cloudflared._download_bytes = fake_download
    cloudflared._cloudflared_arch = lambda: "amd64"
    try:
        exc = _assert_raises_async(RuntimeError, cloudflared.update_cloudflared_user_binary, "2025.2.1")
    finally:
        cloudflared._download_expected_sha = original_expected_sha
        cloudflared._download_bytes = original_download
        cloudflared._cloudflared_arch = original_arch

    assert "download failed" in str(exc)


def run_tests():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ✓ {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {test.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    print("Running cloudflared update tests...\n")
    success = run_tests()
    sys.exit(0 if success else 1)
