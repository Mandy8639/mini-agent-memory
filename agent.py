#!/usr/bin/env python3
"""
mini-agent-memory: one-session company assistant.

Each `ask` run is a fresh session with only memory/MEMORY.md injected.
The agent may search the corpus and read memory notes/wiki when the index points there.
After answering, the harness reveals the correct answer and the agent may write notes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# --- config (change model here) ---
MODEL = "claude-sonnet-4-5-20250929"
MAX_MEMORY_LINES = 200
MAX_MEMORY_BYTES = 25 * 1024  # 25 KB

ROOT = Path(__file__).resolve().parent
CORPUS_DIR = ROOT / "corpus"
MEMORY_DIR = ROOT / "memory"
TRANSCRIPTS_DIR = MEMORY_DIR / "transcripts"
NOTES_DIR = MEMORY_DIR / "notes"
WIKI_DIR = MEMORY_DIR / "wiki"
MEMORY_MD = MEMORY_DIR / "MEMORY.md"
QUESTIONS_PATH = ROOT / "questions.json"

MEMORY_PURPOSE = (
    "You have a memory folder that persists between sessions. Memory is for things a "
    "future session will need that it cannot easily work out again: verified facts, "
    "rules you tested, mistakes you made and why, and traps you found. Do not write "
    "down state you can look up again in the documents. Judge for yourself what is "
    "worth remembering."
)


def ensure_dirs() -> None:
    for d in (TRANSCRIPTS_DIR, NOTES_DIR, WIKI_DIR):
        d.mkdir(parents=True, exist_ok=True)
    if not MEMORY_MD.exists():
        MEMORY_MD.write_text(
            "# MEMORY.md — injected index\n\n"
            "(empty — run sessions and `dream` to populate)\n",
            encoding="utf-8",
        )


def load_memory_md() -> str:
    """Load injected context, enforcing the 200-line / 25 KB cap in code."""
    ensure_dirs()
    raw = MEMORY_MD.read_bytes()
    truncated_bytes = False
    if len(raw) > MAX_MEMORY_BYTES:
        raw = raw[:MAX_MEMORY_BYTES]
        truncated_bytes = True
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    truncated_lines = False
    if len(lines) > MAX_MEMORY_LINES:
        lines = lines[:MAX_MEMORY_LINES]
        truncated_lines = True
        text = "".join(lines)
    if truncated_bytes or truncated_lines:
        text += (
            "\n\n[truncated: MEMORY.md capped at "
            f"{MAX_MEMORY_LINES} lines / {MAX_MEMORY_BYTES} bytes]\n"
        )
    return text


def enforce_memory_md_cap(path: Path | None = None) -> dict:
    """Rewrite MEMORY.md so it fits the cap. Returns size stats."""
    path = path or MEMORY_MD
    ensure_dirs()
    if not path.exists():
        path.write_text("# MEMORY.md\n", encoding="utf-8")
    raw = path.read_bytes()
    before = {"bytes": len(raw), "lines": raw.decode("utf-8", errors="replace").count("\n") + (0 if raw.endswith(b"\n") or not raw else 1)}
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    changed = False
    if len(raw) > MAX_MEMORY_BYTES or len(lines) > MAX_MEMORY_LINES:
        # Prefer line cap first, then byte cap.
        lines = lines[:MAX_MEMORY_LINES]
        encoded = "".join(lines).encode("utf-8")
        while len(encoded) > MAX_MEMORY_BYTES and lines:
            lines.pop()
            encoded = "".join(lines).encode("utf-8")
        if not encoded.endswith(b"\n"):
            encoded += b"\n"
        path.write_bytes(encoded)
        changed = True
        text = encoded.decode("utf-8")
    after_raw = path.read_bytes()
    after = {
        "bytes": len(after_raw),
        "lines": len(after_raw.decode("utf-8", errors="replace").splitlines()),
        "truncated": changed,
        "within_cap": len(after_raw) <= MAX_MEMORY_BYTES
        and len(after_raw.decode("utf-8", errors="replace").splitlines()) <= MAX_MEMORY_LINES,
    }
    after["before"] = before
    return after


def keyword_search(query: str, directory: Path, top_k: int = 5) -> list[dict]:
    """Simple keyword search over markdown files. No vector DB."""
    tokens = [t.lower() for t in re.findall(r"[a-zA-Z0-9_./$-]+", query) if len(t) > 2]
    if not tokens:
        return []
    results = []
    if not directory.exists():
        return []
    for path in sorted(directory.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lower = text.lower()
        score = sum(lower.count(tok) for tok in tokens)
        # Bonus if filename tokens match
        name = path.name.lower()
        score += sum(3 for tok in tokens if tok in name)
        if score > 0:
            rel = path.relative_to(directory) if directory in path.parents or path.parent == directory else path.name
            results.append(
                {
                    "path": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
                    "score": score,
                    "snippet": text[:500],
                }
            )
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:top_k]


def read_file_safe(rel_or_abs: str) -> str:
    """Read a file under ROOT only."""
    p = Path(rel_or_abs)
    if not p.is_absolute():
        p = ROOT / p
    p = p.resolve()
    if not str(p).startswith(str(ROOT.resolve())):
        return f"ERROR: path escapes project root: {rel_or_abs}"
    if not p.exists():
        return f"ERROR: file not found: {rel_or_abs}"
    data = p.read_bytes()
    if len(data) > 50_000:
        return data[:50_000].decode("utf-8", errors="replace") + "\n[truncated]\n"
    return data.decode("utf-8", errors="replace")


def get_client():
    try:
        import anthropic
    except ImportError as e:
        raise SystemExit(
            "anthropic package not installed. Run: pip install -r requirements.txt"
        ) from e
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set. Export it or copy .env.example to .env and load it."
        )
    return anthropic.Anthropic(api_key=api_key)


def _tool_schemas() -> list[dict]:
    return [
        {
            "name": "search_corpus",
            "description": "Keyword search over company documents in corpus/.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["query"],
            },
        },
        {
            "name": "search_memory",
            "description": "Keyword search over memory/notes and memory/wiki.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["query"],
            },
        },
        {
            "name": "read_file",
            "description": "Read a project-relative markdown file (corpus/ or memory/).",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    ]


def _run_tool(name: str, inputs: dict) -> str:
    if name == "search_corpus":
        hits = keyword_search(inputs["query"], CORPUS_DIR, top_k=int(inputs.get("top_k", 5)))
        return json.dumps(hits, indent=2)
    if name == "search_memory":
        notes = keyword_search(inputs["query"], NOTES_DIR, top_k=int(inputs.get("top_k", 5)))
        wiki = keyword_search(inputs["query"], WIKI_DIR, top_k=int(inputs.get("top_k", 5)))
        return json.dumps({"notes": notes, "wiki": wiki}, indent=2)
    if name == "read_file":
        return read_file_safe(inputs["path"])
    return f"ERROR: unknown tool {name}"


def _message_text(message) -> str:
    parts = []
    for block in message.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


def call_with_tools(
    client,
    system: str,
    user: str,
    tools: list[dict] | None = None,
    max_rounds: int = 8,
    transcript: list | None = None,
) -> tuple[str, int, list]:
    """Tool-using chat. Returns (final_text, input_tokens_approx, tool_trace)."""
    tools = tools or _tool_schemas()
    messages = [{"role": "user", "content": user}]
    tool_trace: list[dict] = []
    input_tokens = 0
    output_tokens = 0

    for _ in range(max_rounds):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=system,
            tools=tools,
            messages=messages,
        )
        usage = getattr(resp, "usage", None)
        if usage:
            input_tokens += getattr(usage, "input_tokens", 0) or 0
            output_tokens += getattr(usage, "output_tokens", 0) or 0

        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        if resp.stop_reason == "tool_use" and tool_uses:
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for tu in tool_uses:
                out = _run_tool(tu.name, tu.input)
                tool_trace.append({"tool": tu.name, "input": tu.input, "output_preview": out[:400]})
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": out,
                    }
                )
            messages.append({"role": "user", "content": results})
            continue

        text = _message_text(resp)
        if transcript is not None:
            transcript.append(
                {
                    "event": "model_final",
                    "text": text,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                }
            )
        return text, input_tokens, tool_trace

    return _message_text(resp), input_tokens, tool_trace


def parse_answer_json(text: str) -> dict:
    """Extract JSON answer object from model text."""
    # Try fenced block first
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    blob = m.group(1) if m else None
    if not blob:
        m = re.search(r"\{[^{}]*\"answer\"[^{}]*\}", text, re.DOTALL)
        blob = m.group(0) if m else None
    if blob:
        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            pass
    return {"answer": text.strip(), "docs_used": [], "raw": True}


def write_note(filename: str, content: str) -> str:
    ensure_dirs()
    # sanitize filename
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", filename).strip("-") or "note"
    if not safe.endswith(".md"):
        safe += ".md"
    path = NOTES_DIR / safe
    path.write_text(content.strip() + "\n", encoding="utf-8")
    return str(path.relative_to(ROOT))


def learn_from_feedback(
    client,
    question: str,
    agent_answer: str,
    correct_answer: str,
    session_id: str,
    tool_trace: list,
) -> list[str]:
    """After grader reveals the correct answer, let the agent write memory notes."""
    system = (
        "You are updating your persistent memory after receiving the correct answer.\n"
        f"{MEMORY_PURPOSE}\n\n"
        "If you write a note, respond with JSON only:\n"
        '{"notes":[{"filename":"...md","content":"..."}]}\n'
        "Each note body must include: the fact/rule, how you know it is true "
        "(confirmed by the grader / unconfirmed), the session id, and — for failures — "
        "a theory of what went wrong.\n"
        'If nothing is worth remembering, return {"notes":[]}.'
    )
    user = (
        f"Session: {session_id}\n"
        f"Question: {question}\n"
        f"Your answer: {agent_answer}\n"
        f"Correct answer: {correct_answer}\n"
        f"Tools you used (preview): {json.dumps(tool_trace)[:2000]}\n"
    )
    text, _, _ = call_with_tools(client, system, user, tools=[], max_rounds=1)
    written: list[str] = []
    try:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        data = json.loads(m.group(0)) if m else {"notes": []}
    except json.JSONDecodeError:
        data = {"notes": []}
    for note in data.get("notes") or []:
        fn = note.get("filename") or f"note-{session_id}.md"
        content = note.get("content") or ""
        if content.strip():
            written.append(write_note(fn, content))
    return written


def append_transcript(session_id: str, record: dict) -> Path:
    ensure_dirs()
    path = TRANSCRIPTS_DIR / f"{session_id}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def run_ask(
    question: str,
    correct_answer: str | None = None,
    session_id: str | None = None,
    learn: bool = True,
    allow_corpus: bool = True,
    allow_memory_search: bool = True,
    allow_wiki: bool = True,
) -> dict:
    """Run one fresh session. Returns result dict for eval/harness."""
    ensure_dirs()
    client = get_client()
    session_id = session_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    memory_text = load_memory_md()

    tools = []
    if allow_corpus:
        tools.append(_tool_schemas()[0])  # search_corpus
    if allow_memory_search:
        tools.append(_tool_schemas()[1])  # search_memory
    tools.append(_tool_schemas()[2])  # read_file always (scoped by paths agent chooses)

    wiki_hint = ""
    if not allow_wiki:
        wiki_hint = (
            " Do not read files under memory/wiki/; you may use notes and corpus only."
            if allow_memory_search
            else " Do not use memory notes or wiki; corpus search only."
        )

    system = (
        "You are a company assistant for Northstar Labs. "
        "Answer questions using the corpus and your memory.\n"
        f"{MEMORY_PURPOSE}\n\n"
        "Injected memory index (MEMORY.md):\n"
        f"{memory_text}\n\n"
        "Use tools to search and read files before answering when needed. "
        "Prefer current policies over superseded documents. "
        "Watch for traps (legacy pricing, outdated defaults).\n"
        "When done, respond with a short JSON object:\n"
        '{"answer": "...", "docs_used": ["file.md", ...]}\n'
        f"{wiki_hint}"
    )

    # Restrict tools for search-alone condition
    if not allow_memory_search and not allow_wiki:
        tools = [t for t in _tool_schemas() if t["name"] in ("search_corpus", "read_file")]
        system = (
            "You are a company assistant for Northstar Labs. "
            "Answer using corpus search only — you have no persistent memory.\n"
            "Use tools to search and read corpus files.\n"
            'Respond with JSON: {"answer": "...", "docs_used": ["file.md", ...]}\n'
        )

    transcript_events: list = []
    append_transcript(
        session_id,
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "session_start",
            "question": question,
            "memory_injected_chars": len(memory_text),
        },
    )

    text, input_tokens, tool_trace = call_with_tools(
        client, system, f"Question: {question}", tools=tools, transcript=transcript_events
    )
    parsed = parse_answer_json(text)
    agent_answer = parsed.get("answer", text)

    append_transcript(
        session_id,
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "answer",
            "answer": agent_answer,
            "docs_used": parsed.get("docs_used", []),
            "tool_trace": tool_trace,
            "input_tokens": input_tokens,
            "raw_text": text,
        },
    )

    notes_written: list[str] = []
    if learn and correct_answer is not None:
        append_transcript(
            session_id,
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": "grader_feedback",
                "correct_answer": correct_answer,
            },
        )
        notes_written = learn_from_feedback(
            client, question, agent_answer, correct_answer, session_id, tool_trace
        )
        append_transcript(
            session_id,
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": "notes_written",
                "paths": notes_written,
            },
        )

    result = {
        "session_id": session_id,
        "question": question,
        "answer": agent_answer,
        "docs_used": parsed.get("docs_used", []),
        "correct_answer": correct_answer,
        "notes_written": notes_written,
        "input_tokens": input_tokens,
        "tool_trace": tool_trace,
    }
    return result


def load_question(qid: str) -> dict:
    questions = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    for q in questions:
        if q["id"] == qid:
            return q
    raise SystemExit(f"Unknown question id: {qid}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="mini-agent-memory agent")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ask_p = sub.add_parser("ask", help="Run one session")
    ask_p.add_argument("--question", "-q", help="Free-form question text")
    ask_p.add_argument("--id", help="Question id from questions.json")
    ask_p.add_argument("--no-learn", action="store_true", help="Skip post-answer note writing")
    ask_p.add_argument("--session-id", default=None)

    dream_p = sub.add_parser("dream", help="Run dreaming pass (delegates to dream.py)")
    eval_p = sub.add_parser("eval", help="Run eval harness (delegates to eval.py)")
    eval_p.add_argument("--limit", type=int, default=None, help="Shrink question set")
    eval_p.add_argument("--skip-api", action="store_true", help="Print empty table if no key")

    args = parser.parse_args(argv)

    if args.cmd == "dream":
        from dream import run_dream

        report = run_dream()
        print(json.dumps(report, indent=2))
        return 0

    if args.cmd == "eval":
        from eval import run_eval

        run_eval(limit=args.limit, skip_if_no_key=args.skip_api)
        return 0

    # ask
    ensure_dirs()
    if args.id:
        q = load_question(args.id)
        question = q["question"]
        correct = q["answer"]
        sid = args.session_id or f"ask-{args.id}-{datetime.now(timezone.utc).strftime('%H%M%S')}"
    elif args.question:
        question = args.question
        correct = None
        sid = args.session_id
    else:
        print("Provide --question or --id", file=sys.stderr)
        return 2

    result = run_ask(
        question,
        correct_answer=correct if not args.no_learn else None,
        session_id=sid,
        learn=not args.no_learn and correct is not None,
    )
    print(json.dumps({k: v for k, v in result.items() if k != "tool_trace"}, indent=2))
    if result.get("tool_trace"):
        print(f"\n(tools used: {len(result['tool_trace'])})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
