#!/usr/bin/env python3
"""
Domain Intelligence & Recon Framework
Version 1.0.0
Coded by IamG2

Continuously discovers domains, checks responsiveness,
detects technologies, and saves to per‑tech files.
"""
import asyncio
import aiohttp
import sqlite3
import os
import sys
import hashlib
import idna
import shutil
import time
from pyfiglet import Figlet
from colorama import Fore, Style, init
from builtwith import builtwith  # technology detection
from urllib.parse import urlparse
import json
from typing import Optional, Set, List, Dict
import logging
from datetime import datetime

# ------------------------- Configuration -------------------------
CONCURRENCY = 50                # concurrent HTTP checks
HTTP_TIMEOUT = 10               # seconds
USER_AGENT = "Mozilla/5.0 (compatible; DomainRecon/1.0)"
SOURCES = [
    "crt.sh",                  # Certificate Transparency logs
    # "commoncrawl",           # placeholder for other sources
    # "zonedata",
]
DB_PATH = "domains.db"
OUTPUT_DIR = "output"
FETCH_INTERVAL = 3600          # seconds between full source fetches
MAX_QUEUE_SIZE = 10000

# ------------------------- Banner -------------------------
init(autoreset=True)

def clear():
    os.system('cls' if os.name == 'nt' else 'clear')

def banner():
    clear()
    term_width = shutil.get_terminal_size().columns
    # Choose font based on width
    fig = Figlet(font='slant', width=min(term_width, 120))
    title = fig.renderText("ALL-DOMAIN")
    # If title too wide, fallback to 'small'
    if max(len(line) for line in title.split('\n')) > term_width:
        fig = Figlet(font='small', width=term_width)
        title = fig.renderText("ALL-DOMAIN")
    print(f"{Fore.RED}{title}{Style.RESET_ALL}")

    box_width = min(term_width - 4, 80)
    border = "╔" + "═" * (box_width - 2) + "╗"
    print(border)
    print(f"║{'Domain Intelligence & Recon Framework'.center(box_width-2)}║")
    print(f"║{'Version 1.0.0'.center(box_width-2)}║")
    print(f"║{'Coded by IamG2'.center(box_width-2)}║")
    print("╚" + "═" * (box_width - 2) + "╝")

# ------------------------- Utilities -------------------------
def domain_fingerprint(raw: str) -> str:
    """Canonical fingerprint for deduplication."""
    # Strip protocol, path, port, etc.
    if '://' in raw:
        raw = raw.split('://')[1]
    raw = raw.split('/')[0].split(':')[0]
    # Convert to punycode and lowercase
    try:
        raw = idna.encode(raw).decode('ascii')
    except idna.IDNAError:
        raw = raw.lower()
    raw = raw.rstrip('.')
    return raw.lower()

