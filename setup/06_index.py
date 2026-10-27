"""The AI Search index over the library's passages: Delta Sync from the passages table, embeddings managed by Databricks.

The index keeps only what retrieval filters on (document, type, date, current or superseded, independent or not) plus
the headed passage text it embeds and keyword-matches (hybrid search). The app asks it for passage ids and reads the
text itself from the governed tables as the signed-in analyst.
"""
import os as _os
import sys as _sys
_ROOT = next((_sys.argv[i + 1] for i, a in enumerate(_sys.argv[:-1]) if a == "--root"), None)  # job tasks have no __file__
_sys.path.insert(0, _os.path.join(_ROOT, "setup") if _ROOT else _os.path.dirname(_os.path.abspath(__file__)))
import time

from databricks.sdk.service.vectorsearch import DeltaSyncVectorIndexSpecRequest, EmbeddingSourceColumn, PipelineType, VectorIndexType

from common import ARGS, FQ, sql, w

ENDPOINT = ARGS.endpoint
INDEX = f"{FQ}.passages_index"

try:
    w.vector_search_indexes.get_index(INDEX)
    w.vector_search_indexes.sync_index(INDEX)
    print("syncing", INDEX)
except Exception as e:
    if "does not exist" not in str(e).lower() and "not found" not in str(e).lower():
        raise
    w.vector_search_indexes.create_index(
        name=INDEX, endpoint_name=ENDPOINT, primary_key="element_id", index_type=VectorIndexType.DELTA_SYNC,
        delta_sync_index_spec=DeltaSyncVectorIndexSpecRequest(
            source_table=f"{FQ}.passages", pipeline_type=PipelineType.TRIGGERED,
            embedding_source_columns=[EmbeddingSourceColumn(name="search_text", embedding_model_endpoint_name="databricks-gte-large-en")],
            columns_to_sync=["element_id", "doc_id", "page", "type", "search_text", "doc_type", "collection", "as_of_int", "is_current", "independent"]))
    print("created", INDEX)

want = int(sql(f"SELECT count(*) FROM {FQ}.passages")[1][0][0])
for _ in range(160):
    st = w.vector_search_indexes.get_index(INDEX).status
    print(f"  {st.indexed_row_count or 0}/{want} rows, ready={st.ready}: {(st.message or '')[:110]}")
    if st.ready and (st.indexed_row_count or 0) == want:
        break
    time.sleep(15)
