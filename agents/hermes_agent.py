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

                models = ollama_models()
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
                        payload = msg.get("payload", {})
                        prompt  = payload.get("prompt", "") if isinstance(payload, dict) else str(payload)
                        model   = payload.get("model", DEFAULT_MODEL) if isinstance(payload, dict) else DEFAULT_MODEL

                        print(f"[Hermes] PROMPT ({model}) → {prompt[:80]}…")
                        reply = ollama_generate(prompt, model)
                        print(f"[Hermes] RESPONSE → {reply[:80]}…")

                        await ws.send(json.dumps({
                            "type":    "HERMES_RESPONSE",
                            "agent":   "hermes",
                            "payload": reply,
                        }))

                    elif mtype == "TOOL_REQUEST":
                        tool_name = msg.get("payload", {}).get("tool", "unknown")
                        print(f"[Hermes] TOOL_REQUEST — {tool_name}")
                        result = ollama_generate(f"Tool call: {json.dumps(msg.get('payload', {}))}")
                        await ws.send(json.dumps({
                            "type":    "TOOL_RESULT",
                            "agent":   "hermes",
                            "payload": {"tool": tool_name, "result": result},
                        }))

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


async def heartbeat(ws):
    while True:
        await asyncio.sleep(HEARTBEAT_EVERY)
        try:
            await ws.send(json.dumps({"type": "HEARTBEAT", "agent": "hermes", "ts": time.time()}))
        except Exception:
            break


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n[Hermes] Stopped.")
