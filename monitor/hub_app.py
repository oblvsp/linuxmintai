#!/usr/bin/env python3
"""Веб-приложение linuxmintai · хаб: окно GTK3 + WebKit2 без браузерной обвязки.

    python3 hub_app.py [URL]

По умолчанию открывает http://127.0.0.1:11436/; сервер сам выбирает
вертикальную (/v1) или широкую (/v2) вёрстку по ориентации окна.
"""
import sys
from urllib.parse import urlsplit

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, Gio, GLib, Gtk, WebKit2  # noqa: E402

URL = "http://127.0.0.1:11436/"
TITLE = "linuxmintai · хаб"

ERROR_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<style>
 body { margin:0; background:#0f1115; color:#e6e8eb; height:100vh;
        font:15px/1.5 system-ui, sans-serif;
        display:flex; align-items:center; justify-content:center; text-align:center; }
 h2 { margin:0 0 8px; font-size:18px; }
 p { margin:4px 0; color:#9aa4b2; }
 code { background:#171a21; border:1px solid #232833; border-radius:8px;
        padding:2px 7px; color:#e6e8eb; }
</style></head><body><div>
 <h2>Панель не отвечает</h2>
 <p>Проверьте: <code>systemctl --user start lmintai-status</code></p>
 <p>Повтор через несколько секунд…</p>
</div></body></html>"""


class HubWindow(Gtk.ApplicationWindow):
    def __init__(self, app, url):
        super().__init__(application=app, title=TITLE)
        self.set_default_size(1280, 860)
        self.set_icon_name("lmintai-hub")
        self.url = url
        parts = urlsplit(url)
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.retrying = False
        self.view = WebKit2.WebView()
        settings = self.view.get_settings()
        settings.set_enable_developer_extras(False)
        settings.set_enable_page_cache(False)
        try:
            self.view.set_background_color(Gdk.RGBA(0.059, 0.067, 0.082, 1))
        except AttributeError:
            pass
        self.view.connect("load-failed", self.on_load_failed)
        self.view.connect("key-press-event", self.on_key)
        self.view.connect("create", self.on_create)
        self.view.connect("decide-policy", self.on_decide_policy)
        self.add(self.view)
        self.connect("key-press-event", self.on_key)
        self.view.load_uri(url)

    def is_internal(self, uri):
        return uri.startswith(self.origin + "/") or uri.startswith("about:")

    def open_external(self, uri):
        if uri.startswith(("http://", "https://")):
            Gio.AppInfo.launch_default_for_uri(uri, None)

    def on_create(self, _view, action):
        # target="_blank": без обработчика WebKit молча глотает клик
        request = action.get_request()
        if request is not None:
            self.open_external(request.get_uri())
        return None

    def on_decide_policy(self, _view, decision, kind):
        if kind != WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            return False
        uri = decision.get_navigation_action().get_request().get_uri() or ""
        if self.is_internal(uri):
            return False
        self.open_external(uri)
        decision.ignore()
        return True

    def on_key(self, _widget, event):
        if event.keyval == Gdk.KEY_F5:
            self.view.reload()
            return True
        if event.keyval == Gdk.KEY_F11:
            gdk_win = self.get_window()
            full = bool(gdk_win and gdk_win.get_state() & Gdk.WindowState.FULLSCREEN)
            self.unfullscreen() if full else self.fullscreen()
            return True
        return False

    def on_load_failed(self, view, _event, _uri, _error):
        view.load_html(ERROR_HTML, self.url)
        if not self.retrying:
            self.retrying = True
            GLib.timeout_add_seconds(3, self.retry)
        return True

    def retry(self):
        self.retrying = False
        self.view.load_uri(self.url)
        return False


def main():
    Gdk.set_program_class("linuxmintai-hub")
    GLib.set_prgname("lmintai-hub")
    url = sys.argv[1] if len(sys.argv) > 1 else URL
    app = Gtk.Application(application_id="org.linuxmintai.Hub")

    def activate(a):
        win = HubWindow(a, url)
        a.add_window(win)
        win.show_all()

    app.connect("activate", activate)
    return app.run(sys.argv[:1])


if __name__ == "__main__":
    sys.exit(main())
