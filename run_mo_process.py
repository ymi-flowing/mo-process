"""
MO Process - SNS Multi Family Management LLC / ResMan

Runs the full Move-Out / Final Account Statement workflow for a former resident:
  1. Login to ResMan.
  2. Open Move Out Reconciliation from the resident's Leasing Workflow.
  3. Fill Move-out rec. date (defaults to today).
  4. Add each charge in the payload (default category: "Cleaning/Damage Charges").
  5. Capture MOR totals (open balance, MO charges, balance owed, deposit, etc.).
  6. Approve the reconciliation.
  7. Read the resident's forwarding address; fall back to the unit's address if empty.
  8. Generate a filled Claim Form docx from Claim Form Example.Docx.
  9. Download the ResMan-generated Final Account Statement PDF.
 10. Upload the Claim Form docx back to the resident's Documents tab.
 11. Open the resident email, apply the ***MO Docs Email template, attach both docs
     from ResMan, set From to the property, and (unless --no-send) click Send.

USAGE
    python run_mo_process.py --payload @payload.json
    python run_mo_process.py --payload -                # read JSON from stdin
    python run_mo_process.py --payload @payload.json --no-send
    python run_mo_process.py --payload @payload.json --headless

PAYLOAD (single object)
    {
      "leaseUrl": "https://sns.myresman.com/#/Residents/Detail/<leaseId>",
      "charges": [
        { "description": "Cleaning",        "amount": 150.00 },
        { "description": "Carpet Cleaning", "amount": 200.00 }
      ],
      "morDate": "7/13/2026",             // optional; defaults to today (M/D/YYYY)
      "email": {
        "enabled": true,                  // default true
        "from": "property",               // "property" or "assistant"; default property
        "template": "***MO Docs Email"    // default "***MO Docs Email"
      },
      "outputDir": "out"                  // optional; defaults to CWD
    }

RESULT
    A single JSON object is ALWAYS printed to stdout at the end (both on success
    and on error). Downstream (n8n, GitHub Actions) can rely on this shape:

    {
      "status": "sent" | "sent_no_email" | "parked" | "error",
      "startedAt": "2026-07-13T13:58:00Z",
      "endedAt":   "2026-07-13T14:02:11Z",
      "durationSeconds": 251,
      "resident": {
        "name": "Dwaun Spigner",
        "unit": "317",
        "property": "49th St Apartments",
        "leaseUrl": "https://sns.myresman.com/#/Residents/Detail/...",
        "email": "dwaun803@gmail.com"
      },
      "mor": {
        "date": "7/13/2026",
        "status": "Complete",
        "charges":  [ {"category":"Cleaning/Damage Charges","description":"...","amount":150} ],
        "totals":   { "currentOpenBalanceTotal": "3,257.90", ... , "balanceOwed": "2,708.90" },
        "forwardingAddress": { "street":"...", "city":"...", "state":"FL", "zip":"33781" },
        "forwardingSource":  "resident" | "unit"
      },
      "docs": {
        "claimForm":   "out/Claim Form - Dwaun Spigner.docx",
        "fasPdf":      "out/Final Account Statement 7-13-26 - Dwaun Spigner.pdf",
        "combinedPdf": "out/Combined - Dwaun Spigner.pdf"
      },
      "email": {
        "attempted": true, "sent": true, "to": "dwaun803@gmail.com",
        "from": "property", "template": "***MO Docs Email",
        "subject": "49th St Apartments - Move-Out Documents",
        "attachedByResMan": [ {"name":"...","checked":true} ]
      },
      "docupost": null,
      "github": { "repo": "ymi-flowing/mo-process", "runUrl": null },
      "logs": [ "13:58:00 Login user: 'SNS_Assistant'", ... ],
      "error": null   // populated on failure with { "message": "...", "type": "..." }
    }
"""
import argparse
import base64
import json
import os
import re
import sys
import shutil
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout


# -------- Credentials (env overrides; defaults match SNS_Assistant) ---------
USERNAME = os.environ.get("RESMAN_USER") or "SNS_Assistant"
PASSWORD = os.environ.get("RESMAN_PASS") or "SNSassistant123$"
LOGIN_URL = "https://sns.myresman.com/"

