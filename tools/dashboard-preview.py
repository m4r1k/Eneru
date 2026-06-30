#!/usr/bin/env python3
"""Dashboard preview harness — review web/ changes against a LIVE daemon.

The browser dashboard is static assets (``src/eneru/web/``) talking to the
daemon's JSON API. To review a change to ``app.js`` / ``style.css`` you want the
*working-tree* assets rendered against *real* telemetry — not a synthetic
fixture that never reproduces the production data shape.

ELI5: this is a one-way mirror in front of your running daemon. You look at your
brand-new dashboard code, but every time it asks a question ("what's the
battery at?") the question is passed straight through to the real daemon and the
real answer comes back. So you see tomorrow's UI on top of today's live data.

How it works: a tiny HTTP server serves the working-tree ``index.html`` /
``app.js`` / ``style.css`` and forwards every other request (``/api/*``,
``/health``, ...) to the running daemon. Then Playwright screenshots each tab in
light and dark themes so you can eyeball the result (and console errors are
printed).

Usage (inside the dev venv, with `pip install -e ".[dev]"` + `playwright
install chromium`):

    python tools/dashboard-preview.py                 # all tabs, light+dark
    python tools/dashboard-preview.py --themes light  # light only
    python tools/dashboard-preview.py --daemon http://127.0.0.1:9191 \
        --out /tmp/eneru-shots --tabs overview,battery

Outputs ``<out>/dash-[<theme>-]<tab>.png``. Read them to verify the change;
this is the loop the project uses instead of trusting string-only UI tests.
"""
import argparse
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Default working-tree web assets, resolved relative to this file so it works
# regardless of the caller's CWD.
DEFAULT_WEB = Path(__file__).resolve().parent.parent / "src" / "eneru" / "web"
DEFAULT_TABS = ["overview", "power", "battery", "energy", "events",
                "shutdown", "config"]
# Static files the harness serves locally; everything else is proxied.
_STATIC = {
    "/": ("index.html", "text/html"),
    "/index.html": ("index.html", "text/html"),
    "/app.js": ("app.js", "application/javascript"),
    "/style.css": ("style.css", "text/css"),
}


def _make_handler(web_dir: Path, daemon: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence per-request logging
            pass

        def _serve_static(self, rel: str, ctype: str) -> None:
            try:
                body = (web_dir / rel).read_bytes()
            except OSError as exc:
                self.send_error(404, f"missing asset {rel}: {exc}")
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _proxy(self) -> None:
            url = daemon.rstrip("/") + self.path
            # Forward the request body + relevant headers so non-GET verbs
            # (authed POST self-test, control commands) carry their payload.
            length = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(length) if length else None
            req = urllib.request.Request(url, data=data, method=self.command)
            for h in ("Content-Type", "Authorization"):
                if self.headers.get(h):
                    req.add_header(h, self.headers[h])
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = resp.read()
                    ctype = resp.headers.get("Content-Type", "application/json")
                    code = resp.status
            except urllib.error.HTTPError as exc:
                body = exc.read()
                ctype = exc.headers.get("Content-Type", "application/json")
                code = exc.code
            except Exception as exc:  # daemon down / unreachable
                body = ('{"error":{"message":"preview proxy: %s"}}'
                        % exc).encode()
                ctype, code = "application/json", 502
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            static = _STATIC.get(self.path.split("?", 1)[0])
            if static:
                return self._serve_static(*static)
            return self._proxy()

        # The dashboard is read-only over GET; proxy any other verb too so
        # auth/POST flows can be exercised manually if needed.
        def do_POST(self):
            return self._proxy()

    return Handler


def _shoot(port: int, out: Path, tabs, themes, settle_ms: int) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed. Install the dev extra and the "
              "browser:\n    pip install -e \".[dev]\"\n    playwright install "
              "chromium", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    issues = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for theme in themes:
            page = browser.new_page(viewport={"width": 1280, "height": 1000})
            page.add_init_script(
                f"localStorage.setItem('eneru_theme', '{theme}');")
            msgs = []
            page.on("console", lambda m: msgs.append((m.type, m.text)))
            page.goto(f"http://127.0.0.1:{port}/#{tabs[0]}")
            page.wait_for_timeout(settle_ms)   # first refresh + charts settle
            pref = "" if theme == "light" else f"{theme}-"
            for tab in tabs:
                page.evaluate("(h) => { location.hash = h; }", tab)
                page.wait_for_timeout(max(800, settle_ms // 2))
                page.screenshot(path=str(out / f"dash-{pref}{tab}.png"),
                                full_page=True)
                print(f"shot {theme} {tab}")
            for mtype, text in msgs:
                if mtype in ("error", "warning"):
                    issues += 1
                    print(f"console[{theme}] {mtype}: {text[:200]}")
            page.close()
        browser.close()
    return 1 if issues else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--web-dir", type=Path, default=DEFAULT_WEB,
                    help="working-tree web/ assets (default: src/eneru/web)")
    ap.add_argument("--daemon", default="http://127.0.0.1:9191",
                    help="base URL of the running daemon API to proxy to")
    ap.add_argument("--port", type=int, default=9232,
                    help="local port for the preview server")
    ap.add_argument("--out", type=Path, default=Path("/tmp/eneru-preview"),
                    help="output directory for screenshots")
    ap.add_argument("--tabs", default=",".join(DEFAULT_TABS),
                    help="comma-separated dashboard tabs to capture")
    ap.add_argument("--themes", default="light,dark",
                    help="comma-separated themes (light,dark)")
    ap.add_argument("--settle-ms", type=int, default=2500,
                    help="ms to wait for the first refresh + charts to settle")
    args = ap.parse_args(argv)

    if not (args.web_dir / "app.js").exists():
        print(f"no app.js under {args.web_dir} — wrong --web-dir?",
              file=sys.stderr)
        return 2
    tabs = [t.strip() for t in args.tabs.split(",") if t.strip()]
    themes = [t.strip() for t in args.themes.split(",") if t.strip()]

    handler = _make_handler(args.web_dir, args.daemon)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.4)
    print(f"serving {args.web_dir} on :{args.port}, proxying /api -> "
          f"{args.daemon}")
    try:
        rc = _shoot(args.port, args.out, tabs, themes, args.settle_ms)
    finally:
        srv.shutdown()
    print(f"done -> {args.out}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
