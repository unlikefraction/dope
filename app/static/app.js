const state = {
  user: null,
  route: "active",
  dopes: [],
  filters: { min: "", max: "", completedBy: "", from: "", to: "" },
  authMode: "login",
};

const $ = (id) => document.getElementById(id);

function minutesFromText(value) {
  const text = String(value || "").trim().toLowerCase().replace(/\s+/g, "");
  if (!text) return null;
  const match = text.match(/^(\d+(?:\.\d+)?)(h|hr|hrs|hour|hours|m|min|mins|minute|minutes)?$/);
  if (!match) return null;
  const amount = Number(match[1]);
  const unit = match[2] || "hr";
  return Math.max(1, Math.round(unit.startsWith("h") ? amount * 60 : amount));
}

function formatMinutes(minutes) {
  if (minutes < 60) return `${minutes}min`;
  const hours = minutes / 60;
  return Number.isInteger(hours) ? `${hours}hr` : `${hours.toFixed(1).replace(/\.0$/, "")}hr`;
}

function localDate(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function fullDate(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!res.ok) {
    let message = "Something went wrong";
    try { message = (await res.json()).detail || message; } catch {}
    throw new Error(message);
  }
  return res.json();
}

function toast(message, undo) {
  const el = $("toast");
  el.innerHTML = "";
  el.append(document.createTextNode(message));
  if (undo) {
    const btn = document.createElement("button");
    btn.className = "undo";
    btn.textContent = "Undo";
    btn.onclick = undo;
    el.append(btn);
  }
  el.hidden = false;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.hidden = true; }, 10000);
}

function showAuth() {
  $("auth-view").hidden = false;
  $("app-view").hidden = true;
}

function showApp() {
  $("auth-view").hidden = true;
  $("app-view").hidden = false;
  $("user-name").textContent = state.user.display_name;
}

function updateAuthMode() {
  const signup = state.authMode === "signup";
  $("auth-display").hidden = !signup;
  $("auth-display").required = signup;
  $("auth-submit").textContent = signup ? "Create account" : "Sign in";
  $("auth-toggle").textContent = signup ? "Already have an account" : "Create an account";
}

async function init() {
  updateAuthMode();
  try {
    state.user = await api("/api/me");
    showApp();
    await loadRoute();
  } catch {
    showAuth();
  }
}

async function loadRoute() {
  state.route = location.hash.replace("#", "") || "active";
  if (!["active", "completed", "archived"].includes(state.route)) state.route = "active";
  document.querySelectorAll(".nav-links a").forEach((a) => a.classList.toggle("active", a.dataset.route === state.route));
  $("page-title").textContent = state.route === "active" ? "Dopes" : state.route === "completed" ? "Completed Dopes" : "Archived Dopes";
  $("page-subtitle").textContent = state.route === "active" ? "Product work waiting to be amended." : state.route === "completed" ? "Work closed with commit links." : "Dopes moved out of the main queue.";
  $("new-dope").style.display = state.route === "active" ? "inline-flex" : "none";
  $("active-assigned-wrap").style.display = state.route === "active" ? "block" : "none";
  $("completed-filters").style.display = state.route === "completed" ? "block" : "none";
  state.dopes = await api(`/api/dopes?status=${state.route}`);
  render();
}

function searchableText(d) {
  const div = document.createElement("div");
  div.innerHTML = d.description_html || "";
  return `${d.title} ${div.textContent || ""} ${d.assigned_to?.display_name || ""} ${d.completed_by?.display_name || ""}`;
}

function filteredDopes() {
  let items = [...state.dopes];
  const query = $("search").value.trim();
  if (query) {
    const fuse = new Fuse(items.map((d) => ({ ...d, searchText: searchableText(d) })), {
      keys: ["title", "searchText"],
      threshold: 0.6,
      distance: 120,
      ignoreLocation: true,
      includeScore: true,
    });
    items = fuse.search(query).map((r) => r.item);
  }
  const min = minutesFromText(state.filters.min);
  const max = minutesFromText(state.filters.max);
  if (min) items = items.filter((d) => d.time_minutes >= min);
  if (max) items = items.filter((d) => d.time_minutes <= max);
  if (state.route === "completed") {
    const by = state.filters.completedBy.trim().toLowerCase();
    if (by) items = items.filter((d) => (d.completed_by?.display_name || "").toLowerCase().includes(by));
    if (state.filters.from) items = items.filter((d) => d.completed_at?.slice(0, 10) >= state.filters.from);
    if (state.filters.to) items = items.filter((d) => d.completed_at?.slice(0, 10) <= state.filters.to);
  }
  return items;
}

