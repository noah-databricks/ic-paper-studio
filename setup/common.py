"""Shared settings for the setup steps. They run as tasks of the bundle's setup job (catalog, schema and warehouse passed
as --catalog/--schema/--warehouse), or locally with a CLI profile (DATABRICKS_CONFIG_PROFILE plus the same flags)."""
import argparse
import os
import sys
import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

_p = argparse.ArgumentParser(add_help=False)
_p.add_argument("--catalog", default=os.getenv("UC_CATALOG"))
_p.add_argument("--schema", default=os.getenv("UC_SCHEMA", "ic_paper_studio"))
_p.add_argument("--warehouse", default=os.getenv("WAREHOUSE_ID"))
_p.add_argument("--app")                      # the app's name (grants)
_p.add_argument("--endpoint", default="ic-paper-search")  # AI Search endpoint (index)
_p.add_argument("--experiment")               # MLflow experiment id (traces)
_p.add_argument("--root")                     # the repo (bundle files) root; defaults to this file's parent
ARGS, _rest = _p.parse_known_args()
if not ARGS.catalog or not ARGS.warehouse:
    sys.exit("pass --catalog and --warehouse (or set UC_CATALOG and WAREHOUSE_ID)")
CATALOG, SCHEMA, WAREHOUSE = ARGS.catalog, ARGS.schema, ARGS.warehouse
FQ = f"{CATALOG}.{SCHEMA}"
VOL = f"/Volumes/{CATALOG}/{SCHEMA}"
ROOT = __import__("pathlib").Path(ARGS.root) if ARGS.root else __import__("pathlib").Path(__file__).resolve().parents[1]
VOLUMES = ["library", "parsed", "raw", "papers", "agent_state", "uploads"]

w = WorkspaceClient(profile=os.environ["DATABRICKS_CONFIG_PROFILE"]) if os.getenv("DATABRICKS_CONFIG_PROFILE") else WorkspaceClient()


def sql(statement: str, timeout_s: int = 900):
    """Run one statement on the warehouse and return (columns, rows); waits for long ones."""
    r = w.statement_execution.execute_statement(warehouse_id=WAREHOUSE, statement=statement, wait_timeout="50s")
    end = time.time() + timeout_s
    while r.status.state in (StatementState.PENDING, StatementState.RUNNING) and time.time() < end:
        time.sleep(3)
        r = w.statement_execution.get_statement(r.statement_id)
    if r.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(f"{r.status.state}: {r.status.error.message if r.status.error else ''}\n{statement[:400]}")
    cols = [c.name for c in (r.manifest.schema.columns or [])] if r.manifest and r.manifest.schema else []
    rows = list(r.result.data_array or []) if r.result else []
    chunk = r.result.next_chunk_index if r.result else None
    while chunk is not None:
        c = w.statement_execution.get_statement_result_chunk_n(r.statement_id, chunk)
        rows += c.data_array or []
        chunk = c.next_chunk_index
    return cols, rows
