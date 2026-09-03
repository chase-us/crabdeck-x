"""
Hermes Agent v2.2 — CrabDeck LLM Messenger
Connects to CrabDeck Gateway, receives PROMPT events, calls Ollama, returns HERMES_RESPONSE.

Run:
    pip install -r requirements.txt
    python hermes_agent.py

Env vars:
    GATEWAY_URL     default ws://localhost:8765
    GATEWAY_TOKEN   shared secret — must match the gateway's GATEWAY_TOKEN. Required
                    once the gateway is deployed anywhere other than localhost.
    OLLAMA_URL      default http://localhost:11434
    DEFAULT_MODEL   default llama3
"""

import asyncio, json, os, time, sys
import requests
import websockets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from offload import run_blocking
from vault_client import emit_heartbeat, emit_memory, heartbeat_payload

GATEWAY_URL      = os.environ.get("GATEWAY_URL", "ws://localhost:8765")
GATEWAY_TOKEN    = os.environ.get("GATEWAY_TOKEN")  # None in local/dev mode
OLLAMA_URL       = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL    = os.environ.get("DEFAULT_MODEL", "llama3")
HEARTBEAT_EVERY  = 10   # seconds
RECONNECT_DELAY  = 5

# ── Ollama helpers ─────────────────────────────────────────────────────────────

def ollama_available():
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        return r.status_code == 200
    except Exception:
        return False

def ollama_models():
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        data = r.json()
        return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []

def ollama_generate(prompt: str, model: str = DEFAULT_MODEL) -> str:
    payload = {"model": model, "prompt": prompt, "stream": False}
    try:
        r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=120)
        r.raise_for_status()
        data = r.json()
        return data.get("response", "(no response from Ollama)")
    except Exception as e:
        return f"[Hermes error] Ollama call failed: {e}"

# ── Gateway loop ───────────────────────────────────────────────────────────────

async def run():
    print("[Hermes] Starting — connecting to CrabDeck Gateway…")
    if not GATEWAY_TOKEN:
        print("[Hermes] ⚠ GATEWAY_TOKEN not set — only OK if the gateway is also running unauthenticated on localhost.")

    while True:
        try:
            async with websockets.connect(GATEWAY_URL) as ws:
                hello = {"type": "HELLO", "client": "hermes", "version": "2.2"}
                if GATEWAY_TOKEN:
                    hello["token"] = GATEWAY_TOKEN
                await ws.send(json.dumps(hello))

                ack_raw = await asyncio.wait_for(ws.recv(), timeout=5)
                ack = json.loads(ack_raw)
                if ack.get("type") == "ERROR":
                    print(f"[Hermes] ❌ Gateway rejected handshake: {ack.get('message')}")
                    await asyncio.sleep(RECONNECT_DELAY)
                    continue

                print("[Hermes] Connected to Gateway ✅")

                models = await run_blocking(ollama_models)
                if models:
                    print(f"[Hermes] Ollama online — models: {', '.join(models)}")
                else:
                    print("[Hermes] ⚠ Ollama offline — start with: ollama serve")

                heartbeat_task = asyncio.create_task(heartbeat(ws))

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue

                    mtype = msg.get("type")

                    if mtype == "PROMPT":
                        await dispatch_prompt(ws, msg)
                    elif mtype == "TOOL_REQUEST":
                        await dispatch_tool_request(ws, msg)
                    elif mtype == "ERROR":
                        print(f"[Hermes] Gateway error: {msg.get('message')}")

                heartbeat_task.cancel()

        except (websockets.ConnectionClosed, ConnectionRefusedError, OSError) as e:
            print(f"[Hermes] Gateway disconnected: {e} — retrying in {RECONNECT_DELAY}s")
            await asyncio.sleep(RECONNECT_DELAY)
        except asyncio.TimeoutError:
            print(f"[Hermes] Gateway did not respond to handshake — retrying in {RECONNECT_DELAY}s")
            await asyncio.sleep(RECONNECT_DELAY)
        except Exception as e:
            print(f"[Hermes] Unexpected error: {e} — retrying in {RECONNECT_DELAY}s")
            await asyncio.sleep(RECONNECT_DELAY)


async def dispatch_prompt(ws, msg, generate=ollama_generate, remember=emit_memory):
    """Handle a PROMPT without blocking the event loop (gateway watchdog: 20s)."""
    if generate is None or not callable(generate):
        raise TypeError("dispatch_prompt requires a callable generate")
    if remember is not None and not callable(remember):
        raise TypeError("remember must be callable or None")

    raw = msg.get("payload", {}) if isinstance(msg, dict) else {}
    prompt  = raw.get("prompt", "") if isinstance(raw, dict) else str(raw)
    model   = raw.get("model", DEFAULT_MODEL) if isinstance(raw, dict) else DEFAULT_MODEL
    if not isinstance(prompt, str):
        prompt = str(prompt)
    if not isinstance(model, str) or not model.strip():
        model = DEFAULT_MODEL

    print(f"[Hermes] PROMPT ({model}) → {prompt[:80]}…")
    reply = await run_blocking(generate, prompt, model)
    print(f"[Hermes] RESPONSE → {str(reply)[:80]}…")

    await ws.send(json.dumps({
        "type":    "HERMES_RESPONSE",
        "agent":   "hermes",
        "payload": reply,
    }))
    if remember is not None:
        excerpt = f"{prompt[:1200]}\n---\n{str(reply)[:4000]}"
        await run_blocking(remember, "hermes", "prompt_result", excerpt, {"model": model})
    return reply


def _tool_request_payload(msg):
    """Normalize TOOL_REQUEST payload. Non-dicts become {\"raw\": ...}."""
    raw = msg.get("payload", {}) if isinstance(msg, dict) else {}
    if not isinstance(raw, dict):
        return {"tool": "unknown", "raw": raw}
    tool = raw.get("tool", "unknown")
    if not isinstance(tool, str) or not tool.strip():
        tool = "unknown"
    return {**raw, "tool": tool}


async def dispatch_tool_request(ws, msg, generate=ollama_generate):
    """Handle a TOOL_REQUEST without blocking the event loop."""
    if generate is None or not callable(generate):
        raise TypeError("dispatch_tool_request requires a callable generate")

    payload = _tool_request_payload(msg)
    tool_name = payload["tool"]
    print(f"[Hermes] TOOL_REQUEST — {tool_name}")
    try:
        encoded = json.dumps(payload)
    except TypeError:
        encoded = json.dumps({"tool": tool_name, "raw": str(payload)})
    result = await run_blocking(generate, f"Tool call: {encoded}")
    await ws.send(json.dumps({
        "type":    "TOOL_RESULT",
        "agent":   "hermes",
        "payload": {"tool": tool_name, "result": result},
    }))
    return result


async def heartbeat(ws, emit=emit_heartbeat, every=HEARTBEAT_EVERY):
    if emit is not None and not callable(emit):
        raise TypeError("emit must be callable or None")
    if not isinstance(every, (int, float)) or every < 0:
        raise ValueError("every must be a non-negative number")
    while True:
        await asyncio.sleep(every)
        try:
            ts = time.time()
            payload = heartbeat_payload("hermes", ts)
            await ws.send(json.dumps(payload))
            if emit is not None:
                await run_blocking(emit, payload["agent"], payload["ts"], payload["bhive_slot"], "agent")
        except Exception:
            break


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n[Hermes] Stopped.")
