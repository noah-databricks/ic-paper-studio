"""Every agent turn as an MLflow trace, in the Studio's experiment (its traces live in Unity Catalog tables).

The agent runs in Omnigent's harness, outside this process, so the Studio rebuilds each finished turn from the thread's
transcript: one AGENT trace from the analyst's message to the agent's reply, a TOOL span for every paper tool call
(arguments in, result out, failures marked), and a CHAT_MODEL span for each thing the agent said along the way, on
the transcript's own timestamps. The MLflow evaluation (eval/evaluate_paper.py) scores the paper these turns produced.
"""
import json
import logging
import os
import threading

log = logging.getLogger("traces")
_lock = threading.Lock()
_bound: list[bool] = []


def enabled() -> bool:
    return bool(os.getenv("MLFLOW_EXPERIMENT_ID"))


def _text(it: dict) -> str:
    return "".join(c.get("text") or "" for c in (it["data"].get("content") or []))


def _ns(t: float) -> int:
    return int(float(t) * 1e9)


def _parse(s):
    try:
        v = json.loads(s)
        if isinstance(v, dict) and isinstance(v.get("result"), str):
            return _parse(v["result"])
        return v
    except (TypeError, ValueError):
        return s


def turns(items: list[dict]) -> list[list[dict]]:
    """Split a transcript at each analyst (or [Studio]) message."""
    out = []
    for it in items:
        if it["type"] == "message" and it["data"].get("role") == "user":
            out.append([it])
        elif out:
            out[-1].append(it)
    return out


TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def usage_since(before: dict | None, now: dict) -> dict | None:
    """The tokens a thread used between two of Omnigent's per-model running totals, in MLflow's shape: input counts
    cached prompt tokens too (Anthropic reports them separately), as MLflow's own Anthropic tracing does."""
    d = {k: sum(int((m or {}).get(k) or 0) for m in now.values()) - sum(int((m or {}).get(k) or 0) for m in (before or {}).values())
         for k in TOKEN_KEYS}
    if d["output_tokens"] <= 0:
        return None
    d["input_tokens"] += d["cache_read_input_tokens"] + d["cache_creation_input_tokens"]
    d["total_tokens"] = d["input_tokens"] + d["output_tokens"]
    return d


def export(sid: str, title: str, model: str | None, owner: str, items: list[dict], done: set[str], harness: str = "claude-sdk",
           usage: dict | None = None) -> list[str]:
    """Trace the turns of a thread that have finished and are not in `done` yet (call when the thread is idle). Returns
    the ids of the turns traced now, for the caller to remember. `usage` is the thread's tokens since the last trace: it
    goes on the last turn traced (normally the only one), on its final model span, so the trace shows its tokens."""
    import mlflow
    from mlflow.entities import SpanStatusCode, SpanType
    from mlflow.tracing.constant import SpanAttributeKey
    RETRIEVERS = {"search_library", "grep"}

    with _lock:
        todo = [t for t in turns(items) if t[0]["id"] not in done and any(x["type"] == "message" and x["data"].get("role") == "assistant" for x in t)]
        if not todo:
            return []
        mlflow.set_tracking_uri("databricks")
        if not _bound:  # the experiment keeps its traces in Unity Catalog tables in the demo's schema
            # set_experiment(trace_location=...) refuses an experiment that is already linked, so point the tracer at the
            # linked schema directly (deprecated in MLflow 3.16 but the only call that works for a pre-linked experiment)
            from mlflow.entities.trace_location import UCSchemaLocation
            import sources as src
            catalog, schema = src.FQ.split(".")
            mlflow.set_experiment(experiment_id=os.environ["MLFLOW_EXPERIMENT_ID"])
            mlflow.tracing.set_destination(UCSchemaLocation(catalog, schema))
            _bound.append(True)
        for n, turn in enumerate(todo):
            first, last = turn[0], turn[-1]
            request = _text(first)
            replies = [x for x in turn if x["type"] == "message" and x["data"].get("role") == "assistant"]
            root = mlflow.start_span_no_context(
                name="ic_paper_agent", span_type=SpanType.AGENT, inputs={"message": request},
                attributes={"thread_id": sid, "model": model or "", "harness": f"{harness} (Omnigent)"},
                metadata={"mlflow.trace.session": sid, "mlflow.trace.user": owner},
                tags={"thread_title": title[:200], "app": "ic-paper-studio"},
                experiment_id=os.environ["MLFLOW_EXPERIMENT_ID"], start_time_ns=_ns(first["created_at"]))
            outputs = {x["data"].get("call_id"): x for x in turn if x["type"] == "function_call_output"}
            calls = fills = errors = 0
            for x in turn[1:]:
                if x["type"] == "function_call":
                    name = str(x["data"].get("name", "")).split("__")[-1]
                    if "ToolSearch" in name:
                        continue
                    out = outputs.get(x["data"].get("call_id"))
                    res = _parse(out["data"].get("output")) if out else None
                    failed = isinstance(res, str) and "Error executing tool" in res
                    kind = SpanType.RETRIEVER if name in RETRIEVERS else SpanType.TOOL
                    s = mlflow.start_span_no_context(name=name, span_type=kind, parent_span=root,
                                                     inputs=_parse(x["data"].get("arguments")), start_time_ns=_ns(x["created_at"]))
                    hits = (res.get("passages") or res.get("matches")) if isinstance(res, dict) else None
                    if kind == SpanType.RETRIEVER and isinstance(hits, list):  # MLflow's document shape, so retrieval scorers can read it
                        res = [{"page_content": r.get("text", ""), "metadata": {k: v for k, v in r.items() if k != "text"}, "id": r.get("element_id")}
                               for r in hits if isinstance(r, dict)]
                    s.end(outputs=res, status=SpanStatusCode.ERROR if failed else SpanStatusCode.OK,
                          end_time_ns=_ns(out["created_at"] if out else x["created_at"]) + 1)
                    calls += 1
                    fills += name.startswith("fill_") and not failed
                    errors += failed
                elif x["type"] == "message" and x["data"].get("role") == "assistant":
                    s = mlflow.start_span_no_context(name="agent_reply", span_type=SpanType.CHAT_MODEL, parent_span=root,
                                                     inputs={"model": model}, start_time_ns=_ns(x["created_at"]))
                    if usage and n == len(todo) - 1 and x is replies[-1]:
                        s.set_attribute(SpanAttributeKey.CHAT_USAGE, usage)
                    s.end(outputs={"text": _text(x)}, end_time_ns=_ns(x["created_at"]) + 1)
            root.end(outputs={"response": _text(replies[-1])},
                     attributes={"tool_calls": calls, "slots_filled": fills, "tool_errors": errors},
                     end_time_ns=_ns(last["created_at"]) + 2)
        return [t[0]["id"] for t in todo]
