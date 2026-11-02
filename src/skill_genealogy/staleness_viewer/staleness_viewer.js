/* Staleness page. Stale means days_stale is above the threshold.
   Orphaned copies stay orphaned at every threshold. */

const ROLE_ORDER = ["registry", "registry_hub", "hub", "repository"];
const BIN_WIDTH = 15;
const OVERVIEW_BARS = 12;

let lineageRows = [];
let repoRows = [];
let lineageById = new Map();
let cache = new Map();
let loadedCount = 0;
let threshold = 30;
let roleFilter = "all";
let currentId = null;
let tableRows = [];
let sortKey = "days";
let sortAsc = false;
let overviewTimer = 0;

function badge(role) {
  const text = roleBadge(role);
  if (!text) {
    return null;
  }
  const span = document.createElement("span");
  span.className = "badge";
  span.textContent = text;
  span.style.borderColor = roleColor(role);
  return span;
}

function showError(message) {
  const box = document.getElementById("load-error");
  box.hidden = false;
  box.textContent = message;
}

function clearError() {
  const box = document.getElementById("load-error");
  box.hidden = true;
  box.textContent = "";
}

function clampDays(value) {
  const number = Math.round(Number(value));
  if (!Number.isFinite(number)) {
    return threshold;
  }
  return Math.min(365, Math.max(0, number));
}

function setThreshold(value) {
  threshold = clampDays(value);
  document.getElementById("stale-number").value = String(threshold);
  document.getElementById("stale-range").value = String(threshold);
  refresh();
}

function formatCount(value) {
  return Number(value || 0).toLocaleString();
}

function formatShare(part, whole) {
  if (!whole) {
    return "n/a";
  }
  return (100 * part / whole).toFixed(1) + "%";
}

function formatMedian(value) {
  if (value == null) {
    return "n/a";
  }
  const rounded = Math.round(value * 10) / 10;
  return String(rounded);
}

function median(values) {
  const nums = values.filter((value) => value != null && Number.isFinite(value)).sort((a, b) => a - b);
  if (!nums.length) {
    return null;
  }
  const mid = Math.floor(nums.length / 2);
  if (nums.length % 2) {
    return nums[mid];
  }
  return (nums[mid - 1] + nums[mid]) / 2;
}

function isStale(row) {
  if (row.orphaned) {
    return false;
  }
  if (row.days_stale == null || row.days_stale === "") {
    return false;
  }
  return Number(row.days_stale) > threshold;
}

function keepRole(row) {
  return roleFilter === "all" || row.repo_role === roleFilter;
}

function sourceCopies(payload) {
  if (!payload) {
    return [];
  }
  if (Array.isArray(payload.copy_days)) {
    return payload.copy_days;
  }
  const rows = [];
  Object.values(payload.copies_by_version || {}).forEach((group) => {
    group.forEach((row) => rows.push(row));
  });
  return rows;
}

function filteredCopies(payload) {
  return sourceCopies(payload).filter(keepRole);
}

function summarise(rows) {
  const orphaned = rows.filter((row) => row.orphaned).length;
  const defined = rows.length - orphaned;
  const stale = rows.filter(isStale).length;
  const days = rows.filter((row) => !row.orphaned && row.days_stale != null).map((row) => Number(row.days_stale));
  return { copies: rows.length, stale, defined, orphaned, median: median(days) };
}

function roleName(role) {
  return roleBadge(role) || role || "unknown";
}

function paintTotals(rows) {
  const totals = summarise(rows);
  const counts = document.getElementById("counts");
  if (!rows.length && !currentId) {
    counts.textContent = lineageRows.length + " lineages · " + repoRows.length + " repos";
    document.getElementById("role-totals").replaceChildren();
    return;
  }
  const tableNote = currentId ? " · table shows the copies with the most days behind" : "";
  counts.textContent =
    formatCount(totals.copies) + " copies | " +
    formatCount(totals.stale) + " stale (" + formatShare(totals.stale, totals.defined) + ") | " +
    "median " + formatMedian(totals.median) + " days behind | " +
    formatCount(totals.orphaned) + " orphaned" + tableNote;
  paintRoleSplit(rows);
}

