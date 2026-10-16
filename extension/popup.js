const $ = (id) => document.getElementById(id);

chrome.storage.local.get({ enabled: true, lastError: "" }, ({ enabled, lastError }) => {
  $("enabled").checked = enabled;
  if (lastError) { $("err").textContent = lastError; $("err").hidden = false; }
});
$("enabled").addEventListener("change", (e) => chrome.storage.local.set({ enabled: e.target.checked }));

chrome.runtime.sendMessage("ping", (res) => {
  const ok = res && res.ok;
  $("status").className = "status " + (ok ? "on" : "off");
  $("statusText").textContent = ok ? `TurboDL connecté (v${res.version})` : "TurboDL n'est pas lancé";
});

// on efface l'alerte une fois vue
chrome.storage.local.set({ lastError: "" });
chrome.action.setBadgeText({ text: "" });
