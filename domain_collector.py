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
from datetime import datetime
from typing import List, Optional, Dict, Set
from collections import defaultdict

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

# ==================== CONFIGURATION (mutable) ====================
class Config:
    def __init__(self):
        self.concurrency = 30
        self.http_timeout = 10
        self.fetch_interval = 3600
        self.user_agent = "Mozilla/5.0 (compatible; DomainRecon/2.0)"
        self.max_queue_size = 10000
        self.log_limit = 20
        self.paused = False
        self.tech_detection = True

    def update(self, **kwargs):
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)

config = Config()

# ==================== UTILITIES ====================
def domain_fingerprint(raw: str) -> str:
    if '://' in raw:
        raw = raw.split('://')[1]
    raw = raw.split('/')[0].split(':')[0]
    try:
        raw = idna.encode(raw).decode('ascii')
    except idna.IDNAError:
        raw = raw.lower()
    return raw.rstrip('.').lower()

# ==================== DATABASE & FILE MANAGER ====================
class DomainStore:
    def __init__(self):
        self._seen: Set[str] = set()
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        self._init_db_sync()

    def _init_db_sync(self):
        import sqlite3
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
async def check_domain(session: aiohttp.ClientSession, domain: str) -> tuple[bool, List[str]]:
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
                        for category, items in detected.items():
                            techs.extend(items)
                    if techs:
                        break
        except:
            continue
    return responsive, list(set(techs))

