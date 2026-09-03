"""
Scribe Agent v2.3 — CrabDeck swarm librarian.

The mesh's retrieval specialist. Scribe owns the `retrieval`, `memory`, and
`summarize` capabilities: it answers RAG contracts and serves direct
`retrieve` requests from other peers, so Hermes and OpenClaw can ground an
answer in swarm history without each re-implementing retrieval.

Scribe stays useful with Ollama down — it returns the cited passages
themselves, which is the part only the vault can provide.

Run:
    pip install -r requirements.txt
    python scribe_agent.py

Env vars:
    GATEWAY_URL     default ws://localhost:8765
    GATEWAY_TOKEN   shared secret — must match the gateway's GATEWAY_TOKEN
    VAULT_URL       default http://localhost:7070  (Shell Cracked)
    OLLAMA_URL      default http://localhost:11434
    DEFAULT_MODEL   default llama3
"""

import asyncio, json, os, sys, time
import requests
import websockets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mesh import MeshPeer
from offload import run_blocking
from rag import citation_line, ingest, retrieve
from vault_client import emit_heartbeat, emit_memory, heartbeat_payload

GATEWAY_URL     = os.environ.get("GATEWAY_URL", "ws://localhost:8765")
GATEWAY_TOKEN   = os.environ.get("GATEWAY_TOKEN")
OLLAMA_URL      = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL   = os.environ.get("DEFAULT_MODEL", "llama3")
VAULT_URL       = os.environ.get("VAULT_URL", "http://localhost:7070").rstrip("/")
HEARTBEAT_EVERY = 10
RECONNECT_DELAY = 5

CAPABILITIES = ["retrieval", "memory", "summarize"]


# ── Backend probes ────────────────────────────────────────────────────────────

def vault_available() -> bool:
    try:
        return requests.get(f"{VAULT_URL}/health", timeout=2).status_code == 200
    except Exception:
        return False


def ollama_available() -> bool:
    try:
        return requests.get(f"{OLLAMA_URL}/api/tags", timeout=3).status_code == 200
    except Exception:
        return False


def ollama_generate(prompt: str, model: str = DEFAULT_MODEL) -> str:
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=120,
        )
        r.raise_for_status()
        return r.json().get("response", "(no response from Ollama)")
    except Exception as e:
        return f"[Scribe error] Ollama call failed: {e}"


# ── Retrieval ─────────────────────────────────────────────────────────────────

def digest(question: str, grounded: dict) -> str:
    """Answer from passages alone, for when no model is available.

    Not a summary — the verbatim passages plus provenance. Paraphrasing
    without a model would just be lossy truncation.
    """
    if not grounded.get("grounded"):
        reason = "vault unreachable" if grounded.get("degraded") else "no matching passages"
        return f"[scribe] No swarm memory for {question!r} ({reason})."
    lines = [f"[scribe] {len(grounded['citations'])} passage(s) for {question!r}:", ""]
    lines.append(grounded["context"])
    provenance = citation_line(grounded["citations"])
    if provenance:
        lines += ["", f"sources: {provenance}"]
    return "\n".join(lines)


def answer(question: str, model: str = DEFAULT_MODEL, generate=None, fetch=None) -> dict:
    """Retrieve, then synthesize if a model is up. Blocking — offload it."""
    if not isinstance(question, str) or not question.strip():
        return {"result": "[scribe] empty question", "confidence": 0.0, "ok": False, "citations": []}

    retriever = fetch if fetch is not None else retrieve
    grounded = retriever(question)
    citations = grounded.get("citations", [])

    if generate is None and not ollama_available():
        return {
            "result": digest(question, grounded),
            # Passages without synthesis: useful, but not a finished answer.
            "confidence": 0.5 if grounded.get("grounded") else 0.2,
            "ok": True,
            "citations": citations,
        }

    generator = generate if generate is not None else ollama_generate
    reply = generator(grounded["prompt"], model)
    if grounded.get("grounded"):
        provenance = citation_line(citations)
        if provenance:
            reply = f"{reply}\n\nsources: {provenance}"
    return {
        "result": reply,
        "confidence": 0.85 if grounded.get("grounded") else 0.45,
        "ok": True,
        "citations": citations,
    }


# ── Mesh handlers ─────────────────────────────────────────────────────────────

peer = MeshPeer("scribe", CAPABILITIES, healthy=vault_available)


def handle_award(task: str, capabilities: list, task_id: str) -> dict:
    """Awarded a retrieval contract."""
    print(f"[Scribe] AWARD {task_id} → {str(task)[:80]}")
    outcome = answer(str(task))
    if outcome.get("ok"):
        # Feed the answer back into memory so the swarm compounds context.
        emit_memory(
            "scribe",
            "mesh_answer",
            f"{str(task)[:1200]}\n---\n{str(outcome['result'])[:4000]}",
            {"task_id": task_id, "capabilities": list(capabilities)},
        )
    return outcome


