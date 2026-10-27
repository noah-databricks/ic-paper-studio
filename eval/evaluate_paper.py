"""Score a drafted IC paper with MLflow evaluation: one row per text section, judged against its cited sources.

    .venv/bin/python eval/evaluate_paper.py <deal_id> [<deal_id> ...]

Scorers:
- source_wording: share of the section that is the sources' own words (verbatim quotes and governed numbers count
  fully, shortened quotes by their similarity, the agent's linking words not at all).
- quotes_verified: every quote is still on its page word for word (re-checked against the parsed source).
- grounded: MLflow's groundedness judge, with the cited passages and metric values as the context.
- ic_prose / no_new_claims: guidelines judges for how a credit paper section reads, and that it adds nothing of its
  own beyond the sources.
A second run scores retrieval per deal against the library's ground truth (content/library/manifest.json, which ingestion
never reads):
- expected_sources_used: share of the documents an analyst would want for the deal that its chats read or cited.
- no_trap_citations: the paper cites nothing superseded and nothing about a look-alike borrower.
The runs land in the Studio's MLflow experiment, next to the agent traces.
"""
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import mlflow  # noqa: E402
from mlflow.entities import Feedback  # noqa: E402
from mlflow.genai.judges import is_grounded  # noqa: E402
from mlflow.genai.scorers import Guidelines, scorer  # noqa: E402

import paper as P  # noqa: E402
import sources as src  # noqa: E402

EXPERIMENT_ID = os.environ["MLFLOW_EXPERIMENT_ID"]  # the bundle's experiment: databricks bundle summary
JUDGE = os.getenv("JUDGE_MODEL", "databricks:/databricks-claude-sonnet-5-5")  # a workspace model, so judging stays in the workspace


def rows_for(sid: str) -> list[dict]:
    """One row per drafted text section of a deal's paper (the paper is stored per deal)."""
    p = json.loads(src.sp().files.download(f"{src.VOL}/papers/{sid}.json").contents.read())
    out = []
    for tag in P.ORDER_OF(p):
        s = p["slots"][tag]
        if s["kind"] != "text" or s["status"] == "empty":
            continue
        by_id = {c["id"]: c for c in s.get("citations") or []}
        context = []
        for c in by_id.values():
            if c["kind"] == "pdf":
                context.append({"source": f"{c['doc_title']} p.{c['page']} ({c['element_id']})", "text": c["source_text"]})
            else:
                context.append({"source": f"{c['fq']} {c['measure']}", "text": f"{c['measure']} = {c['formatted']} (query: {c['sql']})"})
        text = "\n\n".join(P.join(para["segments"]) for para in s["paragraphs"])
        quotes = [{"element_id": by_id[x["citation"]]["element_id"], "quote": by_id[x["citation"]]["quote"]}
                  for para in s["paragraphs"] for x in para["segments"] if x.get("citation") and by_id[x["citation"]]["kind"] == "pdf"]
        spec = P.SLOTS_OF(p)[tag]
        out.append({"inputs": {"section": spec["section"], "slot": tag, "guidance": spec["guidance"], "deal": sid, "template": p["template_id"]},
                    "outputs": {"text": text, "source_wording": s.get("source_wording"), "quotes": quotes, "context": context}})
    return out


@scorer
def source_wording(outputs) -> Feedback:
    v = outputs.get("source_wording") or 0.0
    return Feedback(value=round(v, 3), rationale=f"{v:.0%} of the section is the sources' own words or governed numbers.")


@scorer
def quotes_verified(outputs) -> Feedback:
    _, els = src.load()
    bad = [q["element_id"] for q in outputs["quotes"] if not (els.get(q["element_id"]) and src.find_quote(els[q["element_id"]], q["quote"]))]
    return Feedback(value=not bad, rationale="All quotes found word for word." if not bad else f"Not found: {', '.join(bad)}")


@scorer
def grounded(inputs, outputs) -> Feedback:
    return is_grounded(request=f"Write the '{inputs['slot']}' part of an IC credit paper: {inputs['guidance']}",
                       response=outputs["text"], context=outputs["context"], name="grounded", model=JUDGE)


ic_prose = Guidelines(name="ic_prose", model=JUDGE, guidelines=[
    "The section reads as a finished credit paper section: complete sentences, or the conventions credit papers use "
    "(a 'Label: detail' line for a term, a bulleted list for conditions or protections). No dangling fragments, "
    "sentences cut off mid-thought, or bare headings such as 'Strengths.' standing alone.",
])
no_new_claims = Guidelines(name="no_new_claims", model=JUDGE, guidelines=[
    "The section states facts from the deal's sources and adds no opinions, ratings, recommendations or numbers of its "
    "own. Restating a source's own recommendation or assessment is allowed.",
])


MANIFEST = Path(__file__).resolve().parents[1] / "content" / "library" / "manifest.json"


def retrieval_row(sid: str) -> dict:
    """What a deal's chats used from the library, and what the manifest says they should have (and should not cite)."""
    p = json.loads(src.sp().files.download(f"{src.VOL}/papers/{sid}.json").contents.read())
    used = P.sources_used(p)
    cited = sorted(k for k, u in used.items() if "cited" in u["how"])
    m, deal = json.loads(MANIFEST.read_text()), src.deal_of(sid)  # a fresh project on a deal has the deal's ground truth
    expected = sorted(x["stem"] for x in m if deal in (x.get("relevant_to_deals") or []))
    traps = sorted(x["stem"] for x in m if x.get("trap") and deal not in (x.get("relevant_to_deals") or [])
                   and (x.get("trap", "").startswith(("Superseded", "Look-alike")) or "supersede" in x.get("trap", "").lower()))
    return {"inputs": {"deal": sid, "template": p.get("template_id")},
            "outputs": {"used": sorted(used), "cited": cited},
            "expectations": {"expected": expected, "traps": traps}}


@scorer
def expected_sources_used(outputs, expectations) -> Feedback:
    exp = set(expectations["expected"])
    hit = exp & set(outputs["used"])
    return Feedback(value=round(len(hit) / len(exp), 3) if exp else 1.0, rationale=f"missed: {sorted(exp - hit)}" if exp - hit else "all used")


@scorer
def no_trap_citations(outputs, expectations) -> Feedback:
    bad = sorted(set(outputs["cited"]) & set(expectations["traps"]))
    return Feedback(value=not bad, rationale=f"cited {bad}" if bad else "no superseded or look-alike documents cited")


def main(sids: list[str]):
    mlflow.set_tracking_uri("databricks")
    mlflow.set_experiment(experiment_id=EXPERIMENT_ID)
    data = [r for sid in sids for r in rows_for(sid)]
    print(f"{len(data)} sections from {len(sids)} paper(s)")
    with mlflow.start_run(run_name=f"ic-paper-eval {'-'.join(s[:6] for s in sids)}"):
        res = mlflow.genai.evaluate(data=data, scorers=[source_wording, quotes_verified, grounded, ic_prose, no_new_claims])
    print(json.dumps({k: round(v, 3) if isinstance(v, float) else v for k, v in res.metrics.items()}, indent=1))
    print("run:", res.run_id)
    if MANIFEST.exists():
        with mlflow.start_run(run_name=f"ic-paper-retrieval {'-'.join(s[:6] for s in sids)}"):
            res = mlflow.genai.evaluate(data=[retrieval_row(sid) for sid in sids], scorers=[expected_sources_used, no_trap_citations])
        print(json.dumps({k: round(v, 3) if isinstance(v, float) else v for k, v in res.metrics.items()}, indent=1))
        print("retrieval run:", res.run_id)


if __name__ == "__main__":
    main(sys.argv[1:])
