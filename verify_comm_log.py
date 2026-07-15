"""Verify whether a resident email actually sent by dumping ResMan's
Communication Log rows for the resident. Opens headed browser, logs
in, clicks the Communication Log accordion, waits long enough for the
Kendo grid to hydrate, and prints the top rows verbatim.
"""
import argparse
import json
import sys
import time

from playwright.sync_api import sync_playwright

import run_mo_process as mo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lease-url", required=True)
    ap.add_argument("--wait-seconds", type=int, default=10)
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    result = {"leaseUrl": args.lease_url, "rows": [], "accordionOpened": False, "error": None}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless, args=["--start-maximized"])
        if args.headless:
            context = browser.new_context(viewport={"width": 1600, "height": 1200})
        else:
            context = browser.new_context(no_viewport=True)
        page = context.new_page()

        try:
            mo.login(page)
            page.goto(args.lease_url, wait_until="domcontentloaded")
            page.wait_for_function(
                r"""() => !!document.querySelector('a[href^="mailto:"]')""",
                timeout=30000,
            )
            mo.log("Resident detail loaded. Locating Communication Log accordion.")

            opened = page.evaluate(
                r"""() => {
                  const hdrs = Array.from(document.querySelectorAll('h3, .accordion-header, .k-header, button, a'));
                  const hdr = hdrs.find(h => (h.textContent || '').trim().startsWith('Communication Log'));
                  if (!hdr) return { ok: false, reason: 'no Communication Log header found' };
                  hdr.scrollIntoView({ block: 'center' });
                  hdr.click();
                  return { ok: true, tag: hdr.tagName, text: (hdr.textContent || '').trim().slice(0, 60) };
                }"""
            )
            mo.log(f"Accordion click: {opened}")
            result["accordionOpened"] = bool(opened.get("ok"))

            mo.log(f"Waiting {args.wait_seconds}s for Kendo grid to hydrate.")
            page.wait_for_timeout(args.wait_seconds * 1000)

            rows = page.evaluate(
                r"""() => {
                  const out = [];
                  const trs = Array.from(document.querySelectorAll('tr'));
                  for (const tr of trs) {
                    const t = (tr.innerText || '').replace(/\s+/g, ' ').trim();
                    if (!t) continue;
                    if (t.length < 8) continue;
                    out.push(t);
                    if (out.length >= 30) break;
                  }
                  return out;
                }"""
            )
            result["rows"] = rows
            mo.log(f"Collected {len(rows)} table rows.")
        except Exception as e:
            mo.log(f"FATAL: {type(e).__name__}: {e}")
            result["error"] = f"{type(e).__name__}: {e}"
        finally:
            context.close()
            browser.close()

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
