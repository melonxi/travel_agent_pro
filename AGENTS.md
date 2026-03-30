# Repository Guidelines

## Project Structure & Module Organization

This repository is split into a Python backend and a React frontend. `backend/` contains the FastAPI app (`main.py`), the agent loop (`agent/`), LLM providers (`llm/`), state and memory models (`state/`, `memory/`), phase routing (`phase/`), domain tools (`tools/`), and pytest suites in `backend/tests/`. `frontend/src/` contains the app shell, chat UI, SSE hook, and travel visualizations. `docs/` stores architecture and learning notes. Playwright E2E files live at the repo root.

## Build, Test, and Development Commands

- `cd backend && source .venv/bin/activate && uvicorn main:app --reload --port 8000`: run the API locally.
- `cd backend && pytest`: run backend unit and integration tests.
- `cd backend && pytest --cov`: run tests with coverage output.
- `cd frontend && npm run dev`: start the Vite dev server with `/api` proxied to `localhost:8000`.
- `cd frontend && npm run build`: type-check and build the frontend.
- `npx playwright test e2e-test.spec.ts`: run the browser E2E flow. Check `playwright.config.ts` for the expected frontend port before running.

## Coding Style & Naming Conventions

Use 4 spaces in Python and keep type hints on public functions and models. Follow the existing backend pattern: snake_case modules, small focused packages, and async tool functions decorated with `@tool`. In TypeScript, keep components in PascalCase (`ChatPanel.tsx`), hooks in camelCase (`useSSE.ts`), and shared types in `frontend/src/types/`. Match the existing concise style; no large framework abstractions.

## Testing Guidelines

Backend tests use `pytest` and `pytest-asyncio`; add new tests under `backend/tests/` with filenames like `test_<feature>.py`. Frontend end-to-end coverage uses Playwright through `e2e-test.spec.ts`. There is no enforced coverage threshold in config, but new behavior should include at least one focused automated test and, for UI changes, an updated manual or E2E verification path.

## Commit & Pull Request Guidelines

Recent history uses conventional prefixes such as `feat:`, `test:`, `docs:`, and `chore:`. Keep commit messages imperative and scoped to one change. PRs should include a short summary, affected areas (`backend`, `frontend`, docs, tests), exact verification commands, and screenshots for visible UI changes.

## Security & Configuration Tips

Keep secrets in `backend/.env`; the backend loads them via `python-dotenv` at startup. Do not hardcode keys in source files. `config.yaml` is optional and can override defaults, but environment variables still matter for provider setup and external APIs. When checking API keys, verify them from the backend runtime context, not only from the current shell; `backend/config.py` loads `backend/.env`, so a shell-level `unset` does not necessarily mean the project is misconfigured.
