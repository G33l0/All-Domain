"""Run the collector on a private event loop in a background thread.

Tkinter is not thread safe and asyncio is not Tk-loop friendly, so the two are
kept strictly apart:

* the engine runs on its own loop inside :class:`CollectorThread`;
* control calls (start/pause/resume/stop) are marshalled onto that loop with
  ``call_soon_threadsafe``;
* events travel back through a plain ``queue.Queue`` that the GUI drains from
  its own ``after()`` timer.

The 1.x GUI called Tk widget methods directly from the asyncio thread, which is
the classic source of random freezes and ``RuntimeError: main thread is not in
main loop`` crashes.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from typing import Callable, List, Optional

from .config import Config
from .engine import Collector, Event, Stats


class CollectorThread:
    """Thread-safe façade around :class:`~domaincollector.engine.Collector`."""

    def __init__(self, config: Config, max_events: int = 10000) -> None:
        self.config = config
        self.events: "queue.Queue[Event]" = queue.Queue(maxsize=max_events)
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._collector: Optional[Collector] = None
        self._ready = threading.Event()
        self._finished = threading.Event()
        self._error: Optional[BaseException] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ state
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def paused(self) -> bool:
        return bool(self._collector and self._collector.paused)

    @property
    def error(self) -> Optional[BaseException]:
        return self._error

    @property
    def stats(self) -> Optional[Stats]:
        return self._collector.stats if self._collector else None

    # ---------------------------------------------------------------- control
    def start(self) -> None:
        """Start collecting.  Safe to call from the GUI thread."""
        with self._lock:
            if self.running:
                return
            self._error = None
            self._ready.clear()
            self._finished.clear()
            self._thread = threading.Thread(target=self._run, name="collector", daemon=True)
            self._thread.start()
        # Wait briefly so callers can rely on ``running`` right after start().
        self._ready.wait(timeout=5)

    def update_config(self, config: Config) -> None:
        """Swap in a new config.  Values read per request apply immediately;
        worker count and source list take effect on the next start."""
        self.config = config
        collector = self._collector
        if collector is not None:
            collector.config = config

    def pause(self) -> None:
        self._call(lambda collector: collector.pause())

    def resume(self) -> None:
        self._call(lambda collector: collector.resume())

    def stop(self, timeout: float = 20.0) -> bool:
        """Ask the engine to shut down and wait for it.  ``True`` when it ended."""
        self._call(lambda collector: collector.stop())
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                return False
        self._thread = None
        return True

    def _call(self, action: Callable[[Collector], None]) -> None:
        loop, collector = self._loop, self._collector
        if loop is None or collector is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(action, collector)
        except RuntimeError:
            pass

    # ------------------------------------------------------------------ drain
    def drain_events(self, limit: int = 500) -> List[Event]:
        """Pop up to *limit* pending events (called from the GUI thread)."""
        drained: List[Event] = []
        for _ in range(limit):
            try:
                drained.append(self.events.get_nowait())
            except queue.Empty:
                break
        return drained

    # ----------------------------------------------------------------- thread
    def _publish(self, event: Event) -> None:
        try:
            self.events.put_nowait(event)
        except queue.Full:
            # Drop the oldest event rather than blocking the engine.
            try:
                self.events.get_nowait()
                self.events.put_nowait(event)
            except queue.Empty:  # pragma: no cover - race, harmless
                pass

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            self._collector = Collector(self.config, on_event=self._publish)
        except Exception as exc:  # bad config / unknown source
            self._error = exc
            self._publish(Event("log", f"Cannot start: {exc}", "ERROR"))
            self._publish(Event("state", "Stopped.", "ERROR", {"state": "stopped"}))
            self._ready.set()
            self._finished.set()
            self._loop = None
            loop.close()
            return
        self._ready.set()
        try:
            loop.run_until_complete(self._collector.run())
        except Exception as exc:  # pragma: no cover - safety net
            self._error = exc
            self._publish(Event("log", f"Collector crashed: {exc}", "ERROR"))
            self._publish(Event("state", "Stopped.", "ERROR", {"state": "stopped"}))
        finally:
            try:
                self._shutdown_loop(loop)
            finally:
                asyncio.set_event_loop(None)
                loop.close()
                self._loop = None
                self._finished.set()

    @staticmethod
    def _shutdown_loop(loop: asyncio.AbstractEventLoop) -> None:
        """Cancel leftovers so no 'Task was destroyed but it is pending' noise."""
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
