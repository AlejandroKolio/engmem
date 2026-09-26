## What this changes

<!-- The behaviour, in a sentence or two. Why, not just what. -->

## Checklist

- [ ] A failing test was written first and observed to fail (ENGMEM-SPEC.md §10, principle I)
- [ ] `uv run pytest -q` is green
- [ ] No existing assertion was weakened to make something pass
- [ ] Comments explain *why*, in a line or two — long rationale went to the spec instead
- [ ] Examples in tests and docs are invented, with no real ticket, repository, class, or
      business term from any private codebase
- [ ] Runtime dependencies unchanged (still `pyyaml` and `markdown-it-py`), or the reason is stated here
