"""OctoSlave Setup Wizard — cross-platform tkinter GUI for first-run configuration.

Called automatically when running a frozen (PyInstaller) binary with no config file.
Can also be triggered manually via: ots config --wizard
"""
import json
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox

# ---------------------------------------------------------------------------
# API-key registration pages — where users generate the key for each backend
# ---------------------------------------------------------------------------
_EINFRA_SIGNUP_URL = "https://llm.ai.e-infra.cz"
_NIM_SIGNUP_URL    = "https://build.nvidia.com"
_OLLAMA_DOWNLOAD_URL = "https://ollama.com/download"

# ---------------------------------------------------------------------------
# Constants (mirrors config.py to avoid import cycles during early startup)
# ---------------------------------------------------------------------------
_CONFIG_DIR  = Path.home() / ".octoslave"
_CONFIG_FILE = _CONFIG_DIR / "config.json"

_EINFRA_URL = "https://llm.ai.e-infra.cz/v1"
_NIM_URL    = "https://integrate.api.nvidia.com/v1"
_OLLAMA_URL = "http://localhost:11434/v1"

_EINFRA_MODELS = [
    "kimi-k2.6",
    "deepseek-v3.2",
    "deepseek-v3.2-thinking",
    "qwen3.5",
    "qwen3.5-122b",
    "qwen3-coder",
    "qwen3-coder-30b",
    "kimi-k2.5",
    "mistral-medium-3.5",
    "llama-4-scout-17b-16e-instruct",
    "gemma4",
    "glm-5",
]

_NIM_MODELS = [
    "meta/llama-4-maverick-17b-128e-instruct",
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/llama-3.3-nemotron-super-49b-v1",
    "meta/llama-3.3-70b-instruct",
    "qwen/qwen3.5-122b-a10b",
    "deepseek-ai/deepseek-v3.2",
    "google/gemma-4-31b-it",
    "microsoft/phi-4-mini-instruct",
]

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
_C = {
    "bg":      "#1a1b26",
    "panel":   "#24283b",
    "border":  "#3b4261",
    "fg":      "#c8d3f5",
    "muted":   "#545c7e",
    "white":   "#ffffff",
    "orange":  "#ff9e64",
    "green":   "#c3e88d",
    "red":     "#f7768e",
    "blue":    "#7aa2f7",
    "btn_act": "#ff9e64",
    "btn_ins": "#3b4261",
    "btn_fg":  "#1a1b26",
}

_W, _H = 740, 540
_SIDE  = 170


class _Page(tk.Frame):
    """Base class for wizard pages."""
    def __init__(self, parent: tk.Frame):
        super().__init__(parent, bg=_C["bg"])

    def heading(self, title: str, subtitle: str = "") -> None:
        tk.Label(self, text=title, font=("Helvetica", 17, "bold"),
                 bg=_C["bg"], fg=_C["white"]).pack(anchor="w", pady=(0, 2))
        if subtitle:
            tk.Label(self, text=subtitle, font=("Helvetica", 10),
                     bg=_C["bg"], fg=_C["muted"], wraplength=510,
                     justify="left").pack(anchor="w", pady=(0, 14))

    def labeled_entry(self, parent: tk.Widget, label: str, var: tk.Variable,
                      show: str | None = None, hint: str = "") -> tk.Entry:
        tk.Label(parent, text=label, font=("Helvetica", 10, "bold"),
                 bg=_C["bg"], fg=_C["fg"]).pack(anchor="w", pady=(10, 2))
        e = tk.Entry(parent, textvariable=var, font=("Helvetica", 11),
                     bg=_C["panel"], fg=_C["white"], insertbackground=_C["white"],
                     relief="flat", bd=6, show=show or "")
        e.pack(fill=tk.X, ipady=5)
        if hint:
            tk.Label(parent, text=hint, font=("Helvetica", 8),
                     bg=_C["bg"], fg=_C["muted"]).pack(anchor="w")
        return e

    def link(self, parent: tk.Widget, text: str, url: str,
             bg: str | None = None) -> tk.Label:
        """A clickable hyperlink that opens `url` in the default browser."""
        bg = bg or _C["bg"]
        lbl = tk.Label(parent, text=text, font=("Helvetica", 10, "underline"),
                       bg=bg, fg=_C["blue"], cursor="hand2", anchor="w")

        def _open(_event=None, u=url):
            try:
                webbrowser.open(u)
            except Exception:
                pass

        lbl.bind("<Button-1>", _open)
        lbl.bind("<Enter>", lambda e: lbl.config(fg=_C["orange"]))
        lbl.bind("<Leave>", lambda e: lbl.config(fg=_C["blue"]))
        return lbl

    def card(self, text: str, subtext: str = "") -> tk.Frame:
        f = tk.Frame(self, bg=_C["panel"], padx=12, pady=8)
        f.pack(fill=tk.X, pady=4)
        tk.Label(f, text=text, font=("Helvetica", 10, "bold"),
                 bg=_C["panel"], fg=_C["orange"], anchor="w").pack(fill=tk.X)
        if subtext:
            tk.Label(f, text=subtext, font=("Helvetica", 9),
                     bg=_C["panel"], fg=_C["muted"], anchor="w",
                     wraplength=480, justify="left").pack(fill=tk.X)
        return f


