# MO Process — ResMan Move-Out / Final Account Statement Workflow

Automates closing out a former resident's Final Account Statement in ResMan and generating the FL statutory Notice of Intention to Impose Claim on Security Deposit (Fla. Stat. § 83.49(3)).

**End-to-end**: MOR approve → Claim Form + FAS merge → certified mail via Docupost → cert screenshot → upload both docs to a `/Move-Out Docs` folder via ResMan API → resident email → Comm Log verify.

## Flow (in order)

```
Login → open Move Out Rec → fill date + charges → capture totals → approve
      → resident info (name/unit/email) + forwarding address (via Lease Edit dialog)
      → generate Claim Form docx → download FAS PDF → merge (strip empty page)
      → Docupost sendletter (push PDF to public repo, POST /sendletter)
      → capture cert screenshot from Docupost dashboard
      → ResMan API: POST /Documents  (merged PDF → /Move-Out Docs, HARD-fail)
      → ResMan API: POST /Documents  (cert PNG   → /Move-Out Docs, SOFT-fail)
      → open Send Email → apply template → attach merged PDF from picker → Send
         (falls back to "Add from Computer" if picker doesn't surface the file)
      → Comm Log verify (Kendo grid lazy-hydrate — waits up to 25s)
```

**Typical run**: 4-5 min in CI. Biggest single wait is the Docupost tracking-number poll (up to 3 min; screenshot fires anyway on timeout).

## Result JSON — what n8n gets back

Always these top-level keys, always a JSON object even on error/crash. `status ∈ {sent, sent_no_email, parked, error}`.

```json
{
  "status": "sent",
  "startedAt": "2026-08-03T22:25:41Z",
  "endedAt":   "2026-08-03T22:31:47Z",
  "durationSeconds": 366,
  "resident": {"name": "Beatriz Pestana Gonzalez", "unit": "1511", "property": "49th St Apartments", "email": "..."},
  "mor": {
    "date": "8/3/2026", "status": "Complete",
    "charges": [{"category": "Cleaning/Damage Charges", "description": "Carpet Cleaning", "amount": 200.0}, ...],
    "totals": {
      "currentOpenBalanceTotal":         "0.00",
      "finalMoveOutChargesCreditsTotal": "420.00",
      "balanceBeforeDepositsTotal":      "420.00",
      "depositsAvailableTotal":          "799.00",
      "availableDepositApplied":         "420.00",
      "paymentCreditRefund":             "0.00",
      "depositRefund":                   "379.00",
      "balanceOwed":                     "0.00"
    },
    "forwardingAddress": {"street": "...", "city": "...", "state": "FL", "zip": "..."},
    "forwardingSource":  "resident"
  },
  "docs": {
    "claimForm":              "out/Claim Form - <resident>.docx",
    "fasPdf":                 "out/Final Account Statement 8-3-2026 - <resident>.pdf",
    "combinedPdf":            "out/Move Out Docs - Unit <#>.pdf",
    "combinedPdfDocumentId":  "<ResMan documentId returned by POST /Documents>"
  },
  "email": {
    "sent": true, "to": "...", "subject": "...",
    "attachedByResMan": [{"name": "...", "checked": true}],
    "commLogVerified": true, "commLogRow": "8/3/2026 <resident> ..."
  },
  "docupost": {
    "letterId":     "1785796086596x615321694122568600",
    "cost":         12.33,
    "class":        "usps_first_class",
    "servicelevel": "certified",
    "pdfUrl":       "https://raw.githubusercontent.com/ymi-flowing/mo-process/main/examples/...",
    "certification": {
      "uploaded":   true,
      "path":       "out/MO Docs - Mail Certification.png",
      "documentId": "<ResMan documentId>",
      "tracking":   "92071902358909000043674521",
      "error":      null
    }
  },
  "github": {"repo": "ymi-flowing/mo-process", "runUrl": "..."},
  "logs":   ["hh:mm:ss ...", ...],
  "error":  null
}
```

## Claim Form field logic (the deposit math)