DEFAULT_CATEGORY = "Cleaning/Damage Charges"
DEFAULT_TEMPLATE = "***MO Docs Email"

HERE = Path(__file__).parent.resolve()
CLAIM_TEMPLATE = HERE / "Claim Form Example.Docx"


# ------------------------------ Utilities ----------------------------------

_LOGS: list[str] = []


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    _LOGS.append(line)
    print(line, flush=True, file=sys.stderr)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_payload(arg):
    if arg == "-":
        raw = sys.stdin.read()
    elif arg.startswith("@"):
        raw = Path(arg[1:]).read_text(encoding="utf-8")
    else:
        raw = arg
    return json.loads(raw)


def today_str():
    n = datetime.now()
    return f"{n.month}/{n.day}/{n.year}"


def money(x):
    """Format a Decimal/float as ResMan-style '4,738.00'."""
    return f"{float(x):,.2f}"


def parse_money(s):
    if s is None:
        return 0.0
    return float(str(s).replace(",", "").strip())


def safe_slug(name):
    return re.sub(r"[^\w\-. ]+", "_", name).strip()


# ------------------------------ ResMan login -------------------------------

def login(page: Page):
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    log(f"Login user: {USERNAME!r}")
    try:
        page.wait_for_selector('input[name="Username"]', timeout=15000)
    except PWTimeout:
        if "myresman.com" in page.url and "Account/Login" not in page.url:
            log("Already authenticated.")
            return
        raise
    page.fill('input[name="Username"]', USERNAME)
    page.fill('input[name="Password"]', PASSWORD)
    page.click('button:has-text("Sign in")')
    page.wait_for_function(
        r"""() => /^https:\/\/sns\.myresman\.com\/#\//.test(location.href)
                  && !/Account\/Login/i.test(location.href)""",
        timeout=45000,
    )


# ------------------------------ MOR steps ----------------------------------

def open_move_out_rec(page: Page, lease_url: str) -> dict:
    """Navigate to the resident detail and click the visible Move Out Rec. link."""
    page.goto(lease_url, wait_until="domcontentloaded")
    # Wait for the sidebar's Move Out Rec. anchor to render.
    page.wait_for_function(
        "() => !!document.querySelector('#MoveOutReconciliationLink')",
        timeout=30000,
    )
    log("Resident detail loaded; clicking Move Out Rec.")
    info = page.evaluate(
        r"""() => {
          const a = document.querySelector('#MoveOutReconciliationLink');
          window.jQuery(a).trigger('click');
          return { dataHref: a.getAttribute('data-href') };
        }"""
    )
    # Wait for the MOR page to render its Move-out rec. date input.
    page.wait_for_function(
        "() => !!document.getElementById('MoveOutReconciliationDate')",
        timeout=30000,
    )
    return info


def fill_mor_date(page: Page, mor_date: str):
    log(f"Setting Move-out rec. date = {mor_date}")
    page.evaluate(
        r"""(d) => {
          const el = document.getElementById('MoveOutReconciliationDate');
          window.jQuery(el).val(d).trigger('change').trigger('blur');
        }""",
        mor_date,
    )


def add_charge(page: Page, description: str, amount: float, category: str = DEFAULT_CATEGORY):
    """Click Add Charge / Credit, pick Category, fill Description + Amount."""
    log(f"Adding charge: {description} = ${amount} ({category})")

    row_ids_before = page.evaluate(
        r"""() => Array.from(document.querySelectorAll('input[name="MoveOutCharges.index"]')).map(i => i.value)"""
    )

    page.locator('button:has-text("Add Charge / Credit")').click()

    # Wait for a new row to appear.
    page.wait_for_function(
        r"""(before) => {
          const now = Array.from(document.querySelectorAll('input[name="MoveOutCharges.index"]')).map(i => i.value);
          return now.length > before.length;
        }""",
        arg=row_ids_before,
        timeout=15000,
    )
    row_id = page.evaluate(
        r"""(before) => {
          const now = Array.from(document.querySelectorAll('input[name="MoveOutCharges.index"]')).map(i => i.value);
          return now.find(v => !before.includes(v));
        }""",
        row_ids_before,
    )

    # Open the row's Category dropdown (2nd button on the row).
    page.evaluate(
        r"""(rowId) => {
          const trs = Array.from(document.querySelectorAll('tr')).filter(tr => tr.innerHTML.includes(rowId));
          const btns = Array.from(trs[0].querySelectorAll('button'));
          btns[1].click();
        }""",
        row_id,
    )

    # Pick the visible Category menu item.
    page.locator(f'[role="menuitem"]:visible:has-text("{category}")').first.click()

    # Fill Description + Amount.
    page.evaluate(
        r"""([rowId, desc, amt]) => {
          const desc_el = document.querySelector(`input[name="MoveOutCharges[${rowId}].Description"]`);
          const amt_el  = document.querySelector(`input[name="MoveOutCharges[${rowId}].ChargeAmount"]`);
          window.jQuery(desc_el).val(desc).trigger('change');
          window.jQuery(amt_el).val(amt).trigger('change').trigger('blur');
        }""",
        [row_id, description, f"{float(amount):.2f}"],
    )
    return row_id