function render() {
  const items = filteredDopes();
  const assigned = items.filter((d) => d.status === "active" && d.assigned_to);
  $("active-assigned").innerHTML = assigned.length ? assigned.map(card).join("") : `<p class="empty">No one is working on a dope right now.</p>`;
  $("list-heading").textContent = state.route === "active" ? "Open Dopes" : state.route === "completed" ? "Completed List" : "Archive";
  $("dope-list").innerHTML = items.map(card).join("");
  $("empty").hidden = items.length !== 0;
  document.querySelectorAll("[data-dope]").forEach((el) => {
    el.onclick = () => openDope(Number(el.dataset.dope));
  });
}

function card(d) {
  const status = d.status === "completed" ? `Completed by ${escapeHtml(d.completed_by?.display_name || "someone")} on ${localDate(d.completed_at)}` :
    d.status === "archived" ? `Archived ${localDate(d.archived_at)}` :
    d.assigned_to ? `Assigned to ${escapeHtml(d.assigned_to.display_name)}` : "Ready to self assign";
  return `<button class="dope-card" data-dope="${d.id}">
    <span><h3>${escapeHtml(d.title)}</h3><span class="meta"><span>${status}</span></span></span>
    <span class="pill"><i class="ph ph-clock"></i>${formatMinutes(d.time_minutes)}</span>
  </button>`;
}

function escapeHtml(value) {
  return String(value || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[c]));
}

function sanitizeHtml(html) {
  const doc = new DOMParser().parseFromString(html, "text/html");
  doc.querySelectorAll("script,style,iframe,object,embed,link,meta").forEach((n) => n.remove());
  doc.body.querySelectorAll("*").forEach((node) => {
    [...node.attributes].forEach((attr) => {
      const name = attr.name.toLowerCase();
      const value = attr.value;
      if (name.startsWith("on")) node.removeAttribute(attr.name);
      if ((name === "href" || name === "src") && !/^(https?:|data:image\/)/i.test(value)) node.removeAttribute(attr.name);
    });
  });
  return doc.body.innerHTML;
}

function openNewDope() {
  $("modal-title").textContent = "New Dope";
  $("modal-body").innerHTML = `
    <label>Title<input id="new-title" placeholder="Improve onboarding empty state"></label>
    <label>Description<div id="new-description" class="editor" contenteditable="true" data-placeholder="Write details. Paste images directly here."></div></label>
    <label>Time to complete<input id="new-time" placeholder="2hr, 30min, 0.5hr"></label>
    <div class="modal-actions"><button id="create-dope" value="default">Create Dope</button></div>
  `;
  wireEditor($("new-description"));
  $("create-dope").onclick = async (event) => {
    event.preventDefault();
    try {
      await api("/api/dopes", {
        method: "POST",
        body: JSON.stringify({
          title: $("new-title").value,
          description_html: sanitizeHtml($("new-description").innerHTML),
          time_text: $("new-time").value,
        }),
      });
      $("dope-dialog").close();
      await loadRoute();
      toast("Dope created");
    } catch (err) { toast(err.message); }
  };
  $("dope-dialog").showModal();
}

function wireEditor(editor) {
  editor.addEventListener("paste", (event) => {
    const item = [...event.clipboardData.items].find((i) => i.type.startsWith("image/"));
    if (!item) return;
    event.preventDefault();
    const reader = new FileReader();
    reader.onload = () => document.execCommand("insertHTML", false, `<img src="${reader.result}" alt="Pasted image">`);
    reader.readAsDataURL(item.getAsFile());
  });
}

