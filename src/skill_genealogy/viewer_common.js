/* Shared helpers for the skill genealogy viewers.
   Each page copies this file next to itself so the folder works alone. */

/**
 * Load a JSON file next to the page.
 * Throws an Error that names the file when the response is not OK,
 * including when the page was opened as a local file instead of
 * through python -m http.server.
 * @param {string} url path relative to the page
 * @returns {Promise<any>}
 */
async function loadJson(url) {
  let response;
  try {
    response = await fetch(url);
  } catch (error) {
    throw new Error(
      "Could not load " + url + ". Serve this folder with python -m http.server. " + error.message
    );
  }
  if (!response.ok) {
    throw new Error(
      "Could not load " + url + " (" + response.status + "). Serve this folder with python -m http.server."
    );
  }
  return response.json();
}

/**
 * Badge text for a repo role.
 * registry, registry_hub, hub, and repository are the stored roles.
 * @param {string} role
 * @returns {string}
 */
function roleBadge(role) {
  const labels = {
    registry: "REGISTRY",
    registry_hub: "REGISTRY · HUB",
    hub: "HUB",
    repository: "REPOSITORY",
  };
  return labels[role] || "";
}

/**
 * CSS colour for a repo role. The value is a custom property so light
 * and dark themes can change it.
 * @param {string} role
 * @returns {string}
 */
function roleColor(role) {
  const variables = {
    registry: "--role-registry",
    registry_hub: "--role-registry-hub",
    hub: "--role-hub",
    repository: "--role-repository",
  };
  const name = variables[role];
  return name ? "var(" + name + ")" : "var(--muted)";
}

/**
 * Show a timestamp as YYYY-MM-DD. An empty value stays empty.
 * @param {string|number|null} value
 * @returns {string}
 */
function formatDate(value) {
  if (value == null || value === "") {
    return "";
  }
  const text = String(value);
  const day = text.slice(0, 10);
  if (/^\d{4}-\d{2}-\d{2}$/.test(day)) {
    return day;
  }
  return text;
}

/**
 * Turn a text input into a short result list.
 * Each item is an object with label, or a string. onPick receives the item.
 * Returns setItems so a filter can replace the list.
 * At most 12 matches are shown. The list stays empty until the person types.
 * @param {HTMLInputElement} inputEl
 * @param {Array<object|string>} items
 * @param {function(object|string): void} onPick
 * @returns {{setItems: function(Array): void}}
 */
function makeSearchBox(inputEl, items, onPick) {
  let current = items.slice();
  const list = document.createElement("ul");
  list.className = "search-list";
  list.hidden = true;
  inputEl.insertAdjacentElement("afterend", list);

  function labelOf(item) {
    if (item == null) {
      return "";
    }
    if (typeof item === "string") {
      return item;
    }
    if (item.label != null) {
      return String(item.label);
    }
    if (item.id != null) {
      return String(item.id);
    }
    if (item.repo != null) {
      return String(item.repo);
    }
    return "";
  }

  function matches() {
    const query = inputEl.value.trim().toLowerCase();
    if (!query) {
      return [];
    }
    return current.filter((item) => labelOf(item).toLowerCase().includes(query)).slice(0, 12);
  }

  function renderList() {
    const found = matches();
    list.replaceChildren();
    if (!found.length) {
      list.hidden = true;
      return;
    }
    found.forEach((item) => {
      const row = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = labelOf(item);
      button.addEventListener("click", () => {
        inputEl.value = labelOf(item);
        list.hidden = true;
        onPick(item);
      });
      row.append(button);
      list.append(row);
    });
    list.hidden = false;
  }

  inputEl.addEventListener("input", renderList);
  inputEl.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      list.hidden = true;
      return;
    }
    if (event.key === "Enter") {
      const found = matches();
      if (found.length) {
        event.preventDefault();
        inputEl.value = labelOf(found[0]);
        list.hidden = true;
        onPick(found[0]);
      }
    }
  });
  document.addEventListener("click", (event) => {
    if (event.target === inputEl || list.contains(event.target)) {
      return;
    }
    list.hidden = true;
  });

  return {
    setItems(next) {
      current = next.slice();
      renderList();
    },
  };
}

/**
 * Show the shared tooltip near the pointer. text is plain text.
 * @param {string} text
 * @param {number} x client X
 * @param {number} y client Y
 */
function showTooltip(text, x, y) {
  let tip = document.getElementById("viewer-tooltip");
  if (!tip) {
    tip = document.createElement("div");
    tip.id = "viewer-tooltip";
    tip.setAttribute("role", "tooltip");
    document.body.append(tip);
  }
  tip.textContent = text;
  tip.hidden = false;
  const width = Math.min(280, window.innerWidth - 16);
  tip.style.maxWidth = width + "px";
  const left = Math.min(x + 12, window.innerWidth - width - 8);
  const top = Math.min(y + 16, window.innerHeight - 80);
  tip.style.left = Math.max(8, left) + "px";
  tip.style.top = Math.max(8, top) + "px";
}

/**
 * Hide the shared tooltip.
 */
function hideTooltip() {
  const tip = document.getElementById("viewer-tooltip");
  if (tip) {
    tip.hidden = true;
  }
}