function paintRoleSplit(rows) {
  const box = document.getElementById("role-totals");
  box.replaceChildren();
  const roles = ROLE_ORDER.filter((role) => rows.some((row) => row.repo_role === role));
  rows.forEach((row) => {
    if (row.repo_role && !roles.includes(row.repo_role)) {
      roles.push(row.repo_role);
    }
  });
  if (!roles.length) {
    return;
  }
  const table = document.createElement("table");
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  ["Role", "Copies", "Stale", "Share", "Median days"].forEach((label) => {
    const cell = document.createElement("th");
    cell.textContent = label;
    headRow.append(cell);
  });
  head.append(headRow);
  const body = document.createElement("tbody");
  roles.forEach((role) => {
    const group = rows.filter((row) => row.repo_role === role);
    const totals = summarise(group);
    const line = document.createElement("tr");
    [roleName(role), formatCount(totals.copies), formatCount(totals.stale), formatShare(totals.stale, totals.defined), formatMedian(totals.median)].forEach((text) => {
      const cell = document.createElement("td");
      cell.textContent = text;
      line.append(cell);
    });
    body.append(line);
  });
  table.append(head, body);
  box.append(table);
}

function chartBox(id) {
  const host = document.getElementById(id);
  const width = Math.max(320, host.clientWidth || 640);
  return { host, width };
}

function clearChart(host) {
  host.replaceChildren();
}

function drawOverview() {
  const { host, width } = chartBox("overview-chart");
  clearChart(host);
  const note = document.getElementById("overview-note");
  if (loadedCount < lineageRows.length) {
    note.textContent = "Loaded " + loadedCount + " of " + lineageRows.length + " lineages. Bars use the ones loaded so far.";
  } else {
    note.textContent = "No lineage is open. The bars use the threshold above. Click a bar to open that lineage.";
  }
  const ranked = lineageRows.map((row) => {
    const payload = cache.get(row.id);
    const totals = summarise(payload ? filteredCopies(payload) : []);
    return { row, stale: totals.stale, copies: payload ? totals.copies : row.copies };
  }).filter((item) => cache.has(item.row.id));
  ranked.sort((a, b) => b.stale - a.stale || b.copies - a.copies || a.row.id.localeCompare(b.row.id));
  const top = ranked.slice(0, OVERVIEW_BARS);
  if (!top.length) {
    return;
  }
  const height = 36 * top.length + 24;
  const margin = { top: 8, right: 16, bottom: 16, left: 280 };
  const inner = Math.max(40, width - margin.left - margin.right);
  const scale = d3.scaleLinear().domain([0, d3.max(top, (item) => item.stale) || 1]).range([0, inner]);
  const svg = d3.select(host).append("svg").attr("height", height).attr("role", "img");
  svg.append("title").text("Top lineages by stale copies at " + threshold + " days");
  const layer = svg.append("g").attr("transform", "translate(" + margin.left + "," + margin.top + ")");
  top.forEach((item, index) => {
    const y = index * 36;
    layer.append("text").attr("class", "chart-label").attr("x", -8).attr("y", y + 18).attr("text-anchor", "end").text(shortLabel(item.row.id));
    layer.append("rect")
      .attr("class", "bar-stale")
      .attr("x", 0)
      .attr("y", y + 6)
      .attr("width", scale(item.stale))
      .attr("height", 18)
      .style("cursor", "pointer")
      .on("mouseenter", (event) => {
        showTooltip(item.row.id + "\n" + formatCount(item.stale) + " stale of " + formatCount(item.copies), event.clientX, event.clientY);
      })
      .on("mouseleave", hideTooltip)
      .on("click", () => openLineage(item.row));
    layer.append("text").attr("class", "chart-label").attr("x", scale(item.stale) + 6).attr("y", y + 20).text(formatCount(item.stale));
  });
}

function shortLabel(text) {
  if (text.length <= 42) {
    return text;
  }
  return text.slice(0, 39) + "...";
}

function versionBars(payload, rows) {
  const versions = (payload.versions || []).slice().sort((a, b) => a.seq - b.seq);
  const bars = versions.map((version) => countHeld(rows, version.seq, version));
  const unheld = rows.filter((row) => row.held_seq == null);
  if (unheld.length) {
    bars.push(countHeld(rows, null, { seq: null, release_date: null, missed_risky_kinds: [] }));
  }
  return bars;
}

