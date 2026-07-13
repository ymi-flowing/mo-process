// n8n Code node — build a styled HTML summary email from the MO Process run result.
//
// INPUT
//   The Webhook node (waiting on $execution.resumeUrl) hands the GitHub
//   Action callback to this Code node. The run result sits in `body` — or
//   the whole JSON is passed directly.
//
// OUTPUT
//   { ...originalData, htmlEmail, emailSubject }
//   Pipe `htmlEmail` into a Gmail / SMTP / Microsoft Teams node.
//
// Follows C:\Users\ymosh\Claude\ROI\email-guidelines.md (SNS brand tokens).

const incoming = $input.first().json;
const data     = incoming.body || incoming;

// --- Brand constants (keep in sync with email-guidelines.md) ---
const PRIMARY  = "#003f75";  // SNS blue
const SUCCESS  = "#16a34a";
const ERROR    = "#dc2626";
const WARN     = "#d97706";
const LOGO_URL = "https://www.dropbox.com/scl/fi/8i6y3e0lnjhm65s2ymhr9/SNS.jpg?rlkey=6ca6wtkxp5hr4l16ppu9hkj1z&raw=1";

// --- Helpers ---
const escapeHtml = (s) =>
  String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");

const formatMoney = (v) => {
  if (v == null || v === "") return "—";
  const n = typeof v === "number" ? v : Number(String(v).replace(/,/g, ""));
  if (Number.isNaN(n)) return String(v);
  return "$" + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
};

const titleCase = (k) => k.replace(/([A-Z])/g, " $1").replace(/_/g, " ")
  .replace(/^\s+|\s+$/g, "")
  .replace(/\b\w/g, (c) => c.toUpperCase());

// --- Status pill / headline ---
const status = data.status || "unknown";
let statusLabel, statusColor, statusEmoji, headline, subtitle;
const residentName = (data.resident && data.resident.name) || "Unknown Resident";
const unit         = (data.resident && data.resident.unit) || "";
const property     = (data.resident && data.resident.property) || "";
const residentUrl  = (data.resident && data.resident.leaseUrl) || "#";
const runUrl       = (data.github   && data.github.runUrl)     || "#";

if (status === "sent") {
  statusLabel = "Sent";  statusColor = SUCCESS; statusEmoji = "✅";
  headline    = "Move-Out Documents Sent";
  subtitle    = `Final Account Statement + Claim Form sent to ${residentName}${unit ? " (unit " + unit + ")" : ""}.`;
} else if (status === "parked") {
  statusLabel = "Parked"; statusColor = WARN; statusEmoji = "⏸️";
  headline    = "MO Process Prepared (Dry Run)";
  subtitle    = `MOR was approved and docs uploaded for ${residentName}, but the resident email was NOT sent.`;
} else if (status === "sent_no_email") {
  statusLabel = "Docs Uploaded"; statusColor = WARN; statusEmoji = "📎";
  headline    = "MO Docs Uploaded (Email Skipped)";
  subtitle    = `Reconciliation completed for ${residentName}. Documents attached in ResMan, email step skipped by config.`;
} else {
  statusLabel = "Error"; statusColor = ERROR; statusEmoji = "⚠️";
  headline    = "MO Process Failed";
  subtitle    = (data.error && data.error.message) ? data.error.message.split("\n")[0] : "Something went wrong.";
}

// --- Resident card ---
const residentItems = [
  ["Resident", residentName],
  ["Unit",     unit || "—"],
  ["Property", property || "—"],
  ["Email",    (data.email && data.email.to) || (data.resident && data.resident.email) || "—"],
  ["ResMan Lease", residentUrl !== "#" ? `<a href="${escapeHtml(residentUrl)}" style="color:${PRIMARY};text-decoration:underline;">Open</a>` : "—"],
];
const residentHtml = residentItems.map(([k, v]) => `
  <li style="margin:0 0 6px 0; border-bottom:1px solid #f0f0f0; padding-bottom:4px;">
    <span style="font-weight:800;">${escapeHtml(k)}:</span> ${k === "ResMan Lease" ? v : escapeHtml(v)}
  </li>`).join("");

