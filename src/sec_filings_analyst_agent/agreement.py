"""Judge vs. human agreement on the labeled set.

  uv run python -m sec_filings_analyst_agent.agreement --labels eval/labeling/human_labels.json \
      --judged eval/judge_runs/judged_labeled_*.jsonl

Reads the labeling KEY (so run it only after labeling). "Fail" is the positive class: fail recall is the
share of human-fail items the judge also failed. Human borderline flags are counted both ways (as pass, as fail).
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from pathlib import Path

from sec_filings_analyst_agent.judge import aggregate, analysis_verdict

ROOT = Path(__file__).resolve().parents[2]


def kappa(pairs: list[tuple[str, str]]) -> float | None:
    n = len(pairs)
    if n == 0:
        return None
    po = sum(a == b for a, b in pairs) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[c] * cb[c] for c in set(ca) | set(cb)) / n**2
    return None if pe == 1 else (po - pe) / (1 - pe)


def kappa_ci(pairs, reps: int = 4000, seed: int = 1):
    rng = random.Random(seed)
    ks = sorted(k for k in (kappa([rng.choice(pairs) for _ in pairs]) for _ in range(reps)) if k is not None)
    return (ks[int(0.025 * len(ks))], ks[int(0.975 * len(ks))]) if ks else (None, None)


def stats(rows: list[dict], border: str | None = None) -> dict:
    """border: None = ignore flag, 'pass'/'fail' = recode human-borderline items that way."""
    pairs = []
    for r in rows:
        h = r["human"]
        if border and r["borderline"]:
            h = border
        pairs.append((h, r["judge"]))
    tp = sum(h == "fail" and j == "fail" for h, j in pairs)
    fn = sum(h == "fail" and j == "pass" for h, j in pairs)
    tn = sum(h == "pass" and j == "pass" for h, j in pairs)
    fp = sum(h == "pass" and j == "fail" for h, j in pairs)
    lo, hi = kappa_ci(pairs) if pairs else (None, None)
    f = lambda x: "n/a" if x is None else f"{x:.2f}"
    return {"n": len(pairs), "agree": f"{(tp + tn) / len(pairs):.0%}" if pairs else "n/a", "kappa": f(kappa(pairs)),
            "ci": f"{f(lo)}..{f(hi)}", "fail_recall": f"{tp}/{tp + fn}", "pass_recall": f"{tn}/{tn + fp}",
            "tp": tp, "fn": fn, "tn": tn, "fp": fp}


def show(title: str, rows: list[dict]) -> None:
    s = stats(rows)
    print(f"{title:28} n={s['n']:3}  agree {s['agree']:>4}  kappa {s['kappa']:>5} (95% CI {s['ci']})  "
          f"fail recall {s['fail_recall']:>6}  pass recall {s['pass_recall']:>6}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--labels", type=Path, required=True)
    p.add_argument("--judged", type=Path, required=True)
    p.add_argument("--key", type=Path, default=ROOT / "eval/labeling/labeling_key.json")
    a = p.parse_args()
    human = json.loads(a.labels.read_text())["labels"]
    key = {k["label_id"]: k for k in json.loads(a.key.read_text())["items"]}
    agg = {(x["item_id"], x["criterion"]): x for x in aggregate(
        [json.loads(l) for l in a.judged.read_text().splitlines() if l.strip()])}
    rows, missing, na = [], [], 0
    for lid, k in key.items():
        h, j = human.get(lid), agg.get((k["item_id"], k["criterion"]))
        if not h or not j:
            missing.append(lid)
            continue
        jv = analysis_verdict(k["criterion"], j["verdict"])
        if jv == "not_applicable":
            na += 1
            jv = "pass"
        seed = k.get("seed") or {}
        rows.append({"label": lid, "criterion": k["criterion"], "origin": k["origin"], "human": h["verdict"],
                     "borderline": bool(h.get("borderline")), "note": h.get("note"), "judge": jv, "judge_raw": j["verdict"],
                     "judge_conf": j["confidence"], "judge_agree": j["agreement"], "mode": seed.get("mode"),
                     "difficulty": seed.get("difficulty"), "q": k["question_id"], "variant": k["variant"]})
    print(f"{len(rows)} items compared; missing {missing}; judge not_applicable treated as pass (non-advice): {na}\n")
    show("ALL", rows)
    for c in ("premise", "misuse", "advice", "narrative"):
        show(c, [r for r in rows if r["criterion"] == c])
    show("real answers", [r for r in rows if r["origin"] == "real"])
    show("seeded failures", [r for r in rows if r["origin"] == "seed"])
    for d in ("obvious", "subtle", "near_miss"):
        show(f"  seeds: {d}", [r for r in rows if r["difficulty"] == d])
    s = stats(rows)
    print(f"\nConfusion matrix (rows = human, columns = judge):\n            judge pass  judge fail\n"
          f"human pass  {s['tn']:10}  {s['fp']:10}\nhuman fail  {s['fn']:10}  {s['tp']:10}")
    for b in ("pass", "fail"):
        t = stats(rows, border=b)
        print(f"Human borderline counted as {b}: agree {t['agree']}, kappa {t['kappa']} (CI {t['ci']})")
    print("\nDisagreements:")
    for r in rows:
        if r["human"] != r["judge"]:
            print(f"  {r['label']} {r['criterion']:9} {r['origin']:4} {r['q']:4} v={r['variant']} human={r['human']}"
                  f"{'(borderline)' if r['borderline'] else ''} judge={r['judge_raw']}/{r['judge_conf']}/{r['judge_agree']}"
                  f"{' seed=' + str(r['mode']) + '/' + str(r['difficulty']) if r['origin'] == 'seed' else ''}"
                  f"{' NOTE: ' + r['note'] if r['note'] else ''}")
    print("\nYour borderline/note items:")
    for r in rows:
        if r["borderline"] or r["note"]:
            print(f"  {r['label']} {r['criterion']:9} {r['origin']:4} human={r['human']} judge={r['judge_raw']} "
                  f"borderline={r['borderline']} NOTE: {r['note']}")


if __name__ == "__main__":
    main()