function countHeld(rows, seq, version) {
  const group = rows.filter((row) => (seq == null ? row.held_seq == null : Number(row.held_seq) === Number(seq)));
  return {
    label: seq == null ? "no held version" : "v" + seq,
    date: version.release_date || "",
    kinds: version.missed_risky_kinds || [],
    fresh: group.filter((row) => !row.orphaned && !isStale(row)).length,
    stale: group.filter(isStale).length,
    orphaned: group.filter((row) => row.orphaned).length,
  };
}

function drawTimeline() {
  const payload = cache.get(currentId);
  const { host, width } = chartBox("timeline");
  clearChart(host);
  if (!payload) {
    return;
  }
  const bars = versionBars(payload, filteredCopies(payload));
  const height = Math.max(48, 36 * bars.length + 16);
  const margin = { top: 8, right: 160, bottom: 8, left: 120 };
  const inner = Math.max(40, width - margin.left - margin.right);
  const maxCount = d3.max(bars, (bar) => bar.fresh + bar.stale + bar.orphaned) || 1;
  const scale = d3.scaleLinear().domain([0, maxCount]).range([0, inner]);
  const svg = d3.select(host).append("svg").attr("height", height).attr("role", "img");
  svg.append("title").text("Copies holding each registry version");
  const layer = svg.append("g").attr("transform", "translate(" + margin.left + "," + margin.top + ")");
  bars.forEach((bar, index) => {
    const y = index * 36;
    let x = 0;
    layer.append("text").attr("class", "chart-label").attr("x", -8).attr("y", y + 18).attr("text-anchor", "end").text(bar.label);
    ["stale", "fresh", "orphaned"].forEach((kind) => {
      const count = bar[kind];
      if (!count) {
        return;
      }
      const widthPx = scale(count);
      layer.append("rect")
        .attr("class", "bar-" + kind)
        .attr("x", x)
        .attr("y", y + 6)
        .attr("width", Math.max(widthPx, 1))
        .attr("height", 18)
        .on("mouseenter", (event) => showTooltip(bar.label + " · " + kind + " " + formatCount(count), event.clientX, event.clientY))
        .on("mouseleave", hideTooltip);
      x += widthPx;
    });
    const total = bar.fresh + bar.stale + bar.orphaned;
    layer.append("text").attr("class", "chart-label").attr("x", x + 6).attr("y", y + 20).text(formatCount(total));
    if (bar.kinds.length) {
      const marker = layer.append("text").attr("class", "marker").attr("x", x + 48).attr("y", y + 20).text("risky update missed");
      marker
        .on("mouseenter", (event) => showTooltip("Risky update missed: " + bar.kinds.join(", "), event.clientX, event.clientY))
        .on("mouseleave", hideTooltip);
    }
  });
}

