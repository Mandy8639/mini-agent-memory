#!/usr/bin/env python3
"""
Eval harness: 25 questions × up to four conditions.

Never invents results. If ANTHROPIC_API_KEY is missing, prints an empty table
with an honest note. Use --limit to shrink the set for cost.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from agent import (
    MEMORY_DIR,
    MEMORY_MD,
    NOTES_DIR,
    QUESTIONS_PATH,
    ROOT,
    TRANSCRIPTS_DIR,
    WIKI_DIR,
    ensure_dirs,
    run_ask,
)
from dream import run_dream

RESULTS_PATH = ROOT / "eval_results.json"


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def grade(agent_answer: str, correct: str, qtype: str, qid: str) -> bool:
    """Heuristic grader — keyword / number overlap, plus trap-specific checks."""
    a = normalize(agent_answer)
    c = normalize(correct)
    if not a:
        return False

    # Trap-specific wrong-answer detectors
    trap_wrong = {
        "q21": [r"\b15\b"],  # legacy PTO
        "q22": [r"\b39\b"],  # legacy price
        "q23": [r"\b30\s*day"],  # old share-link expiry
        "q24": [r"\byes\b.*hipaa|\bhipaa\b.*\b(available|offer|provide|included)\b"],
        "q25": [r"\byes\b|convert(s|ed)?\s+to\s+(paid\s+)?credit"],
    }
    if qid in trap_wrong:
        for pat in trap_wrong[qid]:
            if re.search(pat, a) and qid == "q24":
                # allow "not available" / "no"
                if re.search(r"\b(no|not|does not|doesn't|unavailable|deferred)\b", a):
                    break
                return False
            if qid != "q24" and re.search(pat, a):
                # If they also state the correct fact, still fail for repeating the trap
                if qid == "q21" and re.search(r"\b20\b", a) and "supersed" in a:
                    continue
                if qid == "q22" and re.search(r"\b49\b", a) and ("legacy" in a or "supersed" in a or "not" in a):
                    continue
                if qid == "q23" and re.search(r"\b7\b", a) and ("30" in a) and ("old" in a or "previous" in a or "was" in a or "changed" in a):
                    continue
                if qid == "q25" and re.search(r"\b(no|not|do not|don't)\b", a):
                    continue
                return False

    # Extract salient tokens from correct answer
    numbers = re.findall(r"\$?\d[\d,]*(?:\.\d+)?%?", c)
    must_have = []
    for n in numbers:
        must_have.append(n.replace(",", ""))
    # key phrases
    for phrase in [
        "20 days",
        "16 weeks",
        "$4.2m",
        "us-west-2",
        "eu-central-1",
        "2025-01-20",
        "2025-06-15",
        "$12,936",
        "$30,720",
        "$72,000",
        "enterprise only",
        "7 days",
        "15 minutes",
        "5 minutes",
        "60 requests",
        "3,000",
        "$3,000",
        "100,000",
        "100k",
        "0.002",
        "34",
        "30 days",
        "status.northstarlabs.example",
    ]:
        if phrase in c:
            must_have.append(phrase)

    hits = 0
    need = 0
    for tok in must_have:
        need += 1
        tok_n = tok.lower().replace(",", "")
        if tok_n in a.replace(",", ""):
            hits += 1

    if need == 0:
        # fallback: overlap of content words
        c_words = set(re.findall(r"[a-z0-9$]+", c)) - {"the", "a", "an", "and", "or", "of", "per", "to", "for", "is", "in"}
        a_words = set(re.findall(r"[a-z0-9$]+", a))
        if not c_words:
            return False
        return len(c_words & a_words) / len(c_words) >= 0.45

    return hits / need >= 0.6


def reset_memory(preserve_structure: bool = True) -> None:
    ensure_dirs()
    for d in (NOTES_DIR, WIKI_DIR, TRANSCRIPTS_DIR):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
    MEMORY_MD.write_text(
        "# MEMORY.md — injected index\n\n(empty)\n",
        encoding="utf-8",
    )


def memory_stats() -> dict:
    ensure_dirs()
    notes = list(NOTES_DIR.glob("*.md")) if NOTES_DIR.exists() else []
    wiki = list(WIKI_DIR.glob("*.md")) if WIKI_DIR.exists() else []
    raw = MEMORY_MD.read_bytes() if MEMORY_MD.exists() else b""
    return {
        "notes": len(notes),
        "wiki_pages": len(wiki),
        "memory_md_lines": len(raw.decode("utf-8", errors="replace").splitlines()),
        "memory_md_bytes": len(raw),
    }


def run_condition(
    name: str,
    questions: list[dict],
    *,
    use_memory: bool,
    learn: bool,
    dream_after_half: bool = False,
    search_only: bool = False,
    search_plus_wiki: bool = False,
) -> dict:
    """Run questions under one condition. Returns metrics."""
    if not use_memory and not search_only and not search_plus_wiki:
        reset_memory()

    results = []
    repeat_mistakes = 0
    trap_wrong_answers: dict[str, str] = {}
    total_tokens = 0

    # For memory-on / dream conditions, start clean then accumulate
    if name in ("memory_notes", "memory_dream", "baseline"):
        reset_memory()

    half = max(1, len(questions) // 2)

    for i, q in enumerate(questions):
        if dream_after_half and i == half:
            run_dream()

        allow_corpus = True
        allow_memory = use_memory and not search_only
        allow_wiki = (use_memory or search_plus_wiki) and not search_only
        if search_only:
            allow_memory = False
            allow_wiki = False
        if search_plus_wiki:
            # Ensure wiki exists (from a prior dream or seed)
            if not any(WIKI_DIR.glob("*.md")):
                run_dream(use_llm=bool(os.environ.get("ANTHROPIC_API_KEY")))
            allow_memory = True
            allow_wiki = True

        sid = f"eval-{name}-{q['id']}-{datetime.now(timezone.utc).strftime('%H%M%S')}"
        try:
            out = run_ask(
                q["question"],
                correct_answer=q["answer"] if learn else None,
                session_id=sid,
                learn=learn,
                allow_corpus=allow_corpus,
                allow_memory_search=allow_memory,
                allow_wiki=allow_wiki,
            )
        except SystemExit as e:
            raise
        except Exception as e:
            out = {
                "answer": f"ERROR: {e}",
                "input_tokens": 0,
                "docs_used": [],
            }

        ok = grade(out.get("answer", ""), q["answer"], q.get("type", ""), q["id"])
        tokens = int(out.get("input_tokens") or 0)
        total_tokens += tokens

        # Repeat mistake tracking for traps
        if q.get("type") == "trap" and not ok:
            prev = trap_wrong_answers.get(q["id"])
            norm = normalize(out.get("answer", ""))[:200]
            if prev is not None and prev == norm:
                repeat_mistakes += 1
            trap_wrong_answers[q["id"]] = norm

        results.append(
            {
                "id": q["id"],
                "type": q.get("type"),
                "correct": ok,
                "answer": out.get("answer"),
                "expected": q["answer"],
                "tokens": tokens,
                "docs_used": out.get("docs_used"),
            }
        )
        print(f"  [{name}] {q['id']} {'OK' if ok else 'FAIL'} tokens={tokens}")

    n = len(results) or 1
    return {
        "condition": name,
        "accuracy": sum(1 for r in results if r["correct"]) / n,
        "correct_count": sum(1 for r in results if r["correct"]),
        "n": len(results),
        "repeat_mistakes": repeat_mistakes,
        "avg_tokens": total_tokens / n,
        "memory": memory_stats(),
        "results": results,
    }


def format_table(rows: list[dict]) -> str:
    header = "| Condition | Accuracy | Repeat mistakes | Avg tokens / question |"
    sep = "|---|---|---|---|"
    lines = [header, sep]
    labels = {
        "baseline": "No memory (baseline)",
        "memory_notes": "Memory on (notes only, no dreaming)",
        "memory_dream": "Memory + one dreaming pass",
        "search_alone": "Search alone (cross-doc subset)",
        "search_wiki": "Search + wiki (cross-doc subset)",
    }
    for row in rows:
        label = labels.get(row["condition"], row["condition"])
        acc = f"{row['correct_count']}/{row['n']} ({row['accuracy']*100:.0f}%)"
        lines.append(
            f"| {label} | {acc} | {row['repeat_mistakes']} | {row['avg_tokens']:.0f} |"
        )
    return "\n".join(lines)


def run_eval(limit: int | None = None, skip_if_no_key: bool = False) -> dict:
    ensure_dirs()
    questions = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    if limit:
        questions = questions[:limit]

    if not os.environ.get("ANTHROPIC_API_KEY"):
        empty = {
            "ran": False,
            "reason": "ANTHROPIC_API_KEY not set — eval did not run; no fabricated numbers.",
            "table_markdown": (
                "| Condition | Accuracy | Repeat mistakes | Avg tokens / question |\n"
                "|---|---|---|---|\n"
                "| No memory (baseline) | — | — | — |\n"
                "| Memory on (notes only, no dreaming) | — | — | — |\n"
                "| Memory + one dreaming pass | — | — | — |\n"
                "| Search alone vs search + wiki (on the cross-document subset) | — | — | — |\n"
            ),
            "note": (
                "Set ANTHROPIC_API_KEY and run `python eval.py` (or `python eval.py --limit 15` "
                "to shrink cost). Results will be written to eval_results.json."
            ),
        }
        print(empty["note"])
        print()
        print(empty["table_markdown"])
        RESULTS_PATH.write_text(json.dumps(empty, indent=2) + "\n", encoding="utf-8")
        return empty

    cross = [q for q in questions if q.get("type") == "cross"]
    rows = []

    print("=== Condition 1: baseline (no memory) ===")
    rows.append(
        run_condition("baseline", questions, use_memory=False, learn=False)
    )

    print("=== Condition 2: memory notes only (no dreaming) ===")
    rows.append(
        run_condition("memory_notes", questions, use_memory=True, learn=True)
    )

    print("=== Condition 3: memory + one dreaming pass (midway) ===")
    rows.append(
        run_condition(
            "memory_dream",
            questions,
            use_memory=True,
            learn=True,
            dream_after_half=True,
        )
    )

    print("=== Condition 4a: search alone (cross-doc subset) ===")
    # Fresh memory irrelevant; corpus search only
    rows.append(
        run_condition(
            "search_alone",
            cross,
            use_memory=False,
            learn=False,
            search_only=True,
        )
    )

    print("=== Condition 4b: search + wiki (cross-doc subset) ===")
    reset_memory()
    run_dream()  # build wiki representations
    rows.append(
        run_condition(
            "search_wiki",
            cross,
            use_memory=True,
            learn=False,
            search_plus_wiki=True,
        )
    )

    table = format_table(rows)
    print("\n" + table)
    payload = {
        "ran": True,
        "model": os.environ.get("EVAL_MODEL_NOTE", "see agent.MODEL"),
        "limit": limit,
        "n_questions": len(questions),
        "n_cross": len(cross),
        "table_markdown": table,
        "conditions": [{k: v for k, v in r.items() if k != "results"} for r in rows],
        "details": rows,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    RESULTS_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {RESULTS_PATH}")
    return payload


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Eval harness for mini-agent-memory")
    parser.add_argument("--limit", type=int, default=None, help="Use only first N questions")
    parser.add_argument(
        "--skip-api",
        action="store_true",
        help="If no API key, exit 0 after printing empty table",
    )
    args = parser.parse_args(argv)
    result = run_eval(limit=args.limit, skip_if_no_key=args.skip_api)
    if not result.get("ran") and not args.skip_api:
        return 0  # honest empty table is success for offline
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
