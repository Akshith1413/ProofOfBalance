#!/usr/bin/env python3
"""Capture README screenshots with a headless browser.

  pip install playwright   # uses an installed Chrome; no browser download needed
  scripts/dev/screenshots.py page  URL OUT.png [--text "Ledger gate: FAILED"] [--width 1280] [--wait-ms 2000]
  scripts/dev/screenshots.py term  INPUT.txt OUT.png --title "kubectl argo rollouts get rollout pob-worker"

page: with --text, crops to the smallest block (comment, panel, card) containing that text.
term: renders real command output (ANSI colours kept) as a terminal-style image.
"""
from __future__ import annotations

import argparse
import html
import os
import re
import shutil
import sys

from playwright.sync_api import sync_playwright

CHROME_CANDIDATES = [os.environ.get("CHROME", ""), "/opt/google/chrome/chrome", shutil.which("google-chrome") or "",
                     shutil.which("chromium") or "", shutil.which("chromium-browser") or ""]

ANSI = {"30": "#6e7681", "31": "#ff7b72", "32": "#3fb950", "33": "#d29922", "34": "#58a6ff", "35": "#bc8cff",
        "36": "#39c5cf", "37": "#c9d1d9", "90": "#8b949e", "91": "#ffa198", "92": "#56d364", "93": "#e3b341",
        "94": "#79c0ff", "95": "#d2a8ff", "96": "#56d4dd", "97": "#f0f6fc"}

# Climb from the matched text to the nearest block that looks like a self-contained card.
CROP_JS = """
(text) => {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    if (!node.textContent.includes(text)) continue;
    let el = node.parentElement;
    while (el && el !== document.body) {
      const cls = (el.className && el.className.toString()) || "";
      if (el.id && el.id.startsWith("issuecomment-")) return el.id;
      if (/timeline-comment|react-panel-container|TimelineItem|panel-container|Box-sc/.test(cls) &&
          el.getBoundingClientRect().height > 120) {
        el.setAttribute("data-shot", "1");
        return "[data-shot]";
      }
      el = el.parentElement;
    }
  }
  return null;
}
"""


def chrome() -> str:
    for c in CHROME_CANDIDATES:
        if c and os.path.exists(c):
            return c
    sys.exit("no Chrome/Chromium found; set CHROME=/path/to/chrome")


def ansi_to_html(text: str) -> str:
    out, open_span = [], False
    for part in re.split(r"(\x1b\[[0-9;]*m)", text):
        m = re.fullmatch(r"\x1b\[([0-9;]*)m", part)
        if not m:
            out.append(html.escape(part))
            continue
        if open_span:
            out.append("</span>")
            open_span = False
        codes = [c for c in m.group(1).split(";") if c]
        style = []
        for c in codes:
            if c in ANSI:
                style.append(f"color:{ANSI[c]}")
            elif c == "1":
                style.append("font-weight:700")
        if style:
            out.append(f'<span style="{";".join(style)}">')
            open_span = True
    if open_span:
        out.append("</span>")
    return "".join(out)


def term(args, browser) -> None:
    with open(args.input, encoding="utf-8", errors="replace") as fh:
        body = ansi_to_html(fh.read().rstrip())
    page = browser.new_page(viewport={"width": args.width, "height": 400}, device_scale_factor=2)
    page.set_content(f"""<!doctype html><meta charset="utf-8"><style>
      body{{margin:0;background:#0d1117;padding:24px;font:14px/1.45 'JetBrains Mono','DejaVu Sans Mono',monospace}}
      .win{{background:#161b22;border:1px solid #30363d;border-radius:10px;overflow:hidden;display:inline-block;min-width:100%;box-sizing:border-box}}
      .bar{{padding:10px 14px;border-bottom:1px solid #30363d;color:#8b949e}}
      .dot{{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:6px;vertical-align:-1px}}
      pre{{margin:0;padding:14px 18px;color:#c9d1d9;white-space:pre}}</style>
      <div class="win"><div class="bar"><span class="dot" style="background:#ff5f56"></span><span class="dot" style="background:#ffbd2e"></span><span class="dot" style="background:#27c93f"></span>
      &nbsp;$ {html.escape(args.title)}</div><pre>{body}</pre></div>""")
    page.locator(".win").screenshot(path=args.out)


def shot(args, browser) -> None:
    page = browser.new_page(viewport={"width": args.width, "height": args.height}, device_scale_factor=2,
                            color_scheme="light")
    page.goto(args.url, wait_until="networkidle", timeout=120_000)
    page.wait_for_timeout(args.wait_ms)
    if args.text:
        target = page.evaluate(CROP_JS, args.text)
        if not target:
            sys.exit(f"text not found on page: {args.text!r}")
        loc = page.locator(f"#{target}" if not target.startswith("[") else target).first
        loc.scroll_into_view_if_needed()
        loc.screenshot(path=args.out)
    else:
        page.screenshot(path=args.out, full_page=args.full)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("page")
    p.add_argument("url"); p.add_argument("out")
    p.add_argument("--text"); p.add_argument("--width", type=int, default=1280); p.add_argument("--height", type=int, default=900)
    p.add_argument("--wait-ms", type=int, default=2000); p.add_argument("--full", action="store_true")
    t = sub.add_parser("term")
    t.add_argument("input"); t.add_argument("out"); t.add_argument("--title", default="")
    t.add_argument("--width", type=int, default=1100)
    args = ap.parse_args()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chrome(), headless=True)
        try:
            (term if args.cmd == "term" else shot)(args, browser)
        finally:
            browser.close()
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