function drawHistogram() {
  const payload = cache.get(currentId);
  const { host, width } = chartBox("histogram");
  clearChart(host);
  const note = document.getElementById("histogram-note");
  if (!payload) {
    note.textContent = "";
    return;
  }
  const rows = filteredCopies(payload);
  const noMiss = rows.filter((row) => !row.orphaned && (row.days_stale == null || row.days_stale === "")).length;
  const orphaned = rows.filter((row) => row.orphaned).length;
  note.textContent =
    formatCount(noMiss) + " copies have no missed release and are not in the chart. " +
    formatCount(orphaned) + " orphaned copies are not in the chart. The line is the threshold.";
  const days = rows.filter((row) => !row.orphaned && row.days_stale != null).map((row) => Math.min(360, Number(row.days_stale)));
  if (!days.length) {
    return;
  }
  const height = 220;
  const margin = { top: 12, right: 16, bottom: 32, left: 44 };
  const innerWidth = Math.max(40, width - margin.left - margin.right);
  const innerHeight = height - margin.top - margin.bottom;
  const thresholds = d3.range(0, 375, BIN_WIDTH);
  const bin = d3.bin().domain([0, 360]).thresholds(thresholds);
  const staleBins = bin(days.filter((day) => day > threshold));
  const freshBins = bin(days.filter((day) => day <= threshold));
  const maxCount = d3.max(staleBins, (item, index) => item.length + freshBins[index].length) || 1;
  const x = d3.scaleLinear().domain([0, 360]).range([0, innerWidth]);
  const y = d3.scaleLinear().domain([0, maxCount]).range([innerHeight, 0]);
  const svg = d3.select(host).append("svg").attr("height", height).attr("role", "img");
  svg.append("title").text("Histogram of days behind. The line is " + threshold + " days.");
  const layer = svg.append("g").attr("transform", "translate(" + margin.left + "," + margin.top + ")");
  freshBins.forEach((item, index) => {
    const stale = staleBins[index];
    const freshHeight = innerHeight - y(item.length);
    const staleHeight = innerHeight - y(stale.length);
    const barX = x(item.x0);
    const barWidth = Math.max(0, x(item.x1) - x(item.x0) - 1);
    layer.append("rect").attr("class", "bar-fresh").attr("x", barX).attr("y", y(item.length)).attr("width", barWidth).attr("height", freshHeight);
    layer.append("rect").attr("class", "bar-stale").attr("x", barX).attr("y", y(item.length + stale.length)).attr("width", barWidth).attr("height", staleHeight);
  });
  layer.append("g").attr("transform", "translate(0," + innerHeight + ")").call(d3.axisBottom(x).ticks(8));
  layer.append("g").call(d3.axisLeft(y).ticks(4).tickFormat(d3.format("~s")));
  const lineX = x(Math.min(360, threshold));
  layer.append("line").attr("class", "threshold-line").attr("x1", lineX).attr("x2", lineX).attr("y1", 0).attr("y2", innerHeight);
}

function tableRecords(payload) {
  const rows = [];
  const groups = payload.copies_by_version || {};
  Object.keys(groups).sort(groupOrder).forEach((key) => {
    groups[key].forEach((row) => rows.push(row));
  });
  return rows;
}

function groupOrder(a, b) {
  if (a === "none") {
    return 1;
  }
  if (b === "none") {
    return -1;
  }
  return Number(a) - Number(b);
}

function sortValue(row, key) {
  if (key === "repo") {
    return row.repo || "";
  }
  if (key === "held") {
    return row.held_seq == null ? -1 : Number(row.held_seq);
  }
  if (key === "behind") {
    return Number(row.versions_behind) || 0;
  }
  if (key === "days") {
    return row.days_stale == null ? null : Number(row.days_stale);
  }
  if (key === "change") {
    return row.last_change || "";
  }
  if (key === "risky") {
    return Number(row.missed_risky) || 0;
  }
  return Number(row.missed_script_updates) || 0;
}

function sortedRows() {
  const rows = tableRows.filter(keepRole).slice();
  rows.sort((a, b) => {
    const left = sortValue(a, sortKey);
    const right = sortValue(b, sortKey);
    if (left == null && right == null) {
      return 0;
    }
    if (left == null) {
      return 1;
    }
    if (right == null) {
      return -1;
    }
    if (left < right) {
      return sortAsc ? -1 : 1;
    }
    if (left > right) {
      return sortAsc ? 1 : -1;
    }
    return 0;
  });
  return rows;
}

function buildTable() {
  const body = document.querySelector("#copy-table tbody");
  body.replaceChildren();
  sortedRows().forEach((row) => {
    const line = document.createElement("tr");
    line.dataset.role = row.repo_role || "";
    line.dataset.days = row.days_stale == null ? "" : String(row.days_stale);
    line.dataset.orphaned = row.orphaned ? "true" : "false";
    const repo = document.createElement("td");
    repo.textContent = row.repo || "";
    const mark = badge(row.repo_role);
    if (mark) {
      repo.append(mark);
    }
    line.append(repo);
    appendCell(line, row.held_seq == null ? "none" : "v" + row.held_seq);
    appendCell(line, String(row.versions_behind ?? ""));
    appendCell(line, row.days_stale == null ? "—" : String(row.days_stale));
    appendCell(line, row.last_change || "—");
    appendCell(line, String(row.missed_risky ?? 0));
    appendCell(line, String(row.missed_script_updates ?? 0));
    if (row.path) {
      line.title = row.path + (row.state_basis ? " · " + row.state_basis : "");
    }
    body.append(line);
  });
  paintRows();
}

