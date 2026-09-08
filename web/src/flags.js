// The flags settings page (web/src/flags.html), reachable only by URL: no link to it
// from the chat page or anywhere else. It reads config.json the same way app.js does,
// renders one row per flag name in config.features, and writes the browser-wide
// override set to the same localStorage key features.js reads
// (docs/proposals/feature-flags.md). Committed defaults still change only by editing
// web/features.json and deploying; this page only ever writes the override, never the
// default.

import {
  buildFlagRows,
  setOverride,
  parseStoredOverrides,
  serializeOverrides,
  OVERRIDE_KEY,
} from "./flags-core.js";

function readOverrides() {
  try {
    return parseStoredOverrides(localStorage.getItem(OVERRIDE_KEY));
  } catch {
    return {};
  }
}

function writeOverrides(overrides) {
  try {
    localStorage.setItem(OVERRIDE_KEY, serializeOverrides(overrides));
  } catch {
    // A private window or storage blocked by the browser: the choice still shows on
    // this render, it just will not persist past it.
  }
}

function clearOverrides() {
  try {
    localStorage.removeItem(OVERRIDE_KEY);
  } catch {
    // Nothing to clear if storage is unavailable.
  }
}

function choiceLabel(choice) {
  return choice === "on" ? "On" : choice === "off" ? "Off" : "Default";
}

function buildRow(row, onChoose) {
  const wrap = document.createElement("div");
  wrap.className = "flag-row";
  wrap.dataset.flag = row.name;

  const head = document.createElement("div");
  head.className = "flag-row-head";

  const name = document.createElement("span");
  name.className = "flag-name";
  name.textContent = row.name;

  const effective = document.createElement("span");
  effective.className = "flag-effective";
  effective.textContent = `Effective: ${row.effective ? "on" : "off"}`;

  head.append(name, effective);

  const fieldset = document.createElement("fieldset");
  fieldset.className = "flag-control";

  const legend = document.createElement("legend");
  legend.textContent = `Override for ${row.name} (committed default: ${row.defaultValue ? "on" : "off"})`;
  fieldset.appendChild(legend);

  for (const choice of ["default", "on", "off"]) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "radio";
    input.name = `ff-${row.name}`;
    input.value = choice;
    input.checked = row.override === choice;
    input.addEventListener("change", () => onChoose(row.name, choice));
    label.append(input, document.createTextNode(` ${choiceLabel(choice)}`));
    fieldset.appendChild(label);
  }

  wrap.append(head, fieldset);
  return wrap;
}

function render(config) {
  const overrides = readOverrides();
  const rows = buildFlagRows(config.features || {}, overrides);

  const list = document.getElementById("flags-list");
  const opList = document.getElementById("flags-operational-list");
  list.replaceChildren();
  opList.replaceChildren();

  const onChoose = (name, choice) => {
    writeOverrides(setOverride(readOverrides(), name, choice));
    render(config);
  };

  for (const row of rows) {
    const target = row.operational ? opList : list;
    target.appendChild(buildRow(row, onChoose));
  }
}

async function init() {
  const config = await (await fetch("config.json", { cache: "no-store" })).json();
  render(config);

  document.getElementById("reset-all-btn").addEventListener("click", () => {
    clearOverrides();
    render(config);
  });

  // Another tab (this one included, since flags.html can itself be open in two tabs)
  // may write the same key; keep this page's own controls in sync with the storage it
  // is showing rather than only with the chat page's honest reload-to-pick-up rule.
  window.addEventListener("storage", (event) => {
    if (event.key === OVERRIDE_KEY) render(config);
  });
}

init();
