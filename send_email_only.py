"""Send the MO Docs email for a resident whose MOR is already approved
and whose Combined PDF is already sitting on ResMan Documents.

Reuses helpers from run_mo_process.py so behavior stays in sync, but
hardens the Send step so the "silently already-closed dialog" failure
mode (see main runner log timestamps 15:18:44-15:18:45) can't happen
here: we verify #FromObject is present + a Send button is visible
before clicking, and we verify Communication Log shows the new row.
"""
import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import run_mo_process as mo


def robust_click_send(page):
    """Click Send only if the email dialog is actually open and a visible
    Send button is present. Raises RuntimeError if either check fails."""
    mo.log("Verifying email dialog is open before Send.")
    state = page.evaluate(
        r"""() => {
          const dlg = document.getElementById('FromObject');
          if (!dlg) return { ok: false, reason: 'email dialog is closed (#FromObject missing)' };
          const btns = Array.from(document.querySelectorAll('button')).filter(b => b.textContent.trim() === 'Send' && b.getBoundingClientRect().width > 0);
          if (btns.length === 0) return { ok: false, reason: 'no visible Send button' };
          btns[0].click();
          return { ok: true, sendButtons: btns.length };
        }"""
    )
    mo.log(f"Send click state: {state}")
    if not state.get("ok"):
        raise RuntimeError(f"Send preconditions failed: {state}")
    page.wait_for_function(
        r"""() => !document.getElementById('FromObject')""",
        timeout=45000,
    )
    mo.log("Email dialog closed after Send.")


def verify_via_comm_log(page, lease_url, subject_hint):
    """Open the resident detail page's Communication Log accordion, wait
    for the Kendo grid to lazy-hydrate (3-5s per prior debugging), and
    look for a row with today's subject."""
    mo.log("Verifying send via Communication Log.")
    # Deep-link opens the accordion for us.
    page.goto(lease_url + "?open=Communication%20Log", wait_until="domcontentloaded")
    page.wait_for_function(
        r"""() => document.body.innerText.includes('Communication Log')""",
        timeout=15000,
    )
    # Kendo grid lazy-hydrates; give it time.
    page.wait_for_timeout(5000)
    rows = page.evaluate(
        r"""(hint) => {
          const trs = Array.from(document.querySelectorAll('tr'));
          return trs
            .map(tr => tr.innerText.replace(/\s+/g,' ').trim())
            .filter(t => t.includes(hint) || (t.includes('Email') && t.includes(hint.split(' -')[0])))
            .slice(0, 5);
        }""",
        subject_hint,
    )
    mo.log(f"Comm Log matching rows: {rows}")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--payload", required=True, help="JSON, '@file', or '-' for stdin")
    ap.add_argument("--attachment", help="Merged PDF filename in ResMan Documents (default: 'Move Out Docs - Unit <#>.pdf')")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="Skip the final Send click")
    args = ap.parse_args()

    payload = mo.load_payload(args.payload)
    lease_url = payload["leaseUrl"]
    email_cfg = payload.get("email") or {}
    from_pref = email_cfg.get("from", "property")
    template  = email_cfg.get("template", mo.DEFAULT_TEMPLATE)

    started = time.time()
    result = {"status": None, "attachment": None, "preSend": None, "commLogRows": None, "error": None}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless, args=["--start-maximized"])
        if args.headless:
            context = browser.new_context(viewport={"width": 1600, "height": 1200})
        else:
            context = browser.new_context(no_viewport=True)
        page = context.new_page()

        try:
            mo.login(page)
            page.goto(lease_url, wait_until="domcontentloaded")
            page.wait_for_function(
                r"""() => !!document.querySelector('a[href^="mailto:"]')""",
                timeout=30000,
            )

            unit = mo.unit_number_from_page(page)
            attachment_name = args.attachment or f"Move Out Docs - Unit {unit}.pdf"
            result["attachment"] = attachment_name
            mo.log(f"Target attachment: {attachment_name}")

            mo.open_send_email_dialog(page)
            mo.set_from(page, from_pref)
            mo.apply_template(page, template)
            mo.set_from(page, from_pref)  # template can reset From
            attach_result = mo.attach_from_resman(page, [attachment_name])
            mo.log(f"Attach result: {attach_result}")

            # After attach, the email dialog might have been unexpectedly
            # closed by the retry loop clicking the wrong Cancel. Verify.
            still_open = page.evaluate(r"""() => !!document.getElementById('FromObject')""")
            if not still_open:
                raise RuntimeError("email dialog was closed after attach step; likely Cancel-click hit the outer dialog")

            mo.set_from(page, from_pref)  # attach may reset From

            pre_send = page.evaluate(
                r"""() => {
                  const subj = document.querySelector('input[name="Subject"], input#Subject')?.value || '';
                  const attachRows = Array.from(document.querySelectorAll('.document-name, .doc-name')).map(el => el.textContent.trim());
                  const fromInput = document.getElementById('FromObjectInput')?.value || '';
                  return { subj, attachRows, fromInput };
                }"""
            )
            mo.log(f"Pre-send state: {pre_send}")
            result["preSend"] = pre_send

            if args.dry_run:
                mo.log("--dry-run: skipping Send click.")
                result["status"] = "dry_run"
            else:
                robust_click_send(page)
                result["status"] = "clicked_send"

                # Give ResMan a couple seconds to persist the email, then
                # verify via Communication Log.
                page.wait_for_timeout(3000)
                subject_hint = pre_send.get("subj") or "Move-Out Documents"
                result["commLogRows"] = verify_via_comm_log(page, lease_url, subject_hint)
                if result["commLogRows"]:
                    result["status"] = "verified_in_comm_log"

        except Exception as e:
            mo.log(f"FATAL: {type(e).__name__}: {e}")
            result["error"] = f"{type(e).__name__}: {e}"
            result["status"] = "error"
        finally:
            context.close()
            browser.close()

    result["durationSeconds"] = int(time.time() - started)
    print(json.dumps(result, indent=2, default=str))
    sys.exit(1 if result["status"] == "error" else 0)


if __name__ == "__main__":
    main()