function appendCell(line, text) {
  const cell = document.createElement("td");
  cell.textContent = text;
  line.append(cell);
}

function paintRows() {
  document.querySelectorAll("#copy-table tbody tr").forEach((line) => {
    const orphaned = line.dataset.orphaned === "true";
    const days = line.dataset.days === "" ? null : Number(line.dataset.days);
    const stale = !orphaned && days != null && days > threshold;
    line.classList.toggle("is-stale", stale);
    line.classList.toggle("is-orphaned", orphaned);
  });
}

function visibleRows() {
  if (!currentId) {
    const rows = [];
    cache.forEach((payload) => {
      filteredCopies(payload).forEach((row) => rows.push(row));
    });
    return rows;
  }
  const payload = cache.get(currentId);
  return payload ? filteredCopies(payload) : [];
}

function refresh() {
  paintTotals(visibleRows());
  if (currentId) {
    drawTimeline();
    drawHistogram();
    paintRows();
    document.getElementById("overview").hidden = true;
    document.getElementById("lineage-panel").hidden = false;
    return;
  }
  document.getElementById("overview").hidden = false;
  document.getElementById("lineage-panel").hidden = true;
  drawOverview();
}

function showLineage(row, payload) {
  currentId = row.id;
  cache.set(row.id, payload);
  tableRows = tableRecords(payload);
  const title = document.getElementById("lineage-title");
  title.replaceChildren();
  title.append(document.createTextNode(row.id + " · " + formatCount(row.copies) + " copies "));
  const mark = badge(row.registry_role || payload.registry_role);
  if (mark) {
    title.append(mark);
  }
  if (payload.removed) {
    title.append(document.createTextNode(" · registry removed"));
  }
  buildTable();
  clearError();
  refresh();
}

async function openLineage(row) {
  document.getElementById("lineage-search").value = row.id;
  if (cache.has(row.id)) {
    showLineage(row, cache.get(row.id));
    return;
  }
  try {
    const payload = await loadJson(row.file);
    showLineage(row, payload);
  } catch (error) {
    showError(error.message);
  }
}

function openLineageId(lineageId) {
  const row = lineageById.get(lineageId);
  if (!row) {
    showError("Lineage " + lineageId + " is not in this export.");
    return;
  }
  openLineage(row);
}

function clearLineage() {
  currentId = null;
  tableRows = [];
  document.getElementById("lineage-search").value = "";
  document.querySelector("#copy-table tbody").replaceChildren();
  refresh();
}

function showRepo(repo) {
  const card = document.getElementById("repo-card");
  card.hidden = false;
  card.replaceChildren();
  const title = document.createElement("h2");
  title.textContent = repo.repo;
  const mark = badge(repo.role);
  if (mark) {
    title.append(" ");
    title.append(mark);
  }
  card.append(title);
  const facts = document.createElement("p");
  const share = repo.hub_share == null ? "n/a" : Number(repo.hub_share).toFixed(3);
  facts.textContent =
    "Stars " + (repo.stars == null ? "n/a" : repo.stars) +
    " · skills " + (repo.skills == null ? "n/a" : repo.skills) +
    " · hub share " + share;
  card.append(facts);
  if (repo.hub_reason) {
    const reason = document.createElement("p");
    reason.textContent = repo.hub_reason;
    reason.addEventListener("mouseenter", (event) => {
      const evidence = repo.hub_evidence ? "Evidence: " + repo.hub_evidence : repo.hub_reason;
      showTooltip(evidence, event.clientX, event.clientY);
    });
    reason.addEventListener("mouseleave", hideTooltip);
    card.append(reason);
  }
  const list = document.createElement("ul");
  (repo.lineages || []).slice(0, 40).forEach((lineageId) => {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = lineageId;
    button.addEventListener("click", () => openLineageId(lineageId));
    item.append(button);
    list.append(item);
  });
  card.append(list);
}

