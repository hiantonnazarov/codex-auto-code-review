# Codex Auto Code Review

**Automatic, independent, local code review after significant edits in Codex.** Windows-native, Linux and macOS. Pure Python 3.11+ standard library and Codex CLI; no Claude account, Docker, WSL, external API keys or GitHub PRs required.

> **Important:** Some Codex Desktop/app-server versions do not dispatch `Stop` hooks reliably ([upstream issue #38213](https://github.com/openai/codex/issues/38213)). No hook-based project can solve missing host events. Use the included manual fallback when necessary; do not treat unverified automatic review as a safety guarantee.

## What it does

- **Any normal Codex prompt:** `UserPromptSubmit` records Git tree at start; `Stop` checks the *net changes in that turn*.
- **Significant only:** default: ≥30 changed lines, ≥2 code files, or any critical-path code edit; optionally review every code change.
- **Independent LLM invocation:** launches fresh, read-only `codex exec` and validates structured JSON findings.
- **Same-thread feedback:** P0/P1/P2 high-confidence findings block Stop and ask the coding agent to fix them; review repeats against the *entire original task diff* after edits.
- **Finite loops:** maximum 3 review rounds by default, with explicit unresolved warnings; identical tree is never reviewed twice.
- **Safe Git baselines:** tracked, staged and untracked (nonignored) changes; never mutates your real Git index or commits.
- **Large diffs:** chunked instead of silently truncated; cap violations become visible review failures.
- **Transparent state:** per-session JSON reports, status, doctor, global/local TOML configuration, install/uninstall without overwriting unrelated hooks.

## Installation

Prerequisites: Git, Python 3.11+, authenticated Codex CLI (`codex login status`). On Windows PowerShell:

```powershell
py -m pip install --user git+https://github.com/hiantonnazarov/codex-auto-code-review.git
py -m codex_auto_review.cli install
py -m codex_auto_review.cli doctor
```

Alternative if `codex-auto-review` is on PATH: use `codex-auto-review install` and `codex-auto-review doctor`.

**Restart Codex**, open `/hooks`, **enable/trust the two hooks** (`UserPromptSubmit` and `Stop`). Make a code change through an ordinary prompt and inspect the resulting JSON report. The installer stores hooks in the user-level `~/.codex/hooks.json`, keeping unrelated entries intact. Hooks apply across your Git repositories.

### Manual fallback / diagnostics

```powershell
py -m codex_auto_review.cli review --cwd .
py -m codex_auto_review.cli doctor
py -m codex_auto_review.cli status --session YOUR_CODEX_SESSION_ID
py -m codex_auto_review.cli uninstall
```

Manual `review` checks all uncommitted and untracked changes versus HEAD, and exits nonzero for blocking findings. It is not tied to a single prompt. Your Git working tree must be available to the CLI.

## Configure

Copy [`examples/config.toml`](examples/config.toml) to `~/.codex-auto-code-review/config.toml` globally, or to `.codex-auto-review.toml` in a specific repository. Local values override global values.

```toml
[review]
mode = "significant"    # or "all"
min_changed_lines = 30
min_changed_files = 2
reviewer_model = ""     # default Codex CLI model; use a supported ID to override
reasoning_effort = "high"
max_review_rounds = 3
confidence_threshold = 0.65
fail_open = true
```

Pure docs, generated files and ignored files are excluded by default. Fine-tune glob patterns in the example. Reports are stored **outside the source repo** by default under `~/.codex-auto-code-review/state/reports/`. The reviewer runs in a read-only sandbox; it is never authorized to repair files. Codex receives feedback to decide repairs.

## Verification

```powershell
py -m pip install pytest
py -m pytest -q
```

See [architecture](docs/ARCHITECTURE.md) and [troubleshooting](docs/TROUBLESHOOTING.md). Actual end-to-end invocation requires Codex installed, authenticated and a host version that dispatches hooks; CI tests use a deterministic fake reviewer to avoid model costs.

## License and attribution

MIT license. Original Python implementation, inspired by the concepts of [Just Every Auto Review](https://github.com/just-every/plugin-auto-review) and [Sendbird cc-plugin-codex](https://github.com/sendbird/cc-plugin-codex). No third-party code copied.
