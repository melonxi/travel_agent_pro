# Release notes: v1.0-portfolio

**Tag intent:** first portfolio-ready snapshot — clone, CI, and evidence pack.

## Highlights

- **Dependency lock:** `backend/uv.lock` + `uv sync --all-extras --frozen`
- **CI (A0):** `.github/workflows/ci.yml`
  - Backend core Runtime tests (steering, orchestrator, candidate store, plan writers, eval, …)
  - Golden case load check (40)
  - Frontend `npm ci && npm run build`
- **LICENSE:** MIT
- **README:** Reliable Travel Agent positioning, honest portfolio disclaimer, corrected API paths and test counts
- **Evidence pack:** `docs/evidence/`
  - mock 12-case pass@3 baseline
  - fault-injection → test map (F1–F6)
  - interview talk track + hostile questions

## Verify locally (no API keys)

```bash
./scripts/run-a0-core.sh
# or
cd backend && uv sync --all-extras --frozen
export OTEL_SDK_DISABLED=true
uv run pytest -q -m "not integration"
```

## Known limits (honest)

- No production users
- Mock pass@3 proves the **eval harness**, not live model quality
- Demo recording under `scripts/demo` is **scripted UI walkthrough**
- Full suite may still mark some paths as future hardening (see F7 partial)

## GitHub About (manual)

| Field | Suggested value |
|-------|-----------------|
| Description | A reliable long-horizon planning agent with bounded autonomy, parallel workers, partial replanning, mid-run steering, trace-based evaluation, and failure recovery. |
| Topics | `agent` `llm` `fastapi` `react` `evaluation` `observability` `planning` `sse` |
| Homepage | (optional) docs/evidence/portfolio-proof.md raw or GitHub Pages later |

## Create the release (after CI green)

```bash
git tag -a v1.0-portfolio -m "Portfolio-ready: locked deps, CI A0, evidence pack"
git push origin v1.0-portfolio
gh release create v1.0-portfolio --title "v1.0-portfolio" --notes-file docs/evidence/RELEASE_NOTES_v1.0-portfolio.md
```
