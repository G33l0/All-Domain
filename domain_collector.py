#!/usr/bin/env python3
import asyncio
import aiohttp
import aiosqlite
import aiofiles
import os
import sys
import json
import idna
import shutil
import time
from datetime import datetime
from typing import List, Dict, Set
from collections import defaultdict
import sqlite3

from pyfiglet import Figlet
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text
from rich import box
from rich.align import Align
from rich.prompt import Prompt, Confirm
from rich.markdown import Markdown
import aioconsole

from builtwith import builtwith

# ==================== CONSTANTS ====================
DB_PATH = "domains.db"
OUTPUT_DIR = "output"

# ==================== CONFIGURATION ====================
class Config:
    def __init__(self):
        self.concurrency = 20
        self.http_timeout = 10
        self.fetch_interval = 1800
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        self.max_queue_size = 5000
        self.log_limit = 30
        self.paused = False
        self.tech_detection = True

config = Config()

# ==================== UTILITIES ====================
def domain_fingerprint(raw: str) -> str:
    raw = raw.split('://')[-1].split('/')[0].split(':')[0]
    try:
        raw = idna.encode(raw).decode('ascii')
    except:
        raw = raw.lower()
    return raw.rstrip('.').lower()

# ==================== DATABASE ====================
class DomainStore:
    def __init__(self):
        self._seen: Set[str] = set()
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        self._init_db_sync()

    def _init_db_sync(self):
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS domains (
                    fingerprint TEXT PRIMARY KEY,
                    raw TEXT NOT NULL,
                    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    responsive BOOLEAN DEFAULT 0,
                    technologies TEXT
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_fingerprint ON domains(fingerprint)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_responsive ON domains(responsive)')
            conn.commit()
            cur = conn.execute('SELECT fingerprint FROM domains')
            self._seen = {row[0] for row in cur}

    async def is_new(self, fp: str) -> bool:
        return fp not in self._seen

    async def add_domain(self, raw: str, fp: str, responsive: bool, techs: List[str]):
        if fp in self._seen:
            return False
        tech_json = json.dumps(techs) if techs else None
        async with aiosqlite.connect(DB_PATH) as db:
            try:
                await db.execute(
                    'INSERT INTO domains (fingerprint, raw, responsive, technologies) VALUES (?, ?, ?, ?)',
                    (fp, raw, 1 if responsive else 0, tech_json)
                )
                await db.commit()
                self._seen.add(fp)
                if responsive and techs:
                    await self._write_tech_files(raw, techs)
                return True
            except aiosqlite.IntegrityError:
                return False

    async def _write_tech_files(self, domain: str, techs: List[str]):
        for tech in techs:
            safe = "".join(c for c in tech if c.isalnum() or c in (' ', '-', '_')).strip() or "unknown"
            fpath = os.path.join(OUTPUT_DIR, f"{safe}.txt")
            async with aiofiles.open(fpath, 'a', encoding='utf-8') as f:
                await f.write(domain + '\n')

# ==================== HTTP CHECKER ====================
async def check_domain(session: aiohttp.ClientSession, domain: str):
    techs = []
    responsive = False
    for scheme in ('https', 'http'):
        url = f"{scheme}://{domain}"
        try:
            async with session.get(url, timeout=config.http_timeout, allow_redirects=True) as resp:
                if resp.status < 400:
                    responsive = True
                    if config.tech_detection:
                        html = await resp.text(errors='ignore', limit=200_000)
                        detected = builtwith(html, url=url)
                        for _, items in detected.items():
                            techs.extend(items)
                    if techs:
                        break
        except:
            continue
    return responsive, list(set(techs))

# ==================== DOMAIN SOURCES ====================
async def fetch_crt(session: aiohttp.ClientSession, limit=200) -> List[str]:
    url = f"https://crt.sh/?q=%.&output=json&excluded=expired&limit={limit}"
    try:
        async with session.get(url, timeout=15) as resp:
            if resp.status == 200:
                data = await resp.json()
                domains = set()
                for entry in data:
                    name = entry.get('name_value')
                    if name:
                        for d in name.split('\n'):
                            d = d.strip().lower()
                            if d and not d.startswith('*.'):
                                domains.add(d)
                return list(domains)
    except:
        pass
    return []

async def fetch_fallback(session: aiohttp.ClientSession) -> List[str]:
    url = "https://raw.githubusercontent.com/publicsuffix/list/master/public_suffix_list.dat"
    try:
        async with session.get(url, timeout=10) as resp:
            if resp.status == 200:
                text = await resp.text()
                domains = []
                for line in text.splitlines():
                    line = line.strip()
                    if line and not line.startswith('//') and not line.startswith('!'):
                        domains.append(line)
                return domains[:500]
    except:
        pass
    return []

# ==================== PRODUCER ====================
class DomainProducer:
    def __init__(self, store: DomainStore, ui, queue: asyncio.Queue):
        self.store = store
        self.ui = ui
        self.queue = queue
        self.running = True
        self.retry_count = 0

    async def run(self):
        async with aiohttp.ClientSession(headers={'User-Agent': config.user_agent}) as session:
            while self.running:
                if not config.paused:
                    try:
                        domains = await fetch_crt(session, limit=200)
                        if not domains:
                            self.ui.add_log("crt.sh empty, trying fallback...")
                            domains = await fetch_fallback(session)
                        if domains:
                            self.ui.add_log(f"Fetched {len(domains)} new domains")
                            for d in domains:
                                fp = domain_fingerprint(d)
                                if await self.store.is_new(fp):
                                    await self.queue.put((d, fp))
                            self.ui.stats["queue"] = self.queue.qsize()
                            self.retry_count = 0
                        else:
                            self.retry_count += 1
                            self.ui.add_log(f"No domains fetched (attempt {self.retry_count})")
                            await asyncio.sleep(min(60, 5 * self.retry_count))
                    except Exception as e:
                        self.ui.add_log(f"Producer error: {e}")
                for _ in range(config.fetch_interval):
                    if not self.running:
                        break
                    await asyncio.sleep(1)

    def stop(self):
        self.running = False

# ==================== UI ====================
class LiveUI:
    def __init__(self, store: DomainStore):
        self.store = store
        self.console = Console()
        self.stats = {
            "processed": 0,
            "responsive": 0,
            "new": 0,
            "queue": 0,
            "tech_counts": defaultdict(int),
        }
        self.logs: List[str] = []
        self.running = True
        self.lock = asyncio.Lock()
        self.paused_for_input = False
        self._banner = self._render_banner()
        self.awaiting_start = True

    def _render_banner(self) -> str:
        width = min(shutil.get_terminal_size().columns, 80)
        fig = Figlet(font="small", width=width)
        return fig.renderText("Domain Collector")

    def add_log(self, msg: str, color: str = "white"):
        ts = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{ts}] {msg}")
        if len(self.logs) > config.log_limit:
            self.logs.pop(0)

    async def update_stats(self, responsive: bool, techs: List[str], inserted: bool):
        async with self.lock:
            self.stats["processed"] += 1
            if inserted:
                self.stats["new"] += 1
            if responsive:
                self.stats["responsive"] += 1
                for t in techs:
                    self.stats["tech_counts"][t] += 1

    def render(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=5),
            Layout(name="body", ratio=1),
            Layout(name="footer", size=3)
        )
        # Header - simple title
        header_panel = Panel(
            Align.center(Text(self._banner, style="bold cyan")),
            border_style="blue",
            box=box.SIMPLE
        )
        layout["header"].update(header_panel)

        # Body
        body = Layout()
        body.split_row(
            Layout(name="left", ratio=2),
            Layout(name="right", ratio=1)
        )
        left = Layout()
        left.split_column(
            Layout(name="stats", size=5),
            Layout(name="table", ratio=1)
        )
        # Stats
        if self.paused_for_input:
            status = "⌨️ COMMAND"
        elif self.awaiting_start:
            status = "⏳ WAITING"
        else:
            status = "⏸ PAUSED" if config.paused else "▶ RUNNING"

        stats_text = (
            f"{status}  |  Proc: {self.stats['processed']}  |  "
            f"Live: {self.stats['responsive']}  |  New: {self.stats['new']}  |  Queue: {self.stats['queue']}"
        )
        stats_panel = Panel(Align.center(stats_text), title="📊 Stats", border_style="green", box=box.SIMPLE)
        left["stats"].update(stats_panel)

        # Tech table
        table = Table(show_header=True, header_style="bold magenta", box=box.SIMPLE)
        table.add_column("Technology", style="cyan")
        table.add_column("Domains", justify="right", style="yellow")
        sorted_techs = sorted(self.stats["tech_counts"].items(), key=lambda x: x[1], reverse=True)
        for tech, count in sorted_techs[:15]:
            table.add_row(tech, str(count))
        if not sorted_techs:
            table.add_row("(waiting)", "0")
        table_panel = Panel(table, title="🏷️ Technologies", border_style="blue", box=box.SIMPLE)
        left["table"].update(table_panel)

        # Log panel
        log_text = "\n".join(self.logs[-config.log_limit:]) if self.logs else "No activity yet..."
        log_panel = Panel(
            Align.left(log_text),
            title="📋 Activity Log",
            border_style="yellow",
            box=box.SIMPLE
        )
        body["left"].update(left)
        body["right"].update(log_panel)
        layout["body"].update(body)

        # Footer
        footer = Panel(
            Align.center("[bold]start | pause | resume | config | status | exit[/]"),
            border_style="grey50",
            box=box.SIMPLE
        )
        layout["footer"].update(footer)
        return layout

    async def run_ui(self):
        with Live(self.render(), console=self.console, refresh_per_second=2, screen=True) as live:
            while self.running:
                if not self.paused_for_input:
                    live.update(self.render())
                await asyncio.sleep(0.5)

