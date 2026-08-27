"""The GUI runs the engine in a worker thread - make sure that stays safe."""

import time

import pytest

from domainatlas.config import Config
from domainatlas.engine import Event
from domainatlas.runner import CollectorThread


@pytest.fixture
def config(tmp_path):
    return Config(
        concurrency=2,
        fetch_interval=3600,
        max_domains_per_cycle=1,
        db_path=str(tmp_path / "d.db"),
        output_dir=str(tmp_path / "out"),
        cache_dir=str(tmp_path / "cache"),
        sources=["file"],
        seed_file=str(tmp_path / "seeds.txt"),
    ).validate()


def test_start_and_stop_is_clean(config, tmp_path):
    (tmp_path / "seeds.txt").write_text("127.0.0.1:1\n")
    runner = CollectorThread(config)
    runner.start()
    assert runner.running is True
    time.sleep(1.0)
    assert runner.stop(timeout=30) is True
    assert runner.running is False
    assert runner.error is None
    events = runner.drain_events()
    assert any(event.kind == "state" for event in events)


def test_pause_and_resume_do_not_raise(config, tmp_path):
    (tmp_path / "seeds.txt").write_text("example.com\n")
    runner = CollectorThread(config)
    runner.start()
    try:
        runner.pause()
        time.sleep(0.3)
        runner.resume()
        time.sleep(0.3)
    finally:
        assert runner.stop(timeout=30) is True


def test_control_calls_before_start_are_ignored(config):
    runner = CollectorThread(config)
    runner.pause()
    runner.resume()
    assert runner.stop(timeout=1) is True
    assert runner.running is False


def test_bad_configuration_is_reported_not_raised(tmp_path):
    config = Config(
        db_path=str(tmp_path / "d.db"), output_dir=str(tmp_path / "out"),
        cache_dir=str(tmp_path / "cache"),
    ).validate()
    config.sources = ["nope"]  # bypasses validate() on purpose
    runner = CollectorThread(config)
    runner.start()
    time.sleep(0.5)
    assert runner.error is not None
    assert any("Cannot start" in event.message for event in runner.drain_events())
    runner.stop(timeout=5)


def test_event_queue_drops_the_oldest_when_full(config):
    runner = CollectorThread(config, max_events=3)
    for index in range(10):
        runner._publish(Event("log", f"event {index}"))
    drained = runner.drain_events()
    assert len(drained) == 3
    assert drained[-1].message == "event 9"