def capture_mor_totals(page: Page) -> dict:
    """Read totals + deposit info from the MOR page before clicking Approve."""
    return page.evaluate(
        r"""() => {
          const grab = (id) => document.getElementById(id)?.value || null;
          const cells = Array.from(document.querySelectorAll('td, th'));
          const total = (label) => {
            const cell = cells.find(c => c.textContent.trim() === label);
            if (!cell) return null;
            const vals = Array.from(cell.parentElement.querySelectorAll('td')).map(td => td.textContent.trim()).filter(Boolean);
            return vals[vals.length - 1];
          };
          return {
            currentOpenBalanceTotal:         total('Current Open Balance Total'),
            finalMoveOutChargesCreditsTotal: total('Final Move Out Charges / Credits Total'),
            balanceBeforeDepositsTotal:      total('Balance before Deposits Total'),
            availableDepositApplied:         document.querySelector('input[name*="ApplyToBalanceAmount"]')?.value || null,
            paymentCreditRefund:             grab('PaymentRefundAmount'),
            depositRefund:                   grab('CalculatedDepositRefundAmount'),
            balanceOwed:                     grab('BalanceOwed'),
          };
        }"""
    )


def approve_mor(page: Page):
    log("Actions -> Approve")
    page.locator('#Actions').click()
    page.locator('#Approve').click()
    # Approve redirects to /#/Residents/RedirectToDetail?ulgid=... and eventually
    # to the resident detail page. Wait for the Leasing Workflow to show Complete.
    page.wait_for_function(
        r"""() => document.body.innerText.includes('Move Out Rec (Complete)')""",
        timeout=45000,
    )
    log("MOR approved.")


# --------------------------- Forwarding address ----------------------------

def get_forwarding_address(page: Page) -> dict | None:
    """Read forwarding address from the resident's Vacating Information block."""
    raw = page.evaluate(
        r"""() => {
          const label = Array.from(document.querySelectorAll('label')).find(l => l.textContent.trim().startsWith('Forwarding address'));
          if (!label) return null;
          const cell = label.closest('td');
          const val = cell?.querySelector('.fv');
          return val ? val.textContent.trim() : null;
        }"""
    )
    if not raw:
        return None
    return parse_address_lines(raw)


