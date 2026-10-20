# Troubleshooting

| Symptom | Check | Action |
|---|---|---|
| Review never starts in Codex Desktop | `codex-auto-review doctor`; `/hooks` status; `~/.codex/hooks.json` | Enable/trust hooks and restart Codex. Check upstream [app-server Stop hook issue #38213](https://github.com/openai/codex/issues/38213). Run `codex-auto-review review` as fallback. |
| Reviewer CLI missing / not logged in | `codex --version`; `codex login status` | Install Codex CLI and authenticate in the same OS user context. |
| No review for small changes | `.codex-auto-review.toml`; defaults = 30 changed lines or 2 files | Set `mode = "all"` or lower thresholds. |
| No review for change before prompt | Baseline is captured **at prompt submission** | Use `codex-auto-review review` for pre-existing changes. |
| JSON schema / model incompatibility | CLI logs and session status | Upgrade Codex; configure a model supporting `--output-schema`. |
| Error mentions too many chunks | `max_chunks`, `max_diff_chars_per_chunk` | Raise caps or split the edit into narrower tasks. Errors are not green reviews. |
| Hook loops or repeatedly reports same finding | `status --session <id>` / report | Each fingerprint is reviewed once; after feedback with no code change Stop allows exit with a warning. |
| Reviewer subprocess unexpectedly runs another hook | `CODEX_AUTO_REVIEW_CHILD` env | Child sessions skip hooks; ensure the environment is preserved. |
| New hooks override older hooks | Inspect `~/.codex/hooks.json` | Installer only replaces this tool's own entries and preserves other hook definitions. |
| Need hard guarantee per task | Host issue / Stop compatibility | Use explicit orchestrator or enforce `codex-auto-review review` from CI; a hook cannot guarantee execution if the host never dispatches it. |