def handle_message(intent: str, body, sender: str):
    """Serve a direct peer request. `retrieve` returns context, not prose."""
    query = ""
    if isinstance(body, dict):
        query = body.get("query") or body.get("question") or body.get("task") or ""
    elif isinstance(body, str):
        query = body
    query = str(query).strip()
    if not query:
        return {"error": "no query supplied"}

    print(f"[Scribe] {sender} asked to {intent}: {query[:70]}")
    if intent in {"retrieve", "recall", "context", "message"}:
        grounded = retrieve(query)
        return {
            "context": grounded.get("context", ""),
            "citations": grounded.get("citations", []),
            "grounded": bool(grounded.get("grounded")),
            "degraded": bool(grounded.get("degraded")),
        }
    if intent in {"summarize", "answer"}:
        return answer(query)
    return {"error": f"unsupported intent {intent!r}"}


def handle_gossip(topic: str, body) -> None:
    """Persist mesh notices so they are retrievable later."""
    if not isinstance(topic, str) or not topic.strip():
        return
    try:
        text = body if isinstance(body, str) else json.dumps(body, default=str)
    except (TypeError, ValueError):
        text = str(body)
    if not text.strip():
        return
    ingest("scribe", f"gossip_{topic[:32]}", text[:4000], {"topic": topic[:32]}, source="mesh-gossip")


peer.on_award(handle_award)
peer.on_message(handle_message)
peer.on_gossip(handle_gossip)


# ── Gateway loop ──────────────────────────────────────────────────────────────

async def run():
    print("[Scribe] Starting — swarm librarian (retrieval / memory / summarize)")
    if not GATEWAY_TOKEN:
        print("[Scribe] ⚠ GATEWAY_TOKEN not set — only OK if the gateway is also running unauthenticated on localhost.")

    while True:
        try:
            async with websockets.connect(GATEWAY_URL) as ws:
                await ws.send(json.dumps(peer.hello_payload(GATEWAY_TOKEN)))

                ack_raw = await asyncio.wait_for(ws.recv(), timeout=5)
                ack = json.loads(ack_raw)
                if ack.get("type") == "ERROR":
                    print(f"[Scribe] ❌ Gateway rejected handshake: {ack.get('message')}")
                    await asyncio.sleep(RECONNECT_DELAY)
                    continue

                joined = (ack.get("mesh") or {}).get("joined")
                print(f"[Scribe] Connected ✅  mesh={'joined' if joined else 'observer'}  caps={CAPABILITIES}")

                vault_up = await run_blocking(vault_available)
                print(f"[Scribe] Shell Cracked vault: {'online' if vault_up else 'offline (degraded retrieval)'}")

                heartbeat_task = asyncio.create_task(heartbeat(ws))
                try:
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        if await peer.handle(ws, msg):
                            continue
                        if msg.get("type") == "ERROR":
                            print(f"[Scribe] Gateway error: {msg.get('message')}")
                finally:
                    heartbeat_task.cancel()

        except (websockets.ConnectionClosed, ConnectionRefusedError, OSError) as e:
            print(f"[Scribe] Gateway disconnected: {e} — retrying in {RECONNECT_DELAY}s")
            await asyncio.sleep(RECONNECT_DELAY)
        except asyncio.TimeoutError:
            print(f"[Scribe] Gateway did not respond to handshake — retrying in {RECONNECT_DELAY}s")
            await asyncio.sleep(RECONNECT_DELAY)
        except Exception as e:
            print(f"[Scribe] Unexpected error: {e} — retrying in {RECONNECT_DELAY}s")
            await asyncio.sleep(RECONNECT_DELAY)


async def heartbeat(ws, emit=emit_heartbeat, every=HEARTBEAT_EVERY):
    if emit is not None and not callable(emit):
        raise TypeError("emit must be callable or None")
    if not isinstance(every, (int, float)) or every < 0:
        raise ValueError("every must be a non-negative number")
    while True:
        await asyncio.sleep(every)
        try:
            ts = time.time()
            payload = heartbeat_payload("scribe", ts)
            # Queue depth rides the heartbeat so the gateway can price bids.
            payload["load"] = peer.load
            await ws.send(json.dumps(payload))
            if emit is not None:
                await run_blocking(emit, payload["agent"], payload["ts"], payload["bhive_slot"], "agent")
        except Exception:
            break


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n[Scribe] Stopped.")
