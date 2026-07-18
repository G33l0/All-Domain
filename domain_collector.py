import tkinter as tk
from tkinter import scrolledtext, ttk, messagebox
import threading
import asyncio
import aiohttp
import aiosqlite
import aiofiles
import sqlite3
import os
import json
import idna
from datetime import datetime
from collections import defaultdict
from builtwith import builtwith

# ========== CONFIG ==========
DB_PATH = "domains.db"
OUTPUT_DIR = "output"
CONCURRENCY = 20
HTTP_TIMEOUT = 10
FETCH_INTERVAL = 1800  # seconds
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# ========== CORE LOGIC ==========
def domain_fingerprint(raw):
    raw = raw.split('://')[-1].split('/')[0].split(':')[0]
    try:
        raw = idna.encode(raw).decode('ascii')
    except:
        raw = raw.lower()
    return raw.rstrip('.').lower()

class DomainStore:
    def __init__(self):
        self._seen = set()
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        self._init_db()

    def _init_db(self):
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
            conn.commit()
            cur = conn.execute('SELECT fingerprint FROM domains')
            self._seen = {row[0] for row in cur}

    async def is_new(self, fp):
        return fp not in self._seen

    async def add_domain(self, raw, fp, responsive, techs):
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

    async def _write_tech_files(self, domain, techs):
        for tech in techs:
            safe = "".join(c for c in tech if c.isalnum() or c in (' ', '-', '_')).strip() or "unknown"
            fpath = os.path.join(OUTPUT_DIR, f"{safe}.txt")
            async with aiofiles.open(fpath, 'a', encoding='utf-8') as f:
                await f.write(domain + '\n')

async def check_domain(session, domain):
    techs = []
    responsive = False
    for scheme in ('https', 'http'):
        url = f"{scheme}://{domain}"
        try:
            async with session.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True) as resp:
                if resp.status < 400:
                    responsive = True
                    html = await resp.text(errors='ignore', limit=200_000)
                    detected = builtwith(html, url=url)
                    for _, items in detected.items():
                        techs.extend(items)
                    if techs:
                        break
        except:
            continue
    return responsive, list(set(techs))

async def fetch_crt(session, limit=200):
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
        return []

async def fetch_fallback(session):
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
        return []

