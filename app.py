import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from time import monotonic
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from playwright.sync_api import Error as BrowserError, sync_playwright

from compress import PRESETS, RESOLUTIONS, compress_video

BASE = Path(__file__).resolve().parent
DEFAULT_FOLDER = Path(os.getenv("STREAMDROP_FOLDER") or (r"D:\Video" if os.name == "nt" else str(Path.home() / "Videos")))
PORT = 8765
HOST = f"127.0.0.1:{PORT}"
ORIGIN = f"http://{HOST}"
LOCK = threading.Lock()
PICKER_LOCK = threading.Lock()
PREVIEW_SLOTS = threading.BoundedSemaphore(2)
ACTIVE = {"checking", "scanning", "collecting", "review", "downloading", "compressing", "saving"}
QUALITIES = {"auto", "240", "360", "480", "720", "1080", "1440", "2160"}
job = {"phase": "idle", "message": "Siap menerima link.", "percent": None, "detail": "", "file": "", "folder": "", "scan_id": "", "streams": []}
scan_commands = None


def destination(value):
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        raise ValueError
    folder = Path(value.strip()) if value.strip() else DEFAULT_FOLDER
    if not folder.is_absolute():
        raise ValueError
    return folder


def choose_path(initial, pick_file=False):
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        folder = Path(initial)
        options = {"parent": root, "initialdir": str(folder if folder.is_dir() else Path.home())}
        if pick_file:
            selected = filedialog.askopenfilename(
                **options, title="KlipLokal - Pilih video",
                filetypes=[("Video", ("*.mp4", "*.mkv", "*.mov", "*.webm", "*.avi", "*.m4v", "*.ts", "*.mpg", "*.flv")),
                           ("Semua file", "*")],
            )
        else:
            selected = filedialog.askdirectory(**options, title="KlipLokal - Pilih folder", mustexist=True)
        print(json.dumps(str(Path(selected)) if selected else ""))
    finally:
        root.destroy()


def update(**values):
    with LOCK:
        job.update(values)


def http_url(value):
    try:
        parsed = urlsplit(value)
        if parsed.port == 0:
            return False
        return (
            parsed.scheme in {"http", "https"} and bool(parsed.hostname) and parsed.username is None
            and not any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in value)
        )
    except ValueError:
        return False


def stream_type(url, mime=""):
    path = urlsplit(url).path.lower()
    mime = mime.lower().split(";")[0].strip()
    if mime in {"text/html", "application/json"}:
        return ""
    if path.endswith(".m3u8") or "mpegurl" in mime:
        return "hls"
    if path.endswith(".mpd") or mime == "application/dash+xml":
        return "dash"
    if path.endswith((".mp4", ".webm")) or mime in {"video/mp4", "video/webm"}:
        return "file"
    return ""


def launch_browser(playwright):
    executable = os.getenv("STREAMDROP_BROWSER")
    if executable:
        return playwright.chromium.launch(executable_path=executable, headless=False, chromium_sandbox=True)
    for channel in ("chrome", "msedge"):
        try:
            return playwright.chromium.launch(channel=channel, headless=False, chromium_sandbox=True, timeout=12000)
        except BrowserError:
            continue
    raise RuntimeError("Chrome atau Microsoft Edge belum bisa dibuka. Pastikan salah satunya terpasang.")


AD_MARKER = re.compile(
    r"(?:^|[./_-])(?:ads|doubleclick|googlesyndication|preroll|midroll|vast|vpaid|advertisement)(?:[./_-]|$)",
    re.I,
)
HLS_DURATION = re.compile(r"^#EXTINF:([0-9]{1,8}(?:\.[0-9]{1,12})?),", re.M)


def stream_key(url):
    parsed = urlsplit(url)
    return parsed.netloc, parsed.path


def describe_streams(candidates, manifests):
    durations = {
        stream_key(url): sum(float(value) for value in HLS_DURATION.findall(body))
        for url, body in manifests.items()
    }
    ranked = []
    for url, (kind, headers) in candidates.items():
        body = manifests.get(url, "")
        master = "#EXT-X-STREAM-INF:" in body
        duration = durations.get(stream_key(url), 0)
        if master:
            lines = body.splitlines()
            for index, line in enumerate(lines[:-1]):
                if line.startswith("#EXT-X-STREAM-INF:"):
                    try:
                        variant = stream_key(urljoin(url, lines[index + 1].strip()))
                    except ValueError:
                        continue
                    duration = max(duration, durations.get(variant, 0))
        parsed = urlsplit(url)
        name = parsed.path.rstrip("/").rsplit("/", 1)[-1][:100] or "Stream"
        public = {"name": name, "host": parsed.hostname, "kind": kind.upper(),
                  "seconds": round(duration) if duration else None, "master": master}
        ranked.append(((duration, master, kind != "file"), url, headers, public))
    ranked.sort(key=lambda entry: entry[0], reverse=True)
    return [(url, headers, public) for _, url, headers, public in ranked[:12]]


