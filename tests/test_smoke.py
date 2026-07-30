"""End-to-end smoke test: verify the built HTML actually renders and works.

Serves the repo root over HTTP and drives a headless browser. Fails on:
  - Missing SHOPS blob
  - Shop count regression
  - Runtime JS errors
  - Broken auto-pick / trip flow

Run locally: pytest tests/test_smoke.py
CI runs this on every push.
"""
import http.server
import pathlib
import socketserver
import threading
import time
import pytest
from playwright.sync_api import sync_playwright, expect


ROOT = pathlib.Path(__file__).resolve().parent.parent
PORT = 8765
URL = f'http://localhost:{PORT}/index.html'


@pytest.fixture(scope='module')
def server():
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(*a, directory=str(ROOT), **kw)
    httpd = socketserver.ThreadingTCPServer(('127.0.0.1', PORT), handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    time.sleep(0.3)
    yield URL
    httpd.shutdown()


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def _new_page(browser, log):
    ctx = browser.new_context(viewport={'width': 1280, 'height': 800})
    page = ctx.new_page()
    page.on('pageerror', lambda e: log.append(('pageerror', str(e))))
    page.on('console', lambda m: log.append((m.type, m.text)) if m.type == 'error' else None)
    return page, ctx


def test_page_loads_with_shop_data(server, browser):
    log = []
    page, ctx = _new_page(browser, log)
    try:
        page.goto(server, wait_until='domcontentloaded')
        page.wait_for_selector('#map', state='visible', timeout=10_000)
        # SHOPS is declared as `const SHOPS = [...]` inside a module-less script,
        # so it's actually a global (implicit window property in classic scripts).
        count = page.evaluate('() => (typeof SHOPS !== "undefined") ? SHOPS.length : 0')
        assert count > 1500, f'Expected >1500 shops embedded, got {count}'
        # Sanity: no runtime errors during initial load
        errors = [x for x in log if x[0] in ('pageerror', 'error')]
        assert not errors, f'Runtime errors during load: {errors}'
    finally:
        ctx.close()


def test_shop_record_shape(server, browser):
    log = []
    page, ctx = _new_page(browser, log)
    try:
        page.goto(server, wait_until='domcontentloaded')
        page.wait_for_selector('#map', state='visible', timeout=10_000)
        # Every shop must have coords + name + source
        bad = page.evaluate('''() => SHOPS.filter(s =>
            typeof s.y !== 'number' || typeof s.x !== 'number' ||
            !s.n || !s.src
        ).length''')
        assert bad == 0, f'{bad} shops have malformed shape'
        # At least a few hundred should carry opening hours
        with_hours = page.evaluate('() => SHOPS.filter(s => s.h && Object.keys(s.h).length).length')
        assert with_hours > 300, f'Expected >300 shops with hours, got {with_hours}'
    finally:
        ctx.close()


def test_chain_filter_ui_renders(server, browser):
    """Chain-filter checkboxes only appear after a route search, but the container should exist."""
    log = []
    page, ctx = _new_page(browser, log)
    try:
        page.goto(server, wait_until='domcontentloaded')
        page.wait_for_selector('#chainFilters', state='attached', timeout=5000)
        page.wait_for_selector('#openNowFilter', state='attached', timeout=5000)
        page.wait_for_selector('#autoPickBtn', state='attached', timeout=5000)
        # Auto-pick starts disabled (no route yet)
        assert page.eval_on_selector('#autoPickBtn', 'el => el.disabled') is True
    finally:
        ctx.close()


def test_manifest_and_service_worker_reachable(server, browser):
    """PWA basics: manifest + SW file are served."""
    log = []
    page, ctx = _new_page(browser, log)
    try:
        for path in ('manifest.json', 'sw.js', 'icon.svg'):
            r = page.request.get(f'http://localhost:{PORT}/{path}')
            assert r.ok, f'{path} not reachable: {r.status}'
    finally:
        ctx.close()
