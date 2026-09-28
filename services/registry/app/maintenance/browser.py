"""Deep-tier synthetic journeys in a real browser (ADR-0010 D10).

Deterministic Playwright probes of critical pages. HTTP status is not
enough: a page can answer 200 and still be unreadable, broken or lying.
Every check runs INSIDE the browser and returns only STRUCTURE:

    rule id, page, path (safe charset), selector class, count, numbers

Page text never leaves the browser and never reaches the Society (screenshots
and HTML are untrusted). Rules (``desired_state.json`` browser_experience):

* navigation: final path, redirect loop, status;
* console_error / js_exception / failed_request (same-origin, >= 400 or failed);
* dead_link: same-origin links that are placeholders (``#``, ``javascript:``)
  or answer >= 400;
* text_contrast: WCAG 2.2 AA (4.5:1 normal, 3:1 large) from computed styles
  against the effective background;
* form_label: form controls without an accessible name;
* raw_structured_value: visible text that is a Python/JSON dict or list
  rendering (``{'name': ...}``, ``[{"a": ...}]``);
* unexpected_error_banner: an alert/flash with an error phrase on a page that
  answered 200 as contracted;
* layout_overflow: horizontal overflow at the phone and desktop viewports;
* critical_content_missing: no visible ``h1`` or no ``nav``/``main`` landmark;
* keyboard_focus: Tab does not reach an interactive element;
* axe:<rule> -- axe-core (pinned) WCAG 2.x A/AA violations, when available;
* performance: document/total bytes, request count, third-party origins
  against ``performance_budgets`` (a single run never makes an incident: the
  kernel's confirmation needs repeated observations).

``visual_mode`` disables animations/transitions and emulates reduced motion
for stable comparisons (the Society's particles and live data would make
pixel diffs meaningless; production uses these SEMANTIC assertions instead).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urljoin, urlsplit

PROBE_VERSION = "browser/1"
SAFE_PATH = re.compile(r"^/[A-Za-z0-9._~/\-]{0,127}$")
SAFE_SELECTOR = re.compile(r"[^A-Za-z0-9_.\-#>: ]")
ERROR_PHRASES = ("page not found", "not found", "unexpected backend error", "internal server error", "api error", "something went wrong", "could not load")
VIEWPORTS = ({"width": 1280, "height": 900}, {"width": 390, "height": 844})
RULES = (
    "console_error", "js_exception", "failed_request", "dead_link", "redirect_loop", "text_contrast", "form_label", "raw_structured_value",
    "unexpected_error_banner", "layout_overflow", "critical_content_missing", "keyboard_focus", "performance", "navigation",
)


@dataclass
class Finding:
    rule: str
    count: int
    selectors: List[str] = field(default_factory=list)
    numbers: Dict[str, float] = field(default_factory=dict)
    paths: List[str] = field(default_factory=list)


@dataclass
class PageResult:
    page: str
    path: str
    status: Optional[int]
    final_path: Optional[str]
    findings: List[Finding] = field(default_factory=list)
    metrics: Dict[str, float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict:
        d = asdict(self)
        d["ok"] = self.ok
        return d


_IN_PAGE = r"""
(errorPhrases) => {
  const out = {};
  const vis = el => { const s = getComputedStyle(el); const r = el.getBoundingClientRect();
    return s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || '1') > 0.05 && r.width > 0 && r.height > 0; };
  const sel = el => { let s = el.tagName.toLowerCase(); if (el.classList && el.classList.length) s += '.' + Array.from(el.classList).slice(0, 2).join('.'); return s; };
  const parse = c => { const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return null; const p = m[1].split(',').map(x => parseFloat(x)); return {r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1}; };
  const lum = c => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b); };
  const blend = (top, bottom) => ({r: top.r * top.a + bottom.r * (1 - top.a), g: top.g * top.a + bottom.g * (1 - top.a), b: top.b * top.a + bottom.b * (1 - top.a), a: 1});
  const bgOf = el => { let layers = []; let e = el; while (e && e.nodeType === 1) { const s = getComputedStyle(e);
      if (s.backgroundImage && s.backgroundImage !== 'none') return null; const c = parse(s.backgroundColor); if (c && c.a > 0) { layers.push(c); if (c.a >= 1) break; } e = e.parentElement; }
    let base = {r: 255, g: 255, b: 255, a: 1}; for (let i = layers.length - 1; i >= 0; i--) base = blend(layers[i], base); return base; };
  // contrast + raw structured values over visible text-bearing elements
  const lowContrast = {}; let lowCount = 0, worst = 21; const raw = {}; let rawCount = 0;
  const rawRe = /(\{\s*['"][^'"]{1,40}['"]\s*:)|(\[\s*\{\s*['"])/;
  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  const seen = new Set();
  while (walker.nextNode()) { const t = walker.currentNode; const txt = (t.nodeValue || '').trim(); if (!txt) continue;
    const el = t.parentElement; if (!el || seen.has(el) || ['SCRIPT', 'STYLE', 'NOSCRIPT', 'CODE', 'PRE', 'TEXTAREA'].includes(el.tagName) || el.closest('code,pre,script,style')) continue;
    seen.add(el); if (!vis(el)) continue;
    if (rawRe.test(txt)) { rawCount++; raw[sel(el)] = (raw[sel(el)] || 0) + 1; }
    const s = getComputedStyle(el); const fg = parse(s.color); const bg = bgOf(el); if (!fg || !bg) continue;
    const c = blend(fg, bg); const L1 = lum(c), L2 = lum(bg); const ratio = (Math.max(L1, L2) + 0.05) / (Math.min(L1, L2) + 0.05);
    const px = parseFloat(s.fontSize); const bold = parseInt(s.fontWeight || '400') >= 700; const large = px >= 24 || (bold && px >= 18.66);
    if (ratio < (large ? 3 : 4.5)) { lowCount++; lowContrast[sel(el)] = (lowContrast[sel(el)] || 0) + 1; worst = Math.min(worst, ratio); } }
  out.text_contrast = {count: lowCount, selectors: lowContrast, worst: Math.round(worst * 100) / 100};
  out.raw_structured_value = {count: rawCount, selectors: raw};
  // unexpected error banners
  const banners = {}; let bannerCount = 0;
  document.querySelectorAll('.flash, .flash-message, .alert, [role=alert], .flashes li, .toast').forEach(el => {
    if (!vis(el)) return; const t = (el.innerText || '').toLowerCase(); if (errorPhrases.some(p => t.includes(p))) { bannerCount++; banners[sel(el)] = (banners[sel(el)] || 0) + 1; } });
  out.unexpected_error_banner = {count: bannerCount, selectors: banners};
  // form labels
  const unl = {}; let unlCount = 0;
  document.querySelectorAll('input, select, textarea').forEach(el => {
    const type = (el.getAttribute('type') || '').toLowerCase(); if (['hidden', 'submit', 'button', 'reset', 'image'].includes(type) || !vis(el)) return;
    const id = el.getAttribute('id'); const named = el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || el.getAttribute('title') || el.closest('label') || (id && document.querySelector('label[for="' + CSS.escape(id) + '"]'));
    if (!named) { unlCount++; unl[sel(el)] = (unl[sel(el)] || 0) + 1; } });
  out.form_label = {count: unlCount, selectors: unl};
  // links
  const placeholder = {}; let phCount = 0; const hrefs = [];
  document.querySelectorAll('a').forEach(a => { if (!vis(a)) return; const raw = (a.getAttribute('href') || '').trim();
    if (raw === '' || raw === '#' || raw.toLowerCase().startsWith('javascript:')) { if (a.getAttribute('role') === 'button' || a.hasAttribute('data-toggle') || a.hasAttribute('onclick')) return; phCount++; placeholder[sel(a)] = (placeholder[sel(a)] || 0) + 1; }
    else { try { const u = new URL(raw, location.href); if (u.origin === location.origin) hrefs.push(u.pathname); } catch (e) {} } });
  out.placeholder_links = {count: phCount, selectors: placeholder};
  out.hrefs = Array.from(new Set(hrefs)).slice(0, 40);
  // critical content
  const h1 = Array.from(document.querySelectorAll('h1')).some(vis);
  const landmark = !!document.querySelector('nav, main, [role=navigation], [role=main]');
  out.critical_content_missing = {count: (h1 ? 0 : 1) + (landmark ? 0 : 1), h1: h1, landmark: landmark};
  out.overflow = document.documentElement.scrollWidth - document.documentElement.clientWidth;
  return out;
}
"""

_VISUAL_CSS = "*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important} canvas{visibility:hidden!important}"


def _clean_selectors(d: Dict[str, int], limit: int = 5) -> List[str]:
    items = sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [SAFE_SELECTOR.sub("", k)[:80] for k, _ in items]


def probe_page(context, base_url: str, name: str, path: str, *, axe_source: Optional[str] = None, visual_mode: bool = True, timeout_ms: int = 20000,
               budgets: Optional[Dict[str, int]] = None, expected_final_path: Optional[str] = None, check_links: bool = True) -> PageResult:
    page = context.new_page()
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(base_url))
    console_errors, exceptions, failed = [], [], []
    sizes = {"total": 0, "requests": 0}
    third = set()
    page.on("console", lambda m: console_errors.append(1) if m.type == "error" else None)
    page.on("pageerror", lambda e: exceptions.append(1))
    page.on("requestfailed", lambda r: failed.append(urlsplit(r.url).path) if r.url.startswith(origin) else None)

    def on_response(r):
        sizes["requests"] += 1
        try:
            sizes["total"] += int(r.headers.get("content-length") or 0)
        except ValueError:
            pass
        if r.url.startswith(origin):
            if r.status >= 400 and r.request.resource_type != "document":
                failed.append(urlsplit(r.url).path)
        elif r.url.startswith("http"):
            third.add(urlsplit(r.url).netloc)

    page.on("response", on_response)
    findings: List[Finding] = []
    status = final_path = None
    try:
        resp = page.goto(urljoin(base_url, path), wait_until="load", timeout=timeout_ms)
        status = resp.status if resp else None
        final_path = urlsplit(page.url).path
        doc_bytes = len(resp.body()) if resp else 0
        if visual_mode:
            page.add_style_tag(content=_VISUAL_CSS)
        chain = 0
        req = resp.request if resp else None
        while req is not None and req.redirected_from is not None:
            chain += 1
            req = req.redirected_from
        if chain >= 5:
            findings.append(Finding("redirect_loop", 1, numbers={"hops": chain}))
        if expected_final_path and final_path != expected_final_path:
            findings.append(Finding("navigation", 1, paths=[p for p in [final_path] if p and SAFE_PATH.match(p)], numbers={"status": float(status or 0)}))
        results = []
        for vp in VIEWPORTS:
            page.set_viewport_size(vp)
            results.append(page.evaluate(_IN_PAGE, list(ERROR_PHRASES)))
        r0 = results[0]
        for rule in ("text_contrast", "raw_structured_value", "form_label", "critical_content_missing"):
            c = int(r0[rule]["count"])
            if c:
                nums = {"worst_ratio": r0[rule]["worst"]} if rule == "text_contrast" else {}
                findings.append(Finding(rule, c, _clean_selectors(r0[rule].get("selectors") or {}), nums))
        if status == 200 and int(r0["unexpected_error_banner"]["count"]):
            findings.append(Finding("unexpected_error_banner", int(r0["unexpected_error_banner"]["count"]), _clean_selectors(r0["unexpected_error_banner"]["selectors"])))
        overflow = [vp["width"] for vp, r in zip(VIEWPORTS, results) if int(r["overflow"]) > 2]
        if overflow:
            findings.append(Finding("layout_overflow", len(overflow), numbers={"viewport_width": float(min(overflow))}))
        dead_paths: List[str] = []
        if check_links:
            for href in r0["hrefs"][:25]:
                try:
                    lr = context.request.get(urljoin(origin, href), max_redirects=5, timeout=timeout_ms)
                    if lr.status >= 400:
                        dead_paths.append(href)
                except Exception:  # noqa: BLE001 -- an unreachable link is a dead link
                    dead_paths.append(href)
        dead = int(r0["placeholder_links"]["count"]) + len(dead_paths)
        if dead:
            findings.append(Finding("dead_link", dead, _clean_selectors(r0["placeholder_links"]["selectors"]), paths=[p for p in dead_paths if SAFE_PATH.match(p)][:10]))
        page.set_viewport_size(VIEWPORTS[0])
        page.keyboard.press("Tab")
        page.keyboard.press("Tab")
        focus_ok = page.evaluate("() => { const a = document.activeElement; return !!a && a !== document.body && a.tagName !== 'HTML'; }")
        interactive = page.evaluate("() => document.querySelectorAll('a[href], button, input, select, textarea').length")
        if interactive and not focus_ok:
            findings.append(Finding("keyboard_focus", 1))
        if axe_source:
            page.add_script_tag(content=axe_source)
            res = page.evaluate("async () => { const r = await axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a','wcag2aa','wcag21a','wcag21aa','wcag22aa']}, resultTypes: ['violations']}); return r.violations.map(v => ({id: v.id, impact: v.impact, nodes: v.nodes.length})); }")
            for v in res:
                findings.append(Finding(f"axe:{v['id']}"[:60], int(v["nodes"]), numbers={"serious": 1.0 if v.get("impact") in ("serious", "critical") else 0.0}))
        if console_errors:
            findings.append(Finding("console_error", len(console_errors)))
        if exceptions:
            findings.append(Finding("js_exception", len(exceptions)))
        if failed:
            findings.append(Finding("failed_request", len(failed), paths=sorted({p for p in failed if SAFE_PATH.match(p)})[:10]))
        metrics = {"document_bytes": float(doc_bytes), "total_bytes": float(sizes["total"] + doc_bytes), "requests": float(sizes["requests"]), "third_party_origins": float(len(third))}
        if budgets:
            over = {k: metrics[k] for k in ("document_bytes", "total_bytes", "requests", "third_party_origins") if k in budgets and metrics[k] > budgets[k]}
            if over:
                findings.append(Finding("performance", len(over), numbers=over))
        return PageResult(name, path, status, final_path if (final_path and SAFE_PATH.match(final_path)) else None, findings, metrics)
    except Exception as exc:  # noqa: BLE001 -- a probe failure is itself structural evidence
        return PageResult(name, path, status, final_path, [Finding("navigation", 1, numbers={"error": 1.0}, selectors=[type(exc).__name__[:40]])])
    finally:
        page.close()


def run_journeys(base_url: str, pages: Sequence[Dict[str, Any]], *, axe_source: Optional[str] = None, visual_mode: bool = True, timeout_ms: int = 20000,
                 budgets: Optional[Dict[str, int]] = None, check_links: bool = True) -> List[PageResult]:
    """Probe each page in a fresh, credential-less browser context."""
    from playwright.sync_api import sync_playwright  # noqa: PLC0415 -- optional dependency (browser tier only)

    out: List[PageResult] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context(reduced_motion="reduce" if visual_mode else "no-preference", user_agent="AgentNet-BrowserProbe/1.0 (+https://agentnet.io.vn)", java_script_enabled=True)
            for pg in pages:
                out.append(probe_page(context, base_url, pg["name"], pg["path"], axe_source=axe_source, visual_mode=visual_mode, timeout_ms=timeout_ms, budgets=budgets,
                                      expected_final_path=pg.get("expected_final_path"), check_links=check_links))
            context.close()
        finally:
            browser.close()
    return out


#: Browser rule -> (incident class, base priority)
RULE_CLASS = {
    "text_contrast": ("ACCESSIBILITY", "P2"),
    "form_label": ("ACCESSIBILITY", "P2"),
    "keyboard_focus": ("ACCESSIBILITY", "P2"),
    "raw_structured_value": ("UI_RENDERING", "P2"),
    "unexpected_error_banner": ("UI_RENDERING", "P2"),
    "critical_content_missing": ("UI_RENDERING", "P2"),
    "layout_overflow": ("UI_RENDERING", "P3"),
    "console_error": ("UI_RENDERING", "P3"),
    "js_exception": ("UI_RENDERING", "P2"),
    "failed_request": ("UI_RENDERING", "P3"),
    "dead_link": ("UI_NAVIGATION", "P2"),
    "redirect_loop": ("UI_NAVIGATION", "P1"),
    "navigation": ("UI_NAVIGATION", "P2"),
    "performance": ("PERFORMANCE", "P3"),
}


def rule_class(rule: str):
    if rule.startswith("axe:"):
        return RULE_CLASS["text_contrast"]
    return RULE_CLASS.get(rule, ("UI_RENDERING", "P3"))


__all__ = ["Finding", "PageResult", "probe_page", "run_journeys", "RULES", "RULE_CLASS", "rule_class", "PROBE_VERSION", "ERROR_PHRASES"]
