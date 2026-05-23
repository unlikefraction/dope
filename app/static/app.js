const state = {
  user: null,
  route: "active",
  dopes: [],
  allDopes: [],
  progress: [],
  progressDays: 7,
  filters: { min: "", max: "", depsDoped: false, completedBy: "", from: "", to: "" },
  authMode: "login",
};

const $ = (id) => document.getElementById(id);

function minutesFromText(value) {
  const text = String(value || "").trim().toLowerCase();
  if (!text) return null;
  const tokenRe = /(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)?/g;
  const matches = [...text.matchAll(tokenRe)];
  const consumed = matches.map((match) => match[0]).join("").replace(/\s+/g, "");
  if (!matches.length || consumed !== text.replace(/\s+/g, "")) return null;
  const minutes = matches.reduce((total, match) => {
    const amount = Number(match[1]);
    const unit = match[2] || "hr";
    return total + (unit.startsWith("h") ? amount * 60 : amount);
  }, 0);
  return Math.max(1, Math.round(minutes));
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
  const openModal = document.querySelector("dialog[open] .modal");
  const toastHost = openModal || document.body;
  if (el.parentElement !== toastHost) toastHost.appendChild(el);
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

function celebrateDope() {
  const existing = $("dope-celebration");
  if (existing) existing.remove();
  const shell = document.createElement("div");
  shell.id = "dope-celebration";
  shell.className = "dope-celebration";
  const marks = ["Doped", "Ship it", "Done", "Amended"];
  shell.innerHTML = `
    <div class="celebration-card">
      <i class="ph ph-confetti"></i>
      <strong>Doped</strong>
      <span>Commit links locked in.</span>
    </div>
    ${Array.from({ length: 24 }, (_, i) => {
      const x = 8 + Math.random() * 84;
      const delay = Math.random() * 0.2;
      const duration = 0.75 + Math.random() * 0.45;
      const label = marks[i % marks.length];
      return `<span class="celebration-piece" style="left:${x}%; animation-delay:${delay}s; animation-duration:${duration}s;">${escapeHtml(label)}</span>`;
    }).join("")}
  `;
  document.body.appendChild(shell);
  setTimeout(() => shell.remove(), 1800);
}

function showAuth() {
  $("auth-view").hidden = false;
  $("app-view").hidden = true;
  $("auth-view").style.display = "grid";
  $("app-view").style.display = "none";
}

function showApp() {
  $("auth-view").hidden = true;
  $("app-view").hidden = false;
  $("auth-view").style.display = "none";
  $("app-view").style.display = "block";
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
  } catch {
    showAuth();
    return;
  }
  showApp();
  try {
    await loadRoute();
  } catch (err) {
    toast(err.message || "Could not load dopes");
  }
}

async function loadRoute() {
  state.route = location.hash.replace("#", "") || "active";
  if (!["active", "completed", "archived"].includes(state.route)) state.route = "active";
  const isActive = state.route === "active";
  document.querySelectorAll(".nav-links a").forEach((a) => a.classList.toggle("active", a.dataset.route === state.route));
  $("page-title").textContent = isActive ? "Active Dopes" : state.route === "completed" ? "Completed Dopes" : "Archived Dopes";
  $("page-title").classList.toggle("compact-title", isActive);
  $("page-subtitle").textContent = state.route === "active" ? "Product work waiting to be amended." : state.route === "completed" ? "Work closed with commit links." : "Dopes moved out of the main queue.";
  $("page-subtitle").hidden = isActive;
  $("progress-panel").hidden = !isActive;
  $("new-dope").style.display = isActive ? "inline-flex" : "none";
  $("active-assigned-wrap").style.display = isActive ? "block" : "none";
  $("completed-filters").style.display = state.route === "completed" ? "block" : "none";
  if (isActive) {
    [state.dopes, state.progress] = await Promise.all([
      api(`/api/dopes?status=${state.route}`),
      api(`/api/stats/progress?days=${state.progressDays}`),
    ]);
  } else {
    state.dopes = await api(`/api/dopes?status=${state.route}`);
  }
  state.allDopes = [];
  render();
}

