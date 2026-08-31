"""Qt front-end tests.

They run against the offscreen platform plugin, so no display is needed; the
whole module is skipped when PySide6 (or its system libraries) is unavailable.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from PySide6.QtWidgets import QApplication  # noqa: E402

from domainatlas.config import Config  # noqa: E402
from domainatlas.engine import Event, Stats  # noqa: E402
from domainatlas.qtui import pyside_available  # noqa: E402
from domainatlas.qtui.icons import _SVG, app_icon, icon_pixmap, make_icon  # noqa: E402
from domainatlas.qtui.models import DomainTableModel, TechnologyTableModel  # noqa: E402
from domainatlas.qtui.theme import DARK, LIGHT, palette_for, stylesheet  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    yield app


@pytest.fixture
def window(qapp, tmp_path):
    from domainatlas.qtui.mainwindow import MainWindow

    config = Config(
        concurrency=2, fetch_interval=3600, db_path=str(tmp_path / "d.db"),
        output_dir=str(tmp_path / "out"), cache_dir=str(tmp_path / "cache"),
    ).validate()
    win = MainWindow(config, str(tmp_path / "config.json"), theme="dark")
    win.timer.stop()  # drive the pump by hand in tests
    yield win
    win.collector.stop(timeout=5)
    win.timer.stop()
    win.shutdown_browser()


# ------------------------------------------------------------------- basics
def test_pyside_is_available():
    assert pyside_available() is True


def test_stylesheets_render_for_both_themes(qapp):
    for palette in (LIGHT, DARK):
        sheet = stylesheet(palette)
        assert palette.accent in sheet
        assert "{" not in sheet.split("QWidget#root")[0][-3:]  # no unformatted placeholders
        assert len(sheet) > 2000


def test_palette_for_accepts_explicit_themes(qapp):
    assert palette_for("dark") is DARK
    assert palette_for("light") is LIGHT
    assert palette_for("system") in (LIGHT, DARK)


def test_every_icon_renders(qapp):
    for name in _SVG:
        assert not icon_pixmap(name, "#ff0000", 18).isNull(), name
    assert not make_icon("play", "#fff").isNull()
    assert app_icon("#ffffff").availableSizes()


# ------------------------------------------------------------------- models
def _domain_event(domain, responsive=True, technologies=("Nginx",), status=200, source="tranco"):
    return Event("domain", f"{domain}", "GOOD", {
        "domain": domain, "responsive": responsive, "status": status,
        "source": source, "technologies": list(technologies), "recheck": False,
    })


def test_domain_model_shows_newest_first_and_is_bounded(qapp):
    model = DomainTableModel(max_rows=3)
    model.add_events([_domain_event(f"d{i}.com") for i in range(5)])
    assert model.rowCount() == 3
    assert model.data(model.index(0, 0)) == "d4.com"
    assert model.data(model.index(0, 1)) == "Live"
    assert model.data(model.index(0, 2)) == "200"
    assert model.data(model.index(0, 4)) == "Nginx"


def test_domain_model_renders_unreachable_rows(qapp):
    model = DomainTableModel()
    model.add_events([_domain_event("down.com", responsive=False, technologies=(), status=None)])
    assert model.data(model.index(0, 1)) == "Down"
    assert model.data(model.index(0, 2)) == "-"
    assert model.data(model.index(0, 4)) == "-"
    assert model.data(model.index(0, 0), DomainTableModel.ROLE_RESPONSIVE) is False


def test_domain_model_ignores_non_domain_events(qapp):
    model = DomainTableModel()
    model.add_events([Event("log", "hello"), Event("state", "Started.", data={"state": "running"})])
    assert model.rowCount() == 0


def test_technology_model_sorts_and_computes_share(qapp):
    model = TechnologyTableModel()
    model.set_counts({"React": 5, "Nginx": 20, "Apache": 5})
    assert model.rows() == [("Nginx", 20), ("Apache", 5), ("React", 5)]
    assert model.data(model.index(0, 2), TechnologyTableModel.ROLE_SHARE) == 1.0
    assert model.data(model.index(1, 2), TechnologyTableModel.ROLE_SHARE) == 0.25
    model.clear()
    assert model.rowCount() == 0


# ------------------------------------------------------------------- window
def test_window_starts_stopped_with_the_right_buttons(window):
    assert window._state == "stopped"
    assert window.btn_start.isEnabled()
    assert not window.btn_pause.isEnabled()
    assert not window.btn_stop.isEnabled()
    assert window.pages.count() == 5


def test_navigation_switches_pages_and_titles(window):
    window._select_page(2)
    assert window.pages.currentIndex() == 2
    assert window.page_title.text() == "Technologies"
    assert window.nav_buttons[2].isChecked()
    assert not window.nav_buttons[0].isChecked()


def test_events_populate_the_tables_and_log(window):
    events = [
        Event("log", "Collector started", "INFO"),
        _domain_event("live.example"),
        _domain_event("dead.example", responsive=False, technologies=(), status=None),
    ]
    window._handle_events(events)
    assert window.domain_model.rowCount() == 2
    assert "Collector started" in window.log_view.toPlainText()
    assert "live.example" in window.log_view.toPlainText()


def test_state_events_drive_the_buttons(window):
    window._handle_events([Event("state", "Started.", "INFO", {"state": "running"})])
    assert window._state == "running"
    assert not window.btn_start.isEnabled()
    assert window.btn_pause.isEnabled()
    assert window.btn_pause.text().strip() == "Pause"

    window._handle_events([Event("state", "Paused.", "WARN", {"state": "paused"})])
    assert window.btn_pause.text().strip() == "Resume"


def test_stats_refresh_updates_the_cards(window):
    stats = Stats()
    stats.processed, stats.responsive, stats.unreachable = 40, 30, 10
    stats.new, stats.rechecked, stats.queued = 38, 2, 7
    stats.tech_counts.update({"Nginx": 12, "React": 3})
    window.collector._collector = type("Fake", (), {"stats": stats})()
    window._refresh_stats()
    assert window.cards["processed"].value.text() == "40"
    assert window.cards["rechecked"].value.text() == "2"
    assert "75%" in window.cards["responsive"].hint.text()
    assert window.tech_model.rowCount() == 2


def test_rate_is_hidden_until_it_is_meaningful(window):
    stats = Stats()
    stats.processed = 5
    window.collector._collector = type("Fake", (), {"stats": stats})()
    window._refresh_stats()
    assert "measuring" in window.status_right.text()


def test_current_filter_reflects_the_controls(window):
    window.domain_search.setText("example")
    window.filter_status.setCurrentIndex(1)      # Live only
    window.filter_network.setCurrentIndex(2)     # Tor only
    criteria = window.current_filter()
    assert criteria.text == "example"
    assert criteria.responsive is True
    assert criteria.onion is True

    window.filter_status.setCurrentIndex(2)      # Down only
    window.filter_network.setCurrentIndex(1)     # Clear web
    criteria = window.current_filter()
    assert criteria.responsive is False
    assert criteria.onion is False


def test_filter_changes_are_debounced(window):
    window.domain_search.setText("abc")
    assert window._reload_timer.isActive()


def test_session_proxy_filters_the_dashboard_feed(window):
    window._handle_events([
        _domain_event("alpha.example", technologies=("Nginx",)),
        _domain_event("beta.example", responsive=False, technologies=()),
    ])
    assert window.domain_proxy.rowCount() == 2
    window.domain_proxy.set_needle("alpha")
    assert window.domain_proxy.rowCount() == 1
    window.domain_proxy.set_needle("")
    window.domain_proxy.set_live_only(True)
    assert window.domain_proxy.rowCount() == 1


def test_technology_filter_hides_rows(window):
    window.tech_model.set_counts({"Nginx": 4, "React": 2})
    window.tech_search.setText("rea")
    assert window.tech_proxy.rowCount() == 1
    assert window.tech_proxy.data(window.tech_proxy.index(0, 0)) == "React"
    window.tech_search.clear()
    assert window.tech_proxy.rowCount() == 2


def test_settings_round_trip_through_the_form(window, tmp_path):
    window.in_concurrency.setValue(44)
    window.in_recheck.setValue(3600)
    window.in_certstream.setText("ws://localhost:1234/domains-only")
    window.source_checks["crtsh"].setChecked(False)
    window.source_checks["tranco"].setChecked(True)
    window._save_settings_form()

    assert window.config.concurrency == 44
    assert window.config.recheck_after == 3600
    assert window.config.certstream_url == "ws://localhost:1234/domains-only"
    assert "crtsh" not in window.config.sources
    assert (tmp_path / "config.json").exists()

    saved = Config.load(str(tmp_path / "config.json"))
    assert saved.concurrency == 44
    assert saved.recheck_after == 3600


def test_invalid_settings_are_refused(window, monkeypatch, tmp_path):
    warnings = []
    monkeypatch.setattr(
        "domainatlas.qtui.mainwindow.QMessageBox.warning",
        lambda *args, **kwargs: warnings.append(args[2] if len(args) > 2 else ""),
    )
    window.in_certstream.setText("http://not-a-websocket")
    window._save_settings_form()
    assert warnings and "ws://" in warnings[0]
    assert window.config.certstream_url != "http://not-a-websocket"


def test_theme_switch_repaints_without_error(window):
    window._on_theme_changed(1)  # light
    assert window.theme_name == "light"
    assert window.palette_.name == "light"
    window._on_theme_changed(2)  # dark
    assert window.palette_.name == "dark"


def test_log_is_capped(window):
    for index in range(4000):
        window.log(f"line {index}")
    assert window.log_view.blockCount() <= 3001


def test_pump_survives_a_broken_collector(window):
    class Boom:
        def drain_events(self):
            raise RuntimeError("kaboom")

        running = False
        stats = None

        def stop(self, timeout=0):
            return True

    window.collector = Boom()
    window._pump()  # must not raise
    assert "kaboom" in window.log_view.toPlainText()


# --------------------------------------------------------------- high DPI
def test_icons_are_identical_at_every_device_pixel_ratio(qapp):
    """A DPR-tagged pixmap is painted in logical units.

    Scaling the render rect by the ratio as well drew the glyph several times
    too large, so a 200% display showed only its top-left corner.
    """
    from PySide6.QtCore import Qt

    for name in ("dashboard", "settings", "play"):
        base = icon_pixmap(name, "#ffffff", 16, ratio=1.0).toImage()
        for ratio in (2.0, 4.0):
            scaled = icon_pixmap(name, "#ffffff", 16, ratio=ratio)
            assert scaled.devicePixelRatio() == ratio
            assert scaled.width() == int(16 * ratio)
            # Downsampled back to 1x the artwork must match the 1x render.
            shrunk = scaled.toImage().scaled(
                16, 16, Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            differing = sum(
                1
                for y in range(16)
                for x in range(16)
                if abs(shrunk.pixelColor(x, y).alpha() - base.pixelColor(x, y).alpha()) > 120
            )
            assert differing < 40, f"{name} at {ratio}x differs in {differing} pixels"


def test_logo_is_identical_at_every_device_pixel_ratio(qapp):
    from domainatlas.qtui.logo import logo_pixmap

    for ratio in (1.0, 2.0, 4.0):
        pixmap = logo_pixmap(32, ratio)
        assert pixmap.devicePixelRatio() == ratio
        assert pixmap.width() == int(32 * ratio)
        image = pixmap.toImage()
        # A mis-scaled render fills only the top-left quadrant, leaving the
        # lower right of the tile blank. The extreme corner is transparent by
        # design because the tile is rounded, so sample inside the radius.
        inset = int(image.width() * 0.82)
        assert image.pixelColor(inset, inset).alpha() > 0
        assert image.pixelColor(image.width() // 2, image.height() // 2).alpha() > 0


def test_window_fits_a_1080p_display_at_200_percent(window):
    """1920x1080 at 200% scaling exposes 960x540 logical pixels."""
    minimum = window.minimumSizeHint().expandedTo(window.minimumSize())
    assert minimum.width() <= 960
    assert minimum.height() <= 540


def test_settings_page_is_scrollable(window):
    from PySide6.QtWidgets import QScrollArea

    window._select_page(4)
    assert window.pages.widget(4).findChildren(QScrollArea)


def test_toolbar_drops_labels_when_narrow(window):
    window._apply_compact_toolbar(True)
    assert window.btn_start.text().strip() == ""
    assert window.btn_start.toolTip() == "Start"
    window._apply_compact_toolbar(False)
    assert window.btn_start.text().strip() == "Start"


def test_eliding_label_reports_a_small_minimum(qapp):
    from domainatlas.qtui.widgets import ElidingLabel

    label = ElidingLabel("A caption long enough to force a wide minimum width")
    label.resize(60, 20)
    label._apply_elide()
    assert label.minimumSizeHint().width() <= 40
    assert label.text() != label.fullText()      # elided
    assert label.toolTip() == label.fullText()


def test_responsive_grid_reflows_by_width(qapp):
    from PySide6.QtWidgets import QLabel

    from domainatlas.qtui.widgets import ResponsiveGrid

    grid = ResponsiveGrid(min_item_width=100, spacing=10)
    for index in range(6):
        grid.add(QLabel(f"item {index}"))

    grid.resize(660, 100)
    grid._relayout()
    assert grid._columns == 6

    grid.resize(230, 200)
    grid._relayout()
    assert grid._columns == 2
    assert grid.minimumSizeHint().width() <= 100


def test_every_theme_survives_the_startup_whitelist(qapp, monkeypatch, tmp_path):
    """A theme chosen in Settings has to come back on the next launch."""
    from PySide6.QtCore import QTimer

    import domainatlas.qtui as qtui
    import domainatlas.qtui.mainwindow as mainwindow
    from domainatlas.qtui.theme import theme_names

    seen = []
    original = mainwindow.MainWindow.__init__

    def record(self, config, config_path=".", theme="system"):
        seen.append(theme)
        original(self, config, config_path, theme)
        self.timer.stop()

    monkeypatch.setattr(mainwindow.MainWindow, "__init__", record)
    config = Config(db_path=str(tmp_path / "d.db"), output_dir=str(tmp_path / "out"),
                    cache_dir=str(tmp_path / "cache")).validate()

    for name in ("system", *theme_names()):
        QTimer.singleShot(0, qapp.quit)
        qtui.run_qt_gui(config, str(tmp_path / "config.json"), theme=name)

    assert seen == ["system", *theme_names()]


def test_stored_table_follows_a_run_in_progress(window, monkeypatch):
    """The Domains page must not sit empty while the collector stores rows."""
    reloads = []
    monkeypatch.setattr(type(window), "reload_stored",
                        lambda self: reloads.append(self.current_filter()))

    window._select_page(1)
    window._state = "running"
    for _ in range(41):
        window._pump()
    assert reloads, "a running collector should refresh the stored list"

    # ... but not while the user is reading further down the list.
    reloads.clear()
    window.stored_table.verticalScrollBar().setRange(0, 500)
    window.stored_table.verticalScrollBar().setValue(120)
    for _ in range(41):
        window._pump()
    assert not reloads


def test_shutdown_closes_the_connection_after_the_thread_stops(window):
    """Closing the query connection from the GUI thread first is a race."""
    order = []
    thread = window.browser_thread
    real_wait = thread.wait
    thread.wait = lambda msecs=5000: (order.append("wait"), real_wait(msecs))[1]
    window.browser.close = lambda: order.append("close")

    window.shutdown_browser()
    assert order == ["wait", "close"]


def test_page_subtitles_are_never_elided_at_the_minimum_width(window, qapp):
    """A cut-off subtitle ("Live discovery…") reads as a rendering fault."""
    from domainatlas.qtui.mainwindow import PAGE_HEADINGS

    window.resize(820, 460)
    qapp.processEvents()
    for index, (title, subtitle) in enumerate(PAGE_HEADINGS):
        window._select_page(index)
        qapp.processEvents()
        assert window.page_title.text() == title
        assert window.page_subtitle.fullText() == subtitle
        assert window.page_subtitle.text() == subtitle, (
            f"{title} subtitle was elided to {window.page_subtitle.text()!r}"
        )


def test_the_application_carries_the_brand_icon(window, qapp):
    """The taskbar reads the icon off the application, not only the window."""
    from domainatlas.qtui import claim_windows_taskbar_identity
    from domainatlas.qtui.logo import ICON_SIZES

    assert not window.windowIcon().isNull()
    assert sorted(s.width() for s in window.windowIcon().availableSizes()) == list(ICON_SIZES)
    # Safe to call anywhere; only Windows has a shell to tell.
    assert claim_windows_taskbar_identity() in (True, False)
