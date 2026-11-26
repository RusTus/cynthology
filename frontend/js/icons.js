// Cynthology icon set: original line icons (24x24, round strokes) on gradient tiles.
const ICONS = {
  files: '<path d="M3.5 7.5a2 2 0 0 1 2-2h3.6l2 2h7.4a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/><path d="M3.5 10.5h17"/>',
  storage: '<rect x="4" y="4.5" width="16" height="6.5" rx="1.8"/><rect x="4" y="13" width="16" height="6.5" rx="1.8"/><path d="M7.5 7.75h.01M7.5 16.25h.01M11 7.75h5M11 16.25h5"/>',
  control: '<path d="M5 7h8M17 7h2M5 12h2M11 12h8M5 17h10"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="17" r="2"/>',
  appcenter: '<path d="M12 3.5l7.5 4v9l-7.5 4-7.5-4v-9z"/><path d="M4.5 7.5l7.5 4 7.5-4M12 11.5v9M8.25 5.5l7.5 4"/>',
  containers: '<rect x="3.5" y="12" width="7.5" height="7" rx="1.2"/><rect x="13" y="12" width="7.5" height="7" rx="1.2"/><rect x="8.25" y="4.5" width="7.5" height="7" rx="1.2"/><path d="M6 14.5v2.2M8.5 14.5v2.2M15.5 14.5v2.2M18 14.5v2.2M10.75 7v2.2M13.25 7v2.2"/>',
  webstation: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.4 2.4 3.6 5.2 3.6 8.5s-1.2 6.1-3.6 8.5c-2.4-2.4-3.6-5.2-3.6-8.5S9.6 5.9 12 3.5z"/>',
  backup: '<path d="M7 18.5a4.5 4.5 0 0 1-.6-8.96A6 6 0 0 1 18 10.5a4 4 0 0 1-.5 8z"/><path d="M12 16v-5.5M9.5 13l2.5-2.5 2.5 2.5"/>',
  snapshots: '<path d="M4 12a8 8 0 1 0 2.4-5.7"/><path d="M4 4.5v4h4"/><path d="M12 8v4.5l3 1.8"/>',
  monitor: '<path d="M3.5 12.5h3.5l2.5-6 5 12 2.5-6h3.5"/>',
  personal: '<circle cx="12" cy="8.5" r="3.8"/><path d="M5 20a7 7 0 0 1 14 0"/>',
  // Control Panel items
  cp_shares: '<path d="M3.5 7.5a2 2 0 0 1 2-2h3.6l2 2h7.4a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/><circle cx="12" cy="12.3" r="1.7"/><path d="M9 17c.4-1.6 1.6-2.4 3-2.4s2.6.8 3 2.4"/>',
  cp_services: '<path d="M5 8.5h12.5M14.5 5.5l3 3-3 3"/><path d="M19 15.5H6.5M9.5 12.5l-3 3 3 3"/>',
  cp_users: '<circle cx="9" cy="8.5" r="3.2"/><path d="M3.5 19.5a5.5 5.5 0 0 1 11 0"/><circle cx="16.5" cy="9.5" r="2.5"/><path d="M15.5 14.2a4.5 4.5 0 0 1 5 5.3"/>',
  cp_network: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.4 2.3 3.6 5.1 3.6 8.5s-1.2 6.2-3.6 8.5c-2.4-2.3-3.6-5.1-3.6-8.5S9.6 5.8 12 3.5z"/>',
  cp_security: '<path d="M12 3.5l7 2.8v5.2c0 4.3-2.9 7.6-7 9-4.1-1.4-7-4.7-7-9V6.3z"/><path d="M8.8 12l2.2 2.2 4.2-4.4"/>',
  cp_general: '<path d="M5 7h8M17 7h2M5 12h2M11 12h8M5 17h10"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="17" r="2"/>',
  cp_info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5M12 7.8h.01"/>',
  cp_notify: '<path d="M6.5 16.5V11a5.5 5.5 0 0 1 11 0v5.5l1.5 1.5H5z"/><path d="M10 20.5a2.2 2.2 0 0 0 4 0"/>',
  cp_ups: '<rect x="3.5" y="7.5" width="15" height="9" rx="2"/><path d="M20.5 10.5v3"/><path d="M11.5 9l-2.5 3.5h3.5L10 16"/>',
  cp_usb: '<path d="M12 3.5v13.5"/><path d="M9.5 6l2.5-2.5L14.5 6"/><path d="M12 13l-4.5-2.5V8.5M12 11l4-2V7"/><circle cx="7.5" cy="7.5" r="1.1"/><rect x="15" y="5.5" width="2" height="2"/><circle cx="12" cy="18.8" r="1.8"/>',
  cp_update: '<path d="M19.5 12a7.5 7.5 0 0 1-12.8 5.3M4.5 12a7.5 7.5 0 0 1 12.8-5.3"/><path d="M17.5 3.5v3.3h-3.3M6.5 20.5v-3.3h3.3"/>',
  hdd: '<rect x="4.5" y="3.5" width="15" height="17" rx="2"/><circle cx="12" cy="10.5" r="3.5"/><path d="M12 10.5h.01M8 17.5h.01M16 17.5h.01"/>',
  ssd: '<rect x="3.5" y="6" width="17" height="12" rx="2"/><path d="M7 10h4M7 13.5h7M16.5 10.5h.01"/>',
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
  webstation: ["#38bdf8", "#0369a1"],
  snapshots: ["#9aa5ff", "#5b5bd6"],
  monitor: ["#c58cff", "#7c3aed"],
  personal: ["#94a3b8", "#475569"],
  cp_shares: ["#fbbf24", "#f59e0b"], cp_services: ["#4fa8ff", "#2563eb"], cp_users: ["#2dd4bf", "#0d9488"],
  cp_network: ["#60a5fa", "#4f46e5"], cp_security: ["#34d399", "#059669"], cp_general: ["#8393ad", "#4a5a73"],
  cp_info: ["#38bdf8", "#0284c7"], cp_notify: ["#fb923c", "#ea580c"], cp_ups: ["#facc15", "#ca8a04"],
  cp_usb: ["#22c3d1", "#1b7f99"], cp_update: ["#34d399", "#0e9f6e"],
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
  const app = (typeof WM !== "undefined" && WM.apps[id]) || {};
  const [a, b] = ICON_GRADIENTS[id] || [app.color || "#64748b", app.color || "#475569"];
  const tile = document.createElement("div");
  tile.className = "app-tile";
  tile.style.cssText = `width:${size}px;height:${size}px;border-radius:${Math.round(size * 0.27)}px;background:linear-gradient(145deg, ${a}, ${b})`;
  if (ICONS[id]) tile.append(svgIcon(id, Math.round(size * 0.58)));
  else { tile.textContent = app.icon || "•"; tile.style.fontSize = `${Math.round(size * 0.5)}px`; }
  return tile;
}
