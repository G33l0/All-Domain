# 🌐 Domain Collector

<p align="center">
  <img src="https://img.shields.io/badge/python-3.9+-blue.svg" alt="Python Version">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License">
  <img src="https://img.shields.io/badge/status-active-brightgreen" alt="Status">
  <img src="https://img.shields.io/badge/tests-111%20passing-brightgreen" alt="Tests">
</p>

> *Autonomous, asynchronous domain intelligence & technology fingerprinting framework*

---

Domain Collector continuously discovers domains from public feeds, checks whether they
answer over HTTP/HTTPS, fingerprints the technology stack behind them, and files the
results into a SQLite database and technology-specific text files — with no duplicates.

It runs either as a **desktop GUI** or **fully headless in a terminal**.

---

## 🎯 What it does

- 🔍 **Discovers** domains from several public feeds (Certificate Transparency via crt.sh,
  the Tranco / Cisco Umbrella / Majestic ranking lists, or your own seed file)
- ✅ **Validates** every candidate (punycode, label rules, TLD sanity, no IPs or wildcards)
  before a single request is made
- 🌐 **Probes** each domain over HTTPS, falling back to HTTP
- 🧬 **Fingerprints** the stack — web servers, CDNs, CMSs, frameworks, JS libraries,
  analytics, security headers — from response headers, cookies, `<meta>` tags and HTML,
  including version numbers where they are exposed
- 📁 **Writes** `output/<technology>.txt` in real time, one domain per line
- 🛡️ **Deduplicates** in memory *and* in SQLite, so a domain is never probed or stored twice
- ⚡ **Scales** with configurable concurrency and a bounded work queue

---

## 📦 Installation

```bash
git clone https://github.com/G33l0/All-Domain.git
cd All-Domain
pip install -r requirements.txt
```

Requirements: **Python 3.9+**, `aiohttp`, `aiosqlite`, `idna`.

The GUI additionally needs Tk, which some Python builds ship separately:

```bash
sudo apt install python3-tk      # Debian / Ubuntu
sudo dnf install python3-tkinter # Fedora
```

Without Tk the collector still runs — it just goes straight to headless mode.

---

## 🚀 Usage

```bash
# Desktop GUI (falls back to headless if Tk is missing)
python domain_collector.py

# Headless, runs until you press Ctrl+C
python domain_collector.py --headless

# One fetch cycle of 100 domains, 50 at a time, then exit
python domain_collector.py --headless --once --limit 100 --concurrency 50

# Feed it your own list instead of the public sources
python domain_collector.py --headless --sources file --seed-file my-domains.txt

# Inspect what has been collected so far
python domain_collector.py --stats
python domain_collector.py --export WordPress > wordpress-sites.txt
```

`Ctrl+C` stops gracefully: in-flight probes finish, buffered rows are flushed, the
database and HTTP sessions are closed. Press it twice to force an immediate exit.

### Command line options

| Option | Description |
|---|---|
| `--headless`, `--no-gui` | Run in the terminal instead of opening the GUI |
| `--once` / `--cycles N` | Stop after one / N fetch cycles |
| `--concurrency N` | Simultaneous probes (default 20) |
| `--timeout SECONDS` | Per-request timeout (default 10) |
| `--interval SECONDS` | Delay between fetch cycles (default 1800) |
| `--limit N` | Max domains queued per cycle (default 500) |
| `--sources LIST` | `crtsh`, `tranco`, `umbrella`, `majestic`, `file` (comma separated) |
| `--seed-file PATH` | Local file of candidate domains, one per line |
| `--db PATH` / `--output DIR` | Database and technology-file locations |
| `--responsive-only` | Store only domains that answered |
| `--no-tech-files` | Database only, skip `output/*.txt` |
| `--verify-ssl` | Verify TLS certificates (off by default — many live hosts have broken chains) |
| `--use-builtwith` | Additionally run the optional legacy `builtwith` package |
| `--stats` / `--export TECH` | Report on the database, then exit |
| `--save-config` | Write the resulting settings to the config file and exit |
| `-c`, `--config PATH` | Config file to use (default `config.json`) |

---

## ⚙️ Configuration

Settings live in `config.json` (created by `--save-config` or by the GUI's **Settings**
dialog). Command line flags override the file for a single run.

