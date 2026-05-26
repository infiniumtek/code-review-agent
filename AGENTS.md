# AGENTS.md

> Project contract for Codex and any other AI coding assistant.

**The canonical contract is [`CLAUDE.md`](./CLAUDE.md).** This project keeps a single source of truth to avoid drift — read `CLAUDE.md` for §1–§14 (project, stack, layout, setup & hard rules, env vars, patterns, Docker, make targets, `langgraph.json`, conventions, testing, workflow, out-of-scope, references) and [`PLAN.md`](./PLAN.md) for the architecture, locked decisions, and build phases.

Terminology note (for any tool reading this): review knowledge ships as **portable Agent Skills** in the open **SKILL.md** format (originated by Claude). The agent loads them through its own **provider-agnostic** loader — default provider **OpenAI `gpt-5-mini`**, switchable to Anthropic/Google — and does **not** use Claude's native code-execution Skills runtime. Skills are prompt-only.

> Do not maintain a separate full copy here. If your tooling needs tool-specific notes, add them below this line rather than duplicating `CLAUDE.md` (a prior mechanical "Claude→Codex" copy corrupted factual content — e.g. doc URLs — and is what this pointer prevents).
