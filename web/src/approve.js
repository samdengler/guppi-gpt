// The invite approval page (docs/proposals/invites.md). Sam opens it from the link in a
// request's email; it shows the request and one Approve button. A button rather than an
// approving link, because mail scanners open the links in a message. Every value is set
// with textContent: the request's name and note are a stranger's text.

import {
  approveBody,
  decidedText,
  lookupUrl,
  outcome,
  readLink,
  requestRows,
} from "./approve-core.js";

const $ = (id) => document.getElementById(id);

function say(text, kind = "") {
  const message = $("approve-message");
  message.textContent = text;
  message.className = `approve-message ${kind}`.trim();
  message.hidden = false;
}

function showRows(item) {
  const list = $("approve-details");
  list.replaceChildren();
  for (const [label, value] of requestRows(item)) {
    const term = document.createElement("dt");
    term.textContent = label;
    const detail = document.createElement("dd");
    detail.textContent = value;
    list.append(term, detail);
  }
  list.hidden = false;
}

async function approve(link) {
  const button = $("approve-btn");
  button.disabled = true;
  let status = 0;
  try {
    const response = await fetch("/api/invite/approve", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: approveBody(link),
    });
    status = response.status;
  } catch {
    status = 0;
  }
  const result = outcome(status, link.email);
  say(result.text, result.ok ? "ok" : "error");
  $("approve-actions").hidden = result.ok;
  button.disabled = result.ok;
}

async function main() {
  const link = readLink(location.search);
  if (!link) {
    say("This page needs the link from an invite request email.", "error");
    return;
  }
  let response;
  try {
    response = await fetch(lookupUrl(link));
  } catch {
    say("The request could not be loaded. Try the link again.", "error");
    return;
  }
  if (!response.ok) {
    say("No request matches this link.", "error");
    return;
  }
  const item = await response.json();
  showRows(item);
  const decided = decidedText(item.status);
  if (decided) {
    say(decided);
    return;
  }
  $("approve-email").textContent = item.email;
  $("approve-actions").hidden = false;
  $("approve-btn").addEventListener("click", () => approve(link));
}

main();
