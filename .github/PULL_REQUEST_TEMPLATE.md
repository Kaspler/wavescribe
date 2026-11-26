<!-- What does this change do, and why? For large work, link the issue where it was discussed. -->


## Checklist

<!-- From CONTRIBUTING.md. Delete the lines that don't apply. -->

- [ ] `uv run pytest -m "not models"` passes
- [ ] `uv run ruff check .` and `uv run ruff format --check .` are clean
- [ ] If `frontend/` changed, the page is rebuilt (`npm --prefix frontend run build`) and `src/wavescribe/web/dist/` is committed with it
- [ ] If a dependency or model is new or re-pinned, it has a `docs/LICENSING.md` entry and its licence is named above
- [ ] If an interface, a decision or documented behaviour changed, the matching doc is updated in the same commit
- [ ] No secrets, personal data, model weights or recorded broadcast audio are added
