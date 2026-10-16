"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtBytes = (n) => { if (n == null) return ""; const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; } return `${n.toFixed(i ? 1 : 0)} ${u[i]}`; };
const fmtDate = (s) => s ? new Date(s).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "";

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
}

// ---------------------------------------------------------------- upload (chunked, resumable)
const RESUME_KEY = "pst-uploads";
function resumeMap() { try { return JSON.parse(localStorage.getItem(RESUME_KEY) || "{}"); } catch (_) { return {}; } }
function saveResume(m) { try { localStorage.setItem(RESUME_KEY, JSON.stringify(m)); } catch (_) {} }

async function putChunk(id, offset, blob) {
  for (let attempt = 0; ; attempt++) {
    try {
      return await api(`/api/uploads/${id}?offset=${offset}`, { method: "PUT", body: blob, headers: { "Content-Type": "application/octet-stream" } });
    } catch (e) {
      if (attempt >= 4) throw e;
      await new Promise((res) => setTimeout(res, 1000 * 2 ** attempt));
    }
  }
}

async function uploadFile(file) {
  const key = `${file.name}:${file.size}:${file.lastModified}`;
  const map = resumeMap();
  let id = map[key], offset = 0, chunk = (+document.body.dataset.chunkMb || 10) * 1024 * 1024;
  if (id) {
    try {
      const st = await api(`/api/uploads/${id}`);
      if (st.status === "UPLOADING") offset = st.bytes_received; else id = null;
    } catch (_) { id = null; }
  }
  if (!id) {
    const created = await api("/api/uploads", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ filename: file.name, size: file.size }) });
    id = created.id; chunk = created.chunk_size; offset = 0;
    map[key] = id; saveResume(map);
  }
  $("#upload-progress").hidden = false;
  $("#upload-name").textContent = file.name + (offset ? " (resuming)" : "");
  refreshArchives();
  while (offset < file.size) {
    const res = await putChunk(id, offset, file.slice(offset, offset + chunk));
    offset = res.bytes_received;
    const pct = Math.floor((offset / file.size) * 100);
    $("#upload-bar").value = pct;
    $("#upload-pct").textContent = `${pct}% of ${fmtBytes(file.size)}`;
  }
  await api(`/api/uploads/${id}/complete`, { method: "POST" });
  delete map[key]; saveResume(map);
  $("#upload-name").textContent = `${file.name} uploaded, indexing…`;
  refreshArchives();
}

$("#file-input").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  ev.target.value = "";
  if (!file) return;
  try { await uploadFile(file); }
  catch (e) { $("#upload-progress").hidden = false; $("#upload-name").textContent = `Upload failed: ${e.message}`; }
});

// ---------------------------------------------------------------- archives
let pollTimer = null;
async function refreshArchives() {
  let rows = [];
  try { rows = await api("/api/archives"); } catch (e) { return; }
  const tbody = $("#archives tbody");
  tbody.innerHTML = rows.map((a) => {
    const total = a.total_items || 0;
    const pct = a.status === "UPLOADING" ? (a.file_size_bytes ? Math.floor((a.bytes_received / a.file_size_bytes) * 100) : 0)
      : total ? Math.floor((a.processed_items / total) * 100) : (a.status === "COMPLETED" ? 100 : 0);
    const detail = a.status === "UPLOADING" ? `${pct}% uploaded`
      : `${a.processed_emails} emails, ${a.processed_attachments} attachments${a.skipped_items ? `, ${a.skipped_items} skipped` : ""}`;
    return `<tr>
      <td>${esc(a.filename)}<div class="muted">${fmtDate(a.created_at)}</div></td>
      <td>${fmtBytes(a.file_size_bytes)}</td>
      <td><span class="status ${a.status}">${a.status}</span>${a.error_message ? `<div class="err">${esc(a.error_message)}</div>` : ""}</td>
      <td><progress max="100" value="${pct}"></progress><div class="muted">${detail}</div></td>
      <td>
        ${["FAILED", "COMPLETED"].includes(a.status) ? `<button class="link" data-retry="${a.id}">Re-index</button>` : ""}
        <button class="link danger" data-delete="${a.id}" data-name="${esc(a.filename)}">Delete</button>
      </td></tr>`;
  }).join("");
  $("#no-archives").hidden = rows.length > 0;

  const sel = $("#f-archive"), current = sel.value;
  sel.innerHTML = `<option value="">All</option>` + rows.filter((a) => a.status === "COMPLETED" || a.processed_emails)
    .map((a) => `<option value="${a.id}">${esc(a.filename)}</option>`).join("");
  sel.value = current;

  try { const s = await api("/api/stats"); $("#stats").textContent = `${s.emails} emails · ${s.attachments} attachments · ${s.chunks} indexed chunks`; } catch (_) {}

  clearTimeout(pollTimer);
  if (rows.some((a) => ["UPLOADING", "QUEUED", "PROCESSING"].includes(a.status))) pollTimer = setTimeout(refreshArchives, 2500);
}

