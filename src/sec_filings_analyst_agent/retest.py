"""Test-retest: build a fresh blind page of ~15 already-labeled items, to measure the labeler's consistency with themselves.

  uv run python -m sec_filings_analyst_agent.retest --build     # writes eval/labeling/retest_page.html + retest_key.json
  uv run python -m sec_filings_analyst_agent.retest --compare labels_retest-v1_*.json

Stratified: a fixed number per criterion, mixing real and seeded answers. Items whose label was corrected after the
judge disagreement are left out (the labeler has since seen the judge's reasoning and the filing text for them).
New ids and a new shuffle, and the page shows none of the earlier labels. Reads the key, so run it only as the builder.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

from sec_filings_analyst_agent import judge, labeling

RETEST_ID = "retest-v1"
SHUFFLE_SEED = 20261105
QUOTA = {"premise": (2, 2), "misuse": (2, 1), "advice": (2, 1), "narrative": (3, 2)}  # (real, seed)
KEY_PATH = labeling.OUT_DIR / "labeling_key.json"
LABELS_PATH = labeling.OUT_DIR / "human_labels.json"


def choose(key_items: list[dict], labels: dict) -> list[dict]:
    rng = random.Random(SHUFFLE_SEED)
    eligible = [k for k in key_items if "relabeled_from" not in labels[k["label_id"]]]
    chosen = []
    for crit, (n_real, n_seed) in QUOTA.items():
        for origin, n in (("real", n_real), ("seed", n_seed)):
            pool = [k for k in eligible if k["criterion"] == crit and k["origin"] == origin]
            chosen += rng.sample(pool, n)
    rng.shuffle(chosen)
    return chosen


def compare(path: Path) -> None:
    from sec_filings_analyst_agent.agreement import kappa
    new = json.loads(path.read_text())["labels"]
    rkey = json.loads((labeling.OUT_DIR / "retest_key.json").read_text())["items"]
    old = json.loads(LABELS_PATH.read_text())["labels"]
    pairs, rows = [], []
    for r in rkey:
        if r["retest_id"] not in new:
            continue
        a, b = old[r["label_id"]]["verdict"], new[r["retest_id"]]["verdict"]
        pairs.append((a, b))
        rows.append((r, a, b))
    agree = sum(a == b for a, b in pairs)
    k = kappa(pairs)
    print(f"{len(pairs)} items relabeled; same verdict on {agree} ({agree / len(pairs):.0%}); kappa {'n/a' if k is None else f'{k:.2f}'}")
    for r, a, b in rows:
        if a != b:
            print(f"  changed: {r['label_id']} -> {r['retest_id']} {r['criterion']} {r['origin']} first={a} second={b}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--build", action="store_true")
    p.add_argument("--compare", type=Path)
    a = p.parse_args()
    if a.compare:
        return compare(a.compare)
    if not a.build:
        sys.exit("pass --build or --compare FILE")
    import duckdb
    from sec_filings_analyst_agent.build import DB_PATH
    from sec_filings_analyst_agent.docs import format_documents, load_context

    key = json.loads(KEY_PATH.read_text())["items"]
    labels = json.loads(LABELS_PATH.read_text())["labels"]
    chosen = choose(key, labels)
    items = []
    for n, k in enumerate(chosen, 1):
        items.append({**k, "label_id": f"R{n:02d}", "retest_id": f"R{n:02d}", "orig_label_id": k["label_id"]})
    questions = {json.loads(l)["id"]: json.loads(l) for l in (judge.EVAL_DIR / "questions.jsonl").read_text().splitlines() if l.strip()}
    con = duckdb.connect(str(DB_PATH), read_only=True)
    docs: dict[str, str] = {}
    for it in items:
        if it["criterion"] == "narrative" and it["question_id"] not in docs:
            docs[it["question_id"]] = format_documents(load_context(con, questions[it["question_id"]]).documents)
    payload = labeling.page_payload(items, docs)
    payload["set_id"] = RETEST_ID
    (labeling.OUT_DIR / "retest_page.html").write_text(labeling.render_page(payload))
    (labeling.OUT_DIR / "retest_key.json").write_text(json.dumps(
        {"set_id": RETEST_ID, "items": [{"retest_id": i["retest_id"], "label_id": i["orig_label_id"], "criterion": i["criterion"],
                                          "origin": i["origin"]} for i in items]}, indent=1) + "\n")
    counts = {c: sum(1 for i in items if i["criterion"] == c) for c in labeling.ORDER}
    print(f"{len(items)} items {counts}")
    print("page: eval/labeling/retest_page.html")


if __name__ == "__main__":
    main()