def detect_stream(page_url, commands):
    candidates = {}
    hls_responses = {}
    manifests = {}
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        try:
            context = browser.new_context(accept_downloads=False)
            page = context.new_page()

            def close_popup(popup):
                if popup != page:
                    try:
                        popup.close()
                    except BrowserError:
                        pass

            context.on("page", close_popup)

            def inspect(response):
                if not 200 <= response.status < 300 or not http_url(response.url):
                    return
                if AD_MARKER.search("/".join(stream_key(response.url))):
                    return
                kind = stream_type(response.url, response.headers.get("content-type", ""))
                if not kind or response.url in candidates or len(candidates) >= 80:
                    return
                # DASH players can request individual MP4 segments through Fetch.
                if kind == "file" and response.request.resource_type != "media":
                    return
                if kind == "hls":
                    hls_responses[response.url] = response
                try:
                    candidates[response.url] = (kind, response.request.all_headers())
                except BrowserError:
                    return

            def inspect_finished(request):
                response = hls_responses.pop(request.url, None)
                if response is not None:
                    try:
                        manifests[request.url] = response.text()[:2_000_000]
                    except (BrowserError, UnicodeError):
                        pass

            page.on("response", inspect)
            page.on("requestfinished", inspect_finished)
            try:
                page.goto(page_url, wait_until="domcontentloaded", timeout=18000)
            except BrowserError:
                if page.is_closed():
                    raise RuntimeError("Jendela pemindai ditutup.")
            deadline = monotonic() + 90
            collecting = False
            while not page.is_closed() and monotonic() < deadline:
                try:
                    action, _ = commands.get_nowait()
                except Empty:
                    pass
                else:
                    if action == "cancel":
                        return [], []
                    collecting = True
                    deadline = monotonic() + 4
                page.wait_for_timeout(250)
            if not collecting or not candidates:
                raise RuntimeError("Stream belum ditemukan. Coba lagi, putar video utama, lalu klik Cek stream di dashboard.")
            return describe_streams(candidates, manifests), context.cookies()
        finally:
            browser.close()


def cookie_file(path, cookies):
    lines = ["# Netscape HTTP Cookie File"]
    for cookie in cookies:
        domain = cookie["domain"]
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if cookie["secure"] else "FALSE"
        expires = str(max(0, int(cookie["expires"])))
        fields = [domain, include_subdomains, cookie["path"], secure, expires, cookie["name"], cookie["value"]]
        if all(not any(char in field for char in "\t\r\n") for field in fields):
            if cookie.get("httpOnly"):
                fields[0] = "#HttpOnly_" + domain
            lines.append("\t".join(fields))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def redact(line):
    return re.sub(r"https?://[^\s<>\"']+", "[link]", line)[:350]


def excluded_platform(url):
    host = (urlsplit(url).hostname or "").rstrip(".")
    return any(host == domain or host.endswith("." + domain)
               for domain in ("tiktok.com", "instagram.com", "instagr.am"))


