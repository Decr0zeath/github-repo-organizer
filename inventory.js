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
const topicFilter = document.getElementById("topic-filter");
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
let selectedTopics = [], aboutRepository = null, aboutSaving = false;
const aboutButtons = new Map();
const aboutDialog = document.getElementById("about-dialog");
const aboutForm = document.getElementById("about-form");
const aboutDescription = document.getElementById("about-description");
const aboutHomepage = document.getElementById("about-homepage");
const aboutTopics = document.getElementById("about-topics");
const aboutError = document.getElementById("about-error");
const aboutSave = document.getElementById("about-save");
const aboutCancel = document.getElementById("about-cancel");

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
  if (!ready || saving || refreshing || aboutSaving) throw new Error("Wait for the page to finish loading or saving.");
}
function updateControls() {
  const busy = !ready || saving || refreshing || aboutSaving;
  saveButton.disabled = busy || !dirty;
  refreshButton.disabled = busy;
  categoryFilter.disabled = busy;
  topicFilter.disabled = busy;
  for (const [name, button] of aboutButtons) button.disabled = busy || config.repositories.find(repo => repo.fullName === name).archived;
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
function orderedRepos(categories = config.view.categories, query = searchQuery, topics = selectedTopics) {
  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return config.repositories.filter(repo => {
    if (categories.length && !categories.some(category => displayTagsFor(repo.fullName).includes(category))) return false;
    if (topics.length && !topics.some(topic => (repo.topics || []).includes(topic))) return false;
    const searchable = [repo.fullName, repo.description, repo.language, ...(repo.topics || [])].join(" ").toLowerCase();
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
function parsedTopics() {
  return [...new Set(aboutTopics.value.toLowerCase().split(/[\s,]+/).filter(Boolean))];
}
function normalizeHomepage(value) {
  return !/^[a-z][a-z0-9+.-]*:/i.test(value) && !/\s/.test(value) && value.split("/", 1)[0].includes(".") ? "https://" + value : value;
}
function homepageLink(value) {
  const normalized = normalizeHomepage(value);
  if (!/^https?:\/\//i.test(normalized)) return "";
  try { return new URL(normalized).hostname ? normalized : ""; } catch (_) { return ""; }
}
function validateAboutForm() {
  const topics = parsedTopics();
  const message = topics.length > 20 || topics.some(topic => !/^[a-z0-9][a-z0-9-]{0,49}$/.test(topic))
    ? "Use at most 20 topics, each 1 to 50 lowercase letters, digits, or hyphens, starting with a letter or digit." : "";
  aboutTopics.setCustomValidity(message);
  aboutTopics.setAttribute("aria-invalid", String(Boolean(message)));
  document.getElementById("about-topics-error").textContent = message;
  const length = [...aboutDescription.value].length;
  document.getElementById("about-count").textContent = length + " / 350 characters";
  aboutDescription.setCustomValidity(length > 350 ? "Description must be at most 350 characters." : "");
  const homepage = normalizeHomepage(aboutHomepage.value);
  const websiteValid = !homepage || Boolean(homepageLink(homepage)) && !/[\s\x00-\x1f]/.test(homepage) && [...homepage].length <= 255;
  aboutHomepage.setCustomValidity(websiteValid ? "" : "Use an HTTP or HTTPS website of at most 255 characters, or leave empty.");
  document.getElementById("about-website-help").textContent = homepage !== aboutHomepage.value
    ? "This website will be saved with https:// added." : "An HTTP or HTTPS URL, a host or host/path, or leave empty.";
  aboutSave.disabled = aboutSaving || Boolean(message) || length > 350;
}
function openAbout(repo) {
  ensureEditable();
  if (repo.archived) return;
  aboutRepository = repo.fullName;
  document.getElementById("about-title").textContent = "Edit about: " + repo.name;
  aboutDescription.value = repo.description;
  aboutHomepage.value = repo.homepage || "";
  aboutTopics.value = (repo.topics || []).join(", ");
  aboutError.textContent = "";
  validateAboutForm();
  aboutDialog.showModal();
}
function acceptAbout(result) {
  const repo = config.repositories.find(repo => repo.fullName === result.repository?.fullName);
  if (!repo || repo.fullName !== aboutRepository || typeof result.revision !== "string") throw new Error("Unexpected About response. Reload and refresh to sync GitHub's values.");
  for (const field of ["description", "homepage", "topics"]) repo[field] = result.repository[field];
  revision = result.revision;
  render();
}
async function saveAbout(event) {
  event.preventDefault();
  if (aboutSaving) return;
  validateAboutForm();
  if (!aboutForm.reportValidity()) return;
  ensureEditable();
  aboutSaving = true;
  aboutError.textContent = "";
  aboutSave.textContent = "Saving...";
  for (const control of [aboutDescription, aboutHomepage, aboutTopics, aboutCancel, aboutSave]) control.disabled = true;
  updateControls();
  try {
    const response = await fetch("/api/about", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repository: aboutRepository, revision, description: aboutDescription.value, homepage: normalizeHomepage(aboutHomepage.value), topics: parsedTopics() }),
      signal: AbortSignal.timeout(255000)
    });
    const result = await response.json();
    if (response.ok || (result.repository && result.revision)) acceptAbout(result);
    if (!response.ok && result.repository) {
      aboutDescription.value = result.repository.description;
      aboutHomepage.value = result.repository.homepage;
    }
    if (!response.ok) throw new Error(result.error || "Could not save About fields.");
    aboutDialog.close();
  } catch (error) {
    aboutError.textContent = error.message;
  } finally {
    aboutSaving = false;
    for (const control of [aboutDescription, aboutHomepage, aboutTopics, aboutCancel, aboutSave]) control.disabled = false;
    aboutSave.textContent = "Save to GitHub";
    validateAboutForm(); refreshDirty();
  }
}
for (const input of [aboutDescription, aboutHomepage, aboutTopics]) input.addEventListener("input", validateAboutForm);
aboutForm.addEventListener("submit", saveAbout);
aboutCancel.addEventListener("click", () => { if (!aboutSaving) aboutDialog.close(); });
aboutDialog.addEventListener("cancel", event => { if (aboutSaving) event.preventDefault(); });
aboutDialog.addEventListener("close", () => { (aboutButtons.get(aboutRepository) || searchInput).focus(); });
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
function renderTopics() {
  const topics = [...new Set(config.repositories.flatMap(repo => repo.topics || []))].sort();
  selectedTopics = selectedTopics.filter(topic => topics.includes(topic));
  topicFilter.hidden = !topics.length;
  topicFilter.replaceChildren(element("legend", "", "Filter topics"));
  for (const topic of [null, ...topics]) {
    const selected = topic === null ? !selectedTopics.length : selectedTopics.includes(topic);
    const count = orderedRepos(config.view.categories, searchQuery, topic === null ? [] : [topic]).length;
    const button = element("button", "filter-chip");
    button.type = "button";
    button.dataset.focusKey = JSON.stringify(["topic", topic]);
    button.setAttribute("aria-pressed", String(selected));
    button.append(element("span", "", topic ?? "All"), element("span", "chip-count", String(count)));
    button.addEventListener("click", () => {
      ensureEditable();
      selectedTopics = topic === null ? [] : selected ? selectedTopics.filter(name => name !== topic) : [...selectedTopics, topic];
      render();
    });
    topicFilter.append(button);
  }
}
function renderRows() {
  const body = document.getElementById("repositories-body");
  body.replaceChildren(); textareas.clear(); tagInputs.length = 0; cloneControls.clear(); aboutButtons.clear();
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
    const aboutButton = element("button", "clone-button about-button", "Edit about");
    aboutButton.type = "button";
    aboutButton.title = repo.archived ? "Archived repositories cannot be edited." : "Edit GitHub description, website, and topics";
    aboutButton.setAttribute("aria-label", repo.archived ? "Edit about " + repo.name + ": archived repositories cannot be edited." : "Edit about " + repo.name);
    aboutButton.dataset.focusKey = JSON.stringify(["about", repo.fullName]);
    aboutButton.addEventListener("click", () => openAbout(repo));
    aboutButtons.set(repo.fullName, aboutButton);
    nameCell.append(cloneButton, aboutButton, cloneStatus);
    cloneControls.set(repo.fullName, { button: cloneButton, status: cloneStatus });
    const descriptionCell = makeCell("Description");
    descriptionCell.append(element("p", repo.description ? "repo-description" : "repo-description missing", repo.description || "No description"));
    if (repo.homepage) {
      const href = homepageLink(repo.homepage);
      const website = element(href ? "a" : "span", "repo-website", repo.homepage);
      if (href) { website.href = href; website.target = "_blank"; website.rel = "noopener noreferrer"; }
      descriptionCell.append(website);
    }
    if (repo.topics?.length) {
      const topics = element("div", "repo-topics");
      topics.setAttribute("aria-label", "GitHub topics");
      for (const topic of repo.topics) topics.append(element("span", "topic-tag", topic));
      descriptionCell.append(topics);
    }
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
  renderTopics(); renderCategories(); renderRows();
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
  if (result.config?.schemaVersion !== 3) throw new Error("An older version of the app is still running on this port. Stop it (close its window or press Ctrl+C) and start the app again.");
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
  if (!ready || saving || aboutSaving || aboutDialog.open || (refreshing && !duringRefresh)) throw new Error("Wait for the current operation to finish.");
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
  if (aboutDialog.open) throw new Error("Close the About dialog before refreshing.");
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
    event.preventDefault(); if (ready && dirty && !saving && !refreshing && !aboutDialog.open) saveConfig().catch(() => {});
  }
});
window.addEventListener("beforeunload", event => { if (dirty) { event.preventDefault(); event.returnValue = ""; } });
render();
loadConfig();
