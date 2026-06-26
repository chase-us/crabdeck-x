"""
OpenClaw Agent v2.2 — CrabDeck Sovereign Node
Connects to CrabDeck Gateway, receives TASK events, executes via Hermes/Ollama reasoning,
reports back TASK_RESULT. Integrates with OpenClaw.app via local HTTP if available.

Run:
    pip install -r requirements.txt
    python openclaw_agent.py

Env vars:
    GATEWAY_URL        default ws://localhost:8765
    GATEWAY_TOKEN       shared secret — must match the gateway's GATEWAY_TOKEN.
    OLLAMA_URL          default http://localhost:11434
    OPENCLAW_HTTP       default http://localhost:3131
    DEFAULT_MODEL       default llama3
    ENABLE_SHELL_EXEC   "1" to allow executing <CMD> blocks the model produces.
                        DEFAULT IS OFF. Read the warning below before turning it on.
    SHELL_ALLOWLIST     comma-separated list of command prefixes permitted when
                        ENABLE_SHELL_EXEC=1, e.g. "git status,ls,dir,docker ps".
                        If unset while shell exec is enabled, ALL commands are
                        allowed — only do this on a trusted, isolated host.

⚠️  SECURITY NOTE (read before deploying):
    v2.1 wired an unauthenticated WebSocket straight into this agent's shell
    executor: anyone who could reach the gateway could send a TASK, get the
    LLM to emit a <CMD> block, and have it run with the OpenClaw process's
    permissions — i.e. unauthenticated remote code execution. v2.2 fixes the
    "unauthenticated" half via GATEWAY_TOKEN, but a stolen/leaked token (or a
    successful prompt-injection inside a TASK) would still let a caller run
    shell commands as this process. Shell execution is therefore now opt-in
    (ENABLE_SHELL_EXEC=1) and supports an allowlist. Leave it off, or scope
    SHELL_ALLOWLIST tightly, on anything internet-facing.
"""

import asyncio, json, os, subprocess, sys, platform, time
import requests
import websockets

GATEWAY_URL       = os.environ.get("GATEWAY_URL", "ws://localhost:8765")
GATEWAY_TOKEN     = os.environ.get("GATEWAY_TOKEN")
OLLAMA_URL        = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OPENCLAW_HTTP     = os.environ.get("OPENCLAW_HTTP", "http://localhost:3131")
DEFAULT_MODEL     = os.environ.get("DEFAULT_MODEL", "llama3")
HEARTBEAT_EVERY   = 10
RECONNECT_DELAY   = 10

ENABLE_SHELL_EXEC = os.environ.get("ENABLE_SHELL_EXEC", "0") == "1"
_allowlist_raw    = os.environ.get("SHELL_ALLOWLIST", "").strip()
SHELL_ALLOWLIST   = [p.strip() for p in _allowlist_raw.split(",") if p.strip()] if _allowlist_raw else None

# ── OpenClaw App integration (optional) ───────────────────────────────────────

def openclaw_available():
    try:
        r = requests.get(f"{OPENCLAW_HTTP}/health", timeout=2)
        return r.status_code == 200
    except Exception:
        return False

def openclaw_task(task_text: str) -> str:
    """Send task to OpenClaw app REST endpoint if available."""
    try:
        r = requests.post(f"{OPENCLAW_HTTP}/task", json={"task": task_text}, timeout=30)
        r.raise_for_status()
        return r.json().get("result", "(no result)")
    except Exception:
        return None

# ── System tools ───────────────────────────────────────────────────────────────

def system_info() -> dict:
    return {
        "platform": platform.system(),
        "node":     platform.node(),
        "python":   sys.version.split()[0],
        "cwd":      os.getcwd(),
        "pid":      os.getpid(),
        "shell_exec_enabled": ENABLE_SHELL_EXEC,
    }

def command_allowed(cmd: str) -> bool:
    if SHELL_ALLOWLIST is None:
        return True  # no allowlist configured — operator accepted that risk explicitly
    return any(cmd.strip().startswith(prefix) for prefix in SHELL_ALLOWLIST)

def run_shell(cmd: str) -> str:
    """Execute a shell command and return output. Gated by ENABLE_SHELL_EXEC / SHELL_ALLOWLIST."""
    if not ENABLE_SHELL_EXEC:
        return ("[blocked] Shell execution is disabled on this OpenClaw node "
                "(set ENABLE_SHELL_EXEC=1 to allow it — see the security note "
                "at the top of openclaw_agent.py first).")
    if not command_allowed(cmd):
        return f"[blocked] Command does not match SHELL_ALLOWLIST: {cmd!r}"
    try:
        shell = platform.system() == "Windows"
        result = subprocess.run(cmd, shell=shell, capture_output=True, text=True, timeout=30)
        out = (result.stdout + result.stderr).strip()
        return out[:2000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "[timeout] Command took too long"
    except Exception as e:
        return f"[error] {e}"

def ollama_reason(task: str, model: str = DEFAULT_MODEL) -> str:
    """Use Ollama to reason about a task and produce an action."""
    cmd_instructions = (
        "For system tasks: respond with a shell command wrapped in <CMD>command here</CMD>."
        if ENABLE_SHELL_EXEC else
        "Shell execution is disabled on this node — do not suggest <CMD> blocks; "
        "explain what command WOULD be needed instead, in plain text."
    )
    system_ctx = f"""You are OpenClaw, a sovereign system agent running on {platform.system()}.
You receive TASK events from the CrabDeck platform and decide how to handle them.
{cmd_instructions}
For questions or analysis: respond with a clear answer.
Keep responses concise and actionable."""

    prompt = f"{system_ctx}\n\nTask: {task}\n\nResponse:"
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=60,
        )
        r.raise_for_status()
        return r.json().get("response", "(no response)")
    except Exception as e:
        return f"[Ollama error] {e}"