def video_preview(url):
    executable = shutil.which("yt-dlp")
    if not executable:
        raise RuntimeError("yt-dlp belum ditemukan. Pastikan sudah terpasang.")
    if not PREVIEW_SLOTS.acquire(blocking=False):
        raise RuntimeError("Pratinjau lain masih berjalan. Coba klik Cek video lagi sebentar.")
    try:
        result = subprocess.run(
            [executable, "--ignore-config", "--no-playlist", "--playlist-items", "1",
             "--dump-single-json", "--skip-download", "--no-warnings", "--socket-timeout", "8",
             "--retries", "0", "--encoding", "utf-8", "--", url],
            capture_output=True, encoding="utf-8", errors="replace", timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            raise RuntimeError("Pratinjau gagal dibaca. Coba cek link atau gunakan pemindai browser.")
        info = json.loads(result.stdout)
        if info.get("_type") in {"playlist", "multi_video"}:
            info = next((item for item in info.get("entries") or [] if isinstance(item, dict)), {})
        if not info:
            raise RuntimeError("Video belum terdeteksi dari link ini.")
    except subprocess.TimeoutExpired:
        raise RuntimeError("Pratinjau melewati batas waktu. Coba cek lagi.") from None
    except (ValueError, AttributeError, OSError):
        raise RuntimeError("Metadata video tidak bisa dibaca.") from None
    finally:
        PREVIEW_SLOTS.release()

    def label(key, fallback=""):
        value = info.get(key)
        return value[:300] if isinstance(value, str) and value else fallback

    thumbnail = info.get("thumbnail")
    duration = info.get("duration")
    return {
        "title": label("title", "Video"),
        "source": label("uploader") or label("channel") or urlsplit(url).hostname,
        "thumbnail": thumbnail if isinstance(thumbnail, str) and http_url(thumbnail) else "",
        "duration": duration if type(duration) in (int, float) and 0 < duration < 10**8 else None,
    }


def download(executable, url, quality, folder, extra=(), output=""):
    format_options = []
    if quality != "auto" and output != "mp3":
        # The shorter dimension defines resolution for landscape and portrait video.
        # Filters exclude formats whose matching dimension is unknown.
        selector = (
            f"bv*[height<={quality}]+ba/b[height<={quality}]"
            f"/bv*[width<={quality}]+ba/b[width<={quality}]"
        )
        format_options = ["-f", selector, "-S", f"res:{quality}"]
    output_options = {
        "mp3": ["-x", "--audio-format", "mp3"],
        "mp4": ["--merge-output-format", "mp4", "--recode-video", "mp4"],
    }.get(output, [])
    args = [
        executable, "--ignore-config", "--no-playlist", "--playlist-items", "1",
        "--newline", "--no-colors", "--encoding", "utf-8",
        "--socket-timeout", "15", "--retries", "2", "--fragment-retries", "2", "--abort-on-unavailable-fragments",
        "--progress", "--progress-delta", "0.4",
        "--progress-template", "download:STREAMDROP_PROGRESS %(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s",
        "--print", "after_move:STREAMDROP_FILE %(filepath)s",
        "--no-simulate", "--no-quiet", "-P", str(folder),
        "-o", "%(title)s [%(id)s] [%(format_id)s].%(ext)s",
        *format_options, *output_options, *extra, "--", url,
    ]
    error = ""
    file = ""
    started = False
    with subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ) as process:
        for line in process.stdout:
            line = line.strip()
            if line.startswith("STREAMDROP_PROGRESS "):
                fields = line.removeprefix("STREAMDROP_PROGRESS ").split("|", 2)
                if len(fields) != 3:
                    continue
                progress, speed, eta = map(str.strip, fields)
                value = re.search(r"\d+(?:\.\d+)?", progress)
                percent = min(100, float(value[0])) if value else None
                started = True
                update(phase="downloading", message="Sedang download video.", percent=percent,
                       detail=f"{speed}  ·  ETA {eta}")
            elif line.startswith("STREAMDROP_FILE "):
                file = line.removeprefix("STREAMDROP_FILE ")
            elif line.startswith(("ERROR:", "Error:")):
                error = redact(line)
            elif line.startswith(("[Merger]", "[Fixup", "[VideoRemuxer]")):
                update(phase="saving", message="Sedang menyiapkan file akhir.", detail="")
        code = process.wait()
    return code, error, file, started


def actual_resolution(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return ""
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "V:0", "-show_entries", "stream=width,height",
             "-of", "json", "-i", str(path)],
            capture_output=True, text=True, check=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        video = json.loads(result.stdout)["streams"][0]
        width, height = video["width"], video["height"]
        return f"Resolusi hasil: {min(width, height)}p ({width} × {height})"
    except (OSError, subprocess.SubprocessError, ValueError, IndexError, KeyError):
        return ""


