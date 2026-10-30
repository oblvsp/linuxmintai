"""Проверка веб-инструментов агента: SSRF-защита, разбор HTML, реальный поиск.

Запуск: ~/ai-workspace/venv/bin/python tests/test_web_tools.py
Тесты без сети выполняются всегда; поиск и загрузка страницы — только если есть
интернет и контейнер searxng (иначе помечаются SKIP).
"""
import os
import sys
import urllib.error
from pathlib import Path

HOME = Path(os.environ.get("LMINTAI_HOME", Path.home() / "ai-workspace"))
sys.path.insert(0, str(HOME))
import web_tools as wt  # noqa: E402
from web_tools import FetchPageTool, WebSearchTool  # noqa: E402

BLOCKED = [
    "http://127.0.0.1:8888/",
    "http://localhost/",
    "http://10.0.0.1/",
    "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/",
    "file:///etc/passwd",
    "ftp://example.com/",
    "http://user:pass@example.com/",
]
ALLOWED = [
    "https://example.com/",
    "http://8.8.8.8/",
]

failures = []
skipped = []


def check(cond: bool, msg: str) -> None:
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def skip(msg: str) -> None:
    print("SKIP " + msg)
    skipped.append(msg)


def main() -> int:
    for url in BLOCKED:
        check(not wt.url_allowed(url), f"запрещён адрес: {url}")
    for url in ALLOWED:
        check(wt.url_allowed(url), f"разрешён публичный адрес: {url}")

    try:
        wt._GuardedRedirect().redirect_request(None, None, 302, "Found", {}, "http://127.0.0.1:8888/")
        check(False, "редирект на loopback запрещён")
    except urllib.error.HTTPError:
        check(True, "редирект на loopback запрещён")

    text = wt.to_text(b"<html><style>a{}</style><script>x()</script><p>Hello &amp; bye</p></html>", "utf-8")
    check(text == "Hello & bye", f"HTML вычищен: {text!r}")

    r = FetchPageTool()._run("http://127.0.0.1:8888/")
    check(r.startswith("ОТКАЗАНО"), "fetch_page не ходит на loopback")

    try:
        page = FetchPageTool()._run("https://example.com/")
    except (urllib.error.URLError, OSError):
        skip("нет интернета — загрузка страницы не проверена")
    else:
        check("Example Domain" in page, "публичная страница загружена и очищена от HTML")

    try:
        found = WebSearchTool()._run("linux loopback")
    except Exception:
        found = ""
    if not found or found.startswith("ПОИСК НЕДОСТУПЕН") or found.startswith("Ничего не найдено"):
        skip("SearXNG недоступен — поиск не проверен (install.sh --with-search)")
    else:
        check("http" in found, "поиск вернул результаты со ссылками")

    print()
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)}")
        return 1
    print(f"Пройдено; пропущено из-за отсутствия сети/сервиса: {len(skipped)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
