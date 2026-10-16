"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const iconPaths = {
    inbox: '<path d="M4 4h16l2 10v6H2v-6L4 4Z"/><path d="M2 14h6l2 3h4l2-3h6"/>',
    mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 6 9 7 9-7"/>',
    unread: '<path d="m3 8 9-5 9 5v12H3V8Z"/><path d="m3 8 9 7 9-7"/>',
    star: '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2L12 17.3l-5.6 2.9 1.1-6.2L3 9.6l6.2-.9Z"/>',
    archive: '<path d="M4 8h16v13H4zM2 3h20v5H2zM9 12h6"/>',
    restore: '<path d="M5 12v9h14v-9M3 8h18v4H3zM12 8V2m-3 3 3-3 3 3"/>',
    work: '<rect x="3" y="7" width="18" height="14" rx="2"/><path d="M8 7V3h8v4M3 12c5 3 13 3 18 0M12 12v4"/>',
    finance: '<path d="M3 9h18L12 3 3 9ZM5 12v6m5-6v6m4-6v6m5-6v6M3 21h18"/>',
    personal: '<circle cx="9" cy="8" r="3"/><path d="M2 21v-3a7 7 0 0 1 14 0v3M17 5a3 3 0 0 1 0 6M22 21v-3a7 7 0 0 0-4-6"/>',
    travel: '<path d="m22 2-7 20-4-9-9-4L22 2ZM22 2 11 13"/>',
    shopping: '<path d="M5 7h14l2 14H3L5 7ZM8 8V5a4 4 0 0 1 8 0v3"/>',
    newsletters: '<path d="M5 3h14v18H5zM8 7h8M8 11h8M8 15h3M8 18h8M14 14h2v2h-2z"/>',
    security: '<path d="m12 2 9 4v6c0 5-9 10-9 10S3 17 3 12V6l9-4Z"/><path d="m8 12 3 3 5-6"/>',
    other: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>',
    close: '<path d="m6 6 12 12M6 18 18 6"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    warning: '<path d="m12 3 10 18H2L12 3ZM12 9v5M12 17h.01"/>',
    lightning: '<path d="m13 2-9 12h7l-1 8 10-12h-7l1-8Z"/>',
    flag: '<path d="M5 21V3c5-4 9 4 14 0v11c-5 4-9-4-14 0"/>',
    leaf: '<path d="M20 3C6 1 1 10 6 16s16 2 14-13ZM5 21 16 10"/>',
    paperclip: '<path d="m8 12 6-6a4 4 0 0 1 6 6L10 22a6 6 0 0 1-8-8L13 3a2 2 0 0 1 3 3L5 17"/>',
    sparkles: '<path d="m12 2 2.5 7.5L22 12l-7.5 2.5L12 22l-2.5-7.5L2 12l7.5-2.5L12 2ZM20 2v4M18 4h4"/>',
    file: '<path d="M5 2h9l5 5v15H5V2ZM14 2v6h5M8 12h8M8 16h8"/>',
    upload: '<path d="M12 15V3m-4 4 4-4 4 4M5 14v6h14v-6"/>',
    sync: '<path d="M20 7v5h-5M4 17v-5h5M5.6 7a7 7 0 0 1 11.8-2L20 8M4 16l2.6 3A7 7 0 0 0 18.4 17"/>',
  };
  const categoryColors = {
    work: ["#edf0e4", "#8e9e73"], finance: ["#f1ecdf", "#b3a06c"],
    personal: ["#f4e9e1", "#bd9a7f"], travel: ["#e8efea", "#87a397"],
    shopping: ["#eee9f0", "#a496ae"], newsletters: ["#f0eddf", "#b2a573"],
    security: ["#e8ece1", "#8e9f79"], other: ["#ecefe9", "#99a38e"],
  };
  const priorityLabels = { critical: "Critical", high: "High priority", normal: "Normal", low: "Low priority" };
  const priorityIcons = { critical: "lightning", high: "flag", normal: "check", low: "leaf" };
  const viewLabels = { inbox: "Inbox", unread: "Unread", starred: "Starred", archived: "Archived" };
  const state = { messages: [], categories: [], summary: null, account: null, mode: "priority", view: "inbox", category: "", priority: "", search: "", loading: false, initialized: false, reader: null, readerRequest: 0, request: 0, controller: null, pending: new Set(), lastFocus: null, resetFocus: null };
  let searchTimer;

  function node(tag, attrs = {}, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") el.className = value;
      else if (key === "text") el.textContent = value;
      else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
      else if (key === "disabled") el.disabled = !!value;
      else el.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of children.flat(Infinity)) {
      if (child === undefined || child === null || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  function icon(name, className = "") {
    const el = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    el.setAttribute("viewBox", "0 0 24 24");
    el.setAttribute("class", `icon ${className}`.trim());
    el.setAttribute("aria-hidden", "true");
    // The only HTML insertion in this app is from this fixed, trusted icon set.
    el.innerHTML = iconPaths[name] || iconPaths.mail;
    return el;
  }

  function button(text, className, action, iconName, attrs = {}) {
    return node("button", { type: "button", class: className, onclick: action, ...attrs }, iconName ? icon(iconName) : null, text);
  }

  function categoryName(id) { return state.categories.find((item) => item.id === id)?.name || (id ? id.charAt(0).toUpperCase() + id.slice(1) : "Other"); }
  function initials(name = "") { return String(name).trim().split(/\s+/).slice(0, 2).map((word) => word[0] || "").join("").toUpperCase() || "?"; }
  function fullDate(value) { const date = new Date(value); return Number.isNaN(date.getTime()) ? "Date unavailable" : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }); }
  function compactDate(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    const difference = Math.max(0, Date.now() - date.getTime());
    if (difference < 60000) return "Just now";
    if (difference < 3600000) return `${Math.floor(difference / 60000)}m ago`;
    if (difference < 86400000) return `${Math.floor(difference / 3600000)}h ago`;
    if (difference < 604800000) return `${Math.floor(difference / 86400000)}d ago`;
    return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  function formatSize(size) { return size >= 1048576 ? `${(size / 1048576).toFixed(1)} MB` : size >= 1024 ? `${Math.round(size / 1024)} KB` : `${size || 0} bytes`; }

  async function api(path, options = {}) {
    const response = await fetch(path, { credentials: "same-origin", ...options, headers: { ...(options.body && typeof options.body === "string" ? { "Content-Type": "application/json" } : {}), ...options.headers } });
    let data;
    try { data = await response.json(); } catch (_) { throw new Error("The server returned an unexpected response. Please try again."); }
    if (!response.ok) throw new Error(data.error || `The request could not be completed (${response.status}).`);
    return data;
  }

  function toast(message, error = false) {
    const el = node("div", { class: `toast${error ? " error" : ""}` }, icon(error ? "warning" : "check"), node("span", {}, message));
    el.append(button("", "toast-close", () => el.remove(), "close", { "aria-label": "Dismiss notification" }));
    $("toast-region").append(el);
    window.setTimeout(() => el.remove(), error ? 8500 : 4500);
  }

  function showError(error) { $("error-text").textContent = error.message || String(error); $("error-banner").hidden = false; }

  async function loadInbox() {
    const request = ++state.request;
    state.controller?.abort();
    state.controller = new AbortController();
    state.loading = true;
    $("messages-content").setAttribute("aria-busy", "true");
    $("refresh-indicator").hidden = !state.initialized;
    const query = new URLSearchParams({ view: state.view });
    if (state.search) query.set("search", state.search);
    if (state.category) query.set("category", state.category);
    if (state.priority && state.priority !== "important") query.set("priority", state.priority);
    try {
      const data = await api(`/api/inbox?${query}`, { signal: state.controller.signal });
      if (request !== state.request) return;
      state.account = data.account;
      state.categories = data.categories;
      state.summary = data.summary;
      state.messages = state.priority === "important" ? data.messages.filter((message) => ["critical", "high"].includes(message.priority)) : data.messages;
      state.initialized = true;
      $("error-banner").hidden = true;
      render();
    } catch (error) {
      if (error.name === "AbortError" || request !== state.request) return;
      showError(error);
      if (!state.initialized) $("messages-content").replaceChildren(emptyState("Your inbox is taking a moment", "We couldn’t load the demo mailbox. Check that the server is running, then try again.", "Try again", loadInbox, "mail"));
    } finally {
      if (request === state.request) {
        state.loading = false;
        $("messages-content").setAttribute("aria-busy", "false");
        $("refresh-indicator").hidden = true;
      }
    }
  }

  function render() {
    renderNavigation();
    renderOverview();
    renderHeading();
    renderMessages();
    $("priority-mode").classList.toggle("active", state.mode === "priority");
    $("all-mode").classList.toggle("active", state.mode === "all");
    $("priority-mode").setAttribute("aria-pressed", String(state.mode === "priority"));
    $("all-mode").setAttribute("aria-pressed", String(state.mode === "all"));
    $("priority-filter").value = state.priority;
    $("account-name").textContent = state.account?.name || "Alex Morgan";
    $("account-email").textContent = state.account?.email || "alex.morgan@example.com";
    const exhausted = state.summary?.demo_batches_remaining === 0;
    $("sync-button").title = exhausted ? "All demo batches are synced. Reset the demo to start again." : "Receive another batch of simulated emails";
    $("sync-button").disabled = exhausted;
    $("sync-button").lastElementChild.textContent = exhausted ? "Demo up to date" : "Simulate sync";
  }

  function createNavItem(view, count) {
    return node("button", { class: `nav-item${state.view === view && !state.category ? " active" : ""}`, "aria-current": state.view === view && !state.category ? "page" : null, onclick: () => setView(view) }, icon(view === "inbox" ? "inbox" : view === "unread" ? "unread" : view === "starred" ? "star" : "archive"), node("span", {}, viewLabels[view]), node("span", { class: "nav-count" }, count ?? "—"));
  }

  function renderOverview() {
    const summary = state.summary;
    if (!summary) return;
    const cards = [
      ["total", "Total messages", summary.total, "A little more organized", "inbox", () => { clearFilters(false); state.view = "inbox"; loadInbox(); }],
      ["attention", "Needs attention", summary.important, "The things that matter", "lightning", () => { clearFilters(false); state.view = "inbox"; state.priority = "important"; loadInbox(); }],
      ["unread", "Unread messages", summary.unread, "Waiting for a moment", "unread", () => { clearFilters(false); state.view = "unread"; loadInbox(); }],
      ["starred", "Starred", summary.starred, "Saved for a closer look", "star", () => { clearFilters(false); state.view = "starred"; loadInbox(); }],
    ];
    $("overview").replaceChildren(...cards.map(([key, label, number, description, iconName, action]) => node("button", { class: `stat-card stat-${key}`, onclick: action, "aria-label": `${number} ${label.toLowerCase()}. Show messages.` }, node("span", {}, node("span", { class: "stat-label" }, label), node("span", { class: "stat-number" }, number), node("span", { class: "stat-description" }, description)), node("span", { class: "stat-icon" }, icon(iconName)))));
  }

  function renderHeading() {
    const category = state.category ? categoryName(state.category) : "";
    const title = category ? `${category}, in focus` : state.view === "inbox" ? "Your inbox, in focus" : state.view === "archived" ? "A little breathing room" : state.view === "starred" ? "Worth keeping close" : "Ready when you are";
    $("page-title").replaceChildren(title, node("span", {}, "."));
    $("heading-kicker").textContent = category ? `${viewLabels[state.view].toUpperCase()} / ${category.toUpperCase()}` : state.view === "inbox" ? "THE BIG PICTURE" : `YOUR ${viewLabels[state.view].toUpperCase()} MESSAGES`;
    $("page-subtitle").textContent = state.view === "archived" ? "Out of your way. Here whenever you need them." : state.view === "starred" ? "The messages you’ve set aside for a closer look." : state.view === "unread" ? "Find your next important conversation, without the noise." : "The important things rise to the top. The rest can wait.";
    const count = state.messages.length;
    $("results-title").textContent = state.search ? `Results for “${state.search}”` : category ? `${category} messages` : state.mode === "priority" ? "A place for everything" : "Everything, at a glance";
    const context = state.view === "archived" ? "archived" : state.view === "unread" ? "unread" : state.view === "starred" ? "starred" : "";
    $("results-description").textContent = state.mode === "priority" ? `${count} ${context ? context + " " : ""}message${count === 1 ? "" : "s"}. Your top 3 in each category, important first.` : `${count} ${context ? context + " " : ""}message${count === 1 ? "" : "s"}, sorted by importance. A little less searching.`;
    $("clear-filters").hidden = !state.search && !state.category && !state.priority;
  }

  function priorityBadge(priority) { return node("span", { class: `priority-badge priority-${priority}` }, icon(priorityIcons[priority] || "check"), priorityLabels[priority] || priority); }

  function starButton(message) {
    return button("", `star-button${message.starred ? " is-starred" : ""}`, () => patchMessage(message.id, { starred: !message.starred }, message.starred ? "Star removed." : "Message starred."), "star", { "aria-label": `${message.starred ? "Unstar" : "Star"} ${message.subject || "message"}`, "aria-pressed": String(message.starred), disabled: state.pending.has(message.id), title: message.starred ? "Remove star" : "Star message" });
  }

  function messageCard(message) {
    const name = message.sender.name || message.sender.address || "Unknown sender";
    const subject = message.subject || "(No subject)";
    const footer = node("div", { class: "message-footer" }, priorityBadge(message.priority));
    const extraLabel = (message.labels || []).find((label) => ![message.category, message.priority].includes(label.toLowerCase()) && label.toLowerCase() !== "priority");
    if (extraLabel) footer.append(node("span", { class: "message-label" }, extraLabel));
    if (message.attachments?.length) footer.append(node("span", { class: "attachment-hint", "aria-label": `${message.attachments.length} attachments` }, icon("paperclip"), message.attachments.length));
    return node("article", { class: `message-card${message.unread ? " unread" : ""}` }, node("button", { class: "message-open", onclick: (event) => openMessage(message.id, event.currentTarget), "aria-label": `Open ${subject}, from ${name}${message.unread ? ", unread" : ""}` }, node("div", { class: "message-head" }, node("span", { class: "sender-avatar", "aria-hidden": "true" }, initials(name)), node("span", { class: "sender-name" }, name), node("time", { class: "message-date", datetime: message.received_at, title: fullDate(message.received_at) }, compactDate(message.received_at))), node("div", { class: "message-subject-line" }, message.unread ? node("span", { class: "unread-dot", "aria-hidden": "true" }) : null, node("span", { class: "message-subject" }, subject)), node("p", { class: "message-preview" }, message.preview || "No preview available."), footer), starButton(message));
  }

  function renderMessages() {
    const content = $("messages-content");
    if (!state.messages.length && (state.search || state.priority || state.view !== "inbox" || state.category)) {
      const noMessages = state.view === "archived" ? ["Space to breathe", "Your archive is empty. Archive a message to keep it here, out of the way."] : state.view === "starred" ? ["Keep the good ones close", "Star a message to give it a little place of its own."] : state.view === "unread" ? ["You’re all caught up", "No unread messages match this view. A little breathing room, well earned."] : ["Nothing here just yet", "No messages match these filters. Try a different search or clear your filters."];
      content.replaceChildren(emptyState(noMessages[0], noMessages[1], state.search || state.priority || state.category ? "Clear filters" : "Back to inbox", () => { if (state.search || state.priority || state.category) clearFilters(); else setView("inbox"); }, state.view === "starred" ? "star" : "inbox"));
      return;
    }
    if (state.mode === "all") {
      const list = node("div", { class: "all-list" }, node("div", { class: "list-heading", "aria-hidden": "true" }, node("span", {}, "From"), node("span", {}, "Message"), node("span", {}, "Priority"), node("span", {}, "Received"), node("span")));
      for (const message of state.messages) list.append(messageRow(message));
      content.replaceChildren(list);
      return;
    }
    const categories = state.category ? state.categories.filter((category) => category.id === state.category) : [...state.categories].sort((a, b) => (state.messages.find((message) => message.category === b.id)?.score ?? -1) - (state.messages.find((message) => message.category === a.id)?.score ?? -1));
    const grid = node("div", { class: "category-grid" });
    for (const category of categories) {
      const messages = state.messages.filter((message) => message.category === category.id);
      const [background, color] = categoryColors[category.id] || categoryColors.other;
      const header = node("header", { class: "category-header" }, node("span", { class: "category-icon", style: `--category-bg:${background};--category-color:${color}` }, icon(category.id)), node("div", { class: "category-title" }, node("h3", {}, category.name), node("span", { class: "category-count", "aria-label": `Showing top ${Math.min(messages.length, 3)} of ${messages.length} messages` }, messages.length > 3 ? `3 of ${messages.length}` : messages.length)), button("View all", "text-button", () => { state.category = category.id; state.mode = "all"; loadInbox(); }, null, { "aria-label": `View all ${category.name} messages` }));
      header.lastChild.append(icon("arrow"));
      grid.append(node("section", { class: "category-panel", "aria-label": `${category.name} priority messages` }, header, messages.length ? messages.slice(0, 3).map(messageCard) : node("div", { class: "category-empty" }, icon("check"), "All clear. Nothing in this category.")));
    }
    content.replaceChildren(grid);
  }

  function messageRow(message) {
    const name = message.sender.name || message.sender.address || "Unknown sender";
    return node("article", { class: `message-row${message.unread ? " unread" : ""}` }, node("button", { class: "row-open", onclick: (event) => openMessage(message.id, event.currentTarget), "aria-label": `Open ${message.subject || "message"}, from ${name}${message.unread ? ", unread" : ""}` }, node("span", { class: "row-sender" }, node("span", { class: "sender-avatar", "aria-hidden": "true" }, initials(name)), node("span", { class: "sender-name" }, name)), node("span", { class: "row-message" }, node("span", { class: "message-subject-line" }, message.unread ? node("span", { class: "unread-dot", "aria-hidden": "true" }) : null, node("span", { class: "message-subject" }, message.subject || "(No subject)")), node("span", { class: "message-preview" }, message.preview || "No preview available."), node("span", { class: "row-category" }, icon(message.category), categoryName(message.category))), priorityBadge(message.priority), node("time", { class: "row-date", datetime: message.received_at, title: fullDate(message.received_at) }, compactDate(message.received_at))), starButton(message));
  }

  function emptyState(title, description, actionLabel, action, iconName) {
    return node("div", { class: "empty-state" }, node("span", { class: "empty-icon" }, icon(iconName || "inbox")), node("h3", {}, title), node("p", {}, description), actionLabel ? button(actionLabel, "button button-outline", action, null) : null);
  }

  function setView(view) { state.view = view; state.category = ""; closeNavigation(); loadInbox(); }
  function clearFilters(load = true) { state.category = ""; state.priority = ""; state.search = ""; $("search").value = ""; $("priority-filter").value = ""; window.clearTimeout(searchTimer); if (load) loadInbox(); }

  async function openMessage(id, trigger) {
    state.lastFocus = trigger || document.activeElement;
    const request = ++state.readerRequest;
    const initial = state.messages.find((message) => message.id === id);
    if (initial) { state.reader = initial; renderReader(); }
    else $("reader-content").replaceChildren(node("div", { class: "initial-loading" }, node("span", { class: "spinner" }), "Opening message…"));
    $("reader-overlay").hidden = false;
    document.body.classList.add("body-locked");
    $("reader").focus();
    try {
      const message = await api(`/api/messages/${encodeURIComponent(id)}`);
      if (request !== state.readerRequest || $("reader-overlay").hidden) return;
      state.reader = message;
      renderReader();
    } catch (error) { if (request === state.readerRequest) { toast(error.message, true); if (!initial) closeReader(); } }
  }

  function closeReader() {
    state.readerRequest++;
    state.reader = null;
    $("reader-overlay").hidden = true;
    if ($("reset-overlay").hidden && !$("sidebar").classList.contains("nav-open")) document.body.classList.remove("body-locked");
    if (state.lastFocus?.isConnected) state.lastFocus.focus(); else $("main-content").focus();
  }

  function renderReader() {
    const message = state.reader;
    if (!message) return;
    const focusedId = $("reader").contains(document.activeElement) ? document.activeElement.id : null;
    const name = message.sender.name || message.sender.address || "Unknown sender";
    const busy = state.pending.has(message.id);
    const categoryChip = node("span", { class: "category-chip" }, icon(message.category), categoryName(message.category));
    const star = button(message.starred ? "Starred" : "Star", `button button-outline${message.starred ? " is-starred" : ""}`, () => patchMessage(message.id, { starred: !message.starred }, message.starred ? "Star removed." : "Message starred."), "star", { id: "reader-star", disabled: busy, "aria-pressed": String(message.starred) });
    const read = button(message.unread ? "Mark as read" : "Mark unread", "button button-outline", () => patchMessage(message.id, { unread: !message.unread }, message.unread ? "Marked as read." : "Marked as unread."), message.unread ? "check" : "unread", { id: "reader-read", disabled: busy });
    const archive = button(message.archived ? "Restore to inbox" : "Archive", "button button-outline", () => patchMessage(message.id, { archived: !message.archived }, message.archived ? "Restored to inbox." : "Message archived."), message.archived ? "restore" : "archive", { id: "reader-archive", disabled: busy });
    const categorySelect = node("select", { id: "reader-category", disabled: busy, onchange: (event) => patchMessage(message.id, { category: event.target.value }, "Category updated.") }, state.categories.map((category) => node("option", { value: category.id }, category.name)));
    categorySelect.value = message.category;
    const prioritySelect = node("select", { id: "reader-priority", disabled: busy, onchange: (event) => patchMessage(message.id, { priority: event.target.value }, "Priority updated.") }, Object.keys(priorityLabels).map((priority) => node("option", { value: priority }, priorityLabels[priority])));
    prioritySelect.value = message.priority;
    const attachments = message.attachments?.length ? node("section", { class: "reader-attachments", "aria-label": "Attachments" }, node("h3", { class: "reader-section-title" }, `Attachments · ${message.attachments.length}`), message.attachments.map((attachment) => node("div", { class: "attachment" }, icon("file"), node("div", {}, node("div", { class: "attachment-name" }, attachment.filename || "Unnamed attachment"), node("div", { class: "attachment-meta" }, `${formatSize(attachment.size)} · ${attachment.content_type || "File"}`))))) : null;
    const reasons = message.reasons?.length ? message.reasons : ["Sorted using local, explainable rules."];
    $("reader-content").replaceChildren(node("div", { class: "reader-toolbar" }, node("span", { class: "reader-toolbar-label" }, "A MOMENT FOR THIS MESSAGE"), button("", "icon-button", closeReader, "close", { id: "close-reader", "aria-label": "Close message" })), node("div", { class: "reader-body" }, node("div", { class: "reader-badges" }, priorityBadge(message.priority), categoryChip, message.overridden ? node("span", { class: "override-chip" }, "Manually sorted") : null), node("h2", { id: "reader-subject" }, message.subject || "(No subject)"), node("div", { class: "reader-sender" }, node("span", { class: "sender-avatar", "aria-hidden": "true" }, initials(name)), node("div", { class: "reader-sender-info" }, node("div", { class: "reader-sender-name" }, name), node("div", { class: "reader-address" }, message.sender.address), node("div", { class: "reader-to" }, `To: ${(message.to || []).map((recipient) => recipient.name ? `${recipient.name} <${recipient.address}>` : recipient.address).join(", ") || "Not specified"}`)), node("time", { class: "reader-received", datetime: message.received_at }, fullDate(message.received_at))), node("div", { class: "reader-actions", "aria-label": "Message actions" }, star, read, archive), node("div", { class: "email-body", "aria-label": "Email body" }, message.text || "This message has no text content."), attachments, node("section", { class: "reader-explanation", "aria-label": "Why this message was prioritized" }, node("div", { class: "explanation-header" }, node("h3", {}, icon("sparkles"), "A little context"), node("span", { class: "score-chip" }, `Priority score ${message.score}/100`)), node("ul", {}, reasons.map((reason) => node("li", {}, reason))), message.labels?.length ? node("div", { class: "reader-labels", "aria-label": "Labels" }, message.labels.map((label) => node("span", { class: "message-label" }, label))) : null), node("section", { class: "reader-overrides", "aria-label": "Change classification" }, node("h3", { class: "reader-section-title" }, "Make it your own"), node("p", { class: "override-description" }, "A different place or priority? Your choices are saved automatically."), node("div", { class: "override-fields" }, node("label", { for: "reader-category" }, "Category", categorySelect), node("label", { for: "reader-priority" }, "Priority", prioritySelect))), node("div", { class: "reader-source" }, icon(message.source === "import" ? "upload" : "inbox"), message.source === "import" ? "Imported from a local .eml file" : "From your simulated mailbox · no account connected")));
    if (focusedId && $(focusedId) && !$(focusedId).disabled) $(focusedId).focus({ preventScroll: true });
  }

  async function patchMessage(id, changes, successMessage) {
    if (state.pending.has(id)) return;
    const focusedControl = state.reader?.id === id ? document.activeElement.id : null;
    state.pending.add(id);
    if (state.reader?.id === id) state.readerRequest++;
    if (state.reader?.id === id) renderReader();
    try {
      const message = await api(`/api/messages/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(changes) });
      state.messages = state.messages.map((item) => item.id === id ? message : item);
      if (state.reader?.id === id) state.reader = message;
      if (successMessage) toast(successMessage);
      await loadInbox();
    } catch (error) { toast(error.message, true); }
    finally {
      state.pending.delete(id);
      if (state.reader?.id === id) {
        renderReader();
        if (focusedControl && $(focusedControl)) $(focusedControl).focus({ preventScroll: true });
      } else if (state.initialized) renderMessages();
    }
  }

  async function syncDemo() {
    const el = $("sync-button");
    if (el.disabled) return;
    el.disabled = true;
    el.lastElementChild.textContent = "Syncing…";
    try {
      const result = await api("/api/demo/sync", { method: "POST", body: "{}" });
      await loadInbox();
      toast(result.added ? `${result.added} new messages, already sorted. ${result.remaining_batches ? "More demo mail is ready whenever you are." : "That’s the last demo batch."}` : "You’re up to date. Reset the demo to explore the mailbox again.");
    } catch (error) { toast(error.message, true); }
    finally { const exhausted = state.summary?.demo_batches_remaining === 0; el.disabled = exhausted; el.lastElementChild.textContent = exhausted ? "Demo up to date" : "Simulate sync"; }
  }

  async function importEmail(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    event.target.value = "";
    if (file.size > 25 * 1024 * 1024) { toast("Choose an .eml file smaller than 25 MB.", true); return; }
    const el = $("import-button");
    el.disabled = true;
    try {
      const result = await api("/api/import", { method: "POST", body: file, headers: { "Content-Type": "message/rfc822" } });
      await loadInbox();
      toast("Email imported and sorted. Importing the same file again won’t create a duplicate.");
      await openMessage(result.message.id, el);
    } catch (error) { toast(error.message, true); }
    finally { el.disabled = false; }
  }

  function openReset() { state.resetFocus = document.activeElement; $("reset-overlay").hidden = false; document.body.classList.add("body-locked"); $("cancel-reset").focus(); }
  function closeReset() { if ($("confirm-reset").disabled) return; $("reset-overlay").hidden = true; if ($("reader-overlay").hidden && !$("sidebar").classList.contains("nav-open")) document.body.classList.remove("body-locked"); state.resetFocus?.focus(); }
  async function resetDemo() {
    $("confirm-reset").disabled = true;
    $("cancel-reset").disabled = true;
    $("confirm-reset").textContent = "Resetting…";
    try {
      await api("/api/demo/reset", { method: "POST", body: "{}" });
      if (!$("reader-overlay").hidden) closeReader();
      await loadInbox();
      $("confirm-reset").disabled = false;
      closeReset();
      toast("A fresh start. Demo messages are reset; your imported emails are still here.");
    } catch (error) { toast(error.message, true); }
    finally { $("confirm-reset").disabled = false; $("cancel-reset").disabled = false; $("confirm-reset").textContent = "Reset demo"; }
  }

  function openNavigation() { $("sidebar").classList.add("nav-open"); $("nav-shade").hidden = false; $("nav-toggle").setAttribute("aria-expanded", "true"); document.body.classList.add("body-locked"); $("sidebar").querySelector("button")?.focus(); }
  function closeNavigation() { const wasOpen = $("sidebar").classList.contains("nav-open"); $("sidebar").classList.remove("nav-open"); $("nav-shade").hidden = true; $("nav-toggle").setAttribute("aria-expanded", "false"); if ($("reader-overlay").hidden && $("reset-overlay").hidden) document.body.classList.remove("body-locked"); if (wasOpen) $("nav-toggle").focus(); }
  function trapFocus(event, container) {
    const items = Array.from(container.querySelectorAll('button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),[tabindex="0"]')).filter((el) => el.offsetParent !== null);
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (event.shiftKey && (document.activeElement === first || !container.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && (document.activeElement === last || !container.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
  }

  // Navigation items have no untrusted markup; build their contents as DOM nodes.
  function renderNavigation() {
    const summary = state.summary || {};
    const counts = { inbox: summary.total, unread: summary.unread, starred: summary.starred, archived: summary.archived };
    $("mailbox-nav").replaceChildren(...Object.keys(viewLabels).map((view) => createNavItem(view, counts[view])));
    $("category-nav").replaceChildren(...state.categories.map((category) => {
      const count = state.summary?.categories.find((item) => item.id === category.id)?.count || 0;
      return node("button", { class: `nav-item${state.category === category.id ? " active" : ""}`, "aria-current": state.category === category.id ? "page" : null, onclick: () => { state.category = category.id; closeNavigation(); loadInbox(); } }, icon(category.id), node("span", {}, category.name), node("span", { class: "nav-count" }, count));
    }));
    $("category-total").textContent = state.categories.length;
  }

  $("priority-mode").addEventListener("click", () => { state.mode = "priority"; if (state.initialized) render(); });
  $("all-mode").addEventListener("click", () => { state.mode = "all"; if (state.initialized) render(); });
  $("priority-filter").append(node("option", { value: "important" }, "Needs attention"));
  $("priority-filter").addEventListener("change", (event) => { state.priority = event.target.value; loadInbox(); });
  $("search").addEventListener("input", (event) => { const value = event.target.value; window.clearTimeout(searchTimer); searchTimer = window.setTimeout(() => { state.search = value.trim(); loadInbox(); }, 240); });
  $("clear-filters").addEventListener("click", () => clearFilters());
  $("retry-button").addEventListener("click", loadInbox);
  $("sync-button").addEventListener("click", syncDemo);
  $("import-button").addEventListener("click", () => $("import-file").click());
  $("import-file").addEventListener("change", importEmail);
  $("reset-demo").addEventListener("click", openReset);
  $("cancel-reset").addEventListener("click", closeReset);
  $("confirm-reset").addEventListener("click", resetDemo);
  $("reset-overlay").addEventListener("click", (event) => { if (event.target === $("reset-overlay")) closeReset(); });
  $("reader-backdrop").addEventListener("click", closeReader);
  $("nav-toggle").addEventListener("click", () => $("sidebar").classList.contains("nav-open") ? closeNavigation() : openNavigation());
  $("nav-shade").addEventListener("click", closeNavigation);
  window.addEventListener("resize", () => { if (window.innerWidth > 800 && $("sidebar").classList.contains("nav-open")) closeNavigation(); });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { if (!$("reset-overlay").hidden) closeReset(); else if (!$("reader-overlay").hidden) closeReader(); else closeNavigation(); }
    if (event.key === "Tab") { if (!$("reset-overlay").hidden) trapFocus(event, $("reset-overlay")); else if (!$("reader-overlay").hidden) trapFocus(event, $("reader")); else if ($("sidebar").classList.contains("nav-open")) trapFocus(event, $("sidebar")); }
    if (event.key === "/" && !event.ctrlKey && !event.metaKey && !event.altKey && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName) && $("reader-overlay").hidden && $("reset-overlay").hidden) { event.preventDefault(); closeNavigation(); $("search").focus(); }
  });

  renderNavigation();
  loadInbox();
})();
