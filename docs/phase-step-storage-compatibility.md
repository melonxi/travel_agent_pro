# Phase 2 Step Storage

The public phase model is now `1/2/3/4`.

Phase 2 has four internal substeps: `brief`, `candidate`, `skeleton`, and `lock`.

Runtime state, serialized plan JSON, and SQLite message-history metadata all use the same field name: `phase2_step`.

Rule:

- Do not introduce new user-facing docs, UI labels, prompt text, or configs that describe the framework-planning substep as Phase 3.
