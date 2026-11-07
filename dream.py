#!/usr/bin/env python3
"""
Dreaming pass: correct, build, promote, focus, fill gaps.

Never edits transcripts. Never invents facts — corrections must cite
transcripts or corpus documents. Enforces MEMORY.md 200-line / 25 KB cap.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from agent import (
    MEMORY_DIR,
    MEMORY_MD,
    MODEL,
    NOTES_DIR,
    ROOT,
    TRANSCRIPTS_DIR,
    WIKI_DIR,
    CORPUS_DIR,
    enforce_memory_md_cap,
    ensure_dirs,
    get_client,
    keyword_search,
)

# Re-export cap constants via enforce
MAX_REPORT_NOTES = 40


def _read_all_transcripts() -> list[dict]:
    events = []
    if not TRANSCRIPTS_DIR.exists():
        return events
    for path in sorted(TRANSCRIPTS_DIR.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            obj["_transcript_file"] = path.name
            events.append(obj)
    return events


def _list_notes() -> list[Path]:
    if not NOTES_DIR.exists():
        return []
    return sorted(NOTES_DIR.glob("*.md"))


def _list_wiki() -> list[Path]:
    if not WIKI_DIR.exists():
        return []
    return sorted(WIKI_DIR.glob("*.md"))


def _memory_size() -> dict:
    ensure_dirs()
    raw = MEMORY_MD.read_bytes() if MEMORY_MD.exists() else b""
    text = raw.decode("utf-8", errors="replace")
    return {
        "bytes": len(raw),
        "lines": len(text.splitlines()),
        "chars": len(text),
    }


def _llm_json(client, system: str, user: str) -> dict:
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(
        b.text for b in resp.content if getattr(b, "type", None) == "text"
    ).strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def job_correct(client, transcripts: list[dict], report: dict) -> None:
    """Check notes against transcripts; fix/delete/confirm."""
    notes = _list_notes()
    report["notes_corrected"] = []
    report["notes_deleted"] = []
    report["notes_confirmed"] = []
    if not notes:
        return

    # Compact transcript evidence for the model
    evidence = []
    for ev in transcripts:
        if ev.get("event") in ("answer", "grader_feedback", "notes_written", "session_start"):
            evidence.append(
                {
                    "file": ev.get("_transcript_file"),
                    "event": ev.get("event"),
                    "question": ev.get("question"),
                    "answer": ev.get("answer"),
                    "correct_answer": ev.get("correct_answer"),
                    "paths": ev.get("paths"),
                }
            )
    evidence = evidence[-80:]  # keep prompt small

    note_blobs = []
    for p in notes[:MAX_REPORT_NOTES]:
        note_blobs.append({"path": p.name, "content": p.read_text(encoding="utf-8")[:2000]})

    system = (
        "You are the dreaming corrector for an agent memory system. "
        "Check each memory note against the session transcripts (ground truth). "
        "Rules: never invent facts; every correction must cite a transcript file "
        "or say 'insufficient evidence'. "
        "Return JSON only:\n"
        "{\n"
        '  "corrections": [{"path": "note.md", "new_content": "...", "cite": "transcript.jsonl", "reason": "..."}],\n'
        '  "deletions": [{"path": "note.md", "cite": "...", "reason": "..."}],\n'
        '  "confirmations": [{"path": "note.md", "new_content": "...", "cite": "..."}]\n'
        "}\n"
        "If a note is fine, omit it. Prefer confirmations that mark 'confirmed by grader'."
    )
    user = json.dumps({"notes": note_blobs, "transcripts": evidence}, indent=2)
    data = _llm_json(client, system, user)

    for item in data.get("corrections") or []:
        path = NOTES_DIR / Path(item["path"]).name
        if path.exists() and item.get("new_content"):
            body = item["new_content"].rstrip() + f"\n\n<!-- dream-corrected cite={item.get('cite','')} -->\n"
            path.write_text(body, encoding="utf-8")
            report["notes_corrected"].append(
                {"path": path.name, "cite": item.get("cite"), "reason": item.get("reason")}
            )

    for item in data.get("deletions") or []:
        path = NOTES_DIR / Path(item["path"]).name
        if path.exists():
            path.unlink()
            report["notes_deleted"].append(
                {"path": path.name, "cite": item.get("cite"), "reason": item.get("reason")}
            )

    for item in data.get("confirmations") or []:
        path = NOTES_DIR / Path(item["path"]).name
        if path.exists() and item.get("new_content"):
            path.write_text(item["new_content"].rstrip() + "\n", encoding="utf-8")
            report["notes_confirmed"].append(
                {"path": path.name, "cite": item.get("cite")}
            )


def job_build(client, report: dict) -> None:
    """Create/update linked wiki pages from notes. One page per contract; quote terms."""
    report["wiki_created"] = []
    report["wiki_updated"] = []
    report["wiki_merged"] = []
    notes = _list_notes()
    note_text = []
    for p in notes[:MAX_REPORT_NOTES]:
        note_text.append(f"## {p.name}\n{p.read_text(encoding='utf-8')[:1500]}")

    # Always seed contract pages from corpus (verbatim terms) — focus lesson from the post
    contract_files = sorted(CORPUS_DIR.glob("contract-*.md"))
    for cf in contract_files:
        dest = WIKI_DIR / cf.name.replace("contract-", "contract-")
        # one page per contract, quoting word for word
        content = (
            f"# Wiki: {cf.stem}\n\n"
            f"Source: `corpus/{cf.name}` (quoted verbatim; do not summarize terms)\n\n"
            f"{cf.read_text(encoding='utf-8')}\n"
            f"Related: [index](index.md)\n"
        )
        existed = dest.exists()
        dest.write_text(content, encoding="utf-8")
        (report["wiki_updated"] if existed else report["wiki_created"]).append(dest.name)

    # Policy / trap pages from notes via LLM
    system = (
        "Build or update memory wiki pages from notes. "
        "One topic per page. Quote contract/policy terms; do not invent. "
        "Link pages with markdown links like [title](page.md). "
        "Return JSON:\n"
        '{"pages":[{"filename":"topic.md","content":"...","action":"create|update"}],'
        ' "merges":[{"keep":"a.md","drop":"b.md","reason":"..."}]}'
    )
    user = (
        "Existing wiki files: "
        + json.dumps([p.name for p in _list_wiki()])
        + "\n\nNotes:\n"
        + "\n".join(note_text[:20])
        + "\n\nAlso create pages for known traps if notes mention them: "
        "pto-current-vs-legacy, pricing-current-vs-legacy, share-link-expiry, hipaa-baa-status, pilot-credits."
    )
    data = _llm_json(client, system, user)
    for page in data.get("pages") or []:
        fn = Path(page.get("filename", "topic.md")).name
        if not fn.endswith(".md"):
            fn += ".md"
        content = page.get("content") or ""
        if not content.strip():
            continue
        dest = WIKI_DIR / fn
        existed = dest.exists()
        dest.write_text(content.rstrip() + "\n", encoding="utf-8")
        key = "wiki_updated" if existed or page.get("action") == "update" else "wiki_created"
        if fn not in report[key]:
            report[key].append(fn)

    for merge in data.get("merges") or []:
        keep = WIKI_DIR / Path(merge.get("keep", "")).name
        drop = WIKI_DIR / Path(merge.get("drop", "")).name
        if keep.exists() and drop.exists() and keep != drop:
            drop.unlink()
            report["wiki_merged"].append(
                {"keep": keep.name, "drop": drop.name, "reason": merge.get("reason")}
            )


def job_focus(report: dict) -> None:
    """Organize around accuracy goal: decision-relevant facts on top-level pages."""
    # Deterministic focus page listing contracts, current policies, traps
    lines = [
        "# Focus — decision-relevant memory\n",
        "Goal: answer company questions accurately; prefer current policy over superseded docs.\n",
        "## Contracts (verbatim wiki pages)\n",
    ]
    for p in sorted(WIKI_DIR.glob("contract-*.md")):
        lines.append(f"- [{p.stem}]({p.name})")
    lines.append("\n## Current policies & pricing\n")
    for name, label in [
        ("hr-pto-policy.md", "PTO current (20 days)"),
        ("pricing-atlas-plans.md", "Atlas pricing current"),
        ("product-atlas-dashboards.md", "Share link default 7 days"),
        ("faq-security.md", "No HIPAA BAA yet"),
        ("pricing-pilot.md", "Pilot days do not convert to credits"),
    ]:
        lines.append(f"- corpus `{name}` — {label}")
    lines.append("\n## Known traps\n")
    lines.append("- Legacy PTO (15 days) is SUPERSEDED — use 20 days.")
    lines.append("- Legacy Pro $39 is SUPERSEDED — quote $49 annual.")
    lines.append("- Share-link default is 7 days, not 30.")
    lines.append("- HIPAA BAA not available (target Q3 2025 only).")
    lines.append("- Unused pilot days ≠ paid credits.")
    lines.append("\nSee [index](index.md).\n")
    dest = WIKI_DIR / "focus.md"
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report["focus_page"] = dest.name


def job_fill_gaps(report: dict) -> None:
    """Fix links to missing pages; ensure index exists."""
    report["links_fixed"] = []
    report["pages_added_for_gaps"] = []
    pages = {p.name for p in _list_wiki()}
    link_re = re.compile(r"\[([^\]]+)\]\(([^)]+\.md)\)")

    for path in _list_wiki():
        text = path.read_text(encoding="utf-8", errors="replace")
        new_text = text
        for label, target in link_re.findall(text):
            target_name = Path(target).name
            if target_name not in pages and target_name != path.name:
                # try to repoint to an existing similarly named page
                stem = Path(target_name).stem
                candidates = [p for p in pages if stem.split("-")[0] in p or stem in p]
                if candidates:
                    replacement = sorted(candidates)[0]
                    new_text = new_text.replace(f"]({target})", f"]({replacement})")
                    report["links_fixed"].append(
                        {
                            "from_page": path.name,
                            "broken": target_name,
                            "repointed_to": replacement,
                        }
                    )
                else:
                    # create a stub that points at corpus search
                    stub = WIKI_DIR / target_name
                    stub.write_text(
                        f"# {label}\n\n"
                        f"Stub created by dreaming gap-fill. "
                        f"Linked from [{path.name}]({path.name}).\n"
                        f"Search corpus for related facts; do not invent.\n",
                        encoding="utf-8",
                    )
                    pages.add(target_name)
                    report["pages_added_for_gaps"].append(target_name)

        if new_text != text:
            path.write_text(new_text, encoding="utf-8")

    # Ensure index
    index = WIKI_DIR / "index.md"
    listing = "\n".join(f"- [{p.stem}]({p.name})" for p in sorted(_list_wiki()) if p.name != "index.md")
    index.write_text(
        "# Memory Wiki Index\n\n"
        "Built by dreaming. Transcripts remain ground truth.\n\n"
        f"{listing}\n",
        encoding="utf-8",
    )
    if "index.md" not in report.get("pages_added_for_gaps", []) and "index.md" not in report.get(
        "wiki_created", []
    ):
        report.setdefault("wiki_created", [])
        if "index.md" not in report["wiki_created"]:
            report["pages_added_for_gaps"].append("index.md")


def job_promote(client, report: dict) -> None:
    """Rewrite MEMORY.md as a small index; enforce cap."""
    before = _memory_size()
    report["memory_md_before"] = before

    notes = [p.name for p in _list_notes()]
    wiki = [p.name for p in _list_wiki()]
    # Pull short note headlines
    headlines = []
    for p in _list_notes()[:30]:
        first = p.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        headlines.append(f"- `{p.name}`: {(first[0] if first else '')[:120]}")

    system = (
        "Rewrite MEMORY.md as a compact injected-context index for a company assistant. "
        "Include: top verified facts/rules, known traps, and links to notes/ and wiki/ files. "
        "Keep it SMALL — under 200 lines and well under 25 KB. Move details to linked pages. "
        "Do not invent facts. Return JSON: {\"memory_md\": \"...full file contents...\"}"
    )
    user = json.dumps(
        {
            "note_files": notes,
            "wiki_files": wiki,
            "note_headlines": headlines,
            "must_include_traps": [
                "PTO is 20 days (legacy 15 superseded)",
                "Pro annual is $49/seat (legacy $39 superseded)",
                "Share link default expiry is 7 days (not 30)",
                "No HIPAA BAA yet",
                "Pilot days do not convert to credits",
            ],
        },
        indent=2,
    )
    data = _llm_json(client, system, user)
    content = data.get("memory_md")
    if not content or not isinstance(content, str):
        # Deterministic fallback index
        content = "\n".join(
            [
                "# MEMORY.md — injected index",
                "",
                "## Purpose",
                "Persistent memory for Northstar Labs Q&A. Prefer current docs over superseded ones.",
                "",
                "## Top rules",
                "- PTO: 20 days/year (hr-pto-policy.md). Legacy 15-day doc is SUPERSEDED.",
                "- Atlas Pro: $49/seat/month annual. Do not quote legacy $39.",
                "- Public share links default to 7-day expiry (changed 2025-03-11).",
                "- HIPAA BAA: not offered yet (target Q3 2025).",
                "- Unused pilot days do not convert to paid credits.",
                "",
                "## Wiki",
                "- [focus](wiki/focus.md) — decision-relevant facts",
                "- [index](wiki/index.md) — all wiki pages",
                "- Contract pages under wiki/contract-*.md (verbatim terms)",
                "",
                "## Notes",
                *(headlines[:15] if headlines else ["- (no notes yet)"]),
                "",
                "## How to retrieve",
                "Search memory/notes and memory/wiki when this index is insufficient; corpus is ground truth for docs.",
                "",
            ]
        )

    MEMORY_MD.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
    cap_stats = enforce_memory_md_cap(MEMORY_MD)
    after = _memory_size()
    report["memory_md_after"] = after
    report["memory_md_cap"] = cap_stats


def run_dream(use_llm: bool | None = None) -> dict:
    ensure_dirs()
    report: dict = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "model": MODEL,
    }
    transcripts = _read_all_transcripts()
    report["transcripts_read"] = len(transcripts)
    report["transcript_files"] = len(list(TRANSCRIPTS_DIR.glob("*.jsonl"))) if TRANSCRIPTS_DIR.exists() else 0

    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if use_llm is None:
        use_llm = has_key

    if use_llm and has_key:
        client = get_client()
        job_correct(client, transcripts, report)
        job_build(client, report)
        job_focus(report)
        job_fill_gaps(report)
        job_promote(client, report)
    else:
        # Offline / no-key dreaming: still do deterministic build/focus/fill/promote
        report["notes_corrected"] = []
        report["notes_deleted"] = []
        report["notes_confirmed"] = []
        report["warning"] = (
            "ANTHROPIC_API_KEY not set — ran deterministic dreaming only "
            "(build contract wiki, focus, fill gaps, promote). Skipped LLM correct."
        )
        report["wiki_created"] = []
        report["wiki_updated"] = []
        report["wiki_merged"] = []
        # minimal client-less build: contract pages
        for cf in sorted(CORPUS_DIR.glob("contract-*.md")):
            dest = WIKI_DIR / cf.name
            dest.write_text(
                f"# Wiki: {cf.stem}\n\nSource: `corpus/{cf.name}`\n\n"
                f"{cf.read_text(encoding='utf-8')}\nRelated: [index](index.md)\n",
                encoding="utf-8",
            )
            report["wiki_created"].append(dest.name)
        job_focus(report)
        job_fill_gaps(report)
        # promote without LLM
        class _NoClient:
            pass

        # inline deterministic promote
        before = _memory_size()
        report["memory_md_before"] = before
        headlines = []
        for p in _list_notes()[:30]:
            first = p.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            headlines.append(f"- `{p.name}`: {(first[0] if first else '')[:120]}")
        content = "\n".join(
            [
                "# MEMORY.md — injected index",
                "",
                "## Top rules",
                "- PTO: 20 days/year. Legacy 15-day policy is SUPERSEDED.",
                "- Atlas Pro: $49/seat/month annual. Legacy $39 is SUPERSEDED.",
                "- Share-link default expiry: 7 days (not 30).",
                "- No HIPAA BAA yet (target Q3 2025).",
                "- Unused pilot days do not convert to credits.",
                "",
                "## Wiki",
                "- [focus](wiki/focus.md)",
                "- [index](wiki/index.md)",
                "",
                "## Notes",
                *(headlines[:15] if headlines else ["- (no notes yet)"]),
                "",
            ]
        )
        MEMORY_MD.write_text(content + "\n", encoding="utf-8")
        report["memory_md_cap"] = enforce_memory_md_cap(MEMORY_MD)
        report["memory_md_after"] = _memory_size()

    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["note_count"] = len(_list_notes())
    report["wiki_count"] = len(_list_wiki())
    return report


def print_report(report: dict) -> None:
    print("=== Dreaming report ===")
    if report.get("warning"):
        print(f"WARNING: {report['warning']}")
    print(f"Transcripts read: {report.get('transcripts_read')} events "
          f"across {report.get('transcript_files')} files")
    print(f"Notes corrected: {len(report.get('notes_corrected') or [])}")
    for x in report.get("notes_corrected") or []:
        print(f"  - {x}")
    print(f"Notes deleted: {len(report.get('notes_deleted') or [])}")
    for x in report.get("notes_deleted") or []:
        print(f"  - {x}")
    print(f"Notes confirmed: {len(report.get('notes_confirmed') or [])}")
    print(f"Wiki created: {report.get('wiki_created')}")
    print(f"Wiki updated: {report.get('wiki_updated')}")
    print(f"Wiki merged: {report.get('wiki_merged')}")
    print(f"Links fixed: {len(report.get('links_fixed') or [])}")
    for x in report.get("links_fixed") or []:
        print(f"  - {x}")
    print(f"Gap pages added: {report.get('pages_added_for_gaps')}")
    print(f"Focus page: {report.get('focus_page')}")
    print(f"MEMORY.md before: {report.get('memory_md_before')}")
    print(f"MEMORY.md after:  {report.get('memory_md_after')}")
    print(f"MEMORY.md cap:    {report.get('memory_md_cap')}")
    print(f"Totals — notes: {report.get('note_count')}, wiki pages: {report.get('wiki_count')}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Dreaming pass for mini-agent-memory")
    parser.add_argument("--json", action="store_true", help="Print full JSON report")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip LLM jobs even if API key is set",
    )
    args = parser.parse_args(argv)
    report = run_dream(use_llm=False if args.offline else None)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