function openDope(id) {
  const d = state.dopes.find((item) => item.id === id);
  if (!d) return;
  $("modal-title").textContent = d.title;
  const history = d.assignment_history.length ? `<h2>Assignment History</h2><ul class="history">${d.assignment_history.map((h) => `<li>${escapeHtml(h.display_name)} tried this on ${fullDate(h.assigned_at)}${h.unassigned_at ? ` and unassigned on ${fullDate(h.unassigned_at)}` : ""}</li>`).join("")}</ul>` : "";
  const links = d.commit_links.length ? `<h2>Commits</h2><ul class="links">${d.commit_links.map((l) => `<li><a href="${escapeHtml(l)}" target="_blank" rel="noreferrer">${escapeHtml(l)}</a></li>`).join("")}</ul>` : "";
  const completeBlock = d.status === "active" ? `
    <h2>Close Dope</h2>
    <label>Commit links<textarea-proxy><input id="commit-links" placeholder="One or more links, comma or newline separated"></textarea-proxy></label>
    <label>Completion description<input id="completion-description" placeholder="Optional"></label>
  ` : "";
  $("modal-body").innerHTML = `
    <div class="meta"><span class="pill"><i class="ph ph-clock"></i>${formatMinutes(d.time_minutes)}</span>${d.assigned_to ? `<span>Assigned to ${escapeHtml(d.assigned_to.display_name)}</span>` : ""}${d.completed_by ? `<span>Completed by ${escapeHtml(d.completed_by.display_name)} on ${localDate(d.completed_at)}</span>` : ""}</div>
    <div class="description">${sanitizeHtml(d.description_html)}</div>
    ${d.completion_description ? `<h2>Completion Notes</h2><p class="muted">${escapeHtml(d.completion_description)}</p>` : ""}
    ${links}
    ${history}
    ${completeBlock}
    <div class="modal-actions">
      ${d.status === "active" ? `<button id="assign" value="default"><i class="ph ph-user-plus"></i>Self Assign this Dope</button>` : ""}
      ${d.status === "active" && d.assigned_to ? `<button id="unassign" class="secondary" value="default">Unassign</button>` : ""}
      ${d.status === "active" ? `<button id="complete" class="secondary" value="default">Close with commits</button>` : ""}
      ${d.status !== "archived" ? `<button id="archive" class="danger" value="default"><i class="ph ph-archive"></i>Archive</button>` : `<button id="restore" class="secondary" value="default">Restore</button>`}
    </div>
  `;
  bindDopeActions(d);
  $("dope-dialog").showModal();
}

function bindDopeActions(d) {
  const closeReload = async (message) => {
    $("dope-dialog").close();
    await loadRoute();
    toast(message);
  };
  const assign = $("assign");
  if (assign) assign.onclick = async (event) => { event.preventDefault(); await api(`/api/dopes/${d.id}/assign`, { method: "POST" }); await closeReload("Dope assigned"); };
  const unassign = $("unassign");
  if (unassign) unassign.onclick = async (event) => { event.preventDefault(); await api(`/api/dopes/${d.id}/unassign`, { method: "POST" }); await closeReload("Dope unassigned"); };
  const complete = $("complete");
  if (complete) complete.onclick = async (event) => {
    event.preventDefault();
    const links = $("commit-links").value.split(/[,\n]/).map((x) => x.trim()).filter(Boolean);
    try {
      await api(`/api/dopes/${d.id}/complete`, { method: "POST", body: JSON.stringify({ commit_links: links, completion_description: $("completion-description").value }) });
      await closeReload("Dope completed");
    } catch (err) { toast(err.message); }
  };
  const archive = $("archive");
  if (archive) archive.onclick = async (event) => {
    event.preventDefault();
    if (!confirm("Archive this dope?")) return;
    await api(`/api/dopes/${d.id}/archive`, { method: "POST" });
    $("dope-dialog").close();
    await loadRoute();
    toast("Dope archived", async () => { await api(`/api/dopes/${d.id}/restore`, { method: "POST" }); await loadRoute(); toast("Archive undone"); });
  };
  const restore = $("restore");
  if (restore) restore.onclick = async (event) => { event.preventDefault(); await api(`/api/dopes/${d.id}/restore`, { method: "POST" }); await closeReload("Dope restored"); };
}

$("auth-toggle").onclick = () => { state.authMode = state.authMode === "login" ? "signup" : "login"; updateAuthMode(); };
$("auth-form").onsubmit = async (event) => {
  event.preventDefault();
  const path = state.authMode === "signup" ? "/api/auth/signup" : "/api/auth/login";
  try {
    await api(path, {
      method: "POST",
      body: JSON.stringify({ username: $("auth-username").value, password: $("auth-password").value, display_name: $("auth-display").value }),
    });
    state.user = await api("/api/me");
    showApp();
    await loadRoute();
  } catch (err) { toast(err.message); }
};
$("logout").onclick = async () => { await api("/api/auth/logout", { method: "POST" }); state.user = null; showAuth(); };
$("new-dope").onclick = openNewDope;
$("search").oninput = render;
$("filter-open").onclick = () => {
  $("filter-min").value = state.filters.min;
  $("filter-max").value = state.filters.max;
  $("filter-completed-by").value = state.filters.completedBy;
  $("filter-from").value = state.filters.from;
  $("filter-to").value = state.filters.to;
  $("filter-dialog").showModal();
};
$("filter-apply").onclick = (event) => {
  event.preventDefault();
  state.filters = { min: $("filter-min").value, max: $("filter-max").value, completedBy: $("filter-completed-by").value, from: $("filter-from").value, to: $("filter-to").value };
  $("filter-dialog").close();
  render();
};
$("filter-reset").onclick = (event) => {
  event.preventDefault();
  state.filters = { min: "", max: "", completedBy: "", from: "", to: "" };
  $("filter-dialog").close();
  render();
};
window.addEventListener("hashchange", loadRoute);
init();
