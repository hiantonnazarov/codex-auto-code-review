# Architecture and operational contract

## Lifecycle

1. `UserPromptSubmit`: create an **alternate-index Git tree** reflecting the working tree, including untracked nonignored files. Save it as the immutable baseline for this *session + turn*. The real index, HEAD and working files are not changed.
2. `Stop`: create another tree. Compare both with `git diff --numstat -z`. Filter ignored and non-code paths. Select a review if line/file thresholds are reached or a critical path was changed.
3. Acquire a per-session lock. Check whether the exact tree was already reviewed. Skip duplicates.
4. Split changed patches into bounded, non-truncated chunks (or return an explicit error). Run a fresh `codex exec --sandbox read-only` with `--output-schema` per chunk, isolated from the author and with a child-hook recursion guard. Validate every finding against changed paths.
5. Write an audit JSON record for the precise reviewed tree. If P0-P2 findings exceed confidence threshold, emit `decision=block` so Codex receives concrete feedback in the same turn.
6. When Codex changes code after feedback, `Stop` compares the **entire task diff to original baseline** again. Allow after clean review or on reaching the configured finite retry ceiling with an explicit unresolved warning.

## Scope, guarantees and limitations

- Distinguishes per-turn changes from unrelated pre-existing edits and captures untracked files, subject to Git ignore rules.
- No commit, checkout, stash, stage or destructive write to the user's repository; temporary Git index only. Git tree objects are written to the object store and may later be pruned by Git maintenance.
- No review for pure documentation edits by default; tune `critical_globs` and `mode` for broader enforcement.
- Model is an independent Codex invocation, not the same agent's self-review; it is still the **same model family** by default, so correlated errors are possible.
- Failures are **not passed**. With `fail_open=true`, the hook allows the Codex turn to end and surfaces an explicit error. With `false`, it blocks, which may also block work indefinitely on outage (manual recovery required).
- Fingerprinting detects content changes, including changes to previously staged and untracked files. Metadata-only mode changes can change tree SHA; reviewer receives a metadata diff.
- Very large patches beyond configured limits fail visibly instead of being falsely marked clean. Increase chunk limits or review in smaller tasks.
- Reviewer can inspect source code, which is untrusted. Read-only sandbox, scoped instructions and child-hook guard reduce but cannot eliminate prompt-injection and exfiltration risk. Do not run reviewer with dangerous sandbox bypass flags.
- `stop_hook_active` is not used to suppress review after fix: finite rounds and content fingerprints enforce termination instead.

## State and recovery

State is stored by SHA-256 of the absolute Git path + Codex `session_id` under `~/.codex-auto-code-review/state`. Reports are JSON files under its `reports` directory. Only the latest user turn for the session is active. Restarting within the same session can resume state when the host preserves session ID. Per-session locks protect against duplicate reviews; abandoned locks older than 30 minutes can be reclaimed.

## Host compatibility

`Stop` hook dispatch varies among Codex Desktop/app-server versions. The project **cannot guarantee** that the host fires the hook. Run `codex-auto-review doctor`, do a test edit and use `codex-auto-review review` manually when it doesn't run. CLI manual review compares all uncommitted and untracked code changes versus HEAD; it cannot reproduce a Codex turn boundary without a saved hook baseline.

## Credits and implementation approach

Written as an original Python implementation informed by the hook lifecycle concept in [Just Every Auto Review](https://github.com/just-every/plugin-auto-review), the review-gate and working-tree-fingerprint approach in [Sendbird cc-plugin-codex](https://github.com/sendbird/cc-plugin-codex), and OpenAI's official Codex CLI documentation. This repo does not vendor either project's source code.