def run_job(url, quality, folder, commands, output="", browser="fallback"):
    try:
        executable = shutil.which("yt-dlp")
        if not executable:
            raise RuntimeError("yt-dlp belum ditemukan. Pastikan sudah terpasang, lalu buka ulang aplikasi.")
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise RuntimeError(f"Folder tujuan tidak bisa dibuka atau dibuat: {folder}") from None
        direct = stream_type(url)
        code, error, file, started = (1, "", "", False)
        if browser != "scan":
            code, error, file, started = download(executable, url, quality, folder, output=output)
        if browser == "scan" or (
            browser == "fallback" and code and not direct and not started
            and "requested format is not available" not in error.lower()
        ):
            update(phase="scanning", message="Putar video utama di jendela pemindai.",
                   detail="Setelah video utama jalan, kembali ke dashboard dan klik Cek stream.")
            streams, cookies = detect_stream(url, commands)
            if not streams:
                update(phase="cancelled", message="Pemindaian dibatalkan.", detail="")
                return
            update(phase="review", message="Pilih stream yang mau diunduh.",
                   detail="Cocokkan durasinya dengan video utama sebelum download.",
                   streams=[public for _, _, public in streams])
            try:
                action, index = commands.get(timeout=180)
            except Empty:
                raise RuntimeError("Waktu memilih stream habis. Mulai lagi untuk mendapatkan link yang baru.") from None
            if action == "cancel":
                update(phase="cancelled", message="Download dibatalkan.", detail="", streams=[])
                return
            stream, headers, _ = streams[index]
            update(phase="checking", message="Menghubungi sumber video.", detail="", streams=[])
            with tempfile.TemporaryDirectory(prefix="streamdrop-") as tmp:
                options = []
                for header, flag in (("user-agent", "--user-agent"), ("referer", "--referer")):
                    if headers.get(header):
                        options.extend([flag, headers[header]])
                if headers.get("origin"):
                    options.extend(["--add-headers", "Origin:" + headers["origin"]])
                if cookies:
                    jar = Path(tmp) / "session.txt"
                    cookie_file(jar, cookies)
                    options.extend(["--cookies", str(jar)])
                code, error, file, _ = download(executable, stream, quality, folder, options, output)
        if code:
            if quality != "auto" and "requested format is not available" in error.lower():
                raise RuntimeError(
                    f"Tidak ada format yang terdeteksi pada {quality}p atau lebih rendah. "
                    "Coba link halaman atau playlist yang menyediakan pilihan resolusi."
                )
            raise RuntimeError(error or "Download gagal. Coba lagi dengan link yang baru.")
        if not file:
            raise RuntimeError("yt-dlp selesai tanpa melaporkan file video.")
        path = Path(file).resolve()
        if not path.is_relative_to(folder.resolve()) or not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("File hasil download tidak ditemukan atau kosong di folder tujuan.")
        update(phase="saving", message="Memeriksa resolusi file.", detail="")
        if output and path.suffix.lower() != "." + output:
            raise RuntimeError(f"Format hasil bukan {output.upper()}.")
        detail = "Audio MP3" if output == "mp3" else actual_resolution(path) or "Resolusi hasil belum bisa dibaca."
        update(phase="done", message="Unduhan selesai.", percent=100, detail=detail, file=path.name)
    except (OSError, BrowserError, RuntimeError) as exc:
        message = str(exc) if isinstance(exc, RuntimeError) else str(exc).splitlines()[0]
        update(phase="error", message=redact(message), percent=None, detail="", streams=[])


