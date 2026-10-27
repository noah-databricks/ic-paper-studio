"""Omnigent, run inside the app: its server and one host, as two local processes beside the Studio.

The server keeps threads and history; the host runs the agent harness (Claude Agent SDK) in this container. Only the
Studio can reach either: the server listens on loopback, and the Studio scopes every thread to the signed-in analyst
before relaying it. Model calls go through Unity AI Gateway with the app's identity; paper tools come back to this app
over loopback and run as the analyst who owns the thread. Finished turns are traced to MLflow by the app (traces.py).
"""
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

log = logging.getLogger("omnigent-runtime")
PORT = int(os.getenv("OMNIGENT_PORT", "6767"))
URL = f"http://127.0.0.1:{PORT}"
DATA = Path(os.getenv("OMNIGENT_HOME_DIR", "/tmp/ic-studio-omnigent"))
PROFILE = "ic-studio-app"  # the Databricks profile agent bundles name for gateway calls
state = {"server": None, "host": None, "host_id": None, "error": None, "started": None, "stopping": False, "snap_mtime": 0.0,
         "restored": None, "snapshot_at": None}
# Threads outlive the container: the store is snapshotted to a Unity Catalog volume only the app's service principal can
# read (it holds every user's conversations) and restored when a fresh container starts.
STATE_FILE = f"/Volumes/{os.getenv('UC_CATALOG', 'main')}/{os.getenv('UC_SCHEMA', 'ic_paper_studio')}/agent_state/omnigent-state.tar.gz"
_lock = threading.Lock()


def enabled() -> bool:
    return os.getenv("STUDIO_AGENT", "embedded") == "embedded"


