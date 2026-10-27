"""Fail if the repo names a real customer, person or internal workspace. Run before every push:

    python3 scripts/check_clean.py
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BANNED = re.compile(r"\bMetrics\b|\bMETRICS\b(?!\s+LANGUAGE)|metrics\.com|\bMCP-(RE|CO|L)-|\bMXT\b|\bMOT\b|\bMRE\b|Encino|Real Estate Income"
                    r"|iuswic|fevm|stable_classic|7474643815834226|3a9b160|1632828397626749|noah|solomon|@databricks\.com"
                    r"|Tony|Butler|Jalaty|Salvatore|Louisa|dbc-[0-9a-f]")
TEXT_SKIP = {".pdf", ".docx", ".png", ".jpg", ".svg", ".woff", ".woff2", ".whl", ".gz", ".zip", ".ico"}
hits = []
for f in sorted(p for p in ROOT.rglob("*") if p.is_file() and not {".git", ".databricks", "node_modules", "__pycache__"} & set(p.parts)
                and p.name != "check_clean.py"):
    if f.suffix.lower() == ".pdf":
        text = subprocess.run(["pdftotext", "-q", str(f), "-"], capture_output=True, text=True).stdout
    elif f.suffix.lower() == ".docx":
        text = subprocess.run(["unzip", "-p", str(f)], capture_output=True).stdout.decode("utf-8", "ignore")
    elif f.suffix.lower() in TEXT_SKIP and f.suffix.lower() != ".svg":
        continue
    else:
        try:
            text = f.read_text()
        except UnicodeDecodeError:
            continue
    for m in BANNED.finditer(text):
        hits.append(f"{f.relative_to(ROOT)}: {text[max(0, m.start() - 30):m.end() + 30]!r}")
print("\n".join(hits[:50]) or "clean")
sys.exit(1 if hits else 0)
