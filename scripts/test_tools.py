"""Smoke-test every tool with safe arguments. Destructive tests use a scratch dir."""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import agent_backend as A  # noqa: E402

scratch = A.DATA / "_tooltest"
scratch.mkdir(parents=True, exist_ok=True)
(src := scratch / "alpha beta song.mp3").write_bytes(b"ID3" + b"\x00" * 2048)
(src2 := scratch / "second file.mp3").write_bytes(b"ID3" + b"\x00" * 1024)
(dst := scratch / "out").mkdir(exist_ok=True)

cases = [
    ("sysinfo", {}),
    ("now", {}),
    ("calculate", {"expression": "(17*3+5)/2"}),
    ("list_dir", {"path": str(scratch)}),
    ("tree", {"path": str(scratch), "depth": 2}),
    ("glob", {"pattern": "**/*.mp3", "path": str(scratch)}),
    ("grep", {"pattern": "ID3", "path": str(scratch), "glob": "**/*.mp3"}),
    ("write_file", {"path": str(scratch / "notes.txt"), "content": "hello\nworld\n"}),
    ("read_file", {"path": str(scratch / "notes.txt")}),
    ("edit_file", {"path": str(scratch / "notes.txt"), "old": "world", "new": "there"}),
    ("hash", {"path": str(scratch / "notes.txt")}),
    ("mkdir", {"path": str(scratch / "nested" / "deep")}),
    ("copy", {"src": str(src), "dst": str(dst / src.name)}),
    ("rename", {"path": str(dst / src.name), "new_name": "alpha.mp3"}),
    ("shorten_names", {"path": str(scratch), "pattern": "*.mp3", "max_len": 6, "dry_run": True}),
    ("move", {"src": str(src2), "dst": str(dst / src2.name)}),
    ("gather", {"source": str(scratch), "dest": str(scratch / "gathered"),
                "pattern": "*.mp3", "shorten": True, "move": False}),
    ("archive", {"path": str(scratch / "gathered"), "dest": str(scratch / "kit.zip"),
                 "mode": "compress", "fmt": "zip"}),
    ("archive", {"path": str(scratch / "kit.zip"), "dest": str(scratch / "unz"),
                 "mode": "extract", "fmt": "zip"}),
    ("process_list", {}),
    ("run_command", {"command": "$PSVersionTable.PSVersion.ToString()"}),
    ("run_command", {"command": "echo hi from cmd", "shell": "cmd"}),
    ("clipboard_write", {"text": "anvil fletch test"}),
    ("clipboard_read", {}),
    ("screenshot", {"path": str(scratch / "shot.png")}),
    ("web_fetch", {"url": "https://example.com", "max_chars": 300}),
    ("web_search", {"query": "ollama windows install", "max_results": 4}),
    ("list_tools", {}),
    ("lessons", {}),
    ("delete", {"path": str(scratch / "nested")}),
]

fails = []
for name, args in cases:
    t0 = time.time()
    try:
        ok, summary, detail = A.dispatch(name, args)
    except Exception as e:
        ok, summary, detail = False, f"{type(e).__name__}: {e}", ""
    ms = int((time.time() - t0) * 1000)
    flag = "OK  " if ok else "FAIL"
    if not ok:
        fails.append((name, summary))
    print(f"{flag} {name:16s} {ms:6d}ms  {str(summary)[:110]}")
    print(f"     {str(detail)[:300].replace(chr(10), ' | ')}")

print("\nFAILURES:", fails or "none")

# leftovers
print("scratch:", sorted(p.name for p in scratch.iterdir()))
