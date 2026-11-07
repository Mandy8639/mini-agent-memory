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
6. **Eval** — same 25 questions under four conditions, then **Round 2** paraphrases of the 5 trap questions; reports accuracy, repeat trap mistakes, loop-closed recoveries, and avg input tokens.

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

# Eval (full 25 × conditions + Round 2 traps — uses the API)
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
├── questions.json    # 25 Round-1 questions + 5 Round-2 trap paraphrases
└── memory/
    ├── transcripts/  # append-only session logs
    ├── notes/
    ├── wiki/
    └── MEMORY.md     # injected index (capped)
```

## Eval results

**Full run (25 questions × conditions + Round 2 traps), 2026-11-07.** Model:
`claude-sonnet-4-5-20250929`. No `--limit`. Results in `eval_results.json`
(gitignored).

Grader Phase 2 fix: normalize case/punctuation/whitespace, per-question
aliases, and two-part trap scoring (accuracy = current fact only; trap
awareness reported separately). Round 2 asks the 5 traps again in paraphrase
so “repeat mistakes” / “loop closed” are real learning-loop metrics.

Previously reported 21/25 (84%) / 23/25 (92%) / 23/25 (92%) / 9/10 (90%) /
8/10 (80%), corrected after the grader fix (and re-run) to the table below.

| Condition | Accuracy | Repeat mistakes | Loop closed | Avg tokens / question |
|---|---|---|---|---|
| No memory (baseline) | 24/25 (96%) | 0 | 0 | 4514 |
| Memory on (notes only, no dreaming) | 24/25 (96%) | 0 | 0 | 5254 |
| Memory + one dreaming pass | 25/25 (100%) | 0 | 0 | 6236 |
| Search alone (cross-doc subset) | 9/10 (90%) | — | — | 4829 |
| Search + wiki (cross-doc subset) | 9/10 (90%) | — | — | 7068 |

Memory sizes at end of each condition:

| Condition | Notes | Wiki pages | MEMORY.md lines / bytes |
|---|---|---|---|
| baseline | 0 | 0 | 3 / 40 |
| memory_notes | 6 | 0 | 3 / 40 |
| memory_dream | 10 | 10 | 105 / 3951 |
| search_wiki (final) | 0 | 12 | 48 / 2167 |

Re-run:

```bash
export ANTHROPIC_API_KEY=...   # or: set -a; source .env; set +a
python eval.py                 # full 25 + Round 2
# cheaper: python eval.py --limit 15
```

### Memory-size comparison (design target)

The post contrasts indiscriminate saving (Sonnet 3.7: 413 files / 225 folders)
with selective memory (Fable 5: 9 files + `global.md`). This repo steers the
agent to save verified facts, tested rules, and traps — not re-lookupable doc
state — and dreaming keeps `MEMORY.md` small via the hard cap.

Live eval stayed under the cap: after dreaming, `MEMORY.md` was ~48–105
lines / ~2–4 KB with 10–12 wiki pages (4 contracts + focus/index + trap pages).

## Design choices (when ambiguous → simplest)

- Keyword search only (no embeddings / vector DB).
- Heuristic grader in `eval.py` (normalization + aliases + two-part trap checks), not an LLM judge — cheaper and deterministic.
- Dreaming uses the API when a key is present; `--offline` still builds contract wiki pages, focus, gap-fill, and a deterministic `MEMORY.md`.
- Contract wiki pages are seeded verbatim from `corpus/contract-*.md` (lesson from the post’s overwritten shared-table failure).
- Transcripts are committed as empty dirs in spirit; session logs appear after `ask`/`eval` and are fine to keep locally (not secrets). `.env` is gitignored.
- Round 2 trap paraphrases live in `questions.json` (`round: 2`, `round2_of`) and run without resetting memory so notes/dreaming can close the loop.

## Open questions (from the post — not solved here)

- When should memory be created — in-session vs background dreaming?
- How often to dream as the environment changes?
- Transcript search as foundation, combined with representations.

## MEMORY.md size enforcement

`agent.load_memory_md()` truncates at read time to 200 lines / 25 KB.
`agent.enforce_memory_md_cap()` rewrites the file after dreaming’s promote step.
Both limits are constants in `agent.py` (`MAX_MEMORY_LINES`, `MAX_MEMORY_BYTES`).

## What broke first

From the Phase 2 re-run (grader fixed + Round 2 traps):

1. **q17 multi-doc miss (still real without dreaming)** — baseline, notes-only,
   search-alone, and search+wiki all cited the “second approver” deploy-runbook
   rule but missed canary / status-page pieces from the incident note. **Memory
   + one dreaming pass was the only condition to get q17 right** (25/25).
2. **Search + wiki no longer loses accuracy on this set** — both cross-doc
   conditions scored 9/10 (90%), but wiki still bloated tokens 4829 → 7068
   (~46%) without fixing q17. Previously reported search+wiki 8/10 (80%) was
   mostly the q14 grader false negative.
3. **Round 2 traps: no repeat mistakes, but also nothing to “close”** — all 5
   traps were already correct in Round 1 under every condition, so
   repeat-mistakes = 0 and loop-closed = 0. The metric is now real (R1 wrong ∧
   R2 wrong the same way); on this run the agent simply did not fall for the
   traps the first time.
4. **Grader false negatives (fixed)** — Phase 1 marked q14/q22/q23 wrong when
   answers were right (“Enterprise plan only” vs required `enterprise only`;
   legacy `$39` / `30 days` required in expected strings). Those inflate the
   old 84%/92%/80% figures; corrected numbers are in the table above.
5. **Cap enforcement still matters** — live dreaming stayed ~2–4 KB because
   `enforce_memory_md_cap` rewrites after promote.
