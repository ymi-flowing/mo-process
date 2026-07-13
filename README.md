# MO Process — ResMan Move-Out / Final Account Statement Workflow

Automates closing out a former resident's Final Account Statement in ResMan and generating the FL statutory Notice of Intention to Impose Claim on Security Deposit (Fla. Stat. § 83.49(3)).

## Input
- Resident URL (e.g. `https://sns.myresman.com/#/Residents/Detail/<leaseId>`)
- List of move-out charges: `{ description, amount }` (default category: `Cleaning/Damage Charges`)

## Run (local)

```
pip install -r requirements.txt
playwright install chromium
python run_mo_process.py --payload @payload.example.json
python run_mo_process.py --payload @payload.example.json --no-send   # dry-run: stop before Send
python run_mo_process.py --payload @payload.example.json --headless  # CI
python run_mo_process.py --payload -                                 # stdin JSON
```

## Trigger via HTTP (n8n → GitHub Actions)

The `mo-process.yml` workflow accepts a `workflow_dispatch` payload with three inputs:
- `payload` — the JSON payload (as a single-line string).
- `resume_url` — n8n webhook URL to POST the final result JSON back to.
- `no_send` — `true` for dry-run (skip the resident-email Send click).

**Repo secrets to set once** (Settings → Secrets and variables → Actions):
- `RESMAN_USER` — defaults to `SNS_Assistant` if unset.
- `RESMAN_PASS` — defaults to the SNS_Assistant password if unset.

**n8n HTTP node to dispatch:**
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

The workflow runs the Playwright script headless, POSTs the final result JSON to `resume_url`, and uploads `result.json` + `out/` as an artifact for post-mortem.

### Result JSON — the shape n8n will get back

Success → `examples/result-success.json`. Error → `examples/result-error.json`. Always these top-level keys: `status`, `startedAt`, `endedAt`, `durationSeconds`, `resident`, `mor`, `docs`, `email`, `docupost`, `github`, `logs`, `error`. `status` is one of `sent | sent_no_email | parked | error`.

### Build a summary email in n8n

`examples/n8n-email-builder.js` is a Code node that turns the run result into an SNS-branded HTML summary email (uses the tokens in `C:\Users\ymosh\Claude\ROI\email-guidelines.md`). It renders: status pill, resident block (name/unit/property/email + Open-in-ResMan link), charges list, reconciliation totals, forwarding address (with source), resident-email step result, and — when populated — the Docupost letter block. Downstream: pipe `htmlEmail` into a Gmail / SMTP node.

