# crabdeck-x

🦀 CRABDECK X — Sovereign AI Orchestration Operating System. Unified control layer for multi-agent intelligence.

## Assistant processing

Agents follow a fixed contract before any tool runs:

- Verify available context and parameters
- Ask concisely when a required value is missing — never guess
- Respond with Markdown bullets or code blocks
- On failure, report the error code, bottleneck, and a fallback

The contract lives in:

| Path | Role |
| --- | --- |
| `AGENTS.md` | Human/agent operating rules |
| `orchestrator/processing/` | Stdlib engine that enforces the rules |
| `workflows/assistant-processing.yaml` | Tool and rule declarations |
| `tests/test_processing.py` | Guideline regression tests |

```bash
python3 -m unittest tests.test_processing -v
python3 -m orchestrator.processing --list-guidelines
python3 -m orchestrator.processing --tool dispatch_prompt \
  --param prompt=hello \
  --context gateway_url=ws://localhost:8765
```

A call that omits `prompt` or `gateway_url` returns `needs_input` and does not dispatch.