Variables from `capture_mor_totals` (see [MOR totals capture](#mor-totals-capture)):
- `openBal` = `currentOpenBalanceTotal` (may be negative = credit/overpayment)
- `moCharges` = `finalMoveOutChargesCreditsTotal`
- `deposits` = `depositsAvailableTotal` (sum of every Available Deposit line)
- `depApplied` = `availableDepositApplied` (what ResMan actually pulled from deposit to zero the balance)
- `depRefund` + `payRefund` = amounts going back to resident

Filled by `generate_claim_form` (line references match paragraph indexes in `Claim Form Example.Docx`):

| # | Field | Formula |
|---|---|---|
| 12 | Amount claimed against deposit | `depApplied` |
| 14 | Property return address | from `properties.json` |
| 18 | Amount of Security Deposit | `deposits` |
| 19 | Credit from Overpayment | `abs(openBal) if openBal < 0 else 0` |
| 20 | Total Security Deposit and Credit | `deposits + credit_over` |
| 21 | Total Charges | `moCharges + max(0, openBal)` |
| 23 | Landlord → Resident (refund) | `depRefund + payRefund` if positive & `balOwed == 0` |
| 24 | Resident → Landlord (owed) | `balOwed` |
| 30 | PM email | from `properties.json` |

**Historical**: earlier revisions used `availableDepositApplied` (deposit *applied*) in fields 18/20, causing "Amount of Security Deposit" to show $650 instead of $999 when charges < deposit. Fixed by scraping the Totals row of ResMan's Available Deposit table.

## MOR totals capture

`capture_mor_totals` reads 8 fields from the MOR page before Approve:

- **Text scrape by label** (last `<td>` of the row):
  - `currentOpenBalanceTotal` ← "Current Open Balance Total"
  - `finalMoveOutChargesCreditsTotal` ← "Final Move Out Charges / Credits Total"
  - `balanceBeforeDepositsTotal` ← "Balance before Deposits Total"
- **Totals row of the "Available Deposit" table** (sums across multi-deposit residents):
  - `depositsAvailableTotal` ← the "Available Deposit" column of the row whose first cell is "Totals"
- **Input value scrape** (form fields):
  - `availableDepositApplied` ← `input[name*="ApplyToBalanceAmount"]`
  - `paymentCreditRefund` ← `#PaymentRefundAmount`
  - `depositRefund` ← `#CalculatedDepositRefundAmount`
  - `balanceOwed` ← `#BalanceOwed`

## Forwarding address

Read via ResMan's **Lease Edit dialog** — click `.dialog-form-link` next to the Forwarding address block, wait for `#ForwardingAddress_StreetAddress` to render, grab the 5 structured fields (`StreetAddress` / `City` / `State` / `Zip` / `Country`), close the dialog with X (never Save).

Fall back to `get_unit_address_via_new_tab()` (opens the Unit detail page) if the forwarding is blank.

**Why not scrape the read-only cell?** Earlier revisions did; ResMan renders that cell in at least two DOM shapes (sibling `<div>`s vs plain text with `<br>`) that both collapsed under `textContent` and defeated the CSZ regex. The Edit dialog has stable IDs — zero parsing.

## Merged PDF (Claim Form + FAS)

Filename: `Move Out Docs - Unit <#>.pdf`. Falls back to `Move Out Docs - <resident-slug>.pdf` if the unit is unknown.

The FAS PDF always has an "Images for Charges" trailing page — when no images were uploaded, that page is empty except for the header text. `merge_claim_and_fas_to_pdf` **strips** pages whose extracted text is *exactly* `"Images for Charges"` (case-insensitive), only from the FAS source. Guarded so a real page with header + images survives.

Requires MS Word or LibreOffice for the docx → PDF conversion. CI installs LibreOffice.

## Multi-property support

`properties.json` at repo root. Each entry drives the property-specific data on the Claim Form + Docupost sender:

```json
{
  "49th St Apartments": {
    "proid":    "a262aa42-7393-4d84-9bf5-ae1bff852b32",
    "email":    "pm@49streetapts.com",
    "address1": "8400 49th Street N",
    "city":     "Pinellas Park",
    "state":    "FL",
    "zip":      "33781"
  },
  "The Villas at Ortega": { ... }
}
```

**Property resolution order** (`resolve_property`):
1. `payload.property` explicit override.
2. `proid` GUID from `#MoveOutReconciliationLink` data-href → match against DB entries with a non-null `proid`.
3. Body-text substring match against DB keys (unique match required).

If none resolves, the runner raises **before** touching MOR — no half-completed reconciliation.

**Rendered claim-form samples** live in `examples/claim-form-samples/`.

## ResMan API (upload docs into resident's Documents tab)

`POST https://partners-api.myresman.com/Documents` (multipart/form-data). Auth: Basic (`PartnerId:ApiKey`), plus `ResMan-Account-Id` header.

Body fields the runner sends:
- `propertyId` = property GUID (from properties.json → `proid`)
- `Id` = `oid` (BillingAccountId — extracted from MOR data-href query string)
- `type` = `Lease`
- `file` = merged PDF or cert PNG bytes
- `fileName` = filename to display in Documents
- `path` = `/Move-Out Docs` (folder auto-created if it doesn't exist)
- `showInResidentPortal` = `false`

Response returns `documentId` (logged into result JSON). Retries on Cloudflare 5xx (502/503/504) up to 3× with 65s backoff.

**Failure policy**:
- **Merged PDF upload = HARD-FAIL.** If it fails, the email step can't attach → whole run marked `status: error`.
- **Cert PNG upload = SOFT-FAIL.** Letter is already mailed; cert is nice-to-have. Errors go to `result.docupost.certification.error`.

## Certified mail via Docupost

`POST https://app.docupost.com/api/1.1/wf/sendletter` — **params go in the query string, not the body.**

Minimum working params:

```
api_token, pdf=<publicly reachable URL>,
class=usps_first_class, servicelevel=certified,
from_name, from_address1, from_city, from_state, from_zip,
to_name,   to_address1,   to_city,   to_state,   to_zip
color=false, doublesided=false, description=<internal ≤40 chars>
```

Gotchas learned the hard way:
- `servicelevel=certified` **requires** `class=usps_first_class`. `usps_standard` silently ignores certified.
- `pdf` must be **publicly reachable**. The runner pushes the merged PDF to `examples/` in this public repo via the GitHub Contents API and hands Docupost the `raw.githubusercontent.com` URL.
- Response: `{status, letter_id, cost}` — only 3 fields, **no tracking number**. Tracking # is only available later on the Docupost dashboard.
- Cost for a 3-page certified B&W single-sided letter: **~$12.33**.
- Cancel test letters within 1 hour at https://docupost.com/letters to avoid charge.

## Cert screenshot from Docupost dashboard

`capture_docupost_certification()`:
1. Opens `https://app.docupost.com/letter/<letter_id>` in a fresh browser context.
2. Logs in with `DOCUPOST_WEB_USER` / `DOCUPOST_WEB_PASS`.
3. Waits for the letter page (`Delivery Status` text).
4. Polls up to **180s** for `USPS Tracking # <digits>` to appear in the body (Docupost can take 30-180s to publish tracking after sendletter).
5. Screenshots the smallest `.bubble-element.Group` containing "Delivery Status" + "Recipient" + "Sender" — that's Bubble.io's middle-content column. Falls back to viewport screenshot if the panel selector misses.
6. Saves as `out/MO Docs - Mail Certification.png`.
7. Returns `(path, tracking_number)` — either may be `None` on soft failure.

## Privacy tradeoff

Every real resident's merged claim form briefly ends up as a public file at `raw.githubusercontent.com/ymi-flowing/mo-process/main/examples/Move Out Docs - Unit <#>.pdf`. Docupost's fetcher needs a public URL, and GitHub raw was the only reliable host we tested (catbox.moe / tmpfiles.org failed). Rename or delete those files after each certified letter is safely mailed if that's a concern. Future work: Vercel Blob / Cloudflare R2 with short-TTL signed URLs — the runner's `push_pdf_to_repo(...)` helper is the only piece that would swap.

## Input payload

```json
{
  "leaseUrl": "https://sns.myresman.com/#/Residents/Detail/<leaseId>",
  "charges": [
    { "description": "Cleaning",        "amount": 150.00 },
    { "description": "Carpet Cleaning", "amount": 200.00 }
  ],
  "morDate": null,
  "email": {
    "enabled": true,
    "from":    "property",
    "template": "***MO Docs Email"
  },
  "docupost": {
    "enabled": true,
    "class":        "usps_first_class",
    "servicelevel": "certified",
    "color":        false,
    "doublesided":  false
  },
  "outputDir": "out"
}
```

- `charges[].category` optional (default `Cleaning/Damage Charges`).
- `morDate` optional (defaults to today in `M/D/YYYY`).
- `email.from`: `property` or `assistant`.
- `docupost.enabled: false` skips the certified-mail step; the runner still uploads the docs and emails the resident.

## Run (local)

```
pip install -r requirements.txt
playwright install chromium
python run_mo_process.py --payload @payload.example.json                   # headed
python run_mo_process.py --payload @payload.example.json --headless        # CI
python run_mo_process.py --payload @payload.example.json --no-send         # dry-run: stop before Send
python run_mo_process.py --payload -                                       # stdin JSON
```

## Trigger via HTTP (n8n → GitHub Actions)

Workflow `mo-process.yml` accepts:
- `payload` — the JSON payload (single-line string).
- `resume_url` — n8n webhook URL to POST the final JSON back to.
- `no_send` — `"true"` for dry-run.

**Repo secrets** (Settings → Secrets and variables → Actions):

| Secret | Purpose |
|---|---|
| `RESMAN_USER` | ResMan web login (defaults to `SNS_Assistant`) |
| `RESMAN_PASS` | ResMan web login |
| `RESMAN_API_KEY` | ResMan Partner API — Basic auth password |
| `RESMAN_PARTNER_ID` | ResMan Partner API — Basic auth username (`SNSaPi`) |
| `RESMAN_ACCOUNT_ID` | ResMan Partner API — `ResMan-Account-Id` header (`1550`) |
| `DOCUPOST_TOKEN` | Docupost sendletter API |
| `DOCUPOST_WEB_USER` | Docupost dashboard login (for cert screenshot) |
| `DOCUPOST_WEB_PASS` | Docupost dashboard login |

`GITHUB_TOKEN` is auto-provided by Actions; the workflow has `permissions: { contents: write }` so the runner can push the merged PDF into `examples/` via the Contents API.

**Local dev fallback**: all creds also readable from `Cardentials.txt` (gitignored). Env wins.

**n8n HTTP node to dispatch**:
```
POST https://api.github.com/repos/ymi-flowing/mo-process/actions/workflows/mo-process.yml/dispatches
Headers:
  Authorization: Bearer <GITHUB_PAT with 'workflow' scope>
  Accept:        application/vnd.github+json
Body:
{
  "ref": "main",
  "inputs": {
    "payload":    "{{ JSON.stringify($json.payload) }}",
    "resume_url": "{{ $execution.resumeUrl }}",
    "no_send":    "false"
  }
}
```

## Importable n8n workflow

`examples/n8n-workflow.json` — 6-node workflow: `Fillout Webhook → Transform Payload → Dispatch GH Actions → Wait for Runner Callback → Build Email HTML → Gmail Send`. Wait node uses POST (no `webhookSuffix`) so the bare `$execution.resumeUrl` matches; 30-min timeout so hangs eventually fail loud.

**After import, wire two credentials**:
- **HTTP Header Auth** for `Dispatch GH Actions` — `Authorization: Bearer <GitHub PAT with 'workflow' scope>`.
- **Gmail OAuth2** for `Gmail Send` — signed in as the mailbox you want the summary from.

## Files

- `run_mo_process.py` — headed/headless Playwright runner (entry point).
- `Claim Form Example.Docx` — template with FL statutory notice language.
- `properties.json` — multi-property config.
- `payload.example.json` / `payload.villas.json` — sample input payloads.
- `Cardentials.txt` — creds for local dev (**gitignored**).
- `send_email_only.py` — standalone helper: re-send the resident email for a resident whose MOR is already approved and merged PDF is already in Documents. Useful for recovery after a partial run.
- `verify_comm_log.py` — standalone helper: dump Communication Log rows for a resident (headed) to debug send-verification issues.
- `examples/` — completed runs kept as reference:
  - `Claim Form - Dwaun Spigner.docx`, `Final Account Statement 7-13-26 - Dwaun Spigner.pdf` — a sample completed reconciliation.
  - `Move Out Docs - <resident/unit>.pdf` — merged PDFs from real production runs (public — see privacy note above).
  - `claim-form-samples/` — one Claim Form per property using synthetic resident data.
  - `n8n-workflow.json`, `n8n-email-builder.js` — importable n8n bits.
  - `result-success.json`, `result-error.json` — result JSON shape references.
  - `mor-dwaun-317.json` — sample MOR totals capture.

## Known IDs (for reference)

**49th St Apartments**
- Property ID (`proid`): `a262aa42-7393-4d84-9bf5-ae1bff852b32`