def parse_address_lines(text: str) -> dict:
    """Parse a multi-line address into street/city/state/zip/county."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return {}
    street = lines[0]
    unit_no = None
    m = re.search(r"\b(?:unit|apt|apartment|#)\s*([A-Za-z0-9\-]+)", street, re.I)
    if m:
        unit_no = m.group(1)

    # Try to find "City, ST ZIP" line.
    city, state, zipcode = None, None, None
    csz_rx = re.compile(r"^(.+?),\s*([A-Z]{2})\s*(\d{5}(?:-\d{4})?)$")
    for ln in lines[1:]:
        m = csz_rx.match(ln)
        if m:
            city, state, zipcode = m.group(1).strip(), m.group(2), m.group(3)
            break

    county = None
    for ln in lines[1:]:
        if re.search(r"county", ln, re.I):
            county = ln
            break

    return {
        "street": street,
        "unitNo": unit_no,
        "city": city,
        "state": state,
        "zip": zipcode,
        "county": county,
    }


def get_unit_address_via_new_tab(context, page: Page) -> dict:
    """Follow the Unit link on the resident page in a new tab and read Address.

    The Unit page renders the address label as `Address*` (with a required
    asterisk from a hidden <span class="required">). We match on the LABEL
    for="Address" — that's the ResMan-stable selector — and read the sibling
    `.fv` div for the value.
    """
    unit_href = page.evaluate(
        r"""() => {
          const links = Array.from(document.querySelectorAll('a[href*="/Units/Detail/"]'));
          return links[0] ? links[0].getAttribute('href') : null;
        }"""
    )
    if not unit_href:
        return {}
    url = unit_href if unit_href.startswith("http") else f"https://sns.myresman.com/{unit_href}"
    unit_page = context.new_page()
    try:
        unit_page.goto(url, wait_until="domcontentloaded")
        # Wait for the Address label (for="Address") to appear.
        unit_page.wait_for_function(
            r"""() => !!document.querySelector('label[for="Address"]')""",
            timeout=30000,
        )
        # Give the field value a moment to hydrate.
        unit_page.wait_for_function(
            r"""() => {
              const lbl  = document.querySelector('label[for="Address"]');
              const cell = lbl?.closest('td');
              const fv   = cell?.querySelector('.fv');
              return fv && fv.textContent.trim().length > 0;
            }""",
            timeout=15000,
        )
        addr_text = unit_page.evaluate(
            r"""() => {
              const lbl  = document.querySelector('label[for="Address"]');
              const cell = lbl?.closest('td');
              const fv   = cell?.querySelector('.fv');
              if (!fv) return null;
              // Prefer the structured child divs (street / city+state+zip / country).
              const parts = Array.from(fv.querySelectorAll('div, span'))
                .map(el => el.textContent.trim())
                .filter(t => t.length && t !== 'United States');
              if (parts.length) return parts.join('\n');
              return fv.textContent.trim();
            }"""
        )
    finally:
        unit_page.close()
    return parse_address_lines(addr_text or "")


# ----------------------------- Claim form gen ------------------------------

def generate_claim_form(
    out_dir: Path,
    resident_name: str,
    date_str: str,
    forwarding: dict,
    totals: dict,
    charges: list,
) -> Path:
    """Fill Claim Form Example.Docx with resident's data. Handles deposit vs no-deposit."""
    from docx import Document

    if not CLAIM_TEMPLATE.exists():
        raise FileNotFoundError(f"Claim template missing: {CLAIM_TEMPLATE}")

    dst = out_dir / f"Claim Form - {safe_slug(resident_name)}.docx"
    shutil.copy2(CLAIM_TEMPLATE, dst)

    sec_deposit = parse_money(totals.get("availableDepositApplied") or "0")
    credit_over = 0.00
    sec_plus_cred = sec_deposit + credit_over
    total_charges = parse_money(totals.get("balanceBeforeDepositsTotal"))
    resident_to_landlord = parse_money(totals.get("balanceOwed"))
    landlord_to_resident = 0.0
    # If deposit refunded to resident, adjust:
    dep_refund = parse_money(totals.get("depositRefund") or "0")
    pay_refund = parse_money(totals.get("paymentCreditRefund") or "0")
    if dep_refund + pay_refund > 0 and resident_to_landlord == 0:
        landlord_to_resident = dep_refund + pay_refund

    street = forwarding.get("street") or ""
    city   = forwarding.get("city")
    state  = forwarding.get("state")
    zipc   = forwarding.get("zip")
    citystatezip = ", ".join([p for p in [city, f"{state} {zipc}".strip() if state or zipc else None] if p])

    d = Document(dst)
    paras = d.paragraphs

    def force(p, text):
        for i, run in enumerate(p.runs):
            run.text = text if i == 0 else ""
        if not p.runs:
            p.add_run(text)

    force(paras[6],  f"Date: {date_str}")
    force(paras[8],  f"Resident(s) Name:    {resident_name}")
    force(paras[9],  f"Address: {street}")
    force(paras[10], citystatezip)
    force(paras[12], f"This is a notice of my intention to impose a claim for damages in the amount of: $ {money(sec_deposit)}")
    force(paras[18], f"Amount of Security Deposit:\t \t\t$ {money(sec_deposit)}")
    force(paras[19], f"Credit from Overpayment:\t\t \t$ {money(credit_over)}")
    force(paras[20], f"Total Security Deposit and Credit:\t \t$ {money(sec_plus_cred)}")
    force(paras[21], f"Total Charges:                \t \t\t$ {money(total_charges)}")
    force(paras[23], f"Total Due:  Landlord to Resident:              \t$ {money(landlord_to_resident)}")
    force(paras[24], f"                     Resident to Landlord:           \t$ {money(resident_to_landlord)}")

    d.save(dst)
    log(f"Wrote claim form: {dst}")
    return dst


