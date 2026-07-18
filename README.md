🌐 Domain Collector

<p align="center">
  <img src="https://img.shields.io/badge/python-3.8+-blue.svg" alt="Python Version">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License">
  <img src="https://img.shields.io/badge/status-active-brightgreen" alt="Status">
  <img src="https://img.shields.io/badge/contributions-welcome-orange.svg" alt="Contributions Welcome">
</p>

Autonomous, asynchronous domain intelligence & technology fingerprinting framework

---

Domain Collector continuously discovers live domains from public sources, checks their responsiveness, detects their technology stack, and organises them into technology-specific files – all with zero duplication.

---

🎯 What it does

· 🔍 Discovers domains from public feeds (Certificate Transparency logs, DNS zones, etc.)
· ✅ Validates each domain via HTTP/HTTPS (drops unreachable ones)
· 🧬 Fingerprints the technology stack (web servers, frameworks, CMS, analytics, etc.)
· 📁 Saves results into output/<technology>.txt – one domain per line, in real‑time
· 🛡️ Deduplicates using cryptographic-style fingerprints – no domain ever stored twice
· ⚡ Asynchronous architecture for high‑speed processing with configurable concurrency

---

✨ Features

Feature Description
Continuous operation Runs indefinitely, periodically refreshing from multiple sources
Real‑time output Each responsive domain is immediately written to its technology file(s)
Responsive check Tries both HTTP and HTTPS; marks domains as responsive on 2xx/3xx
Tech detection Uses builtwith to identify frameworks, libraries, servers, and more
Deduplication SQLite backend with UNIQUE fingerprint constraint – guaranteed no dupes
Adaptive banner Professional welcome screen that fits any terminal size
Logging & stats Periodic progress logs (every 1000 domains processed)
Extensible Easily add new domain sources by extending the producer

---

📦 Installation

```bash
# Clone the repository
git clone https://github.com/G33l0/All-Domain.git
cd All-Domain

# Install dependencies
pip install -r requirements.txt
```

Requirements: Python 3.8+ and the packages listed in requirements.txt.

---

🚀 Usage

Start the collector:

```bash
python domain_collector.py
```

Press Ctrl+C at any time to stop gracefully.

---

⚙️ Configuration

All settings are at the top of domain_collector.py – tweak them to your needs, if you got the version work interactive ui expect to set the config within the application:

Variable Default Description
CONCURRENCY 50 Number of simultaneous HTTP checks
HTTP_TIMEOUT 10 Timeout (seconds) for each request
FETCH_INTERVAL 3600 How often to re‑fetch from sources (seconds)
MAX_QUEUE_SIZE 10000 Maximum in‑memory queue size
DB_PATH domains.db SQLite database location
OUTPUT_DIR output Directory for technology files

---

📁 Output Structure

```
domain-collector/
├── domains.db              # SQLite database (all discovered domains)
├── output/                 # Technology‑specific text files
│   ├── React.txt           # Domains using React
│   ├── Nginx.txt           # Domains using Nginx
│   ├── WordPress.txt       # Domains using WordPress
│   └── ...                 # One file per detected technology
└── domain_collector.py     # Main script
```

Each technology file contains one domain per line, appended in real‑time as they are discovered.

---

🛡️ Ethical Considerations

· Respect sources: Only use public, permissive data sources (e.g., Certificate Transparency logs).
· Rate limiting: The tool uses a semaphore to limit concurrent requests; you are responsible for adjusting it to avoid overwhelming target servers.
· Robots.txt: Always obey robots.txt when scraping. This tool does not scrape websites for content – it only checks availability and extracts server headers.
· Legal use: Use this tool only for legitimate purposes, such as security research, technology adoption analysis, or building public datasets. Do not use it to attack, probe without permission, or violate any applicable laws.

---

🧠 How it works (high-level)

1. Producer fetches domains from sources (e.g., crt.sh) and enqueues them.
2. Consumers (workers) take a domain from the queue:
   · Generate a fingerprint (punycode + lowercase + stripped).
   · Check if it already exists in the database – if so, skip.
   · Attempt HTTP and HTTPS connections.
   · If responsive, fetch the HTML (limited size) and run builtwith to detect technologies.
   · Insert the domain, responsiveness flag, and technology list into SQLite.
   · Append the domain to each detected technology’s .txt file.
3. This loop runs forever, with the producer sleeping for FETCH_INTERVAL between cycles.

---

🤝 Contributing

Contributions are welcome! Please:

· Fork the repository.
· Create a feature branch.
· Make your changes (add new sources, improve tech detection, etc.).
· Submit a pull request with a clear description.

For major changes, open an issue first to discuss what you would like to change.

---

📄 License

Distributed under the MIT License. See LICENSE for more information.

---

🙏 Acknowledgements

· crt.sh for providing Certificate Transparency data.
· builtwith for technology fingerprinting.
· pyfiglet for the banner.

---

Happy Recon! – IamG2
