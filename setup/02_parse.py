"""Parse the library PDFs with ai_parse_document v2 (page images to the parsed volume) into library_parsed.

Incremental: only files not parsed yet are sent to the parser, so adding a document to the library costs one parse.
"""
import os as _os
import sys as _sys
_ROOT = next((_sys.argv[i + 1] for i, a in enumerate(_sys.argv[:-1]) if a == "--root"), None)  # job tasks have no __file__
_sys.path.insert(0, _os.path.join(_ROOT, "setup") if _ROOT else _os.path.dirname(_os.path.abspath(__file__)))
from common import FQ, VOL, sql

sql(f"""CREATE TABLE IF NOT EXISTS {FQ}.library_parsed (path STRING, collection STRING, stem STRING, parsed VARIANT, parsed_at TIMESTAMP)
COMMENT 'ai_parse_document v2 output for each library PDF (elements with bounding boxes; page images in the parsed volume)'""")
sql(f"""INSERT INTO {FQ}.library_parsed
SELECT path, regexp_extract(path, '/library/([^/]+)/', 1) AS collection, regexp_extract(path, '([^/]+)\\\\.pdf$', 1) AS stem,
       ai_parse_document(content, map('version', '2.0', 'imageOutputPath', '{VOL}/parsed/', 'descriptionElementTypes', '*')) AS parsed,
       current_timestamp() AS parsed_at
FROM read_files('{VOL}/library/', format => 'binaryFile', pathGlobFilter => '*.pdf', recursiveFileLookup => true)
WHERE path NOT IN (SELECT path FROM {FQ}.library_parsed)""", timeout_s=3600)
# a file removed from the library leaves the table too
sql(f"""DELETE FROM {FQ}.library_parsed WHERE path NOT IN
        (SELECT path FROM read_files('{VOL}/library/', format => 'binaryFile', pathGlobFilter => '*.pdf', recursiveFileLookup => true))""")
_, rows = sql(f"""SELECT collection, count(*), sum(size(cast(parsed:document:pages AS ARRAY<VARIANT>))), count_if(cast(parsed:error_status AS STRING) IS NOT NULL)
                  FROM {FQ}.library_parsed GROUP BY collection ORDER BY collection""")
for c, n, pages, errs in rows:
    print(f"{c}: {n} documents, {pages} pages, {errs} with parse errors")