function fillLegend() {
  const legend = document.getElementById("legend");
  const title = document.createElement("h2");
  title.textContent = "Legend";
  legend.append(title);
  [
    ["registry", "Registry"],
    ["registry_hub", "Registry hub"],
    ["hub", "Hub"],
    ["repository", "Repository"],
  ].forEach(([role, label]) => {
    const item = document.createElement("span");
    item.className = "swatch";
    const dot = document.createElement("span");
    dot.className = "dot";
    dot.style.background = roleColor(role);
    item.append(dot, document.createTextNode(label));
    legend.append(item);
  });
  const stale = document.createElement("span");
  stale.className = "swatch";
  const staleDot = document.createElement("span");
  staleDot.className = "dot bar-stale";
  stale.append(staleDot, document.createTextNode("Stale at this threshold"));
  const fresh = document.createElement("span");
  fresh.className = "swatch";
  const freshDot = document.createElement("span");
  freshDot.className = "dot bar-fresh";
  fresh.append(freshDot, document.createTextNode("Not stale"));
  legend.append(stale, fresh);
}

function applyTheme(theme) {
  if (theme) {
    document.documentElement.setAttribute("data-theme", theme);
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
  const dark = theme ? theme === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
  document.getElementById("theme-toggle").textContent = dark ? "Light theme" : "Dark theme";
}

function startTheme() {
  const saved = localStorage.getItem("staleness-viewer-theme");
  applyTheme(saved);
  document.getElementById("theme-toggle").addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme");
    const dark = current ? current === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
    const next = dark ? "light" : "dark";
    localStorage.setItem("staleness-viewer-theme", next);
    applyTheme(next);
  });
}

function scheduleOverview() {
  window.clearTimeout(overviewTimer);
  overviewTimer = window.setTimeout(() => {
    if (!currentId) {
      refresh();
    }
  }, 200);
}

async function loadRest() {
  const pending = lineageRows.filter((row) => !cache.has(row.id));
  let cursor = 0;
  async function worker() {
    while (cursor < pending.length) {
      const row = pending[cursor];
      cursor += 1;
      try {
        cache.set(row.id, await loadJson(row.file));
        loadedCount = cache.size;
        scheduleOverview();
      } catch (error) {
        showError(error.message);
        return;
      }
    }
  }
  await Promise.all([worker(), worker(), worker(), worker()]);
  loadedCount = cache.size;
  if (!currentId) {
    refresh();
  }
}

function bindControls() {
  document.getElementById("stale-number").addEventListener("input", (event) => setThreshold(event.target.value));
  document.getElementById("stale-range").addEventListener("input", (event) => setThreshold(event.target.value));
  document.getElementById("role-filter").addEventListener("change", (event) => {
    roleFilter = event.target.value;
    if (currentId) {
      buildTable();
    }
    refresh();
  });
  document.getElementById("clear-lineage").addEventListener("click", clearLineage);
  document.querySelectorAll("#copy-table th button").forEach((button) => {
    button.addEventListener("click", () => {
      const key = button.dataset.sort;
      if (sortKey === key) {
        sortAsc = !sortAsc;
      } else {
        sortKey = key;
        sortAsc = key === "repo" || key === "change";
      }
      buildTable();
    });
  });
  window.addEventListener("resize", () => {
    if (currentId) {
      drawTimeline();
      drawHistogram();
    } else {
      drawOverview();
    }
  });
}

async function start() {
  if (!window.d3) {
    showError("D3 did not load from cdnjs. Check the network and reload the page.");
    return;
  }
  startTheme();
  fillLegend();
  bindControls();
  try {
    const options = await loadJson("export_options.json");
    if (options && options.default_stale_days != null) {
      threshold = clampDays(options.default_stale_days);
    }
    lineageRows = await loadJson("lineages_index.json");
    repoRows = await loadJson("repos_index.json");
  } catch (error) {
    showError(error.message);
    return;
  }
  lineageById = new Map(lineageRows.map((row) => [row.id, row]));
  document.getElementById("stale-number").value = String(threshold);
  document.getElementById("stale-range").value = String(threshold);
  document.getElementById("counts").textContent =
    lineageRows.length + " lineages · " + repoRows.length + " repos";
  makeSearchBox(
    document.getElementById("lineage-search"),
    lineageRows.map((row) => Object.assign({ label: row.id + " (" + formatCount(row.copies) + ")" }, row)),
    (row) => openLineage(row)
  );
  makeSearchBox(
    document.getElementById("repo-search"),
    repoRows.map((row) => Object.assign({ label: row.repo }, row)),
    (row) => showRepo(row)
  );
  refresh();
  loadRest();
}

start();
