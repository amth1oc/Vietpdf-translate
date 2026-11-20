from __future__ import annotations

import asyncio

import httpx
import pytest

from vietpdf.security.http import (
    NetworkPolicy,
    NetworkPolicyError,
    ResponseTooLargeError,
    SafeHttpClient,
)


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_policy_allows_only_listed_https_hosts() -> None:
    p = NetworkPolicy.create(["api.openai.com"])
    p.check_url("https://api.openai.com/v1/models")
    for bad in [
        "http://api.openai.com/v1",  # không HTTPS
        "https://evil.example.com/",  # host lạ
        "https://api.openai.com.evil.com/",  # hậu tố đánh lừa
        "https://user:pw@api.openai.com/",  # thông tin đăng nhập trong URL
        "ftp://api.openai.com/",
        "file:///etc/passwd",
    ]:
        with pytest.raises(NetworkPolicyError):
            p.check_url(bad)


def test_offline_mode_blocks_everything_but_loopback() -> None:
    p = NetworkPolicy.create(["localhost", "api.openai.com"], offline=True)
    p.check_url("http://localhost:11434/v1/models")
    with pytest.raises(NetworkPolicyError):
        p.check_url("https://api.openai.com/v1")


def test_loopback_must_still_be_allowlisted() -> None:
    p = NetworkPolicy.create(["api.openai.com"])
    with pytest.raises(NetworkPolicyError):
        p.check_url("http://127.0.0.1:8080/")


def test_redirect_is_not_followed() -> None:
    hits: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(str(req.url))
        return httpx.Response(302, headers={"location": "https://evil.example.com/steal"})

    client = SafeHttpClient(NetworkPolicy.create(["api.openai.com"]), transport=httpx.MockTransport(handler))
    resp = run(client.get("https://api.openai.com/v1/models"))
    run(client.aclose())
    assert resp.status == 302
    assert hits == ["https://api.openai.com/v1/models"]


def test_blocked_host_never_reaches_transport() -> None:
    called = False

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    client = SafeHttpClient(NetworkPolicy.create(["api.openai.com"]), transport=httpx.MockTransport(handler))
    with pytest.raises(NetworkPolicyError):
        run(client.get("https://evil.example.com/"))
    run(client.aclose())
    assert not called


def test_response_size_limit() -> None:
    big = b"x" * 2048
    client = SafeHttpClient(
        NetworkPolicy.create(["api.openai.com"], max_response_bytes=1024),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=big)),
    )
    with pytest.raises(ResponseTooLargeError):
        run(client.get("https://api.openai.com/x"))
    run(client.aclose())
