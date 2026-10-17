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
  const state = { messages: [], categories: [], summary: null, account: null, classifier: null, classifierBusy: false, classifierPanelKey: "", pollFailures: 0, mode: "priority", view: "inbox", category: "", priority: "", search: "", loading: false, initialized: false, reader: null, readerRequest: 0, request: 0, controller: null, pending: new Set(), lastFocus: null, resetFocus: null };
  Object.assign(state, { accounts: [], accountId: "demo", configuration: null, mailbox: null, selection: 0, selectionWrites: Promise.resolve(), preferencesReady: false, preferenceSnapshots: new Map(), preferenceWrites: new Map(), messageWrites: new Map(), accountDialog: null, accountDialogFocus: null, accountBusy: false, accountError: "", accountOptionsKey: "", mailboxPanelKey: "", syncBusyAccount: null });
  let searchTimer;
  let classificationPollTimer;

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
  function accountPath(path, accountId = state.accountId) { const url = new URL(path, window.location.origin); url.searchParams.set("account", accountId); return `${url.pathname}${url.search}`; }
  function isGmail() { return state.account?.provider === "gmail" || state.account?.mode === "gmail"; }
  function preferences() { return { view: state.view, mode: state.mode, category: state.category, priority: state.priority, search: state.search }; }
  function cacheAccount(id) { try { localStorage.setItem("inbox-atlas.account", id); } catch (_) { /* Browser storage may be disabled. Server selection still persists. */ } }
  function cachedAccount() { try { return localStorage.getItem("inbox-atlas.account"); } catch (_) { return null; } }

  function savePreferences() {
    if (!state.preferencesReady) return Promise.resolve();
    const accountId = state.accountId;
    const changes = preferences();
    const snapshot = JSON.stringify(changes);
    if (state.preferenceSnapshots.get(accountId) === snapshot) return state.preferenceWrites.get(accountId) || Promise.resolve();
    state.preferenceSnapshots.set(accountId, snapshot);
    const previous = state.preferenceWrites.get(accountId) || Promise.resolve();
    const write = previous.catch(() => {}).then(() => api(accountPath("/api/preferences", accountId), { method: "PATCH", body: JSON.stringify(changes), keepalive: true })).catch((error) => {
      if (state.preferenceSnapshots.get(accountId) === snapshot) state.preferenceSnapshots.delete(accountId);
      toast(`Your view could not be saved. ${error.message}`, true);
    });
    state.preferenceWrites.set(accountId, write);
    return write;
  }

  function applyPreferences(saved) {
    const defaults = { view: "inbox", mode: "priority", category: "", priority: "", search: "" };
    const values = { ...defaults, ...saved };
    state.view = Object.hasOwn(viewLabels, values.view) ? values.view : "inbox";
    state.mode = values.mode === "all" ? "all" : "priority";
    state.category = typeof values.category === "string" ? values.category : "";
    state.priority = values.priority === "important" || Object.hasOwn(priorityLabels, values.priority) ? values.priority : "";
    state.search = typeof values.search === "string" ? values.search.slice(0, 500) : "";
    $("search").value = state.search;
    $("priority-filter").value = state.priority;
  }

  async function fetchAccounts() {
    const data = await api("/api/accounts");
    state.accounts = data.accounts || [];
    state.configuration = data.configuration || { configured: false, missing: [] };
    renderAccountChrome();
    return data;
  }

  async function switchAccount(id, options = {}) {
    if (!state.accounts.some((account) => account.id === id)) return;
    savePreferences();
    const selection = ++state.selection;
    state.preferencesReady = false;
    state.controller?.abort();
    ++state.request;
    window.clearTimeout(searchTimer);
    window.clearTimeout(classificationPollTimer);
    if (!$("reader-overlay").hidden) closeReader();
    state.accountId = id;
    state.account = state.accounts.find((account) => account.id === id);
    state.messages = [];
    state.summary = null;
    state.classifier = null;
    state.mailbox = null;
    state.classifierPanelKey = "";
    state.mailboxPanelKey = "";
    state.initialized = false;
    state.loading = true;
    applyPreferences({});
    renderAccountChrome();
    renderNavigation();
    $("classifier-panel").hidden = true;
    $("overview").replaceChildren();
    $("messages-content").replaceChildren(node("div", { class: "initial-loading" }, node("span", { class: "spinner" }), "Opening your mailbox…"));
    closeNavigation();
    cacheAccount(id);
    try {
      const existingWrites = state.preferenceWrites.get(id);
      if (existingWrites) await existingWrites;
      if (selection !== state.selection) return;
      const selectionWrite = state.selectionWrites.catch(() => {}).then(() => api("/api/accounts/select", { method: "POST", body: JSON.stringify({ account_id: id }) }));
      state.selectionWrites = selectionWrite;
      const [, saved] = await Promise.all([
        selectionWrite,
        api(accountPath("/api/preferences", id)),
      ]);
      if (selection !== state.selection) return;
      applyPreferences(saved.preferences || saved);
      state.preferenceSnapshots.set(id, JSON.stringify(preferences()));
      state.preferencesReady = true;
      await loadInbox();
      if (options.connected && selection === state.selection) toast("Gmail connected. The first 72 hours of mail will arrive as the initial sync completes.");
    } catch (error) {
      if (selection !== state.selection) return;
      state.loading = false;
      showError(error);
      $("messages-content").replaceChildren(emptyState("This mailbox is taking a moment", error.message, "Try again", () => switchAccount(id), "mail"));
    }
  }

  async function bootstrap() {
    try {
      const data = await fetchAccounts();
      const url = new URL(window.location.href);
      const outcome = url.searchParams.get("gmail");
      const returnedAccount = outcome === "connected" ? url.searchParams.get("account") : null;
      const candidates = [returnedAccount, cachedAccount(), data.active_account_id, "demo"];
      const selected = candidates.find((id) => state.accounts.some((account) => account.id === id));
      if (outcome) {
        url.searchParams.delete("gmail");
        url.searchParams.delete("account");
        history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
        if (["denied", "cancelled"].includes(outcome)) toast("Gmail connection was cancelled. Your existing messages are unchanged.");
        else if (outcome !== "connected") toast("Gmail could not be connected. Open mailbox settings and try again.", true);
      }
      if (!selected) throw new Error("No mailbox is available. Restart the server and try again.");
      await switchAccount(selected, { connected: outcome === "connected" && selected === returnedAccount });
    } catch (error) {
      showError(error);
      $("messages-content").replaceChildren(emptyState("Let’s open your mailboxes", error.message, "Try again", bootstrap, "inbox"));
    }
  }

  function renderAccountChrome() {
    const optionsKey = JSON.stringify(state.accounts.map((account) => [account.id, account.email, account.connected, account.status]));
    if (optionsKey !== state.accountOptionsKey) {
      state.accountOptionsKey = optionsKey;
      $("account-selector").replaceChildren(...state.accounts.map((account) => node("option", { value: account.id }, account.id === "demo" ? "Demo mailbox" : `${account.email}${account.connected ? "" : " · disconnected"}`)));
    }
    $("account-selector").value = state.accountId;
    const account = state.account;
    const gmail = isGmail();
    const connected = gmail && account?.connected;
    const needsAuth = gmail && (!connected || account.status === "needs_reauth");
    const syncing = gmail && (["queued", "syncing"].includes(state.mailbox?.state) || state.syncBusyAccount === state.accountId);
    $("account-name").textContent = account?.name || (gmail ? "Gmail mailbox" : "Alex Morgan");
    $("account-email").textContent = account?.email || "alex.morgan@example.com";
    $("account-avatar").textContent = initials(account?.name || account?.email || "Alex Morgan");
    $("sidebar-provider").textContent = gmail ? needsAuth ? "Gmail · sign-in needed" : "Gmail · read-only" : "Demo mailbox";
    $("sidebar-provider-note").textContent = gmail ? "Your labels and read state are saved in this app." : "Simulated emails. Your changes are saved.";
    $("provider-pill-text").textContent = gmail ? needsAuth ? "Gmail cached" : "Gmail read-only" : "Demo mode";
    $("provider-pill").classList.toggle("provider-disconnected", needsAuth);
    $("reset-demo").hidden = gmail;
    $("header-account-settings").hidden = !gmail;
    $("mailbox-footer").textContent = gmail ? "Gmail is read-only. Your labels and read state are saved in this app." : "Demo mailbox. Your changes are saved in this app.";
    const exhausted = !gmail && state.summary?.demo_batches_remaining === 0;
    $("sync-button").disabled = gmail ? syncing : exhausted;
    $("sync-button").lastElementChild.textContent = gmail ? needsAuth ? "Reconnect Gmail" : syncing ? "Syncing Gmail…" : "Sync Gmail" : exhausted ? "Demo up to date" : "Simulate sync";
    $("sync-button").title = gmail ? needsAuth ? "Sign in again to resume sync. Cached messages are kept." : "Sync new Gmail messages. Your local choices stay unchanged." : exhausted ? "All demo batches are synced. Reset the demo to start again." : "Receive another batch of simulated emails";
    renderMailboxPanel();
  }

  function renderMailboxPanel() {
    const panel = $("mailbox-panel");
    if (!isGmail()) { panel.hidden = true; state.mailboxPanelKey = ""; return; }
    const account = state.account;
    const mailbox = state.mailbox || { state: "idle" };
    const key = JSON.stringify([account, mailbox, state.syncBusyAccount]);
    if (key === state.mailboxPanelKey) return;
    state.mailboxPanelKey = key;
    const disconnected = !account.connected;
    const needsAuth = disconnected || account.status === "needs_reauth";
    const syncing = ["queued", "syncing"].includes(mailbox.state) || state.syncBusyAccount === state.accountId;
    const failed = !syncing && (mailbox.state === "error" || account.status === "error" || account.status === "needs_reauth");
    const title = needsAuth ? disconnected ? "Your saved Gmail messages are still here" : "Gmail needs you to sign in again" : failed ? "Gmail sync needs attention" : syncing ? account.initial_import_complete ? "Bringing your Gmail up to date" : "Your first 72 hours of mail are on their way" : account.initial_import_complete ? "Gmail is up to date" : "Ready for your first Gmail sync";
    const description = needsAuth ? "Reconnect to resume syncing. Existing messages, labels, priorities, and read state stay in this app." : failed ? (mailbox.error || account.error || "Gmail sync paused. Try syncing again when you’re ready.") : syncing ? `${mailbox.processed || 0} processed · ${mailbox.added || 0} newly received. Your saved choices are preserved.` : account.last_synced_at ? `Last synced ${fullDate(account.last_synced_at)}. New mail is checked automatically.` : "New connections import incoming mail from the last 72 hours, then keep receiving new mail.";
    panel.hidden = false;
    panel.className = `mailbox-panel${failed || needsAuth ? " mailbox-panel-warning" : ""}`;
    panel.replaceChildren(node("span", { class: "mailbox-status-icon" }, syncing ? node("span", { class: "spinner" }) : icon(needsAuth ? "warning" : "mail")), node("div", { class: "mailbox-status-copy" }, node("strong", {}, title), node("p", {}, description), node("span", { class: "mailbox-readonly" }, "Gmail read-only · app labels and read state are local")), button(needsAuth ? "Reconnect" : "Settings", "text-button", () => needsAuth ? openAccountDialog("connect", account.id) : openAccountDialog("settings"), needsAuth ? "sync" : null));
  }

  function openAccountDialog(mode, accountId = null) {
    state.accountDialogFocus = document.activeElement;
    state.accountDialog = { mode, accountId };
    state.accountError = "";
    closeNavigation();
    $("account-overlay").hidden = false;
    document.body.classList.add("body-locked");
    renderAccountDialog();
    $("account-overlay").querySelector(".account-dialog").focus();
    if (mode === "connect" && !state.configuration) refreshGmailConfiguration();
  }

  function closeAccountDialog() {
    if (state.accountBusy) return;
    state.accountDialog = null;
    $("account-overlay").hidden = true;
    if ($("reader-overlay").hidden && $("reset-overlay").hidden && !$("sidebar").classList.contains("nav-open")) document.body.classList.remove("body-locked");
    if (state.accountDialogFocus?.isConnected) state.accountDialogFocus.focus();
  }

  function renderAccountDialog() {
    const dialog = state.accountDialog;
    if (!dialog) return;
    const account = dialog.accountId ? state.accounts.find((item) => item.id === dialog.accountId) : state.account;
    const reconnect = dialog.mode === "connect" && !!dialog.accountId;
    const title = dialog.mode === "disconnect" ? "Disconnect, keep your inbox" : dialog.mode === "settings" ? "Your mailbox, your choices" : reconnect ? "Reconnect Gmail" : "A calmer inbox starts here";
    const close = button("", "icon-button", closeAccountDialog, "close", { "aria-label": "Close mailbox dialog", disabled: state.accountBusy });
    const content = node("div", {}, node("div", { class: "account-dialog-top" }, node("span", { class: "account-dialog-icon" }, icon(dialog.mode === "disconnect" ? "archive" : "mail")), close), node("h2", { id: "account-dialog-title" }, title));
    if (dialog.mode === "connect") {
      content.append(node("p", { class: "account-dialog-description" }, reconnect ? `Sign in as ${account?.email || "your connected Gmail account"} to resume syncing. Your cached messages and choices stay as they are.` : "Connect your Gmail account securely through Google. Your first sync starts with incoming mail from the last 72 hours."));
      content.append(node("ul", { class: "connection-promises" }, node("li", {}, icon("sync"), node("span", {}, node("strong", {}, "A focused first sync"), "Last 72 hours for a new connection, then automatic updates for new mail.")), node("li", {}, icon("security"), node("span", {}, node("strong", {}, "Read-only Gmail access"), "This app never changes messages, labels, or read state in Gmail.")), node("li", {}, icon("check"), node("span", {}, node("strong", {}, "Your choices stay saved"), "Stars, archive, labels, priorities, and read state persist here, across restarts."))));
      const configuration = state.configuration;
      if (!configuration?.configured) {
        content.append(node("section", { class: "gmail-setup-note" }, node("strong", {}, configuration ? "One server setup step first" : "Checking Gmail configuration"), node("p", {}, configuration ? "Google sign-in needs an OAuth client configured on the server. Follow the Gmail setup in the project README, then check again." : "The server’s sign-in configuration is being checked."), configuration?.missing?.length ? node("div", { class: "gmail-config-names" }, configuration.missing.map((name) => node("code", {}, name))) : null, configuration?.redirect_uri ? node("p", { class: "gmail-redirect" }, "Authorized redirect URI: ", node("code", {}, configuration.redirect_uri)) : null));
        content.append(node("div", { class: "dialog-actions" }, button("Close", "button button-outline", closeAccountDialog, null, { disabled: state.accountBusy }), button(state.accountBusy ? "Checking…" : "Check configuration", "button button-primary", refreshGmailConfiguration, "sync", { disabled: state.accountBusy })));
      } else content.append(node("p", { class: "connection-privacy" }, "Message text and attachment names/types are sent to the configured Jev service for classification. Attachment files are not sent."), node("div", { class: "dialog-actions" }, button("Not now", "button button-outline", closeAccountDialog, null, { disabled: state.accountBusy }), button(state.accountBusy ? "Opening Google…" : "Continue with Google", "button button-primary", startGmailConnection, "arrow", { id: "continue-google", disabled: state.accountBusy })));
    } else if (dialog.mode === "disconnect") {
      content.append(node("p", { class: "account-dialog-description" }, `Stop syncing ${account?.email || "this Gmail mailbox"} and remove its saved sign-in tokens from this app. All imported emails, labels, priorities, stars, and read state will remain.`), node("p", { class: "connection-privacy" }, "You can reconnect the same Gmail account later without starting over. This does not change your Gmail account."), node("div", { class: "dialog-actions" }, button("Keep connected", "button button-outline", () => { state.accountDialog = { mode: "settings", accountId: null }; renderAccountDialog(); }, null, { disabled: state.accountBusy }), button(state.accountBusy ? "Disconnecting…" : "Disconnect Gmail", "button button-danger", disconnectGmail, null, { id: "confirm-disconnect", disabled: state.accountBusy })));
    } else {
      const gmail = account?.provider === "gmail";
      content.append(node("p", { class: "settings-account-email" }, account?.email || "Demo mailbox"), node("p", { class: "account-dialog-description" }, gmail ? "Gmail sync is read-only. Your read state, stars, archive, labels, and priorities are stored in this app and survive syncs and restarts." : "This is your simulated mailbox. Messages and your choices are saved locally. Connect Gmail whenever you’re ready."));
      if (gmail) {
        content.append(node("dl", { class: "mailbox-settings-facts" }, node("div", {}, node("dt", {}, "Connection"), node("dd", {}, account.connected ? account.status === "needs_reauth" ? "Sign-in needed" : "Connected · read-only" : "Disconnected · cached messages kept")), node("div", {}, node("dt", {}, "First import"), node("dd", {}, account.initial_import_complete ? "Complete · new mail syncs automatically" : "Incoming mail from the first connection’s last 72 hours")), node("div", {}, node("dt", {}, "Last successful sync"), node("dd", {}, account.last_synced_at ? fullDate(account.last_synced_at) : "Not yet synced"))));
        content.append(node("div", { class: "settings-mailbox-actions" }, button("Sync now", "button button-primary", () => { closeAccountDialog(); syncCurrentAccount(); }, "sync", { disabled: !account.connected || state.syncBusyAccount === account.id || ["queued", "syncing"].includes(state.mailbox?.state) }), button("Reconnect", "button button-outline", () => { state.accountDialog = { mode: "connect", accountId: account.id }; renderAccountDialog(); }, "mail"), account.connected ? button("Disconnect", "button button-subtle-danger", () => { state.accountDialog = { mode: "disconnect", accountId: account.id }; renderAccountDialog(); }) : null));
      } else content.append(node("div", { class: "dialog-actions" }, button("Done", "button button-outline", closeAccountDialog), button("Connect Gmail", "button button-primary", () => { state.accountDialog = { mode: "connect", accountId: null }; renderAccountDialog(); }, "mail")));
    }
    if (state.accountError) content.append(node("div", { class: "account-dialog-error", role: "alert" }, state.accountError));
    $("account-dialog-content").replaceChildren(content);
  }

  async function refreshGmailConfiguration() {
    if (state.accountBusy) return;
    state.accountBusy = true;
    state.accountError = "";
    renderAccountDialog();
    try { await fetchAccounts(); }
    catch (error) { state.accountError = error.message; }
    finally { state.accountBusy = false; renderAccountDialog(); }
  }

  async function startGmailConnection() {
    if (state.accountBusy || !state.configuration?.configured) return;
    state.accountBusy = true;
    state.accountError = "";
    renderAccountDialog();
    try {
      const result = await api("/api/accounts/gmail/start", { method: "POST", body: JSON.stringify(state.accountDialog?.accountId ? { account_id: state.accountDialog.accountId } : {}) });
      const authorization = new URL(result.authorization_url);
      if (authorization.origin !== "https://accounts.google.com" || authorization.username || authorization.password || authorization.hash) throw new Error("The server returned an unexpected Google sign-in address.");
      await savePreferences();
      window.location.assign(authorization.href);
    } catch (error) {
      state.accountError = error.message;
      state.accountBusy = false;
      renderAccountDialog();
    }
  }

  async function disconnectGmail() {
    if (state.accountBusy || !state.accountDialog?.accountId) return;
    const accountId = state.accountDialog.accountId;
    state.accountBusy = true;
    state.accountError = "";
    renderAccountDialog();
    try {
      const result = await api(`/api/accounts/${encodeURIComponent(accountId)}/disconnect`, { method: "POST", body: "{}" });
      state.accounts = state.accounts.map((account) => account.id === accountId ? result.account : account);
      if (state.accountId === accountId) state.account = result.account;
      state.accountBusy = false;
      closeAccountDialog();
      await loadInbox({ quiet: true });
      toast("Gmail disconnected. Your saved messages and choices are still here.");
    } catch (error) { state.accountError = error.message; }
    finally { state.accountBusy = false; renderAccountDialog(); }
  }

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

  async function loadInbox(options = {}) {
    if (!state.preferencesReady) return;
    const quiet = options?.quiet === true;
    if (!quiet) savePreferences();
    const accountId = state.accountId;
    const request = ++state.request;
    const readerId = state.reader?.id;
    const readerToken = state.readerRequest;
    state.controller?.abort();
    state.controller = new AbortController();
    state.loading = true;
    if (!quiet) $("messages-content").setAttribute("aria-busy", "true");
    $("messages-content").setAttribute("aria-live", quiet ? "off" : "polite");
    $("refresh-indicator").hidden = quiet || !state.initialized;
    const query = new URLSearchParams({ account: accountId, view: state.view });
    if (state.search) query.set("search", state.search);
    if (state.category) query.set("category", state.category);
    if (state.priority && state.priority !== "important") query.set("priority", state.priority);
    try {
      const data = await api(`/api/inbox?${query}`, { signal: state.controller.signal });
      if (request !== state.request || accountId !== state.accountId) return;
      state.account = data.account;
      state.accounts = state.accounts.map((account) => account.id === accountId ? { ...account, ...data.account } : account);
      state.mailbox = data.mailbox || { state: "idle" };
      state.classifier = data.classifier || { configured: false, status: "needs_key", total: 0, complete: 0, pending: 0, failed: 0, manual: 0, model: "jev-latest" };
      state.categories = data.categories;
      state.summary = data.summary;
      state.messages = state.priority === "important" ? data.messages.filter((message) => ["critical", "high"].includes(message.priority)) : data.messages;
      state.initialized = true;
      state.pollFailures = 0;
      $("error-banner").hidden = true;
      render({ quiet });
      await refreshReaderClassification(data.messages, readerId, readerToken);
    } catch (error) {
      if (error.name === "AbortError" || request !== state.request) return;
      if (quiet) state.pollFailures++;
      showError(error);
      if (!state.initialized) $("messages-content").replaceChildren(emptyState("Your inbox is taking a moment", "We couldn’t load this mailbox. Check that the server is running, then try again.", "Try again", loadInbox, "mail"));
    } finally {
      if (request === state.request) {
        state.loading = false;
        $("messages-content").setAttribute("aria-busy", "false");
        $("refresh-indicator").hidden = true;
        scheduleClassificationPoll();
      }
    }
  }

  function render(options = {}) {
    const focusedMessage = options.quiet ? document.activeElement.dataset?.messageId : null;
    const focusedAction = options.quiet ? document.activeElement.dataset?.messageAction : null;
    renderNavigation();
    renderOverview();
    renderClassifierPanel();
    renderAccountChrome();
    renderHeading();
    renderMessages();
    if (focusedMessage) Array.from($("messages-content").querySelectorAll("[data-message-id]")).find((element) => element.dataset.messageId === focusedMessage && element.dataset.messageAction === focusedAction)?.focus({ preventScroll: true });
    $("priority-mode").classList.toggle("active", state.mode === "priority");
    $("all-mode").classList.toggle("active", state.mode === "all");
    $("priority-mode").setAttribute("aria-pressed", String(state.mode === "priority"));
    $("all-mode").setAttribute("aria-pressed", String(state.mode === "all"));
    $("priority-filter").value = state.priority;
  }

  function createNavItem(view, count) {
    return node("button", { class: `nav-item${state.view === view && !state.category ? " active" : ""}`, "aria-current": state.view === view && !state.category ? "page" : null, onclick: () => setView(view) }, icon(view === "inbox" ? "inbox" : view === "unread" ? "unread" : view === "starred" ? "star" : "archive"), node("span", {}, viewLabels[view]), node("span", { class: "nav-count" }, count ?? "—"));
  }

  function classificationState(message) {
    const classification = message.classification || { provider: "rules", status: "preview" };
    if (message.overridden || classification.status === "manual") return { type: "manual", label: "Your choice", title: "Your category and priority are preserved. Jev will not replace them." };
    if (classification.provider === "jev" && classification.status === "complete") return { type: "complete", label: "Jev AI", title: `Assessed by ${classification.model || "Jev"}. View the message for decision confidence.` };
    if (classification.status === "failed") return { type: "failed", label: "AI needs retry", title: "Jev could not complete this assessment. The current labels are provisional." };
    if (["pending", "processing", "queued"].includes(classification.status)) return { type: "pending", label: classification.provider === "jev" ? "Jev update pending" : "Preview · AI pending", title: classification.provider === "jev" ? "A previous Jev result is shown while the current assessment is pending." : "Local preview labels are shown while Jev assesses this message." };
    return { type: "preview", label: "Rule preview", title: "This category and priority come from local rules. Jev has not assessed this message." };
  }

  function classificationBadge(message) {
    const assessment = classificationState(message);
    return node("span", { class: `classification-badge classification-${assessment.type}`, title: assessment.title }, icon(assessment.type === "complete" ? "sparkles" : assessment.type === "manual" ? "check" : assessment.type === "failed" ? "warning" : "other"), assessment.label);
  }

  function renderClassifierPanel() {
    const status = state.classifier;
    if (!status) return;
    const signature = JSON.stringify([status, state.classifierBusy]);
    if (signature === state.classifierPanelKey) return;
    state.classifierPanelKey = signature;
    const configured = status.configured === true;
    const processing = configured && status.status !== "error" && (status.status === "processing" || status.pending > 0);
    const error = configured && status.status === "error";
    const total = Math.max(0, Number(status.total || 0) - Number(status.manual || 0));
    const complete = Number(status.complete || 0);
    const model = status.model || "jev-latest";
    const title = !configured ? "Jev is ready when you are" : error ? "Jev assessment paused" : processing ? "Jev is finding what matters" : complete ? "Your inbox, assessed by Jev" : "Jev is ready for new mail";
    const description = !configured ? "Configure TYPESAFE_API_KEY on the server to enable Jev. Unassessed messages show local rule previews." : error ? (status.error || "The AI provider could not complete an assessment. Retry when the connection or configuration is ready.") : processing ? "Typed category and priority decisions are arriving. Pending messages keep their current labels until assessed." : complete ? "Category and priority decisions come from Jev. Your manual choices are always preserved." : "Your manual choices are preserved. New messages will be assessed by Jev.";
    const panel = $("classifier-panel");
    panel.hidden = false;
    panel.className = `classifier-panel classifier-panel-${!configured ? "setup" : error ? "error" : processing ? "processing" : "ready"}`;
    const body = node("div", { class: "classifier-copy" }, node("div", { class: "classifier-heading" }, node("strong", {}, title), node("span", { class: "classifier-model" }, model), node("span", { class: "classifier-state" }, !configured ? "Setup needed" : error ? "Needs attention" : processing ? "Assessing" : "Ready")), node("p", {}, description), configured && !processing ? node("span", { class: "classifier-scope-note" }, "Retry and reclassify apply across mailboxes; manual choices are preserved.") : null);
    if (configured) {
      body.append(node("div", { class: "classifier-progress-meta" }, node("span", {}, `${complete} of ${total} assessed${status.manual ? ` · ${status.manual} kept manual` : ""}${status.failed ? ` · ${status.failed} failed` : ""}`), processing ? node("span", {}, `${status.pending} pending`) : null));
      if (processing) body.append(node("div", { class: "classifier-progress", role: "progressbar", "aria-label": "Jev assessment progress", "aria-valuemin": 0, "aria-valuemax": total || 1, "aria-valuenow": Math.min(complete, total), "aria-valuetext": `${complete} of ${total} assessed` }, node("span", { style: `width:${total ? Math.min(100, Math.round(complete / total * 100)) : 0}%` })));
    }
    const action = !configured ? node("span", { class: "classifier-setup-note" }, "Server setup required") : processing ? node("span", { class: "classifier-working" }, node("span", { class: "spinner" }), "Working in the background") : button(state.classifierBusy ? "Queuing…" : error ? "Retry Jev" : "Reclassify", "button button-outline classifier-action", () => runClassification(error ? "retry" : "run"), error ? "sync" : "sparkles", { id: "classifier-action", disabled: state.classifierBusy });
    panel.replaceChildren(node("span", { class: "classifier-brand", "aria-hidden": "true" }, icon("sparkles"), node("span", {}, "Jev AI")), body, action);
  }

  async function runClassification(action) {
    if (state.classifierBusy) return;
    state.classifierBusy = true;
    renderClassifierPanel();
    try {
      const result = await api(accountPath(`/api/classification/${action}`), { method: "POST", body: "{}" });
      state.classifier = result.classifier || result;
      await loadInbox({ quiet: true });
      toast(action === "retry" ? "Jev assessment resumed across mailboxes. Your manual choices stay as they are." : "Jev assessment queued across mailboxes. Your manual choices stay as they are.");
    } catch (error) { toast(error.message, true); await loadInbox({ quiet: true }); }
    finally { state.classifierBusy = false; renderClassifierPanel(); scheduleClassificationPoll(); }
  }

  function scheduleClassificationPoll() {
    window.clearTimeout(classificationPollTimer);
    const status = state.classifier;
    if (document.visibilityState === "hidden" || !state.preferencesReady) return;
    const aiPending = status?.configured && !["needs_key", "error"].includes(status.status) && (status.status === "processing" || status.pending > 0);
    const mailboxPending = isGmail() && ["queued", "syncing"].includes(state.mailbox?.state);
    const gmailConnected = isGmail() && state.account?.connected;
    if (!aiPending && !mailboxPending && !gmailConnected) return;
    const interval = aiPending || mailboxPending ? 2000 : 15000;
    classificationPollTimer = window.setTimeout(() => {
      if (state.loading || state.pending.size || state.classifierBusy) scheduleClassificationPoll();
      else loadInbox({ quiet: true });
    }, interval * Math.min(4, 1 + state.pollFailures));
  }

  function readerBadges(message) {
    return [priorityBadge(message.priority), node("span", { class: "category-chip" }, icon(message.category), categoryName(message.category)), classificationBadge(message)];
  }

  function readerAssessment(message) {
    const classification = message.classification || { provider: "rules", status: "preview" };
    const assessment = classificationState(message);
    const reasons = message.reasons?.length ? message.reasons : [assessment.type === "manual" ? "Your category and priority have been saved." : "No assessment notes are available."];
    const title = assessment.type === "complete" ? "Jev decision & app rubric" : assessment.type === "manual" ? "Your classification" : assessment.type === "failed" ? "AI assessment incomplete" : "Preview classification";
    const explanation = assessment.type === "complete" ? "Jev returns typed decisions. These notes describe the app’s priority rubric; they are not a model-written explanation." : assessment.type === "manual" ? "Your choices are preserved and excluded from automatic reassessment." : assessment.type === "failed" ? "Jev could not complete the current assessment. Retry from the Jev panel. Current labels remain provisional." : classification.provider === "jev" ? "Showing a previous Jev decision while the updated assessment is pending." : "These labels come from local rules, not Jev. They remain a preview until Jev completes an assessment.";
    const details = node("section", { class: `reader-explanation assessment-${assessment.type}`, id: "reader-assessment", "aria-label": title }, node("div", { class: "explanation-header" }, node("h3", {}, icon(assessment.type === "complete" ? "sparkles" : assessment.type === "manual" ? "check" : "other"), title), node("span", { class: "score-chip" }, `Priority score ${message.score}/100`)), node("p", { class: "assessment-description" }, explanation));
    if (assessment.type === "complete") {
      const confidence = (value) => typeof value === "number" && Number.isFinite(value) ? `${Math.round(value * 100)}%` : "Not available";
      details.append(node("dl", { class: "assessment-facts" }, node("div", {}, node("dt", {}, "Model"), node("dd", {}, classification.model || "Jev")), node("div", {}, node("dt", {}, "Category confidence"), node("dd", {}, confidence(classification.category_confidence))), node("div", {}, node("dt", {}, "Priority confidence"), node("dd", {}, confidence(classification.priority_confidence)))));
      details.append(node("p", { class: "assessment-confidence-note" }, "Confidence measures certainty in the decision, not urgency."));
      if (classification.assessed_at) details.append(node("p", { class: "assessment-timestamp" }, `Assessed ${fullDate(classification.assessed_at)}`));
    }
    details.append(node("ul", {}, reasons.map((reason) => node("li", {}, reason))));
    if (message.labels?.length) details.append(node("div", { class: "reader-labels", "aria-label": "Labels" }, message.labels.map((label) => node("span", { class: "message-label" }, label))));
    return details;
  }

  async function refreshReaderClassification(messages, id, token) {
    if (!id || state.reader?.id !== id || token !== state.readerRequest || state.pending.has(id)) return;
    let updated = messages.find((message) => message.id === id);
    if (!updated) {
      try { updated = await api(accountPath(`/api/messages/${encodeURIComponent(id)}`)); }
      catch (_) { return; }
    }
    if (state.reader?.id !== id || token !== state.readerRequest || state.pending.has(id)) return;
    const fields = ["category", "priority", "score", "labels", "reasons", "classification", "overridden"];
    if (fields.every((field) => JSON.stringify(updated[field]) === JSON.stringify(state.reader[field]))) return;
    for (const field of fields) state.reader[field] = updated[field];
    const scroll = $("reader").scrollTop;
    $("reader-badges")?.replaceChildren(...readerBadges(state.reader));
    $("reader-assessment")?.replaceWith(readerAssessment(state.reader));
    if (document.activeElement !== $("reader-category")) $("reader-category").value = state.reader.category;
    if (document.activeElement !== $("reader-priority")) $("reader-priority").value = state.reader.priority;
    $("reader").scrollTop = scroll;
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
    return button("", `star-button${message.starred ? " is-starred" : ""}`, () => patchMessage(message.id, { starred: !message.starred }, message.starred ? "Star removed." : "Message starred."), "star", { "data-message-id": message.id, "data-message-action": "star", "aria-label": `${message.starred ? "Unstar" : "Star"} ${message.subject || "message"}`, "aria-pressed": String(message.starred), disabled: state.pending.has(message.id), title: message.starred ? "Remove star" : "Star message" });
  }

  function messageCard(message) {
    const name = message.sender.name || message.sender.address || "Unknown sender";
    const subject = message.subject || "(No subject)";
    const footer = node("div", { class: "message-footer" }, priorityBadge(message.priority), classificationBadge(message));
    const extraLabel = (message.labels || []).find((label) => ![message.category, message.priority, "jev ai", "priority"].includes(label.toLowerCase()));
    if (extraLabel && classificationState(message).type === "complete") footer.append(node("span", { class: "message-label additional-message-label" }, extraLabel));
    if (message.attachments?.length) footer.append(node("span", { class: "attachment-hint", "aria-label": `${message.attachments.length} attachments` }, icon("paperclip"), message.attachments.length));
    return node("article", { class: `message-card${message.unread ? " unread" : ""}` }, node("button", { class: "message-open", "data-message-id": message.id, "data-message-action": "open", onclick: (event) => openMessage(message.id, event.currentTarget), "aria-label": `Open ${subject}, from ${name}${message.unread ? ", unread" : ""}` }, node("div", { class: "message-head" }, node("span", { class: "sender-avatar", "aria-hidden": "true" }, initials(name)), node("span", { class: "sender-name" }, name), node("time", { class: "message-date", datetime: message.received_at, title: fullDate(message.received_at) }, compactDate(message.received_at))), node("div", { class: "message-subject-line" }, message.unread ? node("span", { class: "unread-dot", "aria-hidden": "true" }) : null, node("span", { class: "message-subject" }, subject)), node("p", { class: "message-preview" }, message.preview || "No preview available."), footer), starButton(message));
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
    return node("article", { class: `message-row${message.unread ? " unread" : ""}` }, node("button", { class: "row-open", "data-message-id": message.id, "data-message-action": "open", onclick: (event) => openMessage(message.id, event.currentTarget), "aria-label": `Open ${message.subject || "message"}, from ${name}${message.unread ? ", unread" : ""}` }, node("span", { class: "row-sender" }, node("span", { class: "sender-avatar", "aria-hidden": "true" }, initials(name)), node("span", { class: "sender-name" }, name)), node("span", { class: "row-message" }, node("span", { class: "message-subject-line" }, message.unread ? node("span", { class: "unread-dot", "aria-hidden": "true" }) : null, node("span", { class: "message-subject" }, message.subject || "(No subject)")), node("span", { class: "message-preview" }, message.preview || "No preview available."), node("span", { class: "row-category" }, icon(message.category), categoryName(message.category), classificationBadge(message))), priorityBadge(message.priority), node("time", { class: "row-date", datetime: message.received_at, title: fullDate(message.received_at) }, compactDate(message.received_at))), starButton(message));
  }

  function emptyState(title, description, actionLabel, action, iconName) {
    return node("div", { class: "empty-state" }, node("span", { class: "empty-icon" }, icon(iconName || "inbox")), node("h3", {}, title), node("p", {}, description), actionLabel ? button(actionLabel, "button button-outline", action, null) : null);
  }

  function setView(view) { state.view = view; state.category = ""; closeNavigation(); loadInbox(); }
  function clearFilters(load = true) { state.category = ""; state.priority = ""; state.search = ""; $("search").value = ""; $("priority-filter").value = ""; window.clearTimeout(searchTimer); if (load) loadInbox(); }

  async function openMessage(id, trigger) {
    const accountId = state.accountId;
    state.lastFocus = trigger || document.activeElement;
    const request = ++state.readerRequest;
    const initial = state.messages.find((message) => message.id === id);
    if (initial) { state.reader = initial; renderReader(); }
    else $("reader-content").replaceChildren(node("div", { class: "initial-loading" }, node("span", { class: "spinner" }), "Opening message…"));
    $("reader-overlay").hidden = false;
    document.body.classList.add("body-locked");
    $("reader").focus();
    try {
      const previousWrite = state.messageWrites.get(id);
      if (previousWrite) await previousWrite;
      if (request !== state.readerRequest || accountId !== state.accountId || $("reader-overlay").hidden) return;
      const message = await api(accountPath(`/api/messages/${encodeURIComponent(id)}`, accountId));
      if (request !== state.readerRequest || accountId !== state.accountId || $("reader-overlay").hidden) return;
      state.reader = message;
      renderReader();
      await patchMessage(id, { unread: false }, null, { accountId });
    } catch (error) { if (request === state.readerRequest) { toast(error.message, true); if (!initial) closeReader(); } }
  }

  function closeReader() {
    state.readerRequest++;
    state.reader = null;
    $("reader-overlay").hidden = true;
    if ($("reset-overlay").hidden && $("account-overlay").hidden && !$("sidebar").classList.contains("nav-open")) document.body.classList.remove("body-locked");
    if (state.lastFocus?.isConnected) state.lastFocus.focus(); else $("main-content").focus();
  }

  function renderReader() {
    const message = state.reader;
    if (!message) return;
    const focusedId = $("reader").contains(document.activeElement) ? document.activeElement.id : null;
    const name = message.sender.name || message.sender.address || "Unknown sender";
    const busy = state.pending.has(message.id);
    const star = button(message.starred ? "Starred" : "Star", `button button-outline${message.starred ? " is-starred" : ""}`, () => patchMessage(message.id, { starred: !message.starred }, message.starred ? "Star removed." : "Message starred."), "star", { id: "reader-star", disabled: busy, "aria-pressed": String(message.starred) });
    const read = button(message.unread ? "Mark as read" : "Mark unread", "button button-outline", () => patchMessage(message.id, { unread: !message.unread }, message.unread ? "Marked as read." : "Marked as unread."), message.unread ? "check" : "unread", { id: "reader-read", disabled: busy });
    const archive = button(message.archived ? "Restore to inbox" : "Archive", "button button-outline", () => patchMessage(message.id, { archived: !message.archived }, message.archived ? "Restored to inbox." : "Message archived."), message.archived ? "restore" : "archive", { id: "reader-archive", disabled: busy });
    const categorySelect = node("select", { id: "reader-category", disabled: busy, onchange: (event) => patchMessage(message.id, { category: event.target.value }, "Category updated."), onblur: (event) => { if (state.reader?.id === message.id && !state.pending.has(message.id)) event.target.value = state.reader.category; } }, state.categories.map((category) => node("option", { value: category.id }, category.name)));
    categorySelect.value = message.category;
    const prioritySelect = node("select", { id: "reader-priority", disabled: busy, onchange: (event) => patchMessage(message.id, { priority: event.target.value }, "Priority updated."), onblur: (event) => { if (state.reader?.id === message.id && !state.pending.has(message.id)) event.target.value = state.reader.priority; } }, Object.keys(priorityLabels).map((priority) => node("option", { value: priority }, priorityLabels[priority])));
    prioritySelect.value = message.priority;
    const attachments = message.attachments?.length ? node("section", { class: "reader-attachments", "aria-label": "Attachments" }, node("h3", { class: "reader-section-title" }, `Attachments · ${message.attachments.length}`), message.attachments.map((attachment) => node("div", { class: "attachment" }, icon("file"), node("div", {}, node("div", { class: "attachment-name" }, attachment.filename || "Unnamed attachment"), node("div", { class: "attachment-meta" }, `${formatSize(attachment.size)} · ${attachment.content_type || "File"}`))))) : null;
    $("reader-content").replaceChildren(node("div", { class: "reader-toolbar" }, node("span", { class: "reader-toolbar-label" }, "A MOMENT FOR THIS MESSAGE"), button("", "icon-button", closeReader, "close", { id: "close-reader", "aria-label": "Close message" })), node("div", { class: "reader-body" }, node("div", { class: "reader-badges", id: "reader-badges" }, readerBadges(message)), node("h2", { id: "reader-subject" }, message.subject || "(No subject)"), node("div", { class: "reader-sender" }, node("span", { class: "sender-avatar", "aria-hidden": "true" }, initials(name)), node("div", { class: "reader-sender-info" }, node("div", { class: "reader-sender-name" }, name), node("div", { class: "reader-address" }, message.sender.address), node("div", { class: "reader-to" }, `To: ${(message.to || []).map((recipient) => recipient.name ? `${recipient.name} <${recipient.address}>` : recipient.address).join(", ") || "Not specified"}`)), node("time", { class: "reader-received", datetime: message.received_at }, fullDate(message.received_at))), node("div", { class: "reader-actions", "aria-label": "Message actions" }, star, read, archive), node("div", { class: "email-body", "aria-label": "Email body" }, message.text || "This message has no text content."), attachments, readerAssessment(message), node("section", { class: "reader-overrides", "aria-label": "Change classification" }, node("h3", { class: "reader-section-title" }, "Make it your own"), node("p", { class: "override-description" }, "A different place or priority? Your choices are saved automatically."), node("div", { class: "override-fields" }, node("label", { for: "reader-category" }, "Category", categorySelect), node("label", { for: "reader-priority" }, "Priority", prioritySelect))), node("div", { class: "reader-source" }, icon(message.source === "import" ? "upload" : "inbox"), message.source === "import" ? "Imported from a local .eml file" : message.source === "gmail" ? "From Gmail · read-only connection · changes are saved in this app" : "From your simulated mailbox · changes are saved in this app")));
    if (focusedId && $(focusedId) && !$(focusedId).disabled) $(focusedId).focus({ preventScroll: true });
  }

  async function patchMessage(id, changes, successMessage, options = {}) {
    if (state.pending.has(id)) return;
    const accountId = options.accountId || state.accountId;
    let settleWrite;
    const completion = new Promise((resolve) => { settleWrite = resolve; });
    state.messageWrites.set(id, completion);
    const focusedControl = state.reader?.id === id ? document.activeElement.id : null;
    state.pending.add(id);
    if (state.reader?.id === id) state.readerRequest++;
    if (state.reader?.id === id) renderReader();
    try {
      const message = await api(accountPath(`/api/messages/${encodeURIComponent(id)}`, accountId), { method: "PATCH", body: JSON.stringify(changes) });
      if (accountId !== state.accountId) return;
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
      } else if (state.initialized && accountId === state.accountId) renderMessages();
      if (state.messageWrites.get(id) === completion) state.messageWrites.delete(id);
      settleWrite();
    }
  }

  async function syncCurrentAccount() {
    if (!isGmail()) { await syncDemo(); return; }
    if (!state.account.connected || state.account.status === "needs_reauth") { openAccountDialog("connect", state.accountId); return; }
    const accountId = state.accountId;
    if (state.syncBusyAccount === accountId || ["queued", "syncing"].includes(state.mailbox?.state)) return;
    state.syncBusyAccount = accountId;
    renderAccountChrome();
    try {
      const result = await api(`/api/accounts/${encodeURIComponent(accountId)}/sync`, { method: "POST", body: "{}" });
      if (state.accountId === accountId) { state.mailbox = result.sync || result; await loadInbox({ quiet: true }); }
      toast("Gmail sync queued. Your local labels and read state are preserved.");
    } catch (error) { toast(error.message, true); }
    finally { if (state.syncBusyAccount === accountId) state.syncBusyAccount = null; renderAccountChrome(); scheduleClassificationPoll(); }
  }

  async function syncDemo() {
    if (state.accountId !== "demo") return;
    const el = $("sync-button");
    if (el.disabled) return;
    el.disabled = true;
    el.lastElementChild.textContent = "Syncing…";
    try {
      const result = await api("/api/demo/sync", { method: "POST", body: "{}" });
      await loadInbox();
      const assessmentNote = state.classifier?.status === "error" ? "Jev is paused; retry from the Jev panel when ready." : state.classifier?.configured ? "Jev assessment runs in the background; pending labels are previews." : "Local preview labels are shown until Jev is configured.";
      toast(result.added ? `${result.added} new messages received. ${assessmentNote}` : "You’re up to date. Reset the demo to explore the mailbox again.");
    } catch (error) { toast(error.message, true); }
    finally { renderAccountChrome(); }
  }

  async function importEmail(event) {
    const accountId = state.accountId;
    const file = event.target.files?.[0];
    if (!file) return;
    event.target.value = "";
    if (file.size > 25 * 1024 * 1024) { toast("Choose an .eml file smaller than 25 MB.", true); return; }
    const el = $("import-button");
    el.disabled = true;
    try {
      const result = await api(accountPath("/api/import", accountId), { method: "POST", body: file, headers: { "Content-Type": "message/rfc822" } });
      if (accountId !== state.accountId) { toast("Email imported into the mailbox you selected when importing."); return; }
      await loadInbox();
      toast(state.classifier?.status === "error" ? "Email imported. Jev is paused; its assessment status is shown in the reader." : state.classifier?.configured ? "Email imported. Check its Jev assessment status in the reader; your manual choices are preserved." : "Email imported. Unassessed messages use local previews until Jev is configured.");
      await openMessage(result.message.id, el);
    } catch (error) { toast(error.message, true); }
    finally { el.disabled = false; }
  }

  function openReset() { state.resetFocus = document.activeElement; $("reset-overlay").hidden = false; document.body.classList.add("body-locked"); $("cancel-reset").focus(); }
  function closeReset() { if ($("confirm-reset").disabled) return; $("reset-overlay").hidden = true; if ($("reader-overlay").hidden && $("account-overlay").hidden && !$("sidebar").classList.contains("nav-open")) document.body.classList.remove("body-locked"); state.resetFocus?.focus(); }
  async function resetDemo() {
    if (state.accountId !== "demo") return;
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
  function closeNavigation() { const wasOpen = $("sidebar").classList.contains("nav-open"); $("sidebar").classList.remove("nav-open"); $("nav-shade").hidden = true; $("nav-toggle").setAttribute("aria-expanded", "false"); if ($("reader-overlay").hidden && $("reset-overlay").hidden && $("account-overlay").hidden) document.body.classList.remove("body-locked"); if (wasOpen) $("nav-toggle").focus(); }
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

  $("priority-mode").addEventListener("click", () => { state.mode = "priority"; savePreferences(); if (state.initialized) render(); });
  $("all-mode").addEventListener("click", () => { state.mode = "all"; savePreferences(); if (state.initialized) render(); });
  $("priority-filter").append(node("option", { value: "important" }, "Needs attention"));
  $("priority-filter").addEventListener("change", (event) => { state.priority = event.target.value; loadInbox(); });
  $("search").addEventListener("input", (event) => { const value = event.target.value; window.clearTimeout(searchTimer); searchTimer = window.setTimeout(() => { state.search = value.trim(); loadInbox(); }, 240); });
  $("clear-filters").addEventListener("click", () => clearFilters());
  $("retry-button").addEventListener("click", () => state.preferencesReady ? loadInbox() : bootstrap());
  $("sync-button").addEventListener("click", syncCurrentAccount);
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
  document.addEventListener("visibilitychange", () => {
    window.clearTimeout(classificationPollTimer);
    if (document.visibilityState === "visible" && state.initialized) {
      if (!state.loading && !state.pending.size) loadInbox({ quiet: true });
      else scheduleClassificationPoll();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { if (!$("account-overlay").hidden) closeAccountDialog(); else if (!$("reset-overlay").hidden) closeReset(); else if (!$("reader-overlay").hidden) closeReader(); else closeNavigation(); }
    if (event.key === "Tab") { if (!$("account-overlay").hidden) trapFocus(event, $("account-overlay")); else if (!$("reset-overlay").hidden) trapFocus(event, $("reset-overlay")); else if (!$("reader-overlay").hidden) trapFocus(event, $("reader")); else if ($("sidebar").classList.contains("nav-open")) trapFocus(event, $("sidebar")); }
    if (event.key === "/" && !event.ctrlKey && !event.metaKey && !event.altKey && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName) && $("reader-overlay").hidden && $("reset-overlay").hidden && $("account-overlay").hidden) { event.preventDefault(); closeNavigation(); $("search").focus(); }
  });

  $("search").maxLength = 500;
  $("account-selector").addEventListener("change", (event) => switchAccount(event.target.value));
  $("connect-gmail").addEventListener("click", () => openAccountDialog("connect"));
  $("account-settings").addEventListener("click", () => openAccountDialog("settings"));
  $("header-account-settings").addEventListener("click", () => openAccountDialog("settings"));
  $("account-overlay").addEventListener("click", (event) => { if (event.target === $("account-overlay")) closeAccountDialog(); });
  renderNavigation();
  bootstrap();
})();
