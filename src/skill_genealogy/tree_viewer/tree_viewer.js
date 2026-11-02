/* Family tree page. Uses viewer_common.js and D3 7.9.0. */

let familyRows = [];
let repoRows = [];
let familyById = new Map();
let loadedTree = null;
let loadedFamily = null;
let zoomBehaviour = null;
let svgSelection = null;

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

function addField(parent, label, value) {
  if (value == null || value === "") {
    return;
  }
  const term = document.createElement("dt");
  term.textContent = label;
  const detail = document.createElement("dd");
  detail.textContent = String(value);
  parent.append(term, detail);
}

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

function showDetails(data) {
  const panel = document.getElementById("details");
  panel.replaceChildren();
  const title = document.createElement("h2");
  title.textContent = data.name || "Node";
  panel.append(title);
  if (data.repo || data.repo_role) {
    const line = document.createElement("p");
    line.textContent = data.repo || "";
    const mark = badge(data.repo_role);
    if (mark) {
      line.append(" ");
      line.append(mark);
    }
    panel.append(line);
  }
  const list = document.createElement("dl");
  addField(list, "Kind", data.node_kind);
  addField(list, "Path", data.path);
  addField(list, "Date", formatDate(data.date));
  addField(list, "Generation", data.generation);
  addField(list, "Similarity", data.similarity);
  addField(list, "Files", data.files);
  addField(list, "Link", data.link);
  addField(list, "Change state", data.state);
  addField(list, "Staleness", data.stale);
  addField(list, "Versions behind", data.versions_behind);
  addField(list, "Days stale", data.days_stale);
  addField(list, "Hub evidence", data.hub_evidence);
  if (data.omitted != null) {
    addField(list, "Nodes left out", data.omitted);
  }
  panel.append(list);
}

function tooltipText(data) {
  const lines = [data.name || "Node"];
  const mark = roleBadge(data.repo_role);
  if (mark) {
    lines.push(mark);
  }
  if (data.date) {
    lines.push(formatDate(data.date));
  }
  if (data.similarity != null) {
    lines.push("similarity " + data.similarity);
  }
  if (data.files != null) {
    lines.push(data.files + " files");
  }
  if (data.hub_evidence) {
    lines.push("evidence: " + data.hub_evidence);
  }
  return lines.join("\n");
}

function filterTree(node, role) {
  if (!role || role === "all") {
    return node;
  }
  const children = (node.children || [])
    .map((child) => filterTree(child, role))
    .filter(Boolean);
  const structural = node.node_kind === "more" || node.node_kind === "family";
  const keep = structural || node.repo_role === role;
  if (!keep && !children.length) {
    return null;
  }
  const copy = Object.assign({}, node, { children: children });
  copy._muted = !keep;
  return copy;
}

function shortName(name) {
  const text = name == null ? "" : String(name);
  if (text.length <= 14) {
    return text;
  }
  return text.slice(0, 13) + "…";
}

function nodeFill(d) {
  if (d.data.node_kind === "more") {
    return "var(--kind-more)";
  }
  if (d.data._muted) {
    return "var(--muted)";
  }
  return roleColor(d.data.repo_role);
}

function nodeStroke(d) {
  if (d.data.node_kind === "registry_version") {
    return "var(--kind-registry)";
  }
  if (d.data.node_kind === "family") {
    return "var(--kind-family)";
  }
  if (d.data.node_kind === "more") {
    return "var(--kind-more)";
  }
  return "var(--kind-copy)";
}

function drawTree(data) {
  const host = document.getElementById("tree");
  host.replaceChildren();
  const height = host.clientHeight || 480;
  const svg = d3.select(host).append("svg").attr("role", "img").attr("aria-label", "Family tree");
  const g = svg.append("g");
  zoomBehaviour = d3.zoom().scaleExtent([0.15, 3]).on("zoom", (event) => {
    g.attr("transform", event.transform);
  });
  svg.call(zoomBehaviour);
  svg.on("dblclick.zoom", null);
  svgSelection = svg;

  const root = d3.hierarchy(data);
  let nextId = 0;
  root.each((d) => {
    d.id = ++nextId;
  });
  const maxFiles = d3.max(root.descendants(), (d) => Number(d.data.files) || 1) || 1;
  const radius = d3.scaleSqrt().domain([1, maxFiles]).range([5, 16]);
  if (root.descendants().length > 40) {
    root.each((d) => {
      if (d.depth > 1 && d.children) {
        d._children = d.children;
        d.children = null;
      }
    });
  }

  const layout = d3.tree().nodeSize([36, 260]);
  const linkShape = d3.linkHorizontal().x((d) => d.y).y((d) => d.x);

  function update() {
    layout(root);
    const nodes = root.descendants();
    const links = root.links();
    g.selectAll(".link")
      .data(links, (d) => d.target.id)
      .join("path")
      .attr("class", "link")
      .attr("d", linkShape);
    const groups = g.selectAll(".node")
      .data(nodes, (d) => d.id)
      .join((enter) => {
        const group = enter.append("g").attr("class", "node");
        group.append("circle");
        group.append("text").attr("class", "node-label").attr("dy", "0.32em");
        return group;
      });
    groups.attr("transform", (d) => "translate(" + d.y + "," + d.x + ")");
    groups.select("circle")
      .attr("r", (d) => radius(Number(d.data.files) || 1))
      .attr("fill", nodeFill)
      .attr("stroke", nodeStroke)
      .attr("stroke-width", 2)
      .attr("stroke-dasharray", (d) => (d.data.node_kind === "more" ? "3 2" : null))
      .style("cursor", "pointer");
    groups.select("text")
      .attr("x", (d) => (radius(Number(d.data.files) || 1) + 6))
      .text((d) => shortName(d.data.name));
    groups.on("click", (event, d) => {
      if (d.children) {
        d._children = d.children;
        d.children = null;
      } else if (d._children) {
        d.children = d._children;
        d._children = null;
      }
      showDetails(d.data);
      update();
      event.stopPropagation();
    });
    groups.on("mouseenter", (event, d) => showTooltip(tooltipText(d.data), event.clientX, event.clientY));
    groups.on("mousemove", (event, d) => showTooltip(tooltipText(d.data), event.clientX, event.clientY));
    groups.on("mouseleave", hideTooltip);
  }

  update();
  const start = d3.zoomIdentity.translate(24, height / 2);
  svg.call(zoomBehaviour.transform, start);
}

