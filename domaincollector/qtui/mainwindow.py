"""The Domain Collector desktop window.

The engine keeps running in its own thread (:class:`CollectorThread`); this
window drains its event queue from a ``QTimer`` on the GUI thread, so no Qt
object is ever touched from the collector thread.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Dict, List, Optional

from PySide6.QtCore import QSettings, QSize, Qt, QTimer, Slot
from PySide6.QtGui import QColor, QCloseEvent, QDesktopServices, QTextCharFormat, QTextCursor
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QSystemTrayIcon,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..config import Config, ConfigError, DEFAULT_CONFIG_PATH
from ..engine import Event
from ..runner import CollectorThread
from ..sources import SOURCE_NAMES
from .icons import app_icon, make_icon
from .models import (
    DomainFilterProxy,
    DomainTableModel,
    TechnologyFilterProxy,
    TechnologyTableModel,
)
from .theme import Palette, apply_qpalette, palette_for, stylesheet
from .widgets import Card, NavButton, SearchBox, ShareBarDelegate, StatCard, StatusDotDelegate, StatusPill

POLL_MS = 250
MAX_LOG_BLOCKS = 3000


class MainWindow(QMainWindow):
    def __init__(self, config: Config, config_path: str = DEFAULT_CONFIG_PATH,
                 theme: str = "system") -> None:
        super().__init__()
        self.config = config
        self.config_path = config_path
        self.theme_name = theme
        self.palette_ = palette_for(theme)
        self.collector = CollectorThread(config)
        self._state = "stopped"
        self._tray: Optional[QSystemTrayIcon] = None

        self.setWindowTitle("Domain Collector")
        self.setMinimumSize(QSize(1040, 660))
        self.resize(1240, 780)

        self.domain_model = DomainTableModel()
        self.domain_proxy = DomainFilterProxy(self)
        self.domain_proxy.setSourceModel(self.domain_model)
        self.tech_model = TechnologyTableModel()
        self.tech_proxy = TechnologyFilterProxy(self)
        self.tech_proxy.setSourceModel(self.tech_model)

        self._build_ui()
        self._build_tray()
        self.apply_theme(self.palette_)
        self._restore_geometry()
        self._set_state("stopped")

        self.timer = QTimer(self)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self._pump)
        self.timer.start()

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_sidebar())

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(22, 18, 22, 14)
        right_layout.setSpacing(14)
        right_layout.addLayout(self._build_header())

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_dashboard_page())
        self.pages.addWidget(self._build_domains_page())
        self.pages.addWidget(self._build_tech_page())
        self.pages.addWidget(self._build_log_page())
        self.pages.addWidget(self._build_settings_page())
        right_layout.addWidget(self.pages, 1)
        outer.addWidget(right, 1)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status_left = QLabel("Ready")
        self.status_right = QLabel("")
        self.status.addWidget(self.status_left, 1)
        self.status.addPermanentWidget(self.status_right)

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(232)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 16, 12, 12)
        layout.setSpacing(4)

        brand = QHBoxLayout()
        brand.setSpacing(10)
        self.brand_icon = QLabel()
        self.brand_icon.setFixedSize(34, 34)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel("Domain Collector")
        title.setObjectName("appTitle")
        subtitle = QLabel(f"v{__version__}")
        subtitle.setObjectName("appSubtitle")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        brand.addWidget(self.brand_icon)
        brand.addLayout(titles)
        brand.addStretch(1)
        layout.addLayout(brand)
        layout.addSpacing(18)

        self.nav_buttons: List[NavButton] = []
        for index, (label, icon_name) in enumerate(
            [("Dashboard", "dashboard"), ("Domains", "domains"),
             ("Technologies", "tech"), ("Activity log", "log"), ("Settings", "settings")]
        ):
            button = NavButton(label, icon_name)
            button.clicked.connect(lambda _checked=False, i=index: self._select_page(i))
            layout.addWidget(button)
            self.nav_buttons.append(button)
        self.nav_buttons[0].setChecked(True)

        layout.addStretch(1)
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["Follow system", "Light", "Dark"])
        self.theme_combo.setCurrentIndex({"system": 0, "light": 1, "dark": 2}.get(self.theme_name, 0))
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        layout.addWidget(QLabel("Appearance"))
        layout.addWidget(self.theme_combo)
        return sidebar

    def _build_header(self) -> QHBoxLayout:
        header = QHBoxLayout()
        header.setSpacing(10)

        titles = QVBoxLayout()
        titles.setSpacing(1)
        self.page_title = QLabel("Dashboard")
        self.page_title.setObjectName("pageTitle")
        self.page_subtitle = QLabel("Live discovery and technology fingerprinting")
        self.page_subtitle.setObjectName("pageSubtitle")
        titles.addWidget(self.page_title)
        titles.addWidget(self.page_subtitle)
        header.addLayout(titles)
        header.addStretch(1)

        self.status_pill = StatusPill()
        header.addWidget(self.status_pill)

        self.btn_start = QPushButton("  Start")
        self.btn_start.setObjectName("primary")
        self.btn_start.clicked.connect(self.on_start)
        self.btn_pause = QPushButton("  Pause")
        self.btn_pause.clicked.connect(self.on_pause_resume)
        self.btn_stop = QPushButton("  Stop")
        self.btn_stop.setObjectName("danger")
        self.btn_stop.clicked.connect(self.on_stop)
        for button in (self.btn_start, self.btn_pause, self.btn_stop):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setIconSize(QSize(15, 15))
            header.addWidget(button)
        return header

    def _build_dashboard_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        grid = QGridLayout()
        grid.setSpacing(12)
        self.cards: Dict[str, StatCard] = {}
        definitions = [
            ("processed", "Probed", "domains checked"),
            ("responsive", "Live", "answered a request"),
            ("unreachable", "Unreachable", "no usable response"),
            ("new", "New", "stored this run"),
            ("rechecked", "Re-checked", "refreshed rows"),
            ("queued", "Queue", "waiting to probe"),
        ]
        for column, (key, label, hint) in enumerate(definitions):
            card = StatCard(label, hint)
            grid.addWidget(card, 0, column)
            self.cards[key] = card
        layout.addLayout(grid)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        recent_card = Card("Recent domains")
        self.recent_table = self._make_domain_table(compact=True)
        recent_card.add_widget(self.recent_table, 1)
        splitter.addWidget(recent_card)

        tech_card = Card("Top technologies")
        self.dash_tech_table = self._make_tech_table()
        tech_card.add_widget(self.dash_tech_table, 1)
        splitter.addWidget(tech_card)
        splitter.setSizes([680, 420])
        layout.addWidget(splitter, 1)
        return page

    def _build_domains_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        card = Card("Probed domains")
        self.domain_search = SearchBox("Filter domains or technologies…", self.palette_.text_muted)
        self.domain_search.setFixedWidth(280)
        self.domain_search.textChanged.connect(self._apply_domain_filter)
        card.add_header_widget(self.domain_search)
        self.only_live = QCheckBox("Live only")
        self.only_live.toggled.connect(self._apply_domain_filter)
        card.add_header_widget(self.only_live)
        export_button = QPushButton("  Export CSV")
        export_button.clicked.connect(self.export_domains)
        self.btn_export_domains = export_button
        card.add_header_widget(export_button)

        self.domain_table = self._make_domain_table(compact=False)
        card.add_widget(self.domain_table, 1)
        layout.addWidget(card, 1)
        return page

    def _build_tech_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        card = Card("Detected technologies")
        self.tech_search = SearchBox("Filter technologies…", self.palette_.text_muted)
        self.tech_search.setFixedWidth(240)
        self.tech_search.textChanged.connect(self._apply_tech_filter)
        card.add_header_widget(self.tech_search)
        open_output = QPushButton("  Open output folder")
        open_output.clicked.connect(self.open_output_dir)
        self.btn_open_output = open_output
        card.add_header_widget(open_output)
        self.tech_table = self._make_tech_table()
        card.add_widget(self.tech_table, 1)
        layout.addWidget(card, 1)
        return page

    def _build_log_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        card = Card("Activity log")
        save_button = QPushButton("  Save log")
        save_button.clicked.connect(self.export_log)
        self.btn_save_log = save_button
        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(lambda: self.log_view.clear())
        card.add_header_widget(save_button)
        card.add_header_widget(clear_button)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("log")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(MAX_LOG_BLOCKS)
        card.add_widget(self.log_view, 1)
        layout.addWidget(card, 1)
        return page

    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)

        columns = QHBoxLayout()
        columns.setSpacing(12)
        columns.setAlignment(Qt.AlignmentFlag.AlignTop)

        collection = QGroupBox("Collection")
        form = QFormLayout(collection)
        form.setSpacing(8)
        self.in_concurrency = QSpinBox()
        self.in_concurrency.setRange(1, 500)
        self.in_timeout = QDoubleSpinBox()
        self.in_timeout.setRange(1, 300)
        self.in_timeout.setDecimals(1)
        self.in_timeout.setSuffix(" s")
        self.in_interval = QSpinBox()
        self.in_interval.setRange(10, 86400)
        self.in_interval.setSuffix(" s")
        self.in_limit = QSpinBox()
        self.in_limit.setRange(1, 1_000_000)
        self.in_queue = QSpinBox()
        self.in_queue.setRange(10, 1_000_000)
        form.addRow("Concurrency", self.in_concurrency)
        form.addRow("HTTP timeout", self.in_timeout)
        form.addRow("Fetch interval", self.in_interval)
        form.addRow("Domains per cycle", self.in_limit)
        form.addRow("Max queue size", self.in_queue)
        columns.addWidget(collection, 1)

        sources = QGroupBox("Sources")
        sources_layout = QVBoxLayout(sources)
        sources_layout.setSpacing(6)
        self.source_checks: Dict[str, QCheckBox] = {}
        for name in SOURCE_NAMES:
            if name == "file":
                continue
            box = QCheckBox(name)
            sources_layout.addWidget(box)
            self.source_checks[name] = box
        sources_layout.addSpacing(6)
        sources_layout.addWidget(QLabel("Certstream URL"))
        self.in_certstream = QLineEdit()
        sources_layout.addWidget(self.in_certstream)
        sources_layout.addWidget(QLabel("Seed file (optional)"))
        seed_row = QHBoxLayout()
        self.in_seed = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_seed)
        seed_row.addWidget(self.in_seed, 1)
        seed_row.addWidget(browse)
        sources_layout.addLayout(seed_row)
        sources_layout.addStretch(1)
        columns.addWidget(sources, 1)

        behaviour = QGroupBox("Storage and re-checks")
        behaviour_form = QFormLayout(behaviour)
        behaviour_form.setSpacing(8)
        self.in_recheck = QSpinBox()
        self.in_recheck.setRange(0, 31_536_000)
        self.in_recheck.setSuffix(" s")
        self.in_recheck.setSpecialValueText("Disabled")
        self.in_recheck_batch = QSpinBox()
        self.in_recheck_batch.setRange(1, 100_000)
        self.chk_recheck_only = QCheckBox("Re-check only (no new discovery)")
        self.chk_tech_files = QCheckBox("Write output/<technology>.txt")
        self.chk_unresponsive = QCheckBox("Store unresponsive domains")
        self.chk_verify_ssl = QCheckBox("Verify TLS certificates")
        self.in_db = QLineEdit()
        self.in_output = QLineEdit()
        behaviour_form.addRow("Re-check after", self.in_recheck)
        behaviour_form.addRow("Re-checks per cycle", self.in_recheck_batch)
        behaviour_form.addRow("", self.chk_recheck_only)
        behaviour_form.addRow("Database", self.in_db)
        behaviour_form.addRow("Output folder", self.in_output)
        behaviour_form.addRow("", self.chk_tech_files)
        behaviour_form.addRow("", self.chk_unresponsive)
        behaviour_form.addRow("", self.chk_verify_ssl)
        for box in (collection, sources, behaviour):
            box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        columns.addWidget(behaviour, 1)
        columns.setAlignment(collection, Qt.AlignmentFlag.AlignTop)
        columns.setAlignment(sources, Qt.AlignmentFlag.AlignTop)
        columns.setAlignment(behaviour, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(columns)
        outer.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.settings_note = QLabel("Concurrency and sources apply the next time you press Start.")
        self.settings_note.setObjectName("pageSubtitle")
        buttons.addWidget(self.settings_note)
        revert = QPushButton("Revert")
        revert.clicked.connect(self._load_settings_form)
        save = QPushButton("Save settings")
        save.setObjectName("primary")
        save.clicked.connect(self._save_settings_form)
        buttons.addWidget(revert)
        buttons.addWidget(save)
        outer.addLayout(buttons)

        self._load_settings_form()
        return page

    # ---------------------------------------------------------------- tables
    def _make_domain_table(self, compact: bool) -> QTableView:
        table = QTableView()
        table.setModel(self.domain_proxy)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(30)
        table.setShowGrid(False)
        table.setWordWrap(False)
        header = table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        table.setColumnWidth(0, 240)
        table.setColumnWidth(1, 90)
        table.setColumnWidth(2, 70)
        table.setColumnWidth(3, 110)
        if compact:
            table.setColumnHidden(3, True)
        delegate = StatusDotDelegate(DomainTableModel.ROLE_RESPONSIVE,
                                     self.palette_.success, self.palette_.text_faint, table)
        table.setItemDelegateForColumn(1, delegate)
        table.status_delegate = delegate  # keep a reference for retinting
        return table

    def _make_tech_table(self) -> QTableView:
        table = QTableView()
        table.setModel(self.tech_proxy)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(30)
        table.setShowGrid(False)
        header = table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(1, 80)
        table.setColumnWidth(2, 110)
        delegate = ShareBarDelegate(TechnologyTableModel.ROLE_SHARE,
                                    self.palette_.accent, self.palette_.surface_hover, table)
        table.setItemDelegateForColumn(2, delegate)
        table.share_delegate = delegate
        return table

    # ----------------------------------------------------------------- theme
    def apply_theme(self, palette: Palette) -> None:
        self.palette_ = palette
        app = QApplication.instance()
        if app is not None:
            apply_qpalette(app, palette)
            app.setStyleSheet(stylesheet(palette))
        icon = app_icon(palette.accent_text if palette.name == "light" else "#0b2739",
                        palette.accent)
        self.setWindowIcon(icon)
        self.brand_icon.setPixmap(icon.pixmap(30, 30))
        if self._tray is not None:
            self._tray.setIcon(icon)
        for button in self.nav_buttons:
            button.retint(palette.text_muted)
        self.btn_start.setIcon(make_icon("play", palette.accent_text, 15))
        self.btn_pause.setIcon(make_icon("pause", palette.text, 15))
        self.btn_stop.setIcon(make_icon("stop", palette.text, 15))
        self.btn_export_domains.setIcon(make_icon("export", palette.text, 15))
        self.btn_save_log.setIcon(make_icon("export", palette.text, 15))
        self.btn_open_output.setIcon(make_icon("refresh", palette.text, 15))
        self.status_pill.set_palette_colors(palette)
        for table in (self.recent_table, self.domain_table):
            table.status_delegate.set_colors(palette.success, palette.text_faint)
        for table in (self.dash_tech_table, self.tech_table):
            table.share_delegate.set_colors(palette.accent, palette.surface_hover)
        self.log_view.viewport().update()

    @Slot(int)
    def _on_theme_changed(self, index: int) -> None:
        self.theme_name = {0: "system", 1: "light", 2: "dark"}.get(index, "system")
        self.apply_theme(palette_for(self.theme_name))
        QSettings("DomainCollector", "DomainCollector").setValue("theme", self.theme_name)

    # ------------------------------------------------------------------ tray
    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray = QSystemTrayIcon(self)
        self._tray.setToolTip("Domain Collector")
        self._tray.activated.connect(lambda _reason: self.showNormal())
        self._tray.show()

    def notify(self, title: str, message: str) -> None:
        if self._tray is not None and QSystemTrayIcon.supportsMessages():
            self._tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 4000)

    # --------------------------------------------------------------- actions
    def _select_page(self, index: int) -> None:
        for position, button in enumerate(self.nav_buttons):
            button.setChecked(position == index)
        self.pages.setCurrentIndex(index)
        titles = [
            ("Dashboard", "Live discovery and technology fingerprinting"),
            ("Domains", "Every domain probed in this session"),
            ("Technologies", "What the live domains are built with"),
            ("Activity log", "Everything the collector reported"),
            ("Settings", "Applied to this run and saved to your config file"),
        ]
        self.page_title.setText(titles[index][0])
        self.page_subtitle.setText(titles[index][1])

    def on_start(self) -> None:
        if self.collector.running:
            return
        self.collector.update_config(self.config)
        self.collector.start()
        if self.collector.error is not None:
            QMessageBox.critical(self, "Cannot start", str(self.collector.error))
            self._set_state("stopped")
            return
        self._set_state("running")

    def on_pause_resume(self) -> None:
        if not self.collector.running:
            return
        if self._state == "running":
            self.collector.pause()
            self._set_state("paused")
        elif self._state == "paused":
            self.collector.resume()
            self._set_state("running")

    def on_stop(self) -> None:
        if not self.collector.running:
            return
        self._set_state("stopping")
        self.log("Stopping - waiting for in-flight probes to finish…", "WARN")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            stopped = self.collector.stop(timeout=25)
        finally:
            QApplication.restoreOverrideCursor()
        if not stopped:
            self.log("Collector did not stop in time; it will exit in the background.", "ERROR")
        self._set_state("stopped")

    def export_domains(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export domains", f"domains-{datetime.now():%Y%m%d-%H%M%S}.csv",
            "CSV files (*.csv);;All files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write("domain,responsive,status,source,technologies\n")
                for row in self._visible_domain_rows():
                    technologies = " ".join(row["technologies"])
                    handle.write(
                        f"{row['domain']},{int(row['responsive'])},"
                        f"{row['status'] if row['status'] is not None else ''},"
                        f"{row['source']},{technologies}\n"
                    )
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        self.status_left.setText(f"Exported {self.domain_model.rowCount()} domains to {path}")

    def _visible_domain_rows(self) -> List[dict]:
        """Rows currently shown, in view order (so exports match the filter)."""
        rows = self.domain_model.rows()
        visible = []
        for proxy_row in range(self.domain_proxy.rowCount()):
            source_row = self.domain_proxy.mapToSource(self.domain_proxy.index(proxy_row, 0)).row()
            if 0 <= source_row < len(rows):
                visible.append(rows[source_row])
        return visible

    def export_log(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save activity log", f"domain-collector-{datetime.now():%Y%m%d-%H%M%S}.log",
            "Log files (*.log);;Text files (*.txt);;All files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.log_view.toPlainText())
        except OSError as exc:
            QMessageBox.warning(self, "Save failed", str(exc))
            return
        self.status_left.setText(f"Log saved to {path}")

    def open_output_dir(self) -> None:
        path = os.path.abspath(self.config.output_dir)
        os.makedirs(path, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _browse_seed(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose a seed file", "",
                                              "Text files (*.txt);;All files (*)")
        if path:
            self.in_seed.setText(path)

    # -------------------------------------------------------------- settings
    def _load_settings_form(self) -> None:
        config = self.config
        self.in_concurrency.setValue(config.concurrency)
        self.in_timeout.setValue(config.http_timeout)
        self.in_interval.setValue(config.fetch_interval)
        self.in_limit.setValue(config.max_domains_per_cycle)
        self.in_queue.setValue(config.max_queue_size)
        self.in_recheck.setValue(config.recheck_after)
        self.in_recheck_batch.setValue(config.recheck_batch)
        self.chk_recheck_only.setChecked(config.recheck_only)
        self.chk_tech_files.setChecked(config.write_tech_files)
        self.chk_unresponsive.setChecked(config.store_unresponsive)
        self.chk_verify_ssl.setChecked(config.verify_ssl)
        self.in_db.setText(config.db_path)
        self.in_output.setText(config.output_dir)
        self.in_certstream.setText(config.certstream_url)
        self.in_seed.setText(config.seed_file or "")
        for name, box in self.source_checks.items():
            box.setChecked(name in config.sources)

    def _save_settings_form(self) -> None:
        candidate = Config(**self.config.to_dict())
        candidate.concurrency = self.in_concurrency.value()
        candidate.http_timeout = self.in_timeout.value()
        candidate.fetch_interval = self.in_interval.value()
        candidate.max_domains_per_cycle = self.in_limit.value()
        candidate.max_queue_size = self.in_queue.value()
        candidate.recheck_after = self.in_recheck.value()
        candidate.recheck_batch = self.in_recheck_batch.value()
        candidate.recheck_only = self.chk_recheck_only.isChecked()
        candidate.write_tech_files = self.chk_tech_files.isChecked()
        candidate.store_unresponsive = self.chk_unresponsive.isChecked()
        candidate.verify_ssl = self.chk_verify_ssl.isChecked()
        candidate.db_path = self.in_db.text().strip()
        candidate.output_dir = self.in_output.text().strip()
        candidate.certstream_url = self.in_certstream.text().strip()
        candidate.seed_file = self.in_seed.text().strip() or None
        chosen = [name for name, box in self.source_checks.items() if box.isChecked()]
        if candidate.seed_file:
            chosen.append("file")
        candidate.sources = chosen

        try:
            candidate.validate()
        except ConfigError as exc:
            QMessageBox.warning(self, "Invalid settings", str(exc))
            return

        self.config = candidate
        self.collector.update_config(candidate)
        try:
            candidate.save(self.config_path)
            self.log(f"Settings saved to {self.config_path}", "GOOD")
            self.status_left.setText(f"Settings saved to {self.config_path}")
        except ConfigError as exc:
            QMessageBox.warning(self, "Settings not saved", str(exc))

    # ------------------------------------------------------------------ pump
    def _pump(self) -> None:
        try:
            events = self.collector.drain_events()
            if events:
                self._handle_events(events)
            self._refresh_stats()
            if self._state in ("running", "paused", "stopping") and not self.collector.running:
                self._set_state("stopped")
        except Exception as exc:  # pragma: no cover - the UI must never die
            self.log(f"UI error: {type(exc).__name__}: {exc}", "ERROR")

    def _handle_events(self, events: List[Event]) -> None:
        self.domain_model.add_events(events)
        for event in events:
            if event.kind == "state":
                state = event.data.get("state")
                if state in ("running", "paused", "stopped"):
                    self._set_state(state)
                if event.message:
                    self.log(event.message, event.level)
            elif event.kind in ("log", "cycle", "domain"):
                self.log(event.message, event.level)

    def _refresh_stats(self) -> None:
        stats = self.collector.stats
        if stats is None:
            return
        values = {
            "processed": stats.processed,
            "responsive": stats.responsive,
            "unreachable": stats.unreachable,
            "new": stats.new,
            "rechecked": stats.rechecked,
            "queued": stats.queued,
        }
        for key, value in values.items():
            self.cards[key].set_value(value)
        if stats.processed:
            share = 100.0 * stats.responsive / stats.processed
            self.cards["responsive"].set_hint(f"{share:.0f}% of probed")
        self.tech_model.set_counts(dict(stats.tech_counts))
        rate = f"{stats.rate:.1f} domains/s" if stats.elapsed >= 1.0 else "measuring…"
        self.status_right.setText(
            f"{rate}   ·   cycle {stats.cycles}   ·   queue {stats.queued}"
        )

    def _set_state(self, state: str) -> None:
        self._state = state
        self.status_pill.set_state(state)
        running = state in ("running", "paused")
        self.btn_start.setEnabled(not running and state != "stopping")
        self.btn_pause.setEnabled(running)
        self.btn_stop.setEnabled(running)
        self.btn_pause.setText("  Resume" if state == "paused" else "  Pause")
        self.btn_pause.setIcon(
            make_icon("play" if state == "paused" else "pause", self.palette_.text, 15))
        self.status_left.setText(
            {"running": "Collecting…", "paused": "Paused", "stopping": "Stopping…",
             "stopped": "Ready"}.get(state, "Ready")
        )

    # ------------------------------------------------------------------- log
    def log(self, message: str, level: str = "INFO") -> None:
        if not message:
            return
        colors = {
            "INFO": self.palette_.text_muted,
            "GOOD": self.palette_.success,
            "WARN": self.palette_.warning,
            "ERROR": self.palette_.danger,
        }
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(colors.get(level, self.palette_.text)))
        cursor.insertText(f"{datetime.now():%H:%M:%S}  {message}\n", fmt)
        scrollbar = self.log_view.verticalScrollBar()
        if scrollbar.value() >= scrollbar.maximum() - 40:
            scrollbar.setValue(scrollbar.maximum())

    # ---------------------------------------------------------------- filter
    def _apply_domain_filter(self) -> None:
        self.domain_proxy.set_needle(self.domain_search.text())
        self.domain_proxy.set_live_only(self.only_live.isChecked())

    def _apply_tech_filter(self) -> None:
        self.tech_proxy.set_needle(self.tech_search.text())

    # -------------------------------------------------------------- geometry
    def _restore_geometry(self) -> None:
        settings = QSettings("DomainCollector", "DomainCollector")
        geometry = settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.collector.running:
            answer = QMessageBox.question(
                self, "Quit Domain Collector",
                "Collection is still running. Stop it and quit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.collector.stop(timeout=25)
        settings = QSettings("DomainCollector", "DomainCollector")
        settings.setValue("geometry", self.saveGeometry())
        settings.setValue("theme", self.theme_name)
        self.timer.stop()
        if self._tray is not None:
            self._tray.hide()
        event.accept()
