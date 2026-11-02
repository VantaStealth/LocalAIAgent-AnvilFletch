"""Drive the backend exactly like the UI does: JSON lines in, JSON lines out."""
import json
import subprocess
import sys
import threading
import time

PROMPT = sys.argv[1] if len(sys.argv) > 1 else "hi"
LIMIT = float(sys.argv[2]) if len(sys.argv) > 2 else 180.0

p = subprocess.Popen(
    ["py", "src\\agent_backend.py", "--serve"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    cwd=r"C:\Users\Umar\Desktop\Anti\LET AI HAVE FUUUN",
    bufsize=0,
)

t0 = time.time()
seen = []


def reader():
    for raw in p.stdout:
        t = time.time() - t0
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            print(f"{t:7.1f}s RAW {line[:200]}")
            continue
        e = d.get("e", "?")
        body = {k: v for k, v in d.items() if k != "e"}
        print(f"{t:7.1f}s {e:8s} {json.dumps(body, ensure_ascii=False)[:240]}")
        seen.append((t, e))
        if e == "done":
            break


th = threading.Thread(target=reader, daemon=True)
th.start()

# wait for the ready handshake, then send one chat request
deadline = time.time() + 20
while time.time() < deadline and not any(e == "ready" for _, e in seen):
    time.sleep(0.05)

p.stdin.write(json.dumps({"action": "chat", "prompt": PROMPT}).encode() + b"\n")
p.stdin.flush()

th.join(LIMIT)
if th.is_alive():
    print(f"!! still running after {LIMIT:.0f}s, killing")
    p.kill()
    sys.exit(2)

p.stdin.close()
p.wait(timeout=5)
print("exit", p.returncode, "events", len(seen))