# ----------------------------- Documents I/O -------------------------------

def download_fas_pdf(page: Page, out_dir: Path, resident_name: str, date_str: str) -> Path | None:
    """Find the auto-generated Final Account Statement <date>.pdf and save it locally."""
    # Expand Documents accordion.
    page.evaluate(
        r"""() => {
          const h = Array.from(document.querySelectorAll('h3')).find(x => x.textContent.trim().startsWith('Documents'));
          h?.scrollIntoView({block:'center'});
          h?.click();
        }"""
    )
    page.wait_for_timeout(1500)
    info = page.evaluate(
        r"""() => {
          const el = Array.from(document.querySelectorAll('.document-name'))
            .find(x => x.textContent.trim().toLowerCase().includes('final account statement'));
          if (!el) return null;
          const row = el.closest('.document-row-grid');
          const dl  = row?.querySelector('a[href*="/Documents/Download"]');
          return { name: el.textContent.trim(), href: dl?.getAttribute('href') };
        }"""
    )
    if not info or not info.get("href"):
        log("Final Account Statement PDF not found on Documents tab.")
        return None

    log(f"Downloading FAS PDF: {info['name']}")
    payload = page.evaluate(
        r"""async (url) => {
          const res = await fetch(url, { credentials: 'include' });
          const buf = await res.arrayBuffer();
          const bytes = new Uint8Array(buf);
          let bin = '';
          const CHUNK = 32768;
          for (let i = 0; i < bytes.length; i += CHUNK) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
          return { status: res.status, size: bytes.length, b64: btoa(bin) };
        }""",
        info["href"],
    )
    data = base64.b64decode(payload["b64"])
    dst = out_dir / f"Final Account Statement {date_str.replace('/', '-')} - {safe_slug(resident_name)}.pdf"
    dst.write_bytes(data)
    log(f"Saved FAS PDF: {dst} ({len(data)} bytes)")
    return dst


def upload_document(page: Page, file_path: Path):
    """Click Add under Documents, pick file, click OK. Works for any file type."""
    log(f"Uploading document via Documents > Add: {file_path.name}")
    with page.expect_file_chooser() as fc_info:
        page.locator('button.add-files').click()
        page.wait_for_timeout(800)  # let the dialog render
        page.locator('input[type="file"]').click()
    fc = fc_info.value
    fc.set_files(str(file_path))
    # Wait for the Name field to auto-populate then click the dialog's OK.
    page.wait_for_timeout(1000)
    page.evaluate(
        r"""() => {
          const btns = Array.from(document.querySelectorAll('button')).filter(b => b.textContent.trim() === 'OK' && b.getBoundingClientRect().width>0);
          btns[0]?.click();
        }"""
    )
    # Wait for the dialog to close and the doc to appear.
    page.wait_for_function(
        r"""(fname) => Array.from(document.querySelectorAll('.document-name')).some(el => el.textContent.trim() === fname)""",
        arg=file_path.name,
        timeout=30000,
    )
    log(f"Uploaded: {file_path.name}")


# ------------------------------- Send Email --------------------------------

def open_send_email_dialog(page: Page):
    log("Opening resident email dialog.")
    # Click the resident's mailto link.
    email_link = page.locator('a[href^="mailto:"]').first
    email_link.click()
    page.wait_for_function(
        r"""() => !!document.getElementById('FromObject') && !!document.getElementById('Add')""",
        timeout=15000,
    )


