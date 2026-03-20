# Claude Commander — Open Issues

### ~~Pre-warm sessions with ClaudeSDKClient~~ ✅ Done
- Implemented: persistent `ClaudeSDKClient` per project stored in `_clients` dict
- `warmup_projects()` connects all registered projects at bot startup (background task via `post_init`)
- `_get_client()` uses double-checked locking to prevent duplicate connect races
- Auto-reconnect on error handled in `run_prompt()` retry block

### Streaming responses
- Responses arrive all at once after Claude finishes
- `ClaudeSDKClient.receive_messages()` yields messages as they arrive
- Could stream partial results to Telegram (edit message as tokens come in)
- Pairs well with the pre-warm refactor above

### Multi-user support
- Currently single-admin only (ADMIN_CHAT_ID)
- Would need per-user project access, permission scoping, separate sessions