# ---------------------------------------------------------------------------
# Individual pages
# ---------------------------------------------------------------------------

class _PageWelcome(_Page):
    def __init__(self, parent):
        super().__init__(parent)
        tk.Label(self, text="", bg=_C["bg"]).pack(pady=18)
        self.heading("Welcome to OctoSlave",
                     "Autonomous AI research & coding assistant\n"
                     "for scientists and engineers.")
        self.card("Multi-agent research pipeline",
                  "Run 8 specialist agents in parallel — researcher, skeptic, coder, evaluator and more.")
        self.card("Web UI + Interactive Terminal",
                  "Browser interface or TUI — your choice. Zero extra software required.")
        self.card("Multiple AI backends",
                  "e-INFRA CZ (free for .cz researchers), NVIDIA NIM, or a local Ollama instance.")
        tk.Label(self, text="\nThis wizard will set up your API credentials and default model.",
                 font=("Helvetica", 10), bg=_C["bg"], fg=_C["muted"]).pack(anchor="w")


class _PageBackend(_Page):
    def __init__(self, parent, backend_var: tk.StringVar, on_change):
        super().__init__(parent)
        self.heading("Choose Your Backend",
                     "Select the AI service you want to connect to.")
        backends = [
            ("einfra", "e-INFRA CZ  (Recommended)",
             "Czech research infrastructure — free API for .cz researchers.",
             _EINFRA_SIGNUP_URL, "🔗  Generate your free API key  →"),
            ("nim",    "NVIDIA NIM",
             "Cloud-hosted open-weight models. Free tier available.",
             _NIM_SIGNUP_URL, "🔗  Get your API key  →"),
            ("ollama", "Ollama  (Local / Offline)",
             "100 % private, runs on your machine. No API key needed.",
             _OLLAMA_DOWNLOAD_URL, "🔗  Download Ollama  →"),
        ]
        for value, label, desc, url, link_text in backends:
            f = tk.Frame(self, bg=_C["panel"], padx=14, pady=10, cursor="hand2")
            f.pack(fill=tk.X, pady=5)
            rb = tk.Radiobutton(f, text=label, variable=backend_var, value=value,
                                font=("Helvetica", 11, "bold"), bg=_C["panel"],
                                fg=_C["white"], selectcolor=_C["panel"],
                                activebackground=_C["panel"], cursor="hand2",
                                command=on_change)
            rb.pack(anchor="w")
            tk.Label(f, text=desc, font=("Helvetica", 9), bg=_C["panel"],
                     fg=_C["muted"], wraplength=470, justify="left").pack(anchor="w", padx=24)
            self.link(f, link_text, url, bg=_C["panel"]).pack(anchor="w", padx=24, pady=(3, 0))
            # clicking frame/radiobutton selects (link clicks open the browser)
            for widget in (f, rb):
                widget.bind("<Button-1>", lambda e, v=value: (backend_var.set(v), on_change()))