def set_from(page: Page, preference: str):
    """preference: 'property' or 'assistant'."""
    log(f"Setting From = {preference}")
    page.evaluate(
        r"""(pref) => {
          const sel = document.getElementById('FromObject');
          const opts = Array.from(sel.options);
          const match = pref === 'property'
            ? opts.find(o => o.dataset.objectType === 'Property')
            : opts.find(o => o.dataset.objectType === 'Person');
          if (!match) return { err: 'no match', options: opts.map(o => o.text) };
          sel.value = match.value;
          const display = document.getElementById('FromObjectInput');
          if (display) display.value = match.text;
          window.jQuery(sel).trigger('change');
          window.jQuery(display).trigger('change').trigger('autocompletechange');
          return { selected: match.text };
        }""",
        preference,
    )


def apply_template(page: Page, template_name: str):
    log(f"Applying template: {template_name}")
    page.locator('button:has-text("Template")').click()
    # Templates render as anchors in a dialog; pick the visible one.
    page.locator(f'a:has-text("{template_name}")').first.click()
    # Wait for Subject to populate.
    page.wait_for_function(
        r"""() => (document.querySelector('input[name="Subject"], input#Subject')?.value || '').length > 0""",
        timeout=15000,
    )


def attach_from_resman(page: Page, filenames: list):
    log(f"Attaching from ResMan: {filenames}")
    # Open Add menu (in email dialog Add button has id #Add).
    page.locator('#Add').click()
    page.locator('#btnAddFromCloud').click()
    page.wait_for_function(
        r"""() => !!document.querySelector('.document-name, .doc-name')""",
        timeout=15000,
    )

    checked = page.evaluate(
        r"""(names) => {
          const results = [];
          names.forEach(name => {
            const els = Array.from(document.querySelectorAll('.document-name, span, div'))
              .filter(el => el.textContent.trim() === name && el.children.length === 0);
            for (const el of els) {
              const row = el.closest('.document-row-grid');
              if (!row) continue;
              const cb = row.querySelector('input[type="checkbox"]');
              if (cb && cb.getBoundingClientRect().width > 0) {
                if (!cb.checked) cb.click();
                results.push({ name, checked: cb.checked });
                break;
              }
            }
            if (!results.find(r => r.name === name)) results.push({ name, checked: false, missing: true });
          });
          return results;
        }""",
        filenames,
    )
    log(f"Attachment check result: {checked}")

    # Click OK on the attachments picker.
    page.evaluate(
        r"""() => {
          const btns = Array.from(document.querySelectorAll('button')).filter(b => b.textContent.trim() === 'OK' && b.getBoundingClientRect().width>0);
          btns[0]?.click();
        }"""
    )
    page.wait_for_function(
        r"""(names) => names.every(n => document.body.innerText.includes(n))""",
        arg=filenames,
        timeout=15000,
    )
    return checked


def click_send(page: Page):
    log("Clicking Send.")
    page.evaluate(
        r"""() => {
          const btns = Array.from(document.querySelectorAll('button')).filter(b => b.textContent.trim() === 'Send' && b.getBoundingClientRect().width>0);
          btns[0]?.click();
        }"""
    )
    # Wait for the Send Email dialog to close (FromObject gone from the DOM).
    page.wait_for_function(
        r"""() => !document.getElementById('FromObject')""",
        timeout=45000,
    )
    log("Email sent.")


# ------------------------------ Runner main --------------------------------

def resident_name_from_page(page: Page) -> str:
    return page.evaluate(
        r"""() => {
          const m = document.body.innerText.match(/Full name\s*\n?\s*([^\n]+)/);
          return m ? m[1].trim() : '';
        }"""
    )


def unit_number_from_page(page: Page) -> str:
    return page.evaluate(
        r"""() => {
          const cells = Array.from(document.querySelectorAll('td'));
          const cell = cells.find(c => c.textContent.trim().startsWith('Unit') && /\d/.test(c.textContent));
          return cell ? (cell.textContent.match(/\d+/) || [''])[0] : '';
        }"""
    )


def resident_email_from_page(page: Page) -> str:
    return page.evaluate(
        r"""() => {
          const a = document.querySelector('a[href^="mailto:"]');
          return a ? a.getAttribute('href').replace(/^mailto:/, '') : '';
        }"""
    )


