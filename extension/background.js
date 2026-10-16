// TurboDL — pont navigateur : transmet les téléchargements à l'app locale.
const API = "http://127.0.0.1:47321";
const MIN_SIZE = 1024 * 1024; // en dessous de 1 Mo, le navigateur s'en charge

async function isEnabled() {
  const { enabled = true } = await chrome.storage.local.get("enabled");
  return enabled;
}

async function call(path, body = {}, timeout = 20000) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeout);
  try {
    const r = await fetch(API + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    return await r.json();
  } finally {
    clearTimeout(t);
  }
}

async function cookieHeader(url) {
  try {
    const cookies = await chrome.cookies.getAll({ url });
    return cookies.map((c) => `${c.name}=${c.value}`).join("; ");
  } catch {
    return "";
  }
}

// Renvoie {ok:true} si TurboDL a pris le relais, {ok:false,error} sinon,
// ou null si TurboDL n'est pas lancé.
async function sendToTurbo(url, referrer, torrent = false) {
  try {
    return await call("/add", {
      url,
      torrent,
      referrer: referrer || "",
      cookies: await cookieHeader(url),
      userAgent: navigator.userAgent,
    });
  } catch {
    return null;
  }
}

async function remember(res, url) {
  const lastError = res === null ? "TurboDL n'est pas lancé : le navigateur a gardé le téléchargement."
    : res.ok ? "" : `${res.error} (le navigateur a gardé le téléchargement)`;
  await chrome.storage.local.set({ lastError, lastUrl: url });
  chrome.action.setBadgeText({ text: lastError ? "!" : "" });
  chrome.action.setBadgeBackgroundColor({ color: "#f87171" });
}

chrome.downloads.onCreated.addListener(async (item) => {
  const url = item.finalUrl || item.url;
  if (item.byExtensionId === chrome.runtime.id) return;   // nos propres replis
  if (!/^https?:/i.test(url)) return;                      // blob:, data:, file:
  const torrent = item.mime === "application/x-bittorrent" || /\.torrent($|[?#])/i.test(url)
    || /\.torrent$/i.test(item.filename || "");
  if (!torrent && item.totalBytes > 0 && item.totalBytes < MIN_SIZE) return;
  if (!(await isEnabled())) return;

  try { await chrome.downloads.pause(item.id); } catch {}
  const res = await sendToTurbo(url, item.referrer, torrent);
  if (res && res.ok) {
    try {
      await chrome.downloads.cancel(item.id);
      await chrome.downloads.erase({ id: item.id });
    } catch {}
  } else {
    try { await chrome.downloads.resume(item.id); } catch {}
  }
  await remember(res, url);
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "turbodl",
    title: "Télécharger avec TurboDL",
    contexts: ["link", "video", "audio"],
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const url = info.linkUrl || info.srcUrl;
  if (!url) return;
  const magnet = /^magnet:/i.test(url);
  const res = await sendToTurbo(url, info.pageUrl || (tab && tab.url), magnet);
  if ((!res || !res.ok) && !magnet) chrome.downloads.download({ url }); // repli : le navigateur
  await remember(res, url);
});

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg === "ping") {
    call("/ping", {}, 1500).then(reply, () => reply(null));
    return true;
  }
});
