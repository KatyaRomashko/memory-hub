"""Render out/compare.jsonl (one line per mode) as a markdown table."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "out/compare.jsonl")
    rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    if not rows:
        print("no modes recorded")
        return

    print("| Mode | What it is | Memories | Recall | False pos. | Exact dupes | Correction | Agent calls | Agent tokens | Extractions | Extract ms |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        correction = "two facts" if r["correction_kept_both"] else "single / updated"
        print(f"| {r['mode']} | {r['description']} | {r['memories']} | {r['recall_score']} | "
              f"{r['false_positives']} | {r['exact_duplicates']} | {correction} | {r['agent_calls']} | "
              f"{r['agent_prompt_tokens']} | {r['proxy_extractions']} | {r['extract_ms_avg'] or '-'} |")

    print("\nDetail per mode:\n")
    for r in rows:
        missing = [k for k, v in r["recall"].items() if not v]
        print(f"- **{r['mode']}** — {r['description']}. embeddings: {r['embeddings']}; "
              f"threads: {r['threads']}, provenance links: {r['provenance_links']}; "
              f"missing facts: {missing or 'none'}; "
              f"top hits for \"dark mode\": {r['top_hits_dark_mode'] or 'none'}"
              + (f"; rule-based fallbacks in the explicit agent: {r['explicit_fallbacks']}"
                 if r.get("explicit_fallbacks") else ""))
    print("\nRecall is measured against the facts the scenarios contain: dark mode, Python, "
          "OpenShift 4.19, eu-west-1. False positives count memories about the smalltalk "
          "scenario (boiling an egg), which should produce none.")


if __name__ == "__main__":
    main()