async function ensureAllDopes(force = false) {
  if (!force && state.allDopes.length) return state.allDopes;
  state.allDopes = await api("/api/dopes?status=all");
  return state.allDopes;
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
  if (state.filters.depsDoped) items = items.filter((d) => (d.blocked_dependencies || []).length === 0);
  if (state.route === "completed") {
    const by = state.filters.completedBy.trim().toLowerCase();
    if (by) items = items.filter((d) => (d.completed_by?.display_name || "").toLowerCase().includes(by));
    if (state.filters.from) items = items.filter((d) => d.completed_at?.slice(0, 10) >= state.filters.from);
    if (state.filters.to) items = items.filter((d) => d.completed_at?.slice(0, 10) <= state.filters.to);
  }
  return items;
}

function filtersActive() {
  const baseActive = Boolean(state.filters.min || state.filters.max || state.filters.depsDoped);
  if (state.route !== "completed") return baseActive;
  return baseActive || Boolean(state.filters.completedBy || state.filters.from || state.filters.to);
}

function renderFilterButton() {
  const active = filtersActive();
  const button = $("filter-open");
  button.classList.toggle("secondary", !active);
  button.classList.toggle("filters-active", active);
  button.innerHTML = `<i class="ph ph-sliders-horizontal"></i>${active ? "Filters Applied" : "Filter"}`;
}