def handle_task(payload) -> str:
    """Route a TASK payload to the right handler."""
    task  = payload.get("task", "") if isinstance(payload, dict) else str(payload)
    model = payload.get("model", DEFAULT_MODEL) if isinstance(payload, dict) else DEFAULT_MODEL

    print(f"[OpenClaw] Task: {task[:80]}")

    if openclaw_available():
        result = openclaw_task(task)
        if result:
            print(f"[OpenClaw] App result: {result[:80]}")
            return f"[via OpenClaw.app] {result}"

    response = ollama_reason(task, model)

    if ENABLE_SHELL_EXEC and "<CMD>" in response and "</CMD>" in response:
        start = response.index("<CMD>") + 5
        end   = response.index("</CMD>")
        cmd   = response[start:end].strip()
        print(f"[OpenClaw] Executing: {cmd}")
        output = run_shell(cmd)
        return f"Reasoned: {response[:200]}\n\nCommand output:\n{output}"

    return response

# ── Gateway loop ───────────────────────────────────────────────────────────────

async def run():
    info = system_info()
    print(f"[OpenClaw] Starting on {info['platform']} / {info['node']}")
    print(f"[OpenClaw] Shell exec: {'ENABLED' if ENABLE_SHELL_EXEC else 'disabled'}"
          + (f" (allowlist: {SHELL_ALLOWLIST})" if ENABLE_SHELL_EXEC and SHELL_ALLOWLIST else ""))
    if not GATEWAY_TOKEN:
        print("[OpenClaw] ⚠ GATEWAY_TOKEN not set — only OK if the gateway is also running unauthenticated on localhost.")

    while True:
        try:
            async with websockets.connect(GATEWAY_URL) as ws:
                hello = {"type": "HELLO", "client": "openclaw", "version": "2.2", "system": info}
                if GATEWAY_TOKEN:
                    hello["token"] = GATEWAY_TOKEN
                await ws.send(json.dumps(hello))

                ack_raw = await asyncio.wait_for(ws.recv(), timeout=5)
                ack = json.loads(ack_raw)
                if ack.get("type") == "ERROR":
                    print(f"[OpenClaw] ❌ Gateway rejected handshake: {ack.get('message')}")
                    await asyncio.sleep(RECONNECT_DELAY)
                    continue

                print("[OpenClaw] Connected to Gateway ✅")

                await ws.send(json.dumps({
                    "type": "TASK_RESULT", "agent": "openclaw",
                    "payload": {"status": "online", "system": info,
                                "openclaw_app": openclaw_available()},
                }))

                heartbeat_task = asyncio.create_task(heartbeat(ws))

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue

                    mtype = msg.get("type")

                    if mtype == "TASK":
                        payload = msg.get("payload", {})
                        result  = handle_task(payload)
                        await ws.send(json.dumps({
                            "type":    "TASK_RESULT",
                            "agent":   "openclaw",
                            "payload": {"task": str(payload)[:80], "result": result},
                        }))

                    elif mtype == "WELCOME":
                        print(f"[OpenClaw] Gateway: {msg.get('message', '')}")

                    elif mtype == "ERROR":
                        print(f"[OpenClaw] Gateway error: {msg.get('message')}")

                heartbeat_task.cancel()

        except (websockets.ConnectionClosed, ConnectionRefusedError, OSError) as e:
            print(f"[OpenClaw] Gateway disconnected: {e} — retrying in {RECONNECT_DELAY}s")
            await asyncio.sleep(RECONNECT_DELAY)
        except asyncio.TimeoutError:
            print(f"[OpenClaw] Gateway did not respond to handshake — retrying in {RECONNECT_DELAY}s")
            await asyncio.sleep(RECONNECT_DELAY)
        except Exception as e:
            print(f"[OpenClaw] Error: {e} — retrying in {RECONNECT_DELAY}s")
            await asyncio.sleep(RECONNECT_DELAY)


async def heartbeat(ws):
    while True:
        await asyncio.sleep(HEARTBEAT_EVERY)
        try:
            await ws.send(json.dumps({"type": "HEARTBEAT", "agent": "openclaw", "ts": time.time()}))
        except Exception:
            break


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n[OpenClaw] Stopped.")