### Payload
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
    "from": "property",
    "template": "***MO Docs Email"
  },
  "outputDir": "out"
}
```
- `charges[].category` optional (default `Cleaning/Damage Charges`).
- `morDate` optional (defaults to today in `M/D/YYYY`).
- `email.from`: `property` (49th St - pm@49streetapts.com) or `assistant` (SNS_Assistant).
- Credentials via env: `RESMAN_USER`, `RESMAN_PASS` (defaults to SNS_Assistant).

### Result (stdout JSON)
```json
{
  "leaseUrl": "...",
  "morDate": "7/13/2026",
  "morStatus": "Complete",
  "totals": { "currentOpenBalanceTotal": "3,257.90", "balanceOwed": "2,708.90", ... },
  "forwardingAddress": { "street": "...", "city": "...", "state": "FL", "zip": "..." },
  "forwardingSource": "resident" | "unit",
  "claimForm": "out/Claim Form - Dwaun Spigner.docx",
  "fasPdf":    "out/Final Account Statement 7-13-2026 - Dwaun Spigner.pdf",
  "emailSent": true,
  "attachedByResMan": [ { "name": "...", "checked": true } ]
}
```

## Credentials
See `Cardentials.txt` — ResMan web login (SNS_Assistant) + Partners API keys.

## Steps

1. **Login**: `https://sns.myresman.com/` → auth form auto-fills; click "Sign in".
2. **Open resident**: navigate to the resident detail URL.
3. **Open Move Out Reconciliation**: in the left "Leasing Workflow" sidebar, click **Move Out Rec.** — this is the visible anchor `#MoveOutReconciliationLink` (there's also a hidden `#MoveOutReconciliationOpenLink` for un-started reconciliations; the visible one is the right entry point). ResMan's jQuery ajax handler rewrites `data-href` into `#/Transactions/MoveOutReconciliation?proid=…&oid=…&lid=…&perid=…`.
4. **Move-out rec. date***: fill with today (M/D/YYYY).
5. **Add each charge**: click **Add Charge / Credit** → set Category (Cleaning/Damage Charges), Description, Amount. Repeat for each additional charge. Amount total shows in "Final Move Out Charges / Credits Total".
6. **Capture totals** to JSON:
   - `currentOpenBalanceTotal`
   - `finalMoveOutChargesCreditsTotal`
   - `balanceBeforeDepositsTotal`
   - `paymentCreditRefund`
   - `depositRefund`
   - `balanceOwed`
7. **Capture forwarding address** (split into `street`, `unitNo`, `city`, `state`, `zip`, `county`). If blank on resident, follow the Unit link (`#/Units/Detail/<unitId>` from the Unit Information cell) and use the unit's address instead.
8. **Approve**: click **Actions** button → **Approve** link (`#Approve`). ResMan redirects to `/#/Residents/RedirectToDetail?ulgid=…` and the workflow item flips to **Move Out Rec (Complete)**.
9. **Generate Claim Form** by copying `Claim Form Example.Docx` and substituting the resident name + totals (see `Claim Form - <resident>.docx`). Numbers when no deposit:
   - Amount of Security Deposit: $0.00
   - Credit from Overpayment: $0.00
   - Total Security Deposit and Credit: $0.00
   - Total Charges: balanceOwed
   - Total Due Landlord to Resident: $0.00
   - Total Due Resident to Landlord: balanceOwed
10. **Download Final Account Statement PDF** from Documents: ResMan auto-generates `Final Account Statement <date>.pdf` on Approve. Get the row's download href (`/Documents/Download?documentID=<uuid>`), then fetch via in-page `fetch(url, {credentials:'include'})`, base64-encode, decode locally to `.pdf`.
11. **Upload the Claim Form** to Documents:
    - Click **Add** button under Documents (`button.add-files`).
    - Set file via file chooser → Name field auto-fills → click **OK**.
12. **Email the docs to the resident**:
    - Click the resident's email (mailto link) — opens ResMan's Send Email dialog.
    - Set From to the property (`#FromObject` select — pick "49th St Apartments - pm@49streetapts.com"). Also mirror the display combobox: `#FromObjectInput.value = <option text>`.
    - Click **Template** button → pick **`***MO Docs Email`** — this fills Subject "49th St Apartments - Move-Out Documents" and body. Template resets From — re-select the property after.
    - Click **Add** button (email dialog's `#Add`) → **Add from ResMan** (`#btnAddFromCloud`) → check both **Claim Form - `<resident>`.docx** and **Final Account Statement `<date>`.pdf** → OK.
    - Click **Send**.

## Files
- `Cardentials.txt` — ResMan web login + API keys (**gitignored**).
- `Claim Form Example.Docx` — template with FL statutory notice language.
- `run_mo_process.py` — headed Playwright runner (entry point).
- `payload.example.json` — sample input payload.
- `requirements.txt`, `.env.example`, `.gitignore`.
- `examples/` — a completed run's artifacts kept as a reference (unit 317, Dwaun Spigner):
  - `mor-<resident>-<unit>.json` — captured totals, charges, forwarding address, MOR status.
  - `Claim Form - <resident>.docx` — generated claim form.
  - `Final Account Statement <date> - <resident>.pdf` — downloaded auto-generated FAS.
  - `Combined - <resident>.pdf` — merged Claim Form + FAS (single PDF used for mailing).

## Merging Claim Form + FAS into one PDF
```python
from docx2pdf import convert       # requires MS Word installed
from pypdf import PdfWriter, PdfReader

convert(str(claim_form_docx))       # writes <name>.pdf beside the docx
w = PdfWriter()
for src in [claim_form_pdf, fas_pdf]:
    for page in PdfReader(str(src)).pages:
        w.add_page(page)
w.write(str(combined_pdf))
```
Order matters: Claim Form is page 1, FAS follows. The combined PDF is what gets mailed by Docupost.

## Certified mail via Docupost
Endpoint: `POST https://app.docupost.com/api/1.1/wf/sendletter` — **params go in the query string, not the body.**

Minimum working params (from live testing):

```
api_token, pdf, class=usps_first_class, servicelevel=certified,
from_name, from_address1, from_city, from_state, from_zip,
to_name,   to_address1,   to_city,   to_state,   to_zip
color=false, doublesided=false, description=<internal ≤40 chars>
```

Notes learned the hard way:
- `servicelevel=certified` **requires** `class=usps_first_class`. Using `usps_standard` with `certified` silently ignores the certified request.
- `pdf` must be a **publicly reachable URL** (no multipart/base64). Docupost's fetcher couldn't reach `catbox.moe` or `tmpfiles.org` — GitHub raw on a public repo works reliably.
- Response: `{ "status": "Successfully queued. Cost: $X.XX ...", "letter_id": "<id>", "cost": <number> }`. Save `letter_id` — that's how you cancel or track the letter in Docupost's dashboard.
- Docs say: "cancel any test letters within an hour to avoid being billed or having the mailpiece sent" — do this at https://docupost.com/letters.
- Cost for a 3-page certified B&W single-sided letter: **~$12.33**.

### Hosting the Combined PDF for Docupost
Docupost fetches the PDF over the public internet. Options tried:
- `catbox.moe`, `tmpfiles.org` — Docupost's helper returned `Temporary error connecting to DocuPost Helpers - S3`. Do not use.
- **GitHub raw on a public repo** — worked first try (this repo).
- Vercel Blob / Cloudflare R2 with signed URLs — better for prod (keeps PDFs private + short TTL). Not yet wired.

## Known IDs (49th St Apartments)
- Property ID (`proid`): `a262aa42-7393-4d84-9bf5-ae1bff852b32`

## Cases exercised
- **cleuza alves ferreira batista** (unit 315) — no deposit, $1,000 in cleaning/damage charges added, balance owed $4,738.00.
- _next: case with security deposit._
