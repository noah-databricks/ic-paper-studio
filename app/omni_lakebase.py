"""Run the Omnigent server on Lakebase Postgres.

Omnigent keeps its thread store behind SQLAlchemy and mints a fresh password for every new connection when given a
token provider. Lakebase passwords are OAuth tokens that last about an hour, so this launcher plugs in the Autoscaling
credential call (for the app's own identity), makes sure the schema the store lives in exists (created, and so owned,
by that identity), then hands over to Omnigent's own CLI.

    python -m omni_lakebase server --host 127.0.0.1 -p 6767   (with OMNIGENT_DATABASE_URI and LAKEBASE_ENDPOINT set)
"""
import os
import time

SCHEMA = os.getenv("OMNIGENT_PG_SCHEMA", "omnigent")
_cache = {"token": None, "at": 0.0}


def token() -> str:
    # tokens last ~1 h; Omnigent recycles pooled connections every 10 min, so a 15 min cache stays well inside that
    if _cache["token"] and time.time() - _cache["at"] < 900:
        return _cache["token"]
    from databricks.sdk import WorkspaceClient
    cred = WorkspaceClient().postgres.generate_database_credential(endpoint=os.environ["LAKEBASE_ENDPOINT"])
    _cache.update(token=cred.token, at=time.time())
    return cred.token


def ensure_schema():
    import psycopg
    with psycopg.connect(host=os.environ["PGHOST"], port=int(os.getenv("PGPORT", "5432")), dbname=os.environ["PGDATABASE"],
                         user=os.environ["PGUSER"], password=token(), sslmode="require", connect_timeout=30) as c:
        c.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')


def main():
    from omnigent.db import utils

    for attempt in range(6):  # a scaled-to-zero endpoint takes a moment to wake
        try:
            ensure_schema()
            break
        except Exception:
            if attempt == 5:
                raise
            time.sleep(5)
    utils.set_lakebase_token_provider(token)
    from omnigent.cli import main as omnigent_main
    omnigent_main()


if __name__ == "__main__":
    main()
