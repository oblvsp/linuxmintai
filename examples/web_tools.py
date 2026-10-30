"""Веб-доступ для локального агента: поиск через локальный SearXNG и чтение страниц.

Модель остаётся локальной, но текст запроса уходит в поисковые системы — это
единственный канал наружу, и он включается только этими инструментами.
fetch_page не ходит на приватные адреса (loopback, LAN, link-local, metadata):
страница может подсунуть редирект на 127.0.0.1, поэтому каждый шаг проверяется.
"""
import html
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

from crewai.tools import BaseTool

SEARX_URL = "http://127.0.0.1:8888"
MAX_PAGE_BYTES = 1_000_000
MAX_TEXT = 4000
MAX_RESULTS = 6
TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) linuxmintai/1.0"

_STRIP = re.compile(r"(?is)<(script|style|noscript|template|svg)[^>]*>.*?</\1>")
_TAGS = re.compile(r"(?s)<[^>]+>")


def host_is_public(host: str) -> bool:
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


def url_allowed(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return False
    if parts.username or parts.password:
        return False
    return host_is_public(parts.hostname or "")


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not url_allowed(newurl):
            raise urllib.error.HTTPError(
                newurl, code, "редирект на непубличный адрес", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def to_text(raw: bytes, charset: str) -> str:
    text = raw.decode(charset or "utf-8", errors="replace")
    text = _STRIP.sub(" ", text)
    text = _TAGS.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def fetch(url: str) -> str:
    if not url_allowed(url):
        raise ValueError("адрес не публичный или схема не http/https")
    opener = urllib.request.build_opener(_GuardedRedirect())
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
        },
    )
    with opener.open(req, timeout=TIMEOUT) as resp:
        ctype = resp.headers.get_content_type()
        if not (ctype.startswith("text/") or ctype in {"application/json", "application/xml"}):
            raise ValueError(f"не текстовый ответ: {ctype}")
        raw = resp.read(MAX_PAGE_BYTES)
        charset = resp.headers.get_content_charset() or "utf-8"
    title = ""
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw.decode(charset, errors="replace"))
    if m:
        title = re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()[:200]
    body = to_text(raw, charset)
    if len(body) > MAX_TEXT:
        body = body[:MAX_TEXT] + f"\n...[обрезано, всего {len(body)} символов]"
    return (f"{title}\n\n{body}" if title else body).strip()


class WebSearchTool(BaseTool):
    name: str = "web_search"
    description: str = (
        "Ищет в интернете через локальный SearXNG. Аргумент — поисковый запрос. "
        "Возвращает до 6 результатов: заголовок, ссылка, короткая выдержка. "
        "Выдержки бывают устаревшими и обрезанными: для версий, дат и точных цифр "
        "открой первоисточник через fetch_page, а не цитируй сниппет."
    )

    def _run(self, query: str) -> str:
        url = f"{SEARX_URL}/search?" + urllib.parse.urlencode(
            {"q": query, "format": "json", "safesearch": 1}
        )
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
                data = json.load(resp)
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
            return f"ПОИСК НЕДОСТУПЕН: {exc}. Проверь контейнер searxng на 127.0.0.1:8888."
        results = data.get("results", [])[:MAX_RESULTS]
        if not results:
            return "Ничего не найдено. Переформулируй запрос."
        lines = []
        for i, item in enumerate(results, 1):
            title = re.sub(r"\s+", " ", item.get("title") or "").strip()
            snippet = re.sub(r"\s+", " ", item.get("content") or "").strip()[:300]
            lines.append(f"{i}. {title} — {item.get('url', '')}\n   {snippet}")
        return "\n".join(lines)


class FetchPageTool(BaseTool):
    name: str = "fetch_page"
    description: str = (
        "Скачивает публичную веб-страницу и возвращает её текст без HTML-разметки "
        "(до 4000 символов). Аргумент — http(s)-ссылка. Приватные и локальные адреса "
        "запрещены, в том числе через редирект."
    )

    def _run(self, url: str) -> str:
        try:
            return fetch(url)
        except ValueError as exc:
            return f"ОТКАЗАНО: {exc}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return f"НЕ УДАЛОСЬ ЗАГРУЗИТЬ: {exc}"