// --- Charges card ---
const chargesArr = (data.mor && data.mor.charges) || [];
const chargesHtml = chargesArr.length ? chargesArr.map((c) => `
  <li style="margin:0 0 6px 0; border-bottom:1px solid #f0f0f0; padding-bottom:4px;">
    <span style="font-weight:800;">${escapeHtml(c.description)}</span> · ${escapeHtml(c.category)} · ${formatMoney(c.amount)}
  </li>`).join("") : `<li style="color:#666;">(no charges added)</li>`;

// --- Totals card ---
const totals = (data.mor && data.mor.totals) || {};
const totalsItems = [
  ["Current Open Balance",             totals.currentOpenBalanceTotal],
  ["Final Move-Out Charges / Credits", totals.finalMoveOutChargesCreditsTotal],
  ["Balance before Deposits",          totals.balanceBeforeDepositsTotal],
  ["Available Deposit Applied",        totals.availableDepositApplied],
  ["Deposit Refund",                   totals.depositRefund],
  ["Payment / Credit Refund",          totals.paymentCreditRefund],
  ["Balance Owed",                     totals.balanceOwed],
];
const totalsHtml = totalsItems.map(([k, v]) => `
  <li style="margin:0 0 6px 0; border-bottom:1px solid #f0f0f0; padding-bottom:4px;">
    <span style="font-weight:800;">${escapeHtml(k)}:</span> ${formatMoney(v)}
  </li>`).join("");

// --- Forwarding address card ---
const fwd  = (data.mor && data.mor.forwardingAddress) || {};
const fwdSrc = (data.mor && data.mor.forwardingSource) || "—";
const fwdLines = [fwd.street, [fwd.city, fwd.state, fwd.zip].filter(Boolean).join(", ")].filter(Boolean);
const fwdHtml = fwdLines.length ? fwdLines.map((l) => `
  <li style="margin:0 0 4px 0; color:#222;">${escapeHtml(l)}</li>`).join("")
  + `<li style="color:#666; font-size:12px; margin-top:6px;">Source: ${escapeHtml(fwdSrc)}</li>`
  : `<li style="color:#666;">(no forwarding address on file)</li>`;

// --- Email + Docupost card ---
const email = data.email || {};
const emailItems = [
  ["Resident Email Sent", email.sent ? "✓ Yes" : (email.attempted ? "✗ Attempted, not sent" : "✗ Skipped")],
  ["From",                email.from    || "—"],
  ["Template",            email.template || "—"],
  ["Subject",             email.subject || "—"],
  ["Attachments",         (email.attachedByResMan || []).length
      ? (email.attachedByResMan || [])
          .map((a) => `${a.checked ? "✓" : (a.missing ? "✗ MISSING" : "✗")} ${escapeHtml(a.name)}`)
          .join("<br>")
      : "—"],
];
const emailCardHtml = emailItems.map(([k, v]) => {
  // "Attachments" already contains inline HTML (<br> + ✓/✗ prefixes) —
  // render raw. All other rows go through escapeHtml.
  const cell = (k === "Attachments") ? v : escapeHtml(v);
  return `
  <li style="margin:0 0 6px 0; border-bottom:1px solid #f0f0f0; padding-bottom:4px;">
    <span style="font-weight:800;">${escapeHtml(k)}:</span> ${cell}
  </li>`;
}).join("");

const dp = data.docupost;
const docupostHtml = dp ? [
  ["Letter ID", dp.letterId || dp.letter_id || "—"],
  ["Cost",      dp.cost != null ? formatMoney(dp.cost) : "—"],
  ["Class",     dp.class || "—"],
  ["Service",   dp.servicelevel || dp.service_level || "—"],
].map(([k, v]) => `
  <li style="margin:0 0 6px 0; border-bottom:1px solid #f0f0f0; padding-bottom:4px;">
    <span style="font-weight:800;">${escapeHtml(k)}:</span> ${escapeHtml(v)}
  </li>`).join("") : "";

// --- Error card (only on error) ---
const errorCard = (status === "error" && data.error) ? `
  <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0"
    style="background:#fef2f2; border:1px solid #fecaca; border-radius:14px; margin-bottom:20px;">
    <tr>
      <td style="padding:16px;">
        <div style="font-size:14px; font-weight:800; color:#991b1b; margin:0 0 8px 0;">Error Detail (${escapeHtml(data.error.type || "Error")})</div>
        <pre style="margin:0; white-space:pre-wrap; word-break:break-word; font-family:'SFMono-Regular', Consolas, monospace; font-size:12px; color:#7f1d1d;">${escapeHtml(data.error.message || "")}</pre>
      </td>
    </tr>
  </table>` : "";