# ==================== COMMAND HANDLER ====================
class CommandHandler:
    def __init__(self, ui: LiveUI, producer: DomainProducer, collector):
        self.ui = ui
        self.producer = producer
        self.collector = collector
        self.running = True

    async def handle_command(self, cmd: str):
        cmd = cmd.strip().lower()
        if not cmd:
            return

        if cmd == "start":
            if self.ui.awaiting_start:
                self.ui.awaiting_start = False
                self.ui.add_log("▶️ Started processing")
            else:
                self.ui.add_log("Already running")
        elif cmd == "pause":
            if not self.ui.awaiting_start:
                if not config.paused:
                    config.paused = True
                    self.ui.add_log("⏸️ Paused")
                else:
                    self.ui.add_log("Already paused")
            else:
                self.ui.add_log("Not started yet")
        elif cmd == "resume":
            if config.paused:
                config.paused = False
                self.ui.add_log("▶️ Resumed")
            else:
                self.ui.add_log("Not paused")
        elif cmd == "config":
            # Temporarily stop UI updates while showing prompts
            self.ui.paused_for_input = True
            self.ui.console.print("\n[bold cyan]Configuration Menu[/] (press Enter to keep current)")
            new_concurrency = Prompt.ask(f"Concurrency (current: {config.concurrency})", default=str(config.concurrency))
            new_interval = Prompt.ask(f"Fetch interval (s) (current: {config.fetch_interval})", default=str(config.fetch_interval))
            new_timeout = Prompt.ask(f"Timeout (s) (current: {config.http_timeout})", default=str(config.http_timeout))
            tech_detect = Confirm.ask(f"Tech detection? (current: {config.tech_detection})", default=config.tech_detection)
            self.ui.paused_for_input = False
            try:
                config.concurrency = int(new_concurrency)
                config.fetch_interval = int(new_interval)
                config.http_timeout = int(new_timeout)
                config.tech_detection = tech_detect
                self.collector.sem = asyncio.Semaphore(config.concurrency)
                self.ui.add_log(f"Config updated: concurrency={config.concurrency}, interval={config.fetch_interval}, timeout={config.http_timeout}, tech_detection={config.tech_detection}")
                self.ui.console.print("[green]✅ Configuration updated[/]")
            except ValueError:
                self.ui.console.print("[red]Invalid input, numbers required.[/]")
        elif cmd == "status":
            self.ui.console.print(f"""
[bold]Current Configuration[/]
  Concurrency:       {config.concurrency}
  Fetch interval:    {config.fetch_interval}s
  HTTP timeout:      {config.http_timeout}s
  Tech detection:    {config.tech_detection}
  Paused:            {config.paused}
  Queue size:        {self.ui.stats['queue']}
  Processed:         {self.ui.stats['processed']}
  Responsive:        {self.ui.stats['responsive']}
  New domains:       {self.ui.stats['new']}
""")
        elif cmd in ("exit", "quit"):
            self.ui.add_log("Shutting down...")
            self.ui.running = False
            self.running = False
            self.producer.stop()
            self.collector.shutdown()
        else:
            self.ui.add_log(f"Unknown command: {cmd}", color="red")

    async def input_loop(self):
        while self.running and self.ui.running:
            self.ui.paused_for_input = True
            try:
                cmd = await aioconsole.ainput("> ")
            except asyncio.CancelledError:
                break
            finally:
                self.ui.paused_for_input = False
            if not cmd:
                continue
            await self.handle_command(cmd)