# ==================== DOMAIN SOURCES ====================
async def fetch_crt(session: aiohttp.ClientSession) -> List[str]:
    url = "https://crt.sh/?q=%.&output=json&excluded=expired&limit=10000"
    try:
        async with session.get(url, timeout=30) as resp:
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
            return []
    except Exception:
        return []

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
        self._banner_text = self._render_banner()
        self.paused_for_input = False  # flag to freeze UI while reading commands

    def _render_banner(self) -> str:
        term_width = shutil.get_terminal_size().columns
        fig = Figlet(font="slant", width=min(term_width, 120))
        title = fig.renderText("ALL-DOMAIN")
        if max(len(l) for l in title.split('\n')) > term_width:
            fig = Figlet(font="small", width=term_width)
            title = fig.renderText("ALL-DOMAIN")
        return title

    def add_log(self, msg: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{timestamp}] {msg}")
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
            Layout(name="header", size=6),
            Layout(name="body", ratio=1),
            Layout(name="footer", size=3)
        )
        header = Panel(
            Align.center(Text(self._banner_text, style="red bold")),
            title="[bold]Domain Intelligence[/]",
            border_style="blue",
            padding=(0, 2)
        )
        layout["header"].update(header)

        body_layout = Layout()
        body_layout.split_row(
            Layout(name="left", ratio=2),
            Layout(name="right", ratio=1)
        )
        left = Layout()
        left.split_column(
            Layout(name="stats", size=5),
            Layout(name="table", ratio=1)
        )
        status = "⏸️ PAUSED" if config.paused else "▶️ RUNNING"
        if self.paused_for_input:
            status = "⌨️ WAITING FOR COMMAND"
        stats_text = (
            f"{status} | "
            f"Processed: {self.stats['processed']} | "
            f"Responsive: {self.stats['responsive']} | "
            f"New: {self.stats['new']} | "
            f"Queue: {self.stats['queue']}"
        )
        stats_panel = Panel(Align.center(stats_text), title="📊 Statistics", border_style="green")
        left["stats"].update(stats_panel)

        table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
        table.add_column("Technology", style="cyan")
        table.add_column("Domains", justify="right", style="yellow")
        sorted_techs = sorted(self.stats["tech_counts"].items(), key=lambda x: x[1], reverse=True)
        for tech, count in sorted_techs[:20]:
            table.add_row(tech, str(count))
        if not sorted_techs:
            table.add_row("(waiting...)", "0")
        table_panel = Panel(table, title="🏷️ Technology Counts", border_style="blue")
        left["table"].update(table_panel)

        log_text = "\n".join(self.logs) if self.logs else "No activity yet..."
        log_panel = Panel(
            Align.left(log_text),
            title="📋 Recent Activity",
            border_style="yellow",
            height=shutil.get_terminal_size().lines - 10
        )
        body_layout["left"].update(left)
        body_layout["right"].update(log_panel)
        layout["body"].update(body_layout)

        footer = Panel(
            Align.center("[bold]Commands: help | config | pause | resume | status[/]"),
            border_style="grey50"
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
    def __init__(self, ui: LiveUI, collector):
        self.ui = ui
        self.collector = collector
        self.running = True

    async def handle_command(self, cmd: str):
        cmd = cmd.strip().lower()
        if cmd == "help":
            self.ui.console.print(Markdown("""
**Available commands:**
- `config`  – change settings (concurrency, interval, timeout, tech detection)
- `pause`   – pause processing
- `resume`  – resume processing
- `status`  – show current configuration
- `help`    – show this help
- `quit`    – stop the tool
"""))
        elif cmd == "config":
            self.ui.console.print("\n[bold cyan]Configuration Menu[/] (press Enter to keep current value)")
            new_concurrency = Prompt.ask(f"Concurrency (current: {config.concurrency})", default=str(config.concurrency))
            new_interval = Prompt.ask(f"Fetch interval (seconds) (current: {config.fetch_interval})", default=str(config.fetch_interval))
            new_timeout = Prompt.ask(f"HTTP timeout (seconds) (current: {config.http_timeout})", default=str(config.http_timeout))
            tech_detect = Confirm.ask(f"Enable technology detection? (current: {config.tech_detection})", default=config.tech_detection)

            try:
                config.concurrency = int(new_concurrency)
                config.fetch_interval = int(new_interval)
                config.http_timeout = int(new_timeout)
                config.tech_detection = tech_detect
                self.collector.sem = asyncio.Semaphore(config.concurrency)
                self.ui.add_log(f"Configuration updated: concurrency={config.concurrency}, interval={config.fetch_interval}, timeout={config.http_timeout}, tech_detection={config.tech_detection}")
                self.ui.console.print("[green]Configuration updated successfully![/]")
            except ValueError:
                self.ui.console.print("[red]Invalid input. Please enter numbers.[/]")
        elif cmd == "pause":
            if not config.paused:
                config.paused = True
                self.ui.add_log("⏸️ Processing paused")
                self.ui.console.print("[yellow]Paused.[/]")
            else:
                self.ui.console.print("[yellow]Already paused.[/]")
        elif cmd == "resume":
            if config.paused:
                config.paused = False
                self.ui.add_log("▶️ Processing resumed")
                self.ui.console.print("[green]Resumed.[/]")
            else:
                self.ui.console.print("[yellow]Already running.[/]")
        elif cmd == "status":
            self.ui.console.print(f"""
[bold]Current Configuration:[/]
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
        elif cmd == "quit":
            self.ui.running = False
            self.running = False
            self.collector.shutdown()
        else:
            self.ui.console.print("[red]Unknown command. Type 'help' for available commands.[/]")

    async def input_loop(self):
        while self.running and self.ui.running:
            # Pause UI updates while we read input
            self.ui.paused_for_input = True
            try:
                cmd = await aioconsole.ainput("> ")  # shows prompt at bottom
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
        self.tasks = []

    def shutdown(self):
        self.ui.running = False
        for t in self.tasks:
            t.cancel()

    async def producer(self):
        async with aiohttp.ClientSession(headers={'User-Agent': config.user_agent}) as session:
            while self.ui.running:
                if not config.paused:
                    try:
                        domains = await fetch_crt(session)
                        self.ui.add_log(f"Fetched {len(domains)} domains from crt.sh")
                        for d in domains:
                            fp = domain_fingerprint(d)
                            if await self.store.is_new(fp):
                                await self.queue.put((d, fp))
                        self.ui.stats["queue"] = self.queue.qsize()
                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        self.ui.add_log(f"Producer error: {e}")
                for _ in range(config.fetch_interval):
                    if not self.ui.running:
                        break
                    await asyncio.sleep(1)

    async def consumer(self, worker_id: int):
        async with aiohttp.ClientSession(headers={'User-Agent': config.user_agent}) as session:
            while self.ui.running:
                if config.paused:
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
                        if inserted and responsive:
                            tech_str = ", ".join(techs) if techs else "unknown"
                            self.ui.add_log(f"✔ {raw} → {tech_str}")
                        self.ui.stats["queue"] = self.queue.qsize()
                    except Exception as e:
                        self.ui.add_log(f"Error processing {raw}: {e}")
                    finally:
                        self.queue.task_done()

    async def run(self):
        ui_task = asyncio.create_task(self.ui.run_ui())
        cmd_handler = CommandHandler(self.ui, self)
        input_task = asyncio.create_task(cmd_handler.input_loop())
        producer_task = asyncio.create_task(self.producer())
        consumers = [asyncio.create_task(self.consumer(i)) for i in range(config.concurrency // 2)]
        self.tasks = [ui_task, input_task, producer_task] + consumers

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