$("#archives").addEventListener("click", async (ev) => {
  const del = ev.target.closest("[data-delete]"), retry = ev.target.closest("[data-retry]");
  try {
    if (del && confirm(`Delete ${del.dataset.name} and everything extracted from it?`)) await api(`/api/archives/${del.dataset.delete}`, { method: "DELETE" });
    if (retry) await api(`/api/archives/${retry.dataset.retry}/retry`, { method: "POST" });
  } catch (e) { alert(e.message); }
  refreshArchives();
});

// ---------------------------------------------------------------- search
$("#search-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const p = new URLSearchParams({ q: $("#q").value, mode: $("#f-mode").value, limit: "30" });
  for (const [k, sel] of [["archive_id", "#f-archive"], ["sender", "#f-sender"], ["date_from", "#f-from"], ["date_to", "#f-to"], ["source_type", "#f-source"]]) {
    if ($(sel).value) p.set(k, $(sel).value);
  }
  $("#result-meta").textContent = "Searching…";
  const t0 = performance.now();
  try {
    const data = await api(`/api/search?${p}`);
    $("#result-meta").textContent = `${data.count} results in ${Math.round(performance.now() - t0)} ms`;
    $("#results").innerHTML = data.results.map((r) => `
      <li data-email="${r.email_id}">
        <div class="subject">${esc(r.subject || "(no subject)")}</div>
        <div class="meta">
          ${r.source_type === "ATTACHMENT" ? `<span class="badge">📎 ${esc(r.attachment_filename)}</span>` : `<span class="badge">Email</span>`}
          ${esc(r.sender_name || r.sender_email || "")} · ${fmtDate(r.sent_at)} · ${esc(r.folder_path)}
          · <span title="keyword rank ${r.kw_rank ?? "–"}, semantic rank ${r.sem_rank ?? "–"}">score ${r.rrf_score.toFixed(4)}</span>
        </div>
        <div class="snippet">${r.snippet}</div>
      </li>`).join("");
  } catch (e) {
    $("#result-meta").textContent = `Search failed: ${e.message}`;
  }
});

$("#results").addEventListener("click", (ev) => {
  const li = ev.target.closest("li[data-email]");
  if (li) openEmail(li.dataset.email);
});

// ---------------------------------------------------------------- email viewer
async function openEmail(id) {
  const e = await api(`/api/emails/${id}`);
  $("#d-subject").textContent = e.subject || "(no subject)";
  const byType = (t) => e.recipients.filter((r) => r.type === t).map((r) => r.email ? `${r.name || ""} <${r.email}>`.trim() : r.name).join(", ");
  const rows = [
    ["From", `${e.sender_name || ""} ${e.sender_email ? `<${e.sender_email}>` : ""}`],
    ["To", byType("to")], ["Cc", byType("cc")], ["Bcc", byType("bcc")],
    ["Date", fmtDate(e.sent_at)], ["Folder", e.folder_path], ["Archive", e.archive_filename], ["Message-ID", e.internet_message_id],
  ].filter(([, v]) => v && v.trim());
  $("#d-meta").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("");
  $("#d-attachments").innerHTML = e.attachments.length ? "<strong>Attachments</strong>" + e.attachments.map((a) =>
    a.downloadable ? `<span class="att"><a href="/api/attachments/${a.id}/download">${esc(a.filename)}</a> <span class="muted">${fmtBytes(a.size_bytes)}</span></span>`
      : `<span class="att muted">${esc(a.filename)} (not stored)</span>`).join("") : "";
  $("#d-body").textContent = e.body_text || "(no text body)";
  $("#d-tabs").hidden = !e.has_html;
  showTab("text");
  $("#d-html").dataset.src = e.has_html ? `/api/emails/${id}/html` : "";
  $("#email-dialog").showModal();
}

function showTab(tab) {
  document.querySelectorAll("#d-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $("#d-body").hidden = tab !== "text";
  const frame = $("#d-html");
  frame.hidden = tab !== "html";
  if (tab === "html" && frame.dataset.src && frame.src !== location.origin + frame.dataset.src) frame.src = frame.dataset.src;
  if (tab === "text") frame.removeAttribute("src");
}
$("#d-tabs").addEventListener("click", (ev) => { const b = ev.target.closest("button[data-tab]"); if (b) showTab(b.dataset.tab); });
$("#d-close").addEventListener("click", () => $("#email-dialog").close());
$("#email-dialog").addEventListener("click", (ev) => { if (ev.target.id === "email-dialog") ev.target.close(); });

refreshArchives();
