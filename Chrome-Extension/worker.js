const key = "capture";
const lifetime = 120_000;
let pending = Promise.resolve();

function serial(task) {
  const next = pending.then(task);
  pending = next.catch(error => console.warn("KlipLokal Capture:", error.message));
  return next;
}

async function currentCapture() {
  const capture = (await chrome.storage.session.get(key))[key];
  if (capture && capture.expires > Date.now()) return capture;
  if (capture) await chrome.storage.session.remove(key);
  return null;
}

function classify(details) {
  if (details.tabId < 0 || details.statusCode < 200 || details.statusCode >= 300) return null;
  const url = new URL(details.url);
  const path = url.pathname.toLowerCase();
  if (/(?:^|[./_-])(?:ads|doubleclick|googlesyndication|preroll|midroll|vast|vpaid)(?:[./_-]|$)/i.test(url.hostname + path)) return null;
  const mime = details.responseHeaders?.find(header => header.name.toLowerCase() === "content-type")
    ?.value?.toLowerCase().split(";")[0].trim() || "";
  if (mime === "text/html" || mime === "application/json") return null;
  let kind = "";
  if (path.endsWith(".m3u8") || mime.includes("mpegurl")) kind = "HLS";
  else if (path.endsWith(".mpd") || mime === "application/dash+xml") kind = "DASH";
  else if (details.type === "media" && (/\.(mp4|webm)$/.test(path) || /^video\/(mp4|webm)$/.test(mime))) kind = "Video";
  if (!kind) return null;
  return {
    url: url.href, host: url.hostname, kind,
    name: url.pathname.split("/").filter(Boolean).at(-1)?.slice(0, 100) || "Stream"
  };
}

chrome.webRequest.onResponseStarted.addListener(details => {
  const stream = classify(details);
  if (!stream) return;
  serial(async () => {
    const capture = await currentCapture();
    if (capture?.tabId !== details.tabId) return;
    const streams = capture.streams.filter(item => item.url !== stream.url);
    streams.unshift(stream);
    streams.sort((a, b) => Number(a.kind === "Video") - Number(b.kind === "Video"));
    capture.streams = streams.slice(0, 24);
    await chrome.storage.session.set({[key]: capture});
  });
}, {urls: ["http://*/*", "https://*/*"]}, ["responseHeaders"]);

chrome.tabs.onRemoved.addListener(tabId => serial(async () => {
  const capture = await currentCapture();
  if (capture?.tabId === tabId) await chrome.storage.session.remove(key);
}));

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id) return false;
  serial(async () => {
    const capture = await currentCapture();
    if (message.action === "start") {
      const tab = await chrome.tabs.get(message.tabId);
      if (!/^https?:\/\//i.test(tab.url || "") || tab.url.startsWith("http://127.0.0.1:8765/")) {
        throw new Error("Buka tab halaman video dulu.");
      }
      await chrome.storage.session.set({[key]: {tabId: tab.id, expires: Date.now() + lifetime, streams: []}});
      return;
    }
    if (message.action === "status") return capture?.tabId === message.tabId ? capture : null;
    if (capture?.tabId !== message.tabId) throw new Error("Mulai tangkap lagi dari tab video.");
    if (message.action === "stop") {
      await chrome.storage.session.remove(key);
      return;
    }
    if (message.action === "send") {
      const stream = capture.streams.find(item => item.url === message.url);
      if (!stream) throw new Error("Stream ini sudah kedaluwarsa. Tangkap ulang.");
      const hash = new URLSearchParams({stream: stream.url});
      await chrome.tabs.create({url: "http://127.0.0.1:8765/#" + hash.toString()});
      await chrome.storage.session.remove(key);
    }
  }).then(value => reply({ok: true, value}), error => reply({ok: false, error: error.message}));
  return true;
});