def merge_claim_and_fas_to_pdf(claim_docx: Path, fas_pdf: Path, out_dir: Path, resident_name: str) -> Path | None:
    """Convert claim docx -> PDF (needs MS Word), then merge with FAS PDF.
    Skips gracefully if docx2pdf/pypdf are not available or Word isn't installed."""
    try:
        from docx2pdf import convert
        from pypdf import PdfWriter, PdfReader
    except ImportError as e:
        log(f"Skipping PDF merge (missing dependency: {e}).")
        return None
    try:
        convert(str(claim_docx))                     # writes <name>.pdf beside the docx
    except Exception as e:
        log(f"docx2pdf conversion failed: {e}")
        return None
    claim_pdf = claim_docx.with_suffix(".pdf")
    if not claim_pdf.exists():
        log("docx2pdf did not produce a PDF; skipping merge.")
        return None
    combined = out_dir / f"Combined - {safe_slug(resident_name)}.pdf"
    w = PdfWriter()
    for src in [claim_pdf, fas_pdf]:
        for page_ in PdfReader(str(src)).pages:
            w.add_page(page_)
    with open(combined, "wb") as f:
        w.write(f)
    try:
        claim_pdf.unlink()  # keep only the merged PDF
    except FileNotFoundError:
        pass
    log(f"Wrote combined PDF: {combined}")
    return combined


