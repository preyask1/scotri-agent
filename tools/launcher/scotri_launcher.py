"""
Scotri Service Launcher - portable, no-install GUI to start/stop/restart the
three local processes this workspace depends on:

  1. Scotri UI       - `npm run dev` in the scotri-ui checkout   (port 5173)
  2. Scotri service  - the FastAPI/uvicorn app.api:app process   (port 8000)
  3. Redis           - a local redis-server                      (port 6379)

Runs on Windows and macOS/Linux; paths are derived from this file's location
(workspace root = two levels up) and can be overridden with the env vars
SCOTRI_ROOT, SCOTRI_UI_DIR and SCOTRI_REDIS_DIR.

Start it with the venv's interpreter - on Windows pythonw.exe, for no console:
    C:\\Scotri\\services\\.venv\\Scripts\\pythonw.exe C:\\Scotri\\tools\\launcher\\scotri_launcher.py
or double-click the launcher next to this file:
    Windows   "Start Scotri Launcher.bat"
    macOS     "Start Scotri Launcher.command"

Everything here is local process management only - no admin rights, no
installer, no writes outside this launcher's own folder (pid files + logs).
"""

import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import math
import time
import tkinter as tk
import webbrowser
from tkinter import ttk, scrolledtext

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

LAUNCHER_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(LAUNCHER_DIR, "state")
PID_DIR = os.path.join(STATE_DIR, "pids")
LOG_DIR = os.path.join(STATE_DIR, "logs")
os.makedirs(PID_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# Workspace root: tools/launcher/ -> tools/ -> root. Keeps the launcher portable
# (the Windows box has it at C:\Scotri, a Mac at ~/scotri) - override if needed.
SCOTRI_ROOT = os.environ.get("SCOTRI_ROOT") or os.path.dirname(os.path.dirname(LAUNCHER_DIR))
SERVICES_DIR = os.path.join(SCOTRI_ROOT, "services")

# scotri-ui is a sibling checkout, not part of this workspace.
UI_DIR = os.environ.get("SCOTRI_UI_DIR") or os.path.join(os.path.dirname(SCOTRI_ROOT), "scotri-ui")

if IS_WINDOWS:
    VENV_PY = os.path.join(SERVICES_DIR, ".venv", "Scripts", "python.exe")
    NPM = shutil.which("npm.cmd") or shutil.which("npm") or "npm.cmd"
    REDIS_DIR = os.environ.get("SCOTRI_REDIS_DIR") or r"C:\Redis"
    REDIS_BIN = os.path.join(REDIS_DIR, "redis-server.exe")
    UI_FONT, MONO_FONT = "Segoe UI", "Consolas"
else:
    VENV_PY = os.path.join(SERVICES_DIR, ".venv", "bin", "python")
    NPM = shutil.which("npm") or "npm"
    # Homebrew keeps redis-server out of the default PATH on some setups, so
    # check the usual Intel/Apple-silicon prefixes too before giving up.
    REDIS_BIN = (
        shutil.which("redis-server")
        or next((p for p in ("/opt/homebrew/bin/redis-server", "/usr/local/bin/redis-server")
                 if os.path.exists(p)), None)
        or "redis-server"
    )
    REDIS_DIR = os.environ.get("SCOTRI_REDIS_DIR") or STATE_DIR
    UI_FONT, MONO_FONT = ("Helvetica Neue", "Menlo") if IS_MAC else ("DejaVu Sans", "DejaVu Sans Mono")

# A redis.conf next to the binary (Windows layout) or in REDIS_DIR is passed
# through; a bare `redis-server` with no conf is fine for local dev.
REDIS_CONF = next(
    (p for p in (os.path.join(REDIS_DIR, "redis.conf"),
                 os.path.join(os.path.dirname(REDIS_BIN), "redis.conf"))
     if os.path.exists(p)),
    None,
)
# The Windows redis-server is a Cygwin build that fails to open a conf given as
# an absolute drive path ("C:/Redis/redis.conf": No such file or directory), so
# when the conf sits in the service's cwd pass it by bare name, as start.bat does.
if REDIS_CONF and os.path.dirname(REDIS_CONF) == REDIS_DIR:
    REDIS_CONF = os.path.basename(REDIS_CONF)
REDIS_CMD = [REDIS_BIN] + ([REDIS_CONF] if REDIS_CONF else [])

SERVICES = [
    {
        "key": "scotri_ui",
        "label": "Scotri UI",
        "cwd": UI_DIR,
        "cmd": [NPM, "run", "dev"],
        "port": 5173,
        "url": "http://localhost:5173",
        "note": "Vite dev server",
    },
    {
        "key": "scotri_service",
        "label": "Scotri (service)",
        "cwd": SERVICES_DIR,
        "cmd": [VENV_PY, "-m", "uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"],
        "port": 8000,
        "url": "http://localhost:8000/docs",
        "note": "FastAPI - link opens Swagger docs",
    },
    {
        "key": "redis",
        "label": "Redis",
        "cwd": REDIS_DIR,
        "cmd": REDIS_CMD,
        "port": 6379,
        "url": None,
        "note": "No web UI",
    },
]

# The admin panel is the API's own page (app/static/admin.html). scotri-ui proxies /admin to it, so
# prefer the UI's origin when the UI is up and fall back to the API's own port.
ADMIN_URLS = {"scotri_ui": "http://localhost:5173/admin", "scotri_service": "http://localhost:8000/admin"}

# --- Brand mark (docs/brand/README.md) -------------------------------------------------------------
# Tk has no SVG, so the mark is drawn from the brand geometry: two quadratic arcs, the second the first
# turned 180 degrees about (24, 24), and a filled pupil, in a 48-unit box. Ink on paper; the Verified
# teal appears only as the app icon's pupil, as in docs/brand/favicon.svg.
INK, PAPER, VERIFIED_DARK = "#12151A", "#F6F4EF", "#5FB3A6"
MARK_ARCS = (((14.5, 16.2), (27, 7.5), (41, 24)), ((33.5, 31.8), (21, 40.5), (7, 24)))


def _arc_points(arc, steps=24, upto=1.0):
    """Points along one quadratic arc, from its start to fraction `upto` of the way along."""
    (x0, y0), (x1, y1), (x2, y2) = arc
    n = max(1, round(steps * upto))
    pts = []
    for i in range(n + 1):
        t = upto * i / n
        u = 1 - t
        pts.append((u * u * x0 + 2 * u * t * x1 + t * t * x2, u * u * y0 + 2 * u * t * y1 + t * t * y2))
    return pts


def _rotate(pts, degrees):
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    return [(24 + (x - 24) * c - (y - 24) * s, 24 + (x - 24) * s + (y - 24) * c) for x, y in pts]


def make_icon_image(size=64):
    """The favicon tile (rounded ink square, paper arcs at stroke 5.2 scaled 0.74, Verified pupil) as a
    Tk PhotoImage for the window/dock icon. Rendered pixel by pixel; there is no image library here."""
    img = tk.PhotoImage(width=size, height=size)
    k = 48 / size
    segs = []
    for arc in MARK_ARCS:
        pts = [(24 + (x - 24) * 0.74, 24 + (y - 24) * 0.74) for x, y in _arc_points(arc, 40)]
        segs += list(zip(pts, pts[1:]))
    half_w, pupil_r = 5.2 * 0.74 / 2, 4.6 * 0.74

    def near_arc(px, py):
        for (ax, ay), (bx, by) in segs:
            dx, dy = bx - ax, by - ay
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
            if (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2 <= half_w * half_w:
                return True
        return False

    def in_tile(px, py, r=10):
        cx, cy = min(max(px, r), 48 - r), min(max(py, r), 48 - r)
        return (px - cx) ** 2 + (py - cy) ** 2 <= r * r

    rows = []
    for j in range(size):
        row = []
        for i in range(size):
            px, py = (i + 0.5) * k, (j + 0.5) * k
            if not in_tile(px, py):
                row.append(None)
            elif (px - 24) ** 2 + (py - 24) ** 2 <= pupil_r * pupil_r:
                row.append(VERIFIED_DARK)
            elif near_arc(px, py):
                row.append(PAPER)
            else:
                row.append(INK)
        rows.append(row)
    for j, row in enumerate(rows):
        img.put("{" + " ".join(c or INK for c in row) + "}", to=(0, j))
        for i, c in enumerate(row):
            if c is None:
                img.transparency_set(i, j, True)
    return img


class MarkCanvas(tk.Canvas):
    """The mark at 32 px (optical scale: stroke 4.6, pupil r 4.1), animated per the brand's motion
    rules: `trace` once at start-up, then `scan` (a turn about its own centre, 180 degrees per 2.4 s)
    while any service action is in flight. Scan always comes to rest on a whole half-turn, which is
    the start pose again, so the mark never stops tilted."""

    SIZE, STROKE, PUPIL = 32, 4.6, 4.1
    TRACE_S, TRACE_STAGGER_S, SCAN_S, FRAME_MS = 1.5, 0.14, 2.4, 33

    def __init__(self, parent, color, background):
        super().__init__(parent, width=self.SIZE, height=self.SIZE, highlightthickness=0, bd=0, bg=background)
        self.color = color
        self.angle = 0.0
        self.scanning = False
        self.started = time.monotonic()
        self.last = None
        self._tick()

    def _px(self, pts):
        k = self.SIZE / 48
        return [c * k for p in pts for c in p]

    def _draw(self, arc_fracs, pupil_scale):
        self.delete("all")
        k = self.SIZE / 48
        for arc, frac in zip(MARK_ARCS, arc_fracs):
            if frac <= 0:
                continue
            pts = _rotate(_arc_points(arc, upto=frac), self.angle)
            self.create_line(*self._px(pts), fill=self.color, width=self.STROKE * k, capstyle="round", joinstyle="round")
        if pupil_scale > 0:
            r = self.PUPIL * pupil_scale * k
            c = 24 * k
            self.create_oval(c - r, c - r, c + r, c + r, fill=self.color, outline="")

    def _tick(self):
        now = time.monotonic()
        t = now - self.started
        if t < self.TRACE_S + self.TRACE_STAGGER_S:
            # scotri-draw: each arc draws in over the first 55% of the trace; scotri-pupil-in: the pupil
            # appears from 45%, overshoots to 1.22 at 72%, and settles at 1.
            ease = lambda x: 1 - (1 - max(0.0, min(1.0, x))) ** 3
            fracs = [ease(t / (self.TRACE_S * 0.55)), ease((t - self.TRACE_STAGGER_S) / (self.TRACE_S * 0.55))]
            p = t / self.TRACE_S
            pupil = 0 if p < 0.45 else (1.22 * (p - 0.45) / 0.27 if p < 0.72 else 1.22 - 0.22 * min(1.0, (p - 0.72) / 0.28))
            self._draw(fracs, pupil)
        else:
            if self.last is not None and (self.scanning or self.angle % 180 > 0.01):
                step = 180 * (now - self.last) / self.SCAN_S
                nxt = self.angle + step
                if not self.scanning and math.floor(nxt / 180) > math.floor(self.angle / 180):
                    nxt = math.floor(nxt / 180) * 180  # come to rest on the half-turn
                self.angle = nxt % 360
            self._draw([1, 1], 1)
        self.last = now
        self.after(self.FRAME_MS, self._tick)

    def set_scanning(self, on):
        self.scanning = on


CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
CREATE_NEW_PROCESS_GROUP = 0x00000200 if IS_WINDOWS else 0


# Popen handles for everything we launched, so they can be reaped. On POSIX a
# dead child stays a zombie until its parent waits on it, and os.kill(pid, 0)
# keeps succeeding for a zombie - which would pin a stopped row on "Running".
_CHILDREN = {}


def _reap_children():
    for pid, proc in list(_CHILDREN.items()):
        if proc.poll() is not None:  # non-blocking wait; clears the zombie
            _CHILDREN.pop(pid, None)


def _quiet_run(cmd):
    """Run a short helper command without flashing a console window (Windows)."""
    kwargs = {"capture_output": True, "text": True}
    if IS_WINDOWS:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        return subprocess.run(cmd, **kwargs)
    except OSError:
        return None


def pid_file(key):
    return os.path.join(PID_DIR, f"{key}.pid")


def log_file(key):
    return os.path.join(LOG_DIR, f"{key}.log")


def read_pid(key):
    path = pid_file(key)
    if not os.path.exists(path):
        return None
    try:
        return int(open(path).read().strip())
    except (ValueError, OSError):
        return None


def is_running(pid):
    if pid is None:
        return False
    if not IS_WINDOWS:
        _reap_children()
    if IS_WINDOWS:
        result = _quiet_run(["tasklist", "/FI", f"PID eq {pid}", "/NH"])
        return bool(result) and str(pid) in result.stdout
    try:
        os.kill(pid, 0)  # signal 0 = existence check only, kills nothing
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, just not ours to signal
    return True


def is_port_listening(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def is_service_running(svc):
    # The tracked PID is the process we launched directly (e.g. npm),
    # but npm/node can hand off to a grandchild and exit that wrapper PID
    # while the real server keeps serving - so treat either signal as "running".
    return is_running(read_pid(svc["key"])) or is_port_listening(svc["port"])


def is_launchable(svc):
    """False when the executable this row needs isn't on this machine at all."""
    exe = svc["cmd"][0]
    return bool(shutil.which(exe) or os.path.exists(exe))


def start_service(svc):
    key = svc["key"]
    if is_service_running(svc):
        return  # already running (tracked PID alive, or port already serving)
    log_path = log_file(key)
    log_handle = open(log_path, "a", encoding="utf-8", errors="replace")
    log_handle.write(f"\n--- start: {' '.join(svc['cmd'])} (cwd={svc['cwd']}) ---\n")
    if not os.path.isdir(svc["cwd"]):
        log_handle.write(f"!!! working directory does not exist: {svc['cwd']}\n")
        log_handle.close()
        return
    log_handle.flush()
    popen_kwargs = {}
    if IS_WINDOWS:
        popen_kwargs["creationflags"] = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    else:
        # Own session/process group, so stop_service can signal the whole tree
        # (npm -> node) the way taskkill /T does on Windows.
        popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            svc["cmd"],
            cwd=svc["cwd"],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            **popen_kwargs,
        )
    except OSError as exc:
        log_handle.write(f"!!! failed to launch: {exc}\n")
        log_handle.close()
        return
    if not IS_WINDOWS:
        _CHILDREN[proc.pid] = proc
    with open(pid_file(key), "w") as f:
        f.write(str(proc.pid))


def pids_owning_port(port):
    if IS_WINDOWS:
        result = _quiet_run(["netstat", "-ano", "-p", "TCP"])
        pids = set()
        if not result:
            return pids
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 5 or parts[0] != "TCP":
                continue
            local_addr, state, owning_pid = parts[1], parts[3], parts[-1]
            if state == "LISTENING" and local_addr.endswith(f":{port}"):
                try:
                    pids.add(int(owning_pid))
                except ValueError:
                    pass
        return pids
    # macOS/Linux: lsof is the portable equivalent of netstat -ano.
    result = _quiet_run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"])
    if not result:
        return set()
    return {int(line) for line in result.stdout.split() if line.strip().isdigit()}


def _terminate_posix(pid):
    """SIGTERM the process group, then SIGKILL whatever is still up."""
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        pgid = None
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        if sig is signal.SIGTERM:
            for _ in range(20):  # up to ~2s for a clean exit before SIGKILL
                if not is_running(pid):
                    break
                threading.Event().wait(0.1)
            else:
                continue  # still up - fall through to SIGKILL
            break
    _reap_children()


def stop_service(svc):
    key = svc["key"]
    pids = set()
    tracked_pid = read_pid(key)
    if tracked_pid is not None:
        pids.add(tracked_pid)
    # Covers processes started outside the launcher, or where the tracked
    # PID (e.g. npm) already exited while a grandchild kept serving.
    if is_port_listening(svc["port"]):
        pids |= pids_owning_port(svc["port"])
    for pid in pids:
        if IS_WINDOWS:
            # /T kills the whole process tree - needed for npm.cmd -> node.exe
            _quiet_run(["taskkill", "/F", "/T", "/PID", str(pid)])
        else:
            _terminate_posix(pid)
    path = pid_file(key)
    if os.path.exists(path):
        os.remove(path)


def restart_service(svc):
    stop_service(svc)
    start_service(svc)


class LauncherApp:
    def __init__(self, root):
        self.root = root
        root.title("Scotri Service Launcher")
        root.geometry("820x460")
        root.resizable(False, False)

        try:
            self.icon = make_icon_image()
            root.iconphoto(True, self.icon)
        except tk.TclError:
            self.icon = None  # an icon is cosmetic; never block the launcher on it

        header = ttk.Frame(root, padding=10)
        header.pack(fill="x")
        bg = ttk.Style().lookup("TFrame", "background") or root.cget("bg")
        r, g, b = (v / 65535 for v in root.winfo_rgb(bg))
        dark = 0.2126 * r + 0.7152 * g + 0.0722 * b < 0.5
        self.mark = MarkCanvas(header, PAPER if dark else INK, bg)
        self.mark.pack(side="left", padx=(0, 10))
        self.admin_btn = ttk.Button(header, text="Open Admin Panel", command=self.open_admin)
        self.admin_btn.pack(side="right", anchor="n")
        titles = ttk.Frame(header)
        titles.pack(side="left", fill="x")
        ttk.Label(titles, text="Scotri Service Launcher", font=(UI_FONT, 13, "bold")).pack(anchor="w")
        ttk.Label(titles, text="Local process control only - no admin rights required.", foreground="#666").pack(anchor="w")

        table = ttk.Frame(root, padding=(10, 0, 10, 10))
        table.pack(fill="x")

        self.status_labels = {}
        self.link_labels = {}
        self.buttons = {}
        self.busy = {}          # key -> "Starting" / "Stopping" / "Restarting" while an action runs
        self.dot_frame = 0
        self.done_queue = queue.Queue()  # worker threads only ever push here - never touch Tk directly
        for i, svc in enumerate(SERVICES):
            key = svc["key"]
            row = i * 2
            ttk.Label(table, text=svc["label"], width=16, font=(UI_FONT, 10, "bold")).grid(row=row, column=0, sticky="w", pady=(6, 0))

            status_lbl = ttk.Label(table, text="checking...", width=10)
            status_lbl.grid(row=row, column=1, sticky="w", pady=(6, 0))
            self.status_labels[key] = status_lbl

            ttk.Label(table, text=f"port {svc['port']}", width=10, foreground="#666").grid(row=row, column=2, sticky="w", pady=(6, 0))

            link_lbl = ttk.Label(table, width=26)
            link_lbl.grid(row=row, column=3, sticky="w", pady=(6, 0))
            if svc["url"]:
                link_lbl.bind("<Button-1>", lambda e, u=svc["url"]: webbrowser.open(u))
            self.link_labels[key] = link_lbl

            start_btn = ttk.Button(table, text="Start", width=8, command=lambda s=svc: self.run_action("Starting", start_service, s))
            stop_btn = ttk.Button(table, text="Stop", width=8, command=lambda s=svc: self.run_action("Stopping", stop_service, s))
            restart_btn = ttk.Button(table, text="Restart", width=8, command=lambda s=svc: self.run_action("Restarting", restart_service, s))
            start_btn.grid(row=row, column=4, padx=3, pady=(6, 0))
            stop_btn.grid(row=row, column=5, padx=3, pady=(6, 0))
            restart_btn.grid(row=row, column=6, padx=3, pady=(6, 0))
            self.buttons[key] = {"start": start_btn, "stop": stop_btn, "restart": restart_btn}

            note = svc.get("note", "")
            if not is_launchable(svc):
                note = f"{note} - {os.path.basename(svc['cmd'][0])} not found on this machine"
            ttk.Label(table, text=note, foreground="#999", font=(UI_FONT, 8)).grid(
                row=row + 1, column=0, columnspan=7, sticky="w")

        ttk.Separator(root).pack(fill="x", padx=10)

        log_frame = ttk.Frame(root, padding=10)
        log_frame.pack(fill="both", expand=True)
        ttk.Label(log_frame, text="Recent log output (all services):").pack(anchor="w")
        self.log_box = scrolledtext.ScrolledText(log_frame, height=14, state="disabled", font=(MONO_FONT, 9))
        self.log_box.pack(fill="both", expand=True)

        self.refresh_status()
        self.animate_busy()
        self.refresh_logs()

    def run_action(self, verb, fn, svc):
        key = svc["key"]
        if self.busy.get(key):
            return  # an action is already in flight for this row - ignore the extra click

        self.busy[key] = verb
        self.render_row(svc)  # immediate feedback: buttons disable, status shows the verb

        def worker():
            try:
                fn(svc)
            finally:
                # Never touch Tk widgets from this thread - just signal completion
                # and let the main thread's own animate_busy tick pick it up.
                self.done_queue.put(svc)

        threading.Thread(target=worker, daemon=True).start()

    def render_row(self, svc):
        key = svc["key"]
        verb = self.busy.get(key)
        status_lbl = self.status_labels[key]
        link_lbl = self.link_labels[key]
        buttons = self.buttons[key]

        if verb:
            dots = "." * ((self.dot_frame % 3) + 1)
            status_lbl.config(text=f"{verb}{dots}", foreground="#b8860b")
            buttons["start"].config(state="disabled")
            buttons["stop"].config(state="disabled")
            buttons["restart"].config(state="disabled")
            return

        running = is_service_running(svc)
        launchable = running or is_launchable(svc)
        status_lbl.config(text="Running" if running else ("Stopped" if launchable else "Not found"),
                           foreground="#1a7f37" if running else ("#888" if launchable else "#b35900"))

        if not svc["url"]:
            link_lbl.config(text="no web UI", foreground="#aaa", cursor="")
        elif running:
            link_lbl.config(text=svc["url"], foreground="#0645ad", cursor="hand2", font=(UI_FONT, 9, "underline"))
        else:
            link_lbl.config(text=svc["url"], foreground="#aaa", cursor="", font=(UI_FONT, 9))

        buttons["start"].config(state="disabled" if running or not launchable else "normal")
        buttons["stop"].config(state="normal" if running else "disabled")
        buttons["restart"].config(state="normal" if running else "disabled")

    def admin_url(self):
        """The admin panel's URL, or None while the API that serves it is down."""
        running = {svc["key"] for svc in SERVICES if svc["key"] in ADMIN_URLS and is_service_running(svc)}
        if "scotri_service" not in running:
            return None
        return ADMIN_URLS["scotri_ui" if "scotri_ui" in running else "scotri_service"]

    def open_admin(self):
        url = self.admin_url()
        if url:
            webbrowser.open(url)

    def refresh_status(self):
        for svc in SERVICES:
            self.render_row(svc)
        self.admin_btn.config(state="normal" if self.admin_url() else "disabled")
        self.root.after(2000, self.refresh_status)

    def animate_busy(self):
        # Drain completions from background workers here, on the main thread -
        # workers never call into Tk themselves (that's what threw
        # "RuntimeError: main thread is not in main loop" before this fix).
        while True:
            try:
                finished_svc = self.done_queue.get_nowait()
            except queue.Empty:
                break
            self.busy[finished_svc["key"]] = None
            self.render_row(finished_svc)

        # Advances the "Starting..." / "Stopping..." dot animation independently
        # of the slower 2s status poll, so it visibly moves while a background
        # start/stop/restart is still in flight.
        self.mark.set_scanning(any(self.busy.values()))
        if any(self.busy.values()):
            self.dot_frame += 1
            for svc in SERVICES:
                if self.busy.get(svc["key"]):
                    self.render_row(svc)
        self.root.after(400, self.animate_busy)

    def refresh_logs(self):
        lines = []
        for svc in SERVICES:
            path = log_file(svc["key"])
            if not os.path.exists(path):
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    tail = f.readlines()[-8:]
            except OSError:
                tail = []
            for line in tail:
                lines.append(f"[{svc['label']}] {line.rstrip()}")
        self.log_box.config(state="normal")
        self.log_box.delete("1.0", tk.END)
        self.log_box.insert(tk.END, "\n".join(lines[-200:]))
        self.log_box.see(tk.END)
        self.log_box.config(state="disabled")
        self.root.after(3000, self.refresh_logs)


def main():
    root = tk.Tk()
    LauncherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
