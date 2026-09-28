# CrabDeck X — Assistant Processing Guidelines

These rules apply to every agent, routine, and workflow that runs inside CrabDeck X. They are encoded in `orchestrator/processing` and `workflows/assistant-processing.yaml`.

## Operating contract

- **Verify before execute.** Always verify available context and parameters before executing a tool.
- **Ask, do not guess.** If a required parameter for a tool is missing, ask the user concisely instead of guessing.
- **Structured output.** Output clean, structured responses using Markdown bullets or code blocks. Avoid fluff.
- **Error fallback.** If an operation fails, analyze the error code, explain the bottleneck briefly, and propose a fallback option.

## Tool dispatch

1. Resolve the tool by exact registered name. Unknown names return `NOT_FOUND`.
2. Check required context, then required parameters. Blank strings count as missing.
3. If anything required is missing, return `needs_input` and stop. Do not invent defaults.
4. Run the handler only after verification succeeds.
5. On exception, classify the error (`TIMEOUT`, `CONNECTION`, `AUTH`, `NOT_FOUND`, `VALIDATION`, `UNKNOWN`) and return code + bottleneck + fallback.

## Commands

```bash
python3 -m unittest tests.test_processing -v
python3 -m orchestrator.processing --list-guidelines
python3 -m orchestrator.processing --tool dispatch_prompt --param prompt=hello --context gateway_url=ws://localhost:8765
```