class _PageCredentials(_Page):
    def __init__(self, parent, backend: str, key_var, url_var, ollama_var):
        super().__init__(parent)
        if backend == "einfra":
            self.heading("e-INFRA CZ — API Credentials",
                         "Free for Czech research organisations (.cz accounts).")
            self.link(self, f"🔗  Click here to generate your API key  —  {_EINFRA_SIGNUP_URL}",
                      _EINFRA_SIGNUP_URL).pack(anchor="w", pady=(0, 6))
            self.labeled_entry(self, "API Key", key_var, show="•",
                               hint="Paste your e-INFRA CZ key (usually starts with sk-…)")
            self.labeled_entry(self, "API Base URL", url_var,
                               hint="Leave as-is unless you have a custom endpoint.")
        elif backend == "nim":
            self.heading("NVIDIA NIM — API Credentials",
                         "Free developer tier available.")
            self.link(self, f"🔗  Click here to generate your API key  —  {_NIM_SIGNUP_URL}",
                      _NIM_SIGNUP_URL).pack(anchor="w", pady=(0, 6))
            self.labeled_entry(self, "NIM API Key", key_var, show="•",
                               hint="Paste your NIM key (starts with nvapi-…)")
            self.labeled_entry(self, "NIM Base URL", url_var,
                               hint="Leave as-is unless you use a self-hosted NIM endpoint.")
        else:
            self.heading("Ollama — Local Configuration",
                         "Ollama must already be installed and running on your machine.\n"
                         "No API key required — everything stays on your device.")
            tk.Label(self, text="No API key needed — Ollama is fully local.",
                     font=("Helvetica", 10, "bold"), bg=_C["bg"],
                     fg=_C["green"]).pack(anchor="w", pady=(0, 6))
            self.link(self, f"🔗  Download & install Ollama  —  {_OLLAMA_DOWNLOAD_URL}",
                      _OLLAMA_DOWNLOAD_URL).pack(anchor="w", pady=(0, 10))
            self.labeled_entry(self, "Ollama API URL", ollama_var,
                               hint="Default: http://localhost:11434/v1")
            tk.Label(self,
                     text="To pull a model before first use, open a terminal and run:\n"
                          "  ollama pull llama3.2",
                     font=("Courier", 9), bg=_C["bg"],
                     fg=_C["muted"]).pack(anchor="w", pady=(12, 0))


class _PageModel(_Page):
    def __init__(self, parent, backend: str, model_var: tk.StringVar):
        super().__init__(parent)
        if backend == "ollama":
            self.heading("Default Model",
                         "Enter the name of a model you have already pulled with Ollama.")
            self.labeled_entry(self, "Model name", model_var,
                               hint="e.g. llama3.2  |  mistral  |  phi4  |  gemma3:12b")
            return

        models = _EINFRA_MODELS if backend == "einfra" else _NIM_MODELS
        self.heading("Select Default Model",
                     "This model will be used for interactive chat and one-shot runs.\n"
                     "You can change it at any time with: ots config  or  /model")

        frame = tk.Frame(self, bg=_C["panel"])
        frame.pack(fill=tk.BOTH, expand=True)
        sb = tk.Scrollbar(frame, troughcolor=_C["panel"])
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        lb = tk.Listbox(frame, yscrollcommand=sb.set, bg=_C["panel"],
                        fg=_C["fg"], selectbackground=_C["orange"],
                        selectforeground=_C["btn_fg"],
                        font=("Courier", 10), relief="flat", bd=0,
                        activestyle="none", highlightthickness=0)
        lb.pack(fill=tk.BOTH, expand=True)
        sb.config(command=lb.yview)
        for m in models:
            lb.insert(tk.END, f"  {m}")
        try:
            idx = models.index(model_var.get())
        except ValueError:
            idx = 0
        lb.selection_set(idx)
        lb.see(idx)

        def _pick(event):
            sel = lb.curselection()
            if sel:
                model_var.set(models[sel[0]])
        lb.bind("<<ListboxSelect>>", _pick)


