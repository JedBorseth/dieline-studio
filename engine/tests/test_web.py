import json

import httpx
import pytest

from dieline_engine import web


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_exa_search_through_its_mcp_endpoint():
    seen = {}

    def handler(request):
        seen["url"], seen["body"], seen["key"] = (
            str(request.url), json.loads(request.content), request.headers.get("x-api-key")
        )
        text = ("Title: FEFCO 0201\nURL: https://example.org/0201\nPublished: N/A\nHighlights:\nRegular slotted case"
                "\n\n---\n\nTitle: N/A\nURL: https://example.org/b\nText: second")
        payload = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}]}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content="event: message\ndata: " + json.dumps(payload) + "\n\n")

    out = web.search("fefco 0201", "exa", None, client(handler))
    assert seen["url"] == web.EXA_ENDPOINT and seen["body"]["method"] == "tools/call"
    assert seen["body"]["params"]["name"] == "web_search_exa"
    assert out["results"][0] == {"url": "https://example.org/0201", "title": "FEFCO 0201",
                                 "content": "Regular slotted case"}
    assert out["results"][1] == {"url": "https://example.org/b", "content": "second"}
    assert seen["key"] is None
    web.search("fefco 0201", "exa", "test-key", client(handler))
    assert seen["url"] == web.EXA_ENDPOINT and seen["key"] == "test-key"


def test_tavily_keyless_and_keyed():
    headers = []

    def handler(request):
        headers.append(dict(request.headers))
        return httpx.Response(200, json={"results": [{"title": "t", "url": "https://x.org", "content": "c"}]})

    web.search("q", "tavily", None, client(handler))
    web.search("q", "tavily", "tvly-key", client(handler))
    assert headers[0].get("x-tavily-access-mode") == "keyless" and "authorization" not in headers[0]
    assert headers[1]["authorization"] == "Bearer tvly-key"


def test_search_rate_limit_message():
    with pytest.raises(web.WebError, match="rate limited"):
        web.search("q", "tavily", None, client(lambda r: httpx.Response(429)))


def test_fetch_converts_html_and_blocks_private_redirects(monkeypatch):
    monkeypatch.setattr(web, "_check_host", lambda host, port: (_ for _ in ()).throw(
        web.WebError("that address is on a private or local network and can't be fetched"))
        if host in ("192.168.1.97", "intranet.example") else None)

    def handler(request):
        if request.url.path == "/page":
            html = "<html><head><title>x</title><script>evil()</script></head><body><h1>Boxes</h1>" \
                   "<p>See <a href='/rsc'>RSC</a></p><ul><li>one</li></ul></body></html>"
            return httpx.Response(200, headers={"content-type": "text/html"}, content=html)
        if request.url.path == "/hop":
            return httpx.Response(302, headers={"location": "http://192.168.1.97/secret"})
        if request.url.path == "/pdf":
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")
        return httpx.Response(404)

    out = web.fetch("https://public.example/page", client=client(handler))
    assert out["content"] == "# Boxes\n\nSee [RSC](/rsc)\n\n- one"
    with pytest.raises(web.WebError, match="private"):
        web.fetch("https://public.example/hop", client=client(handler))
    with pytest.raises(web.WebError, match="unsupported content type"):
        web.fetch("https://public.example/pdf", client=client(handler))


def test_check_url_rules():
    for bad in ("ftp://x.org/f", "https://u:p@x.org/", "http://localhost:7070/", "http://192.168.1.97/",
                "http://10.0.0.5/", "http://[::1]/", "http://printer.local/"):
        with pytest.raises(web.WebError):
            web.check_url(bad)
    assert web.check_url("https://1.1.1.1/a#frag") == "https://1.1.1.1/a"


def test_fetch_caps_identity_body_and_handles_unknown_charset(monkeypatch):
    monkeypatch.setattr(web, "MAX_FETCH_BYTES", 8)

    def handler(request):
        assert request.headers["accept-encoding"] == "identity"
        if request.url.path == "/compressed":
            return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(b""))
        content = b"123456789" if request.url.path == "/large" else "café".encode()
        return httpx.Response(200, headers={"content-type": "text/plain; charset=unknown"}, content=content)

    with client(handler) as mock:
        assert web.fetch("https://1.1.1.1/charset", client=mock)["content"] == "café"
        with pytest.raises(web.WebError, match="compressed responses"):
            web.fetch("https://1.1.1.1/compressed", client=mock)
        with pytest.raises(web.WebError, match="exceeded"):
            web.fetch("https://1.1.1.1/large", client=mock)
