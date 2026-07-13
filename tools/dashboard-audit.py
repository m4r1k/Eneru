#!/usr/bin/env python3
"""Screenshot and inventory a deployed Eneru dashboard without changing it.

This complements ``dashboard-preview.py``: preview renders working-tree assets
over a live API, while this tool checks the assets a deployment actually serves.
It visits every visible tab in light and dark themes, records dropdown options,
captures safe disclosures/detail dialogs, checks a mobile viewport, and writes a
machine-readable report containing console, HTTP, and accessibility findings.

Control commands, self-tests, variable writes, event deletion, config reload,
and shutdown actions are never clicked. Authentication is optional; when
``--username`` is supplied the password is read with ``getpass`` and is never
accepted as a command-line argument or written to the report.
"""

import argparse
import getpass
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_URL = "http://127.0.0.1:9191"
DEFAULT_OUT = Path("/tmp/eneru-dashboard-audit")


def parse_csv(value):
    """Return unique, non-empty comma-separated values in input order."""
    result = []
    for item in (value or "").split(","):
        item = item.strip()
        if item and item not in result:
            result.append(item)
    return result


def safe_slug(value):
    """Turn a UI label into a predictable, filesystem-safe screenshot name."""
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug or "item"


def normalize_url(value):
    """Accept only credential-free HTTP(S) dashboard URLs."""
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("dashboard URL must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("put credentials in --username/getpass, not in the URL")
    return value.rstrip("/")


def select_tabs(visible, requested):
    """Resolve requested tabs while refusing silent skips."""
    if requested is None:
        return list(visible)
    missing = [tab for tab in requested if tab not in visible]
    if missing:
        raise ValueError("requested tab(s) not visible: " + ", ".join(missing))
    return list(requested)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_URL,
                        help="deployed dashboard base URL")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="screenshot/report output directory")
    parser.add_argument("--tabs",
                        help="comma-separated tabs; default is every visible tab")
    parser.add_argument("--themes", default="light,dark",
                        help="comma-separated themes (light,dark,system)")
    parser.add_argument("--settle-ms", type=int, default=1200,
                        help="wait after initial load before inspection")
    parser.add_argument("--tab-settle-ms", type=int, default=650,
                        help="wait after each tab/view change")
    parser.add_argument("--username",
                        help="optional dashboard username; password is prompted")
    parser.add_argument("--mobile-width", type=int, default=390,
                        help="mobile Overview width; 0 disables mobile capture")
    parser.add_argument("--capture-scopes", action="store_true",
                        help="also capture Overview once per individual UPS view")
    return parser


def _visible_texts(locator):
    values = []
    for index in range(locator.count()):
        node = locator.nth(index)
        if node.is_visible():
            text = " ".join(node.inner_text().split())
            if text:
                values.append(text)
    return values


def _select_inventory(locator):
    """Describe visible selects without reading cookies or storage."""
    return locator.evaluate_all("""
        selects => selects.map(select => {
          const label = select.closest('label');
          const directText = label ? Array.from(label.childNodes)
            .filter(node => node.nodeType === Node.TEXT_NODE)
            .map(node => node.textContent.trim()).filter(Boolean).join(' ') : '';
          return {
            id: select.id || null,
            label: directText || select.getAttribute('aria-label') || null,
            selected: select.options[select.selectedIndex]
              ? select.options[select.selectedIndex].textContent.trim() : null,
            options: Array.from(select.options).map(option => ({
              value: option.value, text: option.textContent.trim(),
            })),
          };
        })
    """)


def _login(page, username):
    button = page.locator("#loginBtn")
    if not button.is_visible():
        raise RuntimeError("Sign in is not available on this dashboard")
    password = getpass.getpass(f"Eneru password for {username}: ")
    try:
        button.click()
        page.locator("#login-user").fill(username)
        page.locator("#login-pass").fill(password)
        page.locator("#login-form button[type=submit]").click()
        page.locator("#login-modal").wait_for(state="hidden", timeout=10000)
    finally:
        password = ""  # do not retain plaintext longer than the form submission


