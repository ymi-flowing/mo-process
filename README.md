# MO Process — ResMan Move-Out / Final Account Statement Workflow

Automates closing out a former resident's Final Account Statement in ResMan and generating the FL statutory Notice of Intention to Impose Claim on Security Deposit (Fla. Stat. § 83.49(3)).

## Input
- Resident URL (e.g. `https://sns.myresman.com/#/Residents/Detail/<leaseId>`)
- List of move-out charges: `{ description, amount }` (default category: `Cleaning/Damage Charges`)

## Run

```
pip install -r requirements.txt
playwright install chromium
python run_mo_process.py --payload @payload.example.json
python run_mo_process.py --payload @payload.example.json --no-send   # dry-run: stop before Send
python run_mo_process.py --payload @payload.example.json --headless  # CI
python run_mo_process.py --payload -                                 # stdin JSON
```

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

## Known IDs (49th St Apartments)
- Property ID (`proid`): `a262aa42-7393-4d84-9bf5-ae1bff852b32`

## Cases exercised
- **cleuza alves ferreira batista** (unit 315) — no deposit, $1,000 in cleaning/damage charges added, balance owed $4,738.00.
- _next: case with security deposit._