def _env() -> dict:
    """Children get the app's workspace credentials, private Omnigent/Claude state dirs, and nothing app-specific."""
    home = DATA / "home"
    for d in (home, DATA / "data", DATA / "config", DATA / "work"):
        d.mkdir(parents=True, exist_ok=True)
    host = os.environ.get("DATABRICKS_HOST", "")
    if host and not host.startswith("http"):
        host = "https://" + host
    # Harness children run with a scrubbed environment, so the app's own workspace credential (its service principal's
    # OAuth client in Apps, a token when run locally) goes into a private profile that gateway calls resolve through.
    lines = [f"[{PROFILE}]", f"host = {host}"]
    if os.getenv("DATABRICKS_CLIENT_ID") and os.getenv("DATABRICKS_CLIENT_SECRET"):
        lines += [f"client_id = {os.environ['DATABRICKS_CLIENT_ID']}", f"client_secret = {os.environ['DATABRICKS_CLIENT_SECRET']}",
                  "auth_type = oauth-m2m"]
    elif os.getenv("DATABRICKS_TOKEN"):
        lines += [f"token = {os.environ['DATABRICKS_TOKEN']}"]
    for cfgf in (DATA / "databrickscfg", home / ".databrickscfg"):  # the harness sees only $HOME
        cfgf.touch(mode=0o600)
        cfgf.chmod(0o600)
        cfgf.write_text("\n".join(lines) + "\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "UVICORN")) and k not in ("OMNIGENT_PORT", "OMNIGENT_HOME_DIR")}
    env.update(HOME=str(home), OMNIGENT_DATA_DIR=str(DATA / "data"), OMNIGENT_CONFIG_HOME=str(DATA / "config"),
               DATABRICKS_CONFIG_FILE=str(DATA / "databrickscfg"), OMNIGENT_LOCAL_SINGLE_USER="1",
               OMNIGENT_SKIP_UPDATE_CHECK="1", OMNIGENT_UPDATE_CHECK="0", NO_COLOR="1", PYTHONUNBUFFERED="1")
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")])
    bins = Path(__file__).resolve().parent / "node_modules" / ".bin"  # harness CLIs from package.json
    # Claude Code: the native CLI from package.json (new models need a recent CLI), else the one bundled in claude-agent-sdk
    native = sorted(bins.parent.glob("@anthropic-ai/claude-code-linux-*/claude"), key=lambda p: "musl" in str(p))
    if native:
        env["OMNIGENT_CLAUDE_PATH"] = os.path.abspath(native[0])
    else:
        try:
            import claude_agent_sdk
            bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
            if bundled.exists():
                env["OMNIGENT_CLAUDE_PATH"] = str(bundled)
        except ImportError:
            pass
    # Codex: the native binary its npm package ships, so launching it needs no Node lookup
    native = sorted(bins.parent.glob("@openai/codex-linux-*/vendor/*/codex/codex"))
    codex = str(native[0]) if native else shutil.which("codex")
    if codex:
        env["OMNIGENT_CODEX_PATH"] = os.path.abspath(codex)  # Apps puts a relative node_modules/.bin on PATH
    pi = str(bins / "pi") if (bins / "pi").exists() else shutil.which("pi")
    if pi:
        env["OMNIGENT_PI_PATH"] = os.path.abspath(pi)
    env["PATH"] = os.pathsep.join([str(bins), env["PATH"]])
    # One stable host identity for this app, so threads started before a restart find their host again and resume
    import uuid
    env["OMNIGENT_HOST_ID"] = uuid.uuid5(uuid.NAMESPACE_URL, f"{host}/apps/{os.getenv('DATABRICKS_APP_NAME', 'ic-paper-studio')}").hex
    env["OMNIGENT_HOST_NAME"] = "ic-paper-studio"
    return env


def _spawn(name: str, args: list[str], env: dict, module: str = "omnigent") -> subprocess.Popen:
    logf = open(DATA / f"{name}.log", "ab", buffering=0)
    return subprocess.Popen([sys.executable, "-m", module, *args], env=env, cwd=str(DATA / "work"),
                            stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)


def _wait(fn, timeout: float, what: str):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError(f"Omnigent {what} did not come up within {int(timeout)} s")


def _online_host() -> str | None:
    hosts = httpx.get(f"{URL}/v1/hosts", timeout=5).json().get("hosts", [])
    return next((h["host_id"] for h in hosts if h.get("status") == "online"), None)


def lakebase() -> bool:
    return bool(os.getenv("PGHOST") and os.getenv("LAKEBASE_ENDPOINT"))


def lakebase_uri() -> str:
    """Postgres URI for Omnigent's store; no password (minted per connection), search_path on the app's own schema."""
    from urllib.parse import quote
    schema = os.getenv("OMNIGENT_PG_SCHEMA", "omnigent")
    return (f"postgresql+psycopg://{quote(os.environ['PGUSER'], safe='')}@{os.environ['PGHOST']}:{os.getenv('PGPORT', '5432')}/"
            f"{os.getenv('PGDATABASE', 'databricks_postgres')}?sslmode=require&options=-csearch_path%3D{schema}")


def _store_files():
    root = DATA / "data"
    return [p for p in root.rglob("*") if p.is_file() and "logs" not in p.relative_to(root).parts and "crashes" not in p.relative_to(root).parts
            and not p.name.endswith((".log", "-wal", "-shm", ".snap")) and not (lakebase() and p.suffix == ".db")]


def snapshot(force: bool = False):
    """Copy the thread store (SQLite via its backup API, so the copy is consistent) and the key map, minus tokens."""
    import io
    import json
    import sqlite3
    import tarfile

    files = _store_files()
    newest = max((p.stat().st_mtime for p in files), default=0.0)
    if not files or (not force and newest <= state["snap_mtime"]):
        return
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in files:
            rel = str(p.relative_to(DATA))
            if p.suffix == ".db":
                tmp = p.with_suffix(".snap")
                src, dst = sqlite3.connect(str(p)), sqlite3.connect(str(tmp))
                with dst:
                    src.backup(dst)
                src.close(); dst.close()
                tar.add(str(tmp), arcname=rel)
                tmp.unlink(missing_ok=True)
            else:
                tar.add(str(p), arcname=rel)
        try:
            keys = json.loads((DATA / "keys.json").read_text())
            data = json.dumps({k: {x: y for x, y in v.items() if x != "token"} for k, v in keys.items()}).encode()
            ti = tarfile.TarInfo("keys.json"); ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
        except (OSError, ValueError):
            pass
    from databricks.sdk import WorkspaceClient
    WorkspaceClient().files.upload(STATE_FILE, io.BytesIO(buf.getvalue()), overwrite=True)
    state["snap_mtime"], state["snapshot_at"] = newest, time.time()


def restore():
    """On a fresh container, bring back the last snapshot before the server opens its store."""
    import io
    import json
    import tarfile

    local = (DATA / "data" / "artifacts")
    if (not lakebase() and any((DATA / "data").glob("*.db"))) or (lakebase() and local.exists() and any(local.iterdir())):
        return
    try:
        from databricks.sdk import WorkspaceClient
        blob = WorkspaceClient().files.download(STATE_FILE).contents.read()
    except Exception as e:  # nothing saved yet, or no access: start empty
        log.info("no thread snapshot restored: %s", str(e)[:200])
        return
    (DATA / "data").mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        members = [m for m in tar.getmembers() if not m.name.startswith(("/", "..")) and ".." not in m.name.split("/")]
        keys = next((m for m in members if m.name == "keys.json"), None)
        # with Lakebase holding the threads, only the artifacts (agent bundles, attachments) come from the snapshot
        tar.extractall(DATA, members=[m for m in members if m.name != "keys.json" and not (lakebase() and m.name.endswith(".db"))])
        if keys:
            saved = json.loads(tar.extractfile(keys).read())
            path = DATA / "keys.json"
            try:
                cur = json.loads(path.read_text())
            except (OSError, ValueError):
                cur = {}
            for k, v in saved.items():
                cur.setdefault(k, {**v, "token": ""})  # the user's next visit refreshes the token
            path.touch(mode=0o600)
            path.write_text(json.dumps(cur))
            import agent  # already loaded its (empty) key map at import: add the restored threads to it
            for k, v in cur.items():
                agent._keys.setdefault(k, v)
                if v.get("email"):
                    workspace_dir(agent.owner_key(v["email"]))  # runners of restored threads start in these folders
    state["restored"] = time.time()
    state["snap_mtime"] = max((p.stat().st_mtime for p in _store_files()), default=0.0)
    log.info("restored %d thread-store files from %s", len(members), STATE_FILE)


def _start():
    env = _env()
    if state["restored"] is None:
        try:
            restore()
        except Exception:
            log.exception("thread snapshot restore failed")
        state["restored"] = state["restored"] or 0.0
    state["error"] = None
    if not (state["server"] and state["server"].poll() is None):
        args = ["server", "--host", "127.0.0.1", "-p", str(PORT), "--no-open"]
        if lakebase():
            # threads live in Lakebase; the launcher mints OAuth passwords for the app's identity (omni_lakebase)
            state["server"] = _spawn("server", args + ["--database-uri", lakebase_uri()], env | {"PYTHONPATH": str(Path(__file__).resolve().parent)},
                                     module="omni_lakebase")
        else:
            state["server"] = _spawn("server", args, env)
        _wait(lambda: httpx.get(f"{URL}/health", timeout=3).status_code == 200, 600, "server")  # first start migrates
    if not (state["host"] and state["host"].poll() is None):
        state["host"] = _spawn("host", ["host", "--server", URL], env)
    state["host_id"] = _wait(_online_host, 120, "host")
    state["started"] = time.time()
    log.info("Omnigent up: server %s, host %s", URL, state["host_id"])


def _supervise():
    """Start both processes, then keep them running for the life of the app."""
    while not state["stopping"]:
        with _lock:
            try:
                alive = all(p is not None and p.poll() is None for p in (state["server"], state["host"]))
                if not alive or not state["host_id"]:
                    _start()
                elif time.time() - (state["snapshot_at"] or 0) > 60:
                    snapshot()
            except Exception as e:
                state["error"] = f"{e} (see {DATA}/server.log, host.log)"
                log.exception("Omnigent start failed")
        time.sleep(10)


def start():
    if enabled():
        threading.Thread(target=_supervise, name="omnigent", daemon=True).start()


def stop():
    """Take Omnigent down with the app: the platform waits for every process before it starts the next deployment."""
    import psutil

    state["stopping"] = True
    # the platform allows 15 s from SIGTERM: a last snapshot gets 3 s of that (the loop snapshots every minute anyway)
    t = threading.Thread(target=lambda: snapshot(force=True), daemon=True)
    t.start()
    t.join(3)
    procs = []
    for p in (state["host"], state["server"]):
        if p is not None and p.poll() is None:
            try:
                parent = psutil.Process(p.pid)
                procs += parent.children(recursive=True) + [parent]
            except psutil.NoSuchProcess:
                pass
    for q in procs:
        try:
            q.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(procs, timeout=8)
    for q in alive:
        try:
            q.kill()
        except psutil.NoSuchProcess:
            pass


def host_id() -> str:
    """The live host, waiting briefly while Omnigent starts with the app."""
    for _ in range(90):
        if state["host_id"] and state["host"] and state["host"].poll() is None:
            return state["host_id"]
        time.sleep(1)
    raise RuntimeError(state["error"] or "The agent runtime is still starting. Try again in a moment.")


def workspace_dir(owner_key: str) -> str:
    d = DATA / "work" / owner_key
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def tail(name: str, n: int = 80) -> str:
    try:
        return "\n".join((DATA / f"{name}.log").read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return ""