# ========== GUI APPLICATION ==========
class DomainCollectorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Domain Collector")
        self.root.geometry("900x700")
        self.root.resizable(True, True)
        self.running = False
        self.paused = False
        self.store = DomainStore()
        self.queue = asyncio.Queue(maxsize=5000)
        self.stats = {"processed": 0, "responsive": 0, "new": 0, "tech_counts": defaultdict(int)}
        self.tasks = []
        self.loop = None

        # UI Layout
        top_frame = tk.Frame(root)
        top_frame.pack(fill=tk.X, padx=10, pady=5)

        tk.Button(top_frame, text="Start", command=self.start, bg="lightgreen", width=10).pack(side=tk.LEFT, padx=5)
        tk.Button(top_frame, text="Pause", command=self.pause, bg="lightyellow", width=10).pack(side=tk.LEFT, padx=5)
        tk.Button(top_frame, text="Resume", command=self.resume, bg="lightblue", width=10).pack(side=tk.LEFT, padx=5)
        tk.Button(top_frame, text="Stop", command=self.stop, bg="lightcoral", width=10).pack(side=tk.LEFT, padx=5)
        tk.Button(top_frame, text="Config", command=self.config_dialog, width=10).pack(side=tk.LEFT, padx=5)

        self.status_label = tk.Label(top_frame, text="Status: Stopped", font=("Arial", 10, "bold"), fg="gray")
        self.status_label.pack(side=tk.RIGHT, padx=10)

        # Stats frame
        stats_frame = tk.Frame(root)
        stats_frame.pack(fill=tk.X, padx=10, pady=5)
        self.stats_labels = {}
        for key in ["Processed", "Responsive", "New", "Queue"]:
            lbl = tk.Label(stats_frame, text=f"{key}: 0", font=("Arial", 10))
            lbl.pack(side=tk.LEFT, padx=10)
            self.stats_labels[key] = lbl

        # Main area: left = tech table, right = log
        main_frame = tk.Frame(root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        main_frame.grid_columnconfigure(0, weight=2)
        main_frame.grid_columnconfigure(1, weight=1)
        main_frame.grid_rowconfigure(0, weight=1)

        # Tech table (left)
        tech_frame = tk.LabelFrame(main_frame, text="Technologies", font=("Arial", 10, "bold"))
        tech_frame.grid(row=0, column=0, sticky="nsew", padx=5)
        self.tech_tree = ttk.Treeview(tech_frame, columns=("count",), show="tree headings", height=20)
        self.tech_tree.heading("#0", text="Technology")
        self.tech_tree.heading("count", text="Domains")
        self.tech_tree.column("#0", width=200)
        self.tech_tree.column("count", width=80, anchor="center")
        scroll_tech = tk.Scrollbar(tech_frame, orient=tk.VERTICAL, command=self.tech_tree.yview)
        self.tech_tree.configure(yscrollcommand=scroll_tech.set)
        self.tech_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll_tech.pack(side=tk.RIGHT, fill=tk.Y)

        # Log (right)
        log_frame = tk.LabelFrame(main_frame, text="Activity Log", font=("Arial", 10, "bold"))
        log_frame.grid(row=0, column=1, sticky="nsew", padx=5)
        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # Footer
        footer = tk.Label(root, text="All domains saved to domains.db and output/*.txt", font=("Arial", 8), fg="gray")
        footer.pack(side=tk.BOTTOM, fill=tk.X, pady=2)

    def log(self, msg, color="black"):
        self.log_text.insert(tk.END, f"{datetime.now().strftime('%H:%M:%S')} - {msg}\n")
        self.log_text.see(tk.END)
        # Optional: color tags (we skip for simplicity)

    def update_stats(self):
        self.stats_labels["Processed"].config(text=f"Processed: {self.stats['processed']}")
        self.stats_labels["Responsive"].config(text=f"Responsive: {self.stats['responsive']}")
        self.stats_labels["New"].config(text=f"New: {self.stats['new']}")
        self.stats_labels["Queue"].config(text=f"Queue: {self.queue.qsize() if hasattr(self, 'queue') else 0}")
        # Update tech tree
        self.tech_tree.delete(*self.tech_tree.get_children())
        for tech, count in sorted(self.stats["tech_counts"].items(), key=lambda x: x[1], reverse=True)[:20]:
            self.tech_tree.insert("", tk.END, text=tech, values=(count,))

    def start(self):
        if self.running:
            return
        self.running = True
        self.paused = False
        self.status_label.config(text="Status: Running", fg="green")
        self.log("Started collection.")
        # Run async loop in a separate thread
        def run_loop():
            asyncio.set_event_loop(asyncio.new_event_loop())
            self.loop = asyncio.get_event_loop()
            self.loop.run_until_complete(self.main_loop())

        threading.Thread(target=run_loop, daemon=True).start()

    def pause(self):
        if not self.running:
            return
        self.paused = True
        self.status_label.config(text="Status: Paused", fg="orange")
        self.log("Paused.")

    def resume(self):
        if not self.running:
            return
        self.paused = False
        self.status_label.config(text="Status: Running", fg="green")
        self.log("Resumed.")

    def stop(self):
        if not self.running:
            return
        self.running = False
        self.status_label.config(text="Status: Stopped", fg="gray")
        self.log("Stopping...")
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)

    def config_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Configuration")
        dialog.geometry("300x250")
        dialog.transient(self.root)
        dialog.grab_set()
        # Concurrency
        tk.Label(dialog, text="Concurrency:").pack(pady=5)
        con_entry = tk.Entry(dialog)
        con_entry.insert(0, str(CONCURRENCY))
        con_entry.pack()
        # Fetch interval
        tk.Label(dialog, text="Fetch interval (seconds):").pack(pady=5)
        int_entry = tk.Entry(dialog)
        int_entry.insert(0, str(FETCH_INTERVAL))
        int_entry.pack()
        # Timeout
        tk.Label(dialog, text="HTTP timeout (seconds):").pack(pady=5)
        to_entry = tk.Entry(dialog)
        to_entry.insert(0, str(HTTP_TIMEOUT))
        to_entry.pack()

        def save_config():
            global CONCURRENCY, FETCH_INTERVAL, HTTP_TIMEOUT
            try:
                CONCURRENCY = int(con_entry.get())
                FETCH_INTERVAL = int(int_entry.get())
                HTTP_TIMEOUT = int(to_entry.get())
                messagebox.showinfo("Success", "Configuration updated.\nRestart to apply changes.")
                dialog.destroy()
            except ValueError:
                messagebox.showerror("Error", "Please enter valid numbers.")

        tk.Button(dialog, text="Save", command=save_config).pack(pady=10)

    # ========== ASYNC CORE ==========
    async def main_loop(self):
        # Producer
        producer_task = asyncio.create_task(self.producer())
        # Consumers
        consumers = [asyncio.create_task(self.consumer(i)) for i in range(CONCURRENCY)]
        self.tasks = [producer_task] + consumers
        try:
            await asyncio.gather(*self.tasks)
        except asyncio.CancelledError:
            pass
        finally:
            for t in self.tasks:
                t.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def producer(self):
        async with aiohttp.ClientSession(headers={'User-Agent': USER_AGENT}) as session:
            while self.running:
                if not self.paused:
                    try:
                        domains = await fetch_crt(session, limit=200)
                        if not domains:
                            self.log("crt.sh empty, using fallback...")
                            domains = await fetch_fallback(session)
                        if domains:
                            self.log(f"Fetched {len(domains)} new domains")
                            for d in domains:
                                fp = domain_fingerprint(d)
                                if await self.store.is_new(fp):
                                    await self.queue.put((d, fp))
                            # Update queue size in stats
                            self.stats["queue"] = self.queue.qsize()
                            self.root.after(0, self.update_stats)
                    except Exception as e:
                        self.log(f"Producer error: {e}")
                # Sleep in small steps for responsiveness
                for _ in range(FETCH_INTERVAL):
                    if not self.running:
                        break
                    await asyncio.sleep(1)

    async def consumer(self, worker_id):
        async with aiohttp.ClientSession(headers={'User-Agent': USER_AGENT}) as session:
            while self.running:
                if self.paused:
                    await asyncio.sleep(1)
                    continue
                try:
                    raw, fp = await asyncio.wait_for(self.queue.get(), timeout=1)
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break
                try:
                    responsive, techs = await check_domain(session, raw)
                    inserted = await self.store.add_domain(raw, fp, responsive, techs)
                    if inserted:
                        self.stats["processed"] += 1
                        if responsive:
                            self.stats["responsive"] += 1
                            for t in techs:
                                self.stats["tech_counts"][t] += 1
                        self.stats["new"] += 1
                        color = "green" if responsive else "red"
                        tech_str = ", ".join(techs) if techs else "none"
                        self.log(f"{raw} → {'✓' if responsive else '✗'} {tech_str}")
                        self.root.after(0, self.update_stats)
                    self.stats["queue"] = self.queue.qsize()
                except Exception as e:
                    self.log(f"Error processing {raw}: {e}")
                finally:
                    self.queue.task_done()

# ========== MAIN ==========
if __name__ == "__main__":
    root = tk.Tk()
    app = DomainCollectorApp(root)
    root.mainloop()