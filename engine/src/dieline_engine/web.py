"""Client-side web search and fetch, modelled on OpenCode's websearch/webfetch.

They run here, not on a model provider's servers, so they work with any model
behind OpenRouter.

Search goes through a provider, as in OpenCode:

- Exa, through its hosted MCP endpoint (`tools/call` web_search_exa)
- Tavily, through its REST search API

Both run keyless (rate-limited) or with the user's own key.

Fetch follows OpenCode's webfetch rules:

- http(s) only, no credentials in the URL
- redirects followed by hand (at most 10), each new URL checked again
- a size cap and a timeout
- text types only, HTML converted to markdown

Fetch checks DNS results for private, loopback and link-local addresses
before each request.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

USER_AGENT = "Mozilla/5.0 (compatible; DielineStudio/0.1; web tools for a packaging design app)"
SEARCH_TIMEOUT = 25.0
MAX_SEARCH_BYTES = 256 * 1024
MAX_FETCH_BYTES = 5 * 1024 * 1024
DEFAULT_FETCH_TIMEOUT = 30.0
MAX_FETCH_TIMEOUT = 120.0
MAX_REDIRECTS = 10
MAX_OUTPUT_CHARS = 60_000
PROVIDERS = {"exa": "Exa", "tavily": "Tavily"}

EXA_ENDPOINT = "https://mcp.exa.ai/mcp"
TAVILY_ENDPOINT = "https://api.tavily.com/search"


@dataclass(frozen=True, slots=True)
class Hit:
    url: str
    title: str = ""
    content: str = ""
    published: str = ""

    def to_dict(self) -> dict:
        d = {"url": self.url}
        if self.title:
            d["title"] = self.title
        if self.published:
            d["published"] = self.published
        if self.content:
            d["content"] = self.content[:3000]
        return d


class WebError(ValueError):
    """A user-facing web tool failure (bad URL, blocked host, HTTP error)."""


# ------------------------------------------------------------------ search


def search(query: str, provider: str, key: str | None = None, client: httpx.Client | None = None) -> dict:
    query = query.strip()
    if not query:
        raise WebError("empty search query")
    if provider not in PROVIDERS:
        raise WebError(f"unknown search provider {provider!r}; choose one in Settings")
    own = client is None
    client = client or httpx.Client(timeout=SEARCH_TIMEOUT, headers={"User-Agent": USER_AGENT})
    try:
        hits = _exa(client, query, key) if provider == "exa" else _tavily(client, query, key)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        msg = {429: "rate limited (HTTP 429): wait, or add your own API key in Settings",
               401: "the search API key was rejected (HTTP 401)"}.get(status, f"search failed (HTTP {status})")
        raise WebError(f"{PROVIDERS[provider]}: {msg}") from None
    except httpx.HTTPError as exc:
        raise WebError(f"{PROVIDERS[provider]}: search request failed ({type(exc).__name__})") from None
    finally:
        if own:
            client.close()
    return {"provider": PROVIDERS[provider], "query": query, "results": [h.to_dict() for h in hits],
            **({} if hits else {"note": "No search results found. Try a different query."})}


def _bounded(response: httpx.Response, limit: int) -> bytes:
    # A decoded chunk can exceed the memory budget before iter_bytes yields it.
    encodings = response.headers.get_list("content-encoding", split_commas=True)
    if any(encoding.lower() != "identity" for encoding in encodings):
        raise WebError("compressed responses are unsupported")
    data = bytearray()
    for chunk in response.iter_bytes():
        if len(data) + len(chunk) > limit:
            raise WebError(f"response exceeded {limit} bytes")
        data.extend(chunk)
    return bytes(data)


def _exa(client: httpx.Client, query: str, key: str | None) -> list[Hit]:
    headers = {"Accept": "application/json, text/event-stream", "Accept-Encoding": "identity"}
    if key:
        headers["x-api-key"] = key
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "web_search_exa", "arguments": {"query": query, "numResults": 8}}}
    with client.stream("POST", EXA_ENDPOINT, json=body, headers=headers, follow_redirects=False) as r:
        r.raise_for_status()
        text = _bounded(r, MAX_SEARCH_BYTES).decode("utf-8", "replace")
    result = _mcp_result(text)
    if not isinstance(result, dict) or result.get("isError"):
        raise WebError("Exa: invalid response or search tool failed")
    content = next((c.get("text") for c in result.get("content", []) if c.get("text")), "")
    return _parse_exa(content)


def _mcp_result(body: str) -> dict | None:
    """The JSON-RPC result from a plain JSON or server-sent-events response."""
    def parse(payload: str) -> dict | None:
        payload = payload.strip()
        if not payload.startswith("{"):
            return None
        try:
            msg = json.loads(payload)
        except ValueError:
            return None
        return msg.get("result") if isinstance(msg, dict) else None

    direct = parse(body)
    if direct is not None:
        return direct
    for line in body.splitlines():
        if line.startswith("data: "):
            found = parse(line[6:])
            if found is not None:
                return found
    return None


def _parse_exa(text: str) -> list[Hit]:
    hits = []
    for block in re.split(r"\n\n---\n\n", text):
        url = re.search(r"^URL:\s*(.+)$", block, re.M)
        if not url:
            continue
        title = re.search(r"^Title:\s*(.+)$", block, re.M)
        published = re.search(r"^Published:\s*(.+)$", block, re.M)
        content = re.search(r"^(?:Highlights|Text):\s*\n?([\s\S]*)$", block, re.M)
        hits.append(Hit(
            url=url.group(1).strip(),
            title="" if not title or title.group(1).strip() == "N/A" else title.group(1).strip(),
            published="" if not published or published.group(1).strip() == "N/A" else published.group(1).strip(),
            content=content.group(1).strip() if content else "",
        ))
    return hits


def _tavily(client: httpx.Client, query: str, key: str | None) -> list[Hit]:
    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    headers.update({"Authorization": f"Bearer {key}"} if key else {"X-Tavily-Access-Mode": "keyless"})
    body = {"query": query, "search_depth": "basic", "chunks_per_source": 3, "max_results": 8}
    with client.stream("POST", TAVILY_ENDPOINT, json=body, headers=headers, follow_redirects=False) as r:
        r.raise_for_status()
        data = json.loads(_bounded(r, MAX_SEARCH_BYTES))
    return [Hit(url=i.get("url", ""), title=i.get("title", ""), content=i.get("content", ""))
            for i in data.get("results", []) if i.get("url")]


# ------------------------------------------------------------------- fetch


def check_url(url: str) -> str:
    """The URL without its fragment, if it may be fetched; else WebError."""
    return resolve_url(url)[0]


def resolve_url(url: str) -> tuple[str, str | None]:
    """(URL without its fragment, the checked IP address to connect to)."""
    url = url.split("#", 1)[0].strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise WebError("URL must use http:// or https://")
    if parts.username is not None or parts.password is not None:
        raise WebError("URL must not include credentials")
    host = parts.hostname
    if not host:
        raise WebError("URL has no host")
    # Check the same canonical hostname and port HTTPX will use.
    try:
        target = httpx.URL(url)
    except httpx.InvalidURL:
        raise WebError("invalid URL") from None
    port = target.port
    ip = _check_host(target.raw_host.decode("ascii"), port if port is not None else (443 if target.scheme == "https" else 80))
    return str(target), ip


def _check_host(host: str, port: int) -> str:
    """Resolve the host once and return the address to connect to. Every
    address it resolves to must be public: a name that also points inside the
    network is refused outright."""
    name = host.lower().rstrip(".")
    if name == "localhost" or name.endswith((".local", ".localhost", ".internal", ".lan")):
        raise WebError("that address is on the local network and can't be fetched")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise WebError(f"couldn't find the host {host}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_reserved or ip.is_multicast or getattr(ip, "is_site_local", False):
            raise WebError("that address is on a private or local network and can't be fetched")
    return infos[0][4][0].split("%", 1)[0]


def _pinned(url: str, ip: str | None, headers: dict[str, str]) -> tuple[httpx.URL, dict[str, str], dict]:
    """The request to send to the checked address itself, so the HTTP client
    can't resolve the name again and land somewhere else (DNS rebinding). The
    original name still goes in Host, and in TLS SNI and certificate checks."""
    target = httpx.URL(url)
    if ip is None:  # only when a test replaces the resolver
        return target, headers, {}
    host = target.host
    host_header = host if target.port is None else f"{host}:{target.port}"
    extensions = {"sni_hostname": host} if target.scheme == "https" else {}
    return target.copy_with(host=ip), {**headers, "Host": host_header}, extensions


ACCEPT = {
    "markdown": "text/markdown;q=1.0, text/x-markdown;q=0.9, text/plain;q=0.8, text/html;q=0.7, */*;q=0.1",
    "text": "text/plain;q=1.0, text/markdown;q=0.9, text/html;q=0.8, */*;q=0.1",
    "html": "text/html;q=1.0, application/xhtml+xml;q=0.9, text/plain;q=0.8, text/markdown;q=0.7, */*;q=0.1",
}
TEXTUAL = re.compile(r"^(text/.*|application/(json|xml|xhtml\+xml|javascript|ld\+json|rss\+xml|atom\+xml)|.*\+xml)$")


def fetch(url: str, fmt: str = "markdown", timeout: float | None = None,
          client: httpx.Client | None = None) -> dict:
    if fmt not in ACCEPT:
        raise WebError("format must be markdown, text or html")
    timeout = DEFAULT_FETCH_TIMEOUT if timeout is None else max(1.0, min(float(timeout), MAX_FETCH_TIMEOUT))
    own = client is None
    client = client or httpx.Client(timeout=timeout, trust_env=False)
    headers = {"User-Agent": USER_AGENT, "Accept": ACCEPT[fmt], "Accept-Language": "en-US,en;q=0.9",
               "Accept-Encoding": "identity"}
    try:
        current, ip = resolve_url(url)
        for _ in range(MAX_REDIRECTS + 1):
            target, hop_headers, ext = _pinned(current, ip, headers)
            request = client.build_request("GET", target, headers=hop_headers, extensions=ext)
            r = client.send(request, stream=True, follow_redirects=False)
            try:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    current, ip = resolve_url(urljoin(current, r.headers["location"]))  # every hop is checked
                    continue
                if r.status_code >= 400:
                    raise WebError(f"HTTP {r.status_code} from {urlsplit(current).hostname}")
                ctype = r.headers.get("content-type", "")
                mime = ctype.split(";", 1)[0].strip().lower()
                if mime and not TEXTUAL.match(mime):
                    raise WebError(f"unsupported content type {mime} (only text and web pages)")
                body = _bounded(r, MAX_FETCH_BYTES)
                charset = r.encoding or "utf-8"
                break
            finally:
                r.close()
        else:
            raise WebError(f"too many redirects (more than {MAX_REDIRECTS})")
    except httpx.TimeoutException:
        raise WebError("request timed out") from None
    except httpx.HTTPError as exc:
        raise WebError(f"couldn't fetch the page ({type(exc).__name__})") from None
    finally:
        if own:
            client.close()
    try:
        text = body.decode(charset, "replace")
    except (LookupError, UnicodeError):
        text = body.decode("utf-8", "replace")
    if "html" in mime and fmt != "html":
        text = html_to_markdown(text) if fmt == "markdown" else html_to_text(text)
    truncated = len(text) > MAX_OUTPUT_CHARS
    return {"url": current, "content_type": ctype, "content": text[:MAX_OUTPUT_CHARS],
            **({"truncated": True} if truncated else {})}


class _Markdown(HTMLParser):
    SKIP = {"script", "style", "noscript", "iframe", "object", "embed", "svg", "template", "head"}
    BLOCK = {"p", "div", "section", "article", "header", "footer", "main", "table", "tr", "ul", "ol",
             "blockquote", "pre", "figure", "form", "nav", "aside"}

    def __init__(self, markdown: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.md = markdown
        self.out: list[str] = []
        self.skip = 0
        self.href: str | None = None
        self.link_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP or self.skip:
            if tag in self.SKIP:
                self.skip += 1
            return
        a = dict(attrs)
        if tag in self.BLOCK:
            self.out.append("\n\n")
        elif tag == "br":
            self.out.append("\n")
        elif self.md and re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("\n- " if self.md else "\n")
        elif tag in ("td", "th"):
            self.out.append(" | " if self.md else "\t")
        elif self.md and tag == "a" and a.get("href"):
            self.href, self.link_text = a["href"], []

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if self.md and tag == "a" and self.href is not None:
            text = "".join(self.link_text).strip()
            self.out.append(f"[{text}]({self.href})" if text else "")
            self.href = None
        elif re.fullmatch(r"h[1-6]", tag) or tag in self.BLOCK:
            self.out.append("\n\n")

    def handle_data(self, data):
        if self.skip:
            return
        if self.href is not None:
            self.link_text.append(data)
        else:
            self.out.append(data)

    def result(self) -> str:
        text = "".join(self.out)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r" *\n *", "\n", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()


def html_to_markdown(html: str) -> str:
    p = _Markdown(markdown=True)
    p.feed(html)
    p.close()
    return p.result()


def html_to_text(html: str) -> str:
    p = _Markdown(markdown=False)
    p.feed(html)
    p.close()
    return p.result()