# ------------------------- Domain Manager (DB + Files) -------------------------
class DomainManager:
    def __init__(self):
        self._seen: Set[str] = set()
        self._db_path = DB_PATH
        self._output_dir = OUTPUT_DIR
        os.makedirs(self._output_dir, exist_ok=True)
        self._init_db()
        self._load_seen()

    def _init_db(self):
        with sqlite3.connect(self._db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS domains (
                    fingerprint TEXT PRIMARY KEY,
                    raw TEXT NOT NULL,
                    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    responsive BOOLEAN DEFAULT 0,
                    technologies TEXT   -- JSON array
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_fingerprint ON domains(fingerprint)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_responsive ON domains(responsive)')
            conn.commit()

    def _load_seen(self):
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute('SELECT fingerprint FROM domains')
            for row in cur:
                self._seen.add(row[0])

    def is_new(self, fingerprint: str) -> bool:
        return fingerprint not in self._seen

    def add_domain(self, raw: str, fingerprint: str, responsive: bool = False,
                   technologies: Optional[List[str]] = None):
        """Insert a new domain; return True if inserted, False if duplicate."""
        if fingerprint in self._seen:
            return False
        tech_json = json.dumps(technologies) if technologies else None
        with sqlite3.connect(self._db_path) as conn:
            try:
                conn.execute(
                    'INSERT INTO domains (fingerprint, raw, responsive, technologies) VALUES (?, ?, ?, ?)',
                    (fingerprint, raw, 1 if responsive else 0, tech_json)
                )
                conn.commit()
                self._seen.add(fingerprint)
                # Write to technology files
                if responsive and technologies:
                    self._write_tech_files(raw, technologies)
                return True
            except sqlite3.IntegrityError:
                return False

    def _write_tech_files(self, domain: str, technologies: List[str]):
        """Append domain to each technology's file."""
        for tech in technologies:
            # Sanitize tech name for filename
            safe_tech = "".join(c for c in tech if c.isalnum() or c in (' ', '-', '_')).strip()
            if not safe_tech:
                safe_tech = "unknown"
            filepath = os.path.join(self._output_dir, f"{safe_tech}.txt")
            # Append with newline
            with open(filepath, 'a', encoding='utf-8') as f:
                f.write(domain + '\n')

# ------------------------- HTTP Checker & Tech Detection -------------------------
async def check_domain(session: aiohttp.ClientSession, domain: str) -> tuple[bool, List[str]]:
    """
    Attempts HTTP and HTTPS, returns (responsive, list_of_technologies).
    Uses builtwith for detection.
    """
    technologies = []
    responsive = False
    for scheme in ('https', 'http'):
        url = f"{scheme}://{domain}"
        try:
            async with session.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True) as resp:
                if resp.status < 400:  # treat as responsive
                    responsive = True
                    # Get HTML content for builtwith (only if we haven't already)
                    # builtwith usually needs HTML; we'll read body (limited size)
                    html = await resp.text(errors='ignore', limit=200_000)  # 200KB enough
                    # Detect technologies using builtwith
                    techs = builtwith(html, url=url)
                    # builtwith returns dict like {'javascript-frameworks': ['React'], ...}
                    for category, items in techs.items():
                        technologies.extend(items)
                    # If we got techs, break (we have info)
                    if technologies:
                        break
        except (asyncio.TimeoutError, aiohttp.ClientError, UnicodeError):
            continue
    return responsive, list(set(technologies))  # deduplicate techs

# ------------------------- Domain Fetchers (Sources) -------------------------
async def fetch_from_crt(session: aiohttp.ClientSession) -> List[str]:
    """Fetch domains from crt.sh (Certificate Transparency)."""
    # Use a query that returns a large sample (limit 10000)
    url = "https://crt.sh/?q=%.&output=json&excluded=expired&limit=10000"
    try:
        async with session.get(url, timeout=30) as resp:
            if resp.status == 200:
                data = await resp.json()
                domains = set()
                for entry in data:
                    name = entry.get('name_value')
                    if name:
                        # crt.sh sometimes returns multiple names separated by newline
                        for d in name.split('\n'):
                            d = d.strip().lower()
                            if d and not d.startswith('*.'):  # skip wildcards
                                domains.add(d)
                return list(domains)
    except Exception as e:
        logging.warning(f"crt.sh fetch error: {e}")
    return []

# ------------------------- Main Orchestrator -------------------------
class DomainCollector:
    def __init__(self):
        self.manager = DomainManager()
        self.queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        self.sem = asyncio.Semaphore(CONCURRENCY)
        self.stats = {"processed": 0, "responsive": 0, "new": 0}

    async def producer(self):
        """Periodically fetch domains from sources and enqueue."""
        async with aiohttp.ClientSession(headers={'User-Agent': USER_AGENT}) as session:
            while True:
                try:
                    # Fetch from crt.sh
                    domains = await fetch_from_crt(session)
                    logging.info(f"Fetched {len(domains)} domains from crt.sh")
                    # Add to queue
                    for d in domains:
                        fp = domain_fingerprint(d)
                        if self.manager.is_new(fp):
                            await self.queue.put((d, fp))
                    # Additional sources can be added here
                except Exception as e:
                    logging.error(f"Producer error: {e}")
                await asyncio.sleep(FETCH_INTERVAL)

    async def consumer(self):
        """Process domains from queue: check, detect tech, save."""
        async with aiohttp.ClientSession(headers={'User-Agent': USER_AGENT}) as session:
            while True:
                raw, fp = await self.queue.get()
                async with self.sem:
                    try:
                        # Check responsiveness and detect tech
                        responsive, techs = await check_domain(session, raw)
                        # Save to DB and files
                        inserted = self.manager.add_domain(raw, fp, responsive, techs)
                        if inserted:
                            self.stats["new"] += 1
                        self.stats["processed"] += 1
                        if responsive:
                            self.stats["responsive"] += 1
                        # Log every 1000 processed
                        if self.stats["processed"] % 1000 == 0:
                            logging.info(
                                f"Stats: processed={self.stats['processed']}, "
                                f"new={self.stats['new']}, responsive={self.stats['responsive']}"
                            )
                    except Exception as e:
                        logging.error(f"Error processing {raw}: {e}")
                    finally:
                        self.queue.task_done()

    async def run(self):
        """Start producer and multiple consumers."""
        # Start producer
        producer_task = asyncio.create_task(self.producer())
        # Start consumers
        consumer_tasks = [asyncio.create_task(self.consumer()) for _ in range(CONCURRENCY // 2)]
        # Wait for keyboard interrupt
        try:
            await asyncio.gather(producer_task, *consumer_tasks)
        except KeyboardInterrupt:
            logging.info("Shutting down...")
            # Cancel tasks
            producer_task.cancel()
            for t in consumer_tasks:
                t.cancel()
            await asyncio.gather(*consumer_tasks, return_exceptions=True)

# ------------------------- Entry Point -------------------------
async def main():
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler()]
    )
    banner()
    print(f"{Fore.GREEN}[*] Starting Domain Collector...")
    print(f"{Fore.YELLOW}[!] Press Ctrl+C to stop")
    collector = DomainCollector()
    await collector.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[+] Shutdown complete.")
