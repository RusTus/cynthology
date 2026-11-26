// Cynthology icon set: original line icons (24x24, round strokes) on gradient tiles.
const ICONS = {
  files: '<path d="M3.5 7.5a2 2 0 0 1 2-2h3.6l2 2h7.4a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/><path d="M3.5 10.5h17"/>',
  storage: '<rect x="4" y="4.5" width="16" height="6.5" rx="1.8"/><rect x="4" y="13" width="16" height="6.5" rx="1.8"/><path d="M7.5 7.75h.01M7.5 16.25h.01M11 7.75h5M11 16.25h5"/>',
  control: '<path d="M5 7h8M17 7h2M5 12h2M11 12h8M5 17h10"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="17" r="2"/>',
  appcenter: '<path d="M12 3.5l7.5 4v9l-7.5 4-7.5-4v-9z"/><path d="M4.5 7.5l7.5 4 7.5-4M12 11.5v9M8.25 5.5l7.5 4"/>',
  containers: '<rect x="3.5" y="12" width="7.5" height="7" rx="1.2"/><rect x="13" y="12" width="7.5" height="7" rx="1.2"/><rect x="8.25" y="4.5" width="7.5" height="7" rx="1.2"/><path d="M6 14.5v2.2M8.5 14.5v2.2M15.5 14.5v2.2M18 14.5v2.2M10.75 7v2.2M13.25 7v2.2"/>',
  backup: '<path d="M7 18.5a4.5 4.5 0 0 1-.6-8.96A6 6 0 0 1 18 10.5a4 4 0 0 1-.5 8z"/><path d="M12 16v-5.5M9.5 13l2.5-2.5 2.5 2.5"/>',
  snapshots: '<path d="M4 12a8 8 0 1 0 2.4-5.7"/><path d="M4 4.5v4h4"/><path d="M12 8v4.5l3 1.8"/>',
  monitor: '<path d="M3.5 12.5h3.5l2.5-6 5 12 2.5-6h3.5"/>',
  personal: '<circle cx="12" cy="8.5" r="3.8"/><path d="M5 20a7 7 0 0 1 14 0"/>',
  // taskbar
  menu: '<rect x="4" y="4" width="6.5" height="6.5" rx="1.6"/><rect x="13.5" y="4" width="6.5" height="6.5" rx="1.6"/><rect x="4" y="13.5" width="6.5" height="6.5" rx="1.6"/><rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1.6"/>',
  eject: '<path d="M12 5.5l6.5 7.5h-13z"/><path d="M5.5 18h13"/>',
  user: '<circle cx="12" cy="8.5" r="3.8"/><path d="M5 20a7 7 0 0 1 14 0"/>',
};

const ICON_GRADIENTS = {
  files: ["#fbbf24", "#f59e0b"],
  storage: ["#22c3d1", "#1b7f99"],
  control: ["#8393ad", "#4a5a73"],
  appcenter: ["#ff8a65", "#e8505b"],
  containers: ["#4fa8ff", "#2563eb"],
  backup: ["#34d399", "#059669"],
  snapshots: ["#9aa5ff", "#5b5bd6"],
  monitor: ["#c58cff", "#7c3aed"],
  personal: ["#94a3b8", "#475569"],
};

// A bare line icon (inherits text colour), e.g. for the taskbar.
function svgIcon(name, size = 20) {
  const span = document.createElement("span");
  span.className = "svg-icon";
  span.innerHTML = `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ""}</svg>`;
  return span;
}

// An app tile: white line icon on the app's gradient. Falls back to the app's emoji.
function appTile(id, size = 48) {
  const app = WM.apps[id] || {};
  const [a, b] = ICON_GRADIENTS[id] || [app.color || "#64748b", app.color || "#475569"];
  const tile = document.createElement("div");
  tile.className = "app-tile";
  tile.style.cssText = `width:${size}px;height:${size}px;border-radius:${Math.round(size * 0.27)}px;background:linear-gradient(145deg, ${a}, ${b})`;
  if (ICONS[id]) tile.append(svgIcon(id, Math.round(size * 0.58)));
  else { tile.textContent = app.icon || "•"; tile.style.fontSize = `${Math.round(size * 0.5)}px`; }
  return tile;
}
