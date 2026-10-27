"""Create the schema objects, upload the document library, load the deal tables and create the metric views."""
import os as _os
import sys as _sys
_ROOT = next((_sys.argv[i + 1] for i, a in enumerate(_sys.argv[:-1]) if a == "--root"), None)  # job tasks have no __file__
_sys.path.insert(0, _os.path.join(_ROOT, "setup") if _ROOT else _os.path.dirname(_os.path.abspath(__file__)))
import io
import os
from pathlib import Path

from common import FQ, VOL, VOLUMES, sql, w

from common import ROOT
OUT = ROOT / "content"

sql(f"CREATE SCHEMA IF NOT EXISTS {FQ} COMMENT 'IC Paper Studio demo (synthetic data)'")
for v in VOLUMES:
    sql(f"CREATE VOLUME IF NOT EXISTS {FQ}.{v}")

# the document library: one place for every source, organised by where it came from (data rooms, policy, research,
# past papers), not by deal. The agent retrieves from all of it.
LIBRARY = Path(os.getenv("LIBRARY_DIR", ROOT / "content" / "library"))
local = {str(p.relative_to(LIBRARY)): p for p in sorted(LIBRARY.rglob("*.pdf"))}
for rel, pdf in local.items():
    w.files.upload(f"{VOL}/library/{rel}", io.BytesIO(pdf.read_bytes()), overwrite=True)
print("uploaded", len(local), "library documents")


def walk(d):
    for f in w.files.list_directory_contents(d):
        yield from walk(f.path) if f.is_directory else [f.path]


for path in walk(f"{VOL}/library"):  # files removed from the library locally leave it here too
    if path.split("/library/", 1)[1] not in local:
        w.files.delete(path)
        print("removed", path)

COMMENTS = {
    "deals": "Deal records from the origination system (origination-system extract). Synthetic.",
    "facilities": "Facility terms per deal. Synthetic.",
    "covenants": "Financial covenants with current values and headroom. Synthetic.",
    "drawdown_schedule": "Monthly construction drawdown schedule. Synthetic.",
    "cashflow_projection": "Monthly project cashflow projection, construction and lease-up. Synthetic.",
    "portfolio_exposure": "Quillwood loan book exposure by loan, including pipeline deals. Synthetic.",
}
for csv in sorted((OUT / "structured").glob("*.csv")):
    t = csv.stem
    w.files.upload(f"{VOL}/raw/{csv.name}", io.BytesIO(csv.read_bytes()), overwrite=True)
    sql(f"""CREATE OR REPLACE TABLE {FQ}.{t} COMMENT '{COMMENTS.get(t, "Synthetic")}' AS
            SELECT * FROM read_files('{VOL}/raw/{csv.name}', format => 'csv', header => true, inferSchema => true)""")
    print("table", t, sql(f"SELECT count(*) FROM {FQ}.{t}")[1][0][0])

catalog, schema = FQ.split(".")
text = (OUT / "structured" / "metric_views.sql").read_text().replace("${catalog}", catalog).replace("${schema}", schema)
print("metric views file has", text.count("CREATE OR REPLACE VIEW"), "views")
for stmt in [s.strip() for s in text.split("$$;") if "CREATE" in s]:
    sql(stmt + "$$")
    print("metric view", stmt.split("VIEW", 1)[1].split()[0])