# ==================== MAIN COLLECTOR ====================
class DomainCollector:
    def __init__(self):
        self.store = DomainStore()
        self.ui = LiveUI(self.store)
        self.queue = asyncio.Queue(maxsize=config.max_queue_size)
        self.sem = asyncio.Semaphore(config.concurrency)
        self.producer = DomainProducer(self.store, self.ui, self.queue)
        self.tasks = []

    def shutdown(self):
        self.ui.running = False
        self.producer.stop()
        for t in self.tasks:
            t.cancel()

    async def consumer(self, worker_id: int):
        async with aiohttp.ClientSession(headers={'User-Agent': config.user_agent}) as session:
            while self.ui.running:
                if config.paused or self.ui.awaiting_start:
                    await asyncio.sleep(1)
                    continue
                try:
                    raw, fp = await asyncio.wait_for(self.queue.get(), timeout=1)
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break
                async with self.sem:
                    try:
                        responsive, techs = await check_domain(session, raw)
                        inserted = await self.store.add_domain(raw, fp, responsive, techs)
                        await self.ui.update_stats(responsive, techs, inserted)
                        if inserted:
                            color = "green" if responsive else "red"
                            tech_str = ", ".join(techs) if techs else "none"
                            self.ui.add_log(f"{raw} → {'✓' if responsive else '✗'} {tech_str}", color=color)
                        self.ui.stats["queue"] = self.queue.qsize()
                    except Exception as e:
                        self.ui.add_log(f"Error: {raw} - {e}", color="red")
                    finally:
                        self.queue.task_done()

    async def run(self):
        ui_task = asyncio.create_task(self.ui.run_ui())
        cmd_task = asyncio.create_task(CommandHandler(self.ui, self.producer, self).input_loop())
        producer_task = asyncio.create_task(self.producer.run())
        consumers = [asyncio.create_task(self.consumer(i)) for i in range(config.concurrency)]
        self.tasks = [ui_task, cmd_task, producer_task] + consumers

        self.ui.add_log("Press Enter to start, or type 'pause', 'config', 'exit'")

        try:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        finally:
            for t in self.tasks:
                t.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            self.ui.running = False

# ==================== ENTRY ====================
async def main():
    collector = DomainCollector()
    await collector.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[+] Shutdown complete.")