function render() {
  renderFilterButton();
  renderProgressChart();
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

function progressColor(userId) {
  const colors = ["#1a1a1a", "#5a5a5a", "#8b8b8b", "#2e6f8e", "#5a9a6b", "#7a4d8e", "#9a4d42"];
  return colors[Math.abs(Number(userId) || 0) % colors.length];
}

function dopeCountLabel(count) {
  return `${count} ${count === 1 ? "dope" : "dopes"}`;
}

function renderProgressChart() {
  const panel = $("progress-panel");
  if (!panel || state.route !== "active") return;
  document.querySelectorAll("[data-progress-days]").forEach((button) => {
    button.classList.toggle("active", Number(button.dataset.progressDays) === state.progressDays);
  });
  const chart = $("progress-chart");
  const maxMinutes = Math.max(60, ...state.progress.map((day) => day.total_minutes || 0));
  chart.innerHTML = state.progress.map((day) => {
    const total = day.total_minutes || 0;
    const stacks = day.stacks.length ? day.stacks.map((stack) => {
      const width = Math.max(2, (stack.minutes / maxMinutes) * 100);
      const tip = `${stack.display_name}\n${formatMinutes(stack.minutes)}\n${dopeCountLabel(stack.count)}`;
      return `<span class="progress-stack" style="width:${width}%;background:${progressColor(stack.user_id)}" data-progress-tip="${escapeHtml(tip)}" aria-label="${escapeHtml(tip)}"></span>`;
    }).join("") : `<span class="progress-empty"></span>`;
    return `
      <div class="progress-row">
        <span class="progress-date">${escapeHtml(day.label)}</span>
        <div class="progress-track">${stacks}</div>
        <span class="progress-total">${total ? formatMinutes(total) : "0"}</span>
      </div>
    `;
  }).join("");
}

function showProgressTooltip(event) {
  const target = event.target.closest("[data-progress-tip]");
  const tip = $("progress-tooltip");
  if (!target || !tip) return;
  tip.textContent = target.dataset.progressTip;
  tip.hidden = false;
  moveProgressTooltip(event);
}

function moveProgressTooltip(event) {
  const tip = $("progress-tooltip");
  if (!tip || tip.hidden) return;
  const offset = 14;
  const rect = tip.getBoundingClientRect();
  const left = Math.min(window.innerWidth - rect.width - 10, event.clientX + offset);
  const top = Math.min(window.innerHeight - rect.height - 10, event.clientY + offset);
  tip.style.left = `${Math.max(10, left)}px`;
  tip.style.top = `${Math.max(10, top)}px`;
}

function hideProgressTooltip() {
  const tip = $("progress-tooltip");
  if (tip) tip.hidden = true;
}

function card(d) {
  const status = d.status === "completed" ? `Completed by ${escapeHtml(d.completed_by?.display_name || "someone")} on ${localDate(d.completed_at)}` :
    d.status === "archived" ? `Archived ${localDate(d.archived_at)}` :
    d.assigned_to ? `Claimed by ${escapeHtml(d.assigned_to.display_name)}` : "";
  const blocked = d.status === "active" && (d.blocked_dependencies || []).length > 0;
  return `<button class="dope-card ${blocked ? "is-blocked" : ""}" data-dope="${d.id}">
    <span><h3>${escapeHtml(d.title)}</h3>${status ? `<span class="meta"><span>${status}</span></span>` : ""}</span>
    <span class="card-pills">
      ${d.dependent_count ? `<span class="pill"><i class="ph ph-tree-structure"></i>${d.dependent_count} ${d.dependent_count === 1 ? "dependent" : "dependents"}</span>` : ""}
      <span class="pill"><i class="ph ph-clock"></i>${formatMinutes(d.time_minutes)}</span>
    </span>
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

function linkifyText(value) {
  return escapeHtml(value).replace(/https?:\/\/[^\s<]+/g, (url) => {
    const cleanUrl = url.replace(/[.,;:)\]}]+$/, "");
    const trailing = url.slice(cleanUrl.length);
    return `<a href="${cleanUrl}" target="_blank" rel="noreferrer">${cleanUrl}</a>${trailing}`;
  });
}

function assignmentHistoryLine(d, h) {
  const endedByCompletion = d.status === "completed" && h.unassigned_at === d.completed_at;
  const endLabel = endedByCompletion ? "completed" : "unassigned";
  const reason = !endedByCompletion && h.unassign_reason ? ` because, and i quote "${escapeHtml(h.unassign_reason)}"` : "";
  return `${escapeHtml(h.display_name)} started this on ${fullDate(h.assigned_at)}${h.unassigned_at ? ` and ${endLabel} on ${fullDate(h.unassigned_at)}${reason}` : ""}`;
}

function dependencyPickerHtml(selectedIds = [], excludeId = null) {
  const selected = new Set(selectedIds.map(Number));
  const candidates = state.allDopes
    .filter((d) => d.id !== excludeId && d.status !== "archived")
    .sort((a, b) => a.title.localeCompare(b.title));
  const options = candidates.length ? candidates.map((d) => `
    <label class="dependency-option" data-dependency-option-row>
      <input type="checkbox" data-dependency-option value="${d.id}" ${selected.has(d.id) ? "checked" : ""}>
      <span>
        <strong>${escapeHtml(d.title)}</strong>
        <small>${d.status === "completed" ? "Doped" : "Undoped"} - ${formatMinutes(d.time_minutes)}</small>
      </span>
    </label>
  `).join("") : `<p class="empty mini">No dopes available yet.</p>`;
  return `
    <section class="dependency-picker">
      <button id="dependency-toggle" class="secondary" type="button"><i class="ph ph-link-simple"></i>Add dependencies</button>
      <div id="dependency-panel" class="dependency-panel" hidden>
        <label class="dependency-search"><i class="ph ph-magnifying-glass"></i><input id="dependency-search" name="dope_dependency_query" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder="Search dopes"></label>
        <div class="dependency-list">${options}</div>
      </div>
    </section>
  `;
}

function bindDependencyPicker(expanded = false) {
  const toggle = $("dependency-toggle");
  const panel = $("dependency-panel");
  const search = $("dependency-search");
  if (!toggle || !panel) return;
  panel.hidden = !expanded;
  toggle.onclick = (event) => {
    event.preventDefault();
    panel.hidden = !panel.hidden;
  };
  if (search) {
    search.oninput = () => {
      const query = search.value.trim().toLowerCase();
      document.querySelectorAll("[data-dependency-option-row]").forEach((row) => {
        row.hidden = query && !row.textContent.toLowerCase().includes(query);
      });
    };
  }
}

function selectedDependencyIds() {
  return [...document.querySelectorAll("[data-dependency-option]:checked")].map((input) => Number(input.value));
}

async function openNewDope() {
  await ensureAllDopes(true);
  $("modal-title").hidden = true;
  $("modal-title").textContent = "New Dope";
  $("modal-body").innerHTML = `
    <div class="modal-topbar is-visible"><strong>New Dope</strong><button class="icon-close" value="cancel" aria-label="Close"><i class="ph ph-x"></i></button></div>
    <div class="modal-content">
      <label>Title<input id="new-title" name="dope_new_title" autocomplete="off" autocapitalize="sentences" spellcheck="true" placeholder="Improve onboarding empty state"></label>
      <label>Description<div id="new-description" class="editor" contenteditable="true" data-placeholder="Write details. Paste images directly here."></div></label>
      <label>Time to complete<input id="new-time" name="dope_new_time" autocomplete="off" placeholder="2hr, 30min, 0.5hr"></label>
      ${dependencyPickerHtml()}
    </div>
    <div class="modal-action-bar"><button id="create-dope" class="primary-wide" value="default">Create Dope</button></div>
  `;
  wireEditor($("new-description"));
  bindDependencyPicker();
  $("create-dope").onclick = async (event) => {
    event.preventDefault();
    try {
      await api("/api/dopes", {
        method: "POST",
        body: JSON.stringify({
          title: $("new-title").value,
          description_html: sanitizeHtml($("new-description").innerHTML),
          time_text: $("new-time").value,
          dependency_ids: selectedDependencyIds(),
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

async function openDope(id) {
  let d = state.dopes.find((item) => item.id === id) || state.allDopes.find((item) => item.id === id);
  if (!d) {
    await ensureAllDopes(true);
    d = state.allDopes.find((item) => item.id === id);
  }
  if (!d) return;
  $("modal-title").hidden = true;
  $("modal-title").textContent = d.title;
  const versions = d.versions || [];
  const editCount = Math.max(0, versions.length - 1);
  const activeVersion = versions[0] || { title: d.title, description_html: d.description_html, edited_at: d.created_at, version_number: 1 };
  const versionOptions = versions.map((v) => `<option value="${v.version_number}">v${v.version_number} - ${localDate(v.edited_at)} by ${escapeHtml(v.edited_by?.display_name || "someone")}</option>`).join("");
  const history = d.assignment_history.length ? `
    <section class="history-block">
      <h2>Assignment History</h2>
      <ul class="history">${d.assignment_history.map((h) => `<li>${assignmentHistoryLine(d, h)}</li>`).join("")}</ul>
    </section>
  ` : "";
  const completionNotes = d.completion_description ? `
    <section class="completion-notes">
      <h2>Completion Notes</h2>
      <div class="completion-text">${linkifyText(d.completion_description)}</div>
    </section>
  ` : "";
  const blocked = (d.blocked_dependencies || []).length > 0;
  const sortedDependencies = [...(d.dependencies || [])].sort((a, b) => {
    const aUndoped = a.status !== "completed";
    const bUndoped = b.status !== "completed";
    if (aUndoped !== bUndoped) return aUndoped ? -1 : 1;
    return a.title.localeCompare(b.title);
  });
  const dependencyCount = sortedDependencies.length;
  const undopedCount = sortedDependencies.filter((dep) => dep.status !== "completed").length;
  const dependencyLabel = `${dependencyCount} ${dependencyCount === 1 ? "dependency" : "dependencies"} &bull; ${undopedCount} undoped`;
  const dependencies = dependencyCount ? `
    <section class="dependency-links-wrap">
      <h2>Dependencies</h2>
      <button id="dependency-summary" class="dependency-summary" value="default">
        <span>${dependencyLabel}</span>
        <i class="ph ph-caret-down"></i>
      </button>
      <div id="dependency-links" class="dependency-links" hidden>
        ${sortedDependencies.map((dep) => `<button class="dependency-link ${dep.status !== "completed" ? "is-undoped" : ""}" data-dependency-open="${dep.id}" value="default">
          <span>${escapeHtml(dep.title)}</span>
          <small>${dep.status === "completed" ? "Doped" : "Undoped"}</small>
        </button>`).join("")}
      </div>
    </section>
  ` : "";
  $("modal-body").innerHTML = `
    <div id="modal-topbar" class="modal-topbar"><strong>${escapeHtml(d.title)}</strong><button class="icon-close" value="cancel" aria-label="Close"><i class="ph ph-x"></i></button></div>
    <div class="modal-content">
      <div id="modal-title-sentinel"></div>
      <div class="modal-headline">
        <div>
          <span class="pill"><i class="ph ph-clock"></i>${formatMinutes(d.time_minutes)}</span>
          ${d.assigned_to ? `<span class="pill"><i class="ph ph-user"></i>${escapeHtml(d.assigned_to.display_name)}</span>` : ""}
          ${d.completed_by ? `<span class="pill"><i class="ph ph-check-circle"></i>${escapeHtml(d.completed_by.display_name)} on ${localDate(d.completed_at)}</span>` : ""}
        </div>
        <div class="version-control">
          ${editCount ? `<button id="version-toggle" class="version-button" value="default">${editCount} ${editCount === 1 ? "edit" : "edits"}</button>` : `<span class="version-empty">no edits</span>`}
        </div>
      </div>
      ${editCount ? `<label id="version-picker-wrap" class="version-picker" hidden>Read version<select id="version-picker">${versionOptions}</select></label>` : ""}
      <h2 id="dope-version-title">${escapeHtml(activeVersion.title)}</h2>
      ${dependencies}
      <div id="dope-version-description" class="description">${sanitizeHtml(activeVersion.description_html)}</div>
      ${completionNotes}
      ${history}
    </div>
    <div class="modal-action-bar">
      ${d.status !== "archived" ? `<button id="edit-dope" class="icon-action secondary" value="default" title="Edit"><i class="ph ph-pencil-simple"></i></button>` : ""}
      ${d.status !== "archived" ? `<button id="archive" class="icon-action danger" value="default" title="Archive"><i class="ph ph-archive"></i></button>` : `<button id="restore" class="secondary" value="default"><i class="ph ph-arrow-counter-clockwise"></i>Restore</button>`}
      ${d.status === "active" && d.assigned_to ? `<button id="unassign" class="non-cta" value="default"><i class="ph ph-user-minus"></i>Unassign Dope</button>` : ""}
      ${d.status === "active" && blocked ? `<button class="primary-wide" value="default" disabled><i class="ph ph-warning-circle"></i>Dependencies Undoped</button>` : ""}
      ${d.status === "active" && !blocked && !d.assigned_to ? `<button id="assign" class="primary-wide" value="default"><i class="ph ph-target"></i>I'll take it</button>` : ""}
      ${d.status === "active" && !blocked ? `<button id="complete" class="${d.assigned_to ? "primary-wide" : "secondary action-text"}" value="default"><i class="ph ph-confetti"></i>Doped</button>` : ""}
    </div>
  `;
  document.querySelectorAll("[data-dependency-open]").forEach((el) => {
    el.onclick = (event) => {
      event.preventDefault();
      openDope(Number(el.dataset.dependencyOpen));
    };
  });
  const dependencySummary = $("dependency-summary");
  const dependencyLinks = $("dependency-links");
  if (dependencySummary && dependencyLinks) {
    dependencySummary.onclick = (event) => {
      event.preventDefault();
      dependencyLinks.hidden = !dependencyLinks.hidden;
      dependencySummary.classList.toggle("is-open", !dependencyLinks.hidden);
    };
  }
  bindDopeActions(d);
  bindVersionControls(d);
  $("dope-dialog").showModal();
  bindModalChrome(d.title);
}

function bindDopeActions(d) {
  const closeReload = async (message) => {
    $("dope-dialog").close();
    await loadRoute();
    toast(message);
  };
  const edit = $("edit-dope");
  if (edit) edit.onclick = (event) => { event.preventDefault(); openEditDope(d); };
  const assign = $("assign");
  if (assign) assign.onclick = async (event) => { event.preventDefault(); await api(`/api/dopes/${d.id}/assign`, { method: "POST" }); await closeReload("Dope assigned"); };
  const unassign = $("unassign");
  if (unassign) unassign.onclick = (event) => { event.preventDefault(); openUnassignDope(d); };
  const complete = $("complete");
  if (complete) complete.onclick = (event) => { event.preventDefault(); openCompleteDope(d); };
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

function openUnassignDope(d) {
  $("modal-title").hidden = true;
  $("modal-title").textContent = "Unassign Dope";
  $("modal-body").innerHTML = `
    <div class="modal-topbar is-visible"><strong>Unassign Dope</strong><button class="icon-close" value="cancel" aria-label="Close"><i class="ph ph-x"></i></button></div>
    <div class="modal-content">
      <h2>${escapeHtml(d.title)}</h2>
      <label>Reason<input id="unassign-reason" name="dope_unassign_reason" autocomplete="off" maxlength="500" required placeholder="What blocked this?"></label>
    </div>
    <div class="modal-action-bar">
      <button id="confirm-unassign" class="primary-wide" value="default"><i class="ph ph-user-minus"></i>Unassign Dope</button>
    </div>
  `;
  $("confirm-unassign").onclick = async (event) => {
    event.preventDefault();
    try {
      await api(`/api/dopes/${d.id}/unassign`, {
        method: "POST",
        body: JSON.stringify({ reason: $("unassign-reason").value }),
      });
      $("dope-dialog").close();
      await loadRoute();
      toast("Dope unassigned");
    } catch (err) { toast(err.message); }
  };
  $("unassign-reason").focus();
}

function bindVersionControls(d) {
  const toggle = $("version-toggle");
  const wrap = $("version-picker-wrap");
  const picker = $("version-picker");
  if (!toggle || !wrap || !picker) return;
  toggle.onclick = (event) => {
    event.preventDefault();
    wrap.hidden = !wrap.hidden;
  };
  picker.onchange = () => {
    const version = (d.versions || []).find((v) => String(v.version_number) === picker.value);
    if (!version) return;
    $("dope-version-title").textContent = version.title;
    $("dope-version-description").innerHTML = sanitizeHtml(version.description_html);
  };
}

function bindModalChrome(title) {
  const modal = document.querySelector("#dope-dialog .modal");
  const topbar = $("modal-topbar");
  if (!modal || !topbar) return;
  topbar.classList.remove("is-visible");
  topbar.querySelector("strong").textContent = title;
  modal.onscroll = () => {
    topbar.classList.toggle("is-visible", modal.scrollTop > 90);
  };
}

async function openEditDope(d) {
  await ensureAllDopes(true);
  $("modal-title").hidden = true;
  $("modal-title").textContent = "Edit Dope";
  $("modal-body").innerHTML = `
    <div class="modal-topbar is-visible"><strong>Edit Dope</strong><button class="icon-close" value="cancel" aria-label="Close"><i class="ph ph-x"></i></button></div>
    <div class="modal-content">
      <label>Title<input id="edit-title" name="dope_edit_title" autocomplete="off" autocapitalize="sentences" spellcheck="true" value="${escapeHtml(d.title)}"></label>
      <label>Description<div id="edit-description" class="editor" contenteditable="true" data-placeholder="Write details. Paste images directly here.">${sanitizeHtml(d.description_html)}</div></label>
      ${dependencyPickerHtml((d.dependencies || []).map((dep) => dep.id), d.id)}
    </div>
    <div class="modal-action-bar">
      <button id="save-edit" class="primary-wide" value="default"><i class="ph ph-floppy-disk"></i>Save edit</button>
    </div>
  `;
  wireEditor($("edit-description"));
  bindDependencyPicker((d.dependencies || []).length > 0);
  $("save-edit").onclick = async (event) => {
    event.preventDefault();
    try {
      const updated = await api(`/api/dopes/${d.id}`, {
        method: "PUT",
        body: JSON.stringify({
          title: $("edit-title").value,
          description_html: sanitizeHtml($("edit-description").innerHTML),
          dependency_ids: selectedDependencyIds(),
        }),
      });
      state.dopes = state.dopes.map((item) => item.id === updated.id ? updated : item);
      state.allDopes = state.allDopes.map((item) => item.id === updated.id ? updated : item);
      toast("Edit saved");
      await openDope(updated.id);
    } catch (err) { toast(err.message); }
  };
}

function openCompleteDope(d) {
  $("modal-title").hidden = true;
  $("modal-title").textContent = "Doped";
  $("modal-body").innerHTML = `
    <div class="modal-topbar is-visible"><strong>Doped</strong><button class="icon-close" value="cancel" aria-label="Close"><i class="ph ph-x"></i></button></div>
    <div class="modal-content">
      <h2>${escapeHtml(d.title)}</h2>
      <label>Doped description with commit links<textarea id="completion-text" rows="12" placeholder="Write what changed and include at least one commit link"></textarea></label>
    </div>
    <div class="modal-action-bar">
      <button id="confirm-complete" class="primary-wide" value="default"><i class="ph ph-confetti"></i>Doped</button>
    </div>
  `;
  $("confirm-complete").onclick = async (event) => {
    event.preventDefault();
    try {
      await api(`/api/dopes/${d.id}/complete`, { method: "POST", body: JSON.stringify({ completion_text: $("completion-text").value }) });
      $("dope-dialog").close();
      await loadRoute();
      celebrateDope();
      toast("Dope completed");
    } catch (err) { toast(err.message); }
  };
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
    if (state.authMode === "signup") {
      state.authMode = "login";
      $("auth-notice").textContent = "Account created. Sign in to continue.";
      $("auth-notice").hidden = false;
      updateAuthMode();
      $("auth-display").value = "";
      $("auth-password").focus();
      return;
    }
    state.user = await api("/api/me");
    showApp();
    try {
      await loadRoute();
    } catch (err) {
      toast(err.message || "Could not load dopes");
    }
  } catch (err) { toast(err.message); }
};
$("logout").onclick = async () => { await api("/api/auth/logout", { method: "POST" }); state.user = null; showAuth(); };
$("new-dope").onclick = openNewDope;
$("search").oninput = render;
$("filter-open").onclick = () => {
  $("filter-min").value = state.filters.min;
  $("filter-max").value = state.filters.max;
  $("filter-deps-doped").checked = state.filters.depsDoped;
  $("filter-completed-by").value = state.filters.completedBy;
  $("filter-from").value = state.filters.from;
  $("filter-to").value = state.filters.to;
  $("filter-dialog").showModal();
};
$("filter-apply").onclick = (event) => {
  event.preventDefault();
  state.filters = {
    min: $("filter-min").value,
    max: $("filter-max").value,
    depsDoped: $("filter-deps-doped").checked,
    completedBy: $("filter-completed-by").value,
    from: $("filter-from").value,
    to: $("filter-to").value,
  };
  $("filter-dialog").close();
  render();
};
$("filter-reset").onclick = (event) => {
  event.preventDefault();
  state.filters = { min: "", max: "", depsDoped: false, completedBy: "", from: "", to: "" };
  $("filter-dialog").close();
  render();
};
document.querySelectorAll("[data-progress-days]").forEach((button) => {
  button.onclick = async () => {
    state.progressDays = Number(button.dataset.progressDays);
    if (state.route !== "active") return;
    try {
      state.progress = await api(`/api/stats/progress?days=${state.progressDays}`);
      renderProgressChart();
    } catch (err) { toast(err.message); }
  };
});
$("progress-chart").addEventListener("pointerover", showProgressTooltip);
$("progress-chart").addEventListener("pointermove", moveProgressTooltip);
$("progress-chart").addEventListener("pointerout", (event) => {
  if (!event.relatedTarget || !event.relatedTarget.closest?.("[data-progress-tip]")) hideProgressTooltip();
});
window.addEventListener("hashchange", loadRoute);
init();