| Key | Default | Description |
|---|---|---|
| `concurrency` | `20` | Simultaneous HTTP probes |
| `http_timeout` | `10` | Timeout in seconds for each request |
| `fetch_interval` | `1800` | Seconds between fetch cycles |
| `max_queue_size` | `10000` | Bounded in-memory work queue |
| `max_domains_per_cycle` | `500` | How many candidates to queue per cycle |
| `max_body_bytes` | `262144` | Hard cap on how much HTML is downloaded per domain |
| `db_path` | `domains.db` | SQLite database |
| `output_dir` | `output` | Technology text files |
| `cache_dir` | `.cache` | Cached ranking lists and feed positions |
| `sources` | `["crtsh","tranco","umbrella"]` | Enabled feeds, tried in order |
| `seed_file` | `null` | Optional local candidate list |
| `verify_ssl` | `false` | Verify TLS certificates |
| `write_tech_files` | `true` | Write `output/<technology>.txt` |
| `store_unresponsive` | `true` | Keep unreachable domains in the database |
| `use_builtwith` | `false` | Also run the optional `builtwith` package |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

Every value is range-checked on load — a bad config is reported clearly instead of
crashing the collector mid-run.

---

## 📁 Output

```
All-Domain/
├── domains.db          # every domain, with status, versions, timings and source
├── output/             # one file per detected technology
│   ├── Nginx.txt
│   ├── WordPress.txt
│   └── React.txt
├── .cache/             # cached ranking lists + per-source read positions
└── config.json         # your settings
```

The database keeps more than the text files do:

```sql
-- domains:     fingerprint, raw, first_seen, responsive, technologies (JSON),
--              status_code, scheme, error, elapsed_ms, source, checked_at
-- domain_tech: fingerprint, technology, version
SELECT technology, COUNT(*) FROM domain_tech GROUP BY technology ORDER BY 2 DESC;
```

Databases written by version 1.x are migrated automatically on first open.

---

## 🧠 How it works

1. **Producer** asks each configured source for candidates. A source that fails
   (crt.sh regularly returns HTTP 502) is put on an escalating cooldown while the
   others carry on — one dead feed never stops the run.
2. Candidates are **normalised** (`https://Example.COM:8443/x` → `example.com`,
   `*.a.b.com` → `a.b.com`, IDN → punycode) and rejected if they are not real domains.
3. Each new fingerprint is **reserved** in memory and pushed onto a bounded queue.
4. **Consumers** probe HTTPS, then HTTP, reading at most `max_body_bytes` of the body,
   and fingerprint the response headers, cookies, meta tags and HTML.
5. Results are **batched** into SQLite (WAL mode, `executemany`) and appended to the
   technology files.
6. The producer sleeps for `fetch_interval` and goes again — interruptibly.

The ranking lists are cached on disk for 24 hours and read a window at a time, so each
cycle brings *new* domains rather than re-probing the same first N entries.

---

## 🖥️ The GUI

- Start / Pause / Resume / Stop, with buttons that enable and disable to match the state
- Live counters: processed, responsive, unreachable, new, queue depth, probes per second
- Live technology table and a colour-coded activity log (capped so it cannot eat memory)
- **Settings** dialog that validates input and persists to `config.json`
- **Export** button for the activity log
- Closing the window stops collection cleanly instead of killing it mid-write

The engine runs on its own event loop in a worker thread and communicates with Tk through
a queue — no widget is ever touched from a background thread.

---

## 🧪 Tests

```bash
pip install pytest pytest-asyncio
python -m pytest
```

111 tests cover domain normalisation, technology detection, the store (including 1.x
database migration), source parsing and caching, the threaded runner, the CLI, and a full
producer/consumer cycle against a real local HTTP server.

---

## 🛡️ Ethical considerations

- **Public sources only.** Certificate Transparency logs and published ranking lists.
- **Be polite.** `concurrency` is yours to tune; `limit_per_host` is capped at 4 and each
  domain is requested at most twice (HTTPS, then HTTP). No content scraping, no crawling
  beyond the landing page, no brute forcing.
- **Legal use.** Security research, technology-adoption analysis, and public dataset
  building. Do not use it to attack or probe systems you are not authorised to test.

---

## 🤝 Contributing

Adding a source is a subclass of `Source` in `domaincollector/sources.py` plus an entry in
`SOURCE_CLASSES`. Adding a fingerprint is one `Rule` in `domaincollector/tech.py`. Please
run `python -m pytest` before opening a pull request.

---

## 📄 License

MIT — see [LICENSE](LICENSE).

---

Happy Recon! – IamG2ont
