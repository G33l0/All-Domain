"""Tkinter front-end.

Everything that touches a widget happens on the Tk main thread.  The engine
runs in :class:`~domainatlas.runner.CollectorThread` and communicates only
through a queue that is drained by an ``after()`` timer.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Dict, Optional

from .config import Config, ConfigError, DEFAULT_CONFIG_PATH
from .engine import Event
from .runner import CollectorThread
from .sources import available_sources

try:  # pragma: no cover - import guarded for headless environments
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    TK_IMPORT_ERROR: Optional[BaseException] = None
except Exception as exc:  # pragma: no cover
    tk = None  # type: ignore[assignment]
    TK_IMPORT_ERROR = exc

MAX_LOG_LINES = 2000
UI_POLL_MS = 200
TABLE_REFRESH_MS = 1000

LEVEL_COLORS = {
    "INFO": "#d4d4d4",
    "GOOD": "#4ec9b0",
    "WARN": "#dcdcaa",
    "ERROR": "#f48771",
}


def tkinter_available() -> bool:
    return tk is not None


class DomainAtlasApp:
    """The main window."""

    def __init__(self, root, config: Config, config_path: str = DEFAULT_CONFIG_PATH) -> None:
        self.root = root
        self.config = config
        self.config_path = config_path
        self.collector = CollectorThread(config)
        self._tech_dirty = True
        self._last_table_refresh = 0.0
        self._log_lines = 0
        self._state = "stopped"

        root.title("Domain Atlas 2.0")
        root.geometry("1050x720")
        root.minsize(820, 560)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_toolbar()
        self._build_stats()
        self._build_main()
        self._build_footer()
        self._set_state("stopped")

        self.root.after(UI_POLL_MS, self._pump)

    # ------------------------------------------------------------------- UI
    def _build_toolbar(self) -> None:
        bar = tk.Frame(self.root)
        bar.pack(fill=tk.X, padx=10, pady=(8, 4))

        self.btn_start = tk.Button(bar, text="Start", width=9, command=self.on_start, bg="#c8e6c9")
        self.btn_pause = tk.Button(bar, text="Pause", width=9, command=self.on_pause, bg="#fff9c4")
        self.btn_resume = tk.Button(bar, text="Resume", width=9, command=self.on_resume, bg="#bbdefb")
        self.btn_stop = tk.Button(bar, text="Stop", width=9, command=self.on_stop, bg="#ffcdd2")
        self.btn_config = tk.Button(bar, text="Settings", width=9, command=self.open_settings)
        self.btn_export = tk.Button(bar, text="Export", width=9, command=self.export_log)
        for button in (self.btn_start, self.btn_pause, self.btn_resume, self.btn_stop,
                       self.btn_config, self.btn_export):
            button.pack(side=tk.LEFT, padx=4)

        self.status_label = tk.Label(bar, text="Status: Stopped", font=("Arial", 10, "bold"), fg="gray")
        self.status_label.pack(side=tk.RIGHT, padx=8)

    def _build_stats(self) -> None:
        frame = tk.Frame(self.root)
        frame.pack(fill=tk.X, padx=10, pady=4)
        self.stat_labels: Dict[str, tk.Label] = {}
        for key in ("Processed", "Responsive", "Unreachable", "New", "Re-checked", "Queue", "Rate"):
            label = tk.Label(frame, text=f"{key}: 0", font=("Arial", 10))
            label.pack(side=tk.LEFT, padx=8)
            self.stat_labels[key] = label

    def _build_main(self) -> None:
        main = tk.PanedWindow(self.root, orient=tk.HORIZONTAL, sashrelief=tk.RAISED)
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)

        tech_frame = tk.LabelFrame(main, text="Technologies", font=("Arial", 10, "bold"))
        self.tech_tree = ttk.Treeview(tech_frame, columns=("count",), show="tree headings", height=20)
        self.tech_tree.heading("#0", text="Technology")
        self.tech_tree.heading("count", text="Domains")
        self.tech_tree.column("#0", width=240, stretch=True)
        self.tech_tree.column("count", width=80, anchor="center", stretch=False)
        tech_scroll = ttk.Scrollbar(tech_frame, orient=tk.VERTICAL, command=self.tech_tree.yview)
        self.tech_tree.configure(yscrollcommand=tech_scroll.set)
        self.tech_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tech_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        main.add(tech_frame, minsize=260, stretch="always")

        log_frame = tk.LabelFrame(main, text="Activity Log", font=("Arial", 10, "bold"))
        self.log_text = scrolledtext.ScrolledText(
            log_frame, wrap=tk.WORD, font=("Consolas", 9), state=tk.DISABLED,
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4",
        )
        for level, color in LEVEL_COLORS.items():
            self.log_text.tag_configure(level, foreground=color)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        main.add(log_frame, minsize=320, stretch="always")

    def _build_footer(self) -> None:
        text = (
            f"Database: {self.config.db_path}   |   Technology files: {self.config.output_dir}/*.txt"
            f"   |   Sources: {', '.join(self.config.sources)}"
        )
        self.footer = tk.Label(self.root, text=text, font=("Arial", 8), fg="gray", anchor="w")
        self.footer.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=3)

    # ---------------------------------------------------------------- actions
    def on_start(self) -> None:
        if self.collector.running:
            return
        self.collector.update_config(self.config)
        self.collector.start()
        if self.collector.error is not None:
            messagebox.showerror("Cannot start", str(self.collector.error))
            self._set_state("stopped")
            return
        self._set_state("running")

    def on_pause(self) -> None:
        if self.collector.running and self._state == "running":
            self.collector.pause()
            self._set_state("paused")

    def on_resume(self) -> None:
        if self.collector.running and self._state == "paused":
            self.collector.resume()
            self._set_state("running")

    def on_stop(self) -> None:
        if not self.collector.running:
            return
        self._set_state("stopping")
        self.log("Stopping - waiting for workers to finish...", "WARN")
        self.root.config(cursor="watch")
        self.root.update_idletasks()
        stopped = self.collector.stop(timeout=25)
        self.root.config(cursor="")
        if not stopped:
            self.log("Collector did not stop in time; it will exit in the background.", "ERROR")
        self._set_state("stopped")

    def on_close(self) -> None:
        if self.collector.running:
            if not messagebox.askokcancel("Quit", "Collection is running. Stop it and quit?"):
                return
            self.collector.stop(timeout=25)
        self.root.destroy()

    def export_log(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Export activity log",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialfile=f"domain-atlas-{datetime.now():%Y%m%d-%H%M%S}.log",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.log_text.get("1.0", tk.END))
        except OSError as exc:
            messagebox.showerror("Export failed", str(exc))
            return
        messagebox.showinfo("Export complete", f"Log written to:\n{path}")

    # --------------------------------------------------------------- settings
    def open_settings(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Settings")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.grab_set()

        entries: Dict[str, tk.Entry] = {}
        numeric_fields = [
            ("concurrency", "Concurrency (workers)"),
            ("http_timeout", "HTTP timeout (seconds)"),
            ("fetch_interval", "Fetch interval (seconds)"),
            ("max_queue_size", "Max queue size"),
            ("max_domains_per_cycle", "Domains per cycle"),
            ("recheck_after", "Re-check after (seconds, 0 = off)"),
            ("recheck_batch", "Re-checks per cycle"),
        ]
        row = 0
        for field_name, label in numeric_fields:
            tk.Label(dialog, text=label, anchor="w").grid(row=row, column=0, sticky="w", padx=10, pady=4)
            entry = tk.Entry(dialog, width=18)
            entry.insert(0, str(getattr(self.config, field_name)))
            entry.grid(row=row, column=1, padx=10, pady=4)
            entries[field_name] = entry
            row += 1

        tk.Label(dialog, text="Certstream URL", anchor="w").grid(row=row, column=0, sticky="w", padx=10, pady=4)
        certstream_entry = tk.Entry(dialog, width=18)
        certstream_entry.insert(0, str(self.config.certstream_url))
        certstream_entry.grid(row=row, column=1, padx=10, pady=4)
        row += 1

        tk.Label(dialog, text="Sources", anchor="w").grid(row=row, column=0, sticky="nw", padx=10, pady=4)
        source_frame = tk.Frame(dialog)
        source_frame.grid(row=row, column=1, sticky="w", padx=10, pady=4)
        source_vars: Dict[str, tk.BooleanVar] = {}
        for name in available_sources():
            if name == "file":
                continue
            var = tk.BooleanVar(value=name in self.config.sources)
            tk.Checkbutton(source_frame, text=name, variable=var, anchor="w").pack(anchor="w")
            source_vars[name] = var
        row += 1

        verify_var = tk.BooleanVar(value=self.config.verify_ssl)
        tk.Checkbutton(dialog, text="Verify TLS certificates", variable=verify_var).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=10)
        row += 1
        files_var = tk.BooleanVar(value=self.config.write_tech_files)
        tk.Checkbutton(dialog, text="Write output/<technology>.txt files", variable=files_var).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=10)
        row += 1
        recheck_only_var = tk.BooleanVar(value=self.config.recheck_only)
        tk.Checkbutton(dialog, text="Re-check stored domains only (no new discovery)",
                       variable=recheck_only_var).grid(row=row, column=0, columnspan=2, sticky="w", padx=10)
        row += 1
        unresponsive_var = tk.BooleanVar(value=self.config.store_unresponsive)
        tk.Checkbutton(dialog, text="Store unresponsive domains in the database", variable=unresponsive_var).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=10)
        row += 1

        note = tk.Label(
            dialog,
            text="Timeout, interval and re-check settings apply immediately.\n"
                 "Concurrency and sources apply on the next Start.",
            font=("Arial", 8), fg="gray", justify="left",
        )
        note.grid(row=row, column=0, columnspan=2, sticky="w", padx=10, pady=(6, 0))
        row += 1

        def save() -> None:
            candidate = Config(**self.config.to_dict())
            try:
                for field_name, entry in entries.items():
                    raw = entry.get().strip()
                    value = float(raw) if field_name == "http_timeout" else int(raw)
                    setattr(candidate, field_name, value)
                candidate.certstream_url = certstream_entry.get().strip()
                candidate.verify_ssl = verify_var.get()
                candidate.write_tech_files = files_var.get()
                candidate.store_unresponsive = unresponsive_var.get()
                candidate.recheck_only = recheck_only_var.get()
                chosen = [name for name, var in source_vars.items() if var.get()]
                # Sources with no checkbox (e.g. a seed "file" source) are kept.
                preserved = [name for name in self.config.sources if name not in source_vars]
                combined = preserved + chosen
                if not combined:
                    raise ConfigError("select at least one source")
                candidate.sources = combined
                candidate.validate()
            except (ValueError, ConfigError) as exc:
                messagebox.showerror("Invalid settings", str(exc), parent=dialog)
                return

            self.config = candidate
            was_running = self.collector.running
            # Live-apply what can be changed without a restart.
            self.collector.update_config(candidate)
            try:
                candidate.save(self.config_path)
                saved_to = self.config_path
            except ConfigError as exc:
                messagebox.showwarning("Settings not saved", str(exc), parent=dialog)
                saved_to = None
            self.footer.config(
                text=f"Database: {candidate.db_path}   |   Technology files: {candidate.output_dir}/*.txt"
                     f"   |   Sources: {', '.join(candidate.sources)}"
            )
            self.log(
                "Settings updated" + (f" and saved to {saved_to}" if saved_to else "")
                + ("; restart collection to change the worker count." if was_running else "."),
                "INFO",
            )
            dialog.destroy()

        button_row = tk.Frame(dialog)
        button_row.grid(row=row, column=0, columnspan=2, pady=10)
        tk.Button(button_row, text="Save", width=10, command=save).pack(side=tk.LEFT, padx=6)
        tk.Button(button_row, text="Cancel", width=10, command=dialog.destroy).pack(side=tk.LEFT, padx=6)
        dialog.bind("<Return>", lambda _event: save())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())

    # ------------------------------------------------------------------- pump
    def _pump(self) -> None:
        """Drain engine events and refresh the widgets (Tk thread only)."""
        try:
            events = self.collector.drain_events()
            for event in events:
                self._handle_event(event)
            now = time.monotonic()
            if events or now - self._last_table_refresh > TABLE_REFRESH_MS / 1000:
                self._refresh_stats()
                if self._tech_dirty and now - self._last_table_refresh > TABLE_REFRESH_MS / 1000:
                    self._refresh_tech_table()
                    self._last_table_refresh = now
            if self._state in ("running", "paused", "stopping") and not self.collector.running:
                self._set_state("stopped")
        except Exception as exc:  # pragma: no cover - never kill the UI loop
            try:
                self.log(f"UI error: {type(exc).__name__}: {exc}", "ERROR")
            except Exception:
                pass
        finally:
            self.root.after(UI_POLL_MS, self._pump)

    def _handle_event(self, event: Event) -> None:
        if event.kind in ("log", "cycle", "domain"):
            self.log(event.message, event.level)
            if event.kind == "domain" and event.data.get("technologies"):
                self._tech_dirty = True
        elif event.kind == "state":
            state = event.data.get("state")
            if state in ("running", "paused", "stopped"):
                self._set_state(state)
            if event.message:
                self.log(event.message, event.level)

    def _refresh_stats(self) -> None:
        stats = self.collector.stats
        if stats is None:
            return
        self.stat_labels["Processed"].config(text=f"Processed: {stats.processed}")
        self.stat_labels["Responsive"].config(text=f"Responsive: {stats.responsive}")
        self.stat_labels["New"].config(text=f"New: {stats.new}")
        self.stat_labels["Unreachable"].config(text=f"Unreachable: {stats.unreachable}")
        self.stat_labels["Re-checked"].config(text=f"Re-checked: {stats.rechecked}")
        self.stat_labels["Queue"].config(text=f"Queue: {stats.queued}")
        self.stat_labels["Rate"].config(text=f"Rate: {stats.rate:.1f}/s")

    def _refresh_tech_table(self) -> None:
        stats = self.collector.stats
        if stats is None:
            return
        top = stats.tech_counts.most_common(30)
        self.tech_tree.delete(*self.tech_tree.get_children())
        for technology, count in top:
            self.tech_tree.insert("", tk.END, text=technology, values=(count,))
        self._tech_dirty = False

    def _set_state(self, state: str) -> None:
        self._state = state
        text, color = {
            "running": ("Running", "green"),
            "paused": ("Paused", "orange"),
            "stopping": ("Stopping", "orange"),
            "stopped": ("Stopped", "gray"),
        }.get(state, ("Stopped", "gray"))
        self.status_label.config(text=f"Status: {text}", fg=color)
        running = state in ("running", "paused")
        self.btn_start.config(state=tk.DISABLED if running or state == "stopping" else tk.NORMAL)
        self.btn_pause.config(state=tk.NORMAL if state == "running" else tk.DISABLED)
        self.btn_resume.config(state=tk.NORMAL if state == "paused" else tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL if running else tk.DISABLED)

    # -------------------------------------------------------------------- log
    def log(self, message: str, level: str = "INFO") -> None:
        if not message:
            return
        tag = level if level in LEVEL_COLORS else "INFO"
        line = f"{datetime.now():%H:%M:%S} - {message}\n"
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, line, tag)
        self._log_lines += 1
        if self._log_lines > MAX_LOG_LINES:
            trim = self._log_lines - MAX_LOG_LINES
            self.log_text.delete("1.0", f"{trim + 1}.0")
            self._log_lines = MAX_LOG_LINES
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)


def run_gui(config: Config, config_path: str = DEFAULT_CONFIG_PATH) -> int:
    """Entry point used by the CLI.  Returns a process exit code."""
    if tk is None:
        raise RuntimeError(
            "tkinter is not available in this Python installation.\n"
            "Install it (Debian/Ubuntu: 'sudo apt install python3-tk', Fedora: "
            "'sudo dnf install python3-tkinter') or run the collector headless "
            "with:  python domain_atlas.py --headless"
        ) from TK_IMPORT_ERROR
    root = tk.Tk()
    DomainAtlasApp(root, config, config_path)
    root.mainloop()
    return 0
