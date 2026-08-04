from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from lib import url_safety


def test_private_and_local_urls_are_rejected():
    with pytest.raises(url_safety.UnsafeUrlError):
        url_safety.validate_public_url("http://127.0.0.1/admin")
    with pytest.raises(url_safety.UnsafeUrlError):
        url_safety.validate_public_url("http://localhost:5000/")


def test_hostname_resolving_to_private_address_is_rejected():
    with patch.object(url_safety.socket, "getaddrinfo", return_value=[
        (2, 1, 6, "", ("10.0.0.4", 80)),
    ]):
        with pytest.raises(url_safety.UnsafeUrlError):
            url_safety.validate_public_url("http://example.test/feed")


def test_stream_without_content_length_is_still_bounded(tmp_path):
    response = SimpleNamespace(
        headers={},
        iter_content=lambda chunk_size: iter([b"1234", b"5678"]),
    )

    @contextmanager
    def fake_response(*_args, **_kwargs):
        yield response

    with patch.object(url_safety, "safe_response", fake_response):
        with pytest.raises(url_safety.DownloadTooLargeError):
            url_safety.safe_get_bytes("https://example.com/audio", max_bytes=6, timeout=1)
        destination = tmp_path / "audio.mp3"
        with pytest.raises(url_safety.DownloadTooLargeError):
            url_safety.safe_download(
                "https://example.com/audio", destination, max_bytes=6, timeout=1,
            )
        assert not destination.exists()
