"""Keep the app's agent traces in Unity Catalog: link the MLflow experiment to the demo schema, which creates the trace
tables there (mlflow_experiment_trace_otel_*). Runs before the grants so the app can write to them."""
import os as _os
import sys as _sys
_ROOT = next((_sys.argv[i + 1] for i, a in enumerate(_sys.argv[:-1]) if a == "--root"), None)  # job tasks have no __file__
_sys.path.insert(0, _os.path.join(_ROOT, "setup") if _ROOT else _os.path.dirname(_os.path.abspath(__file__)))
import os

import mlflow
from mlflow.entities.trace_location import UnityCatalog

from common import ARGS, CATALOG, SCHEMA, WAREHOUSE

if not ARGS.experiment:
    raise SystemExit("pass --experiment <id>")
os.environ["MLFLOW_TRACING_SQL_WAREHOUSE_ID"] = WAREHOUSE
mlflow.set_tracking_uri("databricks")
name = mlflow.get_experiment(ARGS.experiment).name
try:
    mlflow.set_experiment(experiment_name=name, trace_location=UnityCatalog(catalog_name=CATALOG, schema_name=SCHEMA,
                                                                              table_prefix="mlflow_experiment_trace"))  # the names the app and grants use
    print("linked", name, "to", f"{CATALOG}.{SCHEMA}")
except Exception as e:  # already linked on an earlier run
    if "already" not in str(e).lower():
        raise
    print("already linked:", name)