class _PageTest(_Page):
    def __init__(self, parent, backend: str, key_var, url_var, ollama_var, model_var):
        super().__init__(parent)
        self._backend   = backend
        self._key_var   = key_var
        self._url_var   = url_var
        self._ollama_var = ollama_var
        self._model_var  = model_var

        self.heading("Test Connection",
                     "Verify that OctoSlave can reach the selected backend.")

        info = tk.Frame(self, bg=_C["panel"], padx=12, pady=10)
        info.pack(fill=tk.X, pady=(0, 14))
        label = {"einfra": "e-INFRA CZ", "nim": "NVIDIA NIM", "ollama": "Ollama"}[backend]
        tk.Label(info, text=f"Backend:  {label}", font=("Courier", 10),
                 bg=_C["panel"], fg=_C["fg"]).pack(anchor="w")
        if backend != "ollama":
            k = key_var.get()
            masked = (k[:6] + "…" + k[-3:]) if len(k) > 9 else ("*" * len(k))
            tk.Label(info, text=f"API Key:  {masked}", font=("Courier", 10),
                     bg=_C["panel"], fg=_C["fg"]).pack(anchor="w")
        tk.Label(info, text=f"Model:    {model_var.get()}", font=("Courier", 10),
                 bg=_C["panel"], fg=_C["fg"]).pack(anchor="w")

        self._status_var = tk.StringVar(value="")
        self._status_lbl = tk.Label(self, textvariable=self._status_var,
                                    font=("Helvetica", 10), bg=_C["bg"],
                                    fg=_C["muted"], wraplength=500)
        self._status_lbl.pack(pady=6)

        self._btn = tk.Button(self, text="   Test Connection   ",
                              command=self._run,
                              bg=_C["btn_act"], fg=_C["btn_fg"],
                              font=("Helvetica", 11, "bold"), relief="flat",
                              cursor="hand2", activebackground="#e8894e",
                              activeforeground=_C["btn_fg"])
        self._btn.pack(pady=4)

        tk.Label(self,
                 text="\nYou can skip the test — run 'ots config' later to verify your settings.",
                 font=("Helvetica", 9), bg=_C["bg"], fg=_C["muted"]).pack()

    def _run(self):
        self._btn.config(state=tk.DISABLED, text="   Testing…   ")
        self._status_var.set("Connecting…")
        self._status_lbl.config(fg=_C["muted"])
        self.update()

        def _worker():
            try:
                import openai
                if self._backend == "ollama":
                    client = openai.OpenAI(
                        api_key="ollama",
                        base_url=self._ollama_var.get().rstrip("/"),
                    )
                else:
                    client = openai.OpenAI(
                        api_key=self._key_var.get().strip(),
                        base_url=self._url_var.get().rstrip("/"),
                    )
                resp = client.chat.completions.create(
                    model=self._model_var.get(),
                    messages=[{"role": "user", "content": "Reply with the single word OK."}],
                    max_tokens=8,
                )
                _ = resp.choices[0].message.content
                self.after(0, lambda: self._done(True, "Connection successful!"))
            except Exception as exc:
                self.after(0, lambda: self._done(False, f"Error: {exc}"))

        threading.Thread(target=_worker, daemon=True).start()

    def _done(self, ok: bool, msg: str):
        self._btn.config(state=tk.NORMAL, text="   Test Connection   ")
        self._status_var.set(msg)
        self._status_lbl.config(fg=_C["green"] if ok else _C["red"])


class _PageDone(_Page):
    def __init__(self, parent, backend: str):
        super().__init__(parent)
        tk.Label(self, text="", bg=_C["bg"]).pack(pady=20)
        tk.Label(self, text="Setup Complete!", font=("Helvetica", 22, "bold"),
                 bg=_C["bg"], fg=_C["green"]).pack()
        tk.Label(self, text="OctoSlave is configured and ready.",
                 font=("Helvetica", 11), bg=_C["bg"], fg=_C["muted"]).pack(pady=6)
        tk.Frame(self, bg=_C["border"], height=1).pack(fill=tk.X, pady=16)

        tk.Label(self, text="How to run:", font=("Helvetica", 10, "bold"),
                 bg=_C["bg"], fg=_C["fg"]).pack(anchor="w")
        cmds = [
            ("ots",             "Interactive TUI assistant"),
            ("ots web",         "Browser-based Web UI (http://127.0.0.1:7860)"),
            ("ots run 'task'",  "One-shot task execution"),
            ("ots config",      "Change settings at any time"),
        ]
        for cmd, desc in cmds:
            row = tk.Frame(self, bg=_C["bg"])
            row.pack(fill=tk.X, pady=2)
            tk.Label(row, text=f"  {cmd:<22}", font=("Courier", 10),
                     bg=_C["bg"], fg=_C["orange"]).pack(side=tk.LEFT)
            tk.Label(row, text=desc, font=("Helvetica", 10),
                     bg=_C["bg"], fg=_C["muted"]).pack(side=tk.LEFT)

        if sys.platform == "darwin":
            tk.Label(self,
                     text="\nmacOS: type 'ots' in Terminal, or use the Web UI button in the OctoSlave launcher.",
                     font=("Helvetica", 9), bg=_C["bg"], fg=_C["muted"],
                     wraplength=510).pack(anchor="w", pady=(8, 0))
        elif sys.platform == "win32":
            tk.Label(self,
                     text="\nWindows: search 'OctoSlave' in the Start Menu to open the Web UI or Terminal.",
                     font=("Helvetica", 9), bg=_C["bg"], fg=_C["muted"],
                     wraplength=510).pack(anchor="w", pady=(8, 0))