function renderLoaded() {
  if (!loadedTree) {
    return;
  }
  const role = document.getElementById("role-filter").value;
  const filtered = filterTree(loadedTree, role);
  if (!filtered) {
    showError("No node in this family has that role.");
    return;
  }
  clearError();
  drawTree(filtered);
}

async function openFamily(row) {
  try {
    loadedTree = await loadJson(row.file);
    loadedFamily = row;
    document.getElementById("tree-title").textContent =
      row.id + " · " + row.kind + " · " + row.texts + " texts · depth " + row.depth;
    renderLoaded();
    clearError();
  } catch (error) {
    showError(error.message);
  }
}

function openFamilyId(familyId) {
  const row = familyById.get(familyId);
  if (!row) {
    showError("Family " + familyId + " is not in this export.");
    return;
  }
  openFamily(row);
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
  (repo.families || []).slice(0, 40).forEach((familyId) => {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = familyId;
    button.addEventListener("click", () => openFamilyId(familyId));
    item.append(button);
    list.append(item);
  });
  card.append(list);
}

function fillKindFilter() {
  const select = document.getElementById("kind-filter");
  const kinds = Array.from(new Set(familyRows.map((row) => row.kind).filter(Boolean))).sort();
  kinds.forEach((kind) => {
    const option = document.createElement("option");
    option.value = kind;
    option.textContent = kind;
    select.append(option);
  });
}

function visibleFamilies() {
  const kind = document.getElementById("kind-filter").value;
  const rows = kind === "all" ? familyRows : familyRows.filter((row) => row.kind === kind);
  return rows.map((row) => Object.assign({ label: row.id + " (" + row.kind + ", " + row.texts + ")" }, row));
}

function fillLegend() {
  const legend = document.getElementById("legend");
  const roles = [
    ["registry", "Registry"],
    ["registry_hub", "Registry hub"],
    ["hub", "Hub"],
    ["repository", "Repository"],
  ];
  const title = document.createElement("h2");
  title.textContent = "Legend";
  legend.append(title);
  roles.forEach(([role, label]) => {
    const item = document.createElement("span");
    item.className = "swatch";
    const dot = document.createElement("span");
    dot.className = "dot";
    dot.style.background = roleColor(role);
    item.append(dot, document.createTextNode(label + " fill"));
    legend.append(item);
  });
  const note = document.createElement("span");
  note.className = "swatch";
  note.textContent = "Circle size is the file count. A dashed circle is a cut (+N more). Click a node to open or close its branch. Drag to pan, scroll to zoom.";
  legend.append(note);
}

function applyTheme(theme) {
  if (theme) {
    document.documentElement.setAttribute("data-theme", theme);
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
  const dark = theme
    ? theme === "dark"
    : window.matchMedia("(prefers-color-scheme: dark)").matches;
  document.getElementById("theme-toggle").textContent = dark ? "Light theme" : "Dark theme";
}

function startTheme() {
  const saved = localStorage.getItem("tree-viewer-theme");
  applyTheme(saved);
  document.getElementById("theme-toggle").addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme");
    const dark = current
      ? current === "dark"
      : window.matchMedia("(prefers-color-scheme: dark)").matches;
    const next = dark ? "light" : "dark";
    localStorage.setItem("tree-viewer-theme", next);
    applyTheme(next);
  });
}

async function start() {
  if (!window.d3) {
    showError("D3 did not load from cdnjs. Check the network and reload the page.");
    return;
  }
  startTheme();
  fillLegend();
  try {
    familyRows = await loadJson("families_index.json");
    repoRows = await loadJson("repos_index.json");
  } catch (error) {
    showError(error.message);
    return;
  }
  familyById = new Map(familyRows.map((row) => [row.id, row]));
  document.getElementById("counts").textContent =
    familyRows.length + " families · " + repoRows.length + " repos";
  fillKindFilter();
  const familySearch = makeSearchBox(
    document.getElementById("family-search"),
    visibleFamilies(),
    (row) => openFamily(row)
  );
  document.getElementById("kind-filter").addEventListener("change", () => {
    familySearch.setItems(visibleFamilies());
  });
  document.getElementById("role-filter").addEventListener("change", renderLoaded);
  makeSearchBox(
    document.getElementById("repo-search"),
    repoRows.map((row) => Object.assign({ label: row.repo }, row)),
    (row) => showRepo(row)
  );
  document.getElementById("reset-view").addEventListener("click", () => {
    if (!svgSelection || !zoomBehaviour) {
      return;
    }
    const height = document.getElementById("tree").clientHeight || 480;
    svgSelection.call(zoomBehaviour.transform, d3.zoomIdentity.translate(24, height / 2));
  });
}

start();
