"""
Ra HUD (Tkinter)
====================
A proper command-center in a dark, cyan-lit panel: pulsing status core,
conversation transcript, a live "hearing/typing" line, a running system-log
pane, quick-action chips, mic + wake + computer-control toggles. Everything is
thread-safe over a single queue and polled on the main loop.
"""
import math
import queue
import tkinter as tk
from datetime import datetime

BG = "#04070c"
PANEL = "#0a0f16"
PANEL2 = "#0d131c"
ENTRY_BG = "#101a26"
ACCENT = "#4fd6ff"
ACCENT_DIM = "#16435c"
TEXT = "#dcebff"
MUTED = "#5b6b82"
DIM = "#22303f"
USER_COLOR = "#7ee787"
ERR = "#ff5f5f"
WARN = "#ffd479"
OK = "#62e08f"
TOOL = "#c3a6ff"

FONT_MONO = "Consolas"

STATUS_COLORS = {
    "idle": "#3a4657",
    "listening": "#2ea043",
    "thinking": "#f2c744",
    "working": "#c3a6ff",
    "tooling": "#c3a6ff",
    "speaking": ACCENT,
    "offline": "#3a4657",
    "starting": "#f2c744",
}

LOG_COLORS = {
    "info": MUTED,
    "ok": OK,
    "warn": WARN,
    "err": ERR,
    "tool": TOOL,
    "voice": "#7fd2ff",
}


