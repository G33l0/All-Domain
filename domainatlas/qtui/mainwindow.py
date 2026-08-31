"""The Domain Atlas desktop window.

The engine keeps running in its own thread (:class:`CollectorThread`); this
window drains its event queue from a ``QTimer`` on the GUI thread, so no Qt
object is ever touched from the collector thread.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Dict, List, Optional

from PySide6.QtCore import QSettings, QSize, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QCloseEvent, QDesktopServices, QTextCharFormat, QTextCursor
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
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
from ..query import ORDER_NEWEST, DomainFilter
from ..runner import CollectorThread
from ..sources import available_sources
from .browser import PAGE_SIZE, ExportTask, StoredDomainModel, start_browser
from .icons import make_icon
from .logo import logo_icon
from .models import (
    DomainFilterProxy,
    DomainTableModel,
    TechnologyFilterProxy,
    TechnologyTableModel,
)
from .theme import Palette, apply_qpalette, palette_for, stylesheet, theme_labels
from .widgets import (
    Card,
    ElidingLabel,
    NavButton,
    ResponsiveGrid,
    SearchBox,
    ShareBarDelegate,
    StatCard,
    StatusDotDelegate,
    StatusPill,
)

POLL_MS = 250
MAX_LOG_BLOCKS = 3000

#: Indices of the pages inside the stacked widget, in nav order.
PAGE_DASHBOARD, PAGE_DOMAINS, PAGE_TECH, PAGE_LOG, PAGE_SETTINGS = range(5)

#: Header title and subtitle per page. Subtitles are kept short enough to read
#: in full at the smallest supported window width rather than being elided.
PAGE_HEADINGS = (
    ("Dashboard", "Live discovery and fingerprinting"),
    ("Domains", "Every domain stored, searchable"),
    ("Technologies", "What the live domains run on"),
    ("Activity log", "Everything the collector reported"),
    ("Settings", "Applied now, saved to your config"),
)


class MainWindow(QMainWindow):
    open_database = Signal(str)
    request_facets = Signal()
    request_summary = Signal()

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

        self.setWindowTitle("Domain Atlas")
        # A 1920x1080 display at 200% scaling exposes only 960x540 logical
        # pixels, and 225% leaves 853x480, so the floor stays below both.
        self.setMinimumSize(QSize(820, 460))
        self.resize(1240, 780)

        self.domain_model = DomainTableModel()
        self.domain_proxy = DomainFilterProxy(self)
        self.domain_proxy.setSourceModel(self.domain_model)
        self.tech_model = TechnologyTableModel()
        self.tech_proxy = TechnologyFilterProxy(self)
        self.tech_proxy.setSourceModel(self.tech_model)
        self.stored_model = StoredDomainModel(self)

        self._build_ui()
        self._connect_browser()
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
        self.status_database = QLabel("")
        self.status_right = QLabel("")
        self.status.addWidget(self.status_left, 1)
        self.status.addPermanentWidget(self.status_database)
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
        title = QLabel("Domain Atlas")
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
        self._theme_keys = ["system"] + [key for key, _label in theme_labels()]
        self.theme_combo.addItems(["Follow system"] + [label for _key, label in theme_labels()])
        if self.theme_name in self._theme_keys:
            self.theme_combo.setCurrentIndex(self._theme_keys.index(self.theme_name))
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
        self.page_subtitle = ElidingLabel(PAGE_HEADINGS[PAGE_DASHBOARD][1])
        self.page_subtitle.setObjectName("pageSubtitle")
        titles.addWidget(self.page_title)
        titles.addWidget(self.page_subtitle)
        # The title block takes the free width. Without a stretch factor the
        # column is only as wide as the page title, and the longer subtitle
        # underneath it is elided even on a wide window.
        header.addLayout(titles, 1)
        header.addSpacing(10)

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

        cards_area = ResponsiveGrid(min_item_width=150)
        self.cards: Dict[str, StatCard] = {}
        definitions = [
            ("processed", "Probed", "checked"),
            ("responsive", "Live", "answered"),
            ("unreachable", "Down", "no response"),
            ("new", "New", "this run"),
            ("rechecked", "Re-checked", "refreshed"),
            ("discovered", "Self-found", "from responses"),
            ("queued", "Queue", "waiting"),
        ]
        for key, label, hint in definitions:
            card = StatCard(label, hint)
            card.setMinimumWidth(150)
            cards_area.add(card)
            self.cards[key] = card
        layout.addWidget(cards_area)

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

        card = Card("Stored domains")

        self.btn_refresh = QPushButton("  Refresh")
        self.btn_refresh.clicked.connect(self.reload_stored)
        card.add_header_widget(self.btn_refresh)
        self.btn_export_domains = QPushButton("  Export")
        self.btn_export_domains.clicked.connect(self.export_domains)
        card.add_header_widget(self.btn_export_domains)

        self.domain_search = SearchBox("Search domains…", self.palette_.text_muted)
        self.domain_search.setMinimumWidth(150)
        self.domain_search.textChanged.connect(self._schedule_reload)
        card.add_widget(self.domain_search)

        # A single fixed row of filters would set a floor on the window width,
        # so they reflow into as many columns as there is room for.
        filters = ResponsiveGrid(min_item_width=130, spacing=8)

        self.filter_technology = QComboBox()
        self.filter_technology.addItem("Any technology", None)
        self.filter_technology.currentIndexChanged.connect(self._schedule_reload)
        filters.add(self.filter_technology)

        self.filter_source = QComboBox()
        self.filter_source.addItem("Any source", None)
        self.filter_source.currentIndexChanged.connect(self._schedule_reload)
        filters.add(self.filter_source)

        self.filter_status = QComboBox()
        for label, value in (("Any status", None), ("Live only", True), ("Down only", False)):
            self.filter_status.addItem(label, value)
        self.filter_status.currentIndexChanged.connect(self._schedule_reload)
        filters.add(self.filter_status)

        self.filter_network = QComboBox()
        for label, value in (("All networks", None), ("Clear web", False), ("Tor (.onion)", True)):
            self.filter_network.addItem(label, value)
        self.filter_network.currentIndexChanged.connect(self._schedule_reload)
        filters.add(self.filter_network)

        self.filter_grouping = QComboBox()
        for label, value in (("One row per site", True), ("Every host name", False)):
            self.filter_grouping.addItem(label, value)
        self.filter_grouping.setToolTip(
            "Grouped, example.com and www.example.com are one result.\n"
            "Ungrouped, every host name observed is listed separately."
        )
        self.filter_grouping.currentIndexChanged.connect(self._schedule_reload)
        filters.add(self.filter_grouping)

        self.btn_clear_filters = QPushButton("Clear filters")
        self.btn_clear_filters.clicked.connect(self.clear_filters)
        filters.add(self.btn_clear_filters)

        card.add_widget(filters)

        self.stored_table = self._make_stored_table()
        card.add_widget(self.stored_table, 1)

        self.result_label = ElidingLabel("No database opened yet.")
        self.result_label.setObjectName("pageSubtitle")
        card.add_widget(self.result_label)

        layout.addWidget(card, 1)
        return page

    def clear_filters(self) -> None:
        for combo in (self.filter_technology, self.filter_source,
                      self.filter_status, self.filter_network, self.filter_grouping):
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
        self.domain_search.blockSignals(True)
        self.domain_search.clear()
        self.domain_search.blockSignals(False)
        self.reload_stored()

    def _build_tech_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        card = Card("Detected technologies")
        self.tech_search = SearchBox("Filter technologies…", self.palette_.text_muted)
        self.tech_search.setMinimumWidth(150)
        self.tech_search.textChanged.connect(self._apply_tech_filter)
        card.add_header_widget(self.tech_search)
        open_output = QPushButton("  Output folder")
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
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(12)

        scroller = QScrollArea()
        scroller.setWidgetResizable(True)
        scroller.setFrameShape(QFrame.Shape.NoFrame)
        scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        content = QWidget()
        outer = QVBoxLayout(content)
        outer.setContentsMargins(0, 0, 6, 0)
        outer.setSpacing(12)

        columns = ResponsiveGrid(min_item_width=280)

        collection = QGroupBox("Collection")
        form = QFormLayout(collection)
        form.setSpacing(8)
        # At high scale factors a fixed label column truncates mid-word.
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
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
        columns.add(collection)

        sources = QGroupBox("Sources")
        sources_layout = QVBoxLayout(sources)
        sources_layout.setSpacing(6)
        self.source_checks: Dict[str, QCheckBox] = {}
        for name in available_sources():
            if name == "file":
                continue
            box = QCheckBox(name)
            sources_layout.addWidget(box)
            self.source_checks[name] = box
        sources_layout.addSpacing(6)
        sources_layout.addWidget(QLabel("Certstream URL"))
        self.in_certstream = QLineEdit()
        sources_layout.addWidget(self.in_certstream)
        sources_layout.addWidget(QLabel("Onion index URL"))
        self.in_onion_index = QLineEdit()
        sources_layout.addWidget(self.in_onion_index)
        sources_layout.addWidget(QLabel("Tor SOCKS proxy"))
        self.in_tor_proxy = QLineEdit()
        self.in_tor_proxy.setPlaceholderText("socks5://127.0.0.1:9050")
        self.in_tor_proxy.setToolTip(
            "Leave blank to store .onion domains without probing them."
        )
        sources_layout.addWidget(self.in_tor_proxy)
        sources_layout.addWidget(QLabel("Seed file (optional)"))
        seed_row = QHBoxLayout()
        self.in_seed = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_seed)
        seed_row.addWidget(self.in_seed, 1)
        seed_row.addWidget(browse)
        sources_layout.addLayout(seed_row)
        sources_layout.addStretch(1)
        columns.add(sources)

        behaviour = QGroupBox("Storage and re-checks")
        behaviour_form = QFormLayout(behaviour)
        behaviour_form.setSpacing(8)
        behaviour_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        behaviour_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.in_recheck = QSpinBox()
        self.in_recheck.setRange(0, 31_536_000)
        self.in_recheck.setSuffix(" s")
        self.in_recheck.setSpecialValueText("Disabled")
        self.in_recheck_batch = QSpinBox()
        self.in_recheck_batch.setRange(1, 100_000)
        self.chk_recheck_only = QCheckBox("Re-check only")
        self.chk_recheck_only.setToolTip("Refresh stored domains without discovering new ones.")
        self.chk_tech_files = QCheckBox("Write technology files")
        self.chk_tech_files.setToolTip("Append each live domain to output/<technology>.txt")
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
            box.setMinimumWidth(280)
        columns.add(behaviour)
        outer.addWidget(columns)
        outer.addStretch(1)
        scroller.setWidget(content)
        page_layout.addWidget(scroller, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.settings_note = ElidingLabel("Concurrency and sources apply on the next Start.")
        self.settings_note.setObjectName("pageSubtitle")
        buttons.addWidget(self.settings_note)
        revert = QPushButton("Revert")
        revert.clicked.connect(self._load_settings_form)
        save = QPushButton("Save settings")
        save.setObjectName("primary")
        save.clicked.connect(self._save_settings_form)
        buttons.addWidget(revert)
        buttons.addWidget(save)
        page_layout.addLayout(buttons)

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
        header.setMinimumSectionSize(46)
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

    def _make_stored_table(self) -> QTableView:
        table = QTableView()
        table.setModel(self.stored_model)
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
        header.setMinimumSectionSize(46)
        table.setColumnWidth(0, 260)  # Domain
        table.setColumnWidth(1, 56)   # Hosts
        table.setColumnWidth(2, 90)   # Status
        table.setColumnWidth(3, 70)   # HTTP
        table.setColumnWidth(4, 110)  # Source
        table.setColumnWidth(5, 150)  # First seen
        delegate = StatusDotDelegate(StoredDomainModel.ROLE_RESPONSIVE,
                                     self.palette_.success, self.palette_.text_faint, table)
        table.setItemDelegateForColumn(2, delegate)
        table.status_delegate = delegate
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
        header.setMinimumSectionSize(46)
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
        icon = logo_icon()
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
        for table in (self.recent_table, self.stored_table):
            table.status_delegate.set_colors(palette.success, palette.text_faint)
        for table in (self.dash_tech_table, self.tech_table):
            table.share_delegate.set_colors(palette.accent, palette.surface_hover)
        self.log_view.viewport().update()

    @Slot(int)
    def _on_theme_changed(self, index: int) -> None:
        if 0 <= index < len(self._theme_keys):
            self.theme_name = self._theme_keys[index]
        self.apply_theme(palette_for(self.theme_name))
        QSettings("DomainAtlas", "DomainAtlas").setValue("theme", self.theme_name)

    # --------------------------------------------------------------- browser
    def _connect_browser(self) -> None:
        self.browser_thread, self.browser = start_browser(self.config.db_path)
        application = QApplication.instance()
        if application is not None:
            application.aboutToQuit.connect(self.shutdown_browser)
        self.open_database.connect(self.browser.open)
        self.request_facets.connect(self.browser.fetch_facets)
        self.request_summary.connect(self.browser.fetch_summary)
        self.stored_model.request_page.connect(self.browser.fetch_page)
        self.stored_model.request_count.connect(self.browser.fetch_count)
        self.browser.page_ready.connect(self.stored_model.on_page)
        self.browser.count_ready.connect(self.stored_model.on_count)
        self.browser.page_ready.connect(self._on_page_loaded)
        self.browser.count_ready.connect(self._on_count_loaded)
        self.browser.facets_ready.connect(self._on_facets)
        self.browser.summary_ready.connect(self._on_summary)
        self.browser.failed.connect(self._on_browser_error)

        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(300)
        self._reload_timer.timeout.connect(self.reload_stored)

        self.open_database.emit(self.config.db_path)
        self.reload_stored()
        self.request_facets.emit()
        self.request_summary.emit()

    def current_filter(self) -> DomainFilter:
        """The filter described by the controls on the Domains page."""
        return DomainFilter(
            text=self.domain_search.text().strip(),
            technology=self.filter_technology.currentData(),
            source=self.filter_source.currentData(),
            responsive=self.filter_status.currentData(),
            onion=self.filter_network.currentData(),
            sites_only=bool(self.filter_grouping.currentData()),
        )

    def _schedule_reload(self) -> None:
        self._reload_timer.start()

    @Slot()
    def reload_stored(self) -> None:
        self._counted = False
        self.result_label.setText("Loading…")
        self.stored_model.reload(self.current_filter(), ORDER_NEWEST)

    @Slot(int, list, bool)
    def _on_page_loaded(self, request_id: int, rows: list, has_more: bool) -> None:
        self._update_result_label()

    @Slot(int, int, bool)
    def _on_count_loaded(self, request_id: int, total: int, capped: bool) -> None:
        self._counted = True
        self._update_result_label()

    def _update_result_label(self) -> None:
        loaded = self.stored_model.loaded_count()
        total = self.stored_model.total
        noun = "sites" if self.stored_model.filter.sites_only else "domains"
        if loaded and not getattr(self, "_counted", False):
            self.result_label.setText(f"Showing {loaded:,} {noun}, counting…")
            return
        if total == 0 and loaded == 0:
            criteria = self.current_filter()
            self.result_label.setText(
                f"No {noun} match this filter." if not criteria.is_empty()
                else "No domains stored yet - press Start to begin collecting."
            )
            return
        total_text = f"{total:,}+" if self.stored_model.total_capped else f"{total:,}"
        self.result_label.setText(f"Showing {loaded:,} of {total_text} matching {noun}")

    @Slot(list, list)
    def _on_facets(self, technologies: list, sources: list) -> None:
        self._refill_combo(self.filter_technology, "Any technology", technologies)
        self._refill_combo(self.filter_source, "Any source", sources)

    @staticmethod
    def _refill_combo(combo: QComboBox, placeholder: str, values: list) -> None:
        previous = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(placeholder, None)
        for value in values:
            combo.addItem(str(value), value)
        if previous is not None:
            index = combo.findData(previous)
            combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)

    @Slot(dict)
    def _on_summary(self, summary: dict) -> None:
        self.stored_summary = summary
        onion = summary.get("onion", 0)
        extra = f"   ·   {onion:,} .onion" if onion else ""
        self.status_database.setText(
            f"{summary.get('total', 0):,} stored   ·   "
            f"{summary.get('responsive', 0):,} live   ·   "
            f"{summary.get('technologies', 0):,} technologies{extra}   "
        )

    @Slot(str)
    def _on_browser_error(self, message: str) -> None:
        if "no database" in message.lower():
            self.result_label.setText("No database yet - press Start to begin collecting.")
            return
        self.log(f"Database: {message}", "ERROR")

    # ------------------------------------------------------------------ tray
    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray = QSystemTrayIcon(self)
        self._tray.setToolTip("Domain Atlas")
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
        titles = PAGE_HEADINGS
        self.page_title.setText(titles[index][0])
        self.page_subtitle.setText(titles[index][1])
        if index in (PAGE_DOMAINS, PAGE_TECH) and getattr(self, "_state", "") == "running":
            if index == PAGE_DOMAINS:
                self._auto_refresh_stored()
            self.request_summary.emit()

    def on_start(self) -> None:
        if self.collector.running:
            return
        self.collector.update_config(self.config)
        self.collector.start()
        # The database file may not have existed when the window opened.
        self.open_database.emit(self.config.db_path)
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
        criteria = self.current_filter()
        suggestion = "domains"
        if criteria.technology:
            suggestion = criteria.technology.lower().replace(" ", "-")
        path, selected = QFileDialog.getSaveFileName(
            self,
            "Export domains",
            f"{suggestion}-{datetime.now():%Y%m%d-%H%M%S}.csv",
            "CSV (*.csv);;JSON (*.json);;JSON Lines (*.jsonl);;Domain list (*.txt)",
        )
        if not path:
            return

        from ..export import format_for_path

        export_format = format_for_path(path)
        if selected.startswith("JSON Lines"):
            export_format = "jsonl"
        elif selected.startswith("JSON"):
            export_format = "json"
        elif selected.startswith("Domain list"):
            export_format = "txt"
        elif selected.startswith("CSV"):
            export_format = "csv"

        task = ExportTask(self.config.db_path, path, criteria, export_format)
        task.signals.finished.connect(self._on_export_finished)
        task.signals.failed.connect(self._on_export_failed)
        self.btn_export_domains.setEnabled(False)
        self.result_label.setText(f"Exporting to {path}…")
        QThreadPool.globalInstance().start(task)

    @Slot(int, str)
    def _on_export_finished(self, written: int, path: str) -> None:
        self.btn_export_domains.setEnabled(True)
        self.log(f"Exported {written:,} domains to {path}", "GOOD")
        self.status_left.setText(f"Exported {written:,} domains to {path}")
        self._update_result_label()

    @Slot(str)
    def _on_export_failed(self, message: str) -> None:
        self.btn_export_domains.setEnabled(True)
        QMessageBox.warning(self, "Export failed", message)
        self._update_result_label()

    def export_log(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save activity log", f"domain-atlas-{datetime.now():%Y%m%d-%H%M%S}.log",
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
        self.in_onion_index.setText(config.onion_index_url)
        self.in_tor_proxy.setText(config.tor_proxy)
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
        candidate.onion_index_url = self.in_onion_index.text().strip()
        candidate.tor_proxy = self.in_tor_proxy.text().strip()
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

        database_changed = candidate.db_path != self.config.db_path
        self.config = candidate
        self.collector.update_config(candidate)
        if database_changed:
            self.open_database.emit(candidate.db_path)
            self.reload_stored()
            self.request_facets.emit()
            self.request_summary.emit()
        try:
            candidate.save(self.config_path)
            self.log(f"Settings saved to {self.config_path}", "GOOD")
            self.status_left.setText(f"Settings saved to {self.config_path}")
        except ConfigError as exc:
            QMessageBox.warning(self, "Settings not saved", str(exc))

    def _auto_refresh_stored(self) -> None:
        """Pull newly stored domains into the Domains page during a run.

        Only done when it cannot disturb what the user is looking at: the
        page has to be on screen, scrolled to the top, and showing no more
        than the first page of results.
        """
        if self.pages.currentIndex() != PAGE_DOMAINS:
            return
        if self.stored_table.verticalScrollBar().value() != 0:
            return
        if self.stored_model.loaded_count() > PAGE_SIZE:
            return
        self.reload_stored()
        self.request_facets.emit()

    # ------------------------------------------------------------------ pump
    def _pump(self) -> None:
        try:
            events = self.collector.drain_events()
            if events:
                self._handle_events(events)
            self._refresh_stats()
            if self._state in ("running", "paused", "stopping") and not self.collector.running:
                self._set_state("stopped")
                self.reload_stored()
                self.request_facets.emit()
                self.request_summary.emit()
            self._refresh_tick = getattr(self, "_refresh_tick", 0) + 1
            if self._state == "running" and self._refresh_tick % 40 == 0:
                self.request_summary.emit()
                self._auto_refresh_stored()
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
            "discovered": stats.discovered,
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

    #: Below this width the toolbar buttons drop their labels.
    COMPACT_WIDTH = 1000

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_compact_toolbar(self.width() < self.COMPACT_WIDTH)

    def _apply_compact_toolbar(self, compact: bool) -> None:
        if getattr(self, "_compact_toolbar", None) == compact:
            return
        self._compact_toolbar = compact
        note = getattr(self, "settings_note", None)
        if note is not None:
            note.setVisible(not compact)
        for button, label in (
            (self.btn_start, "Start"),
            (self.btn_pause, "Resume" if self._state == "paused" else "Pause"),
            (self.btn_stop, "Stop"),
        ):
            button.setToolTip(label)
            button.setText("" if compact else f"  {label}")

    def _set_state(self, state: str) -> None:
        self._state = state
        self.status_pill.set_state(state)
        running = state in ("running", "paused")
        self.btn_start.setEnabled(not running and state != "stopping")
        self.btn_pause.setEnabled(running)
        self.btn_stop.setEnabled(running)
        if getattr(self, "_compact_toolbar", False):
            self.btn_pause.setText("")
            self.btn_pause.setToolTip("Resume" if state == "paused" else "Pause")
        else:
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
    def _apply_tech_filter(self) -> None:
        self.tech_proxy.set_needle(self.tech_search.text())

    # -------------------------------------------------------------- geometry
    def _restore_geometry(self) -> None:
        settings = QSettings("DomainAtlas", "DomainAtlas")
        geometry = settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.collector.running:
            answer = QMessageBox.question(
                self, "Quit Domain Atlas",
                "Collection is still running. Stop it and quit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.collector.stop(timeout=25)
        settings = QSettings("DomainAtlas", "DomainAtlas")
        settings.setValue("geometry", self.saveGeometry())
        settings.setValue("theme", self.theme_name)
        self.timer.stop()
        if self._tray is not None:
            self._tray.hide()
        self.shutdown_browser()
        event.accept()

    @Slot()
    def shutdown_browser(self) -> None:
        """Stop the query thread. Safe to call more than once."""
        thread = getattr(self, "browser_thread", None)
        if thread is None:
            return
        thread.quit()
        stopped = thread.wait(5000)
        browser = getattr(self, "browser", None)
        # Close the connection only once the thread that owns it has finished,
        # otherwise a query still in flight reads from a closed handle.
        if browser is not None and stopped:
            browser.close()
        self.browser_thread = None
