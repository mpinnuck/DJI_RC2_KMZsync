import os
import queue
import threading
import time
import traceback
import io
import tkinter as tk
from datetime import datetime
from tkinter import ttk, filedialog, messagebox

_APP_VERSION = "v1.3"


try:
    from PIL import Image, ImageTk
except ImportError:
    Image = None
    ImageTk = None

from model.rc2_mission import RC2Mission
from model.kmz_file import KMZFile
from viewmodel.sync_viewmodel import SyncViewModel


# ---------------------------------------------------------------------------
# Colour palette & fonts
# ---------------------------------------------------------------------------
_BG         = "#f4f6f9"
_PANEL_BG   = "#ffffff"
_BORDER     = "#d8dee9"
_ACCENT     = "#d97706"         # amber  — RC-2 side
_ACCENT2    = "#2563eb"         # blue   — PC side
_TEXT       = "#1f2937"
_TEXT_DIM   = "#667085"
_SEL_BG     = "#dbeafe"
_SEL_FG     = "#0f172a"
_SUCCESS    = "#15803d"
_ERROR      = "#dc2626"
_LOG_BG     = "#f8fafc"
_LOG_BORDER = "#cbd5e1"
_MODE_BG    = "#fff7ed"
_MODE_FG    = "#9a3412"

_FONT_BODY  = ("Segoe UI", 10)
_FONT_BOLD  = ("Segoe UI", 10, "bold")
_FONT_TITLE = ("Segoe UI", 11, "bold")
_FONT_SMALL = ("Segoe UI", 8)


