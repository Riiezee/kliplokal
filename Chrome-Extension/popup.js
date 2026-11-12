const el = id => document.getElementById(id);
let tab;
let busy = false;
let capture = null;
let shownUrls = "";

async function command(action, extra = {}) {
  const response = await chrome.runtime.sendMessage({action, tabId: tab.id, ...extra});
  if (!response?.ok) throw new Error(response?.error || "Ekstensi belum bisa terhubung.");
  return response.value;
}

function render() {
  const streams = capture?.streams || [];
  el("status").textContent = capture
    ? streams.length ? "Pilih stream video utama. Link akan dibuka di dashboard lokal."
      : "Putar video utama. Kalau sudah diputar, coba refresh halaman lalu Play lagi."
    : "Buka video di tab ini, lalu mulai tangkap.";
  el("start").hidden = Boolean(capture);
  el("stop").hidden = !capture;
  el("streams").hidden = el("send").hidden = streams.length === 0;
  const urls = JSON.stringify(streams.map(stream => stream.url));
  if (shownUrls !== urls) {
    const previous = el("streams").value;
    const options = streams.map(stream => new Option(
      stream.kind + " · " + stream.name + " · " + stream.host, stream.url
    ));
    el("streams").replaceChildren(new Option("Pilih link video", ""), ...options);
    if (streams.some(stream => stream.url === previous)) el("streams").value = previous;
    shownUrls = urls;
  }
  el("start").disabled = el("stop").disabled = busy;
  el("send").disabled = busy || !el("streams").value;
}

async function perform(task) {
  if (busy) return;
  busy = true;
  el("error").textContent = "";
  render();
  try {
    await task();
    capture = await command("status");
  } catch (error) {
    el("error").textContent = error.message;
  } finally {
    busy = false;
    render();
  }
}

el("start").addEventListener("click", () => perform(async () => {
  const granted = await chrome.permissions.request({origins: ["http://*/*", "https://*/*"]});
  if (!granted) throw new Error("Izin diperlukan karena video bisa dimuat dari server lain.");
  await command("start");
}));
el("stop").addEventListener("click", () => perform(() => command("stop")));
el("send").addEventListener("click", () => perform(() => command("send", {url: el("streams").value})));
el("streams").addEventListener("change", () => { el("send").disabled = busy || !el("streams").value; });

async function poll() {
  try {
    if (!tab) [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    if (!busy && tab) {
      const state = await command("status");
      if (!busy) {
        capture = state;
        render();
      }
    }
  } catch (error) {
    el("error").textContent = error.message;
  } finally {
    setTimeout(poll, 600);
  }
}
poll();
