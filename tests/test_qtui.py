"""Qt front-end tests.

They run against the offscreen platform plugin, so no display is needed; the
whole module is skipped when PySide6 (or its system libraries) is unavailable.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")

from PySide6.QtWidgets import QApplication  # noqa: E402

from domaincollector.config import Config  # noqa: E402
from domaincollector.engine import Event, Stats  # noqa: E402
from domaincollector.qtui import pyside_available  # noqa: E402
from domaincollector.qtui.icons import _SVG, app_icon, icon_pixmap, make_icon  # noqa: E402
from domaincollector.qtui.models import DomainTableModel, TechnologyTableModel  # noqa: E402
from domaincollector.qtui.theme import DARK, LIGHT, palette_for, stylesheet  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    yield app


@pytest.fixture
def window(qapp, tmp_path):
    from domaincollector.qtui.mainwindow import MainWindow

    config = Config(
        concurrency=2, fetch_interval=3600, db_path=str(tmp_path / "d.db"),
        output_dir=str(tmp_path / "out"), cache_dir=str(tmp_path / "cache"),
    ).validate()
    win = MainWindow(config, str(tmp_path / "config.json"), theme="dark")
    win.timer.stop()  # drive the pump by hand in tests
    yield win
    win.collector.stop(timeout=5)
    win.timer.stop()


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


def test_domain_filter_hides_rows(window):
    window._handle_events([
        _domain_event("alpha.example", technologies=("Nginx",)),
        _domain_event("beta.example", responsive=False, technologies=()),
    ])
    assert window.domain_proxy.rowCount() == 2
    window.domain_search.setText("alpha")
    assert window.domain_proxy.rowCount() == 1
    assert window.domain_proxy.data(window.domain_proxy.index(0, 0)) == "alpha.example"

    window.domain_search.setText("nginx")   # technologies are searched too
    assert window.domain_proxy.rowCount() == 1

    window.domain_search.clear()
    window.only_live.setChecked(True)
    assert window.domain_proxy.rowCount() == 1

    # A filter must survive new results arriving
    window._handle_events([_domain_event("gamma.example")])
    assert window.domain_proxy.rowCount() == 2
    window.only_live.setChecked(False)
    assert window.domain_proxy.rowCount() == 3


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
        "domaincollector.qtui.mainwindow.QMessageBox.warning",
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