class MainView:
    def __init__(self, root: tk.Tk, viewmodel: SyncViewModel):
        self._root      = root
        self._vm        = viewmodel
        self._missions_by_guid: dict[str, RC2Mission] = {}
        self._last_rc2_missions: list[RC2Mission] = []
        self._last_preview_data: dict[str, bytes | None] = {}
        self._mission_preview_images: dict[str, tk.PhotoImage] = {}
        self._preview_placeholder: tk.PhotoImage | None = None
        self._refresh_queue: queue.Queue = queue.Queue()
        self._copy_queue: queue.Queue = queue.Queue()
        self._inspect_queue: queue.Queue = queue.Queue()
        self._delete_queue: queue.Queue = queue.Queue()
        self._mapping_rows: list[dict[str, str]] = []
        self._pc_files_by_item: dict[str, KMZFile] = {}
        self._refresh_serial = 0
        self._refresh_active = False
        self._refresh_pc_loaded = False
        self._refresh_rc_loaded = False
        self._busy = False
        self._copy_in_progress = False
        self._inspect_in_progress = False
        self._delete_in_progress = False
        self._refresh_pending = False
        self._last_error_log_key: str | None = None
        self._last_error_log_time = 0.0
        self._setup_root()
        self._apply_styles()
        self._rc2_filter_var = tk.StringVar(value="")
        self._build_ui()
        # Ensure uncaught Tk callback exceptions are surfaced in the UI log.
        self._root.report_callback_exception = self._handle_callback_exception
        self._refresh()

    # ------------------------------------------------------------------
    # Root window
    # ------------------------------------------------------------------
    def _setup_root(self) -> None:
        self._root.title("DJI RC-2  KMZ Mission Sync")
        width, height = 1320, 940
        self._root.minsize(920, 760)
        self._root.configure(bg=_BG)
        self._root.resizable(True, True)

        # Center the initial window on the active display.
        self._root.update_idletasks()
        screen_w = self._root.winfo_screenwidth()
        screen_h = self._root.winfo_screenheight()
        x = max((screen_w - width) // 2, 0)
        y = max((screen_h - height) // 2, 0)
        self._root.geometry(f"{width}x{height}+{x}+{y}")

    # ------------------------------------------------------------------
    # Styles
    # ------------------------------------------------------------------
    def _apply_styles(self) -> None:
        s = ttk.Style()
        s.theme_use("clam")

        s.configure(".",          background=_BG,       foreground=_TEXT,    font=_FONT_BODY)
        s.configure("TFrame",     background=_BG)
        s.configure("TLabel",     background=_BG,       foreground=_TEXT)
        s.configure("TEntry",     fieldbackground=_PANEL_BG, foreground=_TEXT,
                    insertcolor=_TEXT, bordercolor=_BORDER,
                    lightcolor=_BORDER, darkcolor=_BORDER)

        s.configure("TButton",    background="#edf2f7", foreground=_TEXT,
                    borderwidth=0, focusthickness=0, padding=6)
        s.map("TButton",
              background=[("active", "#e2e8f0"), ("pressed", "#cbd5e1")])

        s.configure("Accent.TButton", background=_ACCENT, foreground="#ffffff",
                    font=_FONT_BOLD, padding=(14, 8))
        s.map("Accent.TButton",
              background=[("active", "#b45309"), ("pressed", "#92400e")])

        s.configure("Treeview",
                    background=_PANEL_BG, fieldbackground=_PANEL_BG,
                    foreground=_TEXT, rowheight=28, font=_FONT_BODY,
                    borderwidth=0)
        s.configure("Mission.Treeview",
                background=_PANEL_BG, fieldbackground=_PANEL_BG,
                foreground=_TEXT, rowheight=68, font=_FONT_BODY,
                borderwidth=0)
        s.configure("Treeview.Heading",
                    background=_BG, foreground=_ACCENT2,
                    font=_FONT_BOLD, relief="flat", borderwidth=0)
        s.map("Treeview",
              background=[("selected", _SEL_BG)],
              foreground=[("selected", _SEL_FG)])
        s.map("Treeview.Heading",
              background=[("active", "#e5e7eb")])

        s.configure("TSeparator", background=_BORDER)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        # Header
        header = tk.Frame(self._root, bg=_PANEL_BG, height=48)
        header.pack(fill=tk.X)
        tk.Label(header, text="DJI RC-2  KMZ Mission Sync",
                 bg=_PANEL_BG, fg=_ACCENT, font=("Segoe UI", 13, "bold")).pack(
            side=tk.LEFT, padx=16, pady=10)
        tk.Label(header, text="RC-2  ←→  Dronelink",
                 bg=_PANEL_BG, fg=_TEXT_DIM, font=_FONT_SMALL).pack(
            side=tk.LEFT, padx=4, pady=10)
        tk.Label(
            header,
            text=_APP_VERSION,
            bg=_PANEL_BG,
            fg=_TEXT_DIM,
            font=_FONT_SMALL,
        ).pack(side=tk.LEFT, padx=4, pady=10)
        self._mode_var = tk.StringVar(value="Mode: Not Set")
        self._mode_label = tk.Label(
            header,
            textvariable=self._mode_var,
            bg=_MODE_BG,
            fg=_MODE_FG,
            font=_FONT_SMALL,
            padx=10,
            pady=4,
        )
        self._mode_label.pack(side=tk.RIGHT, padx=16, pady=8)
        self._busy_var = tk.StringVar(value="")
        self._busy_label = tk.Label(
            header,
            textvariable=self._busy_var,
            bg=_PANEL_BG,
            fg=_TEXT_DIM,
            font=_FONT_SMALL,
            anchor=tk.E,
        )
        self._busy_label.pack(side=tk.RIGHT, padx=(0, 10), pady=10)

        # Path config bar
        cfg = tk.Frame(self._root, bg=_BG)
        cfg.pack(fill=tk.X, padx=12, pady=(10, 4))
        self._rc2_entry = self._path_row(cfg, "RC-2 Root:",     self._vm.rc2_folder, self._browse_rc2, row=0)
        self._pc_entry  = self._path_row(cfg, "PC KMZ Folder:", self._vm.pc_folder,  self._browse_pc,  row=1)

        # Refresh lists when a path is edited manually.
        self._rc2_entry.bind("<Return>", lambda _event: self._refresh())
        self._pc_entry.bind("<Return>", lambda _event: self._refresh())
        self._rc2_entry.bind("<FocusOut>", lambda _event: self._refresh())
        self._pc_entry.bind("<FocusOut>", lambda _event: self._refresh())

        ttk.Separator(self._root, orient="horizontal").pack(fill=tk.X, padx=12, pady=4)

        # Main panel
        main = tk.Frame(self._root, bg=_BG, height=420)
        main.pack(fill=tk.X, expand=False, padx=12, pady=(0, 4))
        main.pack_propagate(False)

        self._rc2_tree = self._mission_panel(main)
        self._copy_button_panel(main)
        self._pc_tree  = self._kmz_panel(main)
        self._bind_tree_clear_on_blank(self._rc2_tree)
        self._bind_tree_clear_on_blank(self._pc_tree)
        self._root.bind("<Escape>", self._clear_all_selections)

        # Status bar
        self._status_var = tk.StringVar(value="Ready.")
        self._status_label = tk.Label(
            self._root, textvariable=self._status_var,
            bg=_BG, fg=_TEXT_DIM, font=_FONT_SMALL, anchor=tk.W)
        self._status_label.pack(fill=tk.X, padx=14, pady=(0, 6))

        # Activity log panel
        bottom_tabs = ttk.Notebook(self._root)
        bottom_tabs.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 10))

        log_panel = tk.Frame(bottom_tabs, bg=_PANEL_BG, highlightthickness=1,
                     highlightbackground=_LOG_BORDER, highlightcolor=_LOG_BORDER)
        mapping_panel = tk.Frame(bottom_tabs, bg=_PANEL_BG, highlightthickness=1,
                     highlightbackground=_LOG_BORDER, highlightcolor=_LOG_BORDER)
        bottom_tabs.add(log_panel, text="Activity Log")
        bottom_tabs.add(mapping_panel, text="Mission Mapping")

        log_header = tk.Frame(log_panel, bg=_PANEL_BG)
        log_header.pack(fill=tk.X, padx=8, pady=(6, 2))

        tk.Label(
            log_header,
            text="Activity Log",
            bg=_PANEL_BG,
            fg=_TEXT_DIM,
            font=_FONT_SMALL,
            anchor=tk.W,
        ).pack(side=tk.LEFT)
        ttk.Button(
            log_header,
            text="Detect RC-2",
            command=self._detect_rc2,
        ).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(
            log_header,
            text="ADB Status",
            command=self._check_adb_status,
        ).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(
            log_header,
            text="Inspect Mission",
            command=self._inspect_selected_mission,
        ).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(log_header, text="Clear Log", command=self._clear_log).pack(side=tk.RIGHT)

        log_body = tk.Frame(log_panel, bg=_PANEL_BG)
        log_body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        self._log_text = tk.Text(
            log_body,
            height=20,
            wrap="word",
            bg=_LOG_BG,
            fg=_TEXT,
            relief="flat",
            borderwidth=0,
            font=_FONT_SMALL,
            state=tk.DISABLED,
        )
        self._log_text.bind("<Button-1>", self._focus_log)
        self._log_text.bind("<Control-a>", self._select_all_log)
        self._log_text.bind("<Control-A>", self._select_all_log)
        self._log_text.bind("<Control-c>", self._copy_selected_log)
        self._log_text.bind("<Control-C>", self._copy_selected_log)
        log_scroll = ttk.Scrollbar(log_body, orient="vertical", command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=log_scroll.set)
        self._log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Mission mapping tab
        mapping_header = tk.Frame(mapping_panel, bg=_PANEL_BG)
        mapping_header.pack(fill=tk.X, padx=8, pady=(6, 2))
        tk.Label(
            mapping_header,
            text="Latest Source → RC-2 Mission Mapping",
            bg=_PANEL_BG,
            fg=_TEXT_DIM,
            font=_FONT_SMALL,
            anchor=tk.W,
        ).pack(side=tk.LEFT)
        ttk.Button(
            mapping_header,
            text="Refresh Mapping",
            command=self._refresh_mapping,
        ).pack(side=tk.RIGHT)

        self._mapping_updated_var = tk.StringVar(value="Updated: n/a")
        tk.Label(
            mapping_panel,
            textvariable=self._mapping_updated_var,
            bg=_PANEL_BG,
            fg=_TEXT_DIM,
            font=_FONT_SMALL,
            anchor=tk.W,
        ).pack(fill=tk.X, padx=10, pady=(0, 2))

        self._mapping_note_var = tk.StringVar(value="")
        tk.Label(
            mapping_panel,
            textvariable=self._mapping_note_var,
            bg=_PANEL_BG,
            fg=_TEXT_DIM,
            font=_FONT_SMALL,
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=1200,
        ).pack(fill=tk.X, padx=10, pady=(0, 6))

        mapping_body = tk.Frame(mapping_panel, bg=_PANEL_BG)
        mapping_body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self._mapping_tree = ttk.Treeview(
            mapping_body,
            columns=("source", "mission", "target", "mode", "copied"),
            show="headings",
            selectmode="browse",
        )
        self._mapping_tree.heading("source", text="Source KMZ")
        self._mapping_tree.heading("mission", text="Mission GUID")
        self._mapping_tree.heading("target", text="Saved As")
        self._mapping_tree.heading("mode", text="Mode")
        self._mapping_tree.heading("copied", text="Copied At")
        self._mapping_tree.column("source", width=300, stretch=True)
        self._mapping_tree.column("mission", width=300, stretch=True)
        self._mapping_tree.column("target", width=240, stretch=True)
        self._mapping_tree.column("mode", width=100, stretch=False, anchor=tk.CENTER)
        self._mapping_tree.column("copied", width=180, stretch=False)
        map_scroll = ttk.Scrollbar(mapping_body, orient="vertical", command=self._mapping_tree.yview)
        self._mapping_tree.configure(yscrollcommand=map_scroll.set)
        self._mapping_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        map_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._bind_tree_clear_on_blank(self._mapping_tree)

        self._log("Application started.")
        self._refresh_mapping()

    def _bind_tree_clear_on_blank(self, tree: ttk.Treeview) -> None:
        tree.bind("<Button-1>", lambda event, tv=tree: self._clear_tree_selection_if_blank(tv, event), add="+")

    @staticmethod
    def _clear_tree_selection_if_blank(tree: ttk.Treeview, event) -> None:
        region = tree.identify("region", event.x, event.y)
        row_id = tree.identify_row(event.y)
        if region in ("tree", "cell") and row_id:
            return
        tree.selection_remove(tree.selection())
        tree.focus("")

    def _clear_all_selections(self, _event=None):
        for tree in (self._rc2_tree, self._pc_tree, self._mapping_tree):
            tree.selection_remove(tree.selection())
            tree.focus("")
        self._set_status("Selections cleared.", colour=_TEXT_DIM)
        return "break"

    # ------------------------------------------------------------------
    # Path row helper
    # ------------------------------------------------------------------
    def _path_row(self, parent, label: str, default: str,
                  browse_cmd, row: int) -> ttk.Entry:
        tk.Label(parent, text=label, bg=_BG, fg=_TEXT_DIM,
                 font=_FONT_SMALL, width=14, anchor=tk.E).grid(
            row=row, column=0, sticky=tk.E, padx=(0, 6), pady=3)

        entry = ttk.Entry(parent, width=68)
        entry.insert(0, default)
        entry.grid(row=row, column=1, sticky=tk.EW, padx=(0, 6), pady=3)

        ttk.Button(parent, text="…", width=3, command=browse_cmd).grid(
            row=row, column=2, pady=3)

        parent.columnconfigure(1, weight=1)
        return entry

    # ------------------------------------------------------------------
    # Left panel — RC-2 missions
    # ------------------------------------------------------------------
    def _mission_panel(self, parent) -> ttk.Treeview:
        frame = tk.Frame(parent, bg=_PANEL_BG)
        frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Configure grid rows: top (fixed), middle (expanding), bottom (fixed)
        frame.grid_rowconfigure(1, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        # Top section - labels and filter
        top_frame = tk.Frame(frame, bg=_PANEL_BG)
        top_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 2))

        tk.Label(top_frame, text="DJI RC-2  —  Mission Slots",
                 bg=_PANEL_BG, fg=_ACCENT, font=_FONT_TITLE).pack(anchor=tk.W)
        tk.Label(top_frame, text="Filter by mission name, GUID, or timestamp",
                 bg=_PANEL_BG, fg=_TEXT_DIM, font=_FONT_SMALL).pack(
            anchor=tk.W, pady=(0, 4))

        filter_entry = ttk.Entry(top_frame, textvariable=self._rc2_filter_var)
        filter_entry.pack(fill=tk.X, pady=(0, 6))
        filter_entry.bind("<KeyRelease>", lambda _event: self._render_rc2_tree())

        # Middle section - tree with scrollbar
        tree_frame = tk.Frame(frame, bg=_PANEL_BG)
        tree_frame.grid(row=1, column=0, sticky="nsew", padx=(6, 0), pady=(0, 6))
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        tree = ttk.Treeview(tree_frame, columns=("slot",),
                    show="tree headings", selectmode="browse",
                    style="Mission.Treeview")
        tree.heading("#0", text="Preview")
        tree.heading("slot", text="Mission (KMZ / Slot GUID / Last Modified)")
        tree.column("#0", width=92, minwidth=92, stretch=False, anchor=tk.CENTER)
        tree.column("slot", width=420, stretch=True)
        tree.tag_configure("empty", foreground=_TEXT_DIM)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.grid(row=0, column=1, sticky="ns", padx=(0, 4))
        tree.grid(row=0, column=0, sticky="nsew")

        # Bottom section - delete button
        bottom_frame = tk.Frame(frame, bg=_PANEL_BG)
        bottom_frame.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 8))
        ttk.Button(bottom_frame, text="Delete Selected Mission", command=self._delete_selected_mission).pack(
            fill=tk.X
        )

        return tree

    # ------------------------------------------------------------------
    # Centre — copy button
    # ------------------------------------------------------------------
    def _copy_button_panel(self, parent) -> None:
        frame = tk.Frame(parent, bg=_BG, width=100)
        frame.pack(side=tk.LEFT, fill=tk.Y, padx=8)
        frame.pack_propagate(False)

        tk.Frame(frame, bg=_BG).pack(expand=True, fill=tk.BOTH)
        ttk.Button(frame, text="COPY  ◀", style="Accent.TButton",
                   command=self._on_copy).pack(padx=8, pady=4)
        ttk.Button(frame, text="COPY  ▶",
                   command=self._on_copy_back).pack(padx=8, pady=4)
        tk.Label(frame, text="select one\nfrom each\npanel",
                 bg=_BG, fg=_TEXT_DIM, font=_FONT_SMALL,
                 justify=tk.CENTER).pack(pady=(6, 0))
        tk.Frame(frame, bg=_BG).pack(expand=True, fill=tk.BOTH)

    # ------------------------------------------------------------------
    # Right panel — PC KMZ files
    # ------------------------------------------------------------------
    def _kmz_panel(self, parent) -> ttk.Treeview:
        frame = tk.Frame(parent, bg=_PANEL_BG)
        frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Configure grid rows: top (fixed), middle (expanding), bottom (fixed)
        frame.grid_rowconfigure(1, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        # Top section - labels
        top_frame = tk.Frame(frame, bg=_PANEL_BG)
        top_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 2))

        tk.Label(top_frame, text="PC Source  —  Dronelink KMZ Files",
                 bg=_PANEL_BG, fg=_ACCENT2, font=_FONT_TITLE).pack(anchor=tk.W)
        tk.Label(top_frame, text="select the mission to inject",
                 bg=_PANEL_BG, fg=_TEXT_DIM, font=_FONT_SMALL).pack(
            anchor=tk.W, pady=(0, 4))

        # Middle section - tree with scrollbar
        tree_frame = tk.Frame(frame, bg=_PANEL_BG)
        tree_frame.grid(row=1, column=0, sticky="nsew", padx=(6, 0), pady=(0, 6))
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        tree = ttk.Treeview(tree_frame, columns=("filename",),
                                show="tree headings", selectmode="browse")
        tree.heading("#0", text="Folder / Path")
        tree.heading("filename", text="KMZ Filename")
        tree.column("#0", width=150, stretch=False)
        tree.column("filename", width=300, stretch=True)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.grid(row=0, column=1, sticky="ns", padx=(0, 4))
        tree.grid(row=0, column=0, sticky="nsew")

        # Bottom section - delete button
        bottom_frame = tk.Frame(frame, bg=_PANEL_BG)
        bottom_frame.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 8))
        ttk.Button(bottom_frame, text="Delete Selected KMZ", command=self._delete_selected_kmz).pack(
            fill=tk.X
        )

        return tree

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------
    def _browse_rc2(self) -> None:
        path = filedialog.askdirectory(title="Select DJI RC-2 Waypoint Root Folder")
        if path:
            self._rc2_entry.delete(0, tk.END)
            self._rc2_entry.insert(0, path)
            self._vm.set_rc2_folder(path)
            self._log(f"RC-2 root set to: {path}")
            self._refresh()

    def _browse_pc(self) -> None:
        path = filedialog.askdirectory(title="Select PC Source KMZ Folder")
        if path:
            self._pc_entry.delete(0, tk.END)
            self._pc_entry.insert(0, path)
            self._vm.set_pc_folder(path)
            self._log(f"PC KMZ folder set to: {path}")
            self._refresh()

    def _on_copy(self) -> None:
        if self._copy_in_progress:
            self._set_status("Copy already in progress...", colour=_TEXT_DIM)
            self._log("Copy request ignored: copy is already in progress.", level="WARN")
            return

        mission  = self._selected_mission()
        kmz_file = self._selected_kmz()

        if kmz_file is None:
            self._set_status("Select one KMZ file first.", colour=_ERROR)
            self._log("Copy blocked: no KMZ file selected.", level="WARN")
            return

        if mission is None:
            self._set_status("Select an existing RC-2 mission first.", colour=_ERROR)
            self._log(
                "Copy blocked: no RC-2 mission selected. RC-2 does not reliably index synthetic mission folders; use overwrite of an existing mission.",
                level="WARN",
            )
            return

        dest_filename = mission.kmz_name if mission.kmz_name else f"{mission.guid}.kmz"
        self._log(f"Copy requested: {kmz_file.filename} -> mission {mission.guid}", level="INFO")
        self._log(
            f"Copy started: source={kmz_file.full_path} | destination={mission.full_folder_path}\\{dest_filename}",
            level="INFO",
        )
        if mission.kmz_name:
            self._log(
                f"Selected mission contains '{mission.kmz_name}'. Performing silent overwrite.",
                level="INFO",
            )
        self._log(self._vm.confirm_copy_message(mission, kmz_file).replace("\n", " | "), level="INFO")

        self._set_busy(True, "Copying mission to RC-2...")
        self._set_status("Copy in progress...", colour=_TEXT_DIM)
        self._copy_in_progress = True

        def _worker() -> None:
            started = time.monotonic()
            try:
                ok, msg = self._vm.execute_copy(
                    mission,
                    kmz_file,
                )
            except Exception as exc:
                ok, msg = False, f"Unhandled copy error: {exc}"
            elapsed_ms = int((time.monotonic() - started) * 1000)
            self._copy_queue.put((ok, msg, elapsed_ms))

        threading.Thread(target=_worker, daemon=True).start()
        self._root.after(50, self._drain_copy_queue)

    def _on_copy_back(self) -> None:
        if self._copy_in_progress:
            self._set_status("Copy already in progress...", colour=_TEXT_DIM)
            self._log("Copy-back request ignored: copy is already in progress.", level="WARN")
            return

        mission = self._selected_mission()
        target_kmz = self._selected_kmz()

        if mission is None:
            self._set_status("Select an existing RC-2 mission first.", colour=_ERROR)
            self._log("Copy-back blocked: no RC-2 mission selected.", level="WARN")
            return

        if target_kmz is None:
            default_name = f"{mission.guid}.kmz"
            default_path = os.path.join(self._vm.pc_folder, default_name)
            target_kmz = KMZFile(filename=default_name, full_path=default_path)
            self._log(
                f"Copy-back target not selected. Defaulting target filename to {default_name}",
                level="INFO",
            )

        self._log(f"Copy-back requested: mission {mission.guid} -> {target_kmz.filename}", level="INFO")
        self._set_busy(True, "Copying mission back to PC...")
        self._set_status("Copy-back in progress...", colour=_TEXT_DIM)
        self._copy_in_progress = True

        def _worker() -> None:
            started = time.monotonic()
            try:
                ok, msg = self._vm.execute_copy_from_mission(mission, target_kmz)
            except Exception as exc:
                ok, msg = False, f"Unhandled copy-back error: {exc}"
            elapsed_ms = int((time.monotonic() - started) * 1000)
            self._copy_queue.put((ok, msg, elapsed_ms))

        threading.Thread(target=_worker, daemon=True).start()
        self._root.after(50, self._drain_copy_queue)

    def _delete_selected_mission(self) -> None:
        mission = self._selected_mission()
        if mission is None:
            self._set_status("Select an RC-2 mission to delete.", colour=_ERROR)
            self._log("Delete blocked: no RC-2 mission selected.", level="WARN")
            return

        if not messagebox.askyesno(
            "Confirm Delete",
            f"Permanently delete RC-2 mission slot?\n\n{mission.guid}\n\nThis cannot be undone."
        ):
            return

        self._set_busy(True, "Deleting mission...")
        self._set_status("Delete in progress...", colour=_TEXT_DIM)
        self._delete_in_progress = True

        def _worker() -> None:
            started = time.monotonic()
            try:
                ok, msg = self._vm.delete_rc2_mission(mission)
            except Exception as exc:
                ok, msg = False, f"Unhandled delete error: {exc}"
            elapsed_ms = int((time.monotonic() - started) * 1000)
            self._delete_queue.put((ok, msg, elapsed_ms))

        threading.Thread(target=_worker, daemon=True).start()
        self._root.after(50, self._drain_delete_queue)

    def _delete_selected_kmz(self) -> None:
        kmz_file = self._selected_kmz()
        if kmz_file is None:
            self._set_status("Select a Dronelink KMZ file to delete.", colour=_ERROR)
            self._log("Delete blocked: no PC KMZ selected.", level="WARN")
            return

        if not messagebox.askyesno(
            "Confirm Delete",
            f"Delete PC KMZ file?\n\n{kmz_file.filename}"
        ):
            return

        ok, msg = self._vm.delete_pc_kmz_file(kmz_file)
        if ok:
            self._set_status("KMZ file deleted.", colour=_SUCCESS)
            self._log(msg, level="OK")
            files = self._vm.load_pc_kmz_files()
            pc_error = self._vm.consume_last_error()
            self._populate_pc_tree(files, pc_error)
            self._refresh_mapping()
            return

        self._set_status("✘  KMZ delete failed.", colour=_ERROR)
        self._log(msg.replace("\n", " | "), level="ERROR")

    def _drain_copy_queue(self) -> None:
        latest = None
        while True:
            try:
                latest = self._copy_queue.get_nowait()
            except queue.Empty:
                break

        if latest is None:
            if self._copy_in_progress:
                self._root.after(50, self._drain_copy_queue)
            return

        ok, msg, elapsed_ms = latest
        self._copy_in_progress = False
        self._set_busy(False, "")

        if ok:
            self._set_status(f"✔  {msg.splitlines()[0]}", colour=_SUCCESS)
            self._log(msg.replace("\n", " | "), level="OK")
            self._log(f"Copy completed in {elapsed_ms} ms", level="DEBUG")
            self._refresh_mapping()
            self._refresh()
            return

        self._set_status("✘  Copy failed.", colour=_ERROR)
        self._log(msg.replace("\n", " | "), level="ERROR")
        self._log(f"Copy failed after {elapsed_ms} ms", level="DEBUG")

    def _drain_delete_queue(self) -> None:
        latest = None
        while True:
            try:
                latest = self._delete_queue.get_nowait()
            except queue.Empty:
                break

        if latest is None:
            if self._delete_in_progress:
                self._root.after(50, self._drain_delete_queue)
            return

        ok, msg, elapsed_ms = latest
        self._delete_in_progress = False
        self._set_busy(False, "")

        if ok:
            self._set_status(f"✔  Mission deleted.", colour=_SUCCESS)
            self._log(msg, level="OK")
            self._log(f"Delete completed in {elapsed_ms} ms", level="DEBUG")
            self._refresh()
            return

        self._set_status("✘  Delete failed.", colour=_ERROR)
        self._log(msg.replace("\n", " | "), level="ERROR")
        self._log(f"Delete failed after {elapsed_ms} ms", level="DEBUG")

    def _check_adb_status(self) -> None:
        self._log("ADB status check requested.")
        ok, message = self._vm.get_adb_status()
        if ok:
            self._set_status("ADB device ready.", colour=_SUCCESS)
            self._log(message, level="OK")
            return

        self._set_status("✘  ADB not ready.", colour=_ERROR)
        self._log(message, level="ERROR")

    def _inspect_selected_mission(self) -> None:
        if self._inspect_in_progress:
            self._set_status("Mission inspection already in progress...", colour=_TEXT_DIM)
            self._log("Inspect request ignored: inspection already in progress.", level="WARN")
            return

        mission = self._selected_mission()
        if mission is None:
            self._set_status("Select an RC-2 mission to inspect.", colour=_ERROR)
            self._log("Inspect blocked: no RC-2 mission selected.", level="WARN")
            return

        self._inspect_in_progress = True
        self._set_busy(True, "Inspecting selected mission...")
        self._set_status("Inspecting selected mission...", colour=_TEXT_DIM)
        self._log(f"Inspect mission requested for mission {mission.guid}", level="INFO")

        def _worker() -> None:
            ok, details = self._vm.inspect_mission_storage(mission)
            self._inspect_queue.put((ok, details))

        threading.Thread(target=_worker, daemon=True).start()
        self._root.after(50, self._drain_inspect_queue)

    def _drain_inspect_queue(self) -> None:
        latest = None
        while True:
            try:
                latest = self._inspect_queue.get_nowait()
            except queue.Empty:
                break

        if latest is None:
            if self._inspect_in_progress:
                self._root.after(50, self._drain_inspect_queue)
            return

        ok, details = latest
        self._inspect_in_progress = False
        self._set_busy(False, "")
        self._log(details, level="OK" if ok else "ERROR")
        self._set_status("Mission inspection complete." if ok else "Mission inspection failed.", colour=_SUCCESS if ok else _ERROR)

    def _detect_rc2(self) -> None:
        self._log("RC-2 detection requested.")
        ok, message = self._vm.auto_detect_rc2_folder()
        if ok:
            self._rc2_entry.delete(0, tk.END)
            self._rc2_entry.insert(0, self._vm.rc2_folder)
            self._set_status("RC-2 access path detected.", colour=_SUCCESS)
            self._log(message, level="OK")
            self._refresh()
            return

        self._set_status("✘  RC-2 not detected.", colour=_ERROR)
        self._log(message, level="ERROR")

    # ------------------------------------------------------------------
    # Tree population
    # ------------------------------------------------------------------
    def _refresh(self) -> None:
        if self._busy:
            self._refresh_pending = True
            return

        rc2_path = self._rc2_entry.get().strip()
        pc_path  = self._pc_entry.get().strip()
        if rc2_path != self._vm.rc2_folder:
            self._vm.set_rc2_folder(rc2_path)
        if pc_path != self._vm.pc_folder:
            self._vm.set_pc_folder(pc_path)

        self._refresh_serial += 1
        serial = self._refresh_serial
        self._refresh_active = True
        self._refresh_pc_loaded = False
        self._refresh_rc_loaded = False
        self._set_busy(True, "Please wait initializing...")
        self._set_status("Please wait initializing...")
        self._log("Initializing mission and file lists...", level="INFO")

        def _worker() -> None:
            started = time.monotonic()

            def _load_pc() -> None:
                try:
                    files = self._vm.load_pc_kmz_files()
                    pc_error = self._vm.consume_last_error()
                    self._refresh_queue.put(("pc", serial, files, pc_error, None))
                except Exception as exc:
                    self._refresh_queue.put(("error", serial, f"PC refresh failed: {exc}"))

            def _load_rc2() -> None:
                try:
                    self._vm.clear_stale_preview_cache()
                    missions = self._vm.load_rc2_missions()
                    rc2_error = self._vm.consume_last_error()
                    preview_data: dict[str, bytes | None] = {}
                    for mission in missions:
                        path = self._vm.get_mission_preview_path(mission.guid)
                        data: bytes | None = None
                        if path and os.path.isfile(path):
                            try:
                                with open(path, "rb") as fh:
                                    data = fh.read()
                            except OSError:
                                data = None

                            lowered = os.path.normpath(path).lower()
                            if "djirc2kmzsync-previews" in lowered:
                                try:
                                    os.remove(path)
                                except OSError:
                                    pass

                        preview_data[mission.guid] = data

                    self._refresh_queue.put(("rc", serial, missions, rc2_error, preview_data))
                except Exception as exc:
                    self._refresh_queue.put(("error", serial, f"RC-2 refresh failed: {exc}"))

            pc_thread = threading.Thread(target=_load_pc, daemon=True)
            rc_thread = threading.Thread(target=_load_rc2, daemon=True)
            pc_thread.start()
            rc_thread.start()
            pc_thread.join()
            rc_thread.join()

            elapsed_ms = int((time.monotonic() - started) * 1000)
            self._refresh_queue.put(("done", serial, elapsed_ms))

        threading.Thread(target=_worker, daemon=True).start()
        self._root.after(50, self._drain_refresh_queue)

    def _drain_refresh_queue(self) -> None:
        had_items = False
        while True:
            try:
                payload = self._refresh_queue.get_nowait()
            except queue.Empty:
                break
            had_items = True

            kind = payload[0]
            serial = payload[1]
            if serial != self._refresh_serial:
                continue

            if kind == "pc":
                files, pc_error = payload[2], payload[3]
                self._populate_pc_tree(files, pc_error)
                self._refresh_pc_loaded = True
                if not self._refresh_rc_loaded:
                    self._set_status("PC files loaded. Loading RC-2 missions...")
                continue

            if kind == "rc":
                missions, rc2_error, preview_data = payload[2], payload[3], payload[4]
                self._populate_rc2_tree(missions, rc2_error, preview_data)
                self._refresh_rc_loaded = True
                if not self._refresh_pc_loaded:
                    self._set_status("RC-2 missions loaded. Loading PC files...")
                continue

            if kind == "error":
                worker_error = payload[2]
                self._refresh_active = False
                self._set_status("✘  Refresh failed.", colour=_ERROR)
                self._set_busy(False, "")
                self._log(f"Background refresh failed: {worker_error}", level="ERROR")
                continue

            if kind == "done":
                elapsed_ms = payload[2]
                self._refresh_active = False
                self._refresh_mapping()
                self._update_connection_mode()
                self._set_status("Ready.")
                self._set_busy(False, "")
                self._log(f"Refresh time: {elapsed_ms} ms", level="DEBUG")
                self._log("Lists refreshed.")

                if self._refresh_pending:
                    self._refresh_pending = False
                    self._root.after(10, self._refresh)

        if self._refresh_active and not had_items:
            self._root.after(50, self._drain_refresh_queue)
            return

        if self._refresh_active:
            self._root.after(50, self._drain_refresh_queue)

    def _populate_rc2_tree(
        self,
        missions: list[RC2Mission],
        last_error: str | None,
        preview_data: dict[str, bytes | None],
    ) -> None:
        self._last_rc2_missions = list(missions)
        self._last_preview_data = dict(preview_data)
        self._render_rc2_tree()

        count = len(missions)
        active_filter = self._rc2_filter_var.get().strip()
        if active_filter:
            shown = len(self._rc2_tree.get_children())
            self._set_status(f"Showing {shown} of {count} missions on RC-2.")
        else:
            self._set_status(f"{count} mission{'s' if count != 1 else ''} found on RC-2.")
        self._log(f"RC-2 missions loaded: {count}")
        if last_error:
            self._log(last_error, level="ERROR")

    def _render_rc2_tree(self) -> None:
        for row in self._rc2_tree.get_children():
            self._rc2_tree.delete(row)
        self._missions_by_guid = {}
        self._mission_preview_images = {}
        filter_text = self._rc2_filter_var.get().strip().lower()

        for m in self._last_rc2_missions:
            if filter_text:
                haystack = f"{m.guid} {m.display_kmz_name} {m.display_last_modified}".lower()
                if filter_text not in haystack:
                    continue

            self._missions_by_guid[m.guid] = m
            tag = "empty" if m.is_empty else ""
            preview_image = self._preview_image_for_mission(m.guid, self._last_preview_data.get(m.guid))
            slot_display = f"{m.display_kmz_name}\n{m.guid}\n{m.display_last_modified}"
            self._rc2_tree.insert("", tk.END,
                                  iid=m.guid,
                                  image=preview_image,
                                  values=(slot_display,),
                                  tags=(tag,))

    def _populate_pc_tree(self, files: list[KMZFile], last_error: str | None) -> None:
        for row in self._pc_tree.get_children():
            self._pc_tree.delete(row)
        self._pc_files_by_item = {}

        # Build folder hierarchy from relative file paths.
        folders: dict[str, str] = {}
        for f in files:
            parts = [part for part in f.filename.replace("\\", "/").split("/") if part]
            if not parts:
                continue

            parent_path = ""
            parent_id = ""
            for folder_name in parts[:-1]:
                parent_path = f"{parent_path}/{folder_name}" if parent_path else folder_name
                folder_id = folders.get(parent_path)
                if folder_id is None:
                    folder_id = self._pc_tree.insert(
                        parent_id if parent_id else "",
                        tk.END,
                        text=folder_name,
                        open=True,
                    )
                    folders[parent_path] = folder_id
                parent_id = folder_id

            kmz_name = parts[-1]
            item_id = self._pc_tree.insert(
                parent_id if parent_id else "",
                tk.END,
                values=(kmz_name,),
                tags=("file",),
            )
            self._pc_files_by_item[item_id] = f
        self._log(f"PC KMZ files loaded: {len(files)}")
        if last_error:
            self._log(last_error, level="ERROR")

    def _refresh_mapping(self) -> None:
        rows, updated_at, note = self._vm.get_copy_mapping_summary()
        self._mapping_rows = rows
        self._mapping_updated_var.set(f"Updated: {updated_at or 'n/a'}")
        self._mapping_note_var.set(note)

        for row_id in self._mapping_tree.get_children():
            self._mapping_tree.delete(row_id)

        for row in rows:
            self._mapping_tree.insert(
                "",
                tk.END,
                values=(
                    row.get("source_filename", ""),
                    row.get("target_mission_guid", ""),
                    row.get("target_kmz_filename", ""),
                    row.get("connection_mode", ""),
                    row.get("copied_at", ""),
                ),
            )

    # ------------------------------------------------------------------
    # Selection helpers — return model objects
    # ------------------------------------------------------------------
    def _selected_mission(self) -> RC2Mission | None:
        sel = self._rc2_tree.selection()
        if not sel:
            return None
        guid = str(sel[0])
        mission = self._missions_by_guid.get(guid)
        if mission is not None:
            return mission
        folder_path = os.path.join(self._vm.rc2_folder, guid)
        return RC2Mission(guid=guid, kmz_name="", full_folder_path=folder_path)

    def _selected_kmz(self) -> KMZFile | None:
        sel = self._pc_tree.selection()
        if not sel:
            return None
        item_id = sel[0]
        mapped = self._pc_files_by_item.get(item_id)
        if mapped is not None:
            return mapped

        item = self._pc_tree.item(item_id)
        values = item.get("values", ())

        # Only allow selection of file nodes (which have values)
        if not values:
            return None

        filename = values[0]
        full_path = os.path.join(self._vm.pc_folder, filename)
        return KMZFile(filename=filename, full_path=full_path)

    # ------------------------------------------------------------------
    # Status bar
    # ------------------------------------------------------------------
    def _set_status(self, text: str, colour: str = _TEXT_DIM) -> None:
        self._status_var.set(text)
        self._status_label.configure(fg=colour)

    def _update_connection_mode(self) -> None:
        mode = self._vm.get_rc2_connection_mode()
        colours = {
            "MTP": (_MODE_BG, _MODE_FG),
            "ADB": ("#eff6ff", "#1d4ed8"),
            "Filesystem": ("#ecfdf5", "#166534"),
            "Unavailable": ("#fef2f2", "#b91c1c"),
            "Not Set": ("#f3f4f6", _TEXT_DIM),
        }
        bg, fg = colours.get(mode, ("#f3f4f6", _TEXT_DIM))
        self._mode_var.set(f"Mode: {mode}")
        self._mode_label.configure(bg=bg, fg=fg)

    def _set_busy(self, busy: bool, message: str) -> None:
        self._busy = busy
        self._busy_var.set(message if busy else "")

    def _preview_image_for_mission(self, guid: str, image_data: bytes | None) -> tk.PhotoImage:
        if not image_data:
            self._log(f"No preview bytes for mission {guid}, using placeholder.", level="DEBUG")
            return self._preview_placeholder_image()

        # Tk PhotoImage can fail for JPEG depending on Tcl/Tk build; Pillow
        # gives consistent decoding for RC-2 preview images.
        if Image is not None and ImageTk is not None:
            try:
                with Image.open(io.BytesIO(image_data)) as opened:
                    rendered = opened.convert("RGB")
                    rendered.thumbnail((80, 60))
                    image = ImageTk.PhotoImage(rendered)
                self._mission_preview_images[guid] = image
                self._log(f"Successfully loaded image for mission: {guid}", level="DEBUG")
                return image
            except Exception as e:
                self._log(f"Failed to decode preview bytes for {guid}: {e}", level="ERROR")
                return self._preview_placeholder_image()

        return self._preview_placeholder_image()

    def _preview_placeholder_image(self) -> tk.PhotoImage:
        if self._preview_placeholder is None:
            image = tk.PhotoImage(width=80, height=60)
            image.put("#e5e7eb", to=(0, 0, 80, 60))
            image.put("#cbd5e1", to=(0, 0, 80, 1))
            image.put("#cbd5e1", to=(0, 59, 80, 60))
            image.put("#cbd5e1", to=(0, 0, 1, 60))
            image.put("#cbd5e1", to=(79, 0, 80, 60))
            self._preview_placeholder = image
        return self._preview_placeholder

    def _log(self, message: str, level: str = "INFO") -> None:
        if level == "ERROR":
            key = message.strip()
            now = time.monotonic()
            if key == self._last_error_log_key and (now - self._last_error_log_time) < 2.0:
                return
            self._last_error_log_key = key
            self._last_error_log_time = now

        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] [{level}] {message}\n"
        self._log_text.configure(state=tk.NORMAL)
        self._log_text.insert(tk.END, line)
        self._log_text.yview_moveto(1.0)
        self._log_text.see(tk.END)
        self._log_text.configure(state=tk.DISABLED)

    def _clear_log(self) -> None:
        self._log_text.configure(state=tk.NORMAL)
        self._log_text.delete("1.0", tk.END)
        self._log_text.configure(state=tk.DISABLED)
        self._log("Log cleared.")

    def _focus_log(self, _event) -> None:
        self._log_text.focus_set()

    def _select_all_log(self, _event):
        self._log_text.tag_add(tk.SEL, "1.0", tk.END)
        self._log_text.mark_set(tk.INSERT, "1.0")
        self._log_text.see(tk.INSERT)
        return "break"

    def _copy_selected_log(self, _event):
        try:
            selected = self._log_text.get(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            self._set_status("No log selection to copy.", colour=_TEXT_DIM)
            return "break"

        self._root.clipboard_clear()
        self._root.clipboard_append(selected)
        self._root.update()
        self._set_status("Selected log copied to clipboard.", colour=_SUCCESS)
        self._log("Selected log copied to clipboard.", level="OK")
        return "break"

    def _handle_callback_exception(self, exc_type, exc_value, exc_tb) -> None:
        summary = f"Unhandled UI exception: {exc_value}"
        self._set_status("✘  Unhandled UI exception occurred.", colour=_ERROR)
        self._log(summary, level="ERROR")
        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb)).strip()
        if tb_text:
            self._log(tb_text, level="ERROR")