// --- Section renderer ---
const sectionCard = (title, innerHtml) => `
  <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0"
    style="background:#fafafa; border:1px solid #eeeeee; border-radius:14px; margin-bottom:20px;">
    <tr>
      <td style="padding:16px;">
        <div style="font-size:14px; line-height:20px; color:#111111; font-weight:800; margin:0 0 10px 0;">${escapeHtml(title)}</div>
        <ul style="margin:0; padding:0 0 0 18px; color:#222222; font-size:13px; line-height:20px;">${innerHtml}</ul>
      </td>
    </tr>
  </table>`;

const htmlEmail = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="x-apple-disable-message-reformatting" />
  <title>${headline}</title>
</head>
<body style="margin:0; padding:0; background-color:#f4f4f4; font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, Helvetica, sans-serif;">
  <div style="display:none; font-size:1px; line-height:1px; max-height:0; max-width:0; opacity:0; overflow:hidden; mso-hide:all;">
    ${statusEmoji} ${headline} — ${escapeHtml(residentName)}
  </div>
  <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0" style="background-color:#f4f4f4; padding:28px 0;">
    <tr><td align="center" style="padding:0 12px;">
      <table role="presentation" width="500" border="0" cellspacing="0" cellpadding="0"
        style="width:500px; max-width:500px; background-color:#ffffff; border-radius:16px; overflow:hidden; box-shadow:0 6px 20px rgba(0,0,0,0.06);">

        <tr><td align="left" style="padding:20px 26px; background-color:#ffffff; border-bottom:1px solid #eeeeee;">
          <div style="border-radius:8px; overflow:hidden; display:inline-block;">
            <img src="${LOGO_URL}" height="45" alt="SNS Logo" style="display:block; border:0; outline:none; text-decoration:none; height:45px; width:auto;" />
          </div>
        </td></tr>

        <tr><td style="padding:26px;">

          <div style="display:inline-block; background-color:${statusColor}; color:#ffffff; padding:4px 12px; border-radius:999px; font-size:11px; font-weight:800; letter-spacing:0.4px; text-transform:uppercase; margin:0 0 12px 0;">
            ${statusEmoji} ${statusLabel}
          </div>

          <div style="margin:0 0 8px 0; font-size:22px; line-height:28px; color:#111111; font-weight:800;">${headline}</div>
          <div style="margin:0 0 20px 0; font-size:14px; line-height:22px; color:#333333;">${escapeHtml(subtitle)}</div>

          ${errorCard}

          ${sectionCard("Resident", residentHtml)}
          ${sectionCard("Move-Out Charges Added", chargesHtml)}
          ${sectionCard("Reconciliation Totals",  totalsHtml)}
          ${sectionCard("Forwarding Address",     fwdHtml)}
          ${sectionCard("Email to Resident",      emailCardHtml)}
          ${docupostHtml ? sectionCard("Certified Mail (Docupost)", docupostHtml) : ""}

          <table role="presentation" border="0" cellspacing="0" cellpadding="0" style="margin:0 auto 12px auto;">
            <tr>
              <td align="center" style="background-color:${PRIMARY}; border-radius:10px;">
                <a href="${escapeHtml(residentUrl)}" style="display:inline-block; padding:12px 24px; font-size:14px; font-weight:800; color:#ffffff; text-decoration:none;">
                  Open Resident in ResMan
                </a>
              </td>
            </tr>
          </table>

          <table role="presentation" border="0" cellspacing="0" cellpadding="0" style="margin:0 auto 4px auto;">
            <tr>
              <td align="center" style="background-color:#6b7280; border-radius:10px;">
                <a href="${escapeHtml(runUrl)}" style="display:inline-block; padding:10px 22px; font-size:13px; font-weight:800; color:#ffffff; text-decoration:none;">
                  View Workflow Run
                </a>
              </td>
            </tr>
          </table>

        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>`;

return [{
  json: {
    ...data,
    htmlEmail,
    emailSubject: `${statusEmoji} ${headline} — ${residentName}${unit ? " · " + unit : ""}`,
  }
}];
