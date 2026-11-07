#!/usr/bin/env python3
"""
Eval harness: 25 questions × up to four conditions, plus Round 2 trap paraphrases.

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

# Explicit phrasing aliases — grader scores meaning, not exact strings.
# Keys are question ids (Round 2 variants inherit via round2_of).
ALIASES: dict[str, list[str]] = {
    "q14": [
        "enterprise only",
        "enterprise plan only",
        "enterprise plans only",
        "only on enterprise",
        "only on the enterprise",
        "only on the enterprise plan",
        "only on the enterprise plans",
        "only available on enterprise",
        "available only on the enterprise",
        "available only on enterprise",
        "available on the enterprise plan only",
        "available on enterprise plans only",
        "available on enterprise only",
    ],
    "q16": [
        "cannot be stacked",
        "can't be stacked",
        "can not be stacked",
        "do not stack",
        "don't stack",
        "not stackable",
        "no stacking",
        "cannot stack",
        "can't stack",
    ],
    "q13": [
        "dedicated csm is included",
        "dedicated csm included",
        "includes a dedicated csm",
        "includes dedicated csm",
        "dedicated customer success manager",
    ],
}

# Trap questions: accuracy = current fact only; trap awareness reported separately.
# legacy_patterns: if present as the answer's claim without correction → not trap-aware
# and (for some) may indicate wrong current fact.
TRAP_SPECS: dict[str, dict] = {
    "q21": {
        "current_patterns": [r"\b20\s*days?\b", r"\b20\b"],
        "legacy_patterns": [r"\b15\s*days?\b"],
        "legacy_ok_with_correction": True,
    },
    "q22": {
        "current_patterns": [r"\$?\s*49\b"],
        "legacy_patterns": [r"\$?\s*39\b"],
        "legacy_ok_with_correction": True,
    },
    "q23": {
        "current_patterns": [r"\b7\s*days?\b", r"\b7\b"],
        "legacy_patterns": [r"\b30\s*days?\b"],
        "legacy_ok_with_correction": True,
    },
    "q24": {
        "current_patterns": [
            r"\b(no|not offered|does not offer|doesn't offer|unavailable|not available)\b",
        ],
        "legacy_patterns": [
            r"\byes\b.*\bhipaa\b|\bhipaa\b.*\b(available|offer|provide|included)\b",
        ],
        "legacy_ok_with_correction": False,
        "require_negation_if_hipaa_yes_claim": True,
    },
    "q25": {
        "current_patterns": [
            r"\b(no|do not|don't|does not|doesn't)\b.*\b(convert|credit)",
            r"\b(do not|don't|does not|doesn't)\s+convert\b",
            r"\bnot?\s+convert",
            r"\bno\b",
        ],
        "legacy_patterns": [
            r"\byes\b|convert(s|ed)?\s+to\s+(paid\s+)?credit",
        ],
        "legacy_ok_with_correction": False,
    },
}

CORRECTION_HINTS = re.compile(
    r"\b(legacy|supersed|outdated|old|previous|formerly|was|changed|no longer|"
    r"do not quote|don't quote|not current|deprecated|replaced)\b",
    re.I,
)


def normalize(text: str) -> str:
    """Lowercase, strip punctuation (keep $ % . / -), collapse whitespace."""
    text = (text or "").lower()
    text = text.replace("—", " ").replace("–", " ").replace("−", "-")
    text = re.sub(r"[^\w\s$%./-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _base_qid(qid: str) -> str:
    """Map Round 2 ids (q21r2) back to the Round 1 trap id (q21)."""
    if qid.endswith("r2") and qid[:-2] in TRAP_SPECS:
        return qid[:-2]
    return qid


def _has_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def grade_trap(agent_answer: str, qid: str) -> dict:
    """
    Two-part trap grading:
      (a) current_fact — does the answer state the current correct value?
      (b) trap_aware — does it treat the legacy value as outdated (or omit it)?
    Accuracy uses (a) only. Never require the legacy value to appear.
    """
    base = _base_qid(qid)
    spec = TRAP_SPECS[base]
    a = normalize(agent_answer)
    if not a:
        return {
            "correct": False,
            "current_fact": False,
            "trap_aware": False,
            "wrong_claim": "",
        }

    current_ok = _has_any(a, spec["current_patterns"])

    # q24/q25: "yes" affirming the trap is a current-fact failure
    if base == "q24":
        yes_hipaa = bool(
            re.search(
                r"\byes\b.*\bhipaa\b|\bhipaa\b.*\b(available|offer|provide|included)\b",
                a,
                re.I,
            )
        )
        neg = bool(
            re.search(
                r"\b(no|not|does not|doesn't|unavailable|deferred|not offered|not available)\b",
                a,
                re.I,
            )
        )
        if yes_hipaa and not neg:
            current_ok = False
        elif neg:
            current_ok = True
    if base == "q25":
        affirms_convert = bool(
            re.search(r"\byes\b|convert(s|ed)?\s+to\s+(paid\s+)?credit", a, re.I)
        )
        neg = bool(re.search(r"\b(no|not|do not|don't|does not|doesn't)\b", a, re.I))
        if affirms_convert and not neg:
            current_ok = False
        elif neg and re.search(r"convert|credit|pilot", a, re.I):
            current_ok = True
        elif neg:
            current_ok = True

    legacy_present = _has_any(a, spec["legacy_patterns"])
    corrected = bool(CORRECTION_HINTS.search(a))
    if not legacy_present:
        trap_aware = True  # omitted legacy → not presenting it as current
    elif spec.get("legacy_ok_with_correction") and corrected and current_ok:
        trap_aware = True
    elif base in ("q24", "q25") and current_ok and not (
        legacy_present and not bool(re.search(r"\b(no|not)\b", a))
    ):
        trap_aware = current_ok
    else:
        # Legacy stated without clear correction → not trap-aware
        trap_aware = False
        # If they only state legacy and not current, current_ok may still be true
        # via a loose pattern — tighten: requiring current patterns is enough.

    # Extract a compact wrong-claim fingerprint for repeat-mistake matching
    wrong_claim = ""
    if not current_ok:
        for pat in spec["legacy_patterns"]:
            m = re.search(pat, a, re.I)
            if m:
                wrong_claim = normalize(m.group(0))
                break
        if not wrong_claim:
            wrong_claim = a[:160]

    return {
        "correct": bool(current_ok),
        "current_fact": bool(current_ok),
        "trap_aware": bool(trap_aware),
        "wrong_claim": wrong_claim,
    }


def grade(agent_answer: str, correct: str, qtype: str, qid: str) -> dict:
    """
    Deterministic grader. Returns dict with at least {"correct": bool}.
    Trap questions also include current_fact / trap_aware / wrong_claim.
    """
    base = _base_qid(qid)
    if base in TRAP_SPECS or qtype == "trap":
        return grade_trap(agent_answer, qid)

    a = normalize(agent_answer)
    c = normalize(correct)
    if not a:
        return {"correct": False}

    # q14 meaning check: Pro cannot + Enterprise required (word order varies)
    if base == "q14":
        neg = bool(re.search(r"\b(no|cannot|can't|can not|not)\b", a))
        ent = bool(re.search(r"\benterprise\b", a))
        onlyish = bool(re.search(r"\b(only|required|requires|must)\b", a))
        if neg and ent and onlyish:
            return {"correct": True}

    # Alias short-circuit: any listed phrasing present → accept
    for alias in ALIASES.get(base, []):
        if normalize(alias) in a:
            # Still require core numbers from the expected answer when present
            numbers = re.findall(r"\$?\d[\d,]*(?:\.\d+)?%?", c)
            # Filter date fragments that are noise when aliases already match meaning
            # For q14 there are no required numbers.
            required_nums = []
            for n in numbers:
                n2 = n.replace(",", "")
                # skip bare year-like tokens unless they look like money/counts we care about
                if re.fullmatch(r"\d{4}", n2):
                    continue
                required_nums.append(n2)
            if not required_nums or all(
                n.lower() in a.replace(",", "") for n in required_nums
            ):
                return {"correct": True}

    # Salient tokens from correct answer — skip legacy trap numbers that are
    # only mentioned as "do not quote / superseded" context (handled above for traps).
    numbers = re.findall(r"\$?\d[\d,]*(?:\.\d+)?%?", c)
    must_have: list[str] = []
    for n in numbers:
        n2 = n.replace(",", "")
        # Date components embedded in prose (2025-01-20 already matched as whole via phrase)
        if re.fullmatch(r"\d{1,2}", n2) and ("-" in correct or "/" in correct):
            # skip day/month fragments; prefer full ISO date phrases below
            continue
        must_have.append(n2)

    phrase_bank = [
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
        "enterprise plan only",
        "enterprise plans only",
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
        "cannot be stacked",
        "second approver",
        "canary",
    ]
    for phrase in phrase_bank:
        if phrase in c:
            must_have.append(phrase)

    # Deduplicate while preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for tok in must_have:
        key = tok.lower().replace(",", "")
        if key not in seen:
            seen.add(key)
            uniq.append(tok)
    must_have = uniq

    hits = 0
    need = 0
    for tok in must_have:
        need += 1
        tok_n = tok.lower().replace(",", "")
        if tok_n in a.replace(",", ""):
            hits += 1
        else:
            # soft match: "canary deploys" vs "canary"
            if tok_n == "canary" and "canary" in a:
                hits += 1

    if need == 0:
        c_words = set(re.findall(r"[a-z0-9$]+", c)) - {
            "the",
            "a",
            "an",
            "and",
            "or",
            "of",
            "per",
            "to",
            "for",
            "is",
            "in",
            "on",
        }
        a_words = set(re.findall(r"[a-z0-9$]+", a))
        if not c_words:
            return {"correct": False}
        return {"correct": len(c_words & a_words) / len(c_words) >= 0.45}

    # q17 expects canary + second-approver (+ status/PagerDuty). Partial
    # "second approver only" answers stay wrong — that was a real Phase-1 miss.
    if base == "q17":
        has_canary = bool(re.search(r"\bcanary\b", a))
        has_approver = bool(re.search(r"second\s+approver", a))
        return {"correct": has_canary and has_approver}

    return {"correct": hits / need >= 0.6}


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


def load_question_sets(limit: int | None = None) -> tuple[list[dict], list[dict]]:
    """Return (round1 questions, round2 trap paraphrases)."""
    all_q = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    round1 = [q for q in all_q if q.get("round", 1) == 1]
    round2 = [q for q in all_q if q.get("round") == 2]
    if limit:
        round1 = round1[:limit]
        # keep only Round 2 variants whose parent is in the limited set
        keep = {q["id"] for q in round1}
        round2 = [q for q in round2 if q.get("round2_of") in keep]
    return round1, round2


def run_condition(
    name: str,
    questions: list[dict],
    *,
    use_memory: bool,
    learn: bool,
    dream_after_half: bool = False,
    search_only: bool = False,
    search_plus_wiki: bool = False,
    reset: bool = True,
    round2_questions: list[dict] | None = None,
) -> dict:
    """Run questions under one condition. Returns metrics including Round 2."""
    results = []
    trap_meta: dict[str, dict] = {}  # qid -> grade details from round 1 traps
    total_tokens = 0
    trap_awareness_hits = 0
    trap_awareness_n = 0

    if reset and name in ("memory_notes", "memory_dream", "baseline"):
        reset_memory()
    elif reset and not use_memory and not search_only and not search_plus_wiki:
        reset_memory()

    half = max(1, len(questions) // 2)

    def _ask_one(q: dict, round_label: int) -> dict:
        nonlocal total_tokens, trap_awareness_hits, trap_awareness_n
        allow_corpus = True
        allow_memory = use_memory and not search_only
        allow_wiki = (use_memory or search_plus_wiki) and not search_only
        if search_only:
            allow_memory = False
            allow_wiki = False
        if search_plus_wiki:
            if not any(WIKI_DIR.glob("*.md")):
                run_dream(use_llm=bool(os.environ.get("ANTHROPIC_API_KEY")))
            allow_memory = True
            allow_wiki = True

        sid = (
            f"eval-{name}-r{round_label}-{q['id']}-"
            f"{datetime.now(timezone.utc).strftime('%H%M%S')}"
        )
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
        except SystemExit:
            raise
        except Exception as e:
            out = {
                "answer": f"ERROR: {e}",
                "input_tokens": 0,
                "docs_used": [],
            }

        g = grade(out.get("answer", ""), q["answer"], q.get("type", ""), q["id"])
        ok = bool(g.get("correct"))
        tokens = int(out.get("input_tokens") or 0)
        total_tokens += tokens

        if q.get("type") == "trap" or _base_qid(q["id"]) in TRAP_SPECS:
            trap_awareness_n += 1
            if g.get("trap_aware"):
                trap_awareness_hits += 1

        row = {
            "id": q["id"],
            "type": q.get("type"),
            "round": round_label,
            "round2_of": q.get("round2_of"),
            "correct": ok,
            "current_fact": g.get("current_fact", ok),
            "trap_aware": g.get("trap_aware"),
            "wrong_claim": g.get("wrong_claim", ""),
            "answer": out.get("answer"),
            "expected": q["answer"],
            "tokens": tokens,
            "docs_used": out.get("docs_used"),
        }
        print(
            f"  [{name}] r{round_label} {q['id']} "
            f"{'OK' if ok else 'FAIL'} tokens={tokens}"
        )
        return row

    for i, q in enumerate(questions):
        if dream_after_half and i == half:
            run_dream()
        row = _ask_one(q, 1)
        results.append(row)
        if q.get("type") == "trap":
            trap_meta[q["id"]] = row

    # Round 2: paraphrased traps (same memory state — do not reset)
    round2_results: list[dict] = []
    repeat_mistakes = 0
    loop_closed = 0
    if round2_questions:
        for q in round2_questions:
            row = _ask_one(q, 2)
            round2_results.append(row)
            parent = q.get("round2_of")
            r1 = trap_meta.get(parent)
            if not r1:
                continue
            if (not r1["correct"]) and (not row["correct"]):
                # same wrong claim?
                w1 = r1.get("wrong_claim") or normalize(r1.get("answer") or "")[:160]
                w2 = row.get("wrong_claim") or normalize(row.get("answer") or "")[:160]
                if w1 and w2 and (w1 == w2 or w1 in w2 or w2 in w1):
                    repeat_mistakes += 1
                else:
                    # both wrong, possibly different claims — still a repeat mistake
                    # if the legacy trap value is the shared failure mode
                    base = _base_qid(parent)
                    spec = TRAP_SPECS.get(base, {})
                    shared_legacy = False
                    for pat in spec.get("legacy_patterns", []):
                        if re.search(pat, r1.get("answer") or "", re.I) and re.search(
                            pat, row.get("answer") or "", re.I
                        ):
                            shared_legacy = True
                            break
                    if shared_legacy:
                        repeat_mistakes += 1
            elif (not r1["correct"]) and row["correct"]:
                loop_closed += 1

    all_rows = results + round2_results
    n = len(results) or 1  # accuracy is Round 1 only
    return {
        "condition": name,
        "accuracy": sum(1 for r in results if r["correct"]) / n,
        "correct_count": sum(1 for r in results if r["correct"]),
        "n": len(results),
        "repeat_mistakes": repeat_mistakes,
        "loop_closed": loop_closed,
        "trap_awareness": (
            trap_awareness_hits / trap_awareness_n if trap_awareness_n else None
        ),
        "trap_awareness_hits": trap_awareness_hits,
        "trap_awareness_n": trap_awareness_n,
        "avg_tokens": total_tokens / (len(all_rows) or 1),
        "avg_tokens_round1": total_tokens / n if not round2_results else (
            sum(r["tokens"] for r in results) / n
        ),
        "memory": memory_stats(),
        "results": results,
        "round2_results": round2_results,
    }


def format_table(rows: list[dict]) -> str:
    header = (
        "| Condition | Accuracy | Repeat mistakes | Loop closed | "
        "Avg tokens / question |"
    )
    sep = "|---|---|---|---|---|"
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
        # Search subsets have no Round 2 — show em dash for loop metrics
        if row["condition"] in ("search_alone", "search_wiki"):
            rm = "—"
            lc = "—"
        else:
            rm = str(row.get("repeat_mistakes", 0))
            lc = str(row.get("loop_closed", 0))
        tok = row.get("avg_tokens_round1", row["avg_tokens"])
        lines.append(f"| {label} | {acc} | {rm} | {lc} | {tok:.0f} |")
    return "\n".join(lines)


def run_eval(limit: int | None = None, skip_if_no_key: bool = False) -> dict:
    ensure_dirs()
    questions, round2 = load_question_sets(limit)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        empty = {
            "ran": False,
            "reason": "ANTHROPIC_API_KEY not set — eval did not run; no fabricated numbers.",
            "table_markdown": (
                "| Condition | Accuracy | Repeat mistakes | Loop closed | "
                "Avg tokens / question |\n"
                "|---|---|---|---|---|\n"
                "| No memory (baseline) | — | — | — | — |\n"
                "| Memory on (notes only, no dreaming) | — | — | — | — |\n"
                "| Memory + one dreaming pass | — | — | — | — |\n"
                "| Search alone vs search + wiki (on the cross-document subset) | — | — | — | — |\n"
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
        run_condition(
            "baseline",
            questions,
            use_memory=False,
            learn=False,
            round2_questions=round2,
        )
    )

    print("=== Condition 2: memory notes only (no dreaming) ===")
    rows.append(
        run_condition(
            "memory_notes",
            questions,
            use_memory=True,
            learn=True,
            round2_questions=round2,
        )
    )

    print("=== Condition 3: memory + one dreaming pass (midway) ===")
    rows.append(
        run_condition(
            "memory_dream",
            questions,
            use_memory=True,
            learn=True,
            dream_after_half=True,
            round2_questions=round2,
        )
    )

    print("=== Condition 4a: search alone (cross-doc subset) ===")
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
        "n_round2": len(round2),
        "n_cross": len(cross),
        "table_markdown": table,
        "conditions": [{k: v for k, v in r.items() if k not in ("results", "round2_results")} for r in rows],
        "details": rows,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "grader_note": (
            "Phase 2 grader: normalize case/punct/whitespace; per-question aliases; "
            "trap scoring splits current_fact (accuracy) vs trap_aware. "
            "Round 2 = 5 paraphrased traps for repeat-mistakes / loop-closed."
        ),
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
    parser.add_argument(
        "--self-test-grader",
        action="store_true",
        help="Run deterministic grader checks against known Phase-1 false negatives",
    )
    args = parser.parse_args(argv)
    if args.self_test_grader:
        return _self_test_grader()
    result = run_eval(limit=args.limit, skip_if_no_key=args.skip_api)
    if not result.get("ran") and not args.skip_api:
        return 0  # honest empty table is success for offline
    return 0


def _self_test_grader() -> int:
    """Confirm Fix 1 findings are resolved; q17 incomplete answer still fails."""
    cases = [
        (
            "q14",
            "cross",
            "No, a Pro customer cannot enable SAML SSO. SAML SSO is available on the Enterprise plan only.",
            "No — SSO is Enterprise only",
            True,
        ),
        (
            "q22",
            "trap",
            "For a new prospect, you should quote Atlas Pro at $49/seat/month for annual billing.",
            "$49/seat/month annual. Do not quote the legacy $39 price.",
            True,
        ),
        (
            "q23",
            "trap",
            "The default expiry for Atlas public dashboard share links is 7 days.",
            "7 days (changed 2025-03-11 from 30 days)",
            True,
        ),
        (
            "q22",
            "trap",
            "Quote the legacy price of $39/seat/month.",
            "$49/seat/month annual. Do not quote the legacy $39 price.",
            False,
        ),
        (
            "q17",
            "cross",
            "alert-evaluator deploys require a second approver.",
            "Canary deploys required for the evaluator (and status page auto-update hooked to PagerDuty); deploy runbook requires a second approver for alert-evaluator deploys",
            False,  # real miss: canary / status-page pieces absent
        ),
        (
            "q17",
            "cross",
            "Canary deploys are required for the evaluator, and the deploy runbook requires a second approver for alert-evaluator deploys.",
            "Canary deploys required for the evaluator (and status page auto-update hooked to PagerDuty); deploy runbook requires a second approver for alert-evaluator deploys",
            True,
        ),
    ]
    failed = 0
    for qid, qtype, ans, exp, want in cases:
        g = grade(ans, exp, qtype, qid)
        ok = g["correct"] == want
        print(f"{'PASS' if ok else 'FAIL'} {qid} got={g['correct']} want={want} detail={g}")
        if not ok:
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
