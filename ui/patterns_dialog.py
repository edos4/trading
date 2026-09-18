"""
ui/patterns_dialog.py — Patterns editor dialog for the tkinter UI.

Thin Tk front end over the shared `core.pattern_editor_api.PatternEditor`. It
owns no business rules: catalog/version reads, request building, submission,
validation, backtesting and lifecycle operations all go through the shared
facade, so the desktop and web tabs enforce identical invariants.

All service calls run on background threads; results are marshalled back to the
Tk thread with `after`. Closing the window only stops polling — it never marks a
durable job cancelled or completed.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Optional

from core.backtest_params import REPLAY_PARAMS
from core.market import default_market, parse_extra_symbols
from utils.logger import log

STREAM_SKIP = ("mode", "timeframe")
TERMINAL_STATES = ("completed", "failed", "cancelled", "blocked", "interrupted")


def _decimals_for_increment(inc: float) -> int:
    text = f"{inc:.10f}".rstrip("0")
    return len(text.split(".")[1]) if "." in text else 0


class PatternsDialog:
    """Pattern/version selection, AI edit submission, and result inspection."""

    def __init__(self, parent: tk.Misc, *, editor=None,
                 start_job_id: Optional[str] = None,
                 on_job: Optional[Callable[[str], None]] = None):
        self._closed = False
        self._editor = editor
        self._on_job = on_job
        self._pattern_id: Optional[str] = None
        self._version_id: Optional[str] = None
        self._job_id: Optional[str] = None
        self._runs: dict = {}
        self._polling = False
        self._patterns: dict[str, dict] = {}
        self._versions: list[dict] = []
        self._presets: dict[str, str] = {}
        self._vars: dict[str, tk.Variable] = {}
        # Worker threads enqueue UI updates; the main thread drains them. Calling
        # Tk (`after`) directly from a worker thread is not thread-safe.
        self._ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()

        self._top = tk.Toplevel(parent)
        self._top.title("Pattern Editor")
        self._top.geometry("1180x860")
        self._top.minsize(900, 700)
        self._top.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_selector()
        self._build_detail()
        self._build_edit_form()
        self._build_results()
        self._top.after(50, self._drain_queue)
        self._load_presets()
        self._refresh_catalog()

        if start_job_id:
            self._adopt_job(start_job_id)

    # ── async plumbing ───────────────────────────────────────────────────
    def _post(self, fn: Callable[[], None]) -> None:
        if self._closed:
            return
        self._ui_queue.put(fn)

    def _drain_queue(self) -> None:
        if self._closed:
            return
        while True:
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except tk.TclError:
                self._closed = True
                return
            except Exception as exc:  # noqa: BLE001 - never crash the UI loop
                log.warning(f"PatternsDialog | UI update failed: {exc}")
        self._top.after(50, self._drain_queue)

    def _run_async(self, work: Callable[[], Any], *, on_done=None, on_error=None) -> None:
        def worker() -> None:
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - shown in the status bar
                # Bind the exception now: it is cleared when this frame exits.
                self._post(lambda error=exc: (on_error or self._show_error)(error))
            else:
                if on_done is not None:
                    self._post(lambda: on_done(result))

        threading.Thread(target=worker, daemon=True, name="patterns-dialog").start()

    def editor(self):
        if self._editor is None:
            from core.pattern_editor_api import PatternEditor

            self._editor = PatternEditor()
        return self._editor

    def _show_error(self, exc: Exception) -> None:
        self._status_var.set(f"Error: {exc}")

    # ── selector ─────────────────────────────────────────────────────────
    def _build_selector(self) -> None:
        frame = ttk.LabelFrame(self._top, text="Pattern / version", padding=8)
        frame.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(8, 4))

        ttk.Label(frame, text="Pattern").grid(row=0, column=0, sticky=tk.W)
        self._pattern_combo = ttk.Combobox(frame, width=38, state="readonly")
        self._pattern_combo.grid(row=0, column=1, sticky=tk.W, padx=(4, 12))
        self._pattern_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_pattern_change())

        self._include_archived = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Include archived", variable=self._include_archived,
                        command=self._load_versions).grid(row=0, column=2, sticky=tk.W)

        cols = ("number", "version", "actor", "created", "validation", "default", "archived")
        self._tree = ttk.Treeview(frame, columns=cols, show="headings", height=7)
        for col, width in zip(cols, (45, 90, 80, 150, 80, 65, 70)):
            self._tree.heading(col, text=col.capitalize())
            self._tree.column(col, width=width, anchor=tk.W)
        self._tree.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self._tree.tag_configure("default", background="#e6f4ea")
        self._tree.tag_configure("archived", foreground="#888888")
        self._tree.bind("<<TreeviewSelect>>", lambda _e: self._on_version_select())

    def _refresh_catalog(self) -> None:
        self._run_async(self.editor().catalog, on_done=self._populate_catalog)

    def _populate_catalog(self, catalog: list[dict]) -> None:
        self._patterns = {row["pattern_id"]: row for row in catalog}
        values = [f"{row['display_name']} ({row['pattern_id']})" for row in catalog]
        self._pattern_combo["values"] = values
        self._pattern_values = {f"{row['display_name']} ({row['pattern_id']})": row["pattern_id"]
                                for row in catalog}
        if catalog and not self._pattern_combo.get():
            self._pattern_combo.current(0)
            self._on_pattern_change()

    def _selected_pattern(self) -> Optional[str]:
        label = self._pattern_combo.get()
        return getattr(self, "_pattern_values", {}).get(label)

    def _on_pattern_change(self) -> None:
        self._pattern_id = self._selected_pattern()
        self._version_id = None
        self._job_id = None
        self._runs = {}
        self._load_versions()

    def _load_versions(self) -> None:
        pattern_id = self._selected_pattern()
        if not pattern_id:
            return
        include = bool(self._include_archived.get())
        self._run_async(
            lambda: self.editor().versions_for(pattern_id, include_archived=include),
            on_done=self._populate_versions)

    def _populate_versions(self, versions: list[dict]) -> None:
        self._versions = versions
        self._tree.delete(*self._tree.get_children())
        pattern = self._patterns.get(self._pattern_id or "", {})
        default_id = pattern.get("default_version_id")
        for index, version in enumerate(versions):
            tags = []
            if version["version_id"] == default_id:
                tags.append("default")
            if version["archived"]:
                tags.append("archived")
            self._tree.insert("", tk.END, iid=str(index), tags=tuple(tags), values=(
                version["number"], version["version_id"][:8], version.get("actor") or "",
                (version.get("created_at") or "")[:19], version["validation"],
                "default" if version["version_id"] == default_id else "",
                "archived" if version["archived"] else ""))
        if versions:
            self._tree.selection_set("0")
            self._on_version_select()

    def _selected_version(self) -> Optional[dict]:
        selection = self._tree.selection()
        if not selection:
            return None
        index = int(selection[0])
        if index < len(self._versions):
            return self._versions[index]
        return None

    def _on_version_select(self) -> None:
        version = self._selected_version()
        if version is None:
            return
        self._version_id = version["version_id"]
        if version.get("edit_job_id"):
            self._adopt_job(version["edit_job_id"], resume=False)
        self._load_detail(self._version_id)
        self._update_actions(version)

    def _update_actions(self, version: dict) -> None:
        pattern = self._patterns.get(self._pattern_id or "", {})
        is_default = version["version_id"] == pattern.get("default_version_id")
        state = tk.DISABLED if version["archived"] or is_default else tk.NORMAL
        self._default_btn.config(state=state)
        self._again_btn.config(state=tk.NORMAL if self._job_id else tk.DISABLED)
        self._delete_btn.config(state=tk.DISABLED if version["archived"] else tk.NORMAL)
        others = [v for v in self._versions
                  if v["version_id"] != version["version_id"] and not v["archived"]
                  and v["validation"] == "passed"]
        self._replacement["values"] = [v["version_id"][:8] for v in others]
        self._replacement_map = {v["version_id"][:8]: v["version_id"] for v in others}
        if is_default and others:
            self._replacement_label.grid()
            self._replacement.grid()
        else:
            self._replacement_label.grid_remove()
            self._replacement.grid_remove()

    def _load_detail(self, version_id: str) -> None:
        def work():
            editor = self.editor()
            return editor.version_detail(version_id), editor.source(version_id), editor.diff(version_id)

        self._run_async(work, on_done=lambda pair: self._render_detail(*pair))

    def _render_detail(self, detail, source, diff) -> None:
        self._detail_text.config(state=tk.NORMAL)
        self._detail_text.delete("1.0", tk.END)
        lines = [
            f"Version #{detail['number']}  {detail['version_id']}",
            f"Pattern:    {detail['pattern_id']}",
            f"Actor:      {detail.get('actor')}  ({detail.get('provenance')})",
            f"Created:    {detail.get('created_at')}",
            f"Parent:     {detail.get('parent_version_id') or 'none'}",
            f"Validation: {detail['lifecycle']['validation']}",
            f"Default run:{detail['lifecycle']['successful_run_id'] or 'none'}",
            f"Archived:   {detail['lifecycle']['archived_at'] or 'no'}",
        ]
        if detail.get("instruction"):
            lines.append(f"Instruction: {detail['instruction']}")
        if detail.get("explanation"):
            lines.append(f"Explanation: {detail['explanation']}")
        for report in detail.get("reports", []):
            lines.append(f"  report [{report.get('status')}] {str(report.get('report_id'))[:8]}")
        for run in detail.get("backtests", []):
            lines.append(f"  run {str(run.get('run_id'))[:8]} {run.get('state')}")
        self._detail_text.insert(tk.END, "\n".join(lines) + "\n")
        self._detail_text.config(state=tk.DISABLED)

        files = source.get("files", {})
        self._set_text(self._source_text, files.get(source.get("source_path"), ""))
        self._set_text(self._doc_text, files.get(source.get("documentation_path"), ""))
        diff_files = diff.get("files", {})
        self._set_text(self._diff_text, "\n".join(diff_files.get(name, "") for name in diff_files))

    def _set_text(self, widget: tk.Text, value: str) -> None:
        widget.config(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert(tk.END, value or "—")
        widget.config(state=tk.DISABLED)

    # ── edit form ────────────────────────────────────────────────────────
    def _build_edit_form(self) -> None:
        frame = ttk.LabelFrame(self._top, text="AI edit", padding=8)
        frame.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(4, 4))
        for column in range(6):
            frame.columnconfigure(column, weight=1 if column % 2 else 0, pad=6)

        row = 0
        ttk.Label(frame, text="Saved preset").grid(row=row, column=0, sticky=tk.W)
        self._preset_combo = ttk.Combobox(frame, width=32, state="readonly")
        self._preset_combo.grid(row=row, column=1, columnspan=2, sticky=tk.W)
        self._preset_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_preset_change())
        ttk.Label(frame, text="Preset name (new)").grid(row=row, column=3, sticky=tk.W)
        self._preset_name = ttk.Entry(frame, width=26)
        self._preset_name.grid(row=row, column=4, columnspan=2, sticky=tk.W)

        row += 1
        ttk.Label(frame, text="Market").grid(row=row, column=0, sticky=tk.W)
        self._vars["market"] = tk.StringVar(value=default_market().id)
        ttk.Combobox(frame, textvariable=self._vars["market"], values=["us", "ph"],
                     state="readonly", width=8).grid(row=row, column=1, sticky=tk.W)
        ttk.Label(frame, text="Symbols").grid(row=row, column=3, sticky=tk.W)
        self._vars["symbols"] = tk.StringVar()
        ttk.Entry(frame, textvariable=self._vars["symbols"], width=26).grid(
            row=row, column=4, columnspan=2, sticky=tk.W)

        row += 1
        ttk.Label(frame, text="Universe").grid(row=row, column=0, sticky=tk.W)
        self._vars["universe"] = tk.StringVar()
        ttk.Entry(frame, textvariable=self._vars["universe"], width=18).grid(
            row=row, column=1, sticky=tk.W)

        row += 1
        for index, entry in enumerate(
                [e for e in REPLAY_PARAMS if e[0] not in STREAM_SKIP]):
            if index and index % 3 == 0:
                row += 1
            key, label, _desc, ptype, default, choices = entry
            column = (index % 3) * 2
            ttk.Label(frame, text=label).grid(row=row, column=column, sticky=tk.W)
            self._make_widget(frame, key, ptype, default, choices, column + 1, row)

        row += 1
        ttk.Label(frame, text="Instruction").grid(row=row, column=0, sticky=tk.NW)
        self._instruction = tk.Text(frame, height=4, width=70, wrap=tk.WORD)
        self._instruction.grid(row=row, column=1, columnspan=5, sticky="ew")

        row += 1
        controls = ttk.Frame(frame)
        controls.grid(row=row, column=0, columnspan=6, sticky=tk.W, pady=(6, 0))
        self._submit_btn = ttk.Button(controls, text="Submit", command=self._submit)
        self._submit_btn.pack(side=tk.LEFT)
        self._cancel_btn = ttk.Button(controls, text="Cancel edit", command=self._cancel,
                                      state=tk.DISABLED)
        self._cancel_btn.pack(side=tk.LEFT, padx=(6, 0))
        self._progress = ttk.Progressbar(controls, mode="determinate", length=220)
        self._progress.pack(side=tk.LEFT, padx=(8, 4))
        self._status_var = tk.StringVar(value="Idle.")
        ttk.Label(controls, textvariable=self._status_var).pack(side=tk.LEFT, padx=(6, 0))

    def _make_widget(self, parent, key, ptype, default, choices, column, row) -> None:
        if ptype == "spin":
            default_val, minv, maxv, inc = default
            var = tk.DoubleVar(value=default_val)
            ttk.Spinbox(parent, from_=minv, to=maxv, increment=inc, textvariable=var,
                        width=12, format=f"%.{_decimals_for_increment(inc)}f").grid(
                row=row, column=column, sticky=tk.W, padx=(0, 8))
        elif ptype == "check":
            var = tk.BooleanVar(value=bool(default))
            ttk.Checkbutton(parent, variable=var).grid(row=row, column=column, sticky=tk.W)
        elif ptype == "combo":
            var = tk.StringVar(value=default)
            ttk.Combobox(parent, textvariable=var, values=choices or [], state="readonly",
                         width=14).grid(row=row, column=column, sticky=tk.W, padx=(0, 8))
        else:
            var = tk.StringVar(value=str(default or ""))
            ttk.Entry(parent, textvariable=var, width=14).grid(
                row=row, column=column, sticky=tk.W, padx=(0, 8))
        self._vars[key] = var

    def _load_presets(self) -> None:
        self._run_async(self.editor().presets, on_done=self._populate_presets)

    def _populate_presets(self, presets: list[dict]) -> None:
        self._presets = {p["name"]: p["preset_id"] for p in presets}
        self._preset_combo["values"] = [""] + sorted(self._presets)

    def _on_preset_change(self) -> None:
        preset_id = self._presets.get(self._preset_combo.get())
        if not preset_id:
            return

        def work():
            return next((p for p in self.editor().presets()
                         if p["preset_id"] == preset_id), None)

        def apply(preset):
            if not preset:
                return
            settings = preset["settings"]
            self._vars["market"].set(settings["market"])
            self._preset_name.delete(0, tk.END)
            self._preset_name.insert(0, preset["name"])

        self._run_async(work, on_done=apply)

    # ── collect / submit ─────────────────────────────────────────────────
    def _collect_values(self) -> dict:
        values: dict = {"mode": "historical-stream", "timeframe": "1d"}
        for key, _label, _desc, ptype, default, _choices in REPLAY_PARAMS:
            if key in STREAM_SKIP:
                continue
            var = self._vars.get(key)
            if var is None:
                continue
            if ptype == "check":
                values[key] = bool(var.get())
            elif ptype == "spin":
                try:
                    values[key] = float(var.get())
                except (TypeError, ValueError, tk.TclError):
                    values[key] = float(default[0])
            else:
                text = str(var.get()).strip()
                values[key] = text or None
        values["market"] = self._vars["market"].get() or default_market().id
        values["symbols"] = parse_extra_symbols(self._vars["symbols"].get())
        values["universe"] = (self._vars["universe"].get() or "").strip() or None
        return values

    def _submit(self) -> None:
        version = self._selected_version()
        if version is None:
            self._status_var.set("Select a base version first.")
            return
        instruction = self._instruction.get("1.0", tk.END).strip()
        if not instruction:
            self._status_var.set("Enter an instruction.")
            return
        preset_id = self._presets.get(self._preset_combo.get())
        preset_name = self._preset_name.get().strip()
        if not preset_id and not preset_name:
            self._status_var.set("Select a saved preset or name a new one.")
            return
        pattern_id = self._pattern_id
        version_id = version["version_id"]
        values = self._collect_values()
        self._submit_btn.config(state=tk.DISABLED)
        self._status_var.set("Submitting…")

        def work():
            from core.pattern_editor_api import edit_request_from_values

            editor = self.editor()
            request = edit_request_from_values(
                editor, values, pattern_id=pattern_id, base_version_id=version_id,
                instruction=instruction, preset_id=preset_id,
                preset_name=preset_name or None)
            return editor.submit_edit(request)

        self._run_async(work, on_done=self._on_submitted, on_error=self._on_submit_error)

    def _on_submit_error(self, exc: Exception) -> None:
        self._submit_btn.config(state=tk.NORMAL)
        self._show_error(exc)

    def _on_submitted(self, job: dict) -> None:
        self._adopt_job(job["id"], resume=True)

    def _adopt_job(self, job_id: str, *, resume: bool = True) -> None:
        self._job_id = job_id
        self._again_btn.config(state=tk.NORMAL)
        self._cancel_btn.config(state=tk.NORMAL)
        self._submit_btn.config(state=tk.DISABLED)
        self._status_var.set(f"Job {job_id[:8]}: queued")
        if self._on_job is not None:
            self._on_job(job_id)
        if resume and not self._polling:
            self._polling = True
            self._top.after(500, self._poll)

    def _poll(self) -> None:
        if self._closed or not self._job_id:
            self._polling = False
            return

        def work():
            return self.editor().job_detail(self._job_id)

        self._run_async(work, on_done=self._render_job, on_error=self._on_poll_error)

    def _on_poll_error(self, exc: Exception) -> None:
        self._polling = False
        self._finish_job()
        self._show_error(exc)

    def _render_job(self, detail: dict) -> None:
        if self._closed:
            return
        progress = detail.get("progress") or {}
        done, total = progress.get("completed_units", 0), progress.get("total_units") or 0
        self._progress["value"] = (done / total * 100) if total else 0
        self._status_var.set(
            f"Job {(self._job_id or '')[:8]}: {detail['state']} "
            f"({done}/{total or '?'} {progress.get('unit', 'stages')})")
        error = detail.get("error")
        if error:
            self._set_text(self._diagnostics_text,
                           f"[{error['code']}] {error['message']}"
                           + (" (retryable)" if error.get("retryable") else ""))
        self._set_text(self._explanation_text, detail.get("explanation") or "No explanation yet.")
        diff = detail.get("diff") or {}
        self._set_text(self._edit_diff_text,
                       "\n".join(diff.get(name, "") for name in diff))
        report = detail.get("report")
        if report:
            lines = [f"Status: {report['status']}"]
            lines += [f"  [{c['outcome']}] {c['name']}"
                      + (f" — {c['detail']}" if c.get("detail") else "")
                      for c in report.get("checks", [])]
            self._set_text(self._diagnostics_text, "\n".join(lines))
        self._runs = detail.get("runs") or {}
        if detail["state"] in TERMINAL_STATES:
            self._finish_job()
            if self._runs:
                self._load_comparison()
        else:
            self._top.after(1000, self._poll)

    def _finish_job(self) -> None:
        self._polling = False
        self._submit_btn.config(state=tk.NORMAL)
        self._cancel_btn.config(state=tk.DISABLED)

    def _cancel(self) -> None:
        if not self._job_id:
            return
        job_id = self._job_id
        self._status_var.set("Cancellation requested…")
        self._run_async(lambda: self.editor().cancel_job(job_id))

    # ── results ──────────────────────────────────────────────────────────
    def _build_results(self) -> None:
        frame = ttk.LabelFrame(self._top, text="Results", padding=8)
        frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))

        left = ttk.Frame(frame)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._comparison_text = tk.Text(left, height=12, width=52, wrap=tk.WORD, state=tk.DISABLED)
        self._comparison_text.pack(fill=tk.BOTH, expand=True)

        right = ttk.Frame(frame)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))
        for label, attr in (("Explanation", "_explanation_text"),
                            ("Edit diff", "_edit_diff_text"),
                            ("Diagnostics", "_diagnostics_text")):
            ttk.Label(right, text=label, font=("TkDefaultFont", 9, "bold")).pack(anchor=tk.W)
            widget = tk.Text(right, height=4, wrap=tk.WORD, state=tk.DISABLED)
            widget.pack(fill=tk.BOTH, expand=True)
            setattr(self, attr, widget)

    def _build_detail(self) -> None:
        frame = ttk.LabelFrame(self._top, text="Source / documentation / diff", padding=8)
        frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(4, 4))

        columns = ttk.Frame(frame)
        columns.pack(fill=tk.BOTH, expand=True)
        self._source_text = self._text_column(columns, "Source", 0)
        self._doc_text = self._text_column(columns, "Documentation", 1)
        self._diff_text = self._text_column(columns, "Diff vs parent", 2)
        self._detail_text = self._text_column(columns, "History / reports", 3)

        self._default_btn = ttk.Button(frame, text="Set default", command=self._set_default)
        self._default_btn.pack(side=tk.LEFT, pady=(6, 0))
        self._again_btn = ttk.Button(frame, text="Backtest again", command=self._backtest_again,
                                     state=tk.DISABLED)
        self._again_btn.pack(side=tk.LEFT, padx=(6, 0), pady=(6, 0))
        self._editfrom_btn = ttk.Button(frame, text="Edit from this version",
                                        command=lambda: self._instruction.focus_set())
        self._editfrom_btn.pack(side=tk.LEFT, padx=(6, 0), pady=(6, 0))
        self._delete_btn = ttk.Button(frame, text="Delete version", command=self._delete)
        self._delete_btn.pack(side=tk.LEFT, padx=(6, 0), pady=(6, 0))
        self._replacement_label = ttk.Label(frame, text="Replacement")
        self._replacement_label.pack(side=tk.LEFT, padx=(6, 2), pady=(6, 0))
        self._replacement = ttk.Combobox(frame, width=12, state="readonly")
        self._replacement.pack(side=tk.LEFT, pady=(6, 0))
        self._replacement_map: dict[str, str] = {}
        self._replacement_label.pack_forget()
        self._replacement.pack_forget()

    def _text_column(self, parent, label: str, column: int) -> tk.Text:
        wrapper = ttk.Frame(parent)
        wrapper.grid(row=0, column=column, sticky="nsew", padx=2)
        parent.columnconfigure(column, weight=1)
        ttk.Label(wrapper, text=label, font=("TkDefaultFont", 9, "bold")).pack(anchor=tk.W)
        widget = tk.Text(wrapper, height=10, width=34, wrap=tk.NONE, state=tk.DISABLED)
        widget.pack(fill=tk.BOTH, expand=True)
        return widget

    def _current_generation(self) -> int:
        return int(self._patterns.get(self._pattern_id or "", {}).get("generation", 0))

    def _set_default(self) -> None:
        version = self._selected_version()
        if version is None:
            return
        generation = self._current_generation()

        def work():
            return self.editor().set_default(
                pattern_id=self._pattern_id, version_id=version["version_id"],
                expected_generation=generation)

        self._run_async(work, on_done=lambda _r: self._after_mutation("Default updated."),
                        on_error=self._on_mutation_error)

    def _backtest_again(self) -> None:
        if not self._job_id:
            return
        job_id = self._job_id

        def work():
            return self.editor().retry_backtest(job_id)

        self._run_async(work, on_done=self._on_submitted, on_error=self._on_mutation_error)

    def _delete(self) -> None:
        version = self._selected_version()
        if version is None:
            return
        generation = self._current_generation()
        replacement = self._replacement_map.get(self._replacement.get())

        def work():
            return self.editor().archive(
                pattern_id=self._pattern_id, version_id=version["version_id"],
                replacement_default_version_id=replacement, expected_generation=generation)

        self._run_async(work, on_done=lambda _r: self._after_mutation("Version archived."),
                        on_error=self._on_mutation_error)

    def _on_mutation_error(self, exc: Exception) -> None:
        # A generation conflict reloads the catalog; other errors are shown.
        if "changed" in str(exc).lower() or "conflict" in str(exc).lower():
            self._status_var.set(f"Conflict: {exc}")
            self._refresh_catalog()
            self._load_versions()
        else:
            self._show_error(exc)

    def _after_mutation(self, message: str) -> None:
        self._status_var.set(message)
        self._version_id = None
        self._refresh_catalog()
        self._load_versions()

    def _load_comparison(self) -> None:
        runs = self._runs or {}
        candidate_id, base_id = runs.get("candidate"), runs.get("base")

        def work():
            editor = self.editor()
            return (editor.run_payload(candidate_id) if candidate_id else None,
                    editor.run_payload(base_id) if base_id else None)

        self._run_async(work, on_done=self._render_comparison)

    def _render_comparison(self, payloads) -> None:
        candidate, base = payloads
        keys = ("trade_count", "win_rate", "realized_pnl", "unrealized_pnl",
                "net_pnl", "fees", "max_drawdown_pct", "open_position_count")
        lines = [
            f"Candidate: {candidate['state'] if candidate else 'none'}"
            f"   Base: {base['state'] if base else 'none'}",
            "",
            f"{'metric':22s}{'base':>14s}{'candidate':>14s}",
            "-" * 50,
        ]

        def value(payload, key):
            if not payload or not payload.get("result"):
                return None
            return payload["result"]["metrics"].get(key)

        for key in keys:
            left, right = value(base, key), value(candidate, key)
            lines.append(f"{key:22s}{str(left if left is not None else '—'):>14s}"
                         f"{str(right if right is not None else '—'):>14s}")
        if candidate and candidate.get("result"):
            lines.append("")
            lines.append("Zero trades: " + (
                "yes (a valid result, not a failure)"
                if candidate["result"]["zero_trades"] else "no"))
        self._set_text(self._comparison_text, "\n".join(lines))

    # ── lifecycle ────────────────────────────────────────────────────────
    def _on_close(self) -> None:
        # Closing stops this window's polling only. The durable job keeps its
        # state; nothing here marks it cancelled or completed.
        self._closed = True
        self._polling = False
        try:
            self._top.destroy()
        except tk.TclError:
            pass

    # ── test/debug helpers ───────────────────────────────────────────────
    @property
    def job_id(self) -> Optional[str]:
        return self._job_id
