"use strict";

const UNTAGGED = "Untagged";
let config = {
  schemaVersion: 3, account: "", view: { sort: "public-name", categories: [] },
  categories: [], repositories: [], comments: {}, repositoryCategories: {}, lastRefreshedAt: null
};
let baseline = "", baselineComments = {}, baselineTags = {}, revision = null;
let ready = false, saving = false, refreshing = false, dirty = false;
const saveButton = document.getElementById("save-button");
const refreshButton = document.getElementById("refresh-button");
const categoryFilter = document.getElementById("category-filter");
const categoryName = document.getElementById("new-category-name");
const addCategoryButton = document.getElementById("add-category");
const deleteCategoryButton = document.getElementById("delete-category");
const deleteCategorySelect = document.getElementById("delete-category-select");
const statusLabel = document.getElementById("save-status");
const notice = document.getElementById("notice");
const textareas = new Map();
const tagInputs = [];
const cloneControls = new Map();
const cloneStates = new Map();
let cloningRepository = null;
const searchInput = document.getElementById("repository-search");
const clearSearch = document.getElementById("clear-search");
const sortSelect = document.getElementById("sort-order");
const sortButtons = [...document.querySelectorAll("[data-sort-column]")];
let searchQuery = "";

function editableSnapshot(value = config) {
  return JSON.stringify({ view: value.view, comments: value.comments, categories: value.categories, repositoryCategories: value.repositoryCategories });
}
function setStatus(message, state = "") { statusLabel.textContent = message; statusLabel.dataset.state = state; }
function showNotice(message) { notice.textContent = message; notice.hidden = !message; }
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function makeCell(label) { const cell = element("td"); cell.dataset.label = label; return cell; }
function tagsFor(name) { return config.repositoryCategories[name] || []; }
function displayTagsFor(name) { const tags = tagsFor(name); return tags.length ? tags : [UNTAGGED]; }
function ensureEditable() {
  if (!ready || saving || refreshing) throw new Error("Wait for the page to finish loading or saving.");
}
function updateControls() {
  const busy = !ready || saving || refreshing;
  saveButton.disabled = busy || !dirty;
  refreshButton.disabled = busy;
  categoryFilter.disabled = busy;
  searchInput.disabled = busy;
  clearSearch.disabled = busy;
  sortSelect.disabled = busy;
  for (const button of sortButtons) button.disabled = busy;
  categoryName.disabled = busy;
  addCategoryButton.disabled = busy;
  deleteCategorySelect.disabled = busy || !config.categories.length;
  deleteCategoryButton.disabled = busy || !deleteCategorySelect.value;
  for (const textarea of textareas.values()) textarea.disabled = busy;
  for (const input of tagInputs) input.disabled = busy;
  for (const [name, controls] of cloneControls) {
    const state = cloneStates.get(name);
    controls.button.disabled = !ready || cloningRepository !== null || state?.cloned === true;
    controls.button.textContent = cloningRepository === name ? "Cloning..." : state?.cloned ? "Cloned" : state?.error ? "Retry clone" : "Clone";
    controls.status.textContent = cloningRepository === name ? "Cloning into repos/" + name.split("/")[1] + "..." : state?.message || "";
    controls.status.dataset.state = state?.error ? "error" : "";
  }
  saveButton.textContent = saving ? "Saving..." : "Save changes";
  refreshButton.textContent = refreshing ? "Refreshing..." : "Refresh from GitHub";
}
function refreshDirty() {
  dirty = ready && editableSnapshot() !== baseline;
  for (const [name, textarea] of textareas) {
    const edited = (config.comments[name] || "") !== (baselineComments[name] || "") || JSON.stringify(tagsFor(name)) !== JSON.stringify(baselineTags[name] || []);
    textarea.closest("tr").classList.toggle("edited", edited);
  }
  updateControls();
  if (ready && !saving && !refreshing) setStatus(dirty ? "Unsaved changes" : "All changes saved", dirty ? "dirty" : "saved");
}
function orderedRepos(categories = config.view.categories, query = searchQuery) {
  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return config.repositories.filter(repo => {
    if (categories.length && !categories.some(category => displayTagsFor(repo.fullName).includes(category))) return false;
    const searchable = [repo.fullName, repo.description, repo.language].join(" ").toLowerCase();
    return terms.every(term => searchable.includes(term));
  }).sort((a, b) => {
    const name = a.name.localeCompare(b.name, "en", { sensitivity: "base", numeric: true });
    if (config.view.sort === "name-asc") return name;
    if (config.view.sort === "name-desc") return -name;
    if (config.view.sort.startsWith("pushed-")) {
      if (!a.pushedAt || !b.pushedAt) return Number(!a.pushedAt) - Number(!b.pushedAt) || name;
      const date = a.pushedAt.localeCompare(b.pushedAt);
      return (config.view.sort === "pushed-newest" ? -date : date) || name;
    }
    const ranks = config.view.sort === "private-name" ? { private: 0, internal: 1, public: 2 } : { public: 0, internal: 1, private: 2 };
    return ranks[a.visibility] - ranks[b.visibility] || name;
  });
}
function setSort(sort) { ensureEditable(); config.view.sort = sort; render(); }
async function cloneRepository(repo) {
  if (!ready || cloningRepository !== null || cloneStates.get(repo.fullName)?.cloned) return;
  cloningRepository = repo.fullName;
  cloneStates.delete(repo.fullName);
  updateControls();
  try {
    const result = await responseData(await fetch("/api/clone", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repository: repo.fullName }), signal: AbortSignal.timeout(315000)
    }));
    cloneStates.set(repo.fullName, { cloned: true, message: "Saved to " + result.path });
  } catch (error) {
    cloneStates.set(repo.fullName, { error: true, message: error.message });
  } finally {
    cloningRepository = null;
    updateControls();
  }
}
searchInput.addEventListener("input", () => { searchQuery = searchInput.value; render(); });
clearSearch.addEventListener("click", () => { searchQuery = ""; searchInput.value = ""; render(); searchInput.focus(); });
searchInput.addEventListener("keydown", event => { if (event.key === "Escape") { event.preventDefault(); clearSearch.click(); } });
sortSelect.addEventListener("change", () => setSort(sortSelect.value));
for (const button of sortButtons) button.addEventListener("click", () => {
  const orders = { name: ["name-asc", "name-desc"], visibility: ["public-name", "private-name"], pushed: ["pushed-newest", "pushed-oldest"] }[button.dataset.sortColumn];
  setSort(config.view.sort === orders[0] ? orders[1] : orders[0]);
});
function addCategory(name) {
  ensureEditable();
  name = name.trim();
  if (!name || name.length > 80 || /[\r\n\t]/.test(name)) throw new Error("Enter a category name of 1 to 80 characters.");
  if (name.toLowerCase() === UNTAGGED.toLowerCase()) throw new Error("Untagged appears automatically when no categories are assigned.");
  if (config.categories.some(category => category.toLowerCase() === name.toLowerCase())) throw new Error("That category already exists.");
  config.categories.push(name);
  categoryName.value = "";
  showNotice(""); render();
}
function deleteCategory(name) {
  ensureEditable();
  if (!config.categories.includes(name)) throw new Error("That category does not exist.");
  config.categories = config.categories.filter(category => category !== name);
  for (const [repository, tags] of Object.entries(config.repositoryCategories)) config.repositoryCategories[repository] = tags.filter(tag => tag !== name);
  config.view.categories = config.view.categories.filter(category => category !== name);
  showNotice(""); render();
}
function setRepositoryCategory(name, category, assigned) {
  ensureEditable();
  if (!config.repositories.some(repo => repo.fullName === name) || !config.categories.includes(category)) throw new Error("Unknown repository or category.");
  const tags = new Set(tagsFor(name));
  assigned ? tags.add(category) : tags.delete(category);
  config.repositoryCategories[name] = config.categories.filter(tag => tags.has(tag));
  render();
}
function renderCategories() {
  categoryFilter.replaceChildren(element("legend", "", "Filter categories"));
  for (const category of [null, UNTAGGED, ...config.categories]) {
    const count = orderedRepos(category === null ? [] : [category]).length;
    const selected = category === null ? !config.view.categories.length : config.view.categories.includes(category);
    const button = element("button", "filter-chip");
    button.type = "button";
    button.dataset.focusKey = JSON.stringify(["filter", category]);
    button.setAttribute("aria-pressed", String(selected));
    button.append(element("span", "", category ?? "All"), element("span", "chip-count", String(count)));
    button.addEventListener("click", () => {
      ensureEditable();
      config.view.categories = category === null ? [] : !selected ? [...config.view.categories, category] : config.view.categories.filter(name => name !== category);
      render();
    });
    categoryFilter.append(button);
  }
  const previous = deleteCategorySelect.value;
  deleteCategorySelect.replaceChildren();
  for (const category of config.categories) {
    const option = element("option", "", category); option.value = category;
    deleteCategorySelect.append(option);
  }
  if (config.categories.includes(previous)) deleteCategorySelect.value = previous;
}
function renderRows() {
  const body = document.getElementById("repositories-body");
  body.replaceChildren(); textareas.clear(); tagInputs.length = 0; cloneControls.clear();
  const list = orderedRepos();
  document.getElementById("list-title").textContent = config.view.categories.join(", ") || "All repositories";
  document.getElementById("visible-count").textContent = list.length + " of " + config.repositories.length + " repositories";
  document.getElementById("empty-state").hidden = list.length > 0;
  let previousVisibility = null;
  for (const repo of list) {
    const row = element("tr"); row.dataset.repository = repo.fullName; row.dataset.visibility = repo.visibility;
    if (["public-name", "private-name"].includes(config.view.sort) && previousVisibility !== null && previousVisibility !== repo.visibility) row.classList.add("visibility-break");
    previousVisibility = repo.visibility;
    const nameCell = makeCell("Repository"), link = element("a", "repo-link", repo.name);
    link.href = repo.url; link.target = "_blank"; link.rel = "noopener noreferrer";
    nameCell.append(link);
    const metadata = [config.account];
    if (repo.isFork) metadata.push("Fork");
    if (repo.archived) metadata.push("Archived");
    if (repo.disabled) metadata.push("Disabled");
    nameCell.append(element("div", "repo-meta", metadata.join(" / ")));
    const cloneButton = element("button", "clone-button", "Clone");
    cloneButton.type = "button";
    cloneButton.title = "Clone into repos/" + repo.name;
    cloneButton.setAttribute("aria-label", "Clone " + repo.name + " to this computer");
    cloneButton.dataset.focusKey = JSON.stringify(["clone", repo.fullName]);
    cloneButton.addEventListener("click", () => cloneRepository(repo));
    const cloneStatus = element("div", "clone-status");
    cloneStatus.setAttribute("role", "status");
    cloneStatus.setAttribute("aria-live", "polite");
    nameCell.append(cloneButton, cloneStatus);
    cloneControls.set(repo.fullName, { button: cloneButton, status: cloneStatus });
    const descriptionCell = makeCell("Description");
    descriptionCell.append(element("p", repo.description ? "repo-description" : "repo-description missing", repo.description || "No description"));
    const commentCell = makeCell("My Comments"), textarea = element("textarea");
    textarea.value = config.comments[repo.fullName] || "";
    textarea.placeholder = "Add a comment...";
    textarea.setAttribute("aria-label", "My Comments for " + repo.name);
    textarea.rows = 2; textarea.maxLength = 20000;
    textarea.addEventListener("input", () => { config.comments[repo.fullName] = textarea.value; refreshDirty(); });
    commentCell.append(textarea); textareas.set(repo.fullName, textarea);
    const categoryCell = makeCell("Categories"), chips = element("div", "repo-tags");
    const assigned = tagsFor(repo.fullName);
    if (!assigned.length) chips.append(element("span", "untagged-chip", UNTAGGED));
    for (const category of assigned) {
      const chip = element("span", "repo-tag");
      const remove = element("button", "tag-remove", "\u00d7");
      remove.type = "button";
      remove.dataset.focusKey = JSON.stringify(["remove", repo.fullName, category]);
      remove.dataset.focusFallback = JSON.stringify(["add", repo.fullName]);
      remove.title = "Remove " + category;
      remove.setAttribute("aria-label", "Remove " + category + " from " + repo.name);
      remove.addEventListener("click", () => { try { setRepositoryCategory(repo.fullName, category, false); } catch (error) { showNotice(error.message); } });
      chip.append(element("span", "tag-name", category), remove);
      chips.append(chip); tagInputs.push(remove);
    }
    categoryCell.append(chips);
    const available = config.categories.filter(category => !assigned.includes(category));
    if (available.length) {
      const select = element("select", "tag-add");
      select.dataset.focusKey = JSON.stringify(["add", repo.fullName]);
      select.setAttribute("aria-label", "Add category to " + repo.name);
      const placeholder = element("option", "", "+ Add category");
      placeholder.value = ""; placeholder.disabled = true; placeholder.selected = true;
      select.append(placeholder);
      for (const category of available) {
        const option = element("option", "", category); option.value = category; select.append(option);
      }
      select.addEventListener("change", () => { try { setRepositoryCategory(repo.fullName, select.value, true); } catch (error) { select.value = ""; showNotice(error.message); } });
      categoryCell.append(select); tagInputs.push(select);
    } else if (!config.categories.length) {
      categoryCell.append(element("span", "tag-hint", "Create a category above to start tagging."));
    }
    const visibilityCell = makeCell("Visibility");
    visibilityCell.append(element("span", "badge", repo.visibility[0].toUpperCase() + repo.visibility.slice(1)));
    const languageCell = makeCell("Language");
    languageCell.append(element("span", "language", repo.language || "-"));
    const pushedCell = makeCell("Last pushed"), time = element("time", "", repo.pushedAt || "-");
    if (repo.pushedAt) time.dateTime = repo.pushedAt;
    pushedCell.append(time);
    row.append(nameCell, descriptionCell, commentCell, categoryCell, visibilityCell, languageCell, pushedCell);
    body.append(row);
  }
}
function render() {
  const focusKey = document.activeElement?.dataset.focusKey;
  const fallbackKey = document.activeElement?.dataset.focusFallback;
  renderCategories(); renderRows();
  clearSearch.hidden = !searchQuery;
  sortSelect.value = config.view.sort;
  const sortLabels = { "public-name": "Public first, then name", "private-name": "Private first, then name", "pushed-newest": "Last pushed: newest first", "pushed-oldest": "Last pushed: oldest first", "name-asc": "Name: A to Z", "name-desc": "Name: Z to A" };
  document.getElementById("sort-caption").textContent = sortLabels[config.view.sort];
  for (const button of sortButtons) {
    const orders = { name: ["name-asc", "name-desc"], visibility: ["public-name", "private-name"], pushed: ["pushed-oldest", "pushed-newest"] }[button.dataset.sortColumn];
    const direction = config.view.sort === orders[0] ? "ascending" : config.view.sort === orders[1] ? "descending" : "none";
    button.closest("th").setAttribute("aria-sort", direction);
    button.querySelector(".sort-indicator").textContent = direction === "none" ? "↕" : direction === "ascending" ? "↑" : "↓";
  }
  document.getElementById("account-name").textContent = config.account || "No account loaded";
  document.getElementById("total-count").textContent = config.repositories.length;
  document.getElementById("public-count").textContent = config.repositories.filter(repo => repo.visibility === "public").length;
  document.getElementById("private-count").textContent = config.repositories.filter(repo => repo.visibility === "private").length;
  document.getElementById("refreshed-at").textContent = config.lastRefreshedAt ? config.lastRefreshedAt.replace("T", " ").replace("Z", " UTC") : "Not refreshed yet";
  refreshDirty();
  if (focusKey) {
    const controls = [...document.querySelectorAll("[data-focus-key]")];
    const target = controls.find(node => node.dataset.focusKey === focusKey) || controls.find(node => node.dataset.focusKey === fallbackKey) || categoryFilter.querySelector("button");
    target?.focus({ preventScroll: true });
  }
}
function acceptState(result) {
  if (result.config?.schemaVersion !== 3) throw new Error("The older launcher is still running. Close its window, reopen Open-GitHub-Projects.cmd, then reload this page.");
  config = result.config; revision = result.revision;
  baseline = editableSnapshot(); baselineComments = { ...config.comments };
  baselineTags = JSON.parse(JSON.stringify(config.repositoryCategories));
  ready = true; render();
}
async function responseData(response) {
  let result;
  try { result = await response.json(); } catch (_) { throw new Error("The local server could not respond. Reopen the launcher and reload this page."); }
  if (!response.ok) throw new Error(result.error || "The request failed.");
  return result;
}
async function saveConfig(duringRefresh = false) {
  if (!ready || saving || (refreshing && !duringRefresh)) throw new Error("Wait for the current operation to finish.");
  if (!dirty) return { saved: true, changed: false };
  saving = true; updateControls(); setStatus("Saving comments and categories...");
  try {
    const result = await responseData(await fetch("/api/config", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ revision, config }), signal: AbortSignal.timeout(15000) }));
    acceptState(result); showNotice("");
    return { saved: true, changed: true };
  } catch (error) {
    showNotice(error.message + " Your unsaved edits are still on this page.");
    throw error;
  } finally {
    saving = false; refreshDirty();
    if (!notice.hidden) setStatus("Changes have not been saved", "error");
  }
}
async function refreshFromGitHub() {
  ensureEditable();
  refreshing = true; updateControls(); showNotice("");
  let summary = null;
  try {
    if (dirty) await saveConfig(true);
    setStatus("Refreshing from GitHub...");
    const result = await responseData(await fetch("/api/refresh", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ revision }), signal: AbortSignal.timeout(180000) }));
    acceptState(result); summary = result.refresh;
    return summary;
  } catch (error) {
    showNotice(error.message);
    throw error;
  } finally {
    refreshing = false; refreshDirty();
    if (summary) setStatus("Refreshed " + summary.total + " repositories: " + summary.added + " added, " + summary.renamed + " renamed.", "saved");
    else setStatus(dirty ? "Refresh stopped; edits have not been saved" : "Refresh failed; saved inventory kept", "error");
  }
}
async function loadConfig() {
  if (location.protocol === "file:") {
    showNotice("Start the app with Open-GitHub-Projects.cmd on Windows, or run python3 serve.py --open on macOS or Linux from the app folder. Repositories load through the local server.");
    setStatus("Start the local app to load repositories"); return;
  }
  try {
    acceptState(await responseData(await fetch("/api/config", { cache: "no-store", signal: AbortSignal.timeout(10000) })));
  } catch (error) {
    showNotice(error.message); setStatus("Saved configuration unavailable", "error");
  }
}
function addCategoryFromInput() { try { addCategory(categoryName.value); } catch (error) { showNotice(error.message); } }
addCategoryButton.addEventListener("click", addCategoryFromInput);
categoryName.addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); addCategoryFromInput(); } });
deleteCategoryButton.addEventListener("click", () => { try { deleteCategory(deleteCategorySelect.value); } catch (error) { showNotice(error.message); } });
saveButton.addEventListener("click", () => { saveConfig().catch(() => {}); });
refreshButton.addEventListener("click", () => { refreshFromGitHub().catch(() => {}); });
document.addEventListener("keydown", event => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
    event.preventDefault(); if (ready && dirty && !saving && !refreshing) saveConfig().catch(() => {});
  }
});
window.addEventListener("beforeunload", event => { if (dirty) { event.preventDefault(); event.returnValue = ""; } });
render();
const loading = loadConfig();

