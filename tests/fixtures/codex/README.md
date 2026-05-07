# Codex provider test fixtures

Real Codex CLI rollouts captured from a live `codex exec` run on a puppet,
then sanitized: workspace path scrubbed, `base_instructions.text` slimmed
to a placeholder so fixtures stay stable across Codex CLI updates. Session
UUIDs and other content are kept verbatim.

Captured against Codex CLI v0.128.0 (gpt-5.5 default model, plan_type=plus,
auth via ChatGPT device-code login).

## Scenarios

- `01_pure_conversation/rollout.jsonl` -- session_meta + role=user/assistant message round trip; no tools
- `02_function_call/rollout.jsonl` -- function_call (exec_command shell) + function_call_output round trip
- `04_reasoning/rollout.jsonl` -- response_item.reasoning record with summary[].text + encrypted_content
- `05_compacted/rollout.jsonl` -- synthetic: compacted record between two turns; recall mode must slice after it
- `06_resume/rollout.jsonl` -- two turns in the same rollout via codex exec resume --last; resume appends in place
- `08_plan_update/rollout.jsonl` -- function_call name=update_plan, with structured plan in arguments; multi-step task
- `09_web_search/rollout.jsonl` -- response_item/web_search_call records (NEW type); multiple queries + reasoning interleave
- `10_mcp/rollout.jsonl` -- MCP tool call surfaces as function_call with namespace=mcp__<server>__ field

## Re-capture

Capture script: `.copilot/codex_scenario_run.py` (invokes rig + codex exec on the puppet).
Finalize / sanitize / synthesize script: `.copilot/codex_fixtures_finalize.py`.

## Compacted fixture

Codex CLI v0.128.0 has no manual `/compact` flag and the 258k-token
window cannot be filled organically in a short capture. The
`05_compacted` fixture is synthesized from `06_resume` by injecting a
`compacted` record at the turn boundary, with `replacement_history`
set to the original turn-1 response_items. This is enough to test
recovery-mode slicing (`since_last_compact=True`).
