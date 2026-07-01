"""macOS GUI launcher window — shown after the wizard when opened from Finder.

Provides three actions (when a Tk GUI is available):
  1. Open Web UI  — starts the FastAPI server in a daemon thread and opens browser
  2. Open Terminal — opens Terminal.app running `ots` via osascript
  3. Configuration — re-runs the setup wizard

On Pythons built without ``_tkinter`` (e.g. some conda / standalone builds used
to bundle the .app), there is no window: ``run_launcher()`` falls back to a
headless mode that simply starts the web server and opens the browser — which is
the app's primary experience anyway. Configuration is then done in the web UI's
Settings tab.
"""
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

_C = {
    "bg":     "#1a1b26",
    "panel":  "#24283b",
    "border": "#3b4261",
    "fg":     "#c8d3f5",
    "muted":  "#545c7e",
    "white":  "#ffffff",
    "orange": "#ff9e64",
    "green":  "#c3e88d",
    "red":    "#f7768e",
}

_W, _H = 340, 360
_WEB_PORT = 7860
_WEB_HOST = "127.0.0.1"
_WEB_URL = f"http://{_WEB_HOST}:{_WEB_PORT}"


def _run_gui() -> None:
    """Show the Tk launcher window. Only called when tkinter imports cleanly."""
    import tkinter as tk

    class _LauncherWindow(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("OctoSlave")
            self.resizable(False, False)
            self.configure(bg=_C["bg"])
            self.geometry(f"{_W}x{_H}")
            self._center()
            self._server_started = False
            self._build()

        def _center(self):
            self.update_idletasks()
            x = (self.winfo_screenwidth()  - _W) // 2
            y = (self.winfo_screenheight() - _H) // 2
            self.geometry(f"{_W}x{_H}+{x}+{y}")

        def _build(self):
            tk.Label(self, text="OctoSlave", font=("Helvetica", 20, "bold"),
                     bg=_C["bg"], fg=_C["orange"]).pack(pady=(32, 4))
            tk.Label(self, text="Autonomous AI Research Assistant",
                     font=("Helvetica", 10), bg=_C["bg"],
                     fg=_C["muted"]).pack(pady=(0, 28))

            for text, cmd, color in [
                ("  Open Web UI  ",    self._open_web,    _C["orange"]),
                ("  Open Terminal  ",  self._open_term,   _C["panel"]),
                ("  Configuration  ",  self._open_config, _C["panel"]),
            ]:
                fg = _C["bg"] if color == _C["orange"] else _C["fg"]
                b = tk.Button(self, text=text, command=cmd,
                              bg=color, fg=fg, relief="flat",
                              font=("Helvetica", 12, "bold") if color == _C["orange"] else ("Helvetica", 11),
                              cursor="hand2", width=22, pady=8,
                              activebackground="#e8894e" if color == _C["orange"] else _C["border"],
                              activeforeground=fg)
                b.pack(pady=6)

            tk.Frame(self, bg=_C["border"], height=1).pack(fill=tk.X, pady=14)

            self._status_var = tk.StringVar(value="Ready")
            tk.Label(self, textvariable=self._status_var,
                     font=("Helvetica", 9), bg=_C["bg"],
                     fg=_C["muted"]).pack()

            tk.Button(self, text="Quit", command=self.destroy,
                      bg=_C["bg"], fg=_C["muted"], relief="flat",
                      font=("Helvetica", 9), cursor="hand2",
                      activebackground=_C["panel"]).pack(pady=(4, 0))

        def _set_status(self, msg: str):
            self._status_var.set(msg)
            self.update_idletasks()

        def _open_web(self):
            if not self._server_started:
                self._set_status("Starting server…")
                self._server_started = True

                def _start():
                    try:
                        _serve()
                    except Exception as exc:
                        self.after(0, lambda: self._set_status(f"Server error: {exc}"))
                        self._server_started = False

                threading.Thread(target=_start, daemon=True).start()
                # Give the server a moment, then open browser
                self.after(1800, self._launch_browser)
            else:
                self._launch_browser()

        def _launch_browser(self):
            webbrowser.open(_WEB_URL)
            self._set_status(f"Web UI running on port {_WEB_PORT}")

        def _open_term(self):
            # Find the binary path (sys.executable in a PyInstaller .app)
            exe = sys.executable
            script = f'tell application "Terminal" to do script "{exe}"'
            try:
                subprocess.Popen(["osascript", "-e", script])
                self._set_status("Opened Terminal")
            except Exception as exc:
                self._set_status(f"Could not open Terminal: {exc}")

        def _open_config(self):
            self.destroy()
            from octoslave.wizard import run_wizard
            run_wizard()

    app = _LauncherWindow()
    app.mainloop()


def _serve() -> None:
    """Start the FastAPI/uvicorn web server (blocking)."""
    import uvicorn
    from octoslave.web.app import app as _web_app
    uvicorn.run(_web_app, host=_WEB_HOST, port=_WEB_PORT, log_level="error")


def _run_headless() -> None:
    """No Tk available: start the web server and open the browser. Blocks while
    the server runs so the .app stays alive until the user quits it."""
    def _open_later():
        time.sleep(1.8)
        try:
            webbrowser.open(_WEB_URL)
        except Exception:
            pass

    threading.Thread(target=_open_later, daemon=True).start()
    print(f"[OctoSlave] Web UI at {_WEB_URL} — quit this app to stop the server.",
          file=sys.stderr)
    _serve()


def run_launcher() -> None:
    """Show the Tk launcher if possible, else fall back to headless web start."""
    try:
        import tkinter  # noqa: F401  (probe only)
    except Exception:
        _run_headless()
        return
    try:
        _run_gui()
    except Exception as exc:
        # A Tk failure at runtime (no display, etc.) shouldn't brick the app.
        print(f"[OctoSlave] GUI launcher unavailable ({exc}); starting web UI.",
              file=sys.stderr)
        _run_headless()
