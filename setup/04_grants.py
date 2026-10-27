"""Grant the app's service principal what it needs: read the library and query its search index, write papers, state,
uploads and the library inbox, write agent traces."""
import os as _os
import sys as _sys
_ROOT = next((_sys.argv[i + 1] for i, a in enumerate(_sys.argv[:-1]) if a == "--root"), None)  # job tasks have no __file__
_sys.path.insert(0, _os.path.join(_ROOT, "setup") if _ROOT else _os.path.dirname(_os.path.abspath(__file__)))
from common import ARGS, FQ, sql, w

sp = w.apps.get(ARGS.app or "ic-paper-studio").service_principal_client_id
catalog = FQ.split(".")[0]
for stmt in [
    f"GRANT USE CATALOG ON CATALOG {catalog} TO `{sp}`",
    f"GRANT USE SCHEMA, SELECT ON SCHEMA {FQ} TO `{sp}`",
    f"GRANT READ VOLUME, WRITE VOLUME ON VOLUME {FQ}.library TO `{sp}`",  # analysts' uploads land in library/inbox
    f"GRANT READ VOLUME ON VOLUME {FQ}.parsed TO `{sp}`",
    f"GRANT READ VOLUME, WRITE VOLUME ON VOLUME {FQ}.papers TO `{sp}`",
    f"GRANT READ VOLUME, WRITE VOLUME ON VOLUME {FQ}.agent_state TO `{sp}`",
    f"GRANT READ VOLUME, WRITE VOLUME ON VOLUME {FQ}.uploads TO `{sp}`",
    f"GRANT MODIFY, SELECT ON TABLE {FQ}.mlflow_experiment_trace_otel_spans TO `{sp}`",
    f"GRANT MODIFY, SELECT ON TABLE {FQ}.mlflow_experiment_trace_otel_logs TO `{sp}`",
]:
    try:
        sql(stmt)
        print("ok:", stmt)
    except RuntimeError as e:  # the trace tables appear once the experiment is linked (07_traces)
        if "mlflow_experiment_trace" not in stmt:
            raise
        print("skipped (no trace tables yet):", stmt, str(e)[:120])

# the AI Search index: the app asks it for passage ids (the text itself is read as the analyst)
sql(f"GRANT SELECT ON TABLE {FQ}.passages_index TO `{sp}`")
print("ok: SELECT on passages_index")
ep = w.vector_search_endpoints.get_endpoint(ARGS.endpoint)
w.api_client.do("PATCH", f"/api/2.0/permissions/vector-search-endpoints/{ep.id}",
                body={"access_control_list": [{"service_principal_name": sp, "permission_level": "CAN_USE"}]})
print("ok: CAN_USE on the", ARGS.endpoint, "endpoint")

# The app started before it had any of this: restart it so its agent runtime comes up with access to the schema.
name = ARGS.app or "ic-paper-studio"
w.apps.stop(name).result()
w.apps.start(name).result()
print("restarted the app", name)