class RaGUI:
    def __init__(self, on_submit, on_mic=None, on_wake_toggle=None,
                 on_computer_toggle=None, assistant_name="Ra"):
        self.on_submit = on_submit
        self.on_mic = on_mic or (lambda: None)
        self.on_wake_toggle = on_wake_toggle or (lambda a: None)
        self.on_computer_toggle = on_computer_toggle or (lambda a: None)
        self.assistant_name = assistant_name
        self._status_key = "starting"
        self._pulse = 0.0
        self._wake_on = True
        self._computer_on = True
        self._topmost_on = False
        self._partial_lines = 0

        self.root = tk.Tk()
        self.root.title(f"{assistant_name} Command Center")
        self.root.geometry("1020x700+60+40")
        self.root.configure(bg=BG)
        self.root.minsize(860, 560)

        self._build_layout()
        self._queue = queue.Queue()
        self.root.after(60, self._poll_queue)
        self.root.after(50, self._animate)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI
    def _build_layout(self):
        root = self.root

        # ---- Header -----------------------------------------------------
        header = tk.Frame(root, bg=BG)
        header.pack(fill="x", padx=20, pady=(18, 6))

        self.ring = tk.Canvas(header, width=56, height=56, bg=BG, highlightthickness=0)
        self.ring.pack(side="left")

        title_box = tk.Frame(header, bg=BG)
        title_box.pack(side="left", padx=(14, 0))
        tk.Label(title_box, text="Ra", fg=ACCENT, bg=BG,
                 font=("Segoe UI", 26, "bold")).pack(anchor="w")
        self.status_label = tk.Label(title_box, text="STARTING", fg=MUTED, bg=BG,
                                     font=("Segoe UI", 10, "bold"))
        self.status_label.pack(anchor="w")

        self.model_label = tk.Label(header, text="", fg=MUTED, bg=BG,
                                    font=("Consolas", 9))
        self.model_label.pack(side="right", anchor="ne", pady=(4, 0))

        # Header control toggles
        ctl = tk.Frame(header, bg=BG)
        ctl.pack(side="right", padx=(0, 16))
        self.wake_btn = self._toggle_button(ctl, "LIVE VOICE", self._wake_on,
                                            lambda: self._flip_wake())
        self.wake_btn.pack(anchor="e", pady=1)
        self.computer_btn = self._toggle_button(ctl, "COMPUTER CNTL", self._computer_on,
                                                lambda: self._flip_computer())
        self.computer_btn.pack(anchor="e", pady=1)
        self.top_btn = self._toggle_button(ctl, "ON TOP", self._topmost_on,
                                           lambda: self._flip_topmost())
        self.top_btn.pack(anchor="e", pady=1)

        tk.Frame(root, bg=ACCENT_DIM, height=1).pack(fill="x", padx=20, pady=(6, 8))

        # ---- Live line (partials / streaming) ---------------------------
        interim = tk.Frame(root, bg=PANEL2)
        interim.pack(fill="x", padx=20, pady=(0, 8))
        self.partial_label = tk.Label(interim, text="", fg=ACCENT, bg=PANEL2,
                                      font=(FONT_MONO, 10), anchor="w", justify="left")
        self.partial_label.pack(fill="x", padx=12, pady=6)

        # ---- Main split --------------------------------------------------
        main = tk.Frame(root, bg=BG)
        main.pack(fill="both", expand=True, padx=20, pady=(0, 8))

        # Chat
        chat_frame = tk.Frame(main, bg=PANEL)
        chat_frame.pack(side="left", fill="both", expand=True)
        self.chat = tk.Text(chat_frame, bg=PANEL, fg=TEXT, insertbackground=ACCENT,
                            font=(FONT_MONO, 10), wrap="word", borderwidth=0,
                            padx=14, pady=12)
        scroll = tk.Scrollbar(chat_frame, command=self.chat.yview, bg=BG,
                              troughcolor=PANEL, activebackground=ACCENT_DIM)
        self.chat.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.chat.pack(side="left", fill="both", expand=True)
        self.chat.configure(state="disabled")
        self._setup_chat_tags()

        # Sidebar
        side = tk.Frame(main, bg=BG, width=330)
        side.pack(side="right", fill="y", padx=(14, 0))
        side.pack_propagate(False)

        tk.Label(side, text="SYSTEM LOG", fg=MUTED, bg=BG,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(0, 4))
        logbox = tk.Frame(side, bg=PANEL2)
        logbox.pack(fill="both", expand=True)
        self.log = tk.Text(logbox, bg=PANEL2, fg=MUTED, font=(FONT_MONO, 9),
                           wrap="word", borderwidth=0, padx=10, pady=8, height=10,
                           state="disabled")
        lscroll = tk.Scrollbar(logbox, command=self.log.yview, bg=BG,
                               troughcolor=PANEL2, activebackground=ACCENT_DIM)
        self.log.configure(yscrollcommand=lscroll.set)
        lscroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        for lvl, col in LOG_COLORS.items():
            self.log.tag_config(f"log-{lvl}", foreground=col)

        # Quick actions
        tk.Label(side, text="QUICK ACTIONS", fg=MUTED, bg=BG,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(10, 4))
        chips = tk.Frame(side, bg=BG)
        chips.pack(fill="x")
        for label, cmd in (
            ("TIME", "what time is it"),
            ("BATTERY", "what's my battery level"),
            ("SCREENSHOT", "take a screenshot"),
            ("LOCK PC", "lock the pc"),
        ):
            tk.Button(chips, text=label, command=lambda c=cmd: self._chip(c),
                      bg=PANEL2, fg=TEXT, activebackground=ACCENT_DIM, activeforeground=ACCENT,
                      relief="flat", font=("Segoe UI", 9, "bold"), padx=8, pady=5, cursor="hand2"
                      ).pack(side="left", padx=(0, 6))

        # ---- Input row ---------------------------------------------------
        bottom = tk.Frame(root, bg=BG)
        bottom.pack(fill="x", padx=20, pady=(0, 16))

        self.entry = tk.Entry(bottom, bg=ENTRY_BG, fg=TEXT, insertbackground=ACCENT,
                              font=("Segoe UI", 12), relief="flat")
        self.entry.pack(side="left", fill="x", expand=True, ipady=9, padx=(0, 8))
        self.entry.bind("<Return>", self._submit)
        self.entry.focus_set()

        self.mic_btn = tk.Button(bottom, text="  MIC  ", command=lambda: self.on_mic(),
                                 bg="#1f6feb", fg="white", relief="flat", padx=14, pady=7,
                                 font=("Segoe UI", 10, "bold"), cursor="hand2",
                                 activebackground="#3b82f6")
        self.mic_btn.pack(side="right", padx=(0, 8))
        tk.Button(bottom, text="SEND", command=self._submit,
                  bg=ACCENT_DIM, fg=ACCENT, relief="flat", padx=18, pady=7,
                  font=("Segoe UI", 10, "bold"), cursor="hand2",
                  activebackground=ACCENT, activeforeground="#04131a").pack(side="right")

    def _toggle_button(self, parent, label, active, command):
        return tk.Button(
            parent, text=f"{label}: {'ON' if active else 'OFF'}",
            bg="#1c3a2e" if active else PANEL2,
            fg=OK if active else MUTED,
            activebackground=ACCENT_DIM, relief="flat",
            font=("Segoe UI", 8, "bold"), padx=8, pady=3, cursor="hand2",
            command=command,
        )

    def _setup_chat_tags(self):
        self.chat.tag_config("you-name", foreground=USER_COLOR, font=("Segoe UI", 9, "bold"))
        self.chat.tag_config("time", foreground=DIM, font=(FONT_MONO, 8))
        self.chat.tag_config("you-body", foreground=TEXT, font=(FONT_MONO, 10), lmargin1=6)
        self.chat.tag_config("ai-name", foreground=ACCENT, font=("Segoe UI", 9, "bold"))
        self.chat.tag_config("ai-body", foreground=TEXT, font=(FONT_MONO, 10), lmargin1=6)
        self.chat.tag_config("divider", foreground=DIM, font=(FONT_MONO, 8))

    def _chip(self, cmd: str):
        self.entry.insert("end", cmd)
        self._submit()

    # ----------------------------------------------------- public (thread-safe)
    def post_status(self, text: str, key: str):
        self._queue.put(("status", (text, key)))

    def post_message(self, who: str, text: str):
        self._queue.put(("message", (who, text)))

    def post_partial(self, text: str):
        self._queue.put(("partial", text))

    def post_log(self, level: str, message: str):
        self._queue.put(("log", (level, message)))

    def set_model(self, label: str):
        self._queue.put(("model", label))

    def shutdown(self, delay_ms=400):
        self.root.after(delay_ms, self.root.destroy)

    def run(self):
        self.root.mainloop()

    # ----------------------------------------------------- internal
    def _submit(self, event=None):
        text = self.entry.get().strip()
        if text:
            self.entry.delete(0, "end")
            self.on_submit(text)

    def _flip_wake(self):
        self._wake_on = not self._wake_on
        self.wake_btn.configure(
            text=f"LIVE VOICE: {'ON' if self._wake_on else 'OFF'}",
            bg="#1c3a2e" if self._wake_on else PANEL2,
            fg=OK if self._wake_on else MUTED,
        )
        self.on_wake_toggle(self._wake_on)

    def _flip_computer(self):
        self._computer_on = not self._computer_on
        self.computer_btn.configure(
            text=f"COMPUTER CNTL: {'ON' if self._computer_on else 'OFF'}",
            bg="#1c3a2e" if self._computer_on else PANEL2,
            fg=OK if self._computer_on else MUTED,
        )
        self.on_computer_toggle(self._computer_on)

    def _flip_topmost(self):
        self._topmost_on = not self._topmost_on
        self.root.attributes("-topmost", self._topmost_on)
        self.top_btn.configure(
            text=f"ON TOP: {'ON' if self._topmost_on else 'OFF'}",
            bg="#1c3a2e" if self._topmost_on else PANEL2,
            fg=OK if self._topmost_on else MUTED,
        )

    def _on_close(self):
        self._queue.put(("quit", None))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "quit":
                    self.root.destroy()
                    continue
                if kind == "status":
                    text, key = payload
                    self._status_key = key
                    self.status_label.configure(text=text.upper())
                elif kind == "message":
                    self._append_chat(*payload)
                elif kind == "partial":
                    self.partial_label.configure(text=payload)
                elif kind == "log":
                    self._append_log(*payload)
                elif kind == "model":
                    self.model_label.configure(text=payload)
        except queue.Empty:
            pass
        try:
            self.root.after(60, self._poll_queue)
        except Exception:
            pass

    def _append_chat(self, who: str, text: str):
        now = datetime.now().strftime("%H:%M")
        self.chat.configure(state="normal")
        is_ai = who == self.assistant_name
        tag_name = "ai-name" if is_ai else "you-name"
        tag_body = "ai-body" if is_ai else "you-body"
        self.chat.insert("end", f"[{now}]\n", "time")
        self.chat.insert("end", f"{who.upper()}\n", tag_name)
        self.chat.insert("end", f"{text}\n", tag_body)
        self.chat.insert("end", "─" * 40 + "\n", "divider")
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def _append_log(self, level: str, message: str):
        now = datetime.now().strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"{now} {level.upper():4} {message}\n", f"log-{level}")
        line_count = int(self.log.index("end-1c").split(".")[0])
        if line_count > 500:
            self.log.delete("1.0", f"{line_count - 400}.0")
        self.log.configure(state="disabled")
        try:
            self.log.see("end")
        except Exception:
            pass

    def _draw_ring(self, color, glow):
        self.ring.delete("all")
        cx, cy, r = 28, 28, 21
        if glow > 0:
            for i in range(1, 3):
                gr = r + i * glow * 5
                self.ring.create_oval(cx - gr, cy - gr, cx + gr, cy + gr,
                                      outline=color, width=1)
        self.ring.create_oval(cx - r, cy - r, cx + r, cy + r, outline=color, width=3)
        inner = r - 8
        self.ring.create_oval(cx - inner, cy - inner, cx + inner, cy + inner,
                              fill=color, outline="")

    def _animate(self):
        color = STATUS_COLORS.get(self._status_key, ACCENT)
        if self._status_key in ("listening", "thinking", "tooling", "speaking", "working"):
            self._pulse += 0.13
            glow = (math.sin(self._pulse) + 1) / 2
        else:
            glow = 0
        self._draw_ring(color, glow)
        try:
            self.root.after(60, self._animate)
        except Exception:
            pass