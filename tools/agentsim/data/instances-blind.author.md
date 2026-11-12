# Blind author pass — record

- Model: Claude Sonnet (the `sonnet` subagent model), a different model from the one that tuned the
  templates and wrote the harness.
- Inputs it could read: this folder only — `facts.json`, `FORMAT.md`, `validate.py`. It was told not
  to open, list or search anything else and to run nothing but `uv run python .tmp/blind/validate.py`.
- Output: `instances.json`, sha256 `99c8fd8227156c8a8dc76fe0adf66283e1e9c249593da63595d48ad74705b7cb`.
- Transcript: sha256 `e3b7d03d482ece640ce587b8c8cc6aa1ef96acd31b67ec184367897194b29e37` (kept by the
  orchestrator, outside the repository).

## Prompt (verbatim summary of the instructions given)

Write 10 instances per class present in facts.json (1, 2, 3, 4, 6, 7, 10, 11, 12, 13), 100 in all;
every fact keyed at least once and at most three times; wording split about evenly between
identifier, paraphrase and symptom; tasks phrased as a working engineer would, never containing the
answer or a stale value; identifier names the component, key or error string; paraphrase avoids the
exact name; symptom describes an observed problem; class 12 and 10 ask for the current value without
hinting which document holds it; class 11 asks what the system currently does or should use; class 13
identifier wording quotes the key or error string exactly. `required` regexes match every keyed
current value and never a stale one; `stale` regexes match the stale values and not the current one;
`keys` are the fact ids; ids `c<class>-<slug>-<n>`; `repo`/`files` only when the task names them.
Validate until the validator prints `OK 100 instances`.

## Round 2 (after the loader gained two rules, ARCH-804)

The same author rewrote only the 11 instances listed in `REJECTED.md` and the task text of
`c11-collector-4` (it named the file holding the answer), under the same isolation. The orchestrator
checked that every other instance is unchanged and in the same order. Output sha256 `737cc46bbe32f69cb3d19fe95804f005224716bb87900450ca99cc036edfb1ca`.

## Round 3 (after the loader gained the stale-leak rule, ARCH-816)

The same author rewrote only the `stale` regexes of the 7 instances listed in `REJECTED.md`, under the
same isolation; the validator now also checks the generated stores. The orchestrator checked that only
those 7 instances changed, and only in `stale`. Output sha256 `cc564328944c0a298ea62e9b36b8dbef06d498f2d8f034afb72a84e54f09deff`.
