# G<nn> — <card title>

<!--
How to write this document
- One document per card. A revision of the card updates this file; it does not add a new one.
- Write in B2 English: short sentences, everyday words, explain each technical term once.
- Length: 700–1,200 words (about 2–3 printed pages). Not a copy of the code or of CODE_FLOW.md.
- Describe the code as it is now, not the card's plan. Never list a method that does not exist.
- Link to CODE_FLOW.md algorithm cards (A1…), QUERIES.md (Q…) and git commands (GIT1…) instead of repeating them.
- Delete these comments when you write the real document.
-->

**Module(s):** `src/skill_genealogy/<module>.py` · **Stage:** <number and name, or "shared helpers"> · **Status:** <reviewed PASS / pending>

## What this task does
3–5 sentences. Which problem does this code solve? Why does the project need it?
Which research question or later stage depends on it?

## Where it fits
A small text diagram: inputs → this module → outputs, and the cards that use the result.

```text
<input table or caller>  ──>  <module>.py  ──>  <output table or return value>
                                                  used by: G.., G..
```

## Files
| File | What is in it |
|---|---|
| `src/skill_genealogy/<module>.py` | one line |
| `tests/skill_genealogy/test_<module>.py` | one line |

## Methods
One table per module. Public methods in the order they appear in the code.
"What it does" is 1–2 sentences: what goes in, what comes out, and any rule worth remembering.

| Method | What it does | Used by |
|---|---|---|
| `public_method()` | … | G.., stage .. |
| Helpers | `_a`, `_b`: what they do together, in one sentence. | this module |

## Settings
Only the settings this task uses or adds (from `settings.py`).

| Setting | Value | Why this value |
|---|---|---|

## Worked example
A tiny real input and its real output, taken from a test, a doctest or a dev run.

## Questions you may be asked
4–6 short questions and answers that help in a presentation or a code review:
why this algorithm, what its limits are, why a threshold has its value, what happens in an edge case.

**Q: …?**
A: …

## Tests
| Test (or group) | What it proves |
|---|---|

```bash
.venv/bin/python -m pytest -q tests/skill_genealogy/test_<module>.py
```