def run(payload: dict, send: bool, headless: bool) -> dict:
    lease_url = payload["leaseUrl"]
    charges   = payload["charges"]
    mor_date  = payload.get("morDate") or today_str()
    email_cfg = payload.get("email") or {}
    email_enabled = email_cfg.get("enabled", True) and send
    from_pref = email_cfg.get("from", "property")
    template  = email_cfg.get("template", DEFAULT_TEMPLATE)
    out_dir   = Path(payload.get("outputDir") or HERE / "out")
    out_dir.mkdir(parents=True, exist_ok=True)

    result: dict = {
        "status": None,
        "startedAt": now_iso(),
        "endedAt": None,
        "durationSeconds": None,
        "resident": {
            "name": None, "unit": None, "property": "49th St Apartments",
            "leaseUrl": lease_url, "email": None,
        },
        "mor": {
            "date": mor_date, "status": None,
            "charges": [
                {"category": c.get("category", DEFAULT_CATEGORY),
                 "description": c["description"],
                 "amount": float(c["amount"])} for c in charges
            ],
            "totals": None,
            "forwardingAddress": None,
            "forwardingSource": None,
        },
        "docs": {"claimForm": None, "fasPdf": None, "combinedPdf": None},
        "email": {
            "attempted": email_enabled, "sent": False, "to": None,
            "from": from_pref, "template": template,
            "subject": None, "attachedByResMan": None,
        },
        "docupost": None,
        "github": {
            "repo": os.environ.get("GITHUB_REPOSITORY") or "ymi-flowing/mo-process",
            "runUrl": (
                f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
                if os.environ.get("GITHUB_RUN_ID") and os.environ.get("GITHUB_REPOSITORY") else None
            ),
        },
        "logs": None,      # filled at the end
        "error": None,
    }
    started = time.time()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless, args=["--start-maximized"])
        context = browser.new_context(no_viewport=True)
        page = context.new_page()

        login(page)
        open_move_out_rec(page, lease_url)
        fill_mor_date(page, mor_date)
        for c in charges:
            add_charge(page,
                       description=c["description"],
                       amount=float(c["amount"]),
                       category=c.get("category", DEFAULT_CATEGORY))
            page.wait_for_timeout(400)

        result["mor"]["totals"] = capture_mor_totals(page)
        log(f"Totals: {result['mor']['totals']}")

        approve_mor(page)
        result["mor"]["status"] = "Complete"

        resident_name = resident_name_from_page(page)
        result["resident"]["name"]  = resident_name
        result["resident"]["unit"]  = unit_number_from_page(page)
        result["resident"]["email"] = resident_email_from_page(page)
        log(f"Resident: {resident_name!r} unit {result['resident']['unit']!r}")

        fwd = get_forwarding_address(page)
        if fwd and fwd.get("street"):
            result["mor"]["forwardingSource"] = "resident"
        else:
            log("Forwarding blank -> falling back to unit address.")
            fwd = get_unit_address_via_new_tab(context, page) or {}
            result["mor"]["forwardingSource"] = "unit"
        result["mor"]["forwardingAddress"] = fwd

        claim_form_path = generate_claim_form(
            out_dir=out_dir,
            resident_name=resident_name,
            date_str=datetime.now().strftime("%m/%d/%Y"),
            forwarding=fwd,
            totals=result["mor"]["totals"],
            charges=charges,
        )
        result["docs"]["claimForm"] = str(claim_form_path)

        fas_path = download_fas_pdf(page, out_dir, resident_name, mor_date)
        result["docs"]["fasPdf"] = str(fas_path) if fas_path else None

        # Merge Claim Form + FAS into one PDF *before* uploading. We upload
        # only the merged PDF to ResMan's Documents (the docx becomes an
        # intermediate) so email attachments are a single, tidy file that
        # matches what Docupost mails.
        combined = None
        if fas_path:
            combined = merge_claim_and_fas_to_pdf(claim_form_path, fas_path, out_dir, resident_name)
            if combined:
                result["docs"]["combinedPdf"] = str(combined)

        # Prefer the Combined PDF for upload; fall back to the docx if the
        # merge step didn't run (Word missing, deps missing, etc.).
        uploaded_doc = combined or claim_form_path
        upload_document(page, uploaded_doc)

        if email_enabled:
            open_send_email_dialog(page)
            set_from(page, from_pref)
            apply_template(page, template)
            set_from(page, from_pref)  # template resets From; re-set.
            # Attach only the Combined PDF when available (contains both
            # Claim Form + FAS); otherwise the two separate docs.
            if combined:
                attachment_names = [combined.name]
            else:
                attachment_names = [claim_form_path.name]
                if fas_path:
                    attachment_names.append(fas_path.name)
            result["email"]["attachedByResMan"] = attach_from_resman(page, attachment_names)
            result["email"]["to"] = result["resident"]["email"]
            result["email"]["subject"] = f"{result['resident']['property']} - Move-Out Documents"
            click_send(page)
            result["email"]["sent"] = True
        else:
            log("Email skipped (either --no-send or email.enabled=false).")

        context.close()
        browser.close()

    result["status"] = "sent" if result["email"]["sent"] else ("parked" if not email_enabled else "sent_no_email")
    result["endedAt"] = now_iso()
    result["durationSeconds"] = int(time.time() - started)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--payload", required=True, help="JSON, '@file', or '-' for stdin")
    ap.add_argument("--headless", action="store_true", help="Run headless (default: headed)")
    ap.add_argument("--no-send", action="store_true", help="Skip the final email Send click")
    args = ap.parse_args()

    started_iso = now_iso()
    started_t   = time.time()

    payload = None
    try:
        payload = load_payload(args.payload)
        result = run(payload, send=not args.no_send, headless=args.headless)
        result["logs"] = list(_LOGS)
    except Exception as e:
        log(f"FATAL: {type(e).__name__}: {e}")
        result = {
            "status": "error",
            "startedAt": started_iso,
            "endedAt":   now_iso(),
            "durationSeconds": int(time.time() - started_t),
            "resident": {
                "name": None, "unit": None, "property": None,
                "leaseUrl": (payload or {}).get("leaseUrl") if isinstance(payload, dict) else None,
                "email": None,
            },
            "mor":   {"date": None, "status": None, "charges": [], "totals": None,
                      "forwardingAddress": None, "forwardingSource": None},
            "docs":  {"claimForm": None, "fasPdf": None, "combinedPdf": None},
            "email": {"attempted": False, "sent": False, "to": None,
                      "from": None, "template": None, "subject": None,
                      "attachedByResMan": None},
            "docupost": None,
            "github": {
                "repo":   os.environ.get("GITHUB_REPOSITORY") or "ymi-flowing/mo-process",
                "runUrl": (
                    f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
                    if os.environ.get("GITHUB_RUN_ID") and os.environ.get("GITHUB_REPOSITORY") else None
                ),
            },
            "logs":  list(_LOGS),
            "error": {
                "type": type(e).__name__,
                "message": str(e),
                "traceback": traceback.format_exc().splitlines()[-10:],
            },
        }

    # Emit a single JSON result on stdout so callers/CI can capture it.
    print(json.dumps(result, indent=2, default=str))
    # Non-zero exit on error so GH Actions marks the job failed but still emits JSON.
    sys.exit(1 if result.get("status") == "error" else 0)


if __name__ == "__main__":
    main()