def execute_job(function, args):
    try:
        function(*args)
    except Exception as exc:
        print(f"KlipLokal: {function.__name__} gagal ({type(exc).__name__}).", file=sys.stderr, flush=True)
        update(phase="error", message="Proses gagal. Coba lagi atau periksa jendela CMD.", percent=None,
               detail="", streams=[])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_data(self, code, data, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, code, body):
        self.send_data(code, json.dumps(body).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        if self.headers.get("Host") != HOST:
            self.send_error(403)
        elif self.path == "/":
            self.send_data(200, (BASE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/job":
            with LOCK:
                snapshot = dict(job)
            self.send_json(200, {**snapshot, "default_folder": str(DEFAULT_FOLDER)})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.headers.get("Host") != HOST or self.headers.get("Origin") != ORIGIN:
            self.send_error(403)
            return
        if self.path not in {"/api/job", "/api/preview", "/api/folder", "/api/file", "/api/scan", "/api/compress"}:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 65536:
                raise ValueError
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            self.send_json(400, {"error": "Permintaan tidak valid."})
            return
        if self.path == "/api/scan":
            self.control_scan(body)
            return
        if self.path in {"/api/job", "/api/preview"}:
            try:
                url = body["url"].strip()
                if not http_url(url):
                    raise ValueError
            except (ValueError, TypeError, KeyError, AttributeError):
                self.send_json(400, {"error": "Masukkan link lengkap yang diawali https:// atau http://."})
                return
            if excluded_platform(url):
                self.send_json(400, {"error": "TikTok dan Instagram tidak didukung. Coba YouTube atau situs video lain."})
                return
            if self.path == "/api/preview":
                try:
                    self.send_json(200, video_preview(url))
                except RuntimeError as exc:
                    self.send_json(422, {"error": str(exc)})
                return
        try:
            folder = destination(body.get("folder", ""))
        except ValueError:
            self.send_json(400, {"error": "Masukkan path folder lengkap, misalnya D:\\Video."})
            return
        if self.path in {"/api/folder", "/api/file"}:
            self.pick_path(folder, pick_file=self.path == "/api/file")
            return
        if self.path == "/api/compress":
            self.start_compression(body, folder)
            return
        quality = body.get("quality", "auto")
        if not isinstance(quality, str) or quality not in QUALITIES:
            self.send_json(400, {"error": "Pilih kualitas yang tersedia."})
            return
        output, browser = body.get("output", ""), body.get("browser", "fallback")
        if output not in ("", "mp4", "mp3") or browser not in ("fallback", "never", "scan"):
            self.send_json(400, {"error": "Pilihan unduhan tidak valid."})
            return
        commands = Queue()
        self.start_job(run_job, (url, quality, folder, commands, output, browser), folder,
                       "Memeriksa link video.", commands)

    def start_compression(self, body, folder):
        try:
            source = body["source"].strip()
            if not source:
                raise ValueError
            source = destination(source)
        except (AttributeError, KeyError, ValueError):
            self.send_json(400, {"error": "Pilih video atau masukkan path file yang lengkap."})
            return
        mode, resolution, preset = (body.get(name) for name in ("mode", "resolution", "preset"))
        if (mode not in ("quality", "target") or not isinstance(resolution, str) or resolution not in RESOLUTIONS
                or not isinstance(preset, str) or preset not in PRESETS):
            self.send_json(400, {"error": "Pilihan kompres tidak valid."})
            return
        target = body.get("target")
        if mode == "target" and (type(target) not in (int, float) or not 1 <= target <= 100_000):
            self.send_json(400, {"error": "Target ukuran harus antara 1 dan 100.000 MB."})
            return
        self.start_job(compress_video, (source, folder, resolution, mode, preset, target, update),
                       folder, "Membaca file video.")

    def start_job(self, function, args, folder, message, commands=None):
        global scan_commands
        with LOCK:
            if job["phase"] in ACTIVE:
                busy = True
            else:
                busy = False
                scan_commands = commands
                job.update(phase="checking", message=message, percent=None, detail="", file="", folder=str(folder),
                           scan_id=uuid4().hex if commands is not None else "", streams=[])
        if busy:
            self.send_json(409, {"error": "Tunggu proses yang sedang berjalan."})
            return
        threading.Thread(target=execute_job, args=(function, args), daemon=True).start()
        self.send_json(202, {"ok": True})

    def control_scan(self, body):
        action = body.get("action")
        index = body.get("index")
        accepted = False
        with LOCK:
            phase = job["phase"]
            if scan_commands is not None and body.get("scan_id") == job["scan_id"]:
                if action == "collect" and phase == "scanning":
                    job.update(phase="collecting", message="Mengecek stream video.", detail="")
                    accepted = True
                elif action == "download" and phase == "review" and type(index) is int and 0 <= index < len(job["streams"]):
                    job.update(phase="checking", message="Menyiapkan download.", detail="")
                    accepted = True
                elif action == "cancel" and phase in {"scanning", "review"}:
                    job.update(phase="checking", message="Membatalkan.", detail="")
                    accepted = True
                if accepted:
                    scan_commands.put((action, index))
        if accepted:
            self.send_json(200, {"ok": True})
        else:
            self.send_json(409, {"error": "Pilihan ini sudah berubah. Cek status terbaru di dashboard."})

    def pick_path(self, initial, pick_file):
        if not PICKER_LOCK.acquire(blocking=False):
            self.send_json(409, {"error": "Jendela pemilih masih terbuka."})
            return
        try:
            result = subprocess.run(
                [sys.executable, str(BASE / "app.py"), "--choose-file" if pick_file else "--choose-folder", str(initial)],
                capture_output=True, check=True, text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.send_json(200, {"file" if pick_file else "folder": json.loads(result.stdout)})
        except (OSError, subprocess.SubprocessError, ValueError):
            self.send_json(500, {"error": "Jendela pemilih gagal dibuka. Ketik path lengkap di kolom yang sesuai."})
        finally:
            PICKER_LOCK.release()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] in {"--choose-folder", "--choose-file"}:
        choose_path(sys.argv[2], pick_file=sys.argv[1] == "--choose-file")
        raise SystemExit
    try:
        with ThreadingHTTPServer(("127.0.0.1", PORT), Handler) as server:
            print(f"KlipLokal: {ORIGIN}\nFolder bawaan: {DEFAULT_FOLDER}\nBiarkan jendela ini terbuka.", flush=True)
            if os.getenv("STREAMDROP_NO_OPEN") != "1":
                webbrowser.open(ORIGIN)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        raise SystemExit(f"Gagal membuka KlipLokal di {ORIGIN}: {exc}")