def _expand_visible_config_nodes(page):
    expanded = 0
    while True:
        collapsed = page.locator(
            "#config-body details.json-node:not([open]) > summary:visible")
        if collapsed.count() == 0:
            return expanded
        collapsed.first.click()
        expanded += 1
        if expanded > 500:
            raise RuntimeError("Config disclosure expansion did not converge")


def audit(args):
    """Run the browser audit and return the report plus process exit code."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "Playwright is missing; install .[dev] in the uv virtualenv")

    url = normalize_url(args.url)
    themes = parse_csv(args.themes)
    if not themes or any(theme not in ("light", "dark", "system") for theme in themes):
        raise ValueError("--themes must contain light, dark, or system")
    if args.settle_ms < 0 or args.tab_settle_ms < 0:
        raise ValueError("settle times cannot be negative")
    if args.mobile_width < 0:
        raise ValueError("--mobile-width cannot be negative")

    args.out.mkdir(parents=True, exist_ok=True)
    report = {
        "url": url,
        "footer": None,
        "visibleTabs": [],
        "tabs": {},
        "headerSelects": [],
        "details": [],
        "extras": {},
        "console": [],
        "httpErrors": [],
        "requestFailures": [],
        "accessibility": {},
    }

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1050})
        page.on("console", lambda message: report["console"].append({
            "type": message.type, "text": message.text[:300],
        }) if message.type in ("error", "warning") else None)

        def record_response(response):
            if response.status >= 400:
                report["httpErrors"].append({
                    "status": response.status,
                    "path": urlsplit(response.url).path,
                })

        page.on("response", record_response)
        page.on("requestfailed", lambda request: report["requestFailures"].append({
            "path": urlsplit(request.url).path,
            "failure": request.failure,
        }))

        started = time.monotonic()
        page.goto(url + "/#overview", wait_until="domcontentloaded")
        page.locator("#tabs [role=tab]").first.wait_for(state="visible")
        page.wait_for_timeout(args.settle_ms)
        if args.username:
            _login(page, args.username)
            page.wait_for_timeout(args.tab_settle_ms)

        visible_tabs = page.locator("#tabs [role=tab]").evaluate_all(
            "tabs => tabs.filter(tab => !tab.hidden && tab.offsetParent !== null)"
            ".map(tab => tab.dataset.tab)")
        requested = parse_csv(args.tabs) if args.tabs is not None else None
        tabs = select_tabs(visible_tabs, requested)
        report["visibleTabs"] = visible_tabs
        report["footer"] = page.locator("#status-line").inner_text().strip()
        report["initialRenderMs"] = round((time.monotonic() - started) * 1000)
        report["headerSelects"] = _select_inventory(
            page.locator("header select:visible"))

        for theme in themes:
            theme_select = page.locator("#theme-select")
            if theme_select.count():
                theme_select.select_option(theme)
            for tab in tabs:
                page.locator(f"#tab-{tab}").click()
                page.wait_for_timeout(args.tab_settle_ms)
                panel = page.locator(f"#panel-{tab}")
                report["tabs"].setdefault(tab, {})[theme] = {
                    "headings": _visible_texts(panel.locator("h2, h3")),
                    "cards": panel.locator(".card:visible").count(),
                    "graphs": panel.locator(".graph:visible").count(),
                    "selects": _select_inventory(panel.locator("select:visible")),
                }
                page.screenshot(
                    path=str(args.out / f"dash-{theme}-{safe_slug(tab)}.png"),
                    full_page=True)

        # Safe drill-downs: read-only UPS detail dialogs, Event type disclosure,
        # and the sanitized Config tree. Never click generic action buttons.
        if "overview" in tabs:
            page.locator("#tab-overview").click()
            rows = page.locator(".fleet-overview-row")
            for index in range(rows.count()):
                row_name = rows.nth(index).get_attribute("aria-label") or f"ups-{index + 1}"
                rows.nth(index).click()
                page.locator("#detail-modal").wait_for(state="visible")
                report["details"].append({
                    "row": row_name,
                    "title": page.locator("#detail-title").inner_text().strip(),
                    "focused": page.locator(":focus").get_attribute("id"),
                })
                page.screenshot(
                    path=str(args.out / f"detail-{index + 1}.png"), full_page=True)
                page.keyboard.press("Escape")
                page.locator("#detail-modal").wait_for(state="hidden")

        if "events" in tabs:
            page.locator("#tab-events").click()
            picker = page.locator("#event-type-summary")
            if picker.is_visible():
                picker.click()
                report["extras"]["eventTypes"] = page.locator(
                    "#event-type-filter .event-type-option").count()
                page.screenshot(path=str(args.out / "events-types.png"), full_page=True)
                picker.click()

        if "config" in tabs:
            page.locator("#tab-config").click()
            before = page.locator("#config-body details.json-node[open]").count()
            expanded = _expand_visible_config_nodes(page)
            report["extras"]["config"] = {
                "openBefore": before,
                "expanded": expanded,
                "openAfter": page.locator(
                    "#config-body details.json-node[open]").count(),
            }
            page.screenshot(path=str(args.out / "config-expanded.png"), full_page=True)

        if (args.capture_scopes and "overview" in tabs
                and page.locator("#global-ups").count()):
            scope_options = page.locator("#global-ups option").evaluate_all(
                "options => options.map(option => ({value: option.value, "
                "text: option.textContent.trim()}))")
            captures = []
            for option in scope_options:
                if option["value"] == "__all__":
                    continue
                page.locator("#global-ups").select_option(option["value"])
                page.locator("#tab-overview").click()
                page.wait_for_timeout(args.tab_settle_ms)
                name = safe_slug(option["text"])
                page.screenshot(path=str(args.out / f"scope-{name}.png"), full_page=True)
                captures.append(option["text"])
            report["extras"]["scopes"] = captures
            page.locator("#global-ups").select_option("__all__")

        if args.mobile_width and "overview" in tabs:
            page.locator("#tab-overview").click()
            page.set_viewport_size({"width": args.mobile_width, "height": 844})
            page.wait_for_timeout(args.tab_settle_ms)
            report["extras"]["mobile"] = page.locator(".tabs").evaluate(
                "node => ({clientWidth: node.clientWidth, scrollWidth: node.scrollWidth, "
                "bodyOverflow: document.documentElement.scrollWidth > "
                "document.documentElement.clientWidth})")
            page.screenshot(path=str(args.out / "mobile-overview.png"), full_page=True)

        page.set_viewport_size({"width": 1440, "height": 1050})
        if "overview" in tabs:
            page.locator("#tab-overview").click()
        unnamed = page.locator(
            "button:visible, select:visible, input:visible").evaluate_all("""
            nodes => nodes.filter(node => {
              const label = node.getAttribute('aria-label') || node.innerText ||
                (node.labels && Array.from(node.labels).map(item => item.innerText).join(' '));
              return !String(label || '').trim();
            }).map(node => node.id || node.outerHTML.slice(0, 120))
        """)
        report["accessibility"] = {
            "unnamedVisibleControls": unnamed,
            "selectedTab": page.locator(
                '[role="tab"][aria-selected="true"]').get_attribute("id"),
        }
        browser.close()

    report_path = args.out / "report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    issues = (report["console"] or report["httpErrors"]
              or report["requestFailures"]
              or report["accessibility"]["unnamedVisibleControls"])
    return report, 1 if issues else 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report, status = audit(args)
    except (RuntimeError, ValueError) as exc:
        print(f"dashboard audit failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "out": str(args.out),
        "footer": report["footer"],
        "tabs": list(report["tabs"]),
        "consoleIssues": len(report["console"]),
        "httpErrors": len(report["httpErrors"]),
        "requestFailures": len(report["requestFailures"]),
        "unnamedControls": len(
            report["accessibility"]["unnamedVisibleControls"]),
    }, indent=2))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