# ---------------------------------------------------------------------------
# Main wizard window
# ---------------------------------------------------------------------------

_STEPS = ["Welcome", "Backend", "Credentials", "Model", "Test", "Done"]


class SetupWizard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("OctoSlave — Setup Wizard")
        self.resizable(False, False)
        self.configure(bg=_C["bg"])
        self.geometry(f"{_W}x{_H}")
        self._center()

        # State vars
        self._step = 0
        self.backend_var   = tk.StringVar(value="einfra")
        self.key_var       = tk.StringVar()
        self.url_var       = tk.StringVar(value=_EINFRA_URL)
        self.ollama_var    = tk.StringVar(value=_OLLAMA_URL)
        self.model_var     = tk.StringVar(value=_EINFRA_MODELS[0])

        self._build()
        self._show(0)

    def _center(self):
        self.update_idletasks()
        x = (self.winfo_screenwidth()  - _W) // 2
        y = (self.winfo_screenheight() - _H) // 2
        self.geometry(f"{_W}x{_H}+{x}+{y}")

    def _build(self):
        # ── Sidebar
        self._sidebar = tk.Frame(self, bg=_C["panel"], width=_SIDE)
        self._sidebar.pack(side=tk.LEFT, fill=tk.Y)
        self._sidebar.pack_propagate(False)
        tk.Label(self._sidebar, text="OctoSlave",
                 font=("Helvetica", 14, "bold"),
                 bg=_C["panel"], fg=_C["orange"]).pack(pady=(26, 2))
        tk.Label(self._sidebar, text="Setup Wizard",
                 font=("Helvetica", 9), bg=_C["panel"],
                 fg=_C["muted"]).pack(pady=(0, 18))
        self._slbls = []
        for i, s in enumerate(_STEPS):
            lbl = tk.Label(self._sidebar, text=f"  {i+1}. {s}",
                           font=("Helvetica", 10), bg=_C["panel"],
                           fg=_C["muted"], anchor="w", width=16)
            lbl.pack(pady=3, padx=6, fill=tk.X)
            self._slbls.append(lbl)

        # ── Right side
        right = tk.Frame(self, bg=_C["bg"])
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._content = tk.Frame(right, bg=_C["bg"])
        self._content.pack(fill=tk.BOTH, expand=True, padx=26, pady=22)

        # Nav bar
        nav = tk.Frame(right, bg=_C["bg"], height=58)
        nav.pack(side=tk.BOTTOM, fill=tk.X)
        nav.pack_propagate(False)
        tk.Frame(nav, bg=_C["border"], height=1).pack(fill=tk.X)
        btns = tk.Frame(nav, bg=_C["bg"])
        btns.pack(side=tk.RIGHT, padx=22, pady=12)
        self._btn_back = tk.Button(btns, text="← Back", command=self._back,
                                    width=10, bg=_C["btn_ins"], fg=_C["fg"],
                                    relief="flat", font=("Helvetica", 10),
                                    cursor="hand2", activebackground=_C["panel"],
                                    activeforeground=_C["fg"])
        self._btn_back.pack(side=tk.LEFT, padx=(0, 8))
        self._btn_next = tk.Button(btns, text="Next →", command=self._next,
                                    width=10, bg=_C["btn_act"], fg=_C["btn_fg"],
                                    relief="flat", font=("Helvetica", 10, "bold"),
                                    cursor="hand2", activebackground="#e8894e",
                                    activeforeground=_C["btn_fg"])
        self._btn_next.pack(side=tk.LEFT)

    def _update_sidebar(self):
        for i, lbl in enumerate(self._slbls):
            if i < self._step:
                lbl.config(fg=_C["green"], font=("Helvetica", 10),
                           text=f"  ✓ {_STEPS[i]}")
            elif i == self._step:
                lbl.config(fg=_C["white"], font=("Helvetica", 10, "bold"),
                           text=f"  → {_STEPS[i]}")
            else:
                lbl.config(fg=_C["muted"], font=("Helvetica", 10),
                           text=f"  {i+1}. {_STEPS[i]}")

    def _show(self, step: int):
        self._step = step
        self._update_sidebar()
        for w in self._content.winfo_children():
            w.destroy()

        page: _Page
        backend = self.backend_var.get()
        if   step == 0: page = _PageWelcome(self._content)
        elif step == 1: page = _PageBackend(self._content, self.backend_var, self._on_backend_change)
        elif step == 2: page = _PageCredentials(self._content, backend, self.key_var, self.url_var, self.ollama_var)
        elif step == 3: page = _PageModel(self._content, backend, self.model_var)
        elif step == 4: page = _PageTest(self._content, backend, self.key_var, self.url_var, self.ollama_var, self.model_var)
        else:           page = _PageDone(self._content, backend)
        page.pack(fill=tk.BOTH, expand=True)

        self._btn_back.config(state=tk.DISABLED if step == 0 else tk.NORMAL)
        last = len(_STEPS) - 1
        self._btn_next.config(
            text="Finish" if step == last else "Next →",
            command=self.destroy if step == last else self._next,
        )

    def _on_backend_change(self):
        b = self.backend_var.get()
        if b == "einfra":
            self.url_var.set(_EINFRA_URL)
            self.model_var.set(_EINFRA_MODELS[0])
        elif b == "nim":
            self.url_var.set(_NIM_URL)
            self.model_var.set(_NIM_MODELS[0])
        else:
            self.ollama_var.set(_OLLAMA_URL)
            self.model_var.set("llama3.2")

    def _validate(self) -> bool:
        if self._step == 2:
            backend = self.backend_var.get()
            if backend in ("einfra", "nim") and not self.key_var.get().strip():
                messagebox.showerror("Missing API Key",
                                     "Please enter your API key to continue.",
                                     parent=self)
                return False
        return True

    def _back(self):
        if self._step > 0:
            self._show(self._step - 1)

    def _next(self):
        if not self._validate():
            return
        if self._step == 4:      # just before Done
            self._save()
        if self._step < len(_STEPS) - 1:
            self._show(self._step + 1)

    def _save(self):
        """Write config using octoslave.config.save_config if available,
        otherwise write the JSON directly to avoid import issues."""
        backend = self.backend_var.get()
        key     = self.key_var.get().strip()
        url     = self.url_var.get().strip()
        ollama  = self.ollama_var.get().strip()
        model   = self.model_var.get().strip()

        try:
            from octoslave.config import save_config  # type: ignore
            if backend == "einfra":
                save_config(api_key=key, base_url=url, default_model=model, backend="einfra")
            elif backend == "nim":
                save_config(api_key="", default_model=model, backend="nim",
                            nim_api_key=key, nim_url=url)
            else:
                save_config(api_key="", default_model=model, backend="ollama",
                            ollama_url=ollama)
        except Exception:
            # Fallback: write minimal JSON directly
            _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            existing: dict = {}
            if _CONFIG_FILE.exists():
                try:
                    existing = json.loads(_CONFIG_FILE.read_text())
                except Exception:
                    pass
            if backend == "einfra":
                existing.update({"api_key": key, "base_url": url,
                                 "default_model": model, "backend": "einfra"})
            elif backend == "nim":
                existing.update({"nim_api_key": key, "nim_url": url,
                                 "default_model": model, "backend": "nim"})
            else:
                existing.update({"ollama_url": ollama, "default_model": model,
                                 "backend": "ollama"})
            _CONFIG_FILE.write_text(json.dumps(existing, indent=2))


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def run_wizard() -> bool:
    """Launch the wizard.  Returns True if config was written."""
    app = SetupWizard()
    app.mainloop()
    return _CONFIG_FILE.exists()


def needs_wizard() -> bool:
    """True when the wizard should run (frozen binary, no config yet)."""
    return getattr(sys, "frozen", False) and not _CONFIG_FILE.exists()
