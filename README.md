# mini-agent-memory

Tiny, honest reproduction of the memory stack in Anthropic’s
[The components of agent memory](https://www.anthropic.com/research/components-of-agent-memory)
(Lance Martin, Nov 6 2026).

A company-assistant CLI answers questions over a small Northstar Labs corpus.
Each question is a **fresh session** with only `memory/MEMORY.md` injected
(CL-Bench-style). After the grader reveals the correct answer, the agent may
write notes. A separate **dreaming** pass corrects notes, builds a wiki,
promotes an index, focuses on decision-relevant facts, and fills link gaps.

No fine-tuning. No vector DB. Memory is files only.

## Method (~10 lines)

1. **Transcripts** — append-only JSONL per session under `memory/transcripts/` (ground truth; dreaming never edits them).
2. **Notes** — optional markdown lessons written after grader feedback (`memory/notes/`).
3. **Wiki** — linked pages built by dreaming (`memory/wiki/`), including one page per contract with terms quoted verbatim.
4. **Injected context** — `memory/MEMORY.md`, capped in code at **200 lines / 25 KB**, loaded at the start of every session.
5. **Dreaming** — offline pass that corrects notes against transcripts, builds/merges wiki pages, promotes a small index, focuses on traps/contracts, and fixes broken links.
6. **Eval** — same 25 questions under four conditions; reports accuracy, repeat trap mistakes, and avg input tokens.

## Setup

```bash
cd mini-agent-memory
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # set ANTHROPIC_API_KEY
export ANTHROPIC_API_KEY=...   # or: set -a; source .env; set +a
```

Python 3.11+ required. Model constant: `MODEL` at the top of `agent.py`
(default: a current Sonnet-class Claude).

## CLI

```bash
# One session (by question id from questions.json)
python agent.py ask --id q01

# Free-form question (no grader / no learn unless you pass feedback yourself)
python agent.py ask -q "What is Atlas Pro annual seat price?"

# Dreaming pass
python dream.py
# or: python agent.py dream

# Eval (full 25 × conditions — uses the API)
python eval.py
# cheaper: python eval.py --limit 15
```

## Layout

```
mini-agent-memory/
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── agent.py
├── dream.py
├── eval.py
├── corpus/           # 61 short markdown docs
├── questions.json    # 25 questions (10 single / 10 cross / 5 trap)
└── memory/
    ├── transcripts/  # append-only session logs
    ├── notes/
    ├── wiki/
    └── MEMORY.md     # injected index (capped)
```

## Eval results

**Full run (25 questions × conditions), 2026-11-07.** Model:
`claude-sonnet-4-5-20250929`. No `--limit`. Results in `eval_results.json`
(gitignored).

| Condition | Accuracy | Repeat mistakes | Avg tokens / question |
|---|---|---|---|
| No memory (baseline) | 21/25 (84%) | 0 | 4267 |
| Memory on (notes only, no dreaming) | 23/25 (92%) | 0 | 5270 |
| Memory + one dreaming pass | 23/25 (92%) | 0 | 5402 |
| Search alone (cross-doc subset) | 9/10 (90%) | 0 | 4591 |
| Search + wiki (cross-doc subset) | 8/10 (80%) | 0 | 7257 |

Memory sizes at end of each condition:

| Condition | Notes | Wiki pages | MEMORY.md lines / bytes |
|---|---|---|---|
| baseline | 0 | 0 | 3 / 40 |
| memory_notes | 10 | 0 | 3 / 40 |
| memory_dream | 8 | 9 | 64 / 1996 |
| search_wiki (final) | 0 | 11 | 67 / 2196 |

Re-run:

```bash
export ANTHROPIC_API_KEY=...   # or: set -a; source .env; set +a
python eval.py                 # full 25
# cheaper: python eval.py --limit 15
```

### Memory-size comparison (design target)

The post contrasts indiscriminate saving (Sonnet 3.7: 413 files / 225 folders)
with selective memory (Fable 5: 9 files + `global.md`). This repo steers the
agent to save verified facts, tested rules, and traps — not re-lookupable doc
state — and dreaming keeps `MEMORY.md` small via the hard cap.

Live eval stayed well under the cap: after dreaming, `MEMORY.md` was ~64–67
lines / ~2 KB with 8–11 wiki pages (4 contracts + focus/index + trap pages).

## Design choices (when ambiguous → simplest)

- Keyword search only (no embeddings / vector DB).
- Heuristic grader in `eval.py` (number/phrase overlap + trap detectors), not an LLM judge — cheaper and deterministic.
- Dreaming uses the API when a key is present; `--offline` still builds contract wiki pages, focus, gap-fill, and a deterministic `MEMORY.md`.
- Contract wiki pages are seeded verbatim from `corpus/contract-*.md` (lesson from the post’s overwritten shared-table failure).
- Transcripts are committed as empty dirs in spirit; session logs appear after `ask`/`eval` and are fine to keep locally (not secrets). `.env` is gitignored.

## Open questions (from the post — not solved here)

- When should memory be created — in-session vs background dreaming?
- How often to dream as the environment changes?
- Transcript search as foundation, combined with representations.

## MEMORY.md size enforcement

`agent.load_memory_md()` truncates at read time to 200 lines / 25 KB.
`agent.enforce_memory_md_cap()` rewrites the file after dreaming’s promote step.
Both limits are constants in `agent.py` (`MAX_MEMORY_LINES`, `MAX_MEMORY_BYTES`).

## What broke first

From the live full eval (plus earlier offline lessons):

1. **Heuristic grader false negatives** — several “fails” were correct answers
   docked by token rules: q14 answers said “Enterprise plan only” but the
   grader required the exact phrase `enterprise only`; q22/q23 expected
   strings include legacy numbers (`$39`, `30 days`), so omitting the trap
   value drops hit-rate below 0.6 even when the current fact is right.
2. **q17 multi-doc miss (real)** — across conditions the agent cited the
   “second approver” deploy-runbook rule but rarely the canary / status-page
   pieces from the incident note. Cross-doc synthesis, not memory, was the
   weak spot.
3. **Search + wiki hurt more than it helped on this set** — cross-doc accuracy
   fell 90% → 80% vs search alone, while avg tokens jumped 4591 → 7257
   (wiki context bloat without fixing q14/q17).
4. **Repeat trap mistakes stayed at 0** — notes/dreaming did not re-poison
   sessions on this run; memory_notes and memory_dream both lifted overall
   accuracy 84% → 92% vs baseline.
5. **Cap enforcement still matters** — early drafts only documented the
   200-line / 25 KB limit; live dreaming stayed ~2 KB because
   `enforce_memory_md_cap` rewrites after promote.