const modelContext = document.modelContext;
if (modelContext?.registerTool) {
  const lifecycle = new AbortController();
  window.addEventListener("pagehide", () => lifecycle.abort(), { once: true });
  const register = tool => { try { Promise.resolve(modelContext.registerTool(tool, { signal: lifecycle.signal })).catch(() => {}); } catch (_) {} };
  register({ name: "read_project_inventory", title: "Read project inventory", description: "Read the inventory, categories, and comments, including unsaved edits.", inputSchema: { type: "object", properties: {}, additionalProperties: false }, annotations: { readOnlyHint: true, untrustedContentHint: true }, execute: () => ({ saved: !dirty, categories: config.categories, repositories: orderedRepos([], "").map(repo => ({ name: repo.fullName, visibility: repo.visibility, categories: tagsFor(repo.fullName), comment: config.comments[repo.fullName] || "" })) }) });
  register({ name: "stage_project_comments", title: "Stage project comments", description: "Stage comments without saving them. Use save_project_config to write them to the local config.", inputSchema: { type: "object", properties: { comments: { type: "object", additionalProperties: { type: "string", maxLength: 20000 } } }, required: ["comments"], additionalProperties: false }, annotations: { readOnlyHint: false, untrustedContentHint: true }, execute: input => {
    ensureEditable();
    if (!input || !input.comments || typeof input.comments !== "object" || Array.isArray(input.comments)) throw new Error("Provide a comments object.");
    const entries = Object.entries(input.comments);
    for (const [name, value] of entries) if (!config.repositories.some(repo => repo.fullName === name) || typeof value !== "string" || value.length > 20000) throw new Error("Invalid repository or comment: " + name);
    for (const [name, value] of entries) config.comments[name] = value;
    render(); return { staged: entries.length, saved: false };
  } });
  register({ name: "save_project_config", title: "Save project config", description: "Save the current comments, categories, tags, and selected categories to the local config file.", inputSchema: { type: "object", properties: {}, additionalProperties: false }, annotations: { readOnlyHint: false, untrustedContentHint: false }, execute: () => saveConfig() });
}
