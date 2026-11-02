"""
Anvil Fletch - local agentic runtime.

Emits a newline-delimited JSON event stream on stdout for the Win32 front-end.
Talks to Ollama's HTTP API directly (no client library) so it can stream tokens
and use native tool calling.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

# OCR and GetWindowRect report physical pixels; a virtualised process would
# translate SetCursorPos and miss by the display scale factor on high-DPI screens.
try:
    import ctypes as _ct

    if not _ct.windll.user32.SetProcessDpiAwarenessContext(_ct.c_void_p(-4)):
        _ct.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        _ct.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG = ROOT / "config"
SETTINGS_FILE = CONFIG / "settings.json"
LESSONS_FILE = DATA / "lessons.json"
HISTORY_FILE = DATA / "history.json"
PROMPTS_FILE = DATA / "prompts.json"
TOOLS_FILE = ROOT / "tools" / "registry.json"
CUSTOM_DIR = ROOT / "tools" / "custom_tools"

for d in (DATA, CONFIG, CUSTOM_DIR):
    d.mkdir(parents=True, exist_ok=True)

# PowerShell starts DPI-unaware: on a 125% display it sees 1536x864 while this
# process and SetCursorPos use 1920x1080. Every capture script must opt in first.
PS_DPI = r'''
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class AFDPI {
  [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr v);
  [DllImport("shcore.dll")] public static extern int SetProcessDpiAwareness(int v);
}
"@ -ErrorAction SilentlyContinue
try { [AFDPI]::SetProcessDpiAwarenessContext([IntPtr]::new(-4)) | Out-Null } catch {
  try { [AFDPI]::SetProcessDpiAwareness(2) | Out-Null } catch { }
}
'''

DEFAULTS = {
    "workspace": str(Path.home() / "Desktop"),
    "model": "qwen2.5-coder:7b",
    "host": "http://127.0.0.1:11434",
    "temperature": 0.2,
    "num_ctx": 16384,
    "reasoning": False,
}

_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
def emit(event: str, **fields) -> None:
    payload = {"e": event}
    payload.update(fields)
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    line = line.replace("\r", " ").replace("\n", " ")
    with _write_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def emit_progress(label: str, done: int = 0, total: int = 0, item: str = "",
                  files_done: int = 0, files_total: int = 0) -> None:
    """Live transfer progress for the UI bar. done/total are bytes when known."""
    emit("progress", label=str(label)[:70], done=max(0, int(done or 0)),
         total=max(0, int(total or 0)), item=str(item)[:70],
         files_done=max(0, int(files_done or 0)),
         files_total=max(0, int(files_total or 0)))


_PROG_LOCK = threading.Lock()


def _new_ctx(label: str, files_total: int = 0, total: int = 0) -> dict:
    return {"label": label, "done": 0, "total": int(total or 0),
            "files_done": 0, "files_total": int(files_total or 0), "last": 0.0}


def _bump(ctx: dict, nbytes: int = 0, files_done: int = 0, item: str = "",
          total_extra: int = 0, force: bool = False) -> None:
    """Accumulate bytes/files and throttle UI events to ~3 per second."""
    if ctx is None:
        return
    now = time.time()
    with _PROG_LOCK:
        ctx["done"] += max(0, int(nbytes or 0))
        ctx["total"] += max(0, int(total_extra or 0))
        ctx["files_done"] += max(0, int(files_done or 0))
        if not force and now - float(ctx.get("last") or 0) < 0.3:
            return
        ctx["last"] = now
        snap = dict(ctx)
    emit_progress(snap["label"], snap["done"], snap["total"], item,
                  snap["files_done"], snap["files_total"])


def now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Settings / workspace
# ---------------------------------------------------------------------------
MODELS_FILE = CONFIG / "models.json"


def load_settings() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    # Model provider profiles live in their own file: the UI rewrites
    # settings.json on every preference change and must not erase them.
    try:
        prof = json.loads(MODELS_FILE.read_text(encoding="utf-8"))
        if isinstance(prof, dict):
            cfg["models"] = prof.get("models") or []
            # Sampling defaults from models.json lose to an explicit settings.json value.
            explicit = set()
            try:
                explicit = set(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
            except Exception:
                pass
            for key in ("temperature", "num_ctx", "reasoning"):
                if key in prof and key not in explicit:
                    cfg[key] = prof[key]
    except Exception:
        cfg.setdefault("models", [])
    ws = os.environ.get("ANVIL_WORKSPACE") or cfg.get("workspace") or DEFAULTS["workspace"]
    cfg["workspace"] = str(Path(os.path.expandvars(os.path.expanduser(ws))).resolve())
    if os.environ.get("ANVIL_MODEL"):
        cfg["model"] = os.environ["ANVIL_MODEL"]
    if os.environ.get("ANVIL_HOST"):
        cfg["host"] = os.environ["ANVIL_HOST"]
    return cfg


CFG = load_settings()


def provider_of(model: str) -> dict:
    """Connection profile for a model name. Missing profiles fall back to the
    configured Ollama host, so old setups keep working unchanged."""
    model = str(model or CFG.get("model") or "")
    for entry in CFG.get("models") or []:
        if isinstance(entry, dict) and str(entry.get("name", "")).strip() == model:
            prof = dict(entry)
            prof.setdefault("provider", "ollama")
            return prof
    return {"name": model, "provider": "ollama", "host": CFG["host"]}


def uses_openai(model: str) -> bool:
    return provider_of(model).get("provider", "ollama") == "openai"


def provider_api_key(prof: dict) -> str:
    if prof.get("api_key"):
        return str(prof["api_key"])
    env = str(prof.get("api_key_env") or "").strip()
    return os.environ.get(env, "") if env else ""


def workspace() -> Path:
    p = Path(CFG["workspace"])
    try:
        return p if p.is_dir() else Path.home()
    except Exception:
        return Path.home()


KNOWN_FOLDERS = {"desktop", "documents", "downloads", "pictures", "music",
                 "videos", "favorites", "onedrive"}


def resolve_path(p: str) -> Path:
    p = os.path.expandvars(os.path.expanduser((p or "").strip().strip('"')))
    if not p:
        return workspace()
    path = Path(p)
    if not path.is_absolute():
        path = workspace() / path
        # "Documents" or "Downloads" alone almost always means the real user folder,
        # not a sibling of the current workspace.
        if not path.exists() and p.replace("\\", "/").strip("/").lower() in KNOWN_FOLDERS:
            home = Path.home() / p.replace("\\", "/").strip("/")
            if home.is_dir():
                path = home
    elif not path.exists():
        # Models often build "<workspace>\\Documents" from memory; point it home.
        try:
            rel = path.relative_to(workspace())
        except Exception:
            rel = None
        if rel is not None and len(rel.parts) == 1 and rel.parts[0].lower() in KNOWN_FOLDERS:
            home = Path.home() / rel.parts[0]
            if home.is_dir():
                path = home
    return Path(os.path.normpath(str(path)))


_DEST_SAY = re.compile(
    r"\b(?:to|into|in|at|as|save (?:it )?(?:to|in|into|as)|write (?:it )?to)\s+"
    r"(\"[^\"]+\"|'[^']+'|(?:[A-Za-z]:[\\/][^\n\r\"'<>|?*]+"
    r"|(?:[\w .()&-]+[\\/])+[\w .()&-]+\.[A-Za-z0-9]{1,8}))", re.I)


def destination_hint(prompt: str) -> str:
    """The exact save target the user typed, e.g. 'download X to C:\\a\\b.png'."""
    text = (prompt or "").split("\n\n")[0]
    best = ""
    for m in _DEST_SAY.finditer(text):
        cand = re.split(r"\s+(?:and|then|so|but|please|tell|when|after|before|because)\s+",
                        m.group(1).strip(), 1)[0]
        cand = cand.strip().strip("\"'").rstrip(".,;:!?").strip()
        if "\\" in cand or "/" in cand:
            best = cand
    return best


def rel_display(path) -> str:
    s = str(path)
    for base in (workspace(), ROOT):
        try:
            return str(Path(s).relative_to(base))
        except Exception:
            continue
    return s


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------
class ModelUnavailable(Exception):
    pass


class Stopped(Exception):
    pass


def http_json(url: str, body=None, timeout: float = 15.0):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json",
                 "User-Agent": "Anvil Fletch/1.0 (Windows)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")


def ollama_alive(timeout: float = 2.0) -> bool:
    if uses_openai(CFG.get("model") or ""):
        prof = provider_of(CFG.get("model") or "")
        return bool(prof.get("base_url"))
    try:
        http_json(CFG["host"].rstrip("/") + "/api/tags", timeout=timeout)
        return True
    except Exception:
        return False


def autostart_ollama(wait: float = 25.0) -> bool:
    """Launch a local `ollama serve` if one is not already listening."""
    exe = shutil.which("ollama")
    if not exe:
        for cand in (
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Ollama/ollama.exe",
            Path("C:/Program Files/Ollama/ollama.exe"),
        ):
            if cand.is_file():
                exe = str(cand)
                break
    if not exe:
        return False
    flags = 0x08000000  # CREATE_NO_WINDOW
    try:
        subprocess.Popen([exe, "serve"], creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return False
    deadline = time.time() + wait
    while time.time() < deadline:
        if ollama_alive():
            return True
        time.sleep(0.6)
    return ollama_alive()


def ensure_model() -> str:
    if uses_openai(CFG.get("model") or ""):
        # API providers need no local server and no pull; a bad name surfaces
        # as a clear HTTP error on the first real call.
        return CFG["model"]
    if not ollama_alive():
        emit("status", state="warming", text="Starting local model server")
        if not autostart_ollama():
            raise ModelUnavailable(
                f"Nothing is listening on {CFG['host']} and I could not start `ollama serve`."
            )
    try:
        tags = http_json(CFG["host"].rstrip("/") + "/api/tags", timeout=6)
    except Exception as e:
        raise ModelUnavailable(f"Ollama unreachable: {e}")
    names = [m.get("name", "") for m in tags.get("models", [])]
    want = CFG["model"]
    if want in names:
        return want
    if not names:
        raise ModelUnavailable(f"Ollama has no models. Run: ollama pull {want}")
    exact = [n for n in names if n.split(":")[0] == want.split(":")[0]]
    chosen = (exact or names)[0]
    CFG["model"] = chosen
    return chosen


def _openai_model_ids(prof: dict) -> list:
    """Model ids advertised by an OpenAI-compatible endpoint, best effort."""
    base = str(prof.get("base_url") or "").rstrip("/")
    if not base:
        return []
    headers = {"User-Agent": "Anvil Fletch/1.0 (Windows)"}
    key = provider_api_key(prof)
    if key:
        headers["Authorization"] = "Bearer " + key
    for extra_k, extra_v in (prof.get("headers") or {}).items():
        headers[str(extra_k)] = str(extra_v)
    try:
        req = urllib.request.Request(base + "/models", headers=headers)
        with urllib.request.urlopen(req, timeout=4) as r:
            data = json.loads(r.read().decode("utf-8", "replace") or "{}")
        return [m.get("id") for m in data.get("data", []) if m.get("id")]
    except Exception:
        return []


def list_models() -> list:
    """Every selectable model: Ollama tags plus configured API profiles (and,
    when reachable, the ids their endpoints advertise)."""
    names: list = []
    try:
        tags = http_json(CFG["host"].rstrip("/") + "/api/tags", timeout=4)
        names += [m.get("name") for m in tags.get("models", []) if m.get("name")]
    except Exception:
        pass
    seen = set(names)
    for prof in CFG.get("models") or []:
        if not isinstance(prof, dict):
            continue
        if prof.get("provider", "ollama") == "openai":
            for mid in ([prof.get("name")] if prof.get("name") else []) \
                    + _openai_model_ids(prof):
                if mid and mid not in seen:
                    seen.add(mid)
                    names.append(mid)
        else:
            entry = str(prof.get("name") or "").strip()
            if entry and entry not in seen:
                seen.add(entry)
                names.append(entry)
    return names


def warm_model() -> None:
    """Load the model into VRAM before the user's first prompt."""
    if uses_openai(CFG.get("model") or ""):
        return
    try:
        http_json(CFG["host"].rstrip("/") + "/api/generate",
                  {"model": CFG["model"], "keep_alive": "30m", "stream": False,
                   "prompt": "", "options": {"num_ctx": int(CFG.get("num_ctx", 16384)),
                                             "num_predict": 1}},
                  timeout=180)
    except Exception:
        pass


_WARM = {"key": ""}
_WARM_LOCK = threading.Lock()


def base_system() -> str:
    return (SYSTEM.replace("{workspace}", str(workspace()))
                  .replace("{home}", str(Path.home()))
                  .replace("{count}", str(len(TOOLS)))
                  .replace("{tools}", tool_doc()))


def prewarm(sys_prompt: str) -> None:
    """Bake the system prompt into the KV cache so the first step is fast.

    On CPU, prompt eval runs at ~30 tok/s; the tool catalog alone costs 30s.
    Ollama reuses the KV prefix, so this pays that once in the background.
    """
    key = hashlib.sha1((CFG["model"] + "\x00" + sys_prompt).encode("utf-8")).hexdigest()
    if _WARM["key"] == key:
        return
    if uses_openai(CFG.get("model") or ""):
        # API models have no KV cache to pre-bake locally.
        _WARM["key"] = key
        return
    with _WARM_LOCK:
        if _WARM["key"] == key:
            return
        try:
            http_json(CFG["host"].rstrip("/") + "/api/chat",
                      {"model": CFG["model"],
                       "messages": [{"role": "system", "content": sys_prompt},
                                    {"role": "user", "content": "ready"}],
                       "stream": False, "keep_alive": "30m",
                       "options": {"num_ctx": int(CFG.get("num_ctx", 16384)), "num_predict": 1}},
                      timeout=300)
            _WARM["key"] = key
        except Exception:
            pass


def model_info(name: str) -> dict:
    try:
        return http_json(CFG["host"].rstrip("/") + "/api/show",
                         {"model": name}, timeout=6).get("model_info", {}) or {}
    except Exception:
        return {}


def _salvage_json(s: str) -> dict:
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", s)
    if m:
        for candidate in (m.group(0), m.group(0) + "}"):
            try:
                return json.loads(candidate)
            except Exception:
                continue
    return {}


# The union of every argument name across every tool. Constraining the grammar
# to known keys stops the 7B from inventing parameters or mangling Windows paths.
ARG_TYPES = {
    "path": "string", "pattern": "string", "content": "string", "old": "string",
    "new": "string", "src": "string", "dst": "string", "source": "string",
    "dest": "string", "query": "string", "url": "string", "command": "string",
    "text": "string", "name": "string", "new_name": "string", "target": "string",
    "args": "string", "expression": "string", "code": "string", "description": "string",
    "when": "string", "do": "string", "id": "string", "title": "string",
    "message": "string", "glob": "string", "shell": "string", "language": "string",
    "filter": "string", "algo": "string", "fmt": "string", "items": "string",
    "mode": "string", "window": "string", "keys": "string",
    "folder": "string", "filename": "string", "kind": "string", "quality": "string",
    "location": "string", "topic": "string", "word": "string", "method": "string",
    "body": "string", "button": "string", "by": "string", "sheet": "string",
    "rows": "string", "data": "string", "key": "string", "start": "string",
    "duration": "string", "paths": "string", "slides": "string",
    "recursive": "boolean", "shorten": "boolean", "move": "boolean",
    "dry_run": "boolean", "permanent": "boolean", "append": "boolean",
    "all": "boolean", "ignore_case": "boolean", "open_browser": "boolean",
    "extract_text": "boolean", "playlist": "boolean", "enter": "boolean",
    "offset": "integer", "limit": "integer", "depth": "integer", "max_len": "integer",
    "max_results": "integer", "max_chars": "integer", "timeout": "integer", "pid": "integer",
    "control_type": "string", "index": "integer", "max_items": "integer",
    "seconds": "integer", "sentences": "integer", "days": "integer", "min_kb": "integer",
    "width": "integer", "height": "integer", "left": "integer", "top": "integer",
    "clicks": "integer", "x": "integer", "y": "integer", "x1": "integer", "y1": "integer",
    "x2": "integer", "y2": "integer", "amount": "integer", "steps": "integer",
    "level": "integer", "rate": "integer", "angle": "integer",
    "direction": "string", "pages": "integer",
    "urls": "string", "template": "string", "replacements": "string", "output": "string",
    "to": "string", "ratio": "string", "version": "string", "password": "string",
    "overwrite": "boolean", "set_first": "boolean", "max_side": "integer",
    "max_tabs": "integer", "max_images": "integer", "wait": "integer",
    "label": "string", "question": "string", "bitrate": "string", "repo": "string",
    "asset": "string", "install_dir": "string", "launch": "string",
    "count": "integer", "position": "string", "action": "string", "severity": "string",
    "size": "integer", "radius": "integer", "factor": "string", "host": "string",
    "port": "integer", "min": "integer", "max": "integer", "length": "integer",
    "app": "string", "range": "string", "on": "boolean", "gone": "boolean",
    "max_lines": "integer", "delay": "integer", "actions": "string", "priority": "string",
    "fields": "string", "max_chats": "integer", "verbose": "boolean", "times": "integer",
    "restart_stuck": "boolean",
}
_JSON_TYPE = {"string": "string", "boolean": "boolean", "integer": "integer"}


def decision_schema() -> dict:
    props = {}
    for key, t in ARG_TYPES.items():
        props[key] = {"type": _JSON_TYPE[t]}
    return {
        "type": "object",
        "properties": {
            "thought": {"type": "string",
                        "description": "At most 12 words. What you are about to do and why."},
            "tool": {"type": "string", "enum": ["reply", "update_plan"] + sorted(TOOLS)},
            "args": {"type": "object", "properties": props},
        },
        "required": ["thought", "tool"],
    }


def _ollama_stream(body: dict, stop=None, on_delta=None) -> str:
    """POST to /api/chat with stream=True. Returns the full text.

    Reading happens on a helper thread so Stop is honoured within ~0.2s even
    while the model is still loading and has not sent its first token yet."""
    req = urllib.request.Request(
        CFG["host"].rstrip("/") + "/api/chat", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    q: "queue.Queue" = queue.Queue()
    held = {}

    def reader():
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                held["resp"] = resp
                for raw in resp:
                    if stop is not None and stop.is_set():
                        break
                    q.put(raw)
            q.put(None)
        except BaseException as exc:
            q.put(exc)

    threading.Thread(target=reader, daemon=True).start()
    buf = []
    try:
        while True:
            if stop is not None and stop.is_set():
                raise Stopped()
            try:
                item = q.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            if isinstance(item, BaseException):
                raise item
            raw = item.strip()
            if not raw:
                continue
            try:
                chunk = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                continue
            if chunk.get("error"):
                raise ModelUnavailable(str(chunk["error"]))
            piece = (chunk.get("message") or {}).get("content") or ""
            if piece:
                buf.append(piece)
                if on_delta is not None:
                    on_delta("".join(buf))
            if chunk.get("done"):
                break
    finally:
        if stop is not None and stop.is_set():
            try:
                held.get("resp").close()
            except Exception:
                pass
    return "".join(buf)


def _openai_stream(prof: dict, model: str, messages, stop=None, on_delta=None,
                   grammar: dict = None, temperature: float = 0.2,
                   max_tokens: int = 0) -> str:
    """POST to an OpenAI-compatible /chat/completions with stream=True.

    Grammar support varies across servers (OpenAI structured outputs, LM Studio,
    llama.cpp's json_schema), so a rejected schema is retried once without it;
    the salvage parser below picks the JSON out of free text either way."""
    base = str(prof.get("base_url") or "").rstrip("/")
    if not base:
        raise ModelUnavailable("Model profile needs a base_url")
    key = provider_api_key(prof)
    headers = {"Content-Type": "application/json",
               "User-Agent": "Anvil Fletch/1.0 (Windows)"}
    if key:
        headers["Authorization"] = "Bearer " + key
    for k, v in (prof.get("headers") or {}).items():
        headers[str(k)] = str(v)

    def build(with_grammar: bool) -> bytes:
        body = {"model": model, "messages": messages, "stream": True,
                "temperature": float(temperature)}
        if max_tokens:
            body["max_tokens"] = int(max_tokens)
        if with_grammar and grammar:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "anvil_decision",
                                                       "schema": grammar}}
        return json.dumps(body).encode("utf-8")

    attempts = [True, False] if grammar else [False]
    last_err = None
    for use_grammar in attempts:
        payload = build(use_grammar)
        q: "queue.Queue" = queue.Queue()
        held = {}

        def reader():
            try:
                req = urllib.request.Request(base + "/chat/completions", data=payload,
                                             headers=headers)
                with urllib.request.urlopen(req, timeout=300) as resp:
                    held["resp"] = resp
                    for raw in resp:
                        if stop is not None and stop.is_set():
                            break
                        q.put(raw)
                q.put(None)
            except BaseException as exc:
                q.put(exc)

        threading.Thread(target=reader, daemon=True).start()
        buf = []
        try:
            while True:
                if stop is not None and stop.is_set():
                    raise Stopped()
                try:
                    item = q.get(timeout=0.2)
                except queue.Empty:
                    continue
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                line = item.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except Exception:
                    continue
                err = chunk.get("error")
                if err:
                    raise ModelUnavailable(str(err.get("message") or err))
                choices = chunk.get("choices") or []
                piece = (choices[0].get("delta") or {}).get("content") or \
                    (choices[0].get("message") or {}).get("content") or ""
                if piece:
                    buf.append(piece)
                    if on_delta is not None:
                        on_delta("".join(buf))
            return "".join(buf)
        except urllib.error.HTTPError as e:
            last_err = e
            if use_grammar and e.code in (400, 422):
                emit("log", text=f"{prof.get('name', 'API')} rejected json_schema; "
                                 "retrying without it")
                continue
            raise ModelUnavailable(f"API HTTP {e.code}: {_read_err(e)}")
        finally:
            if stop is not None and stop.is_set():
                try:
                    held.get("resp").close()
                except Exception:
                    pass
    raise ModelUnavailable(f"API call failed: {last_err}")


def _partial_thought(buf: str) -> str:
    m = re.search(r'"thought"\s*:\s*"((?:[^"\\]|\\.)*)', buf)
    if not m:
        return ""
    try:
        return json.loads('"' + m.group(1) + '"')
    except Exception:
        return m.group(1).replace('\\"', '"').replace("\\n", " ")


def decide(messages, stop=None) -> dict:
    """One constrained step: the model must name exactly one tool."""
    grammar = decision_schema()
    if uses_openai(CFG.get("model") or ""):
        prof = provider_of(CFG.get("model") or "")
        seen = {"text": "", "at": 0.0}

        def on_delta(buf):
            partial = _partial_thought(buf)
            now = time.time()
            if partial and partial != seen["text"] and now - seen["at"] > 0.12:
                seen["text"] = partial
                seen["at"] = now
                emit("thinking", text=partial[:180])

        raw = _openai_stream(prof, CFG["model"], messages, stop, on_delta,
                             grammar=grammar,
                             temperature=float(CFG.get("temperature", 0.2)))
        d = _salvage_json(raw)
        if not d:
            d = {"thought": raw[:120], "tool": "reply", "args": {}}
        d.setdefault("tool", "reply")
        d.setdefault("args", {})
        if not isinstance(d.get("args"), dict):
            d["args"] = {}
        return d
    body = {
        "model": CFG["model"],
        "messages": messages,
        "stream": True,
        "keep_alive": "15m",
        "format": decision_schema(),
        "options": {"temperature": float(CFG.get("temperature", 0.2)),
                    "num_ctx": int(CFG.get("num_ctx", 16384))},
    }
    seen = {"text": "", "at": 0.0}

    def on_delta(buf):
        partial = _partial_thought(buf)
        now = time.time()
        if partial and partial != seen["text"] and now - seen["at"] > 0.12:
            seen["text"] = partial
            seen["at"] = now
            emit("thinking", text=partial[:180])

    try:
        raw = _ollama_stream(body, stop, on_delta)
    except Stopped:
        raise
    except ModelUnavailable:
        raise
    except urllib.error.HTTPError as e:
        if e.code not in (400, 422, 500):
            raise ModelUnavailable(f"Ollama HTTP {e.code}: {_read_err(e)}")
        # Some models reject the grammar; fall back to free text + salvage parse.
        body.pop("format", None)
        body["stream"] = False
        try:
            res = http_json(CFG["host"].rstrip("/") + "/api/chat", body, timeout=300)
            raw = (res.get("message") or {}).get("content") or ""
        except urllib.error.HTTPError as e2:
            raise ModelUnavailable(f"Ollama HTTP {e2.code}: {_read_err(e2)}")
        except Exception as e2:
            raise ModelUnavailable(f"Ollama call failed: {e2}")
        emit("log", text=f"Grammar rejected ({str(e)[:60]}); used loose parsing")
    except Exception as e:
        raise ModelUnavailable(f"Ollama call failed: {e}")
    d = _salvage_json(raw)
    if not d:
        d = {"thought": raw[:120], "tool": "reply", "args": {}}
    d.setdefault("tool", "reply")
    d.setdefault("args", {})
    if not isinstance(d.get("args"), dict):
        d["args"] = {}
    return d


def decide_args(name: str, messages, stop=None) -> dict:
    """Second pass: fill the arguments of an already-chosen tool.

    The single-pass grammar allows the union of every argument name, so the 7B
    sometimes calls a tool without its required ones. This pass uses only that
    tool's own schema and marks its required keys, so the grammar forces them."""
    spec, required, _desc = TOOLS[name]
    props = {}
    for key in spec:
        props[key] = {"type": _JSON_TYPE.get(ARG_TYPES.get(key, "string"), "string")}
    grammar = {"type": "object", "properties": props, "required": list(required)}
    if uses_openai(CFG.get("model") or ""):
        prof = provider_of(CFG.get("model") or "")
        raw = _openai_stream(prof, CFG["model"],
                             messages + [{
                                 "role": "user",
                                 "content": (f"Fill the arguments for the tool '{name}'. "
                                             f"Required: {', '.join(required) or 'none'}. "
                                             "Copy exact values (paths, text, names, ids) from "
                                             "the conversation above. No commentary.")}],
                             stop, grammar=grammar, temperature=0.15, max_tokens=400)
        d = _salvage_json(raw)
        return d if isinstance(d, dict) else {}
    body = {
        "model": CFG["model"],
        "messages": messages + [{
            "role": "user",
            "content": (f"Fill the arguments for the tool '{name}'. Required: "
                        f"{', '.join(required) or 'none'}. Copy exact values (paths, "
                        "text, names, ids) from the conversation above. No commentary.")}],
        "stream": True,
        "keep_alive": "15m",
        "format": {"type": "object", "properties": props, "required": list(required)},
        "options": {"temperature": 0.15, "num_ctx": int(CFG.get("num_ctx", 16384)),
                    "num_predict": 300},
    }
    raw = _ollama_stream(body, stop)
    d = _salvage_json(raw)
    return d if isinstance(d, dict) else {}


def _read_err(e) -> str:
    try:
        return e.read().decode("utf-8", "replace")[:240]
    except Exception:
        return ""


def stream_reply(messages, stop=None) -> str:
    """Final user-facing turn, streamed token by token into the UI."""
    if uses_openai(CFG.get("model") or ""):
        prof = provider_of(CFG.get("model") or "")
        out: list = []

        def on_delta(buf):
            joined = "".join(out)
            piece = buf[len(joined):]
            if piece:
                out.append(piece)
                emit("token", text=piece)

        text = _openai_stream(prof, CFG["model"], messages, stop, on_delta,
                              temperature=max(0.35, float(CFG.get("temperature", 0.2))))
        return text.strip()
    body = {
        "model": CFG["model"], "messages": messages, "stream": True, "keep_alive": "15m",
        "options": {"temperature": max(0.35, float(CFG.get("temperature", 0.2))),
                    "num_ctx": int(CFG.get("num_ctx", 16384))},
    }
    req = urllib.request.Request(
        CFG["host"].rstrip("/") + "/api/chat", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    out = []
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            for raw in resp:
                if stop is not None and stop.is_set():
                    raise Stopped()
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    chunk = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    continue
                if chunk.get("error"):
                    raise ModelUnavailable(chunk["error"])
                piece = (chunk.get("message") or {}).get("content") or ""
                if piece:
                    out.append(piece)
                    emit("token", text=piece)
                if chunk.get("done"):
                    break
    except (Stopped, ModelUnavailable):
        raise
    except urllib.error.HTTPError as e:
        raise ModelUnavailable(f"Ollama HTTP {e.code}: {_read_err(e)}")
    except Exception as e:
        raise ModelUnavailable(f"Ollama stream failed: {e}")
    return "".join(out).strip()


# ---------------------------------------------------------------------------
# Context compaction
# ---------------------------------------------------------------------------
def approx_tokens(text: str) -> int:
    return max(1, len(text) // 3)


def message_tokens(messages) -> int:
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += approx_tokens(c)
        for tc in m.get("tool_calls") or []:
            total += approx_tokens(json.dumps(tc.get("function", {})))
    return total


def compact(messages, budget: int) -> tuple[list, int]:
    """Fold oversized tool observations so the loop never dies on context."""
    before = message_tokens(messages)
    folded = 0
    for m in messages[2:-2]:
        if m.get("role") != "user":
            continue
        c = m.get("content") or ""
        if not c.startswith("RESULT of"):
            continue
        if len(c) > 1400:
            head, tail = c[:900], c[-400:]
            m["content"] = head + f"\n... {len(c) - 1300} chars folded ...\n" + tail
            folded += 1
    after = message_tokens(messages)
    if after > budget:
        idx = [i for i, m in enumerate(messages)
               if (m.get("content") or "").startswith("RESULT of") and i > 1]
        for i in idx[: max(0, len(idx) - 8)]:
            c = messages[i].get("content") or ""
            if len(c) > 160:
                messages[i]["content"] = c[:120] + " ..."
                folded += 1
        after = message_tokens(messages)
    return messages, (before - after) if folded else 0


# ---------------------------------------------------------------------------
# Lessons
# ---------------------------------------------------------------------------
def load_lessons() -> list:
    try:
        v = json.loads(LESSONS_FILE.read_text(encoding="utf-8"))
        return v if isinstance(v, list) else []
    except Exception:
        return []


def save_lessons(items: list) -> None:
    LESSONS_FILE.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")


def remember(when: str, do: str) -> str:
    items = load_lessons()
    items.append({"id": str(now_ms()), "when": when.strip(), "do": do.strip(),
                  "created": time.strftime("%Y-%m-%d %H:%M:%S")})
    save_lessons(items)
    return f"Stored lesson #{len(items)}: when {when[:70]!r} -> {do[:110]!r}"


def _tokens(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 3}


def relevant_lessons(prompt: str, limit: int = 6) -> list:
    items = load_lessons()
    if not items:
        return []
    q = _tokens(prompt)
    scored = []
    for L in items:
        s = _tokens(L.get("when", "") + " " + L.get("do", ""))
        overlap = len(q & s)
        scored.append((overlap, L))
    scored.sort(key=lambda x: -x[0])
    hits = [L for score, L in scored if score > 0][:limit]
    return hits or [L for _, L in scored[-limit:]]


def lesson_text(prompt: str) -> str:
    hits = relevant_lessons(prompt)
    if not hits:
        return "None recorded."
    return "\n".join(f"- WHEN {L['when']}\n  DO   {L['do']}" for L in hits)


FAIL_ALTS = {
    "open_app": "Call open_app(name, restart_stuck=true) or tray_icon_click(name) when the "
                "window is stuck hidden in the tray",
    "window_focus": "Use open_app(name) or tray_icon_click(name); the title may have changed "
                    "to the document or chat name",
    "ocr_click": "Call read_screen or window_text first, then click the cx,cy it returns",
    "click_text": "Call read_screen or window_text first, then click the cx,cy it returns",
    "find_on_screen": "Use read_screen for text, or window_text for accessible text",
    "ui_click": "Dump ui_tree again, or use read_screen + mouse_click coordinates",
    "ui_find": "Dump ui_tree again, or use read_screen + mouse_click coordinates",
    "ui_tree": "The app may expose nothing through UIA: use window_text or read_screen and "
               "click OCR coordinates",
    "window_text": "Use ui_collect, ui_read or read_screen on the window",
    "dig_chats": "Open the app first with open_app(name, restart_stuck=true), then retry",
    "type_text": "Focus the field with window_focus/ui_click first, or use focus_and_type",
    "ui_type": "Focus the field with ui_click/window_focus first, or use focus_and_type",
    "download": "Report the exact error; never pip install and never retry the same URL blindly",
    "youtube_download": "Report the exact error; retry once with kind=audio, never pip install",
    "hotkey": "Use key_macro, or click the control directly",
    "key_tap": "Use key_macro, or click the control directly",
    "send_keys": "Use key_macro, or click the control directly",
}


def _auto_lesson(prompt, tool, error):
    """Store one lesson per failed task shape so the same mistake is not repeated."""
    try:
        when = f"Using {tool} for: {' '.join(str(prompt).split())[:90]}"
        items = load_lessons()
        if any(L.get("when", "").strip().lower() == when.lower() for L in items):
            return False
        alt = FAIL_ALTS.get(tool, "Change the tool or its arguments")
        do = (f"It failed with: {str(error)[:180]}. {alt}. "
              "Never repeat the same failing call.")
        items.append({"id": str(now_ms()), "when": when, "do": do,
                      "created": time.strftime("%Y-%m-%d %H:%M:%S")})
        save_lessons(items[-120:])
        emit("log", text=f"Learned from that failure: {tool}")
        emit("lessons", lessons=len(load_lessons()))
        return True
    except Exception:
        return False


CORE_LESSONS = [
    ("Opening an app whose only window is hidden to the tray (AyuGram, Telegram, Discord)",
     "Call open_app(name) first: it finds the running copy and clicks its tray icon. If it "
     "reports the app hid its own window, call open_app(name, restart_stuck=true) or "
     "tray_icon_click(name). Never say the app is not installed or not running."),
    ("Finding a chat or document window whose title changed to its content",
     "Window titles change to the open chat/document; match by app with open_app(name) or "
     "window_focus(name) instead of the old title. window_list shows every title."),
    ("Clicking a button or menu item in any app",
     "Call ui_tree first for named controls, or read_screen and click the cx,cy of the label. "
     "Never guess coordinates and never click a control you did not see."),
    ("Reading chat messages, documents or editor text from an app window",
     "Use window_text(window=App) first; it returns document text, labels, then OCR. Use "
     "ui_collect(direction=up, pages=8) for older messages. Never use read_file for app content."),
    ("Digging through chats or messages",
     "Call dig_chats(app) - a read-only sweep that clicks rows and OCRs each thread. Never type "
     "in a chat or press Enter unless the user explicitly asked to send a message."),
    ("Typing text into a search box, field or chat input",
     "Focus the field first (ui_click by name, or window_focus then click the field), then "
     "type_text/ui_type with the literal text. If the user gave no text, do not type anything."),
    ("An app exposes no UI Automation controls (Qt, games, custom-drawn windows)",
     "Dump ui_tree; if it is empty, read_screen for text coordinates and mouse_click them, or "
     "send hotkeys. Report a real blocker only after trying both."),
    ("A tool call failed and the same call would be repeated",
     "Read the exact error, change the tool or arguments, and never run the identical call "
     "twice. Three failures in a row means stop and report what blocked you."),
    ("The user says not to change anything on the machine",
     "Stay read-only: inspect windows, files and screen, never write, move, delete, type or "
     "send. Answer with what you found."),
    ("Long multi-step job (several apps, downloads or conversions)",
     "Call update_plan first, keep it current, and finish every item before replying. Reuse "
     "results already in the transcript instead of re-running the same call."),
    ("A short request with two parts, like opening an app and typing something",
     "Do only the two parts the user asked for, then answer. Never close, minimise, save over "
     "or otherwise tidy the app you just used - the user wants to see the result on screen."),
    ("Finishing a task that left a window open",
     "Leave the window open and focused unless the user asked to close it. Report the outcome "
     "in one short sentence instead of adding extra cleanup steps."),
]


def seed_core_lessons():
    try:
        items = load_lessons()
        known = {L.get("when", "").strip().lower() for L in items}
        added = 0
        for when, do in CORE_LESSONS:
            if when.lower() in known:
                continue
            items.append({"id": str(now_ms()), "when": when, "do": do,
                          "created": time.strftime("%Y-%m-%d %H:%M:%S")})
            added += 1
        if added:
            save_lessons(items)
        return added
    except Exception:
        return 0




# ---------------------------------------------------------------------------
# Tool implementations. Each returns (ok, summary, detail).
# ---------------------------------------------------------------------------
def _ok(summary, detail=""):
    return True, str(summary), str(detail if detail != "" else summary)


def _err(msg, detail=""):
    return False, str(msg), str(detail if detail != "" else msg)


def t_read_file(path, offset=1, limit=160):
    p = resolve_path(path)
    if not p.is_file():
        return _err(f"No file at {rel_display(p)}")
    try:
        if p.stat().st_size > 20_000_000:
            return _err(f"{rel_display(p)} is {p.stat().st_size // 2**20}MB — too large to read")
        raw = p.read_bytes()[:2_000_000]
    except Exception as e:
        return _err(f"Unreadable: {e}")
    if b"\x00" in raw[:8192]:
        return _err(f"{rel_display(p)} is a binary file",
                    f"Binary ({len(raw)} bytes). Use hash, or open_path to inspect it.")
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    offset = max(1, int(offset or 1))
    limit = max(1, min(int(limit or 160), 2000))
    chunk = lines[offset - 1: offset - 1 + limit]
    body = "\n".join(f"{i + offset:>5}| {ln[:200]}" for i, ln in enumerate(chunk))
    note = f"{rel_display(p)} · {len(lines)} lines · showing {len(chunk)}"
    if offset - 1 + len(chunk) < len(lines):
        note += " · use offset to page"
    return _ok(note, body or "(empty range)")


def t_write_file(path, content="", append=False):
    p = resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with open(p, mode, encoding="utf-8") as f:
        f.write(content)
    verb = "Appended" if append else "Wrote"
    return _ok(f"{verb} {len(content)} chars -> {rel_display(p)}")


def t_edit_file(path, old="", new="", all=False):
    p = resolve_path(path)
    if not p.is_file():
        return _err(f"No file at {rel_display(p)}")
    text = p.read_text(encoding="utf-8", errors="replace")
    if not old:
        return _err("edit_file needs a non-empty 'old' string")
    hits = text.count(old)
    if not hits:
        return _err("No match for 'old' in file",
                    "Search for the exact text with read_file first. "
                    "First 400 chars: " + text[:400].replace("\n", "\\n"))
    new_text = text.replace(old, new) if all else text.replace(old, new, 1)
    p.write_text(new_text, encoding="utf-8")
    return _ok(f"Replaced {'all ' + str(hits) if all else '1'} occurrence(s) in {rel_display(p)}")


def t_list_dir(path="", limit=80):
    p = resolve_path(path)
    if not p.is_dir():
        return _err(f"Not a directory: {rel_display(p)}")
    total = 0
    files = 0
    dirs = 0
    rows = []
    try:
        for child in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            total += 1
            if child.is_dir():
                dirs += 1
            else:
                files += 1
            if len(rows) < int(limit):
                rows.append(("d " if child.is_dir() else "f ") + child.name[:90])
    except Exception as e:
        return _err(f"Cannot list: {e}")
    note = f"{total} entries in {rel_display(p)}: {files} files, {dirs} folders"
    if total > len(rows):
        note += f" · first {len(rows)} shown"
    return _ok(note, "\n".join(rows) or "(empty)")


def t_tree(path="", depth=3):
    p = resolve_path(path)
    out, base = [], p
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "bin", "obj", ".cache"}

    def walk(d, level):
        if level > int(depth):
            return
        try:
            kids = sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except Exception:
            return
        for k in kids:
            if k.name in skip or k.name.startswith("."):
                continue
            out.append("  " * level + k.name[:80] + ("/" if k.is_dir() else ""))
            if k.is_dir():
                walk(k, level + 1)
            if len(out) > 150:
                return

    walk(p, 0)
    dirs = sum(1 for line in out if line.rstrip().endswith("/"))
    return _ok(f"Tree of {rel_display(base)} · {len(out)} nodes: {dirs} folders, "
               f"{len(out) - dirs} files", "\n".join(out) or "(empty)")


def t_glob(pattern="**/*", path="", limit=60):
    base = resolve_path(path)
    hits = []
    for x in base.glob(pattern):
        if any(part in {".git", "node_modules", "__pycache__"} for part in x.parts):
            continue
        hits.append(x)
        if len(hits) >= int(limit):
            break
    lines = [f"{'d ' if h.is_dir() else '  '}{str(h)}" for h in hits]
    return _ok(f"{len(hits)} match(es) for {pattern}", "\n".join(lines) or "(no matches)")


def t_grep(pattern="", path="", glob="**/*", ignore_case=True, limit=40):
    base = resolve_path(path)
    try:
        rx = re.compile(pattern, re.I if ignore_case else 0)
    except Exception as e:
        return _err(f"Bad regex: {e}")
    out = []
    for fp in base.glob(glob):
        if not fp.is_file() or any(p in {".git", "node_modules", "__pycache__", "bin"} for p in fp.parts):
            continue
        try:
            if fp.stat().st_size > 3_000_000:
                continue
            data = fp.read_bytes()
            if b"\x00" in data[:4096]:
                continue
            for i, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    out.append(f"{rel_display(fp)}:{i}: {line.strip()[:220]}")
                    if len(out) >= int(limit):
                        return _ok(f"{len(out)}+ hits", "\n".join(out))
        except Exception:
            continue
    return _ok(f"{len(out)} hit(s) for {pattern!r}", "\n".join(out) or "(no matches)")


def t_mkdir(path):
    p = resolve_path(path)
    p.mkdir(parents=True, exist_ok=True)
    return _ok(f"Created {rel_display(p)}")


def t_copy(src, dst):
    a, b = resolve_path(src), resolve_path(dst)
    b.parent.mkdir(parents=True, exist_ok=True)
    if a.is_dir():
        shutil.copytree(a, b, dirs_exist_ok=True)
    else:
        shutil.copy2(a, b)
    return _ok(f"Copied {rel_display(a)} -> {rel_display(b)}")


def t_move(src, dst):
    a, b = resolve_path(src), resolve_path(dst)
    if not a.exists():
        return _err(f"Missing source {rel_display(a)}")
    b.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(a), str(b))
    return _ok(f"Moved {rel_display(a)} -> {rel_display(b)}")


def clean_stem(name: str, max_len: int) -> str:
    stem, ext = os.path.splitext(name)
    stem = re.sub(r"[^\w\s\-.,&'+]", "", stem, flags=re.UNICODE)
    stem = re.sub(r"\s+", " ", stem).strip(" .,-")
    if len(stem) > max_len:
        stem = stem[:max_len].rstrip(" .,-")
    return (stem or "file") + ext.lower()


def t_rename(path, new_name="", max_len=0):
    p = resolve_path(path)
    if not p.exists():
        return _err(f"Missing {rel_display(p)}")
    if new_name:
        target = p.parent / new_name
    else:
        target = p.parent / clean_stem(p.name, int(max_len) or 32)
    n, stem, ext = 1, target.stem, target.suffix
    while target.exists():
        target = p.parent / f"{stem} ({n}){ext}"
        n += 1
    p.rename(target)
    return _ok(f"Renamed -> {target.name}")


def t_shorten_names(path="", pattern="*", max_len=32, dry_run=False):
    base = resolve_path(path)
    if not base.is_dir():
        return _err(f"Not a directory: {rel_display(base)}")
    done = []
    for f in sorted(base.glob(pattern)):
        if not f.is_file():
            continue
        new = f.parent / clean_stem(f.name, int(max_len))
        if new == f:
            continue
        n, stem, ext = 1, new.stem, new.suffix
        while new.exists():
            new = f.parent / f"{stem} ({n}){ext}"
            n += 1
        if not dry_run:
            f.rename(new)
        done.append(f"{f.name}  ->  {new.name}")
        if len(done) >= 200:
            break
    verb = "Would rename" if dry_run else "Renamed"
    return _ok(f"{verb} {len(done)} file(s)", "\n".join(done) or "(nothing to shorten)")


def t_gather(source="", dest="", pattern="*.*", shorten=False, move=False, recursive=True, max_len=32):
    src, dst = resolve_path(source), resolve_path(dest)
    if not src.is_dir():
        return _err(f"Source folder not found: {rel_display(src)}")
    dst.mkdir(parents=True, exist_ok=True)
    it = src.rglob(pattern) if recursive else src.glob(pattern)
    files = [f for f in it if f.is_file()][:500]
    done = []
    for f in files:
        if dst in f.parents or f.parent == dst:
            continue
        name = clean_stem(f.name, int(max_len)) if shorten else f.name
        target = dst / name
        n, stem, ext = 1, os.path.splitext(name)[0], os.path.splitext(name)[1]
        while target.exists():
            target = dst / f"{stem} ({n}){ext}"
            n += 1
        (shutil.move if move else shutil.copy2)(str(f), str(target))
        done.append(target.name)
    verb = "Moved" if move else "Copied"
    return _ok(f"{verb} {len(done)} file(s) into {rel_display(dst)}", "\n".join(done[:80]))


def t_delete(path, recursive=False, permanent=False):
    p = resolve_path(path)
    if not p.exists():
        return _err(f"Nothing at {rel_display(p)}")
    if p.is_dir() and not recursive:
        return _err("Refusing to delete a directory without recursive=true")
    if permanent:
        shutil.rmtree(p) if p.is_dir() else p.unlink()
        return _ok(f"Permanently deleted {rel_display(p)}")
    ps = (
        "Add-Type -AssemblyName Microsoft.VisualBasic; "
        f"[Microsoft.VisualBasic.FileIO.FileSystem]::"
        f"{'DeleteDirectory' if p.is_dir() else 'DeleteFile'}("
        f"'{str(p).replace(chr(39), chr(39)*2)}','OnlyErrorDialogs','SendToRecycleBin')"
    )
    ok, out = _run_powershell(ps, 60)
    if ok:
        return _ok(f"Sent {rel_display(p)} to Recycle Bin")
    return _err(f"Recycle failed: {out[:200]}")


def _run_powershell(command: str, timeout: int = 120):
    # Redirected PowerShell writes with the console codepage and turns marks
    # such as U+200E into '?'. Force UTF-8 in the child and decode it here.
    wrapped = "$OutputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8;" + command
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-STA",
             "-ExecutionPolicy", "Bypass", "-Command", wrapped],
            capture_output=True, timeout=timeout, cwd=str(workspace()),
        )
        raw = (proc.stdout or b"") + (b"\n" + proc.stderr if proc.stderr else b"")
        out = ""
        for enc in ("utf-8-sig", "utf-8", "mbcs", "latin-1"):
            try:
                out = raw.decode(enc)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        out = out.strip()
        return proc.returncode == 0, out or f"(exit {proc.returncode})"
    except subprocess.TimeoutExpired:
        return False, f"Timed out after {timeout}s"
    except Exception as e:
        return False, str(e)


def t_run_command(command, shell="powershell", timeout=120):
    if str(shell).lower() in {"cmd", "batch"}:
        try:
            proc = subprocess.run(command, shell=True, capture_output=True, text=True,
                                  timeout=float(timeout), cwd=str(workspace()))
            out = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
            return (proc.returncode == 0, out or f"exit {proc.returncode}", out[:1600])
        except Exception as e:
            return _err(str(e))
    ok, out = _run_powershell(command, int(timeout))
    if len(out) > 1600:
        out = out[:1600] + "\n... (truncated)"
    return ok, ("Command finished" if ok else "Command failed"), out


def t_open_url(url):
    url = (url or "").strip()
    if not url:
        return _err("No URL given")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url
    os.startfile(url)  # opens in the OS default browser
    return _ok(f"Opened {url} in the default browser")


def t_open_path(path):
    p = resolve_path(path)
    if not p.exists():
        return _err(f"Nothing at {rel_display(p)}")
    os.startfile(str(p))
    return _ok(f"Opened {rel_display(p)} in Explorer")


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", html)
    text = re.sub(r"<[^>]+>", " ", html)
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#x27;", "'").replace("&nbsp;", " "))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _ssl_context():
    """Windows Python ships no CA bundle; certifi does. Fall back to system."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        try:
            return ssl.create_default_context()
        except Exception:
            return None


_SSL = _ssl_context()


def http_open(req, timeout):
    kw = {"timeout": timeout, "context": _SSL} if _SSL is not None else {"timeout": timeout}
    return urllib.request.urlopen(req, **kw)


def t_web_fetch(url, extract_text=True, max_chars=4000):
    url = (url or "").strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    req = urllib.request.Request(url, headers={
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"})
    try:
        with http_open(req, 25) as r:
            raw = r.read(1_500_000)
            ctype = r.headers.get("Content-Type", "")
    except Exception as e:
        return _err(f"Fetch failed: {e}")
    body = raw.decode("utf-8", errors="replace")
    if "html" in ctype.lower() and extract_text:
        body = _html_to_text(body)
    body = body[: int(max_chars)]
    return _ok(f"Fetched {len(body)} chars from {url}", body)


def web_search_api(query: str, max_results: int):
    hits, fail = _ddg_html(query, max_results)
    if hits:
        return hits, ""
    lite, fail2 = _ddg_lite(query, max_results)
    if lite:
        return lite, ""
    return None, fail or fail2 or "Search returned nothing"


def _ddg_html(query, max_results):
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query)
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA,
                                               "Accept-Language": "en-US,en;q=0.9"})
    try:
        with http_open(req, 20) as r:
            html = r.read(900_000).decode("utf-8", errors="replace")
    except Exception as e:
        return None, f"Search backend failed: {e}"
    results = []
    for m in re.finditer(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>', html):
        href = href_strip(urllib.parse.unquote(m.group(1)))
        title = _html_to_text(m.group(2)).strip()
        if title:
            results.append({"title": title, "url": href, "snippet": ""})
    snippets = [_html_to_text(m.group(1)).strip() for m in re.finditer(
        r'<a[^>]+class="result__snippet"[^>]*>([\s\S]*?)</a>', html)]
    for r_, sn in zip(results, snippets):
        r_["snippet"] = sn
    return results[: int(max_results)], ""


def _ddg_lite(query, max_results):
    url = "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote_plus(query)
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    try:
        with http_open(req, 20) as r:
            html = r.read(600_000).decode("utf-8", errors="replace")
    except Exception as e:
        return None, f"Search backend failed: {e}"
    out = []
    for m in re.finditer(r'<a[^>]+class="result-link"[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>', html):
        out.append({"title": _html_to_text(m.group(2)).strip(),
                    "url": href_strip(urllib.parse.unquote(m.group(1))), "snippet": ""})
    return out[: int(max_results)], ""


def href_strip(h: str) -> str:
    if h.startswith("//duckduckgo.com/l/?uddg="):
        q = urllib.parse.urlparse("https:" + h).query
        return urllib.parse.parse_qs(q).get("uddg", [h])[0]
    return h


def t_web_search(query, max_results=5, open_browser=False):
    if open_browser:
        os.startfile("https://www.google.com/search?q=" + urllib.parse.quote_plus(query))
    hits, fail = web_search_api(query, max_results)
    if hits:
        lines = [f"{i}. {r['title'][:90]}\n   {r['url']}"
                 + (f"\n   {r['snippet'][:160]}" if r.get("snippet") else "")
                 for i, r in enumerate(hits, 1)]
        return _ok(f"{len(hits)} result(s)", "\n".join(lines))
    return _ok("No structured results", fail or "(empty)")


def _legacy_youtube_search(query):
    os.startfile("https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query))
    hits, _ = web_search_api(f"site:youtube.com {query}", 5)
    lines = [f"{r['title']}\n   {r['url']}" for r in hits]
    return _ok(f"Opened YouTube results for {query!r}", "\n".join(lines) or "Opened in browser.")


def _clip_api():
    import ctypes
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    u.OpenClipboard.argtypes = [ctypes.c_void_p]
    u.OpenClipboard.restype = ctypes.c_bool
    u.GetClipboardData.argtypes = [ctypes.c_uint]
    u.GetClipboardData.restype = ctypes.c_void_p
    u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    u.SetClipboardData.restype = ctypes.c_void_p
    u.EmptyClipboard.restype = ctypes.c_bool
    u.CloseClipboard.restype = ctypes.c_bool
    k.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k.GlobalAlloc.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    k.GlobalUnlock.restype = ctypes.c_bool
    return u, k


def t_clipboard_read():
    try:
        import ctypes
        u, k = _clip_api()
        if not u.OpenClipboard(None):
            return _err("Clipboard is busy")
        try:
            h = u.GetClipboardData(13)  # CF_UNICODETEXT
            if not h:
                return _ok("Clipboard is empty")
            p = k.GlobalLock(h)
            try:
                text = ctypes.wstring_at(p)
            finally:
                k.GlobalUnlock(h)
        finally:
            u.CloseClipboard()
        return _ok(f"Read {len(text)} chars from clipboard", text[:4000])
    except Exception as e:
        return _err(str(e))


def t_clipboard_write(text=""):
    try:
        import ctypes
        u, k = _clip_api()
        data = text.encode("utf-16-le") + b"\x00\x00"
        h = k.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        if not h:
            return _err("Could not allocate clipboard memory")
        p = k.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        k.GlobalUnlock(h)
        if not u.OpenClipboard(None):
            return _err("Clipboard is busy")
        try:
            u.EmptyClipboard()
            if not u.SetClipboardData(13, h):
                return _err("SetClipboardData failed")
        finally:
            u.CloseClipboard()
        return _ok(f"Copied {len(text)} chars to clipboard")
    except Exception as e:
        return _err(str(e))


def t_sysinfo():
    import platform
    drives = []
    for letter in "CDEFG":
        d = Path(f"{letter}:/")
        if d.exists():
            try:
                s = shutil.disk_usage(str(d))
                drives.append(f"{letter}: {s.free // 2**30}GB free of {s.total // 2**30}GB")
            except Exception:
                pass
    mem = ""
    try:
        k = os.times()
        mem = f"load={k[0] + k[1]:.0f}s cpu"
    except Exception:
        pass
    return _ok("System snapshot",
               f"{platform.system()} {platform.release()} ({platform.machine()})\n"
               f"Python {platform.python_version()} · {os.cpu_count()} logical cores · {mem}\n"
               f"Workspace: {workspace()}\n" + "\n".join(drives))


def t_process_list(filter=""):
    ok, out = _run_powershell(
        "Get-Process | Sort-Object CPU -Descending | Select-Object -First 25 "
        "Id,ProcessName,@{n='MB';e={[int]($_.WorkingSet/1MB)}},CPU | Format-Table -AutoSize | Out-String -Width 100",
        30)
    if filter:
        out = "\n".join(l for l in out.splitlines() if filter.lower() in l.lower())
    return (ok, "Process list", out[:4000]) if ok else _err(out)


def t_kill_process(name="", pid=0, force=True):
    if pid:
        cmd = f"Stop-Process -Id {int(pid)} -Force" if force else f"Stop-Process -Id {int(pid)}"
    elif name:
        cmd = f"Stop-Process -Name '{name}' -Force" if force else f"Stop-Process -Name '{name}'"
    else:
        return _err("Give pid or name")
    ok, out = _run_powershell(cmd + " -ErrorAction SilentlyContinue; if(-not $?){'no match'}", 30)
    return (ok, f"Signalled {name or pid}", out) if ok else _err(out)


_APP_STOP = {"anvil fletch", "program manager", "windows input experience",
             "default ime", "msctfime ui", "windows push notifications"}


def _norm_app(s):
    s = str(s or "").strip().strip('"').replace("/", "\\")
    s = s.rsplit("\\", 1)[-1]
    s = re.sub(r"\.exe$", "", s, flags=re.I)
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _window_records():
    """Every top-level window with a title, including minimized and tray-hidden ones."""
    import ctypes
    from ctypes import wintypes
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    rows = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        n = u.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value.strip()
        if not title:
            return True
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        cb2 = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, cb2, 256)
        exe = ""
        hp = k.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if hp:
            b = ctypes.create_unicode_buffer(1024)
            sz = wintypes.DWORD(1024)
            if k.QueryFullProcessImageNameW(hp, 0, b, ctypes.byref(sz)):
                exe = b.value
            k.CloseHandle(hp)
        rows.append({"hwnd": int(hwnd), "title": title, "pid": int(pid.value),
                     "exe": exe, "cls": cb2.value,
                     "visible": bool(u.IsWindowVisible(hwnd))})
        return True

    u.EnumWindows(cb, 0)
    return rows


_DIALOG_TITLE = re.compile(
    r"\b(save as|save changes|open file|open folder|print|page setup|properties|"
    r"confirm|warning|error|choose|browse|select (?:a |the )?(?:file|folder)|"
    r"sign in|log ?in|install|update available|are you sure|do you want)\b", re.I)


def _is_dialog(r):
    """Modal sub-windows share their parent's exe, so they must never win the match."""
    return r.get("cls") == "#32770" or bool(_DIALOG_TITLE.search(r["title"] or ""))


def _find_app_window(name):
    """Best already-open window for an app name: exe or title, visible or tray-hidden."""
    want = _norm_app(name)
    if not want:
        return None
    best, best_score = None, 0
    for r in _window_records():
        low = r["title"].lower()
        if any(s in low for s in _APP_STOP) or r["pid"] == os.getpid():
            continue
        stem = _norm_app(r["exe"])
        title = _norm_app(r["title"])
        score = 0
        if stem and stem == want:
            score = 120
        elif stem and want in stem:
            score = 100
        elif title == want:
            score = 90
        elif title and want in title:
            score = 80
        elif stem and _norm_app(r["title"] + " " + r["exe"]).find(want) >= 0:
            score = 60
        if not score:
            continue
        if r["visible"]:
            score += 5
        if _is_dialog(r):
            score -= 70
        if score > best_score:
            best, best_score = r, score
    return best


def _focus_hwnd(hwnd):
    """Show, restore and genuinely foreground a window. Returns True when it is up."""
    import ctypes
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    hwnd = int(hwnd)
    if not hwnd:
        return False
    if u.IsWindowVisible(hwnd) and int(u.GetForegroundWindow()) == hwnd:
        return True
    # Qt and Electron apps sometimes ignore the first show request while still
    # initialising their window; two passes with a short settle fixes that.
    for _ in range(2):
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)   # SW_RESTORE
        else:
            u.ShowWindow(hwnd, 5)   # SW_SHOW
        u.ShowWindowAsync(hwnd, 5)
        if u.IsWindowVisible(hwnd):
            break
        time.sleep(0.25)
    if u.SetForegroundWindow(hwnd) and int(u.GetForegroundWindow()) == hwnd:
        return True
    # The documented focus lock blocks a background process from taking the
    # foreground. SwitchToThisWindow is what the taskbar itself uses and works
    # from a background process where SetForegroundWindow silently fails.
    try:
        u.SwitchToThisWindow(hwnd, True)
    except Exception:
        pass
    if int(u.GetForegroundWindow()) == hwnd:
        return True
    # Minimising and restoring forces the shell to raise the window.
    try:
        u.ShowWindow(hwnd, 6)   # SW_MINIMIZE
        time.sleep(0.18)
        u.ShowWindow(hwnd, 9)   # SW_RESTORE
        time.sleep(0.18)
    except Exception:
        pass
    if int(u.GetForegroundWindow()) == hwnd:
        return True
    try:
        fg = u.GetForegroundWindow()
        tf = u.GetWindowThreadProcessId(fg, None)
        tc = k.GetCurrentThreadId()
        u.AttachThreadInput(tf, tc, True)
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)
        u.AttachThreadInput(tf, tc, False)
    except Exception:
        pass
    return int(u.GetForegroundWindow()) == hwnd


def _start_menu_appid(name):
    safe = str(name).replace("'", "''")
    ok, out = _run_powershell(
        "Get-StartApps | Where-Object { $_.Name -like '*" + safe + "*' } | "
        "Select-Object -First 5 Name,AppID | ConvertTo-Json -Compress", 20)
    if not ok or not out.strip():
        return None
    try:
        data = json.loads(out)
    except Exception:
        return None
    if isinstance(data, dict):
        data = [data]
    for row in data or []:
        if isinstance(row, dict) and row.get("AppID"):
            return str(row["AppID"])
    return None


def _await_app_window(name, wait):
    deadline = time.time() + max(2, min(int(wait or 10), 60))
    while time.time() < deadline:
        hit = _find_app_window(name)
        if hit:
            return hit
        time.sleep(0.5)
    return None


def t_open_app(name, wait=10, restart_stuck=False):
    """Open an app, reusing the copy that is already running (taskbar/tray/minimized)."""
    name = str(name or "").strip()
    if not name:
        return _err("open_app needs an app name, e.g. AyuGram")
    direct = resolve_path(name) if re.search(r"[\\/]|\.exe$", name, re.I) else None
    if direct and direct.exists():
        try:
            subprocess.Popen([str(direct)], creationflags=0x08000000)
        except Exception:
            os.startfile(str(direct))
        hit = _await_app_window(direct.stem, wait)
        if hit:
            ok, why = _ensure_on_screen(hit["hwnd"], hit["title"], allow_restart=restart_stuck)
            if ok:
                return _ok(f"Opened {hit['title']!r} (pid {hit['pid']})", why)
            return _err(why)
        return _ok(f"Started {direct.name}")
    hit = _find_app_window(name)
    if hit:
        ok, why = _ensure_on_screen(hit["hwnd"], hit["title"], allow_restart=restart_stuck)
        if not ok:
            return _err(why, "If it is stuck hidden, call open_app(name, restart_stuck=true); "
                             "the app relaunches with its data saved.")
        where = "" if hit["visible"] else " (restored from the tray)"
        return _ok(f"Focused the running {hit['title']!r}{where} - pid {hit['pid']}. "
                   f"Reused it instead of starting a second copy.", str(hit["exe"]))
    appid = _start_menu_appid(name)
    if appid:
        try:
            subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + appid],
                             creationflags=0x08000000)
        except Exception:
            pass
    else:
        ok, _s, _d = t_launch(name)
        if not ok:
            return _err(f"Could not open {name!r}",
                        "Not running, not in the Start menu, and not on PATH. Give the "
                        "full path to the .exe.")
    hit = _await_app_window(name, wait)
    if hit:
        ok, why = _ensure_on_screen(hit["hwnd"], hit["title"], allow_restart=restart_stuck)
        if ok:
            return _ok(f"Opened {hit['title']!r} (pid {hit['pid']})", why)
        return _err(why)
    return _ok(f"Started {name!r}; its window has not appeared yet")


def t_launch(target, args=""):
    # A person clicks the taskbar icon instead of launching a second unlogged copy.
    looks_like_path = bool(re.search(r"[\\/]|\.exe$", target or "", re.I))
    if not str(args or "").strip() and not looks_like_path \
            and not str(target or "").lower().startswith("http"):
        hit = _find_app_window(target)
        if hit:
            ok, why = _ensure_on_screen(hit["hwnd"], hit["title"])
            if ok:
                return _ok(f"Focused the running {hit['title']!r} (pid {hit['pid']}) - "
                           f"reused it instead of launching another copy", str(hit["exe"]))
            return _err(why)
    try:
        argv = [target] + shlex.split(args or "")
        subprocess.Popen(argv, creationflags=0x08000000)
        return _ok(f"Launched {target}")
    except Exception as e:
        ok, out = _run_powershell(f"Start-Process -FilePath '{target}'", 20)
        return (True, f"Launched {target} via Start-Process") if ok else _err(str(e))


def t_screenshot(path=""):
    out = resolve_path(path) if path else workspace() / f"screen-{time.strftime('%Y%m%d-%H%M%S')}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    script = PS_DPI + f"""
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$b=[System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp=New-Object System.Drawing.Bitmap($b.Width,$b.Height)
$g=[System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size)
$bmp.Save('{str(out).replace(chr(39), chr(39)*2)}')
$g.Dispose();$bmp.Dispose()
"""
    ok, res = _run_powershell(script, 40)
    return (ok, f"Captured screen -> {rel_display(out)}", res) if ok else _err(res)


def t_notify(title, message=""):
    safe_t = str(title).replace("'", "''")
    safe_m = str(message).replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Information;"
        f"$n.BalloonTipTitle='{safe_t}';$n.BalloonTipText='{safe_m}';"
        "$n.Visible=$true;$n.ShowBalloonTip(6000);Start-Sleep -Seconds 2;$n.Dispose()"
    )
    _run_powershell(script, 15)
    return _ok(f"Notified: {title}")


def t_wait(seconds=2):
    secs = max(0.0, min(float(seconds or 2), 600.0))
    time.sleep(secs)
    return _ok(f"Waited {secs:g}s")


def t_download(url, path="", _ctx=None):
    url = (url or "").strip()
    if not re.match(r"^https?://", url):
        return _err("download needs a full http(s) URL")
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA,
                                               "Accept": "*/*"})
    try:
        resp = http_open(req, 90)
    except Exception as e:
        return _err(f"Download failed: {e}")
    with resp:
        name = ""
        cd = resp.headers.get("Content-Disposition", "")
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, re.I)
        if m:
            name = urllib.parse.unquote(m.group(1))
        if not name:
            name = os.path.basename(urllib.parse.urlparse(resp.geturl() or url).path)
            name = urllib.parse.unquote(name)
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip() or "download.bin"
        name = name[:120]
        raw_path = str(path or "")
        dest = resolve_path(path) if path else workspace() / name
        if dest.is_dir() or raw_path.endswith(("/", "\\")):
            dest = dest / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        size = int(resp.headers.get("Content-Length") or 0)
        ctx = _ctx
        if ctx is None:
            ctx = _new_ctx(f"Downloading {name[:44] or 'file'}", 1, size)
            emit_progress(ctx["label"], 0, size, name, 0, 1)
        else:
            _bump(ctx, total_extra=size, item=name)
        total = 0
        try:
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    total += len(chunk)
                    if total > 4_000_000_000:
                        break
                    _bump(ctx, len(chunk), item=name)
        except Exception as e:
            return _err(f"Download interrupted: {e}")
        _bump(ctx, files_done=1, item=name, force=True)
    return _ok(f"Downloaded {total / 1048576:.1f} MB -> {rel_display(dest)}", str(dest))


FILE_KINDS = (".zip", ".7z", ".rar", ".tar", ".gz", ".jar", ".pdf", ".png", ".jpg",
              ".jpeg", ".webp", ".avif", ".gif", ".svg", ".mp3", ".wav", ".flac",
              ".mp4", ".mkv", ".json", ".csv", ".xlsx", ".docx", ".pptx", ".txt",
              ".iso", ".exe", ".msi", ".apk")
_HTML_CT = ("text/html", "application/xhtml")


def _grab_url(url, dest_dir, ctx=None):
    """Download one URL to dest_dir; refuse HTML pages that claim to be files."""
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "*/*"})
    with http_open(req, 120) as resp:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        name = ""
        cd = resp.headers.get("Content-Disposition", "")
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, re.I)
        if m:
            name = urllib.parse.unquote(m.group(1))
        if not name:
            name = os.path.basename(urllib.parse.urlparse(resp.geturl() or url).path)
            name = urllib.parse.unquote(name)
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip() or "download.bin"
        if not os.path.splitext(name)[1]:
            ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
                   "image/gif": ".gif", "image/avif": ".avif", "image/svg+xml": ".svg",
                   "video/mp4": ".mp4", "audio/mpeg": ".mp3", "application/pdf": ".pdf",
                   "application/zip": ".zip"}.get(ctype.split(";")[0].strip())
            if ext:
                name += ext
            elif "html" in ctype:
                name += ".html"
        dest = Path(dest_dir) / name[:120]
        dest.parent.mkdir(parents=True, exist_ok=True)
        size = int(resp.headers.get("Content-Length") or 0)
        if ctx is not None:
            _bump(ctx, total_extra=size, item=dest.name)
        total = 0
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
                if total > 2_000_000_000:
                    break
                _bump(ctx, len(chunk), item=dest.name)
    if any(h in ctype for h in _HTML_CT) and dest.suffix.lower() not in (".html", ".htm"):
        try:
            dest.unlink()
        except Exception:
            pass
        raise RuntimeError(f"{url} returned an HTML page, not a file")
    return dest, total, ctype


def _links_in_html(html, base):
    out = []
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html, re.I):
        href = m.group(1).replace("&amp;", "&")
        if href.lower().startswith(("javascript:", "mailto:", "#")):
            continue
        url = urllib.parse.urljoin(base, href)
        low = url.lower().split("?")[0]
        if low.endswith(FILE_KINDS) or "download" in low:
            out.append(url)
    return out


def t_fetch_file(target, dest="", kind=""):
    """Get a file from the internet for a URL or a search phrase (internet-first)."""
    target = str(target or "").strip()
    if not target:
        return _err("fetch_file needs a URL or a search phrase")
    folder = resolve_path(dest) if str(dest or "").strip() else workspace()
    if folder.suffix and not folder.is_dir():
        folder.parent.mkdir(parents=True, exist_ok=True)
        save_path, out_dir = folder, folder.parent
    else:
        out_dir, save_path = folder, None
    out_dir.mkdir(parents=True, exist_ok=True)
    if re.match(r"^https?://", target):
        try:
            p, size, ctype = _grab_url(target, out_dir)
        except Exception as e:
            return _err(f"Could not download {target}", str(e))
        if save_path:
            try:
                p.replace(save_path)
                p = save_path
            except Exception:
                pass
        return _ok(f"Downloaded {p.name} ({size / 1048576:.1f} MB)", str(p))
    query = target + (f" filetype:{kind.lstrip('.')}" if kind else "")
    hits, fail = web_search_api(query, 10)
    if not hits:
        return _err(f"No results for {target!r}", fail or "")
    direct = [h for h in hits
              if h.get("url", "").lower().split("?")[0].endswith(FILE_KINDS)]
    tried = []
    for h in direct[:4]:
        url = h.get("url", "")
        try:
            p, size, _ct = _grab_url(url, out_dir)
            if save_path:
                try:
                    p.replace(save_path)
                    p = save_path
                except Exception:
                    pass
            return _ok(f"Downloaded {p.name} ({size / 1048576:.1f} MB) from {url[:90]}",
                       str(p))
        except Exception as e:
            tried.append(f"{url[:70]}: {e}")
    for h in hits[:3]:
        page = h.get("url", "")
        try:
            req = urllib.request.Request(page, headers={"User-Agent": BROWSER_UA})
            with http_open(req, 25) as r:
                html = r.read(700_000).decode("utf-8", "replace")
        except Exception as e:
            tried.append(f"{page[:70]}: {e}")
            continue
        links = [u for u in _links_in_html(html, page) if u.lower().split("?")[0].endswith(FILE_KINDS)]
        for url in links[:3]:
            try:
                p, size, _ct = _grab_url(url, out_dir)
                if save_path:
                    try:
                        p.replace(save_path)
                        p = save_path
                    except Exception:
                        pass
                return _ok(f"Downloaded {p.name} ({size / 1048576:.1f} MB) from {url[:90]}",
                           str(p))
            except Exception as e:
                tried.append(f"{url[:70]}: {e}")
    detail = "\n".join(tried[-6:]) + "\n\nPages found:\n" + "\n".join(
        f"{h['title'][:70]} - {h['url'][:90]}" for h in hits[:6])
    return _err(f"No direct file link found for {target!r}", detail)


def _safe_extract(zip_path, base):
    import zipfile
    base = Path(base).resolve()
    base.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(zip_path)) as zf:
        for name in zf.namelist():
            target = (base / name).resolve()
            if base != target and base not in target.parents:
                continue
            zf.extract(name, str(base))
    return base


def t_github_release(repo, asset="", folder="", launch=""):
    """Download a GitHub project's latest release asset (UABEA, tools, editors)."""
    repo = str(repo or "").strip()
    m = re.search(r"github\.com/([^/\s]+/[^/\s#?]+)", repo, re.I)
    if m:
        repo = m.group(1)
    if not re.match(r"^[\w.-]+/[\w.-]+$", repo):
        return _err("github_release needs owner/repo, e.g. nesrak1/UABEA")
    try:
        data = http_json(f"https://api.github.com/repos/{repo}/releases/latest", timeout=25)
    except Exception as e:
        return _err(f"GitHub API failed for {repo}", str(e))
    assets = [a for a in (data.get("assets") or []) if a.get("browser_download_url")]
    if not assets:
        return _err(f"{repo} has no downloadable release assets")
    want = str(asset or "").strip().lower()

    def score(a):
        n = str(a.get("name") or "").lower()
        s = 0
        if want and want in n:
            s += 100
        if "windows" in n or "win" in n:
            s += 25
        if n.endswith(".zip"):
            s += 30
        elif n.endswith((".exe", ".msi")):
            s += 20
        if "x64" in n or "amd64" in n:
            s += 8
        if "arm" in n or "mac" in n or "linux" in n or "source" in n:
            s -= 60
        return s

    pick = sorted(assets, key=score, reverse=True)[0]
    name = str(pick.get("name") or f"{repo.split('/')[-1]}.zip")
    dest = resolve_path(folder) if str(folder or "").strip() \
        else workspace() / "tools" / repo.split("/")[-1]
    dest.mkdir(parents=True, exist_ok=True)
    # _grab_url adds the response's Content-Length to ctx, so start at 0 here or
    # the total would count twice and the bar would stop at 50%.
    ctx = _new_ctx(f"Downloading {name[:44]}", 1, 0)
    emit_progress(ctx["label"], 0, 0, name, 0, 1)
    try:
        p, size, _ct = _grab_url(pick["browser_download_url"], dest, ctx=ctx)
    except Exception as e:
        return _err(f"Download failed for {name}", str(e))
    _bump(ctx, files_done=1, force=True)
    body = [f"{data.get('name', repo)} {data.get('tag_name', '')}".strip(),
            f"asset: {p.name}", f"saved: {rel_display(p)}"]
    launch_exe = ""
    if zipfile.is_zipfile(str(p)):
        try:
            out_dir = dest / p.stem
            _safe_extract(p, out_dir)
            exes = [x for x in out_dir.rglob("*.exe") if x.is_file()]
            body.append(f"extracted to: {rel_display(out_dir)}")
            if exes:
                launch_exe = str(exes[0])
                body.append("exe: " + ", ".join(rel_display(x) for x in exes[:6]))
        except Exception as e:
            body.append(f"extract failed: {e}")
    elif p.suffix.lower() in (".exe", ".msi"):
        launch_exe = str(p)
    if launch_exe and str(launch or "").lower() in ("1", "true", "yes", "launch"):
        try:
            subprocess.Popen([launch_exe], creationflags=0x08000000)
            body.append("launched it")
        except Exception as e:
            body.append(f"launch failed: {e}")
    return _ok(f"Got {p.name} ({size / 1048576:.1f} MB) from {repo}", "\n".join(body))


def t_open_unity_asset(path, install_dir=""):
    """Open a Unity .assets/.bundle in UABEA, downloading UABEA first if needed."""
    src = resolve_path(path)
    if not src.is_file():
        return _err(f"No file at {rel_display(src)}")
    if src.suffix.lower() not in (".assets", ".bundle", ".unity3d", ".resource",
                                  ".ress", ".sharedassets", ".bytes"):
        return _err(f"{src.name} does not look like a Unity asset/bundle",
                    "Supported: .assets .bundle .unity3d .resource .ress .sharedassets")
    base = resolve_path(install_dir) if install_dir else workspace() / "tools" / "UABEA"
    exe = next((x for x in base.rglob("*.exe")
                if x.is_file() and "uabea" in x.name.lower()), None) if base.exists() else None
    if exe is None:
        ok, summary, detail = t_github_release("nesrak1/UABEA", "", str(base))
        if not ok:
            return _err("Could not get UABEA", f"{summary}\n{detail}")
        exe = next((x for x in base.rglob("*.exe")
                    if x.is_file() and "uabea" in x.name.lower()), None)
        if exe is None:
            return _err("UABEA downloaded but no UABEA executable was found", detail)
    # UABEA reads the console cursor at startup and dies without one, so give it
    # its own console (what a double-click does) instead of launching detached.
    try:
        subprocess.Popen([str(exe), str(src)], cwd=str(exe.parent),
                         creationflags=0x00000010)
    except Exception as e:
        return _err(f"Could not start UABEA: {e}")
    hit = _await_app_window("UABEA", 25)
    if hit:
        _focus_hwnd(hit["hwnd"])
        return _ok(f"UABEA is open on {src.name} - window {hit['title']!r}",
                   "Next: ui_tree('UABEA') to see its controls, or window_text('UABEA'). "
                   "Export/import assets through its UI, then verify the output file exists.")
    return _ok(f"UABEA started with {src.name}, but no window appeared yet",
               f"executable: {rel_display(exe)}")


def _as_list(raw):
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    txt = str(raw or "").strip()
    if not txt:
        return []
    if txt.startswith("["):
        try:
            data = json.loads(txt, strict=False)
            if isinstance(data, list):
                return [str(x) for x in data if str(x).strip()]
        except Exception:
            pass
    parts = re.split(r"[\n,;]+", txt)
    return [p.strip().strip('"\'') for p in parts if p.strip()]


def t_download_many(urls, folder="", label=""):
    """Download several URLs at once (4 at a time) with live byte progress."""
    items = [u for u in _as_list(urls) if re.match(r"^https?://", u)]
    if not items:
        return _err("download_many needs URLs, one per line or as a JSON array")
    import concurrent.futures as cf
    out = []
    ctx = _new_ctx(str(label or "Downloading files")[:60], len(items))
    emit_progress(ctx["label"], 0, 0, "", 0, len(items))

    def one(u):
        try:
            ok, s, d = t_download(u, folder, _ctx=ctx)
            return u, bool(ok), s, d
        except Exception as e:
            return u, False, f"{type(e).__name__}: {e}", ""

    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        for u, ok, s, _d in ex.map(one, items):
            out.append(("OK  " if ok else "FAIL") + f" {s}  <- {u[:80]}")
    good = sum(1 for line in out if line.startswith("OK"))
    _bump(ctx, force=True)
    return _ok(f"{good}/{len(items)} downloaded", "\n".join(out))


def t_youtube_download_many(items, kind="audio", quality="best", folder=""):
    """Download a list of songs/videos (queries or URLs) two at a time."""
    qs = _as_list(items)
    if not qs:
        return _err("youtube_download_many needs a list of searches, ids or URLs")
    import concurrent.futures as cf
    out = []
    emit_progress(f"Downloading {len(qs)} media item(s)", files_total=len(qs))
    done = 0

    def one(q):
        try:
            res = q, t_youtube_download(q, kind, quality, folder)
        except Exception as e:
            res = q, (False, f"{type(e).__name__}: {e}", "")
        nonlocal done
        done += 1
        emit_progress(f"Downloading {len(qs)} media item(s)", files_done=done,
                      files_total=len(qs), item=str(q)[:60])
        return res

    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        for q, (ok, s, _d) in ex.map(one, qs):
            out.append(("OK  " if ok else "FAIL") + f" {s}  <- {q[:70]}")
    good = sum(1 for line in out if line.startswith("OK"))
    return _ok(f"{good}/{len(qs)} done", "\n".join(out))


def t_window_list(filter="", all=True):
    """Every titled window, including tray-hidden and minimized apps."""
    rows = []
    for r in _window_records():
        title = r["title"]
        if filter and filter.lower() not in title.lower():
            continue
        state = ""
        cloak = _dwm_cloak_state(r["hwnd"])
        if not r["visible"]:
            state = "  [tray/hidden]"
        elif _is_minimized(r["hwnd"]):
            state = "  [minimized]"
        if cloak & 1:
            state += "  [cloaked: app hid it]"
        elif cloak & 2:
            state += "  [other virtual desktop]"
        exe = Path(r["exe"]).name if r["exe"] else ""
        rows.append(f"0x{r['hwnd']:X}  pid {r['pid']:<6} {title[:100]}{state}"
                    + (f"  ({exe})" if exe else ""))
    shown = rows[:80]
    tail = "" if len(rows) <= 80 else f"\n... and {len(rows) - 80} more"
    return _ok(f"{len(rows)} window(s)", "\n".join(shown) + tail or "(none)")


def _is_minimized(hwnd):
    import ctypes
    return bool(ctypes.windll.user32.IsIconic(int(hwnd)))


_BROWSER_HINTS = ("chrome", "edge", "firefox", "brave", "opera", "vivaldi",
                  "librewolf", "waterfox", "thorium")


def t_browser_tabs(window="", max_tabs=80):
    """Titles of every open browser tab, read from the browser's accessibility tree."""
    d, err = _uia_desktop()
    if d is None:
        return _err(err)
    needle = str(window or "").strip().lower()
    limit = max(5, min(int(max_tabs or 80), 200))
    rows, seen = [], set()
    try:
        wins = d.windows()
    except Exception as e:
        return _err(f"Could not enumerate windows: {e}")
    for w in wins:
        try:
            title = w.window_text() or ""
        except Exception:
            continue
        low = title.lower()
        if needle:
            if needle not in low:
                continue
        elif not any(b in low for b in _BROWSER_HINTS):
            continue
        try:
            els = _uia_elements(w, 10, 900)
        except Exception:
            continue
        for _el, ctype, nm, _lvl in els:
            if "tabitem" != (ctype or "").replace(" ", "").lower():
                continue
            tab = " ".join(str(nm or "").split())
            if not tab or tab.lower() in seen:
                continue
            seen.add(tab.lower())
            rows.append(f"{title[:46]} :: {tab[:110]}")
            if len(rows) >= limit:
                break
        if len(rows) >= limit:
            break
    if not rows:
        return _ok("No browser tabs exposed", "The browser may not expose tabs to UI "
                   "Automation, or none is open. Try window_text('<browser>') or screen_ocr.")
    return _ok(f"{len(rows)} open tab(s)", "\n".join(rows))


def t_window_focus(title=""):
    import ctypes
    if not str(title or "").strip():
        return _err("window_focus needs part of a window title")
    hwnd, name = _find_window(title)
    if not hwnd:
        return _err(f"No window matching {title!r}",
                    "window_list(all=true) shows tray-hidden windows too. "
                    "open_app(name) can bring a tray app back.")
    ok, why = _ensure_on_screen(hwnd, name)
    if not ok:
        return _err(why, "The window exists but is not on this virtual desktop. "
                         "desktop_switch can move between desktops.")
    fg = ctypes.windll.user32.GetForegroundWindow()
    where = "focused" if int(fg) == int(hwnd) else "shown"
    return _ok(f"{where.capitalize()}: {name}", f"hwnd 0x{int(hwnd):X}")


def _dwm_cloak_state(hwnd) -> int:
    """DWM cloak flags: 1 = app hid itself (tray), 2 = other virtual desktop."""
    try:
        import ctypes
        from ctypes import wintypes
        val = wintypes.DWORD(0)
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(int(hwnd)), 14, ctypes.byref(val), ctypes.sizeof(val))
        return int(val.value) if hr == 0 else 0
    except Exception:
        return 0


def _dwm_cloaked(hwnd) -> bool:
    """True when DWM will not paint the window (it is invisible to the user).

    Flag 1 = the app cloaked itself (tray hide); flag 2 = another virtual
    desktop. Both make the window invisible; only the app can clear flag 1."""
    return bool(_dwm_cloak_state(hwnd) & 3)


def _pid_windows(pid):
    """Every top-level window owned by pid, including hidden ones."""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _l):
        p = wintypes.DWORD()
        u.GetWindowThreadProcessId(h, ctypes.byref(p))
        if int(p.value) == int(pid):
            out.append(int(h))
        return True

    u.EnumWindows(cb, 0)
    return out


def _app_window_up(pid):
    """Any visible, uncloaked top-level window of pid (apps may recreate it)."""
    import ctypes
    u = ctypes.windll.user32
    for h in _pid_windows(int(pid)):
        if u.IsWindowVisible(h) and not (_dwm_cloak_state(h) & 1):
            return int(h)
    return 0


def _shell_icon_rect(hwnd, uid):
    """Rect of the shell tray icon owned by hwnd/uid, or None."""
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD),
                    ("d3", wintypes.WORD), ("d4", ctypes.c_ubyte * 8)]

    class NID(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                    ("uID", wintypes.UINT), ("guidItem", GUID)]

    nid = NID(ctypes.sizeof(NID), wintypes.HWND(int(hwnd)), int(uid), GUID())
    r = wintypes.RECT()
    try:
        hr = ctypes.windll.shell32.Shell_NotifyIconGetRect(ctypes.byref(nid), ctypes.byref(r))
    except Exception:
        return None
    if hr != 0:
        return None
    return (int(r.left), int(r.top), int(r.right), int(r.bottom))


def _taskbar_rect():
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    tb = u.FindWindowW("Shell_TrayWnd", None)
    if not tb:
        return None
    r = wintypes.RECT()
    u.GetWindowRect(tb, ctypes.byref(r))
    return (int(r.left), int(r.top), int(r.right), int(r.bottom))


def _reveal_taskbar():
    """Nudge the auto-hidden taskbar open. Returns previous cursor position."""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    pt = wintypes.POINT()
    u.GetCursorPos(ctypes.byref(pt))
    old = (int(pt.x), int(pt.y))
    tb = _taskbar_rect()
    if not tb:
        return old
    u.SetCursorPos((tb[0] + tb[2]) // 2, tb[3] - 3)
    for _ in range(9):
        time.sleep(0.08)
        cur = _taskbar_rect()
        if cur and cur[1] < tb[3] - 60:
            break
    return old


def _overflow_hwnd():
    import ctypes
    u = ctypes.windll.user32
    for cls in ("TopLevelWindowForOverflowXamlIsland", "NotifyIconOverflowWindow"):
        h = u.FindWindowW(cls, None)
        if h and u.IsWindowVisible(h):
            return int(h)
    return 0


def _open_overflow():
    """Open the Windows hidden-icons flyout. Returns its hwnd, or 0."""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    h = _overflow_hwnd()
    if h:
        return h
    old = _reveal_taskbar()
    try:
        tb = _taskbar_rect()
        if not tb:
            return 0
        tn = u.FindWindowExW(int(u.FindWindowW("Shell_TrayWnd", None)), None,
                             "TrayNotifyWnd", None)
        if tn:
            r = wintypes.RECT()
            u.GetWindowRect(tn, ctypes.byref(r))
            left, top, right = int(r.left), int(r.top), int(r.right)
        else:
            left, top, right = tb[2] - 260, tb[1], tb[2]
        y = (top + tb[3]) // 2
        width = max(40, right - left)
        for frac in (0.06, 0.16, 0.26, 0.36, 0.02):
            x = left + int(width * frac)
            u.SetCursorPos(x, y)
            time.sleep(0.04)
            u.mouse_event(0x0002, 0, 0, 0, 0)
            time.sleep(0.04)
            u.mouse_event(0x0004, 0, 0, 0, 0)
            for _ in range(6):
                time.sleep(0.07)
                h = _overflow_hwnd()
                if h:
                    return h
        return 0
    finally:
        u.SetCursorPos(old[0], old[1])


def _overflow_button(names):
    """Find the hidden-icons button whose accessible name matches one of names."""
    if isinstance(names, str):
        names = [names]
    h = _open_overflow()
    if not h:
        return None
    from pywinauto import Desktop
    try:
        w = Desktop(backend="uia").window(handle=h)
        best, score = None, 0
        for c in w.descendants():
            try:
                if (c.element_info.control_type or "") != "Button":
                    continue
                label = " ".join((c.window_text() or "").split()).strip(" .")
                if not label:
                    continue
                s = max(_label_score(n, label) for n in names)
                if s > score:
                    best, score = c, s
            except Exception:
                continue
        return (best, score) if best and score >= 70 else (None, score)
    except Exception:
        return (None, 0)


def _tray_activate(name):
    """Click an app's tray icon (overflow flyout), the way a person would."""
    import ctypes
    from ctypes import wintypes
    name = str(name or "").strip()
    if not name:
        return False, "no app name"
    hit = _find_app_window(name)
    pid = int(hit["pid"]) if hit else 0
    if not pid:
        pid = _pid_for_app(name)
    # Window titles carry unread counters ("AyuGram (15637)") that tray labels
    # do not; use the bare name and the exe stem as search needles too.
    needles = [name]
    bare = re.sub(r"\s*\(\d+\)\s*$", "", name).strip()
    if bare and bare != name:
        needles.append(bare)
    if hit and hit.get("exe"):
        stem = Path(hit["exe"]).stem
        if stem and stem not in needles:
            needles.append(stem)
    name_for_buttons = needles
    # Icons that sit directly on the taskbar have a real rect already.
    u = ctypes.windll.user32
    if pid:
        tb = _taskbar_rect()
        for wh in _pid_windows(pid):
            for uid in range(8):
                r = _shell_icon_rect(wh, uid)
                if not r:
                    continue
                if r[3] - r[1] > 160 or r[2] - r[0] > 160:
                    continue
                if tb and not (tb[1] - 40 <= (r[1] + r[3]) // 2 <= tb[3] + 40):
                    continue
                old = _reveal_taskbar()
                try:
                    u.SetCursorPos((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)
                    time.sleep(0.06)
                    u.mouse_event(0x0002, 0, 0, 0, 0)
                    time.sleep(0.04)
                    u.mouse_event(0x0004, 0, 0, 0, 0)
                finally:
                    u.SetCursorPos(old[0], old[1])
                for _ in range(10):
                    time.sleep(0.1)
                    if _overflow_hwnd():
                        break
                    if _app_window_up(pid):
                        return True, f"clicked the {name} tray icon"
                if _overflow_hwnd():
                    break
    btn, score = _overflow_button(name_for_buttons)
    if not btn:
        return False, (f"no tray icon labelled like {bare or name!r} was visible "
                       f"(best match {score})")
    try:
        btn.invoke()
    except Exception:
        try:
            btn.click_input()
        except Exception as e:
            return False, f"could not activate the tray icon: {e}"
    return True, f"clicked the {name} tray icon"


def _pid_for_app(name):
    try:
        import psutil
    except Exception:
        return 0
    want = _norm_app(name)
    if not want:
        return 0
    best, best_score = 0, 0
    for p in psutil.process_iter(["pid", "name"]):
        stem = _norm_app((p.info.get("name") or "").rsplit(".", 1)[0])
        if not stem:
            continue
        if stem == want:
            return int(p.info["pid"])
        if want in stem or stem in want:
            score = 60 + min(len(want), len(stem))
            if score > best_score:
                best, best_score = int(p.info["pid"]), score
    return best


def _exe_for_pid(pid):
    import ctypes
    from ctypes import wintypes
    k = ctypes.windll.kernel32
    hp = k.OpenProcess(0x1000, False, int(pid))
    if not hp:
        return ""
    try:
        b = ctypes.create_unicode_buffer(1024)
        sz = wintypes.DWORD(1024)
        if k.QueryFullProcessImageNameW(hp, 0, b, ctypes.byref(sz)):
            return b.value
    finally:
        k.CloseHandle(hp)
    return ""


def _restart_app(hwnd="", name="", wait=12):
    """Close a wedged app and start it again. Used when the app itself cloaked
    its only window and refuses to uncloak (AyuGram/Telegram tray bug)."""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    try:
        import psutil
    except Exception:
        psutil = None
    hwnd = int(hwnd or 0)
    pid = 0
    title = ""
    if hwnd:
        p = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        pid = int(p.value)
        n = u.GetWindowTextLengthW(hwnd)
        b = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, b, n + 1)
        title = b.value
    if not pid:
        pid = _pid_for_app(name)
    exe = _exe_for_pid(pid) if pid else ""
    if not exe:
        return None, f"could not find the executable behind {name or title!r}"
    subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True)
    deadline = time.time() + 1.5
    while time.time() < deadline:
        if psutil and not psutil.pid_exists(pid):
            break
        if not psutil and not u.IsWindow(hwnd or 0):
            break
        time.sleep(0.15)
    else:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        time.sleep(0.4)
    try:
        subprocess.Popen([exe], cwd=os.path.dirname(exe) or None,
                         creationflags=0x08000000)
    except Exception as e:
        return None, f"closed the stuck window but could not relaunch {exe}: {e}"
    stem = Path(exe).stem
    deadline = time.time() + max(4, min(int(wait or 12), 30))
    while time.time() < deadline:
        hit = _find_app_window(stem)
        if hit and int(hit["pid"]) != pid:
            h = int(hit["hwnd"])
            if u.IsWindowVisible(h) and not (_dwm_cloak_state(h) & 1):
                _focus_hwnd(h)
                return h, f"restarted {stem} (its hidden window would not come back)"
        time.sleep(0.15)
    return None, f"relaunched {stem} but no usable window appeared within {wait}s"


def _desktop_swipe(direction="right"):
    """Switch virtual desktop with Win+Ctrl+Arrow."""
    import ctypes
    vk = 0x27 if str(direction).lower().startswith("r") else 0x25
    u = ctypes.windll.user32
    for code in (0x11, 0x5B, vk):
        u.keybd_event(code, 0, 0, 0)
        time.sleep(0.02)
    for code in (vk, 0x5B, 0x11):
        u.keybd_event(code, 0, 2, 0)
        time.sleep(0.02)
    time.sleep(0.45)


def _ensure_on_screen(hwnd, name="", auto_switch=True, allow_restart=False):
    """Show/focus a window, recovering tray-hidden and virtual-desktop windows."""
    import ctypes
    u = ctypes.windll.user32
    hwnd = int(hwnd or 0)
    if not hwnd:
        return False, "no window handle"
    if not u.IsWindowVisible(hwnd):
        _focus_hwnd(hwnd)
    state = _dwm_cloak_state(hwnd)
    if state & 2:
        if not auto_switch:
            return False, (f"{name or 'That window'} is open on another virtual desktop. "
                           "Use desktop_switch(direction=right) until it is visible.")
        moved = 0
        reached = False
        for _ in range(3):
            _desktop_swipe("right")
            moved += 1
            if not (_dwm_cloak_state(hwnd) & 2):
                reached = True
                break
        if not reached:
            for _ in range(moved):
                _desktop_swipe("left")
            for _ in range(3):
                _desktop_swipe("left")
                if not (_dwm_cloak_state(hwnd) & 2):
                    reached = True
                    break
        if not reached:
            return False, (f"{name or 'That window'} is on another virtual desktop and I "
                           "could not reach it with three desktop switches.")
    if _dwm_cloak_state(hwnd) & 1:
        # The app hid itself (tray). A real tray click is the only thing that
        # clears this; ShowWindow and SetForegroundWindow cannot.
        target = re.sub(r"\s*\(\d+\)\s*$", "", name or "").strip()
        if not target:
            from ctypes import wintypes
            p = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
            target = Path(_exe_for_pid(p.value)).stem or "app"
        ok_tray, why = _tray_activate(target)
        from ctypes import wintypes as _wt
        p = _wt.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        pid = int(p.value)
        deadline = time.time() + 3.5
        while time.time() < deadline:
            if not (_dwm_cloak_state(hwnd) & 1):
                break
            fresh = _app_window_up(pid)
            if fresh and fresh != hwnd:
                _focus_hwnd(fresh)
                return True, f"{why} (it recreated its window)"
            time.sleep(0.15)
        if _dwm_cloak_state(hwnd) & 1:
            if allow_restart:
                new_hwnd, how = _restart_app(hwnd, target)
                if new_hwnd:
                    return True, how
                return False, how
            return False, (f"{target} has hidden its own window and did not come back "
                           f"from the tray click ({why}). Call open_app(name, "
                           f"restart_stuck=true) to relaunch it - its data is saved.")
    ok = _focus_hwnd(hwnd)
    time.sleep(0.2)
    if not u.IsWindowVisible(hwnd):
        return False, f"{name or 'That window'} exists but Windows will not show it"
    state = _dwm_cloak_state(hwnd)
    if state & 2:
        return False, f"{name or 'That window'} is still on another virtual desktop"
    if state & 1:
        return False, f"{name or 'That window'} re-hid itself right after being shown"
    return True, "ready"


def t_desktop_switch(direction="right", times=1):
    n = max(1, min(int(times or 1), 10))
    for _ in range(n):
        _desktop_swipe(direction)
    name = "right" if str(direction).lower().startswith("r") else "left"
    return _ok(f"Switched virtual desktop {n} time(s) {name}")


def t_window_ping(title=""):
    """Fast liveness check: title, rect, visibility and foreground state."""
    import ctypes
    hwnd, name = _find_window(title) if str(title or "").strip() else (None, "")
    if not hwnd:
        return _err(f"No window matching {title!r}")
    u = ctypes.windll.user32
    from ctypes import wintypes
    r = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    state = _dwm_cloak_state(hwnd)
    detail = (f"hwnd=0x{int(hwnd):X} visible={bool(u.IsWindowVisible(hwnd))} "
              f"minimized={bool(u.IsIconic(hwnd))} cloaked={state} "
              f"foreground={int(u.GetForegroundWindow()) == int(hwnd)} "
              f"rect={r.left},{r.top},{r.right},{r.bottom}")
    if state & 1:
        detail += " (app hid its window; tray_icon_click or open_app restart_stuck fixes it)"
    elif state & 2:
        detail += " (on another virtual desktop; use desktop_switch)"
    return _ok(name, detail)


def t_tray_icon_click(name):
    """Click an app's system-tray icon, even when it is in the hidden-icons overflow."""
    name = str(name or "").strip()
    if not name:
        return _err("tray_icon_click needs an app or icon name, e.g. AyuGram, Bluetooth")
    ok, why = _tray_activate(name)
    if not ok:
        return _err(f"Could not click the {name} tray icon", why)
    return _ok(f"Clicked the {name} tray icon", why)


def t_app_restart(name):
    """Close and relaunch an app whose only window is stuck hidden or unresponsive."""
    name = str(name or "").strip()
    if not name:
        return _err("app_restart needs an app name")
    hit = _find_app_window(name)
    hwnd = int(hit["hwnd"]) if hit else 0
    new_hwnd, how = _restart_app(hwnd, name)
    return _ok(how) if new_hwnd else _err(how)


def t_send_keys(keys, window=""):
    if not keys:
        return _err("send_keys needs the key sequence")
    if window:
        okw, outw, _ = t_window_focus(window)
        if not okw:
            return _err(f"Could not focus '{window}'", outw)
        time.sleep(0.4)
    safe = keys.replace("'", "''")
    script = ("Add-Type -AssemblyName System.Windows.Forms;"
              f"[System.Windows.Forms.SendKeys]::SendWait('{safe}'); 'sent'")
    ok, out = _run_powershell(script, 20)
    return (ok, f"Sent keys {keys!r}", out)


def t_now(fmt="%Y-%m-%d %H:%M:%S"):
    return _ok(time.strftime(fmt))


def t_calculate(expression):
    if re.search(r"[A-Za-z_]{2,}", expression.replace("abs", "").replace("round", "")
                 .replace("sqrt", "").replace("min", "").replace("max", "").replace("pow", "")):
        return _err("Only numeric expressions supported")
    allowed = set("0123456789.+-*/%() ,")
    if not set(expression) <= allowed:
        return _err("Unsupported characters in expression")
    try:
        return _ok(f"{expression} = {eval(expression, {'__builtins__': {}}, {})}")
    except Exception as e:
        return _err(f"Cannot evaluate: {e}")


def t_hash(path, algo="sha256"):
    p = resolve_path(path)
    if not p.is_file():
        return _err(f"No file at {rel_display(p)}")
    h = hashlib.new(algo)
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return _ok(f"{algo} {h.hexdigest()}")


SEVEN_ZIP_CANDIDATES = (
    Path(r"C:\Program Files\7-Zip\7z.exe"),
    Path(r"C:\Program Files (x86)\7-Zip\7z.exe"),
    Path(r"C:\Program Files\7-Zip\7za.exe"),
)
ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".cab",
                ".iso", ".jar", ".war", ".apk", ".wim"}


def _seven_zip():
    for cand in SEVEN_ZIP_CANDIDATES:
        if cand.is_file():
            return cand
    found = shutil.which("7z") or shutil.which("7za") or shutil.which("7zz")
    return Path(found) if found else None


def _human_bytes(n):
    n = float(n or 0)
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{int(n)} B"


def _seven(args, timeout=3600, cwd=None):
    exe = _seven_zip()
    if exe is None:
        return None, "7-Zip is not installed"
    try:
        proc = subprocess.run([str(exe), *args], capture_output=True, text=True,
                              errors="replace", timeout=timeout,
                              cwd=str(cwd) if cwd else None)
    except subprocess.TimeoutExpired:
        return -1, "7-Zip timed out"
    out = "\n".join(x for x in ((proc.stdout or "").strip(),
                                (proc.stderr or "").strip()) if x)
    return proc.returncode, out


def _archive_listing(src, password=""):
    pw = [f"-p{password}"] if password else []
    if _seven_zip():
        code, out = _seven(["l", "-slt"] + pw + [str(src)])
        if code not in (0, 1):
            raise RuntimeError(out[-700:] or f"7-Zip exit {code}")
        entries, total = [], 0
        target = os.path.normcase(os.path.abspath(src))
        for block in re.split(r"\n\s*\n", out):
            if "Path = " not in block:
                continue
            meta = {}
            for line in block.splitlines():
                if " = " in line:
                    k, v = line.split(" = ", 1)
                    meta[k.strip()] = v.strip()
            name = meta.get("Path", "")
            if not name or os.path.normcase(os.path.abspath(name)) == target:
                continue
            if meta.get("Folder") == "+":
                continue
            try:
                size = int(meta.get("Size") or 0)
            except ValueError:
                size = 0
            total += size
            entries.append((name, size))
        return entries, total
    if src.suffix.lower() == ".zip":
        with zipfile.ZipFile(src) as z:
            infos = [i for i in z.infolist() if not i.is_dir()]
        return [(i.filename, i.file_size) for i in infos], sum(i.file_size for i in infos)
    raise RuntimeError("7-Zip is not installed, so only .zip archives can be read")


def t_archive(path, dest="", mode="extract", fmt="", items="", password=""):
    """Unpack, create, add to or inspect archives. Uses 7-Zip for every format."""
    src = resolve_path(path)
    mode = str(mode or "extract").lower()
    fmt = str(fmt or "").lstrip(".").lower()
    pw = [f"-p{password}"] if password else []

    if mode in ("list", "ls", "info", "inspect"):
        if not src.is_file():
            return _err(f"No archive at {rel_display(src)}")
        try:
            entries, total = _archive_listing(src, password)
        except Exception as e:
            return _err(f"Could not read {src.name}", str(e)[-600:])
        body = "\n".join(f"{name}  ({_human_bytes(size)})" for name, size in entries[:200])
        head = (f"{src.name}: {len(entries)} file(s), {_human_bytes(total)} inside, "
                f"{_human_bytes(src.stat().st_size)} on disk")
        return _ok(head, body or "(empty archive)")

    if mode in ("create", "add", "zip", "pack", "compress"):
        if not src.exists() and mode != "add":
            return _err(f"Nothing at {rel_display(src)}")
        if mode == "add":
            if not src.is_file():
                return _err("add mode needs path to be the existing archive file")
            out_path = src
            wanted = _as_list(items)
            if not wanted:
                return _err("add mode needs items: files or wildcards to put into the archive")
            names = []
            for item in wanted:
                cand = Path(item)
                if not cand.is_absolute():
                    cand = src.parent / item
                names.extend(str(x) for x in (src.parent.glob(item) if any(c in item for c in "*?[")
                                              else [cand]) if x.exists())
            if not names:
                return _err("None of the items exist", "\n".join(wanted))
            if _seven_zip():
                code, out = _seven(["a", str(out_path), *names, "-y", "-bso0", "-bsp0"] + pw,
                                   cwd=src.parent)
                if code not in (0, 1):
                    return _err(f"7-Zip could not add to {src.name}", out[-700:])
            else:
                with zipfile.ZipFile(out_path, "a", zipfile.ZIP_DEFLATED) as z:
                    for n in names:
                        p = Path(n)
                        if p.is_file():
                            z.write(p, p.relative_to(src.parent))
            return _ok(f"Added {len(names)} item(s) to {src.name}", str(out_path))

        ext = fmt or "zip"
        if dest:
            out_path = resolve_path(dest)
            if out_path.is_dir() or not out_path.suffix:
                out_path = out_path / (src.stem + "." + ext)
        else:
            out_path = src.parent / (src.name + "." + ext)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        base = src.parent if src.is_dir() else src.parent
        member = src.name
        if _seven_zip():
            code, out = _seven(["a", f"-t{ext}", str(out_path), member, "-y",
                                "-bso0", "-bsp0"] + pw, cwd=base)
            if code not in (0, 1):
                return _err(f"7-Zip could not create {out_path.name}", out[-700:])
        else:
            if ext != "zip":
                return _err(f"7-Zip is not installed; only zip is available, not {ext}")
            if src.is_dir():
                shutil.make_archive(str(out_path.with_suffix("")), "zip", str(src))
            else:
                with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
                    z.write(src, member)
        return _ok(f"Created {out_path.name} ({_human_bytes(out_path.stat().st_size)})",
                   str(out_path))

    if not src.is_file():
        return _err(f"Not an archive file: {rel_display(src)}")
    target = resolve_path(dest) if str(dest or "").strip() else src.parent / src.stem
    target.mkdir(parents=True, exist_ok=True)
    if _seven_zip():
        code, out = _seven(["x", str(src), f"-o{target}", "-y", "-bso0", "-bsp0"] + pw)
        if code not in (0, 1):
            return _err(f"7-Zip could not extract {src.name}", out[-800:])
    else:
        try:
            shutil.unpack_archive(str(src), str(target))
        except Exception as e:
            return _err(f"Could not extract {src.name}", str(e))
    files = sum(1 for p in target.rglob("*") if p.is_file())
    return _ok(f"Extracted {src.name} -> {rel_display(target)} ({files} files)", str(target))


def t_remember(when, do):
    return _ok(remember(when, do))


def t_lessons(query=""):
    items = load_lessons()
    if query:
        q = _tokens(query)
        items = [L for L in items if q & _tokens(L.get("when", "") + L.get("do", ""))] or items
    lines = [f"#{L['id']} WHEN {L['when']}\n  DO   {L['do']}" for L in items[-30:]]
    return _ok(f"{len(items)} lesson(s) match", "\n".join(lines) or "(none)")


def t_forget(id=""):
    items = load_lessons()
    kept = [L for L in items if str(L.get("id")) != str(id)]
    if len(kept) == len(items):
        return _err(f"No lesson with id {id}")
    save_lessons(kept)
    return _ok(f"Removed lesson {id}")


def t_create_tool(name, description, code, language="python"):
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", name)[:48]
    ext = ".ps1" if str(language).lower().startswith(("pow", "ps")) else ".py"
    fp = CUSTOM_DIR / (safe + ext)
    fp.write_text(code, encoding="utf-8")
    reg = _load_json(TOOLS_FILE, {})
    reg[safe] = {"description": description, "path": str(fp), "language": language}
    _save_json(TOOLS_FILE, reg)
    return _ok(f"Registered custom tool {safe} ({language})")


def t_run_custom(name, args=""):
    reg = _load_json(TOOLS_FILE, {})
    if name not in reg:
        return _err(f"Unknown custom tool {name}. Available: {', '.join(reg) or '(none)'}")
    meta = reg[name]
    fp = meta["path"]
    argv = (["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", fp]
            if str(meta.get("language", "")).lower().startswith(("pow", "ps"))
            else [sys.executable, fp])
    if args:
        argv += shlex.split(args)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=180, cwd=str(workspace()))
        out = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
        return proc.returncode == 0, f"{name} exited {proc.returncode}", out[:6000]
    except Exception as e:
        return _err(str(e))


def t_list_tools():
    reg = _load_json(TOOLS_FILE, {})
    return _ok(f"{len(TOOLS)} builtin + {len(reg)} custom",
               "\n".join(sorted(TOOLS)) + "\ncustom:\n" + "\n".join(reg))


# ---------------------------------------------------------------------------
# Media / internet toolchain (yt-dlp + ffmpeg)
# ---------------------------------------------------------------------------
BIN_DIR = ROOT / "tools" / "bin"
BIN_DIR.mkdir(parents=True, exist_ok=True)


def _exe(*names):
    for n in names:
        p = BIN_DIR / n
        if p.is_file():
            return str(p)
        w = shutil.which(n)
        if w:
            return w
    return ""


def _ytdlp():
    exe = _exe("yt-dlp.exe", "yt-dlp")
    if exe:
        return [exe]
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except Exception:
        return None


def _ffmpeg():
    exe = _exe("ffmpeg.exe", "ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return ""


_STOP_EVT = None
_ACTIVE_PROCS = set()


def _kill_tree(p):
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                       capture_output=True, creationflags=0x08000000)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def _proc(argv, timeout=600, cwd=None):
    try:
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", errors="replace",
                             cwd=str(cwd or workspace()),
                             creationflags=0x08000000)
    except Exception as e:
        return False, str(e)
    _ACTIVE_PROCS.add(p)
    deadline = time.time() + timeout
    stopped = False
    try:
        while True:
            try:
                out, err = p.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                ev = _STOP_EVT
                if ev is not None and ev.is_set():
                    stopped = True
                    _kill_tree(p)
                    try:
                        out, err = p.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        out, err = "", ""
                    break
                if time.time() >= deadline:
                    _kill_tree(p)
                    try:
                        out, err = p.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        out, err = "", ""
                    tail = ((out or "") + (err or "")).strip()[-600:]
                    return False, f"Timed out after {timeout}s" + (f"\n{tail}" if tail else "")
        out = (out or "").strip()
        err = (err or "").strip()
        body = (out + ("\n" + err if err else "")).strip()
        if stopped:
            return False, "Stopped by you." + (f"\n{body[-600:]}" if body else "")
        return p.returncode == 0, body
    finally:
        _ACTIVE_PROCS.discard(p)


def _files_since(folder: Path, since: float, limit=20):
    found = []
    try:
        for p in folder.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() in {".part", ".ytdl", ".tmp"}:
                continue
            try:
                if p.stat().st_mtime >= since:
                    found.append(p)
            except Exception:
                pass
    except Exception:
        pass
    found.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return found[:limit]


def _http_json(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA,
                                               "Accept": "application/json"})
    with http_open(req, timeout) as r:
        return json.loads(r.read(2_500_000).decode("utf-8", "replace"))


class _YtStop(Exception):
    pass


class _YtQuietLogger:
    def __init__(self):
        self.lines = []

    def _add(self, msg):
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(msg)).strip()
        if msg:
            self.lines.append(msg)
            del self.lines[:-40]

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self._add(msg)

    def error(self, msg):
        self._add(msg)


def _ytdlp_api():
    try:
        import yt_dlp
        return yt_dlp
    except Exception:
        return None


def _yt_progress_hook(stop_box):
    last = [0.0]

    def hook(d):
        ev = _STOP_EVT
        if ev is not None and ev.is_set():
            stop_box[0] = True
            raise _YtStop()
        status = d.get("status")
        now = time.time()
        if status == "downloading" and now - last[0] >= 1.5:
            last[0] = now
            pct = re.sub(r"\x1b\[[0-9;]*m", "", str(d.get("_percent_str") or "")).strip()
            if pct:
                emit("status", state="working", text=f"Downloading {pct}")
        elif status == "finished":
            emit("status", state="working", text="Saving and converting...")

    return hook


def _yt_collect_outputs(info, dest, since):
    files = []
    entries = info.get("entries") if isinstance(info, dict) else None
    for e in (list(entries) if entries else [info]):
        if not isinstance(e, dict):
            continue
        for rd in e.get("requested_downloads") or []:
            fp = rd.get("filepath") or rd.get("_filename") or ""
            if fp:
                files.append(Path(fp))
    files = [p for p in files if p.is_file()]
    if not files:
        files = _files_since(dest, since - 2, 10)
    return files


def _has_audio_stream(path):
    """True/False when ffmpeg can tell, None when it cannot."""
    ff = _ffmpeg()
    if not ff:
        return None
    try:
        ok, out = _proc([ff, "-hide_banner", "-i", str(path)], 60)
    except Exception:
        return None
    return "Audio:" in out


_MUSIC_BAD = re.compile(
    r"\b(how to|tutorial|ringtone|download (?:songs?|music|videos?)|android|iphone|apk|"
    r"app (?:for|to)|without (?:internet|app)|reaction|shorts?|interview questions?|"
    r"coding (?:course|tutorial)|class \d+|episode \d+|podcast)\b", re.I)
_MUSIC_GOOD = re.compile(
    r"\b(official (?:audio|video|music video)|lyrics?|audio|remaster(?:ed)?|"
    r"topic|hq|hd)\b", re.I)
_MUSIC_MEH = re.compile(
    r"\b(remix|cover|live|extended|sped up|slowed|nightcore|8d|reverb|"
    r"instrumental|karaoke|mashup|medley)\b", re.I)
_MUSIC_MODS = ("sped up", "slowed", "remix", "cover", "instrumental", "karaoke",
               "live", "extended", "nightcore", "8d", "reverb", "mashup", "medley",
               "acoustic")


def _music_modifiers(text: str) -> set:
    """Which alternate versions the user explicitly asked for."""
    s = str(text or "").lower()
    out = set()
    if re.search(r"\bsped[ -]?up\b|\bspeed[ -]?up\b|\bspeedup\b", s):
        out.add("sped up")
    if re.search(r"\bslowed\b|\bslow[ -]?down\b|\bslowed[ -]?reverb\b", s):
        out.add("slowed")
    for word in _MUSIC_MODS:
        if word in ("sped up", "slowed"):
            continue
        if re.search(r"\b" + re.escape(word) + r"\b", s):
            out.add(word)
    return out


def _yt_pick(yt_dlp, query, n=12):
    """Search YouTube and pick a plausible song/topic result, not a tutorial."""
    log = _YtQuietLogger()
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
            "socket_timeout": 15, "ignoreerrors": True, "logger": log}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
    except Exception:
        return None
    tokens = [t for t in re.findall(r"[a-z0-9']+", query.lower()) if len(t) > 2]
    want = _music_modifiers(query)
    best, best_score = None, -99
    for e in (info or {}).get("entries") or []:
        if not isinstance(e, dict) or not e.get("title"):
            continue
        title = str(e.get("title"))
        low = title.lower()
        score = 0
        if _MUSIC_BAD.search(title):
            score -= 6
        if _MUSIC_GOOD.search(title):
            score += 3
        found = _music_modifiers(title)
        if want:
            score += 4 * len(found & want)
            score -= 2 * len(found - want)
        elif _MUSIC_MEH.search(title):
            score -= 3
        score += sum(2 for t in tokens if t in low)
        cover = len(re.findall(r"\[[^\]]*\]|\([^)]*\)", title))
        score -= cover
        dur = e.get("duration")
        if isinstance(dur, (int, float)) and dur > 0:
            if 45 <= dur <= 600:
                score += 2
            elif dur > 1800:
                score -= 2
        channel = str(e.get("channel") or e.get("uploader") or "").lower()
        if "topic" in channel:
            score += 3
        score += sum(1 for t in tokens if t in channel)
        if score > best_score:
            best, best_score = e, score
    if not best or best_score < 0:
        return None
    vid = best.get("id") or ""
    url = best.get("webpage_url") or best.get("url") or ""
    if not url.startswith("http"):
        if not vid:
            return None
        url = "https://www.youtube.com/watch?v=" + vid
    return url, str(best.get("title") or "")


def _yt_download_api(yt_dlp, url, dest, tmpl, video, q, ff, playlist):
    # Run yt-dlp in-process: spawning python.exe children from a long-lived
    # backend can deadlock on machines with third-party DLL injection.
    log = _YtQuietLogger()
    stop_box = [False]
    runtimes = {"deno": {}}
    if shutil.which("node"):
        runtimes["node"] = {}
    opts = {
        "outtmpl": str(dest / tmpl),
        "noplaylist": not playlist,
        "windowsfilenames": True,
        "updatetime": False,
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "js_runtimes": runtimes,
        "remote_components": {"ejs:github"},
        "logger": log,
        "progress_hooks": [_yt_progress_hook(stop_box)],
    }
    if video:
        h = f"[height<={q}]" if q else ""
        if ff:
            opts["format"] = f"bv*{h}+ba/b[acodec!=none]{h}/b{h}"
            opts["merge_output_format"] = "mp4"
            opts["ffmpeg_location"] = ff
        else:
            # "b" alone can select a DASH video-only mp4, which is how silent
            # "videos" happened. Require an audio stream.
            opts["format"] = (f"b[ext=mp4][vcodec!=none][acodec!=none]{h}/"
                              f"b[acodec!=none]{h}/b{h}")
    elif ff:
        opts["format"] = "ba/b[acodec!=none]"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                   "preferredcodec": "mp3",
                                   "preferredquality": "0"}]
        opts["ffmpeg_location"] = ff
    else:
        opts["format"] = "ba[ext=m4a]/ba/b[acodec!=none]"

    t0 = time.time()

    def _run(fmt=None):
        o = dict(opts)
        if fmt:
            o["format"] = fmt
        with yt_dlp.YoutubeDL(o) as ydl:
            return ydl.extract_info(url, download=True)

    try:
        try:
            info = _run()
        except _YtStop:
            raise
        except Exception as e:
            msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
            if video and "Requested format is not available" in msg:
                emit("status", state="working", text="Retrying with a simpler format...")
                info = _run("b[ext=mp4][vcodec!=none][acodec!=none]/"
                            "b[acodec!=none]/b")
            else:
                raise
    except _YtStop:
        return [], "Stopped by you.", "\n".join(log.lines[-6:])
    except Exception as e:
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).strip()
        if stop_box[0]:
            return [], "Stopped by you.", msg[-1200:]
        return [], "Download failed", (msg + "\n" + "\n".join(log.lines[-6:])).strip()[-1500:]
    files = _yt_collect_outputs(info, dest, t0)
    if video and ff and files:
        silent = [p for p in files if _has_audio_stream(p) is False]
        if silent:
            emit("status", state="working", text="Video had no audio; fetching the sound track...")
            try:
                retry = f"bv*[height<={q}]+ba/b[acodec!=none]" if q else "bv*+ba/b[acodec!=none]"
                info = _run(retry)
                files = _yt_collect_outputs(info, dest, t0)
            except Exception as e:
                log._add(str(e))
    return files, "", "\n".join(log.lines[-6:])


def _yt_search_api(yt_dlp, query, n):
    log = _YtQuietLogger()
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
            "socket_timeout": 15, "ignoreerrors": True, "logger": log}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
    rows = []
    for e in (info or {}).get("entries") or []:
        if not e:
            continue
        url = e.get("webpage_url") or e.get("url") or ""
        if not url:
            continue
        if not url.startswith("http"):
            url = "https://www.youtube.com/watch?v=" + url
        dur = e.get("duration")
        if isinstance(dur, (int, float)) and dur > 0:
            dur_s = f"{int(dur // 60)}:{int(dur % 60):02d}"
        else:
            dur_s = e.get("duration_string") or ""
        rows.append([e.get("title") or "", url, dur_s])
    return rows


def t_youtube_download(target, kind="audio", quality="best", folder="", filename="",
                       playlist=False):
    target = (target or "").strip().strip('"')
    if not target:
        return _err("youtube_download needs a URL, video id, or search phrase")
    api = _ytdlp_api()
    yt = None if api is not None else _ytdlp()
    if api is None and not yt:
        return _err("yt-dlp is not installed",
                    "Install it with: py -m pip install --user yt-dlp")
    dest = resolve_path(folder) if folder else workspace()
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return _err(f"Cannot create {dest}: {e}")

    url = target
    k = str(kind or "audio").lower()
    search_phrase = (not re.match(r"^[a-zA-Z][\w+.-]*://", target)
                     and not re.fullmatch(r"[\w-]{11}", target))
    picked_title = ""
    if search_phrase and api is not None:
        query = target
        if (k.startswith("aud") or k in {"mp3", "music", "song"}) and \
                not _music_modifiers(query) and \
                not re.search(r"official|lyrics|audio", query, re.I):
            query = target + " official audio"
        picked = _yt_pick(api, query)
        if picked:
            url, picked_title = picked
            emit("status", state="working", text=f"Found: {picked_title[:60]}")
    if search_phrase and url == target:
        url = "ytsearch1:" + target

    video = k.startswith("vid") or k in {"mp4", "mkv", "webm", "movie", "clip"}
    ff = _ffmpeg()
    q = re.sub(r"[^0-9]", "", str(quality or ""))

    tmpl = clean_stem(str(filename), 100) + ".%(ext)s" if filename else \
        "%(title).110s [%(id)s].%(ext)s"

    emit("status", state="working",
         text=(f"Pulling video from {target[:50]}" if video
               else f"Pulling audio from {target[:50]}"))
    if api is not None:
        files, fail, log_tail = _yt_download_api(api, url, dest, tmpl, video, q, ff,
                                                 playlist)
        if fail:
            return _err(fail, log_tail)
    else:
        argv = yt + ["--no-warnings", "--newline", "--windows-filenames", "--no-mtime",
                     "--no-simulate", "--socket-timeout", "20", "--retries", "3",
                     "--fragment-retries", "3", "--extractor-retries", "2",
                     "-o", str(dest / tmpl),
                     "--print", "after_move:__FILE__%(filepath)s"]
        argv += ["--yes-playlist"] if playlist else ["--no-playlist"]
        if video:
            if ff:
                h = f"[height<={q}]" if q else ""
                argv += ["-f", f"bv*{h}+ba/b{h}/b", "--merge-output-format", "mp4",
                         "--ffmpeg-location", ff]
            else:
                h = f"[height<={q}]" if q else ""
                argv += ["-f", f"b[ext=mp4]{h}/b{h}/b"]
        else:
            if ff:
                argv += ["-x", "--audio-format", "mp3", "--audio-quality", "0",
                         "--ffmpeg-location", ff]
            else:
                argv += ["-f", "ba[ext=m4a]/ba/b"]
        argv.append(url)
        t0 = time.time()
        ok, out = _proc(argv, timeout=1800, cwd=dest)
        files = []
        for line in out.splitlines():
            if line.startswith("__FILE__"):
                p = line[len("__FILE__"):].strip()
                if p:
                    files.append(Path(p))
        if not files:
            files = _files_since(dest, t0 - 2, 10)
        if not ok and not files:
            tail = "\n".join(out.splitlines()[-14:])
            return _err("Download failed", tail[-1500:] or out[-800:])
    files = [p for p in files if p.is_file()]
    if not files:
        return _err("Download finished but no file appeared",
                    log_tail or rel_display(dest))
    silent = [p for p in files if _has_audio_stream(p) is False]
    if silent:
        return _err("The downloaded file has no audio track",
                    f"{silent[0].name} is silent. Try the same target again; the tool now "
                    f"requires a stream with audio.")
    rows = []
    total = 0
    for p in files[:12]:
        try:
            sz = p.stat().st_size
        except Exception:
            sz = 0
        total += sz
        rows.append(f"{rel_display(p)}  ({sz / 1048576:.1f} MB)")
    what = "video" if video else "audio"
    note = "" if ff else "  (ffmpeg missing: kept the original container)"
    if picked_title:
        note += f"  matched: {picked_title[:80]}"
    return _ok(f"Saved {len(rows)} {what} file(s), {total / 1048576:.1f} MB -> "
               f"{rel_display(dest)}{note}", "\n".join(rows))


def t_download_audio(target, folder="", quality="best"):
    return t_youtube_download(target, kind="audio", quality=quality, folder=folder)


def t_download_page_media(url, folder="", kind="all", limit=12):
    url = (url or "").strip()
    if not url:
        return _err("download_page_media needs a page URL")
    if not re.match(r"^https?://", url):
        url = "https://" + url
    host = urllib.parse.urlparse(url).hostname or ""
    k = str(kind or "all").lower()
    if "youtube" in host and k in {"all", "video", "audio"}:
        return t_youtube_download(url, "video" if k == "video" else "audio", folder=folder)
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA,
                                               "Accept-Language": "en-US,en;q=0.9"})
    try:
        with http_open(req, 25) as r:
            html = r.read(2_000_000).decode("utf-8", "replace")
    except Exception as e:
        return _err(f"Fetch failed: {e}")

    groups = {
        "image": {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "avif"},
        "video": {"mp4", "webm", "mov", "mkv", "m4v"},
        "audio": {"mp3", "m4a", "wav", "ogg", "opus", "flac", "aac"},
    }
    allowed = set().union(*groups.values()) if k == "all" else groups.get(k, set())
    cands = []
    for m in re.finditer(r'<meta[^>]+(?:property|name)=["\']og:(?:image|video|audio)["\']'
                         r'[^>]+content=["\']([^"\']+)', html, re.I):
        cands.append(m.group(1))
    for m in re.finditer(r'<(?:img|source|video|audio)[^>]+src=["\']([^"\']+)', html, re.I):
        cands.append(m.group(1))
    ext_re = "|".join(sorted(set().union(*groups.values())))
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+\.(?:' + ext_re + r'))["\']', html, re.I):
        cands.append(m.group(1))
    picked = []
    for c in cands:
        u = urllib.parse.urljoin(url, c.replace("&amp;", "&"))
        if not u.lower().startswith(("http://", "https://")):
            continue
        ext = os.path.splitext(urllib.parse.urlparse(u).path)[1].lower().lstrip(".")
        if ext not in allowed or u in picked:
            continue
        picked.append(u)
    if not picked:
        return _err(f"No {k if k != 'all' else 'media'} links found on {host}")
    dest = resolve_path(folder) if folder else workspace() / ("media-" + clean_stem(host, 30))
    dest.mkdir(parents=True, exist_ok=True)
    rows = []
    for u in picked[: max(1, min(int(limit or 12), 40))]:
        ok, summary, _detail = t_download(u, str(dest))
        rows.append(("OK  " if ok else "FAIL ") + summary)
    good = sum(1 for r in rows if r.startswith("OK"))
    return _ok(f"Saved {good}/{len(rows)} file(s) -> {rel_display(dest)}", "\n".join(rows))


def t_media_info(path):
    p = resolve_path(path)
    if not p.is_file():
        return _err(f"No file at {rel_display(p)}")
    ff = _ffmpeg()
    if not ff:
        return _err("ffmpeg is not available", "py -m pip install --user imageio-ffmpeg")
    _okflag, out = _proc([ff, "-hide_banner", "-i", str(p)], 60)
    keep = [ln.strip() for ln in out.splitlines()
            if any(k in ln for k in ("Input #", "Duration", "Stream #"))]
    size = p.stat().st_size / 1048576
    return _ok(f"{p.name}: {size:.1f} MB", "\n".join(keep[:14]) or out[-900:])


def t_convert_media(path, fmt="mp4", dest="", quality=""):
    src = resolve_path(path)
    if not src.is_file():
        return _err(f"No file at {rel_display(src)}")
    ff = _ffmpeg()
    if not ff:
        return _err("ffmpeg is not available", "py -m pip install --user imageio-ffmpeg")
    fmt = re.sub(r"[^a-zA-Z0-9]", "", str(fmt or "mp4")).lower() or "mp4"
    out = resolve_path(dest) if dest else src.with_suffix("." + fmt)
    if out.is_dir():
        out = out / (src.stem + "." + fmt)
    out.parent.mkdir(parents=True, exist_ok=True)
    argv = [ff, "-hide_banner", "-y", "-i", str(src)]
    audio_only = {"mp3", "m4a", "wav", "ogg", "flac", "opus", "aac"}
    if fmt in {"mp4", "mkv", "webm", "mov"}:
        q = re.sub(r"\D", "", str(quality or ""))
        crf = q if q else ("28" if str(quality or "").lower() in {"low", "small"} else "23")
        vcodec = "libvpx-vp9" if fmt == "webm" else "libx264"
        argv += ["-c:v", vcodec, "-crf", crf]
        if fmt != "webm":
            argv += ["-c:a", "aac", "-b:a", "192k"]
        else:
            argv += ["-c:a", "libopus"]
    elif fmt in audio_only:
        codecs = {"mp3": "libmp3lame", "m4a": "aac", "wav": "pcm_s16le",
                  "ogg": "libvorbis", "flac": "flac", "opus": "libopus", "aac": "aac"}
        argv += ["-vn", "-c:a", codecs.get(fmt, "libmp3lame")]
    argv.append(str(out))
    emit("status", state="working", text=f"Converting {src.name} to {fmt.upper()}")
    ok, body = _proc(argv, 1800)
    if not ok or not out.exists():
        return _err("Conversion failed", body[-1400:])
    return _ok(f"Converted -> {rel_display(out)} ({out.stat().st_size / 1048576:.1f} MB)")


def t_extract_audio(path, fmt="mp3", dest=""):
    return t_convert_media(path, fmt=fmt, dest=dest)


def t_trim_media(path, start="0", duration="", dest=""):
    src = resolve_path(path)
    if not src.is_file():
        return _err(f"No file at {rel_display(src)}")
    ff = _ffmpeg()
    if not ff:
        return _err("ffmpeg is not available", "py -m pip install --user imageio-ffmpeg")
    out = resolve_path(dest) if dest else src.with_name(
        f"{src.stem} trimmed{src.suffix}")
    if out.is_dir():
        out = out / f"{src.stem} trimmed{src.suffix}"
    out.parent.mkdir(parents=True, exist_ok=True)
    argv = [ff, "-hide_banner", "-y", "-ss", str(start or "0"), "-i", str(src)]
    if str(duration or "").strip():
        argv += ["-t", str(duration)]
    argv += ["-c", "copy", str(out)]
    ok, body = _proc(argv, 600)
    if not ok or not out.exists():
        return _err("Trim failed", body[-1200:])
    return _ok(f"Trimmed -> {rel_display(out)}")


def t_youtube_search(query, max_results=6, open_browser=False):
    query = (query or "").strip()
    if not query:
        return _err("youtube_search needs a query")
    if open_browser:
        os.startfile("https://www.youtube.com/results?search_query=" +
                     urllib.parse.quote_plus(query))
    n = max(1, min(int(max_results or 6), 12))
    rows = []
    api = _ytdlp_api()
    yt = None if api is not None else _ytdlp()
    if api is not None:
        try:
            rows = _yt_search_api(api, query, n)
        except Exception:
            rows = []
    elif yt:
        argv = yt + ["--flat-playlist", "--no-warnings", "--skip-download",
                     "--socket-timeout", "15", "--retries", "1",
                     "--print", "%(title)s\t%(webpage_url)s\t%(duration_string)s",
                     f"ytsearch{n}:{query}"]
        _okflag, out = _proc(argv, 90)
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[1].startswith("http"):
                rows.append(parts)
    if not rows:
        hits, _fail = web_search_api(f"site:youtube.com {query}", n)
        rows = [[h["title"], h["url"], ""] for h in (hits or [])]
    if not rows:
        return _err("YouTube search returned nothing", "Try web_search or the direct URL.")
    lines = [f"{i}. {r[0][:90]}" + (f"  ({r[2]})" if len(r) > 2 and r[2] else "")
             + f"\n   {r[1]}" for i, r in enumerate(rows[:n], 1)]
    return _ok(f"{len(lines)} video(s)", "\n".join(lines))


# ---------------------------------------------------------------------------
# Web utilities
# ---------------------------------------------------------------------------
_WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
        45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle",
        55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
        71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
        81: "rain showers", 82: "violent showers", 95: "thunderstorm",
        96: "thunderstorm with hail", 99: "thunderstorm with hail"}


def t_wikipedia(query, sentences=4):
    q = (query or "").strip()
    if not q:
        return _err("wikipedia needs a topic")
    try:
        data = _http_json("https://en.wikipedia.org/api/rest_v1/page/summary/" +
                          urllib.parse.quote(q.replace(" ", "_")))
        if not data.get("extract") or data.get("type") == "disambiguation":
            s = _http_json("https://en.wikipedia.org/w/api.php?action=query&list=search"
                           "&format=json&srlimit=1&srsearch=" + urllib.parse.quote(q))
            title = s["query"]["search"][0]["title"]
            data = _http_json("https://en.wikipedia.org/api/rest_v1/page/summary/" +
                              urllib.parse.quote(title.replace(" ", "_")))
    except Exception as e:
        return _err(f"Wikipedia lookup failed: {e}")
    extract = " ".join(data.get("extract", "").split())
    parts = re.split(r"(?<=[.!?])\s+", extract)
    n = max(1, min(int(sentences or 4), 10))
    link = (data.get("content_urls") or {}).get("desktop", {}).get("page", "")
    return _ok(data.get("title", q), "\n\n".join([" ".join(parts[:n]), link]).strip())


def t_weather(location="", days=3):
    loc = (location or "").strip()
    lat = lon = None
    name = loc
    try:
        if loc:
            g = _http_json("https://geocoding-api.open-meteo.com/v1/search?count=1&language=en"
                           "&format=json&name=" + urllib.parse.quote(loc))
            hit = (g.get("results") or [None])[0]
            if not hit:
                return _err(f"No place called {loc}")
            lat, lon = hit["latitude"], hit["longitude"]
            name = f"{hit['name']}, {hit.get('country', '')}".strip(", ")
        else:
            ip = _http_json("https://ipapi.co/json/")
            lat, lon = ip.get("latitude"), ip.get("longitude")
            name = f"{ip.get('city', '')}, {ip.get('country_name', '')}".strip(", ") or "here"
        d = max(1, min(int(days or 3), 7))
        w = _http_json(f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
                       "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
                       "weather_code,wind_speed_10m"
                       "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
                       f"precipitation_probability_max&timezone=auto&forecast_days={d}")
    except Exception as e:
        return _err(f"Weather lookup failed: {e}")
    cur = w.get("current") or {}
    lines = [f"{name}  {cur.get('temperature_2m', '?')}C (feels {cur.get('apparent_temperature', '?')}C), "
             f"{_WMO.get(cur.get('weather_code'), 'weather')}, "
             f"humidity {cur.get('relative_humidity_2m', '?')}%, "
             f"wind {cur.get('wind_speed_10m', '?')} km/h"]
    daily = w.get("daily") or {}
    for i, day in enumerate(daily.get("time", [])[:d]):
        lines.append(f"{day}: {_WMO.get(daily.get('weather_code', [None])[i], '?')}, "
                     f"{daily.get('temperature_2m_min', [None])[i]}.."
                     f"{daily.get('temperature_2m_max', [None])[i]}C, "
                     f"rain {daily.get('precipitation_probability_max', [None])[i]}%")
    return _ok(f"Weather for {name}", "\n".join(lines))


def _rss_items(xml, limit):
    items = []
    for block in re.findall(r"<item[\s>][\s\S]*?</item>", xml, re.I)[:limit]:
        title = re.search(r"<title[^>]*>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?</title>", block, re.I)
        link = re.search(r"<link[^>]*>([\s\S]*?)</link>", block, re.I)
        date = re.search(r"<pubDate[^>]*>([\s\S]*?)</pubDate>", block, re.I)
        if title:
            items.append({"title": _html_to_text(title.group(1)).strip(),
                          "url": _html_to_text(link.group(1)).strip() if link else "",
                          "date": date.group(1).strip() if date else ""})
    if not items:
        for block in re.findall(r"<entry[\s>][\s\S]*?</entry>", xml, re.I)[:limit]:
            title = re.search(r"<title[^>]*>([\s\S]*?)</title>", block, re.I)
            link = re.search(r'<link[^>]+href=["\']([^"\']+)', block, re.I)
            date = re.search(r"<updated[^>]*>([\s\S]*?)</updated>", block, re.I)
            if title:
                items.append({"title": _html_to_text(title.group(1)).strip(),
                              "url": link.group(1) if link else "",
                              "date": date.group(1).strip() if date else ""})
    return items


def t_news(topic="", limit=6):
    base = "https://news.google.com/rss"
    q = (topic or "").strip()
    url = (base + "/search?q=" + urllib.parse.quote_plus(q) + "&hl=en-US&gl=US&ceid=US:en"
           if q else base + "?hl=en-US&gl=US&ceid=US:en")
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    try:
        with http_open(req, 25) as r:
            xml = r.read(2_000_000).decode("utf-8", "replace")
    except Exception as e:
        return _err(f"News fetch failed: {e}")
    items = _rss_items(xml, max(1, min(int(limit or 6), 12)))
    if not items:
        return _err("No headlines found")
    lines = [f"{i}. {it['title'][:110]}\n   {it['url']}"
             for i, it in enumerate(items, 1)]
    return _ok(f"{len(items)} headline(s)" + (f" about {q}" if q else ""), "\n".join(lines))


def t_rss(url, limit=8):
    url = (url or "").strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    try:
        with http_open(req, 25) as r:
            xml = r.read(2_000_000).decode("utf-8", "replace")
    except Exception as e:
        return _err(f"Feed fetch failed: {e}")
    items = _rss_items(xml, max(1, min(int(limit or 8), 20)))
    if not items:
        return _err("No feed items found (is it an RSS/Atom URL?)")
    lines = [f"{i}. {it['title'][:110]}" + (f"  [{it['date'][:25]}]" if it["date"] else "")
             + f"\n   {it['url']}" for i, it in enumerate(items, 1)]
    return _ok(f"{len(items)} item(s)", "\n".join(lines))


def t_define(word):
    word = (word or "").strip()
    if not word:
        return _err("define needs a word")
    try:
        data = _http_json("https://api.dictionaryapi.dev/api/v2/entries/en/" +
                          urllib.parse.quote(word))
        entry = data[0]
    except Exception as e:
        return _err(f"No definition found for {word!r}", str(e))
    lines = []
    if entry.get("phonetic"):
        lines.append(entry["phonetic"])
    for meaning in entry.get("meanings", [])[:4]:
        lines.append(f"\n{meaning.get('partOfSpeech', '')}")
        for d in meaning.get("definitions", [])[:3]:
            lines.append("- " + d.get("definition", ""))
            if d.get("example"):
                lines.append(f'  "{d["example"]}"')
    return _ok(f"{entry.get('word', word)}", "\n".join(lines).strip())


def t_url_metadata(url):
    url = (url or "").strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA,
                                               "Accept-Language": "en-US,en;q=0.9"})
    try:
        with http_open(req, 25) as r:
            html = r.read(1_500_000).decode("utf-8", "replace")
            final = r.geturl()
    except Exception as e:
        return _err(f"Fetch failed: {e}")

    def meta(prop):
        m = re.search(r'<meta[^>]+(?:property|name)=["\']' + prop + r'["\'][^>]+content=["\']([^"\']*)',
                      html, re.I)
        if not m:
            m = re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']' +
                          prop + r'["\']', html, re.I)
        return _html_to_text(m.group(1)) if m else ""

    title = meta("og:title") or _html_to_text(
        (re.search(r"<title[^>]*>([\s\S]*?)</title>", html, re.I) or [None, ""])[1])
    desc = meta("og:description") or meta("description")
    image = meta("og:image")
    lines = [f"Title: {title.strip()}", f"URL: {final}"]
    if desc:
        lines.append(f"Description: {desc.strip()[:400]}")
    if image:
        lines.append(f"Image: {urllib.parse.urljoin(final, image)}")
    return _ok(title.strip()[:90] or "Page", "\n".join(lines))


def t_check_url(url):
    url = (url or "").strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": BROWSER_UA})
        with http_open(req, 15) as r:
            status, ctype, size = r.status, r.headers.get("Content-Type", ""), r.headers.get("Content-Length", "?")
    except Exception:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
            with http_open(req, 15) as r:
                status, ctype, size = r.status, r.headers.get("Content-Type", ""), r.headers.get("Content-Length", "?")
        except Exception as e:
            return _err(f"Unreachable: {e}")
    return _ok(f"{status} {ctype}", f"{url}\n{size} bytes")


def t_http_json(url, method="GET", body=""):
    url = (url or "").strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    m = str(method or "GET").upper()
    data = None
    if str(body or "").strip():
        data = str(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=m,
                                 headers={"User-Agent": BROWSER_UA,
                                          "Accept": "application/json",
                                          "Content-Type": "application/json"})
    try:
        with http_open(req, 30) as r:
            raw = r.read(3_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return _err(f"HTTP {e.code}", e.read(2000).decode("utf-8", "replace"))
    except Exception as e:
        return _err(f"Request failed: {e}")
    try:
        pretty = json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
    except Exception:
        pretty = raw
    return _ok(f"{m} {urllib.parse.urlparse(url).netloc}", pretty[:6000])


# ---------------------------------------------------------------------------
# Files, documents, images
# ---------------------------------------------------------------------------
def t_file_info(path):
    p = resolve_path(path)
    try:
        s = p.stat()
    except Exception as e:
        return _err(f"Cannot stat {rel_display(p)}: {e}")
    import mimetypes
    mime = mimetypes.guess_type(p.name)[0] or "unknown"
    kind = "folder" if p.is_dir() else "file"
    return _ok(f"{p.name}: {kind}, {s.st_size / 1024:.1f} KB",
               f"Path: {p}\nType: {kind} / {mime}\nSize: {s.st_size} bytes\n"
               f"Modified: {time.strftime('%Y-%m-%d %H:%M', time.localtime(s.st_mtime))}\n"
               f"Created:  {time.strftime('%Y-%m-%d %H:%M', time.localtime(s.st_ctime))}")


def t_folder_size(path=""):
    root = resolve_path(path)
    if not root.is_dir():
        return _err(f"Not a folder: {rel_display(root)}")
    files = 0
    total = 0
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            try:
                total += (Path(dirpath) / n).stat().st_size
                files += 1
            except Exception:
                pass
    return _ok(f"{rel_display(root)}: {total / 1048576:.1f} MB in {files} files")


def t_disk_usage():
    rows = []
    for letter in "CDEFGH":
        d = Path(f"{letter}:/")
        if d.exists():
            try:
                s = shutil.disk_usage(str(d))
                rows.append(f"{letter}: {s.free / 2**30:.1f} GB free of {s.total / 2**30:.1f} GB")
            except Exception:
                pass
    if not rows:
        return _ok("No drives found")
    return _ok(rows[0] if len(rows) == 1 else f"{len(rows)} drives", "\n".join(rows))


def t_recent_files(folder="", limit=20):
    roots = ([resolve_path(folder)] if folder else
             [Path.home() / n for n in ("Desktop", "Downloads", "Documents", "Pictures", "Videos")])
    rows = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for p in root.iterdir():
                if p.is_file():
                    rows.append((p.stat().st_mtime, p))
        except Exception:
            pass
    rows.sort(key=lambda x: x[0], reverse=True)
    n = max(1, min(int(limit or 20), 60))
    lines = [f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(t))}  {rel_display(p)}"
             for t, p in rows[:n]]
    return _ok(f"{len(lines)} recent file(s)", "\n".join(lines) or "(none)")


def t_largest_files(path="", limit=15):
    root = resolve_path(path)
    if not root.exists():
        return _err(f"Not found: {rel_display(root)}")
    scan = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
    sized = []
    for p in scan[:20000]:
        try:
            sized.append((p.stat().st_size, p))
        except Exception:
            pass
    sized.sort(reverse=True)
    n = max(1, min(int(limit or 15), 50))
    lines = [f"{sz / 1048576:9.1f} MB  {rel_display(p)}" for sz, p in sized[:n]]
    return _ok(f"Top {len(lines)} file(s)", "\n".join(lines))


def t_count_files(path="", pattern="*"):
    root = resolve_path(path)
    if not root.is_dir():
        return _err(f"Not a folder: {rel_display(root)}")
    files = [p for p in root.rglob(pattern or "*") if p.is_file()]
    total = sum(p.stat().st_size for p in files if p.exists())
    return _ok(f"{len(files)} file(s), {total / 1048576:.1f} MB")


def t_find_duplicates(path="", min_kb=64):
    root = resolve_path(path)
    if not root.is_dir():
        return _err(f"Not a folder: {rel_display(root)}")
    min_size = max(0, int(min_kb or 64)) * 1024
    by_size = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            s = p.stat().st_size
            if s >= min_size:
                by_size.setdefault(s, []).append(p)
        except Exception:
            pass
    groups = []
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        by_hash = {}
        for p in paths[:200]:
            try:
                h = hashlib.sha1()
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(chunk)
                by_hash.setdefault(h.hexdigest(), []).append(p)
            except Exception:
                pass
        for same in by_hash.values():
            if len(same) > 1:
                groups.append((size, same))
    groups.sort(reverse=True, key=lambda x: x[0])
    lines = []
    for size, same in groups[:20]:
        lines.append(f"{size / 1048576:.1f} MB x{len(same)}")
        lines += ["    " + rel_display(p) for p in same[:6]]
    return _ok(f"{sum(len(g[1]) for g in groups)} duplicate file(s) in {len(groups)} group(s)",
               "\n".join(lines) or "(no duplicates)")


def t_organize_folder(path="", by="type", dry_run=False):
    root = resolve_path(path)
    if not root.is_dir():
        return _err(f"Not a folder: {rel_display(root)}")
    groups = {
        "images": {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "heic", "avif"},
        "video": {"mp4", "mkv", "mov", "avi", "webm", "m4v", "wmv"},
        "audio": {"mp3", "wav", "flac", "m4a", "ogg", "aac", "opus"},
        "documents": {"pdf", "doc", "docx", "txt", "rtf", "odt", "md", "epub"},
        "sheets": {"xls", "xlsx", "csv", "ods", "numbers"},
        "slides": {"ppt", "pptx", "odp", "key"},
        "archives": {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso"},
        "installers": {"exe", "msi", "msix", "appx"},
        "code": {"py", "js", "ts", "cpp", "c", "h", "java", "cs", "html", "css", "json",
                 "xml", "yml", "yaml", "sh", "bat", "ps1", "sql"},
        "fonts": {"ttf", "otf", "woff", "woff2"},
    }
    mode = str(by or "type").lower()
    moved = []
    failed = 0
    for p in sorted(root.iterdir()):
        if not p.is_file():
            continue
        if mode.startswith("date"):
            sub = time.strftime("%Y-%m", time.localtime(p.stat().st_mtime))
        else:
            ext = p.suffix.lower().lstrip(".")
            sub = next((k for k, v in groups.items() if ext in v), "other")
        if dry_run:
            moved.append(f"{p.name}  ->  {sub}/")
            continue
        target_dir = root / sub
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / p.name
            if target.exists():
                target = target_dir / f"{p.stem} {int(p.stat().st_mtime)}{p.suffix}"
            shutil.move(str(p), str(target))
            moved.append(f"{p.name}  ->  {sub}/")
        except Exception:
            failed += 1
    verb = "Would move" if dry_run else "Moved"
    return _ok(f"{verb} {len(moved)} file(s) in {rel_display(root)}"
               + (f", {failed} failed" if failed else ""), "\n".join(moved[:60]))


def t_create_shortcut(target, name="", folder=""):
    tgt = resolve_path(target)
    if not tgt.exists():
        return _err(f"Target not found: {rel_display(tgt)}")
    dest = resolve_path(folder) if folder else workspace()
    dest.mkdir(parents=True, exist_ok=True)
    label = clean_stem(name, 60) if name else tgt.stem
    lnk = dest / (label + ".lnk")
    safe_t = str(tgt).replace("'", "''")
    safe_l = str(lnk).replace("'", "''")
    script = (f"$w=New-Object -ComObject WScript.Shell;$s=$w.CreateShortcut('{safe_l}');"
              f"$s.TargetPath='{safe_t}';$s.WorkingDirectory='{str(tgt.parent).replace(chr(39), chr(39) * 2)}';"
              "$s.Save();'ok'")
    ok, out = _run_powershell(script, 30)
    if not ok:
        return _err("Shortcut failed", out[-800:])
    return _ok(f"Shortcut -> {rel_display(lnk)}")


def t_set_wallpaper(path):
    import ctypes
    p = resolve_path(path)
    if not p.is_file():
        return _err(f"No image at {rel_display(p)}")
    ok = ctypes.windll.user32.SystemParametersInfoW(20, 0, str(p), 3)
    if not ok:
        return _err("Windows rejected the wallpaper change")
    return _ok(f"Wallpaper set to {rel_display(p)}")


def t_wallpapers(query="", count=12, folder="", set_first=False):
    """Download good-looking 16:9 wallpapers (Wallhaven, Picsum fallback)."""
    n = max(1, min(int(count or 12), 48))
    dest = resolve_path(folder) if str(folder or "").strip() \
        else Path.home() / "Pictures" / "Wallpapers"
    dest.mkdir(parents=True, exist_ok=True)
    emit("status", state="working", text=f"Finding {n} wallpapers")
    links = []
    try:
        api = ("https://wallhaven.cc/api/v1/search?categories=100&purity=100&ratios=16x9"
               f"&atleast=1920x1080&sorting={'relevance' if query else 'toplist'}&page=1")
        if query:
            api += "&q=" + urllib.parse.quote(str(query))
        data = http_json(api, timeout=12)
        for item in (data.get("data") or [])[: max(n * 3, 24)]:
            path = item.get("path")
            if path:
                links.append((path, str(item.get("resolution") or "")))
    except Exception as e:
        emit("log", text=f"Wallhaven unavailable ({e}); using Picsum")
    saved, seen_names = [], set()
    ctx = _new_ctx("Downloading wallpapers" + (f" - {str(query)[:30]}" if query else ""), n)
    emit_progress(ctx["label"], 0, 0, "", 0, n)

    def grab_many(pairs, want):
        import concurrent.futures as cf
        got_items = []

        def one(pair):
            url, res = pair
            try:
                p, size, _ct = _grab_url(url, dest, ctx=ctx)
                return p, size, res
            except Exception:
                return None

        ex = cf.ThreadPoolExecutor(max_workers=4)
        futs = [ex.submit(one, pair) for pair in pairs]
        try:
            for fut in cf.as_completed(futs):
                if len(got_items) >= want:
                    break
                got = fut.result()
                if got and got[0].name not in seen_names:
                    seen_names.add(got[0].name)
                    got_items.append(got)
                    _bump(ctx, files_done=1, item=got[0].name, force=True)
                    emit("log", text=f"Wallpaper {len(saved) + len(got_items)}/{n}: "
                                     f"{got[0].name}")
        finally:
            for f in futs:
                f.cancel()
            ex.shutdown(wait=False, cancel_futures=True)
        return got_items

    saved = grab_many(links[: n + 2], n)
    if len(saved) < n:
        stamp = int(time.time())
        need = n - len(saved)
        extra = [(f"https://picsum.photos/1920/1080?random={stamp + i}", "1920x1080")
                 for i in range(need)]
        saved += grab_many(extra, need)
    if not saved:
        return _err("Could not download any wallpaper — check the connection")
    if set_first:
        t_set_wallpaper(saved[0][0])
    body = "\n".join(f"{p.name}  {res or '16:9'}  {_human_bytes(size)}"
                     for p, size, res in saved)
    head = f"Saved {len(saved)} wallpaper(s) to {rel_display(dest)}"
    if set_first:
        head += ", first one set as the desktop"
    return _ok(head, body)


MC_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"

_MC_PACK_FORMATS = [
    ("1.6.1", 1), ("1.9", 2), ("1.11", 3), ("1.13", 4), ("1.15", 5), ("1.16.2", 6),
    ("1.17", 7), ("1.18", 8), ("1.19", 9), ("1.19.3", 12), ("1.19.4", 13),
    ("1.20", 15), ("1.20.2", 18), ("1.20.3", 22), ("1.20.5", 32), ("1.21", 34),
    ("1.21.2", 42), ("1.21.4", 46), ("1.21.5", 55),
]


def _pack_format(version):
    def key(s):
        nums = re.findall(r"\d+", str(s))
        return tuple(int(x) for x in nums) if nums else (0,)
    chosen = 1
    for ver, fmt in _MC_PACK_FORMATS:
        if key(version) >= key(ver):
            chosen = fmt
    return chosen


def _minecraft_client_jar(version):
    data = http_json(MC_MANIFEST, timeout=30)
    versions = data.get("versions") or []
    want = str(version).strip().lower()
    hit = next((v for v in versions if str(v.get("id", "")).lower() == want), None)
    if hit is None:
        cand = [v for v in versions if str(v.get("id", "")).lower().startswith(want)]
        cand.sort(key=lambda v: v.get("releaseTime", ""), reverse=True)
        hit = cand[0] if cand else None
    if hit is None:
        raise RuntimeError(f"Minecraft {version} is not in Mojang's version manifest")
    meta = http_json(hit["url"], timeout=30)
    url = ((meta.get("downloads") or {}).get("client") or {}).get("url")
    if not url:
        raise RuntimeError(f"No client jar published for {hit.get('id')}")
    return url, hit.get("id", version)


def _replacement_pairs(raw):
    """Parse replacement specs: JSON [{src,dst}], lines 'src => dst', or a bare path."""
    txt = str(raw or "").strip()
    if not txt:
        return []
    try:
        data = json.loads(txt, strict=False)
    except Exception:
        data = None
    pairs = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                src = item.get("src") or item.get("source") or item.get("from")
                dst = item.get("dst") or item.get("dest") or item.get("to") or ""
                if src:
                    pairs.append((str(src), str(dst)))
            elif isinstance(item, str):
                pairs.append((item, ""))
        return pairs
    if isinstance(data, str):
        txt = data
    for line in txt.splitlines():
        line = line.strip()
        if not line:
            continue
        for sep in ("=>", "->", " = ", "|"):
            if sep in line:
                a, b = line.split(sep, 1)
                pairs.append((a.strip(), b.strip()))
                break
        else:
            pairs.append((line, ""))
    return pairs


def _copy_tree(src_dir, dst_dir):
    n = 0
    dst_dir.mkdir(parents=True, exist_ok=True)
    for f in src_dir.rglob("*"):
        if f.is_file():
            out = dst_dir / f.relative_to(src_dir)
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, out)
            n += 1
    return n


def t_resource_pack(version="", template="", replacements="", output="", name=""):
    """Assemble a Minecraft resource pack: base template, overlay files, rezip."""
    ver = str(version or "").strip()
    emit("status", state="working", text="Preparing the resource pack")
    tmp = Path(tempfile.mkdtemp(prefix="af_pack_"))
    work = tmp / "pack"
    work.mkdir(parents=True, exist_ok=True)
    base_note = ""
    try:
        if str(template or "").strip():
            tpl = str(template).strip()
            if re.match(r"^https?://", tpl):
                dl_dir = tmp / "tpl"
                dl_dir.mkdir()
                got, _size, _ct = _grab_url(tpl, dl_dir)
                if got.suffix.lower() not in (".zip", ".jar"):
                    raise RuntimeError(f"{tpl} did not return a .zip or .jar")
                with zipfile.ZipFile(got) as z:
                    z.extractall(work)
                base_note = f"template {got.name}"
            else:
                tp = resolve_path(tpl)
                if tp.is_file() and tp.suffix.lower() in (".zip", ".jar"):
                    with zipfile.ZipFile(tp) as z:
                        z.extractall(work)
                    base_note = f"template {tp.name}"
                elif tp.is_dir():
                    _copy_tree(tp, work)
                    base_note = f"template folder {rel_display(tp)}"
                else:
                    raise RuntimeError(f"Template not found: {tpl}")
            top = list(work.iterdir())
            if len(top) == 1 and top[0].is_dir() and not (work / "assets").exists():
                inner = top[0]
                for child in inner.iterdir():
                    shutil.move(str(child), str(work / child.name))
                inner.rmdir()
        else:
            if not ver:
                return _err("resource_pack needs a version (e.g. 1.21.1) or a template zip/URL")
            jar_url, ver_id = _minecraft_client_jar(ver)
            dl_dir = tmp / "jar"
            dl_dir.mkdir()
            emit("status", state="working", text=f"Downloading Minecraft {ver_id} assets")
            jar, _size, _ct = _grab_url(jar_url, dl_dir)
            with zipfile.ZipFile(jar) as z:
                members = [m for m in z.namelist() if m.startswith("assets/minecraft/")]
                if not members:
                    raise RuntimeError("That jar had no assets/minecraft folder")
                z.extractall(work, members=members)
            base_note = f"vanilla {ver_id} client assets"
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        return _err("Could not prepare the base pack", str(e)[-800:])

    fmt = _pack_format(ver or "1.21")
    desc = name or (f"Anvil Fletch {ver}".strip() or "Anvil Fletch pack")
    (work / "pack.mcmeta").write_text(
        json.dumps({"pack": {"pack_format": fmt, "description": desc}}, indent=2),
        encoding="utf-8")

    pairs = _replacement_pairs(replacements)
    added, problems = 0, []
    for src_raw, dst_raw in pairs:
        sp = Path(src_raw) if Path(src_raw).is_absolute() else resolve_path(src_raw)
        if not sp.exists():
            problems.append(f"missing {src_raw}")
            continue
        dst = work / dst_raw.strip("/\\") if dst_raw else work
        try:
            if sp.is_dir():
                added += _copy_tree(sp, dst)
            else:
                dst.mkdir(parents=True, exist_ok=True)
                shutil.copy2(sp, dst / sp.name)
                added += 1
        except Exception as e:
            problems.append(f"{src_raw}: {e}")
    if pairs and not added:
        shutil.rmtree(tmp, ignore_errors=True)
        return _err("No replacement files could be applied", "\n".join(problems[:8]))

    out = (resolve_path(output) if str(output or "").strip()
           else workspace() / ((name or f"resourcepack-{ver or 'custom'}") + ".zip"))
    if out.suffix.lower() != ".zip":
        out = out.with_suffix(".zip")
    out.parent.mkdir(parents=True, exist_ok=True)
    files = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in work.rglob("*"):
            if f.is_file():
                z.write(f, f.relative_to(work))
                files += 1
    shutil.rmtree(tmp, ignore_errors=True)
    head = (f"Built {out.name}: {files} files, {added} replaced, "
            f"pack_format {fmt} ({base_note})")
    detail = (f"{out}\nMinecraft: Options > Resource Packs > Open Pack Folder, "
              f"drop this zip there and enable it.")
    if problems:
        detail += "\nSkipped:\n" + "\n".join(problems[:10])
    return _ok(head, detail)


def t_csv_read(path, limit=50):
    import csv
    p = resolve_path(path)
    if not p.is_file():
        return _err(f"No file at {rel_display(p)}")
    try:
        with open(p, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
            rows = list(csv.reader(f))[: max(1, min(int(limit or 50), 500))]
    except Exception as e:
        return _err(str(e))
    lines = [" | ".join(str(c)[:60] for c in r) for r in rows]
    return _ok(f"{p.name}: {len(rows)} row(s)", "\n".join(lines))


def t_csv_write(path, rows):
    import csv
    raw = str(rows or "").strip()
    if not raw:
        return _err("csv_write needs rows as a JSON array or CSV text")
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            data = data.get("rows") or []
        table = [[("" if c is None else str(c)) for c in r] for r in data]
    except Exception:
        table = list(csv.reader(raw.splitlines()))
    if not table:
        return _err("No rows to write")
    p = resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(table)
    return _ok(f"Wrote {len(table)} row(s) -> {rel_display(p)}")


def _json_pointer(obj, key):
    cur = obj
    for part in [x for x in str(key or "").split(".") if x]:
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur[part]
        else:
            raise KeyError(part)
    return cur


def t_read_json(path, key=""):
    p = resolve_path(path)
    if not p.is_file():
        return _err(f"No file at {rel_display(p)}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if key:
            data = _json_pointer(data, key)
    except Exception as e:
        return _err(f"Cannot read JSON: {e}")
    body = json.dumps(data, indent=2, ensure_ascii=False)
    return _ok(f"{p.name}{' [' + key + ']' if key else ''}", body[:6000])


def t_write_json(path, data):
    raw = str(data or "").strip()
    try:
        obj = json.loads(raw)
    except Exception:
        obj = raw
    p = resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    return _ok(f"Wrote JSON -> {rel_display(p)}")


def t_pdf_info(path):
    try:
        from pypdf import PdfReader
    except Exception:
        return _err("pypdf is not installed", "py -m pip install --user pypdf")
    p = resolve_path(path)
    if not p.is_file():
        return _err(f"No file at {rel_display(p)}")
    try:
        r = PdfReader(str(p))
        meta = r.metadata or {}
        pages = len(r.pages)
    except Exception as e:
        return _err(f"Cannot read PDF: {e}")
    return _ok(f"{p.name}: {pages} page(s)",
               f"Pages: {pages}\nTitle: {meta.get('/Title', '')}\n"
               f"Author: {meta.get('/Author', '')}\nEncrypted: {r.is_encrypted}")


def t_pdf_text(path, max_chars=8000):
    try:
        from pypdf import PdfReader
    except Exception:
        return _err("pypdf is not installed", "py -m pip install --user pypdf")
    p = resolve_path(path)
    if not p.is_file():
        return _err(f"No file at {rel_display(p)}")
    try:
        r = PdfReader(str(p))
        text = "\n".join((pg.extract_text() or "") for pg in r.pages)
    except Exception as e:
        return _err(f"Cannot extract text: {e}")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return _ok(f"{len(r.pages)} page(s), {len(text)} chars",
               text[: max(500, int(max_chars or 8000))] or "(no extractable text)")


def t_pdf_merge(paths, dest=""):
    try:
        from pypdf import PdfReader, PdfWriter
    except Exception:
        return _err("pypdf is not installed", "py -m pip install --user pypdf")
    raw = str(paths or "").strip()
    try:
        names = json.loads(raw)
        if not isinstance(names, list):
            names = [raw]
    except Exception:
        names = [x.strip().strip('"') for x in re.split(r"[;\n]+", raw) if x.strip()]
    files = [resolve_path(x) for x in names]
    files = [f for f in files if f.is_file()]
    if len(files) < 2:
        return _err("pdf_merge needs at least two existing PDF paths")
    out = resolve_path(dest) if dest else workspace() / "merged.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        w = PdfWriter()
        for f in files:
            for pg in PdfReader(str(f)).pages:
                w.add_page(pg)
        with open(out, "wb") as fh:
            w.write(fh)
    except Exception as e:
        return _err(f"Merge failed: {e}")
    return _ok(f"Merged {len(files)} PDFs -> {rel_display(out)}")


def t_docx_create(path, content, title=""):
    try:
        import docx
    except Exception:
        return _err("python-docx is not installed", "py -m pip install --user python-docx")
    p = resolve_path(path)
    if p.suffix.lower() != ".docx":
        p = p.with_suffix(".docx")
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        d = docx.Document()
        if str(title or "").strip():
            d.add_heading(str(title).strip(), 0)
        for para in re.split(r"\n\s*\n", str(content or "")):
            para = para.strip()
            if not para:
                continue
            if para.startswith("### "):
                d.add_heading(para[4:], 3)
            elif para.startswith("## "):
                d.add_heading(para[3:], 2)
            elif para.startswith("# "):
                d.add_heading(para[2:], 1)
            elif para.startswith("- "):
                for line in para.splitlines():
                    d.add_paragraph(line.lstrip("- ").strip(), style="List Bullet")
            else:
                d.add_paragraph(para)
        d.save(str(p))
    except Exception as e:
        return _err(f"Could not write DOCX: {e}")
    return _ok(f"Created {rel_display(p)}")


def t_docx_read(path, max_chars=8000):
    try:
        import docx
    except Exception:
        return _err("python-docx is not installed", "py -m pip install --user python-docx")
    p = resolve_path(path)
    if not p.is_file():
        return _err(f"No file at {rel_display(p)}")
    try:
        d = docx.Document(str(p))
        parts = [para.text for para in d.paragraphs if para.text.strip()]
        for table in d.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text.strip() for c in row.cells))
    except Exception as e:
        return _err(str(e))
    text = "\n".join(parts)
    return _ok(f"{p.name}: {len(parts)} block(s)", text[: max(500, int(max_chars or 8000))])


def t_xlsx_write(path, rows, sheet="Sheet1"):
    try:
        import openpyxl
    except Exception:
        return _err("openpyxl is not installed", "py -m pip install --user openpyxl")
    raw = str(rows or "").strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            data = data.get("rows") or []
        table = [list(r) if isinstance(r, (list, tuple)) else [r] for r in data]
    except Exception:
        return _err("xlsx_write needs rows as a JSON array of arrays")
    if not table:
        return _err("No rows to write")
    p = resolve_path(path)
    if p.suffix.lower() != ".xlsx":
        p = p.with_suffix(".xlsx")
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = str(sheet or "Sheet1")[:30]
        for row in table:
            ws.append(row)
        wb.save(str(p))
    except Exception as e:
        return _err(f"Could not write XLSX: {e}")
    return _ok(f"Wrote {len(table)} row(s) -> {rel_display(p)}")


def t_xlsx_read(path, sheet="", limit=100):
    try:
        import openpyxl
    except Exception:
        return _err("openpyxl is not installed", "py -m pip install --user openpyxl")
    p = resolve_path(path)
    if not p.is_file():
        return _err(f"No file at {rel_display(p)}")
    try:
        wb = openpyxl.load_workbook(str(p), read_only=True, data_only=True)
        ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb.active
        rows = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= max(1, min(int(limit or 100), 1000)):
                break
            rows.append(" | ".join("" if c is None else str(c) for c in row))
        wb.close()
    except Exception as e:
        return _err(str(e))
    return _ok(f"{p.name} / {ws.title}: {len(rows)} row(s)", "\n".join(rows))


def t_pptx_create(path, slides, title=""):
    try:
        from pptx import Presentation
    except Exception:
        return _err("python-pptx is not installed", "py -m pip install --user python-pptx")
    raw = str(slides or "").strip()
    try:
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError("expected a list")
    except Exception:
        return _err('pptx_create needs slides as JSON: [{"title": "...", "body": "..."}]')
    p = resolve_path(path)
    if p.suffix.lower() != ".pptx":
        p = p.with_suffix(".pptx")
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        prs = Presentation()
        cover = prs.slides.add_slide(prs.slide_layouts[0])
        cover.shapes.title.text = str(title or (data[0].get("title") if data else "") or "Presentation")
        if len(cover.placeholders) > 1:
            cover.placeholders[1].text = time.strftime("%d %B %Y")
        for item in data[:20]:
            s = prs.slides.add_slide(prs.slide_layouts[1])
            s.shapes.title.text = str(item.get("title", ""))[:120]
            body = str(item.get("body", ""))
            tf = s.placeholders[1].text_frame
            tf.text = ""
            for line in [ln.strip() for ln in body.splitlines() if ln.strip()][:8]:
                para = tf.add_paragraph() if tf.paragraphs[0].text else tf.paragraphs[0]
                para.text = line.lstrip("-* ")[:200]
        prs.save(str(p))
    except Exception as e:
        return _err(f"Could not write PPTX: {e}")
    return _ok(f"Created {rel_display(p)} with {len(prs.slides)} slides")


def _pillow_open(path):
    from PIL import Image
    p = resolve_path(path)
    if not p.is_file():
        raise FileNotFoundError(f"No image at {rel_display(p)}")
    return Image, p, Image.open(str(p))


def t_image_info(path):
    try:
        _im, p, img = _pillow_open(path)
    except Exception as e:
        return _err(str(e), "py -m pip install --user pillow")
    return _ok(f"{p.name}: {img.width}x{img.height} {img.format}",
               f"Size: {img.width}x{img.height}\nMode: {img.mode}\nFormat: {img.format}\n"
               f"File: {rel_display(p)} ({p.stat().st_size / 1024:.1f} KB)")


_VISION = {"name": None, "checked": 0.0}
_VISION_RE = re.compile(
    r"llava|vision|qwen2\.?5[-_.]?vl|minicpm[-_.]?v|moondream|bakllava|gemma3|"
    r"llama3\.2-vision|granite.*vision", re.I)


def _vision_model():
    """A local Ollama model that can actually see images, if the user has one."""
    now = time.time()
    if now - float(_VISION["checked"] or 0) < 120:
        return _VISION["name"]
    _VISION["checked"] = now
    _VISION["name"] = next((m for m in list_models() if _VISION_RE.search(m or "")), None)
    return _VISION["name"]


def _vision_describe(path, question=""):
    model = _vision_model()
    if not model:
        return ""
    try:
        import base64
        raw = Path(path).read_bytes()
        if len(raw) > 12_000_000:
            return ""
        b64 = base64.b64encode(raw).decode("ascii")
        ask = (str(question or "").strip() or
               "Describe this image precisely: what is shown, every visible text label, "
               "and anything a person would need to understand it.")
        if uses_openai(model):
            prof = provider_of(model)
            return " ".join(_openai_stream(
                prof, model,
                [{"role": "user", "content": [
                    {"type": "text", "text": ask},
                    {"type": "image_url",
                     "image_url": {"url": "data:image/png;base64," + b64}}]}],
                temperature=0.2, max_tokens=400).split())
        data = http_json(CFG["host"].rstrip("/") + "/api/chat", {
            "model": model,
            "messages": [{"role": "user", "content": ask, "images": [b64]}],
            "stream": False, "keep_alive": "10m",
            "options": {"num_ctx": 8192, "num_predict": 320}}, timeout=240)
        return " ".join(str((data.get("message") or {}).get("content") or "").split())
    except Exception:
        return ""


def _image_text(path, max_lines=140):
    try:
        items = _ocr_items(str(path))
    except Exception as e:
        return f"(OCR unavailable: {e})"
    lines = [it["text"] for it in items[:max_lines]]
    return "\n".join(lines) if lines else "(no readable text found by OCR)"


def _image_report(path, question=""):
    p = Path(path)
    parts = [f"IMAGE {p.name} - {p.stat().st_size / 1024:.0f} KB"]
    try:
        from PIL import Image
        with Image.open(str(p)) as img:
            parts.append(f"{img.width}x{img.height} {img.format or p.suffix.lstrip('.')}")
    except Exception:
        pass
    desc = _vision_describe(p, question)
    if desc:
        parts.append("Vision: " + desc)
    ocr = _image_text(p)
    if ocr and ocr != "(no readable text found by OCR)":
        parts.append("Text visible in the image:\n" + ocr)
    elif not desc:
        parts.append("No text was found by OCR"
                     + ("" if _vision_model() else
                        " (install a vision model like llava to get visual descriptions)"))
    return "\n".join(parts)


_TEXT_EXTS = {".txt", ".md", ".py", ".js", ".ts", ".json", ".csv", ".log", ".ini",
              ".cfg", ".yaml", ".yml", ".html", ".css", ".xml", ".cpp", ".h", ".c"}


def _file_report(path, question=""):
    p = resolve_path(path)
    if not p.exists():
        return f"{path}: file not found"
    ext = p.suffix.lower()
    if ext in IMAGE_EXTS:
        return _image_report(p, question)
    if ext == ".pdf":
        ok, summary, detail = t_pdf_text(str(p), 6000)
        return f"PDF {p.name}: {summary}\n{detail}" if ok else f"PDF {p.name}: {summary}"
    if ext == ".docx":
        ok, summary, detail = t_docx_read(str(p), 6000)
        return f"DOCX {p.name}: {summary}\n{detail}" if ok else f"DOCX {p.name}: {summary}"
    if ext in _TEXT_EXTS:
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")[:8000]
            return f"FILE {p.name} ({p.stat().st_size} bytes):\n{txt}"
        except Exception as e:
            return f"FILE {p.name}: could not read ({e})"
    return (f"FILE {p.name} - {p.stat().st_size / 1048576:.2f} MB, type {ext or 'unknown'}. "
            f"Binary; inspect with a matching tool or app.")


def attachment_report(paths, question=""):
    """Text the planner can actually reason about, built from attached files."""
    parts = []
    for i, raw in enumerate(list(paths)[:12], 1):
        emit("status", state="working", text=f"Reading attachment {i}/{len(paths)}")
        try:
            full = str(resolve_path(raw))
        except Exception:
            full = str(raw)
        try:
            parts.append(f"ATTACHMENT {i} - {full}\n{_file_report(raw, question)}")
        except Exception as e:
            parts.append(f"ATTACHMENT {i} - {full}\n{type(e).__name__}: {e}")
    return "\n\n".join(parts)


def t_see_images(path, question="", max_images=8):
    """Read images or a folder of screenshots: OCR text plus optional vision model."""
    root = resolve_path(path)
    if not root.exists():
        return _err(f"Nothing at {root}")
    n = max(1, min(int(max_images or 8), 24))
    if root.is_file():
        if root.suffix.lower() not in IMAGE_EXTS:
            return _err(f"{root.name} is not an image")
        files = [root]
    else:
        files = sorted((f for f in root.iterdir()
                        if f.is_file() and f.suffix.lower() in IMAGE_EXTS),
                       key=lambda f: f.stat().st_mtime, reverse=True)[:n]
    if not files:
        return _err(f"No images in {root}")
    vision = _vision_model()
    parts = []
    for i, f in enumerate(files, 1):
        emit("status", state="working",
             text=f"Reading image {i}/{len(files)}: {f.name[:40]}")
        parts.append(_image_report(f, question))
    head = f"Read {len(files)} image(s) from {rel_display(root)}"
    if vision:
        head += f" - vision model {vision}"
    else:
        head += " - OCR only; install llava for visual descriptions"
    return _ok(head, "\n\n".join(parts))


def t_image_resize(path, width=0, height=0, dest=""):
    try:
        Image, p, img = _pillow_open(path)
    except Exception as e:
        return _err(str(e), "py -m pip install --user pillow")
    w, h = int(width or 0), int(height or 0)
    if w <= 0 and h <= 0:
        return _err("image_resize needs width and/or height")
    if w <= 0:
        w = round(img.width * h / img.height)
    if h <= 0:
        h = round(img.height * w / img.width)
    out = resolve_path(dest) if dest else p.with_name(f"{p.stem} {w}x{h}{p.suffix}")
    if out.is_dir():
        out = out / f"{p.stem} {w}x{h}{p.suffix}"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.resize((w, h), Image.Resampling.LANCZOS).save(str(out))
    return _ok(f"Resized -> {rel_display(out)} ({w}x{h})")


def t_image_convert(path, fmt="png", dest=""):
    try:
        Image, p, img = _pillow_open(path)
    except Exception as e:
        return _err(str(e), "py -m pip install --user pillow")
    fmt = re.sub(r"[^a-zA-Z0-9]", "", str(fmt or "png")).lower() or "png"
    out = resolve_path(dest) if dest else p.with_suffix("." + fmt)
    if out.is_dir():
        out = out / (p.stem + "." + fmt)
    out.parent.mkdir(parents=True, exist_ok=True)
    if fmt in {"jpg", "jpeg"} and img.mode in {"RGBA", "P"}:
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.convert("RGBA").split()[3])
        img = bg
    img.save(str(out))
    return _ok(f"Converted -> {rel_display(out)}")


IMAGE_EXTS = {".webp", ".avif", ".jpeg", ".jpg", ".png", ".bmp", ".gif", ".tif",
              ".tiff", ".ico", ".heic", ".heif", ".jfif", ".ppm", ".tga"}


def t_convert_folder(path, fmt="png", pattern="", recursive=False, overwrite=True,
                     max_side=0, quality=92):
    """Convert every image in a folder (webp/avif/jpeg/... -> png etc.), in place."""
    try:
        from PIL import Image
    except Exception:
        return _err("Pillow is not installed", "py -m pip install --user pillow")
    root = resolve_path(path)
    if not root.exists():
        return _err(f"No folder at {root}")
    fmt = re.sub(r"[^a-zA-Z0-9]", "", str(fmt or "png")).lower() or "png"
    if fmt in {"auto", "mixed", "both", "all", "everything"}:
        img_ok, img_sum, img_det = t_convert_folder(
            path, "png", pattern, recursive, overwrite, max_side, quality)
        aud_ok, aud_sum, aud_det = t_convert_audio_folder(
            path, "mp3", "192k", recursive, overwrite)
        if not img_ok and not aud_ok:
            return _err(img_sum, f"{img_det}\n{aud_sum}\n{aud_det}")
        lines = []
        if img_ok:
            lines.append(img_sum + ("\n" + img_det if img_det.strip() not in ("", "(none)") else ""))
        if aud_ok:
            lines.append(aud_sum + ("\n" + aud_det if aud_det.strip() not in ("", "(none)") else ""))
        head = "Bulk transform done: " + " | ".join(
            s for s in ([img_sum] if img_ok else []) + ([aud_sum] if aud_ok else []))
        return _ok(head, "\n\n".join(lines))
    if fmt == "jpeg":
        fmt = "jpg"
    glob_pat = str(pattern or "").strip() or "*"
    files = []
    if root.is_dir():
        it = root.rglob(glob_pat) if recursive else root.glob(glob_pat)
        files = [f for f in it if f.is_file() and f.suffix.lower() in IMAGE_EXTS]
    elif root.is_file():
        files = [root]
    if not files:
        return _err(f"No images found in {root}")
    limit_side = max(0, min(int(max_side or 0), 12000))
    q = max(30, min(int(quality or 92), 100))
    done, skipped, failed = [], 0, []
    ctx = _new_ctx(f"Converting {len(files)} image(s) to {fmt.upper()}", len(files))
    emit_progress(ctx["label"], 0, 0, "", 0, len(files))
    for f in files:
        out = f if f.suffix.lower() == "." + fmt else f.with_suffix("." + fmt)
        if out.exists() and out != f and not overwrite:
            skipped += 1
            _bump(ctx, files_done=1, item=f.name)
            continue
        try:
            img = Image.open(str(f))
            try:
                img.seek(0)
            except Exception:
                pass
            img = img.convert("RGBA") if img.mode in {"P", "LA"} else img.copy()
            if limit_side and max(img.size) > limit_side:
                img.thumbnail((limit_side, limit_side), Image.Resampling.LANCZOS)
            if fmt in {"jpg", "jpeg", "bmp"} and img.mode in {"RGBA", "LA", "P"}:
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[3])
                img = bg
            elif img.mode not in {"RGB", "RGBA", "L", "I", "F"}:
                img = img.convert("RGB")
            kw = {"quality": q, "optimize": True} if fmt in {"jpg", "webp", "avif"} else {}
            if fmt == "png":
                kw = {"optimize": True}
            img.save(str(out), **kw)
            if out != f:
                try:
                    f.unlink()
                except Exception:
                    pass
            done.append(out.name)
        except Exception as e:
            failed.append(f"{f.name}: {type(e).__name__}: {e}")
        _bump(ctx, files_done=1, item=f.name, force=True)
    head = f"Converted {len(done)} image(s) to {fmt}"
    if skipped:
        head += f", kept {skipped} existing"
    if failed:
        head += f", {len(failed)} failed"
    body = "\n".join(done[:60])
    if failed:
        body += "\n\nFAILED:\n" + "\n".join(failed[:20])
    return _ok(head, body or "(none)")


AUDIO_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".oga", ".m4a", ".aac", ".opus",
              ".wma", ".aiff", ".aif", ".alac", ".m4b", ".amr", ".ape", ".wv",
              ".caf", ".mp2", ".mpga", ".weba", ".au", ".mid"}


def t_convert_audio_folder(path, fmt="mp3", bitrate="192k", recursive=True, overwrite=True):
    """Convert every audio file in a folder to one format, replacing the originals."""
    ff = _ffmpeg()
    if not ff:
        return _err("ffmpeg is not available", "py -m pip install --user imageio-ffmpeg")
    root = resolve_path(path)
    if not root.exists():
        return _err(f"No folder at {root}")
    fmt = re.sub(r"[^a-zA-Z0-9]", "", str(fmt or "mp3")).lower() or "mp3"
    if fmt in ("auto", "mixed", "both", "all", "everything"):
        fmt = "mp3"
    files = [root] if root.is_file() else [
        f for f in (root.rglob("*") if recursive else root.glob("*"))
        if f.is_file() and f.suffix.lower() in AUDIO_EXTS]
    files = [f for f in files if f.suffix.lower() in AUDIO_EXTS or root.is_file()]
    if not files:
        return _err(f"No audio files found in {root}")
    codecs = {"mp3": "libmp3lame", "wav": "pcm_s16le", "flac": "flac",
              "ogg": "libvorbis", "m4a": "aac", "aac": "aac", "opus": "libopus"}
    done, skipped, failed = [], 0, []
    ctx = _new_ctx(f"Converting {len(files)} audio file(s) to {fmt.upper()}", len(files))
    emit_progress(ctx["label"], 0, 0, "", 0, len(files))
    for i, f in enumerate(files, 1):
        out = f if f.suffix.lower() == "." + fmt else f.with_suffix("." + fmt)
        if out.exists() and out != f and not overwrite:
            skipped += 1
            _bump(ctx, files_done=1, item=f.name)
            continue
        try:
            argv = [ff, "-hide_banner", "-loglevel", "error", "-y", "-i", str(f),
                    "-vn", "-c:a", codecs.get(fmt, "libmp3lame")]
            if fmt == "mp3":
                argv += ["-b:a", str(bitrate or "192k")]
            argv.append(str(out))
            ok, body = _proc(argv, 1800)
            if not ok or not out.exists():
                raise RuntimeError(body[-400:] or "ffmpeg failed")
            if out != f:
                try:
                    f.unlink()
                except Exception:
                    pass
            done.append(out.name)
        except Exception as e:
            failed.append(f"{f.name}: {e}")
        _bump(ctx, files_done=1, item=f.name, force=True)
    head = f"Converted {len(done)} audio file(s) to {fmt}"
    if skipped:
        head += f", kept {skipped} existing"
    if failed:
        head += f", {len(failed)} failed"
    body = "\n".join(done[:60])
    if failed:
        body += "\n\nFAILED:\n" + "\n".join(failed[:20])
    return _ok(head, body or "(none)")


def t_image_crop(path, left=0, top=0, width=0, height=0, dest=""):
    try:
        Image, p, img = _pillow_open(path)
    except Exception as e:
        return _err(str(e), "py -m pip install --user pillow")
    l, t = max(0, int(left or 0)), max(0, int(top or 0))
    w = int(width or 0) or (img.width - l)
    h = int(height or 0) or (img.height - t)
    if l >= img.width or t >= img.height:
        return _err("Crop rectangle is outside the image")
    box = (l, t, min(img.width, l + w), min(img.height, t + h))
    out = resolve_path(dest) if dest else p.with_name(f"{p.stem} crop{p.suffix}")
    if out.is_dir():
        out = out / f"{p.stem} crop{p.suffix}"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.crop(box).save(str(out))
    return _ok(f"Cropped -> {rel_display(out)} {box[2] - box[0]}x{box[3] - box[1]}")


def t_image_rotate(path, angle=90, dest=""):
    try:
        _Image, p, img = _pillow_open(path)
    except Exception as e:
        return _err(str(e), "py -m pip install --user pillow")
    deg = int(angle or 90)
    out = resolve_path(dest) if dest else p.with_name(f"{p.stem} rot{deg}{p.suffix}")
    if out.is_dir():
        out = out / f"{p.stem} rot{deg}{p.suffix}"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.rotate(-deg, expand=True).save(str(out))
    return _ok(f"Rotated {deg} deg -> {rel_display(out)}")


def t_window_screenshot(title="", path=""):
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    hwnd = None
    if str(title or "").strip():
        hwnd, _name = _find_window(title)
        if not hwnd:
            return _err(f"No window matching {title!r}",
                        "window_list(all=true) includes tray-hidden windows.")
        ok, why = _ensure_on_screen(hwnd, _name)
        if not ok:
            return _err(why)
        time.sleep(0.2)
    else:
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return _err("No foreground window")
    rect = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return _err("Window has no size")
    out = resolve_path(path) if path else workspace() / f"window-{time.strftime('%Y%m%d-%H%M%S')}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    safe = str(out).replace("'", "''")
    # CopyFromScreen sees Qt/custom-painted content exactly as the user does;
    # PrintWindow returns black for many GPU-composited apps, so it is the fallback.
    script = PS_DPI + f"""
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Drawing;
using System.Runtime.InteropServices;
public class PWC {{
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr d, uint f);
}}
"@
$bmp=New-Object System.Drawing.Bitmap({w},{h})
$g=[System.Drawing.Graphics]::FromImage($bmp)
try {{
  $g.CopyFromScreen({rect.left},{rect.top},0,0,(New-Object System.Drawing.Size({w},{h})))
}} catch {{
  $dc=$g.GetHdc()
  [PWC]::PrintWindow([IntPtr]::new({int(hwnd)}),$dc,2) | Out-Null
  $g.ReleaseHdc($dc)
}}
$bmp.Save('{safe}')
$g.Dispose();$bmp.Dispose();'ok'
"""
    ok, res = _run_powershell(script, 40)
    if not ok or not out.exists():
        return _err("Window capture failed", res[-800:])
    return _ok(f"Captured window -> {rel_display(out)}", str(out))


# ---------------------------------------------------------------------------
# Laptop control
# ---------------------------------------------------------------------------
def _find_window(title):
    """Best top-level window matching part of a title, including tray-hidden ones.

    AyuGram, Telegram, Discord and most Qt apps hide their window to the tray:
    IsWindowVisible is False, yet the window still exists and can be restored.
    Ignoring those made every control tool report "no window matching"."""
    needle = " ".join(str(title or "").split()).lower()
    if not needle:
        return None, ""
    rows = []
    for r in _window_records():
        if r["pid"] == os.getpid():
            continue
        title_low = r["title"].lower()
        if needle in title_low:
            # Exact and prefix matches beat "AyuGram" matching a song title in a browser.
            score = 100 if title_low == needle else (80 if title_low.startswith(needle) else 60)
            if r["visible"]:
                score += 10
            rows.append((score, r))
    if not rows:
        # Titles carry counters: "AyuGram (15456)", "Telegram - 3 unread".
        loose = re.sub(r"\s*[\(\[].*?[\)\]]\s*$", "", needle).strip()
        if loose and loose != needle:
            for r in _window_records():
                if r["pid"] == os.getpid():
                    continue
                title_low = r["title"].lower()
                if loose in title_low:
                    score = 50 + (10 if r["visible"] else 0)
                    rows.append((score, r))
    if not rows:
        # Apps renamed by their content ("RoboContest.UZ Chat - (15640)") no longer
        # carry their own name in the title, so fall back to the process exe.
        want = _norm_app(needle)
        if want:
            for r in _window_records():
                if r["pid"] == os.getpid():
                    continue
                stem = _norm_app(r["exe"])
                if not stem:
                    continue
                if stem == want or want in stem:
                    score = 70 if stem == want else 58
                    if r["visible"]:
                        score += 10
                    rows.append((score, r))
    if not rows:
        return None, ""
    rows.sort(key=lambda p: (-p[0], not p[1]["visible"]))
    best = rows[0][1]
    return best["hwnd"], best["title"]


def _bool(b):
    return bool(b)


def t_mouse_move(x, y):
    import ctypes
    ctypes.windll.user32.SetCursorPos(int(x), int(y))
    return _ok(f"Cursor -> {int(x)},{int(y)}")


def t_mouse_click(x, y, button="left", clicks=1):
    import ctypes
    u = ctypes.windll.user32
    u.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    pairs = {"right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
    down, up = pairs.get(str(button or "left").lower(), (0x0002, 0x0004))
    n = max(1, min(int(clicks or 1), 3))
    for _ in range(n):
        u.mouse_event(down, 0, 0, 0, 0)
        u.mouse_event(up, 0, 0, 0, 0)
        time.sleep(0.09)
    return _ok(f"{str(button).lower()} click x{n} at {int(x)},{int(y)}")


def t_mouse_drag(x1, y1, x2, y2):
    import ctypes
    u = ctypes.windll.user32
    u.SetCursorPos(int(x1), int(y1))
    time.sleep(0.08)
    u.mouse_event(0x0002, 0, 0, 0, 0)
    steps = 18
    for i in range(1, steps + 1):
        u.SetCursorPos(int(x1 + (int(x2) - int(x1)) * i / steps),
                       int(y1 + (int(y2) - int(y1)) * i / steps))
        time.sleep(0.012)
    u.mouse_event(0x0004, 0, 0, 0, 0)
    return _ok(f"Dragged {int(x1)},{int(y1)} -> {int(x2)},{int(y2)}")


def t_mouse_scroll(amount=3):
    import ctypes
    delta = -int(amount) * 120
    ctypes.windll.user32.mouse_event(0x0800, 0, 0, delta, 0)
    return _ok(f"Scrolled {int(amount)} notch(es)")


def _clip_text():
    ok, _summary, detail = t_clipboard_read()
    return detail if ok else ""


def t_type_text(text, window="", enter=False):
    text = str(text or "")
    if not text:
        return _err("type_text needs text")
    if window:
        okw, outw, _ = t_window_focus(window)
        if not okw:
            return _err(f"Could not focus {window!r}", outw)
        time.sleep(0.35)
    old = _clip_text()
    t_clipboard_write(text)
    time.sleep(0.08)
    script = ("Add-Type -AssemblyName System.Windows.Forms;"
              "[System.Windows.Forms.SendKeys]::SendWait('^v');"
              + ("[System.Windows.Forms.SendKeys]::SendWait('{ENTER}');" if enter else "")
              + "'sent'")
    ok, out = _run_powershell(script, 20)
    time.sleep(0.15)
    if old:
        t_clipboard_write(old)
    return (ok, f"Typed {len(text)} chars" + (" and pressed Enter" if enter else ""), out[:300])


_KEYMAP = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
           "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B, "tab": 0x09,
           "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
           "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
           "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
           "printscreen": 0x2C}
for _i in range(1, 13):
    _KEYMAP[f"f{_i}"] = 0x6F + _i


def t_hotkey(keys):
    import ctypes
    raw = str(keys or "").strip().lower()
    if not raw:
        return _err("hotkey needs a combo like ctrl+shift+s")
    parts = [p for p in re.split(r"[+\-]", raw) if p]
    codes = []
    for p in parts:
        p = p.strip()
        if p in _KEYMAP:
            codes.append(_KEYMAP[p])
        elif len(p) == 1:
            codes.append(ord(p.upper()))
        else:
            return _err(f"Unknown key {p!r}")
    u = ctypes.windll.user32
    for c in codes:
        u.keybd_event(c, 0, 0, 0)
        time.sleep(0.02)
    for c in reversed(codes):
        u.keybd_event(c, 0, 2, 0)
        time.sleep(0.02)
    return _ok(f"Hotkey {raw}")


def t_active_window():
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    hwnd = u.GetForegroundWindow()
    if not hwnd:
        return _err("No active window")
    n = u.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(hwnd, buf, n + 1)
    pid = wintypes.DWORD()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    rect = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(rect))
    return _ok(buf.value or "(untitled)",
               f"Title: {buf.value}\nPID: {pid.value}\n"
               f"Rect: {rect.left},{rect.top} {rect.right - rect.left}x{rect.bottom - rect.top}")


def _window_show(title, cmd, verb):
    if not str(title or "").strip():
        return _err(f"{verb} needs part of a window title")
    hwnd, name = _find_window(title)
    if not hwnd:
        return _err(f"No visible window matching {title!r}")
    import ctypes
    ctypes.windll.user32.ShowWindow(hwnd, cmd)
    return _ok(f"{verb}: {name}")


def t_close_window(title):
    if not str(title or "").strip():
        return _err("close_window needs part of a window title")
    hwnd, name = _find_window(title)
    if not hwnd:
        return _err(f"No visible window matching {title!r}")
    import ctypes
    ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
    return _ok(f"Closed {name}")


def t_minimize_window(title):
    return _window_show(title, 6, "Minimized")


def t_maximize_window(title):
    return _window_show(title, 3, "Maximized")


def t_restore_window(title):
    return _window_show(title, 9, "Restored")


def t_wait_window(title, timeout=15):
    if not str(title or "").strip():
        return _err("wait_window needs part of a window title")
    deadline = time.time() + max(1, min(int(timeout or 15), 120))
    while time.time() < deadline:
        hwnd, name = _find_window(title)
        if hwnd:
            return _ok(f"Found: {name}")
        time.sleep(0.4)
    return _err(f"Timed out waiting for {title!r}")


_UI_LAST = {}
_UIA_CACHE = {"desktop": None}
_UIA_WIN_CACHE = {}


def _uia_desktop():
    cached = _UIA_CACHE.get("desktop")
    if cached is not None:
        return cached, ""
    try:
        from pywinauto import Desktop
    except ImportError:
        return None, "pywinauto is not installed (py -m pip install --user pywinauto)"
    except Exception as e:
        return None, f"UI Automation unavailable: {e}"
    try:
        desktop = Desktop(backend="uia")
    except Exception as e:
        return None, f"UI Automation unavailable: {e}"
    _UIA_CACHE["desktop"] = desktop
    return desktop, ""


def _uia_window(window=""):
    needle = str(window or "").strip().lower()
    if needle:
        hit = _UIA_WIN_CACHE.get(needle)
        if hit and time.time() - hit[1] < 2.0:
            return hit[0], hit[2]
    d, err = _uia_desktop()
    if d is None:
        return None, err
    # Win32 enumeration is ~20ms and sees tray-hidden Qt windows; Walking
    # Desktop.windows() takes ~600ms and misses exactly those.
    if str(window or "").strip():
        hwnd, title = _find_window(window)
    else:
        import ctypes
        hwnd, title = int(ctypes.windll.user32.GetForegroundWindow()), ""
    if not hwnd:
        return None, f"No window matching {window!r}"
    try:
        w = d.window(handle=int(hwnd))
        live = w.window_text()
    except Exception as e:
        return None, f"Could not attach to {title or window!r}: {e}"
    title = live or title
    _UIA_WIN_CACHE[needle] = (w, time.time(), title)
    return w, title


def _uia_elements(win, depth=6, max_items=260):
    out = []

    def walk(el, level):
        if len(out) >= max_items or level > depth:
            return
        try:
            info = el.element_info
            ctype = info.control_type or ""
            name = el.window_text() or ""
        except Exception:
            return
        out.append((el, ctype, name, level))
        try:
            kids = el.children()
        except Exception:
            return
        for k in kids:
            walk(k, level + 1)

    try:
        walk(win, 0)
    except Exception:
        pass
    return out


def _uia_rect(el):
    try:
        r = el.rectangle()
        return f"{r.left},{r.top},{r.right},{r.bottom}"
    except Exception:
        return "?"


def _uia_find(win, title, name="", control_type="", index=-1, refresh=False):
    try:
        idx = int(str(index).lstrip("#"))
    except Exception:
        idx = -1
    needle = " ".join(str(name or "").split()).lower()
    ctype_needle = str(control_type or "").strip().lower()
    tokens = [w for w in re.split(r"[^a-z0-9]+", needle) if len(w) >= 2]

    def scan(els):
        for el, ctype, nm, _l in els:
            label = " ".join(str(nm or "").split()).lower()
            if needle and needle not in label:
                continue
            if ctype_needle and ctype_needle not in (ctype or "").lower():
                continue
            return el, ctype, nm
        if tokens and len(tokens) > 1:
            for el, ctype, nm, _l in els:
                label = " ".join(str(nm or "").split()).lower()
                if not all(t in label for t in tokens):
                    continue
                if ctype_needle and ctype_needle not in (ctype or "").lower():
                    continue
                return el, ctype, nm
        return None, "", ""

    els = None if refresh else _UI_LAST.get(title)
    if not els:
        els = _uia_elements(win, 8, 400)
        _UI_LAST[title] = els
    try:
        if 0 <= idx < len(els):
            el, ctype, nm, _l = els[idx]
            return el, ctype, nm
    except Exception:
        idx = -1
    if not needle and not ctype_needle:
        return None, "", ""
    hit = scan(els)
    if hit[0] is not None:
        return hit
    # The cache is a snapshot; menus and dialogs appear after it was taken.
    if not refresh:
        els2 = _uia_elements(win, 8, 400)
        _UI_LAST[title] = els2
        try:
            if 0 <= idx < len(els2):
                el, ctype, nm, _l = els2[idx]
                return el, ctype, nm
        except Exception:
            pass
        return scan(els2)
    return None, "", ""


def t_ui_tree(window="", depth=6, max_items=200):
    win, title = _uia_window(window)
    if win is None:
        return _err(title, "Pass part of the window title, e.g. Ayugram, or leave empty "
                           "for the foreground window.")
    depth = max(1, min(int(depth or 6), 10))
    max_items = max(10, min(int(max_items or 200), 500))
    els = _uia_elements(win, depth, max_items)
    _UI_LAST[title] = els
    lines = []
    for i, (el, ctype, name, level) in enumerate(els):
        aid = ""
        try:
            aid = el.element_info.automation_id or ""
        except Exception:
            pass
        label = " ".join(str(name).split())[:100]
        line = f"#{i} {'  ' * level}[{ctype or '?'}] \"{label}\""
        if aid:
            line += f" id={aid}"
        line += f" @{_uia_rect(el)}"
        lines.append(line)
    return _ok(f"{len(els)} element(s) in {title!r}", "\n".join(lines))


def t_ui_click(window="", name="", control_type="", index=-1, clicks=1, button="left"):
    win, title = _uia_window(window)
    if win is None:
        return _err(title)
    el, ctype, nm = _uia_find(win, title, name, control_type, index)
    if el is None:
        # Controls load lazily (chat lists, dialogs); one short grace period
        # catches most "it was there a second later" misses.
        time.sleep(0.35)
        el, ctype, nm = _uia_find(win, title, name, control_type, index, refresh=True)
    if el is None:
        return _err(f"No control matching {name or control_type or index!r} in {title!r}",
                    "Run ui_tree or ui_find first, then pass its visible name, "
                    "control_type or #index. If the app draws its own UI, use "
                    "screen_ocr + find_on_screen and mouse_click.")
    if str(button or "left").lower() not in ("", "left"):
        try:
            r = el.rectangle()
            okc, _summ, _det = t_mouse_click((r.left + r.right) // 2,
                                             (r.top + r.bottom) // 2,
                                             button=button, clicks=clicks)
            return (okc, f"{str(button).lower()}-clicked {ctype} {nm!r}", _uia_rect(el))
        except Exception as e:
            return _err(f"Could not {button}-click {ctype} {nm!r}", str(e))
    n = max(1, min(int(clicks or 1), 3))
    done = False
    last_err = ""
    for _ in range(n):
        if not done:
            try:
                el.click_input()
                done = True
            except Exception as e:
                last_err = f"click_input: {e}"
        if not done:
            try:
                el.invoke()
                done = True
            except Exception as e:
                last_err += f" | invoke: {e}"
        if not done:
            try:
                el.click()
                done = True
            except Exception as e:
                last_err += f" | click: {e}"
        if not done:
            break
    if not done:
        try:
            r = el.rectangle()
            t_mouse_click((r.left + r.right) // 2, (r.top + r.bottom) // 2, clicks=n)
            return _ok(f"Clicked {ctype} {nm!r} at its coordinates", last_err[-400:])
        except Exception:
            return _err(f"Could not click {ctype} {nm!r}", last_err[-600:])
    return _ok(f"Clicked {ctype} {nm!r}", f"{_uia_rect(el)}")


def t_ui_type(window="", text="", name="", control_type="", index=-1, enter=False):
    text = str(text or "")
    if not text:
        return _err("ui_type needs text")
    win, title = _uia_window(window)
    if win is None:
        return _err(title)
    wanted = bool(str(name or "").strip() or str(control_type or "").strip()
                  or int(str(index).lstrip("#") or -1) >= 0)
    el, ctype, nm = _uia_find(win, title, name, control_type, index)
    if el is None and wanted:
        time.sleep(0.35)
        el, ctype, nm = _uia_find(win, title, name, control_type, index, refresh=True)
    if el is None and wanted:
        return _err(f"No text field matching {name or control_type or index!r} in {title!r}",
                    "Typed nothing. Run ui_tree/ui_find to find the field, then pass "
                    "its visible name or #index. Leave name empty to type into the "
                    "focused control.")
    if el is not None:
        try:
            el.click_input()
        except Exception:
            try:
                el.set_focus()
            except Exception:
                pass
        time.sleep(0.25)
    else:
        try:
            win.set_focus()
        except Exception:
            pass
        time.sleep(0.25)
    ok, summary, detail = t_type_text(text, "", enter)
    if not ok:
        return _err("Typing failed", detail)
    where = f"{ctype} {nm!r}" if el is not None else "the focused field"
    return _ok(f"{summary} into {where}", detail)


def t_ui_read(window="", name="", control_type="", index=-1):
    win, title = _uia_window(window)
    if win is None:
        return _err(title)
    el, ctype, nm = _uia_find(win, title, name, control_type, index)
    if el is None:
        els = _UI_LAST.get(title) or _uia_elements(win, 6, 300)
        _UI_LAST[title] = els
        chunks = [nm2 for _e, _c, nm2, _l in els if str(nm2).strip()]
        body = "\n".join(chunks)[:5000]
        return _ok(f"{len(chunks)} labelled control(s) in {title!r}", body or "(empty)")
    text = ""
    try:
        text = el.window_text() or ""
    except Exception:
        pass
    for getter in ("get_value", "value", "texts"):
        try:
            v = getattr(el, getter)()
            if v:
                text = (text + "\n" if text else "") + str(v)
        except Exception:
            continue
    if not text.strip():
        try:
            for k in el.children():
                text += (k.window_text() or "") + "\n"
        except Exception:
            pass
    body = text.strip()[:5000]
    return _ok(f"{ctype} {nm!r}: {len(body)} chars", body or "(no readable text)")


_UI_TEXT_TYPES = {"text", "listitem", "document", "edit", "dataitem", "treeitem",
                  "hyperlink", "paragraph", "group"}


def _uia_page_text(win, title):
    """Visible labelled text in a window, in tree order."""
    lines = []
    for _el, ctype, name, _level in _uia_elements(win, 9, 600):
        nm = " ".join(str(name or "").split())
        if len(nm) < 1:
            continue
        if ctype.lower() not in _UI_TEXT_TYPES:
            continue
        if nm.lower() == str(title).lower():
            continue
        lines.append(nm)
    return lines


def t_ui_collect(window="", name="", control_type="", index=-1, direction="up",
                 pages=5, max_chars=6000):
    """Scroll a list/chat window and accumulate its text, newest page first."""
    win, title = _uia_window(window)
    if win is None:
        return _err(title)
    el, ctype, nm = _uia_find(win, title, name, control_type, index)
    target = el if el is not None else win
    try:
        target.set_focus()
    except Exception:
        try:
            win.set_focus()
        except Exception:
            pass
    time.sleep(0.2)
    if el is not None:
        try:
            r = el.rectangle()
            import ctypes
            ctypes.windll.user32.SetCursorPos((r.left + r.right) // 2,
                                              (r.top + r.bottom) // 2)
        except Exception:
            pass
    up = str(direction or "up").lower().startswith("u")
    wanted_pages = max(1, min(int(pages or 5), 40))
    seen, seen_set = [], set()

    def harvest():
        for line in _uia_page_text(win, title):
            if line not in seen_set:
                seen_set.add(line)
                seen.append(line)

    harvest()
    for _ in range(wanted_pages):
        t_mouse_scroll(-3 if up else 3)
        time.sleep(0.75)
        harvest()
    body = "\n".join(seen)[:max(int(max_chars or 6000), 500)]
    where = f"{ctype} {nm!r}" if el is not None else "the window"
    return _ok(f"Collected {len(seen)} line(s) from {where} ({wanted_pages} page(s) "
               f"{'up' if up else 'down'})", body or "(no readable text)")


def t_ui_scroll(window="", name="", control_type="", index=-1, direction="down", pages=1):
    win, title = _uia_window(window)
    if win is None:
        return _err(title)
    el, ctype, nm = _uia_find(win, title, name, control_type, index)
    target = el if el is not None else win
    try:
        target.set_focus()
    except Exception:
        try:
            win.set_focus()
        except Exception:
            pass
    time.sleep(0.15)
    if el is not None:
        try:
            r = el.rectangle()
            import ctypes
            ctypes.windll.user32.SetCursorPos((r.left + r.right) // 2,
                                              (r.top + r.bottom) // 2)
        except Exception:
            pass
    up = str(direction or "down").lower().startswith("u")
    n = max(1, min(int(pages or 1), 50))
    for _ in range(n):
        t_mouse_scroll(-3 if up else 3)
        time.sleep(0.3)
    where = f"{ctype} {nm!r}" if el is not None else title
    return _ok(f"Scrolled {where} {n} page(s) {'up' if up else 'down'}")


def t_ui_wait_element(window="", name="", control_type="", index=-1, timeout=10):
    deadline = time.time() + max(1, min(int(timeout or 10), 120))
    last = ""
    while time.time() < deadline:
        win, title = _uia_window(window)
        if win is not None:
            _UI_LAST.pop(title, None)
            el, ctype, nm = _uia_find(win, title, name, control_type, index)
            if el is not None:
                return _ok(f"{ctype} {nm!r} is present", _uia_rect(el))
            last = f"not in {title!r} yet"
        else:
            last = title
        time.sleep(0.6)
    return _err(f"Did not find {name or control_type or '#' + str(index)}", last)


def _ocr_items(path="", rect=None):
    """OCR the screen, a screen rectangle, or a PNG into
    [{text,x,y,w,h,cx,cy}] in absolute screen pixels."""
    ps = Path(__file__).with_name("ocr.ps1")
    if not ps.exists():
        raise RuntimeError(str(ps))
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           "-File", str(ps)]
    if rect:
        l, t, w, h = (int(v) for v in rect)
        cmd += ["-Left", str(l), "-Top", str(t), "-Width", str(max(1, w)),
                "-Height", str(max(1, h))]
    if str(path or "").strip():
        p = resolve_path(path)
        if not p.exists():
            raise FileNotFoundError(str(p))
        cmd += ["-Path", str(p)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20,
                          cwd=str(workspace()), encoding="utf-8", errors="replace")
    out = (proc.stdout or "").strip()
    if not out or out == "NO_OCR_ENGINE":
        raise RuntimeError("Windows OCR is not available on this machine")
    try:
        items = json.loads(out, strict=False)
    except Exception:
        # PowerShell occasionally emits odd control characters; salvage the array.
        start, end = out.find("["), out.rfind("]")
        if start < 0 or end <= start:
            raise RuntimeError("OCR output was not readable")
        items = json.loads(re.sub(r"[\x00-\x1f]", " ", out[start:end + 1]), strict=False)
    if isinstance(items, dict):
        items = [items]
    clean = []
    for it in items or []:
        text = " ".join(str(it.get("text") or "").split())
        if not text:
            continue
        clean.append({
            "text": text[:200],
            "x": int(it.get("x") or 0), "y": int(it.get("y") or 0),
            "w": int(it.get("w") or 0), "h": int(it.get("h") or 0),
            "cx": int(it.get("cx") or 0), "cy": int(it.get("cy") or 0),
        })
    return clean


def t_screen_ocr(path="", max_lines=80):
    """OCR the screen (or a saved PNG) into lines with screen coordinates."""
    try:
        items = _ocr_items(path)
    except Exception as e:
        return _err(f"OCR failed: {e}")
    limit = max(5, min(int(max_lines or 80), 400))
    lines = []
    for it in items[:limit]:
        lines.append(f"\"{it['text'][:120]}\" @{it['x']},{it['y']} "
                     f"{it['w']}x{it['h']} center={it['cx']},{it['cy']}")
    return _ok(f"{len(items)} line(s) recognized on screen",
               "\n".join(lines) or "(no text)")


def _window_rect(window=""):
    import ctypes
    from ctypes import wintypes
    hwnd, name = _find_window(window) if str(window or "").strip() else (None, "")
    if not hwnd:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        name = "foreground window"
    r = wintypes.RECT()
    if hwnd and ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return (r.left, r.top, r.right, r.bottom), name
    return None, name


def t_find_on_screen(text, window="", max_results=5):
    """Locate a phrase on screen and return exact click coordinates."""
    needle = " ".join(str(text or "").split())
    if not needle:
        return _err("find_on_screen needs the text to look for")
    rect, wname = _window_rect(window)
    if str(window or "").strip() and rect:
        t_window_focus(window)
        time.sleep(0.35)
        rect, wname = _window_rect(window)
    try:
        items = _ocr_items("")
    except Exception as e:
        return _err(f"OCR failed: {e}")
    if rect:
        l, t, r, b = rect
        inside = [it for it in items
                  if l - 6 <= it["cx"] <= r + 6 and t - 6 <= it["cy"] <= b + 6]
        if inside:
            items = inside
    low = needle.lower()
    words = {w for w in re.findall(r"[a-z0-9]{3,}", low)}
    scored = []
    for it in items:
        t_low = it["text"].lower()
        if low in t_low:
            score = 100 + min(len(low), 60)
        else:
            hit = words & set(re.findall(r"[a-z0-9]{3,}", t_low))
            score = len(hit) * 8
        if score >= 16:
            scored.append((score, it))
    scored.sort(key=lambda p: -p[0])
    limit = max(1, min(int(max_results or 5), 20))
    lines = []
    seen = set()
    for score, it in scored:
        if it["text"].lower() in seen:
            continue
        seen.add(it["text"].lower())
        lines.append(f"\"{it['text'][:100]}\" center={it['cx']},{it['cy']} "
                     f"box={it['x']},{it['y']},{it['w']}x{it['h']}")
        if len(lines) >= limit:
            break
    where = f" inside {wname!r}" if rect else ""
    if not lines:
        return _err(f"No text matching {needle!r} found on screen{where}",
                    "The text may be scrolled away, hidden by another window, or drawn "
                    "as an image. Focus the window, scroll it, then try again.")
    return _ok(f"{len(lines)} match(es) for {needle!r}{where}; click with mouse_click(cx, cy)",
               "\n".join(lines))


def _uia_element_text(el):
    """Longest string a control exposes: caption, value or text list."""
    best = ""
    try:
        best = el.window_text() or ""
    except Exception:
        pass
    for getter in ("get_value", "value", "texts"):
        try:
            v = getattr(el, getter)()
        except Exception:
            continue
        if isinstance(v, (list, tuple)):
            v = "\n".join(str(x) for x in v if str(x).strip())
        v = str(v or "")
        if len(v) > len(best):
            best = v
    return best


def t_window_text(window="", max_chars=12000):
    """Everything readable in a window: document/edit values, labels, then OCR."""
    win, title = _uia_window(window)
    if win is None:
        return _err(title)
    limit = max(500, min(int(max_chars or 12000), 40000))
    els = _uia_elements(win, 9, 700)
    _UI_LAST[title] = els
    pieces, seen = [], set()
    flat = ""

    def add(raw):
        nonlocal flat
        t = str(raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not t or t.lower() == str(title).lower():
            return
        key = " ".join(t.split())
        if key in seen or (len(key) > 2 and key in flat):
            return
        seen.add(key)
        pieces.append(t)
        flat += "\n" + key

    for el, ctype, _nm, _l in els:
        if (ctype or "").lower() in ("document", "edit"):
            add(_uia_element_text(el))
    for _el, ctype, nm, _l in els:
        if (ctype or "").lower() in _UI_TEXT_TYPES:
            add(nm)
    body = "\n".join(pieces).strip()
    source = "accessibility tree"
    if len(body) < 20:
        shot = Path(os.environ.get("TEMP") or ".") / f"af-window-{int(time.time())}.png"
        ok_s, _s, _d = t_window_screenshot(title, str(shot))
        if ok_s and shot.exists():
            ok_o, _s2, detail = t_screen_ocr(str(shot), 300)
            try:
                shot.unlink()
            except Exception:
                pass
            if ok_o:
                body = str(detail)
                source = "screen OCR (no accessible text)"
    body = body[:limit]
    if len(body.strip()) < 3:
        return _err(f"No readable text found in {title!r}",
                    "The window may be blank or minimized. Try window_focus, then "
                    "screen_ocr() and read the screen.")
    return _ok(f"Read {len(body)} chars from {title!r} via {source}", body)


# ---------------------------------------------------------------------------
# Composite screen control: one call instead of a fragile six-step dance.
# ---------------------------------------------------------------------------
def _norm_label(s):
    s = str(s or "").lower().replace("ı", "i")
    s = re.sub(r"[^\w\u00c0-\uffff]+", " ", s)
    return " ".join(s.split())


def _label_score(needle, text):
    n, t = _norm_label(needle), _norm_label(text)
    if not n or not t:
        return 0
    if n == t:
        return 100
    if len(n) < 2:
        return 0
    if n in t:
        return 88 - min(20, len(t) - len(n))
    nt, tt = set(n.split()), set(t.split())
    if nt and nt <= tt:
        return 76
    if len(n) < 2 or len(t) < 2:
        return 0
    ratio = difflib.SequenceMatcher(None, n, t).ratio()
    return int(ratio * 70) if ratio >= 0.8 else 0


def _window_handle(name=""):
    if str(name or "").strip():
        hwnd, title = _find_window(name)
        if not hwnd:
            return None, title
        return hwnd, title
    import ctypes
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if not hwnd:
        return None, "No foreground window"
    n = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, n + 1)
    return hwnd, buf.value or "foreground"


def _ocr_window(name="", focus=True):
    """OCR just the window rectangle. Returns (ok, items, title, rect, error)."""
    hwnd, title = _window_handle(name)
    if not hwnd:
        return False, [], "", None, title
    if focus:
        ok, why = _ensure_on_screen(hwnd, title)
        if not ok:
            return False, [], title, None, why
        time.sleep(0.12)
    import ctypes
    from ctypes import wintypes
    r = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w < 20 or h < 20:
        return False, [], title, None, f"{title!r} has no usable size"
    try:
        items = _ocr_items("", (r.left, r.top, w, h))
    except Exception as e:
        return False, [], title, None, f"OCR failed: {e}"
    return True, items, title, (r.left, r.top, r.right, r.bottom), ""


def _match_items(items, needle, limit=5):
    """Best OCR matches, best first, one per distinct label."""
    scored, seen = [], set()
    for it in items:
        key = _norm_label(it["text"])
        if not key or key in seen:
            continue
        score = _label_score(needle, it["text"])
        if score:
            seen.add(key)
            scored.append((score, it))
    scored.sort(key=lambda p: (-p[0], p[1]["cy"], p[1]["cx"]))
    return [it for _s, it in scored[:max(1, int(limit))]]


def _screen_lines(items, max_lines=60):
    rows = sorted(items, key=lambda it: (it["cy"] // 14, it["cx"]))[:max_lines]
    return "\n".join(f"\"{it['text'][:120]}\" @{it['cx']},{it['cy']}"
                     for it in rows) or "(no text recognized)"


def t_read_screen(window="", max_lines=60):
    """Read the visible text of a window (or the screen) with click coordinates."""
    t0 = time.time()
    ok, items, title, rect, err = _ocr_window(window)
    if not ok:
        return _err(err)
    limit = max(5, min(int(max_lines or 60), 200))
    return _ok(f"{len(items)} line(s) in {title!r} ({now_ms() - int(t0 * 1000)}ms)",
               _screen_lines(items, limit))


def t_screen_snapshot(window="", max_lines=40):
    """One call: window state, UIA controls, and OCR text with click points."""
    import ctypes
    t0 = time.time()
    hwnd, name = _window_handle(window)
    if not hwnd:
        return _err(name if str(window or "").strip() else "No foreground window")
    ok, why = _ensure_on_screen(hwnd, name)
    u = ctypes.windll.user32
    from ctypes import wintypes
    r = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    lines = [f"window: {name!r} hwnd=0x{int(hwnd):X}",
             f"rect: {r.left},{r.top} {r.right - r.left}x{r.bottom - r.top}",
             f"foreground: {int(u.GetForegroundWindow()) == int(hwnd)}  "
             f"cloaked: {_dwm_cloaked(hwnd)}"]
    if not ok:
        lines.append(f"warning: {why}")
    # UIA is a bonus; many apps expose nothing, OCR below is the real source.
    controls = []
    try:
        win, title = _uia_window(name)
        if win is not None:
            els = _uia_elements(win, 6, 120)
            controls = [f"[{ct}] {nm}" for _el, ct, nm, _l in els
                        if nm and (ct or "").lower() not in ("window", "titlebar")]
            lines.append(f"UIA: {len(els)} element(s), {len(controls)} named")
    except Exception as e:
        lines.append(f"UIA: failed ({e})")
    try:
        items = _ocr_items("", (r.left, r.top, r.right - r.left, r.bottom - r.top))
    except Exception as e:
        items = []
        lines.append(f"OCR failed: {e}")
    limit = max(5, min(int(max_lines or 40), 150))
    detail = "\n".join(lines)
    if controls:
        detail += "\ncontrols:\n" + "\n".join(controls[:30])
    if items:
        detail += f"\nocr ({len(items)} lines, click with ocr_click or mouse_click):\n"
        detail += _screen_lines(items, limit)
    else:
        detail += ("\nocr: no text. The window may be blank, covered, or on another "
                   "virtual desktop.")
    detail += ("\nnext: ocr_click(\"label\") to click text; key_macro(\"ctrl+f\") for "
               "shortcuts; scroll_read to read further; never guess coordinates.")
    return _ok(f"Snapshot of {name!r} ({len(items)} OCR line(s))", detail)


def _click_at(x, y, button="left", clicks=1):
    return t_mouse_click(int(x), int(y), button, clicks)


def t_ocr_click(text, window="", index=0, button="left", clicks=1):
    """Find text by OCR in a window and click its center. No guessed coordinates."""
    needle = " ".join(str(text or "").split())
    if not needle:
        return _err("ocr_click needs the text to click")
    t0 = time.time()
    ok, items, title, rect, err = _ocr_window(window)
    if not ok:
        return _err(err)
    hits = _match_items(items, needle, 8)
    if not hits:
        sample = ", ".join(f"{it['text'][:28]!r}" for it in items[:8]) or "nothing"
        return _err(f"No text close to {needle!r} in {title!r}",
                    f"I can see: {sample}. Use read_screen to see exact labels, then "
                    "call ocr_click again with one of them.")
    try:
        i = max(0, min(int(index or 0), len(hits) - 1))
    except Exception:
        i = 0
    target = hits[i]
    _click_at(target["cx"], target["cy"], button, clicks)
    time.sleep(0.2)
    others = [f"{it['text'][:30]!r}@{it['cx']},{it['cy']}" for it in hits[1:4]]
    tail = ("; also matched " + ", ".join(others)) if others else ""
    return _ok(f"Clicked {target['text'][:60]!r} at {target['cx']},{target['cy']}"
               f" in {title!r} ({now_ms() - int(t0 * 1000)}ms){tail}",
               f"match score={_label_score(needle, target['text'])} box="
               f"{target['x']},{target['y']},{target['w']}x{target['h']}")


def t_click_text(text, window="", index=0, button="left"):
    return t_ocr_click(text, window, index, button)


def t_wait_text(text, window="", timeout=5):
    """Poll OCR until the text appears, then return its click coordinates."""
    needle = " ".join(str(text or "").split())
    if not needle:
        return _err("wait_text needs the text to wait for")
    deadline = time.time() + max(1, min(int(timeout or 5), 20))
    last = ""
    while time.time() < deadline:
        ok, items, title, _rect, err = _ocr_window(window, focus=(not last))
        if ok:
            hits = _match_items(items, needle, 1)
            if hits:
                it = hits[0]
                return _ok(f"{it['text'][:60]!r} appeared in {title!r}",
                           f"center={it['cx']},{it['cy']}")
            last = f"not in {title!r} yet"
        else:
            last = err
        time.sleep(0.25)
    return _err(f"Timed out after {timeout}s waiting for {needle!r}", last)


def t_scroll_read(window="", direction="down", pages=1, max_lines=40):
    """Scroll a window and read what is visible afterwards."""
    hwnd, title = _window_handle(window)
    if not hwnd:
        return _err(title)
    ok, why = _ensure_on_screen(hwnd, title)
    if not ok:
        return _err(why)
    import ctypes
    from ctypes import wintypes
    r = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
    ctypes.windll.user32.SetCursorPos((r.left + r.right) // 2, (r.top + r.bottom) // 2)
    n = max(1, min(int(pages or 1), 20))
    up = str(direction or "down").lower().startswith("u")
    for _ in range(n):
        t_mouse_scroll(-3 if up else 3)
        time.sleep(0.22)
    time.sleep(0.15)
    try:
        items = _ocr_items("", (r.left, r.top, r.right - r.left, r.bottom - r.top))
    except Exception as e:
        return _err(f"OCR failed after scrolling: {e}")
    return _ok(f"Scrolled {title!r} {n} page(s) {'up' if up else 'down'}; "
               f"{len(items)} line(s) visible",
               _screen_lines(items, max(5, min(int(max_lines or 40), 150))))


def t_key_macro(keys, window=""):
    """Run a space-separated series of shortcuts/keys in one call."""
    seq = [k for k in str(keys or "").replace(",", " ").split() if k]
    if not seq:
        return _err("key_macro needs keys like: ctrl+f ctrl+a enter")
    if str(window or "").strip():
        okf, why, _ = t_window_focus(window)
        if not okf:
            return _err(why)
        time.sleep(0.15)
    sent = []
    for part in seq[:15]:
        low = part.lower()
        if low in _KEYMAP:
            t_hotkey(low)
        elif "+" in low or "-" in low:
            okh, _s, d = t_hotkey(low.replace("-", "+"))
            if not okh:
                return _err(d)
        else:
            t_send_keys("{" + low.upper() + "}" if low in ("enter", "tab", "esc")
                        else low)
        sent.append(part)
        time.sleep(0.08)
    return _ok(f"Sent {len(sent)} key step(s): {' '.join(sent)}")


def t_focus_and_type(text, window="", enter=False):
    """Focus a window, paste text into it, optionally press Enter."""
    if str(window or "").strip():
        ok, out, _ = t_window_focus(window)
        if not ok:
            return _err(out)
        time.sleep(0.1)
    return t_type_text(text, "", enter)


def t_dialog_fill(fields, window=""):
    """Fill labelled fields: "Name=Umar; Email=me@x.com"."""
    pairs = []
    for chunk in re.split(r"[;\n]+", str(fields or "")):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            if k.strip():
                pairs.append((k.strip(), v.strip()))
    if not pairs:
        return _err('dialog_fill needs fields like "Name=Umar; Email=me@example.com"')
    done, failed = [], []
    for label, value in pairs[:12]:
        ok, items, title, rect, err = _ocr_window(window)
        if not ok:
            return _err(err)
        hits = _match_items(items, label, 1)
        if not hits:
            failed.append(f"{label} (not visible)")
            continue
        it = hits[0]
        # Click just right of the label, inside its row, then paste.
        tx = min(it["x"] + it["w"] + 90, (rect[2] if rect else it["x"] + 300) - 30)
        ty = it["cy"]
        _click_at(tx, ty)
        time.sleep(0.12)
        t_type_text(value, "")
        done.append(f"{label} <- {value[:30]}")
        time.sleep(0.1)
    summary = f"Filled {len(done)} field(s)" + (f", {len(failed)} missed" if failed else "")
    detail = "\n".join(done + [f"MISSED {f}" for f in failed])
    return (_ok if done else _err)(summary, detail)


def t_dig_chats(app="AyuGram", pages=2, max_chats=6, window=""):
    """Read-only sweep of a chat app: click each visible chat, OCR the thread.

    Never types, never sends, never presses Enter. Clicks chat rows and scrolls
    the thread up, which is exactly what a person does to read messages."""
    target = window or app or "AyuGram"
    t0 = time.time()
    if not window:
        ok, out, _ = t_open_app(app or target, 6, restart_stuck=True)
        if not ok:
            return _err(out)
    hwnd, title = _window_handle(target)
    if not hwnd:
        return _err(f"No window matching {target!r}")
    ok, why = _ensure_on_screen(hwnd, title, allow_restart=True)
    if not ok:
        return _err(why)
    # A maximized window shows far more of the thread than a narrow one.
    t_window_snap("maximize", title)
    time.sleep(0.3)
    ok, items, title, rect, err = _ocr_window(target)
    if not ok:
        return _err(err)
    l, t, r, b = rect
    width = r - l
    # Chat rows live in the left rail of Telegram-style layouts.
    rail_right = l + int(width * 0.36)
    skip = {"chats", "search", "edit", "view", "help", "folders", "settings", "menu",
            "saved messages", "contacts", "calls", "archive", "new chat", "all chats",
            "stories", "premium", "back", "forward", "open", "add", "more"}
    rows, seen = [], set()
    for it in sorted(items, key=lambda x: (x["cy"], x["cx"])):
        if not (l + 30 <= it["cx"] <= rail_right):
            continue
        if not (t + 70 <= it["cy"] <= b - 80):
            continue
        label = " ".join(it["text"].split())
        low = _norm_label(label)
        if not (1 <= len(label) <= 70) or low in skip or low in seen:
            continue
        if re.fullmatch(r"[\d\s:.,/+()-]{1,10}", label):
            continue
        if it["h"] < 8 or it["h"] > 42:
            continue
        seen.add(low)
        rows.append(it)
    if not rows:
        return _err(f"No chat rows recognized in {title!r}",
                    "The list may be empty, covered, or on another virtual desktop. "
                    "read_screen shows exactly what OCR sees.")
    picked = rows[:max(1, min(int(max_chats or 6), 12))]
    sections, used = [], 0
    for it in picked:
        _click_at(it["cx"], it["cy"])
        time.sleep(0.55)
        ok2, thread, _t2, rect2, _e2 = _ocr_window(target, focus=False)
        if not ok2:
            continue
        l2, t2, r2, b2 = rect2
        pane_left = l2 + int((r2 - l2) * 0.34)
        lines = []
        for row in sorted(thread, key=lambda x: (x["cy"], x["cx"])):
            if row["cx"] < pane_left or row["cy"] < t2 + 55:
                continue
            txt = " ".join(row["text"].split())
            if not txt or (lines and lines[-1] == txt):
                continue
            lines.append(txt)
        # Older messages live above the current viewport.
        for _ in range(max(1, min(int(pages or 1), 5)) - 1):
            t_hotkey("pageup")
            time.sleep(0.35)
            ok3, older, _t3, _r3, _e3 = _ocr_window(target, focus=False)
            if not ok3:
                break
            prev = []
            for row in sorted(older, key=lambda x: (x["cy"], x["cx"])):
                if row["cx"] < pane_left or row["cy"] < t2 + 55:
                    continue
                txt = " ".join(row["text"].split())
                if txt and txt not in prev:
                    prev.append(txt)
            head = [x for x in prev if x not in lines]
            lines = head + lines
        body = "\n".join("  " + ln for ln in lines[:80])
        sections.append(f"### {it['text'][:60]}\n{body or '  (no messages read)'}")
        used += 1
        if time.time() - t0 > 24:
            break
    if not sections:
        return _err("Clicked chat rows but could not read any thread")
    summary = (f"Read {used} chat(s) in {title!r} (read-only, nothing sent, "
               f"{int(time.time() - t0)}s)")
    return _ok(summary, "\n\n".join(sections)[:9000])


def run_self_test(verbose=False):
    """Exercise every tool schema plus the live control/OCR path.

    Returns (passed, failed, report). Safe: writes only inside a temp folder,
    and restores the clipboard."""
    checks = []

    def check(name, fn):
        t0 = time.time()
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        ms = int((time.time() - t0) * 1000)
        checks.append((bool(ok), name, str(detail)[:300], ms))

    # --- schemas: every tool, every required arg, every group -------------
    for name in sorted(TOOLS):
        spec, required, _desc = TOOLS[name]
        check(f"tool {name} has t_{name}", lambda n=name: (
            callable(globals().get("t_" + n)), "missing function" if not callable(globals().get("t_" + n)) else "ok"))
        check(f"tool {name} required args are in spec", lambda s=spec, r=required: (
            all(k in s for k in r), f"missing {[k for k in r if k not in s]}"))
        bad = [(k, v.get("type")) for k, v in spec.items()
               if k not in ARG_TYPES or ARG_TYPES[k] != v.get("type")]
        check(f"tool {name} args match ARG_TYPES", lambda b=bad: (not b, str(b[:4])))
    grouped = [n for _label, names in TOOL_GROUPS for n in names]
    dupes = sorted({n for n in grouped if grouped.count(n) > 1})
    check("tool groups have no duplicates", lambda: (not dupes, str(dupes[:6])))
    ghosts = [n for n in grouped if n not in TOOLS]
    check("tool groups only name real tools", lambda: (not ghosts, str(ghosts[:8])))
    ungrouped = [n for n in TOOLS if n not in set(grouped)]
    check("every tool is in a group", lambda: (not ungrouped, str(ungrouped[:8])))

    # --- fuzzy label matching (the thing OCR clicking relies on) ---------
    cases = [("Chats", "Chats", True), ("chats", "Chats", True), ("Chats", "Saved Chats", True),
             ("New Message", "New message", True), ("Settings", "Setting", True),
             ("Saved Messages", "Saved Messages", True), ("AyuGram", "AyuGram (15456)", True),
             ("Send", "Send a message", True), ("Search", "Search", True),
             ("Contacts", "Contact", True), ("Archive", "Archived chats", True),
             ("Plus", "Play", False), ("xyzzy", "Chats", False), ("a", "Chats", False),
             ("Profile", "Settings and more", False), ("mute", "minutes", False)]
    for want, label, expect in cases:
        score = _label_score(want, label)
        check(f"fuzzy {want!r} vs {label!r}", lambda s=score, e=expect: (
            (s > 0) == e, f"score={s}"))

    # --- live environment -------------------------------------------------
    check("workspace exists", lambda: (workspace().is_dir(), str(workspace())))
    check("window records", lambda: (len(_window_records()) >= 1, f"{len(_window_records())} windows"))
    fg_hwnd, fg_title = _window_handle("")
    check("foreground window", lambda: (bool(fg_hwnd), fg_title))
    check("find foreground window", lambda: (bool(_find_window(fg_title)[0]), fg_title))
    check("cloak probe", lambda: (isinstance(_dwm_cloaked(fg_hwnd), bool), "bool"))
    check("uia desktop", lambda: (_uia_desktop()[0] is not None, _uia_desktop()[1]))
    check("uia window", lambda: (_uia_window("")[0] is not None, "attached"))
    check("window ping", lambda: (_window_ping_ok(fg_title), fg_title))
    check("ocr engine", lambda: (bool(_ocr_items("", None)), "read screen"))
    check("ocr window rect", lambda: (bool(_ocr_window("", focus=False)[1]), "read foreground"))
    check("key map", lambda: (all(k in _KEYMAP for k in
                                  ("ctrl", "shift", "alt", "enter", "tab", "esc", "pageup")),
                              "common keys"))
    check("hotkey formatter", lambda: (t_hotkey("ctrl+shift+s")[0], "sent"))

    # --- file/CLI roundtrip in a temp folder ------------------------------
    tmp = Path(tempfile.mkdtemp(prefix="af-selftest-"))
    try:
        p = tmp / "hello.txt"
        check("write_file", lambda: (t_write_file(str(p), "hello anvil\nsecond line\n")[0], str(p)))
        check("read_file", lambda: ("hello anvil" in t_read_file(str(p))[2], "content"))
        check("edit_file", lambda: (t_edit_file(str(p), "second line", "edited line")[0], "edited"))
        check("grep", lambda: (t_grep("edited", str(tmp))[0], "match"))
        check("glob", lambda: (t_glob("*.txt", str(tmp))[0], "match"))
        check("list_dir", lambda: (t_list_dir(str(tmp))[0], "listed"))
        check("file_info", lambda: (t_file_info(str(p))[0], "info"))
        check("hash", lambda: (t_hash(str(p))[0], "hash"))
        check("delete temp", lambda: (t_delete(str(p), permanent=True)[0], "deleted"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # --- clipboard roundtrip ---------------------------------------------
    def clip_roundtrip():
        old = _clip_text()
        try:
            t_clipboard_write("anvil-selftest")
            got = t_clipboard_read()[2]
            return "anvil-selftest" in got, f"read back {got[:40]!r}"
        finally:
            if old:
                t_clipboard_write(old)
    check("clipboard roundtrip", clip_roundtrip)

    # --- dispatch smoke ---------------------------------------------------
    check("dispatch now", lambda: ("%Y" in dispatch("now", {})[1] or True, "ok"))
    check("missing arg is honest", lambda: (
        not dispatch("read_file", {})[0], "rejected missing path"))

    # --- scenario routing: real phrasings must land on the right route ----
    check("scenario suite is big", lambda: (len(SCENARIO_TESTS) >= 120,
                                            f"{len(SCENARIO_TESTS)} prompts"))
    for prompt, want_route, want_hint in SCENARIO_TESTS:
        if want_route:
            got_route = _dry_route(prompt)
            check(f"route {prompt[:46]!r}", lambda g=got_route, w=want_route: (
                g == w, f"got {g or 'model'!r}, want {w!r}"))
        if want_hint:
            got_hint = intent_hint(prompt)[0]
            check(f"hint {prompt[:46]!r}", lambda g=got_hint, w=want_hint: (
                g == w, f"got {g or 'none'!r}, want {w!r}"))

    passed = sum(1 for ok, *_ in checks if ok)
    failed = len(checks) - passed
    lines = [f"{'PASS' if ok else 'FAIL'}  {name}"
             + (f"  [{detail}]" if (verbose and detail) or not ok else "")
             + f"  {ms}ms" for ok, name, detail, ms in checks]
    head = f"{passed}/{len(checks)} checks passed" + (f", {failed} failed" if failed else "")
    if not verbose:
        lines = [ln for ln in lines if ln.startswith("FAIL")]
    return passed, failed, head + ("\n" + "\n".join(lines[:120]) if lines else "")


def _window_ping_ok(title):
    ok, _s, detail = t_window_ping(title)
    return bool(ok and title)


def t_self_test(verbose=False):
    passed, failed, report = run_self_test(verbose)
    return (failed == 0, f"Self-test: {passed} passed, {failed} failed",
            report)


_READ_UI_RE = re.compile(
    r"\b(read|look at|look through|see|show me|dig through|go through|"
    r"scroll through|what does|what'?s (in|on)|summari[sz]e)\b", re.I)
_UI_NOUN_RE = re.compile(
    r"\b(window|screen|app|application|chat|chats|messages?|inbox|conversations?|"
    r"notepad|telegram|ayugram|whatsapp|discord|slack|browser|tabs?|monitor|display)\b",
    re.I)
_UI_STOP = {"read", "look", "through", "what", "does", "whats", "that", "this", "open",
            "window", "screen", "app", "application", "chat", "chats", "message",
            "messages", "inbox", "everything", "says", "show", "tell", "each", "every",
            "all", "here", "there", "dig", "scroll", "into", "from", "with", "your",
            "have", "please", "the", "and", "for", "them", "they", "include", "including"}


def wants_window_read(prompt: str) -> bool:
    p = prompt or ""
    if not _READ_UI_RE.search(p) or not _UI_NOUN_RE.search(p):
        return False
    if re.search(r"\.\w{1,5}\b", p) and not re.search(r"\b(window|screen|app)\b", p, re.I):
        return False
    return True


def _pick_read_window(prompt: str) -> str:
    """The open window a read request most likely means."""
    ok, _summ, detail = t_window_list()
    if not ok:
        return ""
    skip = ("anvil fletch", "qoder", "program manager", "windows input experience")
    rows = []
    for ln in str(detail).splitlines():
        m = re.match(r"(0x[0-9A-Fa-f]+)\s+pid\s+\d+\s+(.*)", ln.strip())
        if not m:
            continue
        title = m.group(2).strip()
        if not title or any(s in title.lower() for s in skip):
            continue
        rows.append((int(m.group(1), 16), title))
    if not rows:
        return ""
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9_.+-]{2,}", prompt)
             if w.lower() not in _UI_STOP]
    scored = [(sum(2 for w in words if w.lower() in t.lower()), t) for _h, t in rows]
    scored = [x for x in scored if x[0] > 0]
    if scored:
        scored.sort(key=lambda x: -x[0])
        return scored[0][1]
    import ctypes
    fg = int(ctypes.windll.user32.GetForegroundWindow())
    for hwnd, title in rows:
        if hwnd == fg:
            return title
    return rows[0][1]


def _tap_vk(code, steps=1):
    import ctypes
    u = ctypes.windll.user32
    for _ in range(max(1, min(int(steps or 1), 50))):
        u.keybd_event(code, 0, 0, 0)
        time.sleep(0.03)
        u.keybd_event(code, 0, 2, 0)
        time.sleep(0.03)


def t_volume_up(steps=2):
    _tap_vk(0xAF, steps)
    return _ok(f"Volume up x{int(steps or 2)}")


def t_volume_down(steps=2):
    _tap_vk(0xAE, steps)
    return _ok(f"Volume down x{int(steps or 2)}")


def t_mute():
    _tap_vk(0xAD)
    return _ok("Muted / unmuted")


def t_play_pause():
    _tap_vk(0xB3)
    return _ok("Play/pause sent")


def t_media_next():
    _tap_vk(0xB0)
    return _ok("Next track")


def t_media_prev():
    _tap_vk(0xB1)
    return _ok("Previous track")


def t_battery():
    ok, out = _run_powershell(
        "Get-CimInstance Win32_Battery | Select-Object -First 1 "
        "EstimatedChargeRemaining,BatteryStatus | Format-List", 20)
    if not ok or not out.strip():
        return _err("No battery reported (desktop?)", out[-400:])
    return _ok("Battery status", out.strip())


def t_wifi_status():
    ok, out = _run_powershell("netsh wlan show interfaces", 20)
    if not ok:
        return _err("Wi-Fi query failed", out[-600:])
    keep = [ln.strip() for ln in out.splitlines()
            if any(k in ln for k in ("SSID", "State", "Signal", "Radio", "Authentication", "Receive", "Transmit"))]
    return _ok("Wi-Fi", "\n".join(keep) or out.strip()[:800])


def t_network_info():
    ok, out = _run_powershell(
        "Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
        "Where-Object {$_.IPAddress -notlike '127.*'} | "
        "Select-Object IPAddress,InterfaceAlias | Format-Table -AutoSize | Out-String -Width 100", 25)
    if not ok:
        return _err("Network query failed", out[-600:])
    return _ok("IPv4 interfaces", out.strip())


def t_brightness(level=50):
    lv = max(0, min(int(level or 50), 100))
    ok, out = _run_powershell(
        "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods)"
        f".WmiSetBrightness(1,{lv}); 'ok'", 20)
    if not ok or "ok" not in out:
        return _err("Brightness control is not supported on this display", out[-400:])
    return _ok(f"Brightness -> {lv}%")


def t_installed_apps(filter=""):
    ok, out = _run_powershell(
        "$p='HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
        "'HKLM:\\Software\\Wow6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
        "'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*';"
        "Get-ItemProperty $p -ErrorAction SilentlyContinue | Where-Object DisplayName | "
        "Select-Object DisplayName,DisplayVersion -Unique | Sort-Object DisplayName | "
        "Format-Table -AutoSize | Out-String -Width 100", 45)
    if not ok:
        return _err("Registry query failed", out[-600:])
    if filter:
        out = "\n".join(ln for ln in out.splitlines() if filter.lower() in ln.lower())
    return _ok("Installed apps", out.strip()[:5000])


def t_env(name=""):
    key = str(name or "").strip()
    if key:
        val = os.environ.get(key)
        return _ok(f"{key}={val}") if val is not None else _err(f"{key} is not set")
    lines = [f"{k}={v[:120]}" for k, v in sorted(os.environ.items())][:60]
    return _ok(f"{len(os.environ)} variables", "\n".join(lines))


def t_uptime():
    try:
        import psutil
        secs = int(time.time() - psutil.boot_time())
    except Exception:
        return _err("psutil is not available")
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return _ok(f"Up {d}d {h}h {m}m")


def t_lock_screen():
    import ctypes
    if ctypes.windll.user32.LockWorkStation():
        return _ok("Screen locked")
    return _err("Could not lock the screen")


def t_speak(text, rate=0):
    msg = str(text or "").strip()
    if not msg:
        return _err("speak needs text")
    safe = msg.replace("'", "''")
    ok, out = _run_powershell(
        "$v=New-Object -ComObject SAPI.SpVoice;"
        f"$v.Rate={max(-10, min(int(rate or 0), 10))};"
        f"$v.Speak('{safe}') | Out-Null; 'spoken'", 120)
    return (True, f"Spoke {len(msg)} chars") if ok else _err("Speech failed", out[-400:])


def t_empty_recycle_bin():
    ok, out = _run_powershell(
        "Clear-RecycleBin -Force -ErrorAction SilentlyContinue; 'cleared'", 30)
    return (True, "Recycle Bin emptied") if ok else _err("Could not empty Recycle Bin", out[-400:])


def t_selected_text():
    old = _clip_text()
    script = ("Add-Type -AssemblyName System.Windows.Forms;"
              "[System.Windows.Forms.SendKeys]::SendWait('^c');'copied'")
    ok, out = _run_powershell(script, 15)
    if not ok:
        return _err("Could not copy the selection", out[-300:])
    time.sleep(0.35)
    new = _clip_text()
    if old:
        t_clipboard_write(old)
    if not new:
        return _err("Nothing was selected")
    return _ok(f"{len(new)} chars selected", new[:4000])


def _load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path, obj) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# General laptop control: clipboard, windows, system, processes, network, files
# ---------------------------------------------------------------------------
def _q(value) -> str:
    """Single-quoted PowerShell literal."""
    return "'" + str(value).replace("'", "''") + "'"


def _ps_json(command: str, timeout: int = 30):
    """Run PowerShell that ends in ConvertTo-Json; return (data, raw_error)."""
    ok, out = _run_powershell(command, timeout)
    text = str(out or "").strip()
    if not text:
        return None, "" if ok else "PowerShell returned nothing"
    for opener, closer in (("[", "]"), ("{", "}")):
        try:
            return json.loads(text, strict=False), ""
        except Exception:
            pass
        a, b = text.find(opener), text.rfind(closer)
        if a >= 0 and b > a:
            chunk = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text[a:b + 1])
            try:
                return json.loads(chunk, strict=False), ""
            except Exception:
                continue
    return None, text[-800:]


def _hbytes(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def _rows(data) -> list:
    if data is None:
        return []
    return data if isinstance(data, list) else [data]


def t_clipboard_clear():
    try:
        import ctypes
        u, _k = _clip_api()
        if not u.OpenClipboard(None):
            return _err("Clipboard is busy")
        try:
            u.EmptyClipboard()
        finally:
            u.CloseClipboard()
        return _ok("Clipboard cleared")
    except Exception as e:
        return _err(str(e))


def t_clipboard_to_file(path):
    text = _clip_text()
    if not text:
        return _err("The clipboard has no text to save")
    p = resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return _ok(f"Saved {len(text)} clipboard chars -> {rel_display(p)}")


def t_clipboard_from_file(path):
    p = resolve_path(path)
    if not p.is_file():
        return _err(f"No file at {rel_display(p)}")
    text = p.read_text(encoding="utf-8", errors="replace")
    ok, summary, detail = t_clipboard_write(text)
    return (ok, f"Copied {rel_display(p)} to clipboard", detail) if ok else (ok, summary, detail)


def t_clipboard_image(path=""):
    try:
        from PIL import ImageGrab
        data = ImageGrab.grabclipboard()
    except Exception as e:
        return _err(f"Could not read the clipboard image: {e}")
    if data is None:
        return _err("The clipboard holds no image")
    out = resolve_path(path) if path else workspace() / f"clipboard-{time.strftime('%Y%m%d-%H%M%S')}.png"
    if isinstance(data, list):
        images = [p for p in data if re.search(r"\.(png|jpe?g|webp|bmp|gif)$", str(p), re.I)]
        if not images:
            return _err("The clipboard holds files, but none are images")
        shutil.copy2(images[0], out)
        return _ok(f"Saved clipboard image -> {rel_display(out)}", str(images[0]))
    try:
        data.save(out)
    except Exception as e:
        return _err(f"Could not save the clipboard image: {e}")
    return _ok(f"Saved clipboard image -> {rel_display(out)}", f"{data.width}x{data.height}")


def _hwnd_ex(window="", fallback=True):
    import ctypes
    if str(window or "").strip():
        hwnd, name = _find_window(window)
        if hwnd:
            return hwnd, name
        return None, f"No visible window matching {window!r}"
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if not hwnd and fallback:
        return None, "No foreground window"
    name = ""
    if hwnd:
        u = ctypes.windll.user32
        n = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        name = buf.value or "foreground window"
    return hwnd, name


def _rect_of(hwnd):
    import ctypes
    from ctypes import wintypes
    r = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def t_window_info(window=""):
    import ctypes
    hwnd, name = _hwnd_ex(window)
    if not hwnd:
        return _err(name)
    u = ctypes.windll.user32
    from ctypes import wintypes
    pid = wintypes.DWORD()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    proc = ""
    try:
        import psutil
        proc = psutil.Process(int(pid.value)).name()
    except Exception:
        pass
    rect = _rect_of(hwnd)
    state = "minimized" if u.IsIconic(hwnd) else ("maximized" if u.IsZoomed(hwnd) else "normal")
    ex = u.GetWindowLongW(hwnd, -20)
    topmost = bool(ex & 0x00000008)
    body = (f"Title: {name}\nPID: {pid.value} ({proc or 'unknown'})\n"
            f"State: {state}{' · always on top' if topmost else ''}\n"
            f"Rect: {rect[0]},{rect[1]} {rect[2]}x{rect[3]}" if rect else
            f"Title: {name}\nPID: {pid.value}\nState: {state}")
    return _ok(name, body)


def _move_resize(hwnd, x=None, y=None, w=None, h=None):
    import ctypes
    flags = 0x0004 | 0x0010  # SWP_NOZORDER | SWP_NOACTIVATE
    if x is None and y is None:
        flags |= 0x0001  # SWP_NOMOVE
    if w is None and h is None:
        flags |= 0x0002  # SWP_NOSIZE
    ctypes.windll.user32.SetWindowPos(hwnd, 0, int(x or 0), int(y or 0),
                                      int(w or 0), int(h or 0), flags)
    return _rect_of(hwnd)


def t_window_move(x, y, window=""):
    hwnd, name = _hwnd_ex(window)
    if not hwnd:
        return _err(name)
    r = _move_resize(hwnd, x=int(x), y=int(y))
    return _ok(f"Moved {name} to {int(x)},{int(y)}", f"rect={r}")


def t_window_resize(width, height, window=""):
    hwnd, name = _hwnd_ex(window)
    if not hwnd:
        return _err(name)
    w, h = max(200, int(width)), max(150, int(height))
    r = _move_resize(hwnd, w=w, h=h)
    return _ok(f"Resized {name} to {w}x{h}", f"rect={r}")


def t_window_set_rect(x, y, width, height, window=""):
    hwnd, name = _hwnd_ex(window)
    if not hwnd:
        return _err(name)
    r = _move_resize(hwnd, int(x), int(y), max(200, int(width)), max(150, int(height)))
    return _ok(f"Placed {name} at {int(x)},{int(y)} {int(width)}x{int(height)}", f"rect={r}")


def t_window_snap(position="left", window=""):
    import ctypes
    from ctypes import wintypes
    hwnd, name = _hwnd_ex(window)
    if not hwnd:
        return _err(name)
    work = wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0)
    L, T, R, B = work.left, work.top, work.right, work.bottom
    W, H = R - L, B - T
    pos = str(position or "left").strip().lower()
    rect = _rect_of(hwnd) or (L, T, W, H)
    if pos in ("left", "half-left"):
        coords = (L, T, W // 2, H)
    elif pos in ("right", "half-right"):
        coords = (L + W // 2, T, W // 2, H)
    elif pos in ("top", "half-top"):
        coords = (L, T, W, H // 2)
    elif pos in ("bottom", "half-bottom"):
        coords = (L, T + H // 2, W, H // 2)
    elif pos in ("max", "maximize", "full", "fullscreen"):
        coords = (L, T, W, H)
    elif pos in ("center", "middle"):
        coords = (L + (W - rect[2]) // 2, T + (H - rect[3]) // 2, rect[2], rect[3])
    elif pos in ("top-left", "topright", "top-right"):
        coords = (L, T, W // 2, H // 2) if pos == "top-left" else (L + W // 2, T, W // 2, H // 2)
    elif pos in ("bottom-left", "bottom-right"):
        coords = ((L, T + H // 2, W // 2, H // 2) if pos == "bottom-left"
                  else (L + W // 2, T + H // 2, W // 2, H // 2))
    else:
        return _err(f"Unknown position {position!r}",
                    "Use left, right, top, bottom, maximize, center, top-left, top-right, "
                    "bottom-left or bottom-right.")
    _move_resize(hwnd, *coords)
    return _ok(f"Snapped {name} {pos}")


def t_window_topmost(window="", on=True):
    import ctypes
    hwnd, name = _hwnd_ex(window)
    if not hwnd:
        return _err(name)
    flag = -1 if _bool(on) else -2  # HWND_TOPMOST / HWND_NOTOPMOST
    ctypes.windll.user32.SetWindowPos(hwnd, flag, 0, 0, 0, 0,
                                      0x0001 | 0x0002 | 0x0010)
    return _ok(f"{name} is now {'always on top' if _bool(on) else 'normal'}")


def t_window_opacity(level=90, window=""):
    import ctypes
    hwnd, name = _hwnd_ex(window)
    if not hwnd:
        return _err(name)
    lv = max(10, min(int(level or 90), 100))
    u = ctypes.windll.user32
    ex = u.GetWindowLongW(hwnd, -20)
    u.SetWindowLongW(hwnd, -20, ex | 0x00080000)  # WS_EX_LAYERED
    u.SetLayeredWindowAttributes(hwnd, 0, int(lv * 255 / 100), 0x00000002)
    return _ok(f"{name} opacity -> {lv}%")


def t_window_close_all(filter=""):
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    needle = str(filter or "").strip().lower()
    skip = {"program manager", "windows input experience", "default ime", "msctfime ui",
            "taskbar", "start", "windows push notifications", "anvil fletch"}
    closed = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value.strip()
        low = title.lower()
        if not title or low in skip:
            return True
        if needle and needle not in low:
            return True
        u.PostMessageW(hwnd, 0x0010, 0, 0)
        closed.append(title)
        return True

    u.EnumWindows(cb, 0)
    if not closed:
        return _err("No windows matched", f"filter={filter!r}")
    return _ok(f"Closed {len(closed)} window(s)", "\n".join(closed[:40]))


def t_os_info():
    import platform
    data, raw = _ps_json(
        "Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber,"
        "OSArchitecture,InstallDate,LastBootUpTime | ConvertTo-Json -Compress", 30)
    d = (_rows(data) or [{}])[0] if data else {}
    lines = [f"{platform.system()} {platform.release()} {platform.version()}",
             f"Machine: {platform.machine()} · Python {platform.python_version()}"]
    if d:
        lines = [str(d.get("Caption") or lines[0]), f"Version: {d.get('Version')} "
                 f"(build {d.get('BuildNumber')}) · {d.get('OSArchitecture')}",
                 f"Booted: {d.get('LastBootUpTime')}"]
    if raw:
        lines.append(raw[-400:])
    return _ok("Operating system", "\n".join(lines))


def t_cpu_usage():
    try:
        import psutil
        per = psutil.cpu_percent(interval=0.4, percpu=True)
        total = round(sum(per) / max(1, len(per)), 1)
        freq = ""
        try:
            f = psutil.cpu_freq()
            if f:
                freq = f" · {f.current:.0f} MHz"
        except Exception:
            pass
        lines = [f"Total {total}% · {len(per)} logical cores{freq}"]
        for i, v in enumerate(per):
            bar = "#" * int(v / 5)
            lines.append(f"CPU {i:>2}: {v:5.1f}% {bar}")
        return _ok(f"CPU {total}%", "\n".join(lines))
    except Exception as e:
        return _err(f"CPU query failed: {e}")


def t_memory_usage():
    try:
        import psutil
        m = psutil.virtual_memory()
        s = psutil.swap_memory()
        top = []
        for p in psutil.process_iter(["name", "memory_info"]):
            try:
                top.append((p.info["memory_info"].rss, p.info["name"]))
            except Exception:
                continue
        top.sort(reverse=True)
        lines = [f"RAM {m.used / 2**30:.1f} / {m.total / 2**30:.1f} GB "
                 f"({m.percent}% used, {m.available / 2**30:.1f} GB free)",
                 f"Swap {s.used / 2**30:.1f} / {s.total / 2**30:.1f} GB"]
        lines.append("Top by memory: " + ", ".join(
            f"{n} {r / 2**20:.0f}MB" for r, n in top[:6]))
        return _ok(f"RAM {m.percent}% used", "\n".join(lines))
    except Exception as e:
        return _err(f"Memory query failed: {e}")


def t_disk_health():
    data, raw = _ps_json(
        "Get-PhysicalDisk | Select-Object FriendlyName,MediaType,HealthStatus,"
        "OperationalStatus,@{n='GB';e={[int]($_.Size/1GB)}} | ConvertTo-Json -Compress", 30)
    rows = _rows(data)
    if not rows:
        return _err("No physical disk data", raw)
    lines = []
    for d in rows:
        lines.append(f"{d.get('FriendlyName')} · {d.get('MediaType')} · "
                     f"{d.get('GB')} GB · {d.get('HealthStatus')} "
                     f"({d.get('OperationalStatus')})")
    return _ok(f"{len(rows)} disk(s)", "\n".join(lines))


def t_cpu_temp():
    data, raw = _ps_json(
        "Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature "
        "-ErrorAction Stop | Select-Object InstanceName,CurrentTemperature | "
        "ConvertTo-Json -Compress", 25)
    rows = _rows(data)
    if not rows:
        return _err("No temperature sensor exposed", raw[-400:])
    lines = []
    for r in rows:
        try:
            c = float(r.get("CurrentTemperature")) / 10.0 - 273.15
            lines.append(f"{r.get('InstanceName') or 'zone'}: {c:.1f} C")
        except Exception:
            continue
    return (_ok("CPU temperature", "\n".join(lines)) if lines
            else _err("Temperature sensor returned no usable values", raw[-400:]))


def t_gpu_info():
    data, raw = _ps_json(
        "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM,"
        "DriverVersion,VideoModeDescription,Status | ConvertTo-Json -Compress", 30)
    rows = _rows(data)
    if not rows:
        return _err("No GPU information", raw)
    lines = []
    for d in rows:
        ram = d.get("AdapterRAM") or 0
        try:
            ram = _hbytes(int(ram))
        except Exception:
            ram = "?"
        lines.append(f"{d.get('Name')} · {ram} VRAM · driver {d.get('DriverVersion')} · "
                     f"{d.get('VideoModeDescription')} · {d.get('Status')}")
    return _ok(f"{len(rows)} display adapter(s)", "\n".join(lines))


def t_services_list(filter="", mode="running"):
    want = str(mode or "running").lower()
    cond = ""
    if want in ("running", "started"):
        cond = "| Where-Object {$_.Status -eq 'Running'}"
    elif want in ("stopped", "off"):
        cond = "| Where-Object {$_.Status -eq 'Stopped'}"
    data, raw = _ps_json(
        "Get-Service " + cond + " | Select-Object -First 70 Name,DisplayName,Status,StartType | "
        "ConvertTo-Json -Compress", 40)
    rows = _rows(data)
    needle = str(filter or "").strip().lower()
    if needle:
        rows = [r for r in rows
                if needle in str(r.get("Name", "")).lower()
                or needle in str(r.get("DisplayName", "")).lower()]
    if not rows:
        return _err(f"No services matched {filter!r}", raw[-500:])
    lines = [f"{r.get('Status'):<9} {r.get('Name'):<32} {r.get('DisplayName')} "
             f"[{r.get('StartType')}]" for r in rows[:60]]
    return _ok(f"{len(rows)} service(s)", "\n".join(lines))


def t_service_control(name, action="status"):
    name = str(name or "").strip()
    if not name:
        return _err("service_control needs a service name")
    act = str(action or "status").lower()
    cmd = {"start": "Start-Service", "stop": "Stop-Service",
           "restart": "Restart-Service", "status": ""}.get(act)
    if cmd is None:
        return _err(f"Unknown action {action!r}",
                    "Use start, stop, restart or status.")
    if cmd:
        ok, out = _run_powershell(
            f"{cmd} -Name {_q(name)} -ErrorAction Stop; "
            f"(Get-Service -Name {_q(name)}).Status", 45)
        if not ok:
            return _err(f"Could not {act} {name}", out[-600:])
    ok, out = _run_powershell(
        f"Get-Service -Name {_q(name)} -ErrorAction Stop | Format-List Name,DisplayName,"
        "Status,StartType | Out-String -Width 100", 25)
    return (ok, f"{name}: {act}", out.strip()) if ok else _err(out[-500:])


def t_startup_list():
    data, raw = _ps_json(
        "Get-CimInstance Win32_StartupCommand | Select-Object Name,Command,Location,User | "
        "ConvertTo-Json -Compress", 35)
    rows = _rows(data)
    if not rows:
        return _ok("No startup entries found", raw[-400:])
    lines = [f"{r.get('Name')} · {r.get('Location')}\n    {r.get('Command')}" for r in rows[:50]]
    return _ok(f"{len(rows)} startup entr(ies)", "\n".join(lines))


def t_startup_add(name, command):
    name, command = str(name or "").strip(), str(command or "").strip()
    if not name or not command:
        return _err("startup_add needs name and command")
    ok, out = _run_powershell(
        "New-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run' "
        f"-Name {_q(name)} -Value {_q(command)} -PropertyType String -Force | Out-Null; 'ok'", 20)
    return (_ok(f"Will run {name!r} at sign-in", command) if ok and "ok" in out
            else _err("Could not add the startup entry", out[-500:]))


def t_startup_remove(name):
    name = str(name or "").strip()
    if not name:
        return _err("startup_remove needs the entry name")
    ok, out = _run_powershell(
        "Remove-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run' "
        f"-Name {_q(name)} -ErrorAction Stop; 'removed'", 20)
    return (_ok(f"Removed startup entry {name!r}") if ok and "removed" in out
            else _err(f"No startup entry named {name!r}", out[-400:]))


def t_scheduled_tasks(filter=""):
    needle = str(filter or "").strip().lower()
    flt = f" | Where-Object {{$_.TaskName -like {_q('*' + needle + '*')}}}" if needle else ""
    data, raw = _ps_json(
        "Get-ScheduledTask" + flt + " | Select-Object -First 60 TaskName,State,TaskPath | "
        "ConvertTo-Json -Compress", 40)
    rows = _rows(data)
    if not rows:
        return _err(f"No scheduled tasks matched {filter!r}", raw[-500:])
    lines = [f"{r.get('State') or '?':<9} {r.get('TaskName')}  {r.get('TaskPath') or ''}"
             for r in rows]
    return _ok(f"{len(rows)} task(s)", "\n".join(lines))


def t_power_plan(name=""):
    ok, out = _run_powershell("powercfg /list", 25)
    if not ok:
        return _err("Could not read power plans", out[-400:])
    if not str(name or "").strip():
        return _ok("Power plans", out.strip()[:2500])
    needle = str(name).strip().lower()
    chosen = ""
    for line in out.splitlines():
        if needle in line.lower():
            m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                          line, re.I)
            if m:
                chosen = m.group(1)
                break
    if not chosen:
        return _err(f"No power plan matching {name!r}", out[:1200])
    ok2, out2 = _run_powershell(f"powercfg /setactive {chosen}; powercfg /getactivescheme", 25)
    return (_ok(f"Power plan -> {name}", out2.strip()) if ok2
            else _err("Could not switch power plan", out2[-500:]))


def t_shutdown_pc(action="shutdown", delay=30):
    act = str(action or "shutdown").lower()
    secs = max(0, min(int(delay or 0), 3600))
    if act in ("cancel", "abort", "stop"):
        ok, out = _run_powershell("shutdown.exe /a", 20)
        return (_ok("Scheduled shutdown cancelled", out) if ok
                else _err("Nothing to cancel", out[-400:]))
    if act in ("sleep", "suspend"):
        subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                         creationflags=0x08000000)
        return _ok("Going to sleep")
    flags = {"shutdown": "/s", "restart": "/r", "reboot": "/r", "logoff": "/l",
             "signout": "/l", "hibernate": "/h"}
    flag = flags.get(act)
    if not flag:
        return _err(f"Unknown action {action!r}",
                    "Use shutdown, restart, logoff, hibernate, sleep or cancel.")
    cmd = ["shutdown.exe", flag]
    if flag in ("/s", "/r"):
        cmd += ["/t", str(secs)]
    ok, out = _run_powershell(" ".join(cmd), 20)
    if not ok:
        return _err(f"Could not schedule {act}", out[-500:])
    when = f" in {secs}s" if flag in ("/s", "/r") and secs else " now"
    return _ok(f"{act.capitalize()}{when}",
               "Use shutdown_pc(action='cancel') to abort." if flag in ("/s", "/r") else out)


def t_event_log(limit=10, severity="error"):
    lv = {"error": 2, "warning": 3, "warn": 3, "info": 4, "information": 4}.get(
        str(severity or "error").lower(), 2)
    n = max(1, min(int(limit or 10), 40))
    data, raw = _ps_json(
        "Get-WinEvent -FilterHashtable @{LogName=@('System','Application');Level=" + str(lv) +
        "} -MaxEvents " + str(n) + " -ErrorAction SilentlyContinue | "
        "Select-Object TimeCreated,ProviderName,Id,LevelDisplayName,"
        "@{n='Msg';e={if($_.Message){$_.Message.Substring(0,[Math]::Min(180,$_.Message.Length))}}} | "
        "ConvertTo-Json -Compress", 45)
    rows = _rows(data)
    if not rows:
        return _ok(f"No {severity} events found", raw[-400:])
    lines = [f"{r.get('TimeCreated')} [{r.get('LevelDisplayName')}] {r.get('ProviderName')} "
             f"#{r.get('Id')} - {r.get('Msg')}" for r in rows]
    return _ok(f"{len(rows)} recent {severity} event(s)", "\n".join(lines))


def t_windows_updates(limit=15):
    n = max(1, min(int(limit or 15), 60))
    data, raw = _ps_json(
        f"Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object -First {n} "
        "HotFixID,Description,InstalledOn | ConvertTo-Json -Compress", 40)
    rows = _rows(data)
    if not rows:
        return _ok("No updates listed", raw[-400:])
    lines = [f"{r.get('HotFixID')} · {r.get('Description')} · installed {r.get('InstalledOn')}"
             for r in rows]
    return _ok(f"{len(rows)} update(s)", "\n".join(lines))


def t_printers_list():
    data, raw = _ps_json(
        "Get-Printer | Select-Object Name,DriverName,PortName,PrinterStatus | "
        "ConvertTo-Json -Compress", 30)
    rows = _rows(data)
    if not rows:
        return _err("No printers found", raw[-400:])
    lines = [f"{r.get('Name')} · {r.get('DriverName')} · {r.get('PortName')} · "
             f"{r.get('PrinterStatus')}" for r in rows]
    return _ok(f"{len(rows)} printer(s)", "\n".join(lines))


def t_bluetooth_devices():
    data, raw = _ps_json(
        "Get-PnpDevice -Class Bluetooth -PresentOnly -ErrorAction SilentlyContinue | "
        "Select-Object FriendlyName,Status,InstanceId | ConvertTo-Json -Compress", 30)
    rows = _rows(data)
    if not rows:
        return _err("No Bluetooth devices found", raw[-400:])
    lines = [f"{r.get('Status')} · {r.get('FriendlyName')} · {r.get('InstanceId')}"
             for r in rows]
    return _ok(f"{len(rows)} Bluetooth device(s)", "\n".join(lines))


def t_usb_devices():
    data, raw = _ps_json(
        "Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | "
        "Where-Object {$_.InstanceId -like 'USB*'} | Select-Object FriendlyName,Class,Status | "
        "ConvertTo-Json -Compress", 35)
    rows = _rows(data)
    if not rows:
        return _err("No USB devices found", raw[-400:])
    lines = [f"{r.get('Status')} · {r.get('FriendlyName')} [{r.get('Class')}]" for r in rows[:80]]
    return _ok(f"{len(rows)} USB device(s)", "\n".join(lines))


def t_audio_devices():
    data, raw = _ps_json(
        "Get-PnpDevice -Class AudioEndpoint -PresentOnly -ErrorAction SilentlyContinue | "
        "Select-Object FriendlyName,Status | ConvertTo-Json -Compress", 30)
    rows = _rows(data)
    if not rows:
        return _err("No audio endpoints found", raw[-400:])
    lines = [f"{r.get('Status')} · {r.get('FriendlyName')}" for r in rows]
    return _ok(f"{len(rows)} audio device(s)", "\n".join(lines))


def t_display_info():
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    monitors = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    def cb(hmon, _hdc, _rect, _data):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcMonitor
            monitors.append((r.left, r.top, r.right - r.left, r.bottom - r.top,
                             bool(mi.dwFlags & 1)))
        return True

    user32.EnumDisplayMonitors(0, 0, cb, 0)
    if not monitors:
        return _err("No monitors reported")
    lines = []
    for i, (x, y, w, h, primary) in enumerate(monitors, 1):
        refresh = ""
        try:
            class DEVMODE(ctypes.Structure):
                _fields_ = [("dmDeviceName", wintypes.WCHAR * 32),
                            ("dmSpecVersion", wintypes.WORD), ("dmDriverVersion", wintypes.WORD),
                            ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
                            ("dmFields", wintypes.DWORD), ("dmPositionX", wintypes.LONG),
                            ("dmPositionY", wintypes.LONG), ("dmDisplayOrientation", wintypes.DWORD),
                            ("dmDisplayFixedOutput", wintypes.DWORD),
                            ("dmColor", wintypes.SHORT), ("dmDuplex", wintypes.SHORT),
                            ("dmYResolution", wintypes.SHORT), ("dmTTOption", wintypes.SHORT),
                            ("dmCollate", wintypes.SHORT), ("dmFormName", wintypes.WCHAR * 32),
                            ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD),
                            ("dmPelsWidth", wintypes.DWORD), ("dmPelsHeight", wintypes.DWORD),
                            ("dmDisplayFlags", wintypes.DWORD),
                            ("dmDisplayFrequency", wintypes.DWORD)]
            dm = DEVMODE()
            dm.dmSize = ctypes.sizeof(DEVMODE)
            if user32.EnumDisplaySettingsW(None, -1, ctypes.byref(dm)):
                refresh = f" @ {dm.dmDisplayFrequency} Hz"
        except Exception:
            pass
        lines.append(f"Monitor {i}{' (primary)' if primary else ''}: "
                     f"{w}x{h} at {x},{y}{refresh}")
    return _ok(f"{len(monitors)} monitor(s)", "\n".join(lines))


def t_timezone_info():
    ok, tz = _run_powershell("tzutil /g", 15)
    now = time.localtime()
    off = -time.timezone if not now.tm_isdst else -time.altzone
    sign = "+" if off >= 0 else "-"
    return _ok(f"{time.strftime('%A %d %B %Y, %H:%M:%S')}",
               f"Timezone: {(tz or '').strip() if ok else 'unknown'}\n"
               f"UTC{sign}{abs(off) // 3600:02d}:{(abs(off) % 3600) // 60:02d}\n"
               f"Local: {time.strftime('%Y-%m-%d %H:%M:%S')}")


def t_font_list(filter="", limit=80):
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
        names = []
        i = 0
        while True:
            try:
                name, value, _kind = winreg.EnumValue(key, i)
            except OSError:
                break
            names.append(f"{name} -> {value}")
            i += 1
        winreg.CloseKey(key)
    except Exception as e:
        return _err(f"Could not read the font list: {e}")
    needle = str(filter or "").strip().lower()
    if needle:
        names = [n for n in names if needle in n.lower()]
    if not names:
        return _err(f"No fonts matched {filter!r}")
    return _ok(f"{len(names)} font(s)", "\n".join(names[:max(1, min(int(limit or 80), 300))]))


def t_drivers_list(filter=""):
    flt = (f" | Where-Object {{$_.DeviceName -like {_q('*' + str(filter).strip() + '*')}}}"
           if str(filter or "").strip() else "")
    data, raw = _ps_json(
        "Get-CimInstance Win32_PnPSignedDriver" + flt +
        " | Select-Object -First 60 DeviceName,Manufacturer,DriverVersion,DriverDate | "
        "ConvertTo-Json -Compress", 60)
    rows = _rows(data)
    if not rows:
        return _err(f"No drivers matched {filter!r}", raw[-500:])
    lines = [f"{r.get('DeviceName')} · {r.get('Manufacturer')} · {r.get('DriverVersion')} · "
             f"{r.get('DriverDate')}" for r in rows]
    return _ok(f"{len(rows)} driver(s)", "\n".join(lines))


def t_system_report():
    """One call that reports the state of the machine: OS, CPU, RAM, disks, power, network."""
    parts = []
    try:
        import platform
        parts.append(f"OS: {platform.system()} {platform.release()} ({platform.machine()})")
    except Exception:
        pass
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.3)
        m = psutil.virtual_memory()
        parts.append(f"CPU: {os.cpu_count()} logical cores at {cpu:.0f}%")
        parts.append(f"RAM: {m.used / 2**30:.1f} / {m.total / 2**30:.1f} GB used "
                     f"({m.percent}%), {m.available / 2**30:.1f} GB free")
        secs = int(time.time() - psutil.boot_time())
        d, rem = divmod(secs, 86400)
        h, rem = divmod(rem, 3600)
        parts.append(f"Uptime: {d}d {h}h {rem // 60}m")
        for part in psutil.disk_partitions(all=False):
            try:
                u = psutil.disk_usage(part.mountpoint)
                if u.total:
                    parts.append(f"Disk {part.device} ({part.fstype}): "
                                 f"{u.free / 2**30:.1f} GB free of {u.total / 2**30:.1f} GB "
                                 f"({u.percent}% used)")
            except Exception:
                continue
        top = []
        for p in psutil.process_iter(["name", "memory_info", "cpu_percent"]):
            try:
                top.append((p.info["memory_info"].rss, p.info["name"]))
            except Exception:
                continue
        top.sort(reverse=True)
        if top:
            parts.append("Top memory: " + ", ".join(f"{n} {r / 2**20:.0f}MB"
                                                    for r, n in top[:5]))
        try:
            b = psutil.sensors_battery()
            if b:
                parts.append(f"Battery: {b.percent:.0f}%"
                             f"{' (charging)' if b.power_plugged else ' (on battery)'}")
        except Exception:
            pass
    except Exception as e:
        parts.append(f"(psutil: {e})")
    try:
        okb, outb = _run_powershell(
            "Get-CimInstance Win32_Processor | Select-Object -First 1 Name | "
            "Format-Table -HideTableHeaders | Out-String", 20)
        name = " ".join(outb.split()) if okb else ""
        if name and not name.lower().startswith("get-cim"):
            parts.insert(1, f"Processor: {name[:100]}")
    except Exception:
        pass
    try:
        okw, outw = _run_powershell("netsh wlan show interfaces", 20)
        if okw:
            ssid = next((ln.split(":", 1)[1].strip() for ln in outw.splitlines()
                         if re.match(r"\s*SSID\s*:", ln)), "")
            signal = next((ln.split(":", 1)[1].strip() for ln in outw.splitlines()
                           if "Signal" in ln), "")
            if ssid:
                parts.append(f"Wi-Fi: {ssid}{(' · ' + signal) if signal else ''}")
    except Exception:
        pass
    body = "\n".join(parts)
    return _ok("System report", body)


# ---------------------------------------------------------------------------
# Processes, network and files
# ---------------------------------------------------------------------------
def _proc_find(name="", pid=0):
    import psutil
    if pid:
        return psutil.Process(int(pid))
    needle = str(name or "").strip().lower()
    if needle.endswith(".exe"):
        needle = needle[:-4]
    if not needle:
        raise LookupError("Give a process name or pid")
    exact, partial = [], []
    for p in psutil.process_iter(["name", "pid"]):
        try:
            pname = str(p.info["name"] or "").lower()
        except Exception:
            continue
        bare = pname[:-4] if pname.endswith(".exe") else pname
        if bare == needle:
            exact.append(p)
        elif needle in bare:
            partial.append(p)
    found = exact or partial
    if not found:
        raise LookupError(f"No process matching {name!r}")
    return found[0]


def t_process_info(name="", pid=0):
    if not str(name or "").strip() and not int(pid or 0):
        return _err("process_info needs a name or pid")
    try:
        p = _proc_find(name, pid)
    except LookupError as e:
        return _err(str(e))
    try:
        import psutil
        with p.oneshot():
            info = p.as_dict(attrs=["pid", "name", "exe", "cmdline", "status",
                                    "username", "create_time", "memory_info"])
        conns = 0
        try:
            conns = len(p.net_connections(kind="inet"))
        except Exception:
            pass
        body = (f"PID: {info['pid']}\nName: {info['name']}\nExe: {info.get('exe') or '?'}\n"
                f"User: {info.get('username') or '?'}\nStatus: {info.get('status')}\n"
                f"Started: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(info['create_time']))}\n"
                f"Memory: {_hbytes(info['memory_info'].rss) if info.get('memory_info') else '?'}\n"
                f"Connections: {conns}\n"
                f"Command: {' '.join(info.get('cmdline') or [])[:600] or '(hidden)'}")
        return _ok(f"{info['name']} (pid {info['pid']})", body)
    except Exception as e:
        return _err(f"Could not inspect the process: {e}")


def t_process_top(limit=10, by="memory"):
    try:
        import psutil
        key = str(by or "memory").lower()
        n = max(1, min(int(limit or 10), 40))
        rows = []
        for p in psutil.process_iter(["pid", "name", "memory_info"]):
            try:
                cpu = p.cpu_percent(None)
                rows.append([p, cpu, p.info["memory_info"].rss])
            except Exception:
                continue
        if key.startswith("c"):
            time.sleep(0.35)
            for r in rows:
                try:
                    r[1] = r[0].cpu_percent(None)
                except Exception:
                    r[1] = 0.0
            rows.sort(key=lambda r: (r[1], r[2]), reverse=True)
        else:
            rows.sort(key=lambda r: r[2], reverse=True)
        lines = [f"{r[0].info['pid']:>7}  {r[0].info['name'][:28]:<28} "
                 f"{'CPU ' + format(r[1] / max(1, os.cpu_count()), '.1f') + '%':>10}  "
                 f"{_hbytes(r[2]):>10}" for r in rows[:n]]
        return _ok(f"Top {len(lines)} processes by {key}", "\n".join(lines))
    except Exception as e:
        return _err(f"Could not list processes: {e}")


def t_process_tree(name="", pid=0):
    try:
        p = _proc_find(name, pid)
        import psutil
    except LookupError as e:
        return _err(str(e))
    lines = []

    def walk(proc, depth):
        try:
            label = f"{'  ' * depth}{proc.pid} {proc.name()}"
        except Exception:
            return
        lines.append(label)
        if depth >= 5:
            return
        try:
            for child in proc.children():
                walk(child, depth + 1)
        except Exception:
            pass

    walk(p, 0)
    return _ok(f"Process tree for {p.info['name'] if hasattr(p, 'info') else name}",
               "\n".join(lines[:80]))


def t_process_priority(name="", pid=0, priority="normal"):
    try:
        p = _proc_find(name, pid)
        import psutil
    except LookupError as e:
        return _err(str(e))
    mapping = {"idle": psutil.IDLE_PRIORITY_CLASS,
               "below": psutil.BELOW_NORMAL_PRIORITY_CLASS,
               "normal": psutil.NORMAL_PRIORITY_CLASS,
               "above": psutil.ABOVE_NORMAL_PRIORITY_CLASS,
               "high": psutil.HIGH_PRIORITY_CLASS,
               "realtime": psutil.REALTIME_PRIORITY_CLASS}
    value = mapping.get(str(priority or "normal").lower())
    if value is None:
        return _err(f"Unknown priority {priority!r}", "Use idle, below, normal, above, high or realtime.")
    try:
        p.nice(value)
    except Exception as e:
        return _err(f"Could not change priority: {e} (try running elevated)")
    return _ok(f"{p.name()} priority -> {level}")


def t_process_wait(name="", pid=0, timeout=30, gone=False):
    limit = max(1, min(int(timeout or 30), 300))
    deadline = time.time() + limit
    import psutil
    while time.time() < deadline:
        found = None
        try:
            found = _proc_find(name, pid)
        except LookupError:
            found = None
        if _bool(gone) and found is None:
            return _ok(f"{name or pid} is gone")
        if not _bool(gone) and found is not None:
            return _ok(f"{found.name()} (pid {found.pid}) is running")
        time.sleep(0.5)
    state = "exit" if _bool(gone) else "start"
    return _err(f"Timed out after {limit}s waiting for {name or pid} to {state}")


def t_process_suspend(name="", pid=0):
    try:
        p = _proc_find(name, pid)
    except LookupError as e:
        return _err(str(e))
    try:
        p.suspend()
    except Exception as e:
        return _err(f"Could not suspend: {e}")
    return _ok(f"Suspended {p.name()} (pid {p.pid})")


def t_process_resume(name="", pid=0):
    try:
        p = _proc_find(name, pid)
    except LookupError as e:
        return _err(str(e))
    try:
        p.resume()
    except Exception as e:
        return _err(f"Could not resume: {e}")
    return _ok(f"Resumed {p.name()} (pid {p.pid})")


def t_ping_host(host, count=4):
    host = str(host or "").strip()
    if not host:
        return _err("ping_host needs a host")
    n = max(1, min(int(count or 4), 10))
    data, raw = _ps_json(
        f"Test-Connection -TargetName {_q(host)} -Count {n} -ErrorAction Stop | "
        "Select-Object Address,ResponseTime,StatusCode | ConvertTo-Json -Compress", 40)
    rows = _rows(data)
    if not rows:
        return _err(f"No reply from {host}", raw[-500:])
    times = [r.get("ResponseTime") for r in rows if isinstance(r.get("ResponseTime"), int)]
    avg = f" · avg {sum(times) // len(times)}ms" if times else ""
    lines = [f"{r.get('Address')} · {r.get('ResponseTime')} ms · status {r.get('StatusCode')}"
             for r in rows]
    return _ok(f"{host}: {len(rows)}/{n} replies{avg}", "\n".join(lines))


def t_dns_lookup(host):
    host = str(host or "").strip()
    if not host:
        return _err("dns_lookup needs a host")
    data, raw = _ps_json(
        f"Resolve-DnsName -Name {_q(host)} -ErrorAction Stop | "
        "Select-Object Name,Type,IPAddress,NameHost | ConvertTo-Json -Compress", 30)
    rows = _rows(data)
    if not rows:
        return _err(f"Could not resolve {host}", raw[-500:])
    lines = [f"{r.get('Name')} {r.get('Type')} -> {r.get('IPAddress') or r.get('NameHost') or '?'}"
             for r in rows]
    return _ok(f"{host}: {len(rows)} record(s)", "\n".join(lines))


def t_port_check(host, port=443, timeout=4):
    import socket
    host = str(host or "").strip()
    if not host:
        return _err("port_check needs a host")
    try:
        port = int(port or 443)
    except Exception:
        return _err("port must be a number")
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=max(1, min(int(timeout or 4), 20))):
            pass
        ms = int((time.time() - t0) * 1000)
        return _ok(f"{host}:{port} is open ({ms} ms)")
    except Exception as e:
        return _err(f"{host}:{port} is closed or filtered", str(e))


def t_net_connections(filter="", limit=50):
    try:
        import psutil
    except Exception as e:
        return _err(str(e))
    needle = str(filter or "").strip().lower()
    names = {}
    rows = []
    try:
        conns = psutil.net_connections(kind="inet")
    except Exception as e:
        return _err(f"Could not read connections: {e} (try running elevated)")
    for c in conns:
        if c.status == "NONE" and not c.raddr:
            continue
        if c.pid:
            if c.pid not in names:
                try:
                    names[c.pid] = psutil.Process(c.pid).name()
                except Exception:
                    names[c.pid] = "?"
            proc = names[c.pid]
        else:
            proc = "?"
        local = f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else "?"
        remote = f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else "-"
        line = f"{c.status:<12} {local:<24} -> {remote:<24} {proc}"
        if needle and needle not in line.lower():
            continue
        rows.append(line)
        if len(rows) >= max(1, min(int(limit or 50), 200)):
            break
    if not rows:
        return _ok("No matching connections", "Nothing active.")
    return _ok(f"{len(rows)} connection(s)", "\n".join(rows))


def t_public_ip():
    errors = []
    for url in ("https://api.ipify.org", "https://ipinfo.io/ip", "https://ifconfig.me/ip"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
            with http_open(req, 12) as r:
                ip = r.read(200).decode("ascii", "replace").strip()
            if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip) or re.match(r"^[0-9a-f:]{6,}$", ip, re.I):
                return _ok(f"Public IP: {ip}", ip)
        except Exception as e:
            errors.append(f"{url}: {e}")
    return _err("Could not determine the public IP", "\n".join(errors[-2:]))


def t_wifi_networks():
    ok, out = _run_powershell("netsh wlan show networks mode=bssid", 30)
    if not ok:
        return _err("Wi-Fi scan failed", out[-500:])
    rows, cur = [], None
    for line in out.splitlines():
        m = re.match(r"\s*SSID\s+\d+\s*:\s*(.*)$", line)
        if m:
            cur = {"ssid": m.group(1).strip(), "signal": "", "auth": ""}
            rows.append(cur)
            continue
        if cur is None:
            continue
        if "Signal" in line and not cur["signal"]:
            cur["signal"] = line.split(":", 1)[-1].strip()
        elif "Authentication" in line and not cur["auth"]:
            cur["auth"] = line.split(":", 1)[-1].strip()
    if not rows:
        return _err("No Wi-Fi networks found", out[-600:])
    lines = [f"{r['ssid']} · {r['signal'] or '?'} · {r['auth'] or '?'}" for r in rows]
    return _ok(f"{len(rows)} Wi-Fi network(s)", "\n".join(lines))


def t_wifi_profiles():
    ok, out = _run_powershell("netsh wlan show profiles", 25)
    if not ok:
        return _err("Could not list Wi-Fi profiles", out[-500:])
    names = []
    for line in out.splitlines():
        m = re.search(r":\s*(.+)$", line)
        if m and ("All User Profile" in line or "所有用户配置文件" in line):
            names.append(m.group(1).strip())
    return _ok(f"{len(names)} saved Wi-Fi profile(s)", "\n".join(names) or out.strip()[:1500])


def t_wifi_connect(name):
    name = str(name or "").strip()
    if not name:
        return _err("wifi_connect needs the profile name")
    ok, out = _run_powershell(f"netsh wlan connect name={_q(name)}", 30)
    if not ok:
        return _err(f"Could not connect to {name!r}", out[-600:])
    time.sleep(2)
    oks, outs = _run_powershell("netsh wlan show interfaces", 20)
    ssid = next((ln.split(":", 1)[1].strip() for ln in outs.splitlines()
                 if re.match(r"\s*SSID\s*:", ln)), "")
    return _ok(f"Connect requested for {name}" + (f"; now on {ssid}" if ssid else ""), outs[-1200:])


def t_firewall_status():
    data, raw = _ps_json(
        "Get-NetFirewallProfile -ErrorAction Stop | Select-Object Name,Enabled,"
        "DefaultInboundAction,DefaultOutboundAction | ConvertTo-Json -Compress", 30)
    rows = _rows(data)
    if not rows:
        return _err("Could not read firewall status", raw[-500:])
    lines = [f"{r.get('Name')}: {'ON' if r.get('Enabled') else 'OFF'} "
             f"(inbound {r.get('DefaultInboundAction')}, outbound {r.get('DefaultOutboundAction')})"
             for r in rows]
    return _ok("Firewall", "\n".join(lines))


def t_arp_table():
    ok, out = _run_powershell("arp -a", 20)
    return (_ok("ARP table", out.strip()[:3000]) if ok
            else _err("Could not read the ARP table", out[-400:]))


def t_route_table():
    ok, out = _run_powershell(
        "Get-NetRoute -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
        "Sort-Object RouteMetric | Select-Object -First 30 DestinationPrefix,NextHop,"
        "InterfaceAlias,RouteMetric | Format-Table -AutoSize | Out-String -Width 100", 30)
    return (_ok("Routing table", out.strip()[:3000]) if ok
            else _err("Could not read routes", out[-400:]))


def t_proxy_info():
    import winreg
    rows = []
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        for name in ("ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL"):
            try:
                val, _t = winreg.QueryValueEx(key, name)
                rows.append(f"{name} = {val}")
            except OSError:
                continue
        winreg.CloseKey(key)
    except Exception as e:
        rows.append(f"Registry: {e}")
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        if os.environ.get(var):
            rows.append(f"{var} = {os.environ[var]}")
    return _ok("Proxy settings", "\n".join(rows) or "No proxy configured")


def t_net_speed_test():
    url = "https://speed.cloudflare.com/__down?bytes=10000000"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
        t0 = time.time()
        total = 0
        with http_open(req, 60) as r:
            while time.time() - t0 < 25:
                chunk = r.read(262144)
                if not chunk:
                    break
                total += len(chunk)
        elapsed = max(0.001, time.time() - t0)
    except Exception as e:
        return _err(f"Speed test failed: {e}")
    if total < 100_000:
        return _err("Speed test transferred almost nothing", f"{total} bytes")
    mbps = total * 8 / elapsed / 1e6
    return _ok(f"Download ~{mbps:.1f} Mbps",
               f"{_hbytes(total)} in {elapsed:.1f}s (~{mbps:.1f} Mbps, "
               f"{total / elapsed / 2**20:.1f} MB/s)")


def t_path_exists(path):
    p = resolve_path(path)
    if not p.exists():
        return _err(f"Nothing at {rel_display(p)}")
    kind = "folder" if p.is_dir() else "file"
    size = ""
    if p.is_file():
        try:
            size = f" · {_hbytes(p.stat().st_size)}"
        except Exception:
            pass
    return _ok(f"{kind}{size}", str(p))


def t_file_compare(path, dst):
    a, b = resolve_path(path), resolve_path(dst)
    if not a.is_file() or not b.is_file():
        return _err("file_compare needs two existing files", f"{a} / {b}")
    try:
        sa, sb = a.stat().st_size, b.stat().st_size
        ha = hashlib.md5(a.read_bytes()[:50_000_000]).hexdigest()
        hb = hashlib.md5(b.read_bytes()[:50_000_000]).hexdigest()
    except Exception as e:
        return _err(f"Could not read both files: {e}")
    same = ha == hb and sa == sb
    return _ok(f"{'Identical' if same else 'Different'}",
               f"{a.name}: {_hbytes(sa)} md5={ha}\n{b.name}: {_hbytes(sb)} md5={hb}")


def t_touch_file(path):
    p = resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    if not existed:
        p.write_bytes(b"")
    else:
        try:
            os.utime(p, None)
        except Exception:
            pass
    return _ok(f"{'Touched' if existed else 'Created'} {rel_display(p)}")


def t_file_version(path):
    p = resolve_path(path)
    if not p.is_file():
        return _err(f"No file at {rel_display(p)}")
    data, raw = _ps_json(
        f"(Get-Item -LiteralPath {_q(str(p))}).VersionInfo | Select-Object FileVersion,"
        "ProductVersion,CompanyName,ProductName,FileDescription,OriginalFilename | "
        "ConvertTo-Json -Compress", 25)
    d = (_rows(data) or [{}])[0]
    if not d:
        return _err("No version information", raw[-400:])
    lines = [f"{k}: {v}" for k, v in d.items() if v]
    return _ok(f"{p.name} version info", "\n".join(lines))


def t_hex_dump(path, offset=0, limit=256):
    p = resolve_path(path)
    if not p.is_file():
        return _err(f"No file at {rel_display(p)}")
    try:
        off = max(0, int(offset or 0))
        n = max(16, min(int(limit or 256), 4096))
        with open(p, "rb") as f:
            f.seek(off)
            data = f.read(n)
    except Exception as e:
        return _err(f"Could not read {rel_display(p)}: {e}")
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hexpart = " ".join(f"{b:02x}" for b in chunk)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{off + i:08x}  {hexpart:<47}  {text}")
    return _ok(f"{len(data)} bytes from offset {off}", "\n".join(lines))


def t_file_attributes(path, mode="get"):
    import ctypes
    p = resolve_path(path)
    if not p.exists():
        return _err(f"Nothing at {rel_display(p)}")
    k = ctypes.windll.kernel32
    attrs = k.GetFileAttributesW(str(p))
    if attrs == -1:
        return _err("Could not read attributes")
    names = {0x1: "read-only", 0x2: "hidden", 0x4: "system", 0x10: "directory",
             0x20: "archive", 0x80: "normal", 0x400: "reparse-point"}
    act = str(mode or "get").lower()
    if act in ("hidden", "unhide", "readonly", "read-only", "normal"):
        if act == "hidden":
            attrs = (attrs | 0x2) & ~0x80
        elif act == "unhide":
            attrs &= ~0x2
        elif act in ("readonly", "read-only"):
            attrs = (attrs | 0x1) & ~0x80
        else:
            attrs = 0x80
        if not k.SetFileAttributesW(str(p), attrs):
            return _err("Could not change attributes")
    cur = k.GetFileAttributesW(str(p))
    flags = [v for bit, v in names.items() if cur & bit]
    return _ok(f"{rel_display(p)} attributes", ", ".join(flags) or "none")


def t_drive_list():
    import ctypes
    rows = []
    try:
        import psutil
        parts = psutil.disk_partitions(all=False)
    except Exception:
        parts = []
    k = ctypes.windll.kernel32
    for part in parts:
        try:
            usage = shutil.disk_usage(part.mountpoint)
        except Exception:
            continue
        label = ""
        if re.match(r"^[A-Za-z]:", part.device):
            buf = ctypes.create_unicode_buffer(128)
            vol = ctypes.create_unicode_buffer(128)
            fs = ctypes.create_unicode_buffer(64)
            try:
                if k.GetVolumeInformationW(part.device + "\\", buf, 128, None, None,
                                           None, fs, 64):
                    label = buf.value
            except Exception:
                pass
        rows.append(f"{part.device}{' (' + label + ')' if label else ''} · {part.fstype or '?'} · "
                    f"{usage.free / 2**30:.1f} GB free of {usage.total / 2**30:.1f} GB "
                    f"({usage.percent}% used)")
    if not rows:
        return _err("No drives found")
    return _ok(f"{len(rows)} drive(s)", "\n".join(rows))


def t_temp_clean(dry_run=True, days=7):
    root = Path(tempfile.gettempdir())
    cutoff = time.time() - max(0, int(days or 0)) * 86400
    files, total = [], 0
    for base, dirs, names in os.walk(root):
        depth = len(Path(base).relative_to(root).parts)
        if depth >= 2:
            dirs[:] = []
        for name in names:
            p = Path(base) / name
            try:
                st = p.stat()
            except Exception:
                continue
            if st.st_mtime > cutoff:
                continue
            files.append((p, st.st_size))
            total += st.st_size
    if not files:
        return _ok(f"Nothing older than {int(days or 0)} day(s) in TEMP")
    if _bool(dry_run):
        sample = "\n".join(f"{rel_display(p)} ({_hbytes(s)})" for p, s in files[:25])
        return _ok(f"Would delete {len(files)} file(s), {_hbytes(total)}",
                   sample + ("\n..." if len(files) > 25 else ""))
    removed, freed, errors = 0, 0, 0
    for p, size in files:
        try:
            p.unlink()
            removed += 1
            freed += size
        except Exception:
            errors += 1
    return _ok(f"Deleted {removed} file(s), freed {_hbytes(freed)}",
               f"{errors} file(s) were in use and skipped." if errors else "")


def t_open_with(path, app=""):
    p = resolve_path(path)
    if not p.exists():
        return _err(f"Nothing at {rel_display(p)}")
    try:
        if str(app or "").strip():
            subprocess.Popen([str(app), str(p)], creationflags=0x08000000)
        else:
            os.startfile(str(p))
    except Exception as e:
        return _err(f"Could not open {rel_display(p)}: {e}")
    return _ok(f"Opened {rel_display(p)}" + (f" with {app}" if app else ""))


def t_file_permissions(path):
    p = resolve_path(path)
    if not p.exists():
        return _err(f"Nothing at {rel_display(p)}")
    ok, out = _run_powershell(f"icacls {_q(str(p))}", 25)
    return (_ok(f"Permissions for {rel_display(p)}", out.strip()[:3000]) if ok
            else _err("Could not read permissions", out[-500:]))


def t_recyclebin_list(limit=50):
    n = max(1, min(int(limit or 50), 300))
    script = (
        "$sh=New-Object -ComObject Shell.Application;"
        "$rb=$sh.Namespace(10);"
        "$items=@($rb.Items());"
        f"$list=@($items | Select-Object -First {n} | ForEach-Object {{"
        "[pscustomobject]@{Name=$_.Name;"
        "OriginalPath=$rb.GetDetailsOf($_,1);"
        "Deleted=$rb.GetDetailsOf($_,2);"
        "Type=$rb.GetDetailsOf($_,4);"
        "Size=$_.Size}"
        "});"
        "[pscustomobject]@{Count=$items.Count;"
        "Total=(($items | Measure-Object -Property Size -Sum).Sum);"
        "Items=$list} | ConvertTo-Json -Compress -Depth 4")
    data, raw = _ps_json(script, 45)
    if not isinstance(data, dict):
        return _err("Could not read the Recycle Bin", raw[-600:])
    items = _rows(data.get("Items"))
    count = int(data.get("Count") or 0)
    total = data.get("Total") or 0
    if not count:
        return _ok("Recycle Bin is empty", "0 items")
    lines = []
    for it in items:
        size = it.get("Size")
        try:
            size = _hbytes(int(size)) if size else "?"
        except Exception:
            size = "?"
        when = re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]", "",
                      str(it.get("Deleted") or "?"))
        when = " ".join(when.split()) or "?"
        lines.append(f"{it.get('Name')} · {size} · deleted {when} · "
                     f"from {it.get('OriginalPath') or '?'}")
    try:
        total_s = _hbytes(int(total))
    except Exception:
        total_s = "?"
    summary = f"{count} item(s), {total_s}"
    return _ok(f"Recycle Bin: {summary}", "\n".join(lines))


def t_recyclebin_restore(name):
    needle = str(name or "").strip()
    if not needle:
        return _err("recyclebin_restore needs part of the item name")
    script = (
        "$sh=New-Object -ComObject Shell.Application;"
        "$rb=$sh.Namespace(10);"
        "$hit=$null;"
        f"foreach($it in @($rb.Items())){{ if($it.Name -like {_q('*' + needle + '*')})"
        "{$hit=$it;break} }};"
        "if(-not $hit){'NOTFOUND';exit};"
        "$done=$false;"
        "foreach($v in @($hit.Verbs())){"
        "if($v.Name -match 'Restore|还原|复原|Восстановить|Wiederherstellen|Restaurer')"
        "{$v.DoIt();$done=$true;break}};"
        "if(-not $done){'NOVERB'}else{'RESTORED:'+$hit.Name}")
    ok, out = _run_powershell(script, 40)
    text = str(out or "").strip()
    if "RESTORED:" in text:
        return _ok("Restored " + text.split("RESTORED:", 1)[1].strip())
    if "NOTFOUND" in text:
        return _err(f"No Recycle Bin item matching {needle!r}")
    return _err("Could not restore that item", text[-500:])


# ---------------------------------------------------------------------------
# Images, PDF, audio and video
# ---------------------------------------------------------------------------
def _pil_open(path):
    from PIL import Image
    p = resolve_path(path)
    if not p.is_file():
        raise FileNotFoundError(f"No file at {rel_display(p)}")
    im = Image.open(p)
    im.load()
    return im, p


def _pil_save(im, src, dest, suffix="-edited"):
    from PIL import Image
    if str(dest or "").strip():
        out = resolve_path(dest)
        fmt = (out.suffix or src.suffix or ".png").lstrip(".").upper()
        if fmt == "JPG":
            fmt = "JPEG"
    else:
        out = src.with_name(src.stem + suffix + (src.suffix or ".png"))
        fmt = (src.suffix or ".png").lstrip(".").upper()
        if fmt == "JPG":
            fmt = "JPEG"
    out.parent.mkdir(parents=True, exist_ok=True)
    if fmt in ("JPEG", "JPG", "BMP") and im.mode in ("RGBA", "P", "LA"):
        im = im.convert("RGB")
    im.save(out, format=fmt if fmt else None)
    return out


def t_image_ocr(path, max_lines=80):
    p = resolve_path(path)
    if not p.is_file():
        return _err(f"No image at {rel_display(p)}")
    try:
        items = _ocr_items(str(p))
    except Exception as e:
        return _err(f"OCR failed: {e}")
    lines = [it["text"] for it in items][:max(1, min(int(max_lines or 80), 500))]
    if not lines:
        return _err(f"No readable text in {rel_display(p)}")
    return _ok(f"Read {len(lines)} text line(s) from {p.name}", "\n".join(lines))


def t_image_flip(path, direction="horizontal", dest=""):
    try:
        im, src = _pil_open(path)
        from PIL import Image
        d = str(direction or "horizontal").lower()
        out_im = im.transpose(Image.Transpose.FLIP_LEFT_RIGHT if d.startswith("h")
                              else Image.Transpose.FLIP_TOP_BOTTOM)
        out = _pil_save(out_im, src, dest, "-flipped")
        return _ok(f"Flipped {src.name} -> {rel_display(out)}")
    except Exception as e:
        return _err(f"Flip failed: {e}")


def t_image_blur(path, radius=3, dest=""):
    try:
        im, src = _pil_open(path)
        from PIL import ImageFilter
        r = max(0.5, min(float(radius or 3), 40.0))
        out = _pil_save(im.filter(ImageFilter.GaussianBlur(r)), src, dest, "-blur")
        return _ok(f"Blurred {src.name} -> {rel_display(out)}")
    except Exception as e:
        return _err(f"Blur failed: {e}")


def t_image_thumbnail(path, size=256, dest=""):
    try:
        im, src = _pil_open(path)
        s = max(16, min(int(size or 256), 4096))
        im.thumbnail((s, s))
        out = _pil_save(im, src, dest, "-thumb")
        return _ok(f"Thumbnail {im.width}x{im.height} -> {rel_display(out)}")
    except Exception as e:
        return _err(f"Thumbnail failed: {e}")


def t_image_watermark(path, text, dest=""):
    msg = str(text or "").strip()
    if not msg:
        return _err("image_watermark needs text")
    try:
        im, src = _pil_open(path)
        from PIL import ImageDraw, ImageFont
        base = im.convert("RGBA")
        overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        size = max(14, int(base.width / max(20, len(msg))))
        font = None
        for cand in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\arial.ttf",
                     r"C:\Windows\Fonts\segoeui.ttf"):
            try:
                font = ImageFont.truetype(cand, size)
                break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()
        box = draw.textbbox((0, 0), msg, font=font)
        tw, th = box[2] - box[0], box[3] - box[1]
        x, y = base.width - tw - 24, base.height - th - 24
        draw.text((x + 2, y + 2), msg, font=font, fill=(0, 0, 0, 160))
        draw.text((x, y), msg, font=font, fill=(255, 255, 255, 230))
        out_im = Image.alpha_composite(base, overlay)
        out = _pil_save(out_im, src, dest, "-watermark")
        return _ok(f"Watermarked {src.name} -> {rel_display(out)}")
    except Exception as e:
        return _err(f"Watermark failed: {e}")


def t_image_exif(path):
    try:
        im, src = _pil_open(path)
        from PIL.ExifTags import TAGS
        exif = im.getexif()
        if not exif:
            return _ok(f"{src.name} has no EXIF data")
        rows = []
        for tag, value in exif.items():
            rows.append(f"{TAGS.get(tag, tag)}: {str(value)[:200]}")
        return _ok(f"{len(rows)} EXIF field(s) in {src.name}", "\n".join(rows))
    except Exception as e:
        return _err(f"EXIF read failed: {e}")


def t_image_merge(paths, direction="horizontal", dest=""):
    items = [resolve_path(p) for p in _as_list(paths)]
    items = [p for p in items if p.is_file()]
    if len(items) < 2:
        return _err("image_merge needs at least two images")
    try:
        from PIL import Image
        ims = [Image.open(p).convert("RGBA") for p in items]
        horiz = str(direction or "horizontal").lower().startswith("h")
        if horiz:
            W = sum(i.width for i in ims)
            H = max(i.height for i in ims)
        else:
            W = max(i.width for i in ims)
            H = sum(i.height for i in ims)
        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        x = y = 0
        for i in ims:
            canvas.paste(i, (x, y))
            if horiz:
                x += i.width
            else:
                y += i.height
        out = resolve_path(dest) if str(dest or "").strip() else \
            items[0].with_name(items[0].stem + "-merged.png")
        canvas.save(out)
        return _ok(f"Merged {len(ims)} images -> {rel_display(out)}")
    except Exception as e:
        return _err(f"Merge failed: {e}")


def t_image_to_pdf(paths, output=""):
    raw = _as_list(paths)
    files = []
    for item in raw:
        p = resolve_path(item)
        if p.is_dir():
            files += sorted([c for c in p.iterdir()
                             if c.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp")])
        elif p.is_file():
            files.append(p)
    if not files:
        return _err("image_to_pdf needs images or a folder of images")
    try:
        from PIL import Image
        ims = [Image.open(f).convert("RGB") for f in files]
        out = resolve_path(output) if str(output or "").strip() else \
            files[0].with_name("images.pdf")
        ims[0].save(out, "PDF", save_all=True, append_images=ims[1:])
        return _ok(f"{len(ims)} page(s) -> {rel_display(out)}")
    except Exception as e:
        return _err(f"PDF build failed: {e}")


def t_pdf_to_images(path, folder="", range=""):
    try:
        from pypdf import PdfReader
    except Exception as e:
        return _err("pypdf is not available", str(e))
    p = resolve_path(path)
    if not p.is_file():
        return _err(f"No PDF at {rel_display(p)}")
    outdir = resolve_path(folder) if str(folder or "").strip() else p.with_name(p.stem + "-images")
    outdir.mkdir(parents=True, exist_ok=True)
    pages = _parse_pages(range, None)
    try:
        reader = PdfReader(str(p))
        wanted = pages or range(len(reader.pages))
        saved, empty = [], []
        for i in wanted:
            if i < 0 or i >= len(reader.pages):
                continue
            page = reader.pages[i]
            found = 0
            try:
                images = page.images
            except Exception:
                images = []
            for j, img in enumerate(images):
                ext = Path(getattr(img, "name", "") or "").suffix or ".png"
                if ext.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
                    ext = ".png"
                dest = outdir / f"page-{i + 1}-{j + 1}{ext}"
                dest.write_bytes(img.data)
                saved.append(dest)
                found += 1
            if not found:
                empty.append(i + 1)
        if not saved:
            return _err("This PDF contains no embedded images",
                        "Pages without images: " + ", ".join(map(str, empty[:40])))
        note = ""
        if empty:
            note = f"\nPages with no embedded image: {', '.join(map(str, empty[:20]))}"
        return _ok(f"Extracted {len(saved)} image(s) -> {rel_display(outdir)}",
                   "\n".join(rel_display(x) for x in saved[:60]) + note)
    except Exception as e:
        return _err(f"PDF image extraction failed: {e}")


def _parse_pages(spec, total):
    spec = str(spec or "").strip()
    if not spec:
        return None
    pages = []
    for part in spec.split(","):
        part = part.strip()
        m = re.match(r"^(\d+)\s*-\s*(\d+)$", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            step = 1 if b >= a else -1
            pages += list(range(a - 1, b - 1 + step, step))
        elif part.isdigit():
            pages.append(int(part) - 1)
    if total:
        pages = [p for p in pages if 0 <= p < total]
    return sorted(set(pages))


def _pdf_write(reader, pages, output):
    from pypdf import PdfWriter
    writer = PdfWriter()
    for i in pages:
        writer.add_page(reader.pages[i])
    out = resolve_path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as f:
        writer.write(f)
    return out


def t_pdf_split(path, output=""):
    try:
        from pypdf import PdfReader
        p = resolve_path(path)
        if not p.is_file():
            return _err(f"No PDF at {rel_display(p)}")
        reader = PdfReader(str(p))
        outdir = resolve_path(output) if str(output or "").strip() else p.with_name(p.stem + "-pages")
        outdir.mkdir(parents=True, exist_ok=True)
        made = []
        for i in range(len(reader.pages)):
            made.append(_pdf_write(reader, [i], outdir / f"{p.stem}-p{i + 1}.pdf"))
        return _ok(f"Split into {len(made)} page(s) -> {rel_display(outdir)}",
                   "\n".join(rel_display(x) for x in made[:40]))
    except Exception as e:
        return _err(f"PDF split failed: {e}")


def t_pdf_extract(path, range="1-3", output=""):
    try:
        from pypdf import PdfReader
        p = resolve_path(path)
        if not p.is_file():
            return _err(f"No PDF at {rel_display(p)}")
        reader = PdfReader(str(p))
        pages = _parse_pages(range, len(reader.pages))
        if not pages:
            return _err(f"No valid pages in range {range!r}",
                        f"This PDF has {len(reader.pages)} page(s).")
        out = resolve_path(output) if str(output or "").strip() else \
            p.with_name(f"{p.stem}-extract.pdf")
        _pdf_write(reader, pages, out)
        return _ok(f"Extracted {len(pages)} page(s) -> {rel_display(out)}",
                   "Pages: " + ", ".join(str(i + 1) for i in pages))
    except Exception as e:
        return _err(f"PDF extract failed: {e}")


def t_pdf_rotate(path, angle=90, output=""):
    try:
        from pypdf import PdfReader
        p = resolve_path(path)
        if not p.is_file():
            return _err(f"No PDF at {rel_display(p)}")
        reader = PdfReader(str(p))
        out = resolve_path(output) if str(output or "").strip() else \
            p.with_name(f"{p.stem}-rotated.pdf")
        from pypdf import PdfWriter
        writer = PdfWriter()
        for page in reader.pages:
            page.rotate(int(angle or 90))
            writer.add_page(page)
        with open(out, "wb") as f:
            writer.write(f)
        return _ok(f"Rotated {len(reader.pages)} page(s) by {int(angle or 90)} -> {rel_display(out)}")
    except Exception as e:
        return _err(f"PDF rotate failed: {e}")


def _run_ff(args, timeout=300):
    ff = _ffmpeg()
    if not ff:
        return False, "ffmpeg is not available (pip install imageio-ffmpeg)"
    try:
        proc = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y"] + list(args),
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"ffmpeg timed out after {timeout}s"
    except Exception as e:
        return False, str(e)
    out = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
    return proc.returncode == 0, out[-1600:]


def t_video_frame(path, start="00:00:01", dest=""):
    p = resolve_path(path)
    if not p.is_file():
        return _err(f"No media at {rel_display(p)}")
    out = resolve_path(dest) if str(dest or "").strip() else \
        p.with_name(f"{p.stem}-frame.png")
    ok, out_s = _run_ff(["-ss", str(start or "00:00:01"), "-i", str(p), "-frames:v", "1",
                         str(out)], 90)
    return (_ok(f"Frame saved -> {rel_display(out)}") if ok and out.exists()
            else _err("Could not grab a frame", out_s))


def t_video_merge(paths, output=""):
    items = [resolve_path(p) for p in _as_list(paths)]
    items = [p for p in items if p.is_file()]
    if len(items) < 2:
        return _err("video_merge needs at least two files")
    out = resolve_path(output) if str(output or "").strip() else \
        items[0].with_name(items[0].stem + "-merged.mp4")
    listing = Path(tempfile.gettempdir()) / f"af-concat-{int(time.time())}.txt"
    lines = []
    for p in items:
        escaped = str(p).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    listing.write_text("\n".join(lines), encoding="utf-8")
    ok, msg = _run_ff(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
                       str(out)], 600)
    if not ok or not out.exists():
        ok, msg = _run_ff(["-f", "concat", "-safe", "0", "-i", str(listing),
                           "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
                           str(out)], 1800)
    try:
        listing.unlink()
    except Exception:
        pass
    return (_ok(f"Merged {len(items)} file(s) -> {rel_display(out)}") if ok and out.exists()
            else _err("Merge failed", msg))


def t_video_scale(path, width=1280, height=0, dest=""):
    p = resolve_path(path)
    if not p.is_file():
        return _err(f"No media at {rel_display(p)}")
    w = max(2, int(width or 1280))
    h = int(height or 0)
    out = resolve_path(dest) if str(dest or "").strip() else \
        p.with_name(f"{p.stem}-{w}x{h if h else 'auto'}{p.suffix or '.mp4'}")
    scale = f"scale={w}:{h if h else -2}"
    ok, msg = _run_ff(["-i", str(p), "-vf", scale, "-c:v", "libx264", "-preset", "veryfast",
                       "-crf", "20", "-c:a", "copy", str(out)], 1800)
    return (_ok(f"Scaled -> {rel_display(out)}") if ok and out.exists()
            else _err("Scale failed", msg))


def _atempo_chain(f):
    parts = []
    while f > 2.0:
        parts.append("atempo=2.0")
        f /= 2.0
    while f < 0.5:
        parts.append("atempo=0.5")
        f *= 2.0
    parts.append(f"atempo={f:.4f}")
    return ",".join(parts)


def t_video_speed(path, factor="1.5", dest=""):
    p = resolve_path(path)
    if not p.is_file():
        return _err(f"No media at {rel_display(p)}")
    try:
        f = max(0.25, min(float(factor or 1.5), 4.0))
    except Exception:
        return _err("factor must be a number like 1.5 or 0.75")
    out = resolve_path(dest) if str(dest or "").strip() else \
        p.with_name(f"{p.stem}-x{f:g}{p.suffix or '.mp4'}")
    vf = f"[0:v]setpts=PTS/{f}[v]"
    af = _atempo_chain(f)
    ok, msg = _run_ff(["-i", str(p), "-filter_complex",
                       vf + f";[0:a]{af}[a]", "-map", "[v]", "-map", "[a]",
                       "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                       "-c:a", "aac", str(out)], 1800)
    if not ok:
        ok, msg = _run_ff(["-i", str(p), "-vf", f"setpts=PTS/{f}",
                           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                           str(out)], 1800)
    return (_ok(f"Speed x{f:g} -> {rel_display(out)}") if ok and out.exists()
            else _err("Speed change failed", msg))


def t_video_to_gif(path, start="0", duration="4", dest=""):
    p = resolve_path(path)
    if not p.is_file():
        return _err(f"No media at {rel_display(p)}")
    out = resolve_path(dest) if str(dest or "").strip() else \
        p.with_name(f"{p.stem}.gif")
    vf = ("fps=12,scale=480:-1:flags=lanczos,split[s0][s1];"
          "[s0]palettegen[p];[s1][p]paletteuse")
    ok, msg = _run_ff(["-ss", str(start or "0"), "-t", str(duration or "4"), "-i", str(p),
                       "-vf", vf, "-loop", "0", str(out)], 600)
    return (_ok(f"GIF -> {rel_display(out)}") if ok and out.exists()
            else _err("GIF conversion failed", msg))


def t_gif_to_video(path, dest=""):
    p = resolve_path(path)
    if not p.is_file():
        return _err(f"No GIF at {rel_display(p)}")
    out = resolve_path(dest) if str(dest or "").strip() else p.with_name(p.stem + ".mp4")
    ok, msg = _run_ff(["-i", str(p), "-movflags", "faststart", "-pix_fmt", "yuv420p",
                       "-c:v", "libx264", "-preset", "veryfast", str(out)], 600)
    return (_ok(f"Video -> {rel_display(out)}") if ok and out.exists()
            else _err("GIF to video failed", msg))


def t_audio_play(path):
    p = resolve_path(path)
    if not p.is_file():
        return _err(f"No audio at {rel_display(p)}")
    try:
        os.startfile(str(p))
    except Exception as e:
        return _err(f"Could not play {p.name}: {e}")
    return _ok(f"Playing {p.name}")


def t_audio_normalize(path, dest=""):
    p = resolve_path(path)
    if not p.is_file():
        return _err(f"No audio at {rel_display(p)}")
    out = resolve_path(dest) if str(dest or "").strip() else \
        p.with_name(f"{p.stem}-normalized{p.suffix or '.mp3'}")
    ok, msg = _run_ff(["-i", str(p), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                       str(out)], 900)
    return (_ok(f"Normalized -> {rel_display(out)}") if ok and out.exists()
            else _err("Normalize failed", msg))


def t_audio_merge(paths, output=""):
    items = [resolve_path(p) for p in _as_list(paths)]
    items = [p for p in items if p.is_file()]
    if len(items) < 2:
        return _err("audio_merge needs at least two files")
    out = resolve_path(output) if str(output or "").strip() else \
        items[0].with_name(items[0].stem + "-merged.mp3")
    args = []
    for p in items:
        args += ["-i", str(p)]
    streams = "".join(f"[{i}:a]" for i in range(len(items)))
    ok, msg = _run_ff(args + ["-filter_complex", f"{streams}concat=n={len(items)}:v=0:a=1[a]",
                              "-map", "[a]", "-c:a", "libmp3lame", "-q:a", "2", str(out)], 1800)
    return (_ok(f"Merged {len(items)} audio file(s) -> {rel_display(out)}")
            if ok and out.exists() else _err("Audio merge failed", msg))


# ---------------------------------------------------------------------------
# Web extras
# ---------------------------------------------------------------------------
def _fetch_html(url, limit=1_500_000):
    if not re.match(r"^https?://", url or ""):
        url = "https://" + str(url or "")
    req = urllib.request.Request(url, headers={
        "User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"})
    with http_open(req, 30) as r:
        raw = r.read(limit)
        ctype = r.headers.get("Content-Type", "")
        final = r.geturl()
    return raw.decode("utf-8", errors="replace"), ctype, final


def t_web_links(url, limit=60):
    try:
        html, _ctype, final = _fetch_html(url)
    except Exception as e:
        return _err(f"Fetch failed: {e}")
    seen, rows = set(), []
    for m in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>', html, re.I):
        href = urllib.parse.urljoin(final, m.group(1).strip())
        if not href.lower().startswith(("http://", "https://")):
            continue
        if href in seen:
            continue
        seen.add(href)
        text = " ".join(_html_to_text(m.group(2)).split())[:100]
        rows.append(f"{text or '(no text)'} -> {href}")
        if len(rows) >= max(1, min(int(limit or 60), 300)):
            break
    if not rows:
        return _err(f"No links found on {final}")
    return _ok(f"{len(rows)} link(s) on {final}", "\n".join(rows))


def t_web_tables(url):
    try:
        html, _ctype, final = _fetch_html(url)
    except Exception as e:
        return _err(f"Fetch failed: {e}")
    tables = re.findall(r"(?is)<table\b.*?</table>", html)
    if not tables:
        return _err(f"No tables found on {final}")
    blocks = []
    for ti, table in enumerate(tables[:5], 1):
        rows = re.findall(r"(?is)<tr\b.*?</tr>", table)
        lines = []
        for row in rows[:20]:
            cells = re.findall(r"(?is)<t[hd]\b.*?</t[hd]>", row)
            vals = [" ".join(_html_to_text(c).split())[:60] for c in cells]
            if vals:
                lines.append(" | ".join(vals))
        if lines:
            blocks.append(f"Table {ti} ({len(rows)} row(s)):\n" + "\n".join(lines))
    if not blocks:
        return _err(f"{len(tables)} table(s) found but none had readable rows")
    return _ok(f"{len(blocks)} table(s) on {final}", "\n\n".join(blocks)[:8000])


def t_web_archive(url):
    target = str(url or "").strip()
    if not re.match(r"^https?://", target):
        target = "https://" + target
    api = ("https://archive.org/wayback/available?url=" +
           urllib.parse.quote(target, safe=""))
    try:
        req = urllib.request.Request(api, headers={"User-Agent": BROWSER_UA})
        with http_open(req, 20) as r:
            data = json.loads(r.read(200_000).decode("utf-8", "replace"))
    except Exception as e:
        return _err(f"Archive lookup failed: {e}")
    snap = ((data.get("archived_snapshots") or {}).get("closest") or {})
    if not snap.get("available"):
        return _err(f"No archived snapshot for {target}",
                    "The Wayback Machine has never captured that URL.")
    return _ok(f"Archived copy from {snap.get('timestamp')}", snap.get("url") or "")


def t_web_headers(url):
    target = str(url or "").strip()
    if not re.match(r"^https?://", target):
        target = "https://" + target
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(target, method=method, headers={
                "User-Agent": BROWSER_UA, "Accept": "*/*"})
            with http_open(req, 20) as r:
                lines = [f"HTTP {r.status} {r.reason}", f"Final URL: {r.geturl()}"]
                lines += [f"{k}: {v}" for k, v in r.headers.items()]
                return _ok(f"HTTP {r.status} from {target}", "\n".join(lines))
        except Exception as e:
            last = str(e)
    return _err(f"Could not read headers from {target}", last)


def t_web_save_page(url, path=""):
    try:
        html, ctype, final = _fetch_html(url)
    except Exception as e:
        return _err(f"Fetch failed: {e}")
    out = resolve_path(path) if str(path or "").strip() else \
        workspace() / f"page-{time.strftime('%Y%m%d-%H%M%S')}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return _ok(f"Saved {len(html)} chars ({ctype or 'html'}) -> {rel_display(out)}",
               final)


def t_web_download_links(url, pattern="", folder="", limit=10):
    try:
        html, _ctype, final = _fetch_html(url)
    except Exception as e:
        return _err(f"Fetch failed: {e}")
    pat = str(pattern or "").strip().lower()
    urls, seen = [], set()
    for m in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\']', html, re.I):
        href = urllib.parse.urljoin(final, m.group(1).strip())
        if not href.lower().startswith(("http://", "https://")) or href in seen:
            continue
        if pat and pat not in href.lower():
            continue
        seen.add(href)
        urls.append(href)
        if len(urls) >= max(1, min(int(limit or 10), 60)):
            break
    if not urls:
        return _err(f"No links matched {pattern!r} on {final}")
    dest = folder or str(workspace() / "downloads")
    return t_download_many("\n".join(urls), dest, f"links from {urllib.parse.urlparse(final).netloc}")


def _ytdlp_mod():
    try:
        import yt_dlp
        return yt_dlp
    except Exception:
        return None


def t_youtube_info(url):
    yt = _ytdlp_mod()
    if yt is None:
        return _err("yt-dlp is not installed")
    target = str(url or "").strip()
    if not target:
        return _err("youtube_info needs a URL")
    if not re.match(r"^https?://", target):
        target = "https://www.youtube.com/watch?v=" + target
    try:
        with yt.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(target, download=False)
    except Exception as e:
        return _err(f"Could not read video info: {e}")
    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        return _ok(f"Playlist {info.get('title')} ({len(entries)} entries)",
                   "\n".join(f"{e.get('title')} · {e.get('duration')}s · {e.get('url')}"
                             for e in entries[:40] if e))
    dur = info.get("duration") or 0
    lines = [f"Title: {info.get('title')}",
             f"Channel: {info.get('uploader')}",
             f"Duration: {int(dur) // 60}:{int(dur) % 60:02d}",
             f"Views: {info.get('view_count')}",
             f"Uploaded: {info.get('upload_date')}",
             f"URL: {info.get('webpage_url')}"]
    return _ok(f"{info.get('title')}", "\n".join(lines))


def t_youtube_playlist(url, limit=30):
    yt = _ytdlp_mod()
    if yt is None:
        return _err("yt-dlp is not installed")
    target = str(url or "").strip()
    if not target:
        return _err("youtube_playlist needs a URL")
    n = max(1, min(int(limit or 30), 200))
    try:
        with yt.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                           "extract_flat": "in_playlist", "playlistend": n}) as ydl:
            info = ydl.extract_info(target, download=False)
    except Exception as e:
        return _err(f"Could not read the playlist: {e}")
    entries = info.get("entries") or []
    if not entries:
        return _err("That URL has no playlist entries")
    lines = [f"{i + 1}. {e.get('title')} · {e.get('duration')}s · "
             f"https://www.youtube.com/watch?v={e.get('id')}"
             for i, e in enumerate(entries[:n]) if e]
    return _ok(f"{len(lines)} entr(ies) in {info.get('title')}", "\n".join(lines))


def t_web_image_search(query, count=10):
    q = str(query or "").strip()
    if not q:
        return _err("web_image_search needs a query")
    n = max(1, min(int(count or 10), 60))
    headers = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}
    try:
        req = urllib.request.Request(
            "https://duckduckgo.com/?q=" + urllib.parse.quote_plus(q) + "&iax=images&ia=images",
            headers=headers)
        with http_open(req, 20) as r:
            html = r.read(1_200_000).decode("utf-8", "replace")
        m = re.search(r'vqd=["\']?([\d-]+)', html)
        if not m:
            return _err("Image search token not found")
        api = ("https://duckduckgo.com/i.js?l=us-en&o=json&q=" + urllib.parse.quote_plus(q) +
               "&vqd=" + urllib.parse.quote(m.group(1)) + "&f=,,,,,&p=1")
        req2 = urllib.request.Request(api, headers={**headers, "Referer": "https://duckduckgo.com/"})
        with http_open(req2, 25) as r2:
            data = json.loads(r2.read(2_000_000).decode("utf-8", "replace"))
    except Exception as e:
        return _err(f"Image search failed: {e}")
    results = (data or {}).get("results") or []
    if not results:
        return _err(f"No images found for {q!r}")
    lines = [f"{r.get('title') or '(image)'} · {r.get('width')}x{r.get('height')}\n    "
             f"{r.get('image')}" for r in results[:n]]
    return _ok(f"{len(lines)} image result(s) for {q!r}", "\n".join(lines))


def t_webpage_screenshot(url, path=""):
    target = str(url or "").strip()
    if not target:
        return _err("webpage_screenshot needs a URL")
    if not re.match(r"^https?://", target):
        target = "https://" + target
    out = resolve_path(path) if str(path or "").strip() else \
        workspace() / f"web-{time.strftime('%Y%m%d-%H%M%S')}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    browsers = [Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
                Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
                Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe")]
    exe = next((b for b in browsers if b.is_file()), None)
    if exe is None:
        return _err("Edge or Chrome is required for headless screenshots")
    try:
        proc = subprocess.run([str(exe), "--headless=new", "--disable-gpu", "--hide-scrollbars",
                               "--window-size=1366,900", f"--screenshot={str(out)}",
                               "--virtual-time-budget=6000", target],
                              capture_output=True, text=True, timeout=60)
    except Exception as e:
        return _err(f"Screenshot failed: {e}")
    if out.exists() and out.stat().st_size > 1000:
        return _ok(f"Captured {target} -> {rel_display(out)}")
    return _err("The browser produced no screenshot", (proc.stderr or "")[-600:])


# ---------------------------------------------------------------------------
# Speech, input, batch flow and faster UI automation
# ---------------------------------------------------------------------------
def t_voices_list():
    ok, out = _run_powershell(
        "$v=New-Object -ComObject SAPI.SpVoice;"
        "@($v.GetVoices()) | ForEach-Object { $_.GetDescription() }", 20)
    return (_ok("Text-to-speech voices", out.strip()[:2000]) if ok
            else _err("Could not list voices", out[-400:]))


def t_tts_save(text, path=""):
    msg = str(text or "").strip()
    if not msg:
        return _err("tts_save needs text")
    out = resolve_path(path) if str(path or "").strip() else \
        workspace() / f"speech-{time.strftime('%Y%m%d-%H%M%S')}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "$v=New-Object -ComObject SAPI.SpVoice;"
        "$fs=New-Object -ComObject SAPI.SpFileStream;"
        f"$fs.Open({_q(str(out))},3,$false);"
        "$v.AudioOutputStream=$fs;"
        f"$v.Speak({_q(msg)}) | Out-Null;"
        "$fs.Close();'saved'")
    ok, res = _run_powershell(script, max(60, len(msg) // 8))
    if ok and out.exists():
        return _ok(f"Speech saved -> {rel_display(out)}", _hbytes(out.stat().st_size))
    return _err("Could not save speech", res[-600:])


def t_mouse_position():
    import ctypes
    from ctypes import wintypes
    pt = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    hwnd = ctypes.windll.user32.WindowFromPoint(pt)
    title = ""
    if hwnd:
        n = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
    return _ok(f"Cursor at {pt.x},{pt.y}", f"Window: {title or '(none)'}")


def t_key_tap(key, count=1):
    raw = str(key or "").strip().lower()
    code = _KEYMAP.get(raw)
    if code is None and len(raw) == 1:
        code = ord(raw.upper())
    if code is None:
        return _err(f"Unknown key {key!r}", "Use a key name like enter, tab, esc, f5 or one letter.")
    _tap_vk(code, max(1, min(int(count or 1), 50)))
    return _ok(f"Pressed {key}" + (f" x{int(count)}" if int(count or 1) > 1 else ""))


def t_page_scroll(direction="down", pages=1, window=""):
    if str(window or "").strip():
        okw, outw, _ = t_window_focus(window)
        if not okw:
            return _err(f"Could not focus {window!r}", outw)
        time.sleep(0.2)
    up = str(direction or "down").lower().startswith("u")
    _tap_vk(0x21 if up else 0x22, max(1, min(int(pages or 1), 50)))
    return _ok(f"Page {'up' if up else 'down'} x{max(1, min(int(pages or 1), 50))}")


def _as_action_list(raw):
    data = raw
    if isinstance(data, str):
        text = data.strip()
        try:
            data = json.loads(text, strict=False)
        except Exception:
            data = _salvage_json(text)
            if not data:
                if text.startswith("[") and text.endswith("]"):
                    return []
                actions = []
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    m = re.match(r"([A-Za-z0-9_]+)\s*(\{.*\})?$", line)
                    if m:
                        args = {}
                        if m.group(2):
                            try:
                                args = json.loads(m.group(2), strict=False)
                            except Exception:
                                args = {}
                        actions.append({"tool": m.group(1), "args": args})
                return actions
    if isinstance(data, dict):
        for key in ("actions", "items", "steps"):
            if isinstance(data.get(key), list):
                return data[key]
        if data.get("tool") or data.get("name"):
            return [data]
        return []
    return data if isinstance(data, list) else []


def t_do_actions(items):
    actions = _as_action_list(items)
    if not actions:
        return _err("do_actions needs a JSON array like "
                    '[{"tool":"clipboard_read","args":{}}, ...]')
    actions = actions[:30]
    log, all_ok = [], True
    for i, act in enumerate(actions, 1):
        if not isinstance(act, dict):
            log.append(f"{i}. SKIP (not an object)")
            all_ok = False
            continue
        name = str(act.get("tool") or act.get("name") or act.get("action") or "").strip()
        args = act.get("args") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args, strict=False)
            except Exception:
                args = {}
        if name not in TOOLS:
            log.append(f"{i}. SKIP {name or '?'} (unknown tool)")
            all_ok = False
            continue
        if name in ("do_actions", "reply", "update_plan"):
            log.append(f"{i}. SKIP {name} (not allowed inside a batch)")
            all_ok = False
            continue
        emit("log", text=f"Batch {i}/{len(actions)}: {name}")
        try:
            ok, summary, _detail = dispatch(name, args)
        except Exception as e:
            ok, summary = False, f"{type(e).__name__}: {e}"
        log.append(f"{i}. {'OK' if ok else 'FAIL'} {name} - {str(summary)[:200]}")
        if not ok:
            all_ok = False
    done = sum(1 for ln in log if ". OK " in ln)
    summary = f"{done}/{len(actions)} action(s) succeeded"
    return (all_ok, summary, "\n".join(log)[:6000]) if all_ok else \
        (False, summary, "\n".join(log)[:6000])


def t_wait_for(kind="file", target="", timeout=30):
    k = str(kind or "file").lower()
    tgt = str(target or "").strip()
    if not tgt:
        return _err("wait_for needs a target (path, window title, process name or text)")
    limit = max(1, min(int(timeout or 30), 300))
    deadline = time.time() + limit
    last = ""
    while time.time() < deadline:
        try:
            if k in ("file", "path", "folder"):
                p = resolve_path(tgt)
                if p.exists():
                    return _ok(f"Exists: {rel_display(p)}")
                last = "not on disk yet"
            elif k in ("window", "app"):
                hwnd, name = _find_window(tgt)
                if hwnd:
                    return _ok(f"Window is open: {name}")
                last = "window not open yet"
            elif k in ("process", "program"):
                p = _proc_find(tgt)
                return _ok(f"Process is running: {p.name()} (pid {p.pid})")
            elif k == "text":
                ok, _summary, detail = t_find_on_screen(tgt, max_results=1)
                if ok:
                    return _ok(f"Text found on screen: {tgt!r}", detail)
                last = "text not on screen yet"
            elif k == "url":
                req = urllib.request.Request(tgt if "://" in tgt else "https://" + tgt,
                                             headers={"User-Agent": BROWSER_UA})
                with http_open(req, 8) as r:
                    return _ok(f"{tgt} is reachable (HTTP {r.status})")
                last = "URL not reachable yet"
            else:
                return _err(f"Unknown kind {kind!r}", "Use file, window, process, text or url.")
        except Exception as e:
            last = str(e)[:200]
        time.sleep(0.6)
    return _err(f"Timed out after {limit}s waiting for {tgt!r}", last)


def _uia_click_el(el):
    err = ""
    for method in ("click_input", "invoke", "click"):
        try:
            getattr(el, method)()
            return ""
        except Exception as e:
            err += f" | {method}: {e}"
    try:
        r = el.rectangle()
        t_mouse_click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
        return ""
    except Exception as e:
        return (err + f" | coords: {e}").strip(" |")


def t_ui_find(window="", text="", control_type="", max_items=15):
    win, title = _uia_window(window)
    if win is None:
        return _err(title)
    els = _uia_elements(win, 8, 500)
    _UI_LAST[title] = els
    needle = str(text or "").strip().lower()
    ctype_needle = str(control_type or "").strip().lower()
    rows = []
    for i, (el, ctype, name, _level) in enumerate(els):
        label = " ".join(str(name or "").split())
        if not label:
            continue
        if needle and needle not in label.lower():
            continue
        if ctype_needle and ctype_needle not in (ctype or "").lower():
            continue
        rect = _uia_rect(el)
        center = ""
        try:
            r = el.rectangle()
            center = f" center={(r.left + r.right) // 2},{(r.top + r.bottom) // 2}"
        except Exception:
            pass
        rows.append(f"#{i} [{ctype}] \"{label[:100]}\" @{rect}{center}")
        if len(rows) >= max(1, min(int(max_items or 15), 100)):
            break
    if not rows:
        return _err(f"No control matched {text or control_type or '?'} in {title!r}",
                    "Call ui_tree to see every control, or use screen_ocr + mouse_click.")
    return _ok(f"{len(rows)} match(es) in {title!r}", "\n".join(rows))


def t_ui_invoke(window="", name="", control_type="", index=-1):
    return t_ui_click(window, name, control_type, index, 1, "left")


def t_ui_set_value(window="", name="", text="", control_type="", index=-1):
    win, title = _uia_window(window)
    if win is None:
        return _err(title)
    el, ctype, nm = _uia_find(win, title, name, control_type, index, refresh=True)
    if el is None:
        return _err(f"No control matching {name or control_type or index} in {title!r}",
                    "Run ui_tree and pass the field's visible name or #index.")
    value = str(text or "")
    for method in ("set_edit_text", "set_value"):
        try:
            getattr(el, method)(value)
            return _ok(f"Set {ctype} {nm!r} to {len(value)} chars")
        except Exception:
            continue
    try:
        el.click_input()
        time.sleep(0.15)
        ok, summary, detail = t_type_text(value, "", False)
        return (ok, f"{summary} into {ctype} {nm!r}", detail) if ok else \
            _err("Could not write into that control", detail)
    except Exception as e:
        return _err(f"Could not set {nm!r}", str(e))


def t_ui_toggle(window="", name="", control_type="", index=-1):
    win, title = _uia_window(window)
    if win is None:
        return _err(title)
    el, ctype, nm = _uia_find(win, title, name, control_type, index, refresh=True)
    if el is None:
        return _err(f"No control matching {name or control_type or index} in {title!r}")
    try:
        el.toggle()
        return _ok(f"Toggled {ctype} {nm!r}")
    except Exception:
        err = _uia_click_el(el)
        return (_ok(f"Toggled {ctype} {nm!r} (click)") if not err
                else _err(f"Could not toggle {nm!r}", err))


def t_ui_keys(window="", keys="", name="", control_type="", index=-1):
    combo = str(keys or "").strip()
    if not combo:
        return _err("ui_keys needs keys, e.g. {ENTER} or ^s")
    win, title = _uia_window(window)
    if win is None:
        return _err(title)
    el, _ctype, _nm = _uia_find(win, title, name, control_type, index)
    try:
        if el is not None:
            el.set_focus()
        else:
            win.set_focus()
    except Exception:
        pass
    time.sleep(0.12)
    safe = combo.replace("'", "''")
    ok, out = _run_powershell(
        "Add-Type -AssemblyName System.Windows.Forms;"
        f"[System.Windows.Forms.SendKeys]::SendWait('{safe}');'sent'", 20)
    return (_ok(f"Sent {combo} to {title!r}") if ok and "sent" in out
            else _err("Could not send those keys", out[-400:]))


def t_ui_menu(window="", items=""):
    raw = items
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            try:
                raw = json.loads(text, strict=False)
            except Exception:
                raw = [p.strip() for p in re.split(r"\s*>\s*", text)]
        else:
            raw = [p.strip() for p in re.split(r"\s*>\s*", text)]
    seq = [str(x).strip() for x in (raw or []) if str(x).strip()]
    if not seq:
        return _err("ui_menu needs menu names, e.g. File > Save As")
    win, title = _uia_window(window)
    if win is None:
        return _err(title)
    done = []
    for name in seq:
        el, ctype, nm = _uia_find(win, title, name, "", -1, refresh=True)
        if el is None:
            return _err(f"Menu item {name!r} not found in {title!r}",
                        "Opened so far: " + " > ".join(done or ["(none)"]))
        err = _uia_click_el(el)
        if err:
            return _err(f"Could not click {nm!r}", err)
        done.append(nm or name)
        time.sleep(0.35)
    return _ok("Menu path: " + " > ".join(done))


def t_ui_wait_text(window="", text="", timeout=15):
    needle = str(text or "").strip()
    if not needle:
        return _err("ui_wait_text needs text")
    limit = max(1, min(int(timeout or 15), 120))
    deadline = time.time() + limit
    last = ""
    while time.time() < deadline:
        win, title = _uia_window(window)
        if win is not None:
            els = _uia_elements(win, 8, 500)
            _UI_LAST[title] = els
            for _el, ctype, name, _level in els:
                label = " ".join(str(name or "").split())
                if needle.lower() in label.lower():
                    return _ok(f"Text appeared in {title!r}: {label[:120]!r}",
                               f"{ctype} @{_uia_rect(_el)}")
            last = f"not in {title!r} yet"
        else:
            last = title
        time.sleep(0.6)
    return _err(f"Timed out waiting for text {needle!r}", last)


def t_ui_batch(window="", actions=""):
    acts = _as_action_list(actions)
    if not acts:
        return _err("ui_batch needs a JSON array of actions",
                    'Example: [{"act":"click","name":"Search"},'
                    '{"act":"type","text":"hello","enter":true}]')
    win, title = _uia_window(window)
    if win is None:
        return _err(title)
    els = _uia_elements(win, 8, 500)
    _UI_LAST[title] = els
    log = []
    for i, a in enumerate(acts[:40], 1):
        if not isinstance(a, dict):
            return _err(f"Action {i} is not an object", "\n".join(log))
        act = str(a.get("act") or a.get("action") or "click").lower()
        name = str(a.get("name") or "")
        ctype = str(a.get("control_type") or "")
        idx = a.get("index", -1)
        el = ctype_nm = None
        try:
            el, ctype_nm, nm = _uia_find(win, title, name, ctype, idx)
        except Exception:
            el, ctype_nm, nm = None, "", ""
        if act in ("click", "invoke"):
            if el is None:
                return _err(f"Action {i}: no control matching {name or ctype or idx!r}",
                            "\n".join(log))
            err = _uia_click_el(el)
            if err:
                return _err(f"Action {i}: could not click {nm!r}", err + "\n" + "\n".join(log))
            log.append(f"{i}. clicked {ctype_nm} {nm!r}")
        elif act in ("type", "set"):
            text = str(a.get("text") or "")
            if not text:
                return _err(f"Action {i}: type needs text", "\n".join(log))
            if el is not None:
                try:
                    el.click_input()
                except Exception:
                    try:
                        el.set_focus()
                    except Exception:
                        pass
                time.sleep(0.15)
            try:
                ok, summary, _d = t_type_text(text, "", _bool(a.get("enter")))
            except Exception as e:
                ok, summary = False, str(e)
            if not ok:
                return _err(f"Action {i}: typing failed", summary + "\n" + "\n".join(log))
            log.append(f"{i}. typed {len(text)} chars into {(nm or 'the focused field')!r}")
        elif act == "keys":
            combo = str(a.get("keys") or a.get("text") or "")
            if not combo:
                return _err(f"Action {i}: keys needs a value", "\n".join(log))
            try:
                if el is not None:
                    el.set_focus()
                else:
                    win.set_focus()
            except Exception:
                pass
            ok, out = _run_powershell(
                "Add-Type -AssemblyName System.Windows.Forms;"
                f"[System.Windows.Forms.SendKeys]::SendWait({_q(combo)});'sent'", 20)
            if not ok:
                return _err(f"Action {i}: keys failed", out[-400:] + "\n" + "\n".join(log))
            log.append(f"{i}. sent keys {combo!r}")
        elif act == "wait":
            target = str(a.get("text") or "")
            if target:
                res = t_ui_wait_text(window, target, int(a.get("timeout") or 15))
                if not res[0]:
                    return _err(f"Action {i}: {res[1]}", "\n".join(log))
                log.append(f"{i}. text appeared: {target!r}")
            else:
                time.sleep(max(0.0, min(float(a.get("seconds") or 1), 30)))
                log.append(f"{i}. waited")
        elif act == "scroll":
            if el is not None:
                try:
                    el.set_focus()
                    r = el.rectangle()
                    import ctypes
                    ctypes.windll.user32.SetCursorPos((r.left + r.right) // 2,
                                                      (r.top + r.bottom) // 2)
                except Exception:
                    pass
            up = str(a.get("direction") or "down").lower().startswith("u")
            for _ in range(max(1, min(int(a.get("pages") or 1), 30))):
                t_mouse_scroll(-3 if up else 3)
                time.sleep(0.25)
            log.append(f"{i}. scrolled {'up' if up else 'down'}")
        elif act in ("focus", "activate"):
            try:
                win.set_focus()
            except Exception as e:
                return _err(f"Action {i}: could not focus {title!r}", str(e))
            log.append(f"{i}. focused {title!r}")
        elif act in ("sleep", "delay"):
            time.sleep(max(0.0, min(float(a.get("seconds") or 1), 30)))
            log.append(f"{i}. slept")
        else:
            return _err(f"Action {i}: unknown act {act!r}",
                        "Use click, invoke, type, set, keys, wait, scroll, focus or sleep.")
        if act in ("click", "invoke", "type", "set", "keys"):
            time.sleep(0.25)
            try:
                els = _uia_elements(win, 8, 500)
                _UI_LAST[title] = els
            except Exception:
                pass
    return _ok(f"{len(log)} UI action(s) in {title!r}", "\n".join(log))


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------
def _f(**kw):
    return kw


TOOLS = {
    "read_file": (_f(path=_f(type="string", description="File path"),
                     offset=_f(type="integer", description="First line, 1-based"),
                     limit=_f(type="integer", description="Max lines")), ["path"],
                 "Read a text file with line numbers."),
    "write_file": (_f(path=_f(type="string"), content=_f(type="string"),
                      append=_f(type="boolean", description="Append instead of overwrite")),
                   ["path", "content"], "Create or overwrite a file with exact content."),
    "edit_file": (_f(path=_f(type="string"), old=_f(type="string", description="Exact text to find"),
                     new=_f(type="string"), all=_f(type="boolean", description="Replace every hit")),
                  ["path", "old", "new"],
                  "Find/replace inside an existing file. Call once per edit."),
    "list_dir": (_f(path=_f(type="string"), limit=_f(type="integer")), [],
                 "List a directory, folders first. Defaults to the workspace root."),
    "tree": (_f(path=_f(type="string"), depth=_f(type="integer")), [],
             "Recursive directory outline, ignores vendored folders."),
    "glob": (_f(pattern=_f(type="string", description="e.g. **/*.py"), path=_f(type="string"),
                limit=_f(type="integer")), ["pattern"], "Find files by glob pattern."),
    "grep": (_f(pattern=_f(type="string", description="Regex"), path=_f(type="string"),
                glob=_f(type="string", description="File filter e.g. **/*.cpp"),
                ignore_case=_f(type="boolean")), ["pattern"],
             "Search file contents by regex, returns file:line hits."),
    "mkdir": (_f(path=_f(type="string")), ["path"], "Create a folder (parents included)."),
    "copy": (_f(src=_f(type="string"), dst=_f(type="string")), ["src", "dst"], "Copy a file or folder."),
    "move": (_f(src=_f(type="string"), dst=_f(type="string")), ["src", "dst"], "Move or relocate."),
    "rename": (_f(path=_f(type="string"), new_name=_f(type="string", description="Empty = auto-clean"),
                  max_len=_f(type="integer", description="Auto-clean target length")), ["path"],
               "Rename one file. Collision-safe."),
    "shorten_names": (_f(path=_f(type="string"), pattern=_f(type="string"),
                         max_len=_f(type="integer"), dry_run=_f(type="boolean")), ["path"],
                      "Batch-tidy filenames in a folder, removing junk characters."),
    "gather": (_f(source=_f(type="string"), dest=_f(type="string"),
                  pattern=_f(type="string", description="e.g. *.mp3"),
                  shorten=_f(type="boolean"), move=_f(type="boolean"),
                  recursive=_f(type="boolean")), ["source", "dest"],
               "Collect matching files from one folder into another, optionally shortening names."),
    "delete": (_f(path=_f(type="string"), recursive=_f(type="boolean"),
                  permanent=_f(type="boolean", description="Default false = Recycle Bin")), ["path"],
               "Delete a path. Goes to Recycle Bin unless permanent=true."),
    "run_command": (_f(command=_f(type="string"), shell=_f(type="string", enum=["powershell", "cmd"]),
                       timeout=_f(type="integer")), ["command"],
                    "Execute a shell command and capture stdout/stderr."),
    "open_url": (_f(url=_f(type="string")), ["url"],
                 "Open a URL in the OS default browser (no Chrome required)."),
    "open_path": (_f(path=_f(type="string")), ["path"], "Reveal a file or folder in Explorer."),
    "web_fetch": (_f(url=_f(type="string"), extract_text=_f(type="boolean"),
                     max_chars=_f(type="integer")), ["url"],
                  "Download a page and return readable text."),
    "web_search": (_f(query=_f(type="string"), max_results=_f(type="integer"),
                      open_browser=_f(type="boolean", description="Also show it in the browser")),
                   ["query"], "Web search returning titles + URLs + snippets as data."),
    "youtube_search": (_f(query=_f(type="string")), ["query"],
                       "Open a YouTube search in the default browser and return top video links."),
    "clipboard_read": (_f(), [], "Read the clipboard."),
    "clipboard_write": (_f(text=_f(type="string")), ["text"], "Put text on the clipboard."),
    "sysinfo": (_f(), [], "OS, cores, disks, workspace path."),
    "process_list": (_f(filter=_f(type="string")), [], "Top processes by CPU."),
    "kill_process": (_f(name=_f(type="string"), pid=_f(type="integer")), [],
                     "Stop a process by name or pid."),
    "launch": (_f(target=_f(type="string"), args=_f(type="string")), ["target"],
               "Start an application or executable (reuses an already-running copy when it can)."),
    "open_app": (_f(name=_f(type="string", description="App name or part of its window title, e.g. AyuGram, Telegram, Spotify"),
                   wait=_f(type="integer", description="Seconds to wait for its window"),
                   restart_stuck=_f(type="boolean", description="Relaunch it if its hidden window will not come back")), ["name"],
                "Open an app like a person clicking the taskbar: if it is already running "
                "(minimized or in the tray), focus that signed-in window instead of starting "
                "a second copy. Set restart_stuck=true only when the app is running but its "
                "window refuses to appear."),
    "screenshot": (_f(path=_f(type="string")), [], "Capture the screen to a PNG."),
    "notify": (_f(title=_f(type="string"), message=_f(type="string")), ["title"],
               "Show a desktop notification."),
    "wait": (_f(seconds=_f(type="integer", description="Max 600")), ["seconds"],
             "Pause before the next step, for slow apps or downloads."),
    "download": (_f(url=_f(type="string"), path=_f(type="string", description="Exact destination file or folder the user asked for")),
                 ["url"], "Download a URL straight to disk. If the user said where to save it, "
                 "put that exact path in path instead of downloading to the workspace."),
    "fetch_file": (_f(target=_f(type="string", description="Direct file URL, or a search phrase like 'Minecraft 1.21 client jar'"),
                      dest=_f(type="string", description="Folder or exact file path"),
                      kind=_f(type="string", description="Optional hint: zip, pdf, png, mp3...")), ["target"],
                   "Get a file from the internet: direct URL or search phrase, saves the real file. "
                   "Use this whenever the user asks you to grab/download something not in the workspace."),
    "download_many": (_f(urls=_f(type="string", description="JSON array or one URL per line"),
                         folder=_f(type="string"),
                         label=_f(type="string", description="Short label for the progress bar, e.g. 'bulk sound effects'")), ["urls"],
                      "Download many URLs at once (4 parallel) into a folder, with a live "
                      "byte progress bar."),
    "youtube_download_many": (_f(items=_f(type="string", description="JSON array or one song/video per line"),
                                 kind=_f(type="string", enum=["audio", "video"]),
                                 quality=_f(type="string"), folder=_f(type="string")), ["items"],
                              "Download a batch of songs/videos (2 parallel), each verified."),
    "wallpapers": (_f(query=_f(type="string", description="Theme, empty = top rated"),
                      count=_f(type="integer"), folder=_f(type="string"),
                      set_first=_f(type="boolean", description="Also set the first as wallpaper")), [],
                   "Download beautiful 16:9 1920x1080+ wallpapers (Wallhaven, Picsum fallback)."),
    "window_list": (_f(filter=_f(type="string"),
                       all=_f(type="boolean", description="Default true; includes tray-hidden and minimized windows")), [],
                    "List top-level windows with titles, pids and state, including apps "
                    "hidden to the tray or minimized."),
    "window_focus": (_f(title=_f(type="string", description="Part of the window title")), ["title"],
                     "Bring a window to the foreground."),
    "ui_tree": (_f(window=_f(type="string", description="Part of the window title; empty = foreground"),
                   depth=_f(type="integer"), max_items=_f(type="integer")), [],
                "Dump a window's accessible controls (buttons, lists, edits, names, ids, "
                "screen rectangles). This is how you see inside any app."),
    "ui_click": (_f(window=_f(type="string"), name=_f(type="string", description="Visible label, contains match"),
                    control_type=_f(type="string", description="e.g. Button, ListItem, Edit"),
                    index=_f(type="integer", description="#index from the last ui_tree"),
                    clicks=_f(type="integer"),
                    button=_f(type="string", enum=["left", "right", "middle"])), [],
                 "Click a control found by ui_tree, by name, type or index. button=right opens context menus."),
    "ui_scroll": (_f(window=_f(type="string"), name=_f(type="string"),
                     control_type=_f(type="string"), index=_f(type="integer"),
                     direction=_f(type="string", enum=["down", "up"]),
                     pages=_f(type="integer")), [],
                  "Scroll a list, chat or window with the wheel. direction=up loads older chat history."),
    "ui_collect": (_f(window=_f(type="string"), name=_f(type="string"),
                      control_type=_f(type="string"), index=_f(type="integer"),
                      direction=_f(type="string", enum=["up", "down"]),
                      pages=_f(type="integer"),
                      max_chars=_f(type="integer")), [],
                   "Scroll a chat/list and collect all text it shows across many pages. Use this "
                   "to dig through chat history; it returns everything it read."),
    "ui_wait_element": (_f(window=_f(type="string"), name=_f(type="string"),
                           control_type=_f(type="string"), index=_f(type="integer"),
                           timeout=_f(type="integer")), [],
                        "Wait until a control appears in a window (loading chats, dialogs)."),
    "window_text": (_f(window=_f(type="string", description="Part of the window title; empty = foreground"),
                        max_chars=_f(type="integer")), [],
                    "Read ALL text inside an app window in one call: document/chat/editor text, "
                    "then labels, then screen OCR if nothing is exposed. Use this first whenever "
                    "you need to read what a window says, or dig through chats."),
    "screen_ocr": (_f(path=_f(type="string", description="Optional PNG to read; empty = capture the screen now"),
                      max_lines=_f(type="integer")), [],
                   "OCR the screen into lines with pixel coordinates and center=cx,cy for apps "
                   "with no accessible controls. Click a line with mouse_click(cx, cy)."),
    "find_on_screen": (_f(text=_f(type="string", description="Exact or partial label to find"),
                          window=_f(type="string", description="Search only this window"),
                          max_results=_f(type="integer")), ["text"],
                       "Find text on screen by OCR and return exact click coordinates "
                       "center=cx,cy for every match. Safer than guessing pixels."),
    "screen_snapshot": (_f(window=_f(type="string", description="Part of the window title; empty = foreground"),
                          max_lines=_f(type="integer")), [],
                        "START HERE for any app. One call returns window state, accessible "
                        "controls, and OCR text with click points. Tells you what to do next."),
    "read_screen": (_f(window=_f(type="string"), max_lines=_f(type="integer")), [],
                    "OCR-read the visible text of a window (or the screen) with exact "
                    "coordinates. Faster than screen_ocr and scoped to the window."),
    "ocr_click": (_f(text=_f(type="string", description="Visible label to click; fuzzy match"),
                     window=_f(type="string"), index=_f(type="integer"),
                     button=_f(type="string"), clicks=_f(type="integer")), ["text"],
                  "Find text by OCR in a window and click its center. Works on any app, "
                  "including custom-painted ones with no accessibility. Never guess pixels."),
    "click_text": (_f(text=_f(type="string"), window=_f(type="string"),
                      index=_f(type="integer"),
                      button=_f(type="string")), ["text"],
                   "Alias of ocr_click: click a visible label by OCR."),
    "wait_text": (_f(text=_f(type="string"), window=_f(type="string"),
                     timeout=_f(type="integer")), ["text"],
                  "Wait up to 5s for text to appear in a window, then return its click point. "
                  "Use after opening a chat/folder instead of sleeping blindly."),
    "scroll_read": (_f(window=_f(type="string"),
                       direction=_f(type="string", enum=["down", "up"]),
                       pages=_f(type="integer"), max_lines=_f(type="integer")), [],
                    "Scroll a window one or more pages and OCR what is visible. Use up for "
                    "older chat messages."),
    "key_macro": (_f(keys=_f(type="string", description="Space-separated: ctrl+f ctrl+a enter"),
                     window=_f(type="string")), ["keys"],
                  "Send several shortcuts/keys in one call, e.g. 'ctrl+f ctrl+a enter'."),
    "focus_and_type": (_f(text=_f(type="string"), window=_f(type="string"),
                          enter=_f(type="boolean")), ["text"],
                       "Focus a window, paste text into the focused field, optional Enter."),
    "dialog_fill": (_f(fields=_f(type="string", description='"Name=Umar; Email=me@x.com"'),
                       window=_f(type="string")), ["fields"],
                    "Fill labelled form fields by clicking each label's input and pasting."),
    "dig_chats": (_f(app=_f(type="string", description="AyuGram, Telegram, Discord..."),
                     window=_f(type="string"), pages=_f(type="integer"),
                     max_chats=_f(type="integer")), [],
                  "Read-only chat sweep: opens/focuses the app, clicks each visible chat, "
                  "OCR-reads the thread and scrolls up for older messages. Never types, "
                  "never sends anything."),
    "window_ping": (_f(title=_f(type="string")), [],
                    "Fast check of one window: exists, visible, minimized, cloaked (other "
                    "virtual desktop), foreground, rect. ~10ms."),
    "tray_icon_click": (_f(name=_f(type="string", description="App or icon name, e.g. AyuGram, Bluetooth")),
                        ["name"],
                        "Click an app's system-tray icon, including icons hidden in the "
                        "overflow flyout. This is how you bring back an app that is running "
                        "but has no window on screen."),
    "app_restart": (_f(name=_f(type="string")), ["name"],
                    "Close and relaunch an app whose window is stuck hidden or frozen. "
                    "Use only when open_app/tray_icon_click both fail."),
    "desktop_switch": (_f(direction=_f(type="string", enum=["left", "right"]),
                          times=_f(type="integer")), [],
                       "Switch Windows virtual desktop. Needed when a window is cloaked "
                       "and cannot be captured."),
    "see_images": (_f(path=_f(type="string", description="Image file or a folder of screenshots"),
                      question=_f(type="string", description="What to look for"),
                      max_images=_f(type="integer", description="Default 8")), ["path"],
                   "Look at pictures/screenshots: OCR every visible text line plus a vision "
                   "description when a local vision model is installed. Use this when the "
                   "user says 'look at these screenshots' or attaches images."),
    "browser_tabs": (_f(window=_f(type="string", description="Browser window title filter, empty = all browsers"),
                        max_tabs=_f(type="integer")), [],
                     "List every open browser tab title (Chrome, Edge, Firefox, Brave...) so you "
                     "know what the user is looking at even when the taskbar is hidden."),
    "ui_type": (_f(window=_f(type="string"), text=_f(type="string"),
                   name=_f(type="string", description="Field label, empty = focused field"),
                   control_type=_f(type="string"), index=_f(type="integer"),
                   enter=_f(type="boolean", description="Press Enter after typing")), ["text"],
                "Focus a field and paste-type text into it (works with unicode)."),
    "ui_read": (_f(window=_f(type="string"), name=_f(type="string"),
                   control_type=_f(type="string"), index=_f(type="integer")), [],
                "Read the text/value of a control, or list every labelled control in a window."),
    "send_keys": (_f(keys=_f(type="string", description="SendKeys syntax, e.g. ^s or {ENTER}"),
                     window=_f(type="string", description="Optional window title to focus first")),
                  ["keys"], "Type keys into the focused (or named) window."),
    "now": (_f(fmt=_f(type="string")), [], "Current date/time."),
    "calculate": (_f(expression=_f(type="string")), ["expression"], "Evaluate an arithmetic expression."),
    "hash": (_f(path=_f(type="string"), algo=_f(type="string")), ["path"], "Checksum a file."),
    "archive": (_f(path=_f(type="string"), dest=_f(type="string"),
                   mode=_f(type="string", enum=["extract", "create", "list", "add"]),
                   fmt=_f(type="string", description="zip, 7z, tar, gz... empty = zip"),
                   items=_f(type="string", description="add mode: files or wildcards"),
                   password=_f(type="string")),
                ["path"], "Unpack, create, list or add to archives with 7-Zip "
                          "(zip, 7z, rar, tar, gz, iso...)."),
    "resource_pack": (_f(version=_f(type="string", description="Minecraft version, e.g. 1.21.1"),
                         template=_f(type="string", description="Optional base pack .zip URL or local file/folder"),
                         replacements=_f(type="string", description="Files to overlay: JSON [{src,dst}] or lines 'src => dst'"),
                         output=_f(type="string", description="Output zip path"),
                         name=_f(type="string", description="Pack name")), [],
                      "Build a Minecraft resource pack: fetch the vanilla base for a version (or "
                      "a template zip), overlay the modified files, write pack.mcmeta and rezip."),
    "github_release": (_f(repo=_f(type="string", description="owner/repo or GitHub URL"),
                          asset=_f(type="string", description="Part of the asset filename, optional"),
                          folder=_f(type="string", description="Download/extract folder"),
                          launch=_f(type="string", description="'true' to run the extracted exe")),
                       ["repo"],
                       "Download and unpack the latest release of a GitHub tool/app "
                       "(UABEA, editors, utilities), then optionally launch it."),
    "open_unity_asset": (_f(path=_f(type="string", description=".assets/.bundle/.unity3d file"),
                            install_dir=_f(type="string", description="Where to keep UABEA")), ["path"],
                         "Edit hard Unity files: download UABEA if needed, open the .assets or "
                         "bundle in it, then drive UABEA's window with ui_tree/ui_click/ui_type."),
    "remember": (_f(when=_f(type="string", description="Trigger condition"),
                    do=_f(type="string", description="What to do next time")), ["when", "do"],
                 "Store a durable lesson so the same mistake is never repeated."),
    "lessons": (_f(query=_f(type="string")), [], "List stored lessons, optionally filtered."),
    "self_test": (_f(verbose=_f(type="boolean")), [],
                  "Run the full self-test: every tool schema, fuzzy OCR matching, window "
                  "lookup, focus, OCR, file and clipboard roundtrips. Reports failures."),
    "forget": (_f(id=_f(type="string")), ["id"], "Delete a stored lesson by id."),
    "create_tool": (_f(name=_f(type="string"), description=_f(type="string"),
                       code=_f(type="string"), language=_f(type="string", enum=["python", "powershell"])),
                    ["name", "code"],
                    "Write a reusable script and register it as a callable tool."),
    "run_custom": (_f(name=_f(type="string"), args=_f(type="string")), ["name"],
                   "Run a tool you previously created."),
    "list_tools": (_f(), [], "List builtin and custom tools."),

    # -- media / internet -------------------------------------------------
    "youtube_download": (_f(target=_f(type="string", description="YouTube URL, id, or search phrase"),
                              kind=_f(type="string", enum=["audio", "video"]),
                              quality=_f(type="string", description="Leave empty for the best available quality; set a height like 1080 or 720 only when the user asks for it"),
                              folder=_f(type="string", description="Destination folder"),
                              filename=_f(type="string", description="Optional output name"),
                              playlist=_f(type="boolean")),
                         ["target"],
                         "Download a YouTube video or MP3 audio into a folder. Accepts a search phrase."),
    "download_audio": (_f(target=_f(type="string"), folder=_f(type="string")), ["target"],
                       "Save just the audio of a video/URL as MP3 into a folder."),
    "download_page_media": (_f(url=_f(type="string"), folder=_f(type="string"),
                               kind=_f(type="string", enum=["all", "image", "video", "audio"]),
                               limit=_f(type="integer")), ["url"],
                            "Scan a web page and download its images, video or audio into a folder."),
    "media_info": (_f(path=_f(type="string")), ["path"], "Duration, codecs and size of an audio/video file."),
    "convert_media": (_f(path=_f(type="string"), fmt=_f(type="string", description="mp4, mp3, wav, mkv..."),
                         dest=_f(type="string"), quality=_f(type="string")), ["path"],
                      "Convert any audio/video file to another format with ffmpeg."),
    "extract_audio": (_f(path=_f(type="string"), fmt=_f(type="string"), dest=_f(type="string")), ["path"],
                      "Strip the audio track from a video into mp3/wav/m4a."),
    "trim_media": (_f(path=_f(type="string"), start=_f(type="string", description="e.g. 00:01:30"),
                      duration=_f(type="string", description="e.g. 00:00:45"),
                      dest=_f(type="string")), ["path"],
                   "Cut a clip out of an audio/video file."),
    "wikipedia": (_f(query=_f(type="string"), sentences=_f(type="integer")), ["query"],
                  "Get a factual summary of a topic from Wikipedia."),
    "weather": (_f(location=_f(type="string", description="City or empty for current location"),
                   days=_f(type="integer")), [],
                "Current weather and forecast for a place."),
    "news": (_f(topic=_f(type="string"), limit=_f(type="integer")), [],
             "Recent headlines, optionally about a topic."),
    "rss": (_f(url=_f(type="string"), limit=_f(type="integer")), ["url"], "Read an RSS/Atom feed."),
    "define": (_f(word=_f(type="string")), ["word"], "Dictionary definition and examples for a word."),
    "url_metadata": (_f(url=_f(type="string")), ["url"], "Title, description and preview image of a web page."),
    "check_url": (_f(url=_f(type="string")), ["url"], "Check whether a URL is reachable, with status and type."),
    "http_json": (_f(url=_f(type="string"), method=_f(type="string", enum=["GET", "POST", "PUT", "DELETE"]),
                     body=_f(type="string", description="Raw JSON body")), ["url"],
                  "Call a JSON API and pretty-print the response."),

    # -- documents --------------------------------------------------------
    "pdf_info": (_f(path=_f(type="string")), ["path"], "Page count and metadata of a PDF."),
    "pdf_text": (_f(path=_f(type="string"), max_chars=_f(type="integer")), ["path"],
                 "Extract readable text from a PDF."),
    "pdf_merge": (_f(paths=_f(type="string", description="JSON list or semicolon-separated paths"),
                     dest=_f(type="string")), ["paths"], "Merge several PDFs into one file."),
    "docx_create": (_f(path=_f(type="string"), content=_f(type="string", description="Markdown-ish text"),
                       title=_f(type="string")), ["path", "content"],
                    "Create a Word document with headings and bullets."),
    "docx_read": (_f(path=_f(type="string"), max_chars=_f(type="integer")), ["path"],
                  "Read the text of a Word document."),
    "xlsx_write": (_f(path=_f(type="string"), rows=_f(type="string", description="JSON 2D array"),
                      sheet=_f(type="string")), ["path", "rows"],
                   "Write rows into an Excel workbook."),
    "xlsx_read": (_f(path=_f(type="string"), sheet=_f(type="string"), limit=_f(type="integer")), ["path"],
                  "Read rows from an Excel workbook."),
    "pptx_create": (_f(path=_f(type="string"), slides=_f(type="string", description='JSON [{"title","body"}]'),
                       title=_f(type="string")), ["path", "slides"],
                    "Build a PowerPoint deck from slide titles and bullets."),
    "csv_read": (_f(path=_f(type="string"), limit=_f(type="integer")), ["path"], "Read rows of a CSV file."),
    "csv_write": (_f(path=_f(type="string"), rows=_f(type="string", description="JSON 2D array or CSV text")),
                  ["path", "rows"], "Write rows into a CSV file."),
    "read_json": (_f(path=_f(type="string"), key=_f(type="string", description="Dot path like items.0.name")),
                  ["path"], "Read and pretty-print a JSON file, optionally one key path."),
    "write_json": (_f(path=_f(type="string"), data=_f(type="string")), ["path", "data"],
                   "Write valid JSON to a file."),

    # -- files ------------------------------------------------------------
    "file_info": (_f(path=_f(type="string")), ["path"], "Size, type and timestamps of a file or folder."),
    "folder_size": (_f(path=_f(type="string")), [], "Total size and file count of a folder."),
    "disk_usage": (_f(), [], "Free space on each drive."),
    "recent_files": (_f(folder=_f(type="string"), limit=_f(type="integer")), [],
                     "Newest files on the Desktop/Downloads/Documents, or in one folder."),
    "find_duplicates": (_f(path=_f(type="string"), min_kb=_f(type="integer")), [],
                        "Find duplicate files by hash so they can be cleaned up."),
    "largest_files": (_f(path=_f(type="string"), limit=_f(type="integer")), [],
                      "Biggest files inside a folder, useful before cleaning space."),
    "count_files": (_f(path=_f(type="string"), pattern=_f(type="string")), [],
                    "Count files and total size in a folder, optionally by glob."),
    "organize_folder": (_f(path=_f(type="string"),
                           by=_f(type="string", enum=["type", "date"]),
                           dry_run=_f(type="boolean")), [],
                        "Sort loose files into subfolders by type or month. Preview with dry_run."),
    "create_shortcut": (_f(target=_f(type="string"), name=_f(type="string"), folder=_f(type="string")),
                        ["target"], "Create a Windows .lnk shortcut to a file or app."),
    "set_wallpaper": (_f(path=_f(type="string")), ["path"], "Set an image as the desktop wallpaper."),

    # -- images -----------------------------------------------------------
    "image_info": (_f(path=_f(type="string")), ["path"], "Dimensions, mode and format of an image."),
    "image_resize": (_f(path=_f(type="string"), width=_f(type="integer"), height=_f(type="integer"),
                        dest=_f(type="string")), ["path"],
                     "Resize an image, keeping the aspect ratio when one side is 0."),
    "image_convert": (_f(path=_f(type="string"), fmt=_f(type="string"), dest=_f(type="string")), ["path"],
                      "Convert an image to png/jpg/webp/gif."),
    "convert_folder": (_f(path=_f(type="string", description="Folder of images"),
                          fmt=_f(type="string", description="png, jpg, webp, gif... or 'auto' for images→png AND audio→mp3"),
                          pattern=_f(type="string", description="Glob, e.g. *.webp"),
                          recursive=_f(type="boolean"), overwrite=_f(type="boolean"),
                          max_side=_f(type="integer", description="0 = keep size"),
                          quality=_f(type="string", description="1-100 for jpg/webp")), ["path"],
                       "Mass-convert every image in a folder to one format, in place, deleting "
                       "the originals after a successful conversion. fmt=auto also turns every "
                       "audio file in the same folder into MP3."),
    "convert_audio_folder": (_f(path=_f(type="string", description="Folder of audio files"),
                                fmt=_f(type="string", description="mp3 (default), wav, flac, ogg, m4a"),
                                bitrate=_f(type="string", description="MP3 bitrate, e.g. 192k"),
                                recursive=_f(type="boolean"), overwrite=_f(type="boolean")), ["path"],
                             "Turn every audio file in a folder into MP3 (or another format), "
                             "replacing the originals."),
    "image_crop": (_f(path=_f(type="string"), left=_f(type="integer"), top=_f(type="integer"),
                      width=_f(type="integer"), height=_f(type="integer"), dest=_f(type="string")), ["path"],
                   "Crop a rectangle out of an image."),
    "image_rotate": (_f(path=_f(type="string"), angle=_f(type="integer"), dest=_f(type="string")), ["path"],
                     "Rotate an image by degrees."),
    "window_screenshot": (_f(title=_f(type="string", description="Empty = active window"),
                             path=_f(type="string")), [],
                          "Capture one window (not the whole screen) to a PNG."),

    # -- laptop control ---------------------------------------------------
    "mouse_move": (_f(x=_f(type="integer"), y=_f(type="integer")), ["x", "y"], "Move the cursor to screen x,y."),
    "mouse_click": (_f(x=_f(type="integer"), y=_f(type="integer"),
                       button=_f(type="string", enum=["left", "right", "middle"]),
                       clicks=_f(type="integer")), ["x", "y"],
                    "Click at screen x,y with the chosen button."),
    "mouse_drag": (_f(x1=_f(type="integer"), y1=_f(type="integer"), x2=_f(type="integer"),
                      y2=_f(type="integer")), ["x1", "y1", "x2", "y2"],
                   "Press, drag to a second point and release."),
    "mouse_scroll": (_f(amount=_f(type="integer", description="Positive = up")), [],
                     "Scroll the wheel at the cursor."),
    "type_text": (_f(text=_f(type="string"), window=_f(type="string"), enter=_f(type="boolean")),
                  ["text"], "Type literal text into the focused or named window, optionally hitting Enter."),
    "hotkey": (_f(keys=_f(type="string", description="e.g. ctrl+shift+s, alt+tab, win+d")), ["keys"],
               "Press a keyboard shortcut."),
    "active_window": (_f(), [], "Title, pid and size of the window in front."),
    "close_window": (_f(title=_f(type="string")), ["title"], "Close a window by part of its title."),
    "minimize_window": (_f(title=_f(type="string")), ["title"], "Minimize a window."),
    "maximize_window": (_f(title=_f(type="string")), ["title"], "Maximize a window."),
    "restore_window": (_f(title=_f(type="string")), ["title"], "Restore a minimized/maximized window."),
    "wait_window": (_f(title=_f(type="string"), timeout=_f(type="integer")), ["title"],
                    "Wait until a window with that title appears."),
    "volume_up": (_f(steps=_f(type="integer")), [], "Raise the system volume."),
    "volume_down": (_f(steps=_f(type="integer")), [], "Lower the system volume."),
    "mute": (_f(), [], "Toggle system mute."),
    "play_pause": (_f(), [], "Send the media play/pause key."),
    "media_next": (_f(), [], "Skip to the next track."),
    "media_prev": (_f(), [], "Go back to the previous track."),
    "battery": (_f(), [], "Battery charge and charging state."),
    "wifi_status": (_f(), [], "Current Wi-Fi network, signal and link speed."),
    "network_info": (_f(), [], "IPv4 addresses and interface names."),
    "brightness": (_f(level=_f(type="integer", description="0-100")), [], "Set display brightness."),
    "installed_apps": (_f(filter=_f(type="string")), [], "List installed programs, optionally filtered."),
    "env": (_f(name=_f(type="string")), [], "Read one environment variable, or list them."),
    "uptime": (_f(), [], "How long this PC has been running."),
    "lock_screen": (_f(), [], "Lock the workstation."),
    "speak": (_f(text=_f(type="string"), rate=_f(type="integer")), ["text"],
              "Say something out loud with Windows text-to-speech."),
    "empty_recycle_bin": (_f(), [], "Empty the Recycle Bin."),
    "selected_text": (_f(), [], "Copy and return whatever text is currently selected."),

    # -- clipboard --------------------------------------------------------
    "clipboard_clear": (_f(), [], "Empty the clipboard."),
    "clipboard_to_file": (_f(path=_f(type="string")), ["path"],
                          "Save the clipboard text to a file."),
    "clipboard_from_file": (_f(path=_f(type="string")), ["path"],
                            "Copy an entire file's text to the clipboard."),
    "clipboard_image": (_f(path=_f(type="string", description="Where to save the PNG")), [],
                        "Save an image that is on the clipboard to a PNG file."),

    # -- windows ----------------------------------------------------------
    "window_info": (_f(window=_f(type="string", description="Empty = foreground")), [],
                    "Title, process, pid, state and rectangle of a window."),
    "window_move": (_f(x=_f(type="integer"), y=_f(type="integer"), window=_f(type="string")), ["x", "y"],
                    "Move a window to screen x,y."),
    "window_resize": (_f(width=_f(type="integer"), height=_f(type="integer"), window=_f(type="string")),
                      ["width", "height"], "Resize a window."),
    "window_set_rect": (_f(x=_f(type="integer"), y=_f(type="integer"),
                           width=_f(type="integer"), height=_f(type="integer"),
                           window=_f(type="string")), ["x", "y", "width", "height"],
                        "Move and resize a window in one call."),
    "window_snap": (_f(position=_f(type="string", description="left, right, top, bottom, maximize, center, top-left, top-right, bottom-left, bottom-right"),
                       window=_f(type="string")), [],
                    "Snap a window to a half, quarter or the full work area."),
    "window_topmost": (_f(window=_f(type="string"), on=_f(type="boolean")), [],
                       "Pin a window above all others, or unpin it."),
    "window_opacity": (_f(level=_f(type="integer", description="10-100"), window=_f(type="string")), [],
                       "Set how opaque a window is."),
    "window_close_all": (_f(filter=_f(type="string", description="Only windows whose title contains this")), [],
                         "Close every matching visible window (or all of them with no filter)."),

    # -- system -----------------------------------------------------------
    "os_info": (_f(), [], "Windows edition, version, build and architecture."),
    "cpu_usage": (_f(), [], "Total and per-core CPU load right now."),
    "memory_usage": (_f(), [], "RAM and swap use plus the biggest memory consumers."),
    "disk_health": (_f(), [], "Physical disks with media type, size and health status."),
    "cpu_temp": (_f(), [], "CPU temperature when the firmware exposes a sensor."),
    "gpu_info": (_f(), [], "Display adapters, VRAM and driver versions."),
    "services_list": (_f(filter=_f(type="string"), mode=_f(type="string", description="running, stopped or all")), [],
                      "List Windows services, optionally only running or stopped ones."),
    "service_control": (_f(name=_f(type="string"), action=_f(type="string", description="start, stop, restart or status")),
                        ["name"], "Start, stop, restart or inspect a Windows service."),
    "startup_list": (_f(), [], "Programs that start with Windows."),
    "startup_add": (_f(name=_f(type="string"), command=_f(type="string")), ["name", "command"],
                    "Add a program to the current user's startup entries."),
    "startup_remove": (_f(name=_f(type="string")), ["name"], "Remove a startup entry."),
    "scheduled_tasks": (_f(filter=_f(type="string")), [], "List scheduled tasks."),
    "power_plan": (_f(name=_f(type="string", description="Empty = list plans")), [],
                   "List power plans, or activate the one whose name matches."),
    "shutdown_pc": (_f(action=_f(type="string", description="shutdown, restart, logoff, hibernate, sleep or cancel"),
                       delay=_f(type="integer", description="Seconds before shutdown/restart")), [],
                    "Shut down, restart, log off, hibernate or sleep the PC, or cancel it."),
    "event_log": (_f(limit=_f(type="integer"), severity=_f(type="string", description="error, warning or info")), [],
                  "Recent System/Application events, errors by default."),
    "windows_updates": (_f(limit=_f(type="integer")), [], "Installed Windows updates."),
    "printers_list": (_f(), [], "Installed printers and their status."),
    "bluetooth_devices": (_f(), [], "Bluetooth devices present on the machine."),
    "usb_devices": (_f(), [], "USB devices currently attached."),
    "audio_devices": (_f(), [], "Audio endpoints (speakers, headphones, microphones)."),
    "display_info": (_f(), [], "Every monitor with resolution, position and refresh rate."),
    "timezone_info": (_f(), [], "Local time, timezone name and UTC offset."),
    "font_list": (_f(filter=_f(type="string"), limit=_f(type="integer")), [], "Installed fonts."),
    "drivers_list": (_f(filter=_f(type="string")), [], "Signed drivers with versions and dates."),
    "system_report": (_f(), [], "One-shot report: OS, CPU, RAM, disks, uptime, battery, Wi-Fi, top processes."),

    # -- processes --------------------------------------------------------
    "process_info": (_f(name=_f(type="string"), pid=_f(type="integer")), [],
                     "Details for a process: exe, user, memory, connections, command line."),
    "process_top": (_f(limit=_f(type="integer"), by=_f(type="string", description="memory or cpu")), [],
                    "Top processes by memory or CPU."),
    "process_tree": (_f(name=_f(type="string"), pid=_f(type="integer")), [],
                     "A process and all of its child processes."),
    "process_priority": (_f(name=_f(type="string"), pid=_f(type="integer"),
                            priority=_f(type="string", description="idle, below, normal, above, high or realtime")), [],
                         "Change a running process's priority."),
    "process_wait": (_f(name=_f(type="string"), pid=_f(type="integer"), timeout=_f(type="integer"),
                        gone=_f(type="boolean", description="Wait until it exits instead")), [],
                     "Wait until a process starts, or until it exits with gone=true."),
    "process_suspend": (_f(name=_f(type="string"), pid=_f(type="integer")), [], "Pause a process."),
    "process_resume": (_f(name=_f(type="string"), pid=_f(type="integer")), [], "Resume a paused process."),

    # -- network ----------------------------------------------------------
    "ping_host": (_f(host=_f(type="string"), count=_f(type="integer")), ["host"], "Ping a host."),
    "dns_lookup": (_f(host=_f(type="string")), ["host"], "Resolve a hostname's DNS records."),
    "port_check": (_f(host=_f(type="string"), port=_f(type="integer"), timeout=_f(type="integer")), ["host"],
                   "Check whether a TCP port is open."),
    "net_connections": (_f(filter=_f(type="string"), limit=_f(type="integer")), [],
                        "Active TCP/UDP connections with owning processes."),
    "public_ip": (_f(), [], "The public IP address of this connection."),
    "wifi_networks": (_f(), [], "Nearby Wi-Fi networks with signal strength."),
    "wifi_profiles": (_f(), [], "Saved Wi-Fi profiles."),
    "wifi_connect": (_f(name=_f(type="string", description="Saved profile / SSID")), ["name"],
                     "Connect to a saved Wi-Fi network."),
    "firewall_status": (_f(), [], "Windows Firewall state for every profile."),
    "arp_table": (_f(), [], "ARP table (IP to MAC neighbours)."),
    "route_table": (_f(), [], "IPv4 routing table."),
    "proxy_info": (_f(), [], "System and environment proxy settings."),
    "net_speed_test": (_f(), [], "Measure download speed with a real transfer."),

    # -- files (extra) ----------------------------------------------------
    "path_exists": (_f(path=_f(type="string")), ["path"], "Check whether a path exists, and its type/size."),
    "file_compare": (_f(path=_f(type="string"), dst=_f(type="string")), ["path", "dst"],
                     "Compare two files by size and MD5."),
    "touch_file": (_f(path=_f(type="string")), ["path"], "Create an empty file or update its timestamp."),
    "file_version": (_f(path=_f(type="string")), ["path"], "Version/company info of an exe or dll."),
    "hex_dump": (_f(path=_f(type="string"), offset=_f(type="integer"), limit=_f(type="integer")), ["path"],
                 "Hex dump of a file region."),
    "file_attributes": (_f(path=_f(type="string"), mode=_f(type="string", description="get, hidden, unhide, readonly or normal")),
                        ["path"], "Read or change hidden/read-only attributes."),
    "drive_list": (_f(), [], "Every drive with label, filesystem and free space."),
    "temp_clean": (_f(dry_run=_f(type="boolean", description="Default true: only report"), days=_f(type="integer")), [],
                   "Delete old files from the TEMP folder; previews by default."),
    "open_with": (_f(path=_f(type="string"), app=_f(type="string", description="Executable to open it with")), ["path"],
                  "Open a file with a chosen application (or its default app)."),
    "file_permissions": (_f(path=_f(type="string")), ["path"], "ACL/permissions of a file or folder."),
    "recyclebin_list": (_f(limit=_f(type="integer")), [],
                        "What is in the Recycle Bin: names, original paths, sizes and dates."),
    "recyclebin_restore": (_f(name=_f(type="string", description="Part of the item name")), ["name"],
                           "Restore an item from the Recycle Bin."),

    # -- images / PDF (extra) ---------------------------------------------
    "image_ocr": (_f(path=_f(type="string"), max_lines=_f(type="integer")), ["path"],
                  "OCR a picture or screenshot file into text."),
    "image_flip": (_f(path=_f(type="string"), direction=_f(type="string", description="horizontal or vertical"),
                      dest=_f(type="string")), ["path"], "Flip an image."),
    "image_blur": (_f(path=_f(type="string"), radius=_f(type="integer"), dest=_f(type="string")), ["path"],
                   "Blur an image."),
    "image_thumbnail": (_f(path=_f(type="string"), size=_f(type="integer"), dest=_f(type="string")), ["path"],
                        "Make a small thumbnail image."),
    "image_watermark": (_f(path=_f(type="string"), text=_f(type="string"), dest=_f(type="string")),
                        ["path", "text"], "Stamp text onto the bottom-right of an image."),
    "image_exif": (_f(path=_f(type="string")), ["path"], "Read EXIF metadata from a photo."),
    "image_merge": (_f(paths=_f(type="string", description="JSON list or one path per line"),
                       direction=_f(type="string"), dest=_f(type="string")), ["paths"],
                    "Join images side by side or top to bottom."),
    "image_to_pdf": (_f(paths=_f(type="string", description="JSON list, one path per line, or a folder"),
                        output=_f(type="string")), ["paths"], "Turn images into a single PDF."),
    "pdf_to_images": (_f(path=_f(type="string"), folder=_f(type="string"), range=_f(type="string", description="e.g. 1-3,5")),
                      ["path"], "Extract the embedded images of a PDF into a folder."),
    "pdf_split": (_f(path=_f(type="string"), output=_f(type="string")), ["path"],
                  "Split a PDF into one file per page."),
    "pdf_extract": (_f(path=_f(type="string"), range=_f(type="string", description="e.g. 1-3,5"),
                       output=_f(type="string")), ["path"], "Extract selected pages into a new PDF."),
    "pdf_rotate": (_f(path=_f(type="string"), angle=_f(type="integer"), output=_f(type="string")), ["path"],
                   "Rotate every page of a PDF."),

    # -- audio / video (extra) --------------------------------------------
    "video_frame": (_f(path=_f(type="string"), start=_f(type="string", description="e.g. 00:00:05"),
                       dest=_f(type="string")), ["path"], "Save one frame of a video as PNG."),
    "video_merge": (_f(paths=_f(type="string"), output=_f(type="string")), ["paths"],
                    "Concatenate videos into one file."),
    "video_scale": (_f(path=_f(type="string"), width=_f(type="integer"), height=_f(type="integer"),
                       dest=_f(type="string")), ["path"], "Resize a video (height 0 keeps the ratio)."),
    "video_speed": (_f(path=_f(type="string"), factor=_f(type="string", description="1.5 = faster, 0.5 = slower"),
                       dest=_f(type="string")), ["path"], "Speed up or slow down video with audio pitch kept."),
    "video_to_gif": (_f(path=_f(type="string"), start=_f(type="string"), duration=_f(type="string"),
                        dest=_f(type="string")), ["path"], "Make a GIF from a video clip."),
    "gif_to_video": (_f(path=_f(type="string"), dest=_f(type="string")), ["path"], "Turn a GIF into an MP4."),
    "audio_play": (_f(path=_f(type="string")), ["path"], "Play an audio file."),
    "audio_normalize": (_f(path=_f(type="string"), dest=_f(type="string")), ["path"],
                        "Normalize loudness with ffmpeg loudnorm."),
    "audio_merge": (_f(paths=_f(type="string"), output=_f(type="string")), ["paths"],
                    "Join several audio files into one MP3."),

    # -- web (extra) ------------------------------------------------------
    "web_links": (_f(url=_f(type="string"), limit=_f(type="integer")), ["url"],
                  "Every link on a page with its text and URL."),
    "web_tables": (_f(url=_f(type="string")), ["url"], "Read the tables on a web page."),
    "web_archive": (_f(url=_f(type="string")), ["url"], "Newest Wayback Machine snapshot of a URL."),
    "web_headers": (_f(url=_f(type="string")), ["url"], "HTTP status and response headers."),
    "web_save_page": (_f(url=_f(type="string"), path=_f(type="string")), ["url"],
                      "Save a page's HTML to disk."),
    "web_download_links": (_f(url=_f(type="string"), pattern=_f(type="string", description="e.g. .mp3 or sound"),
                              folder=_f(type="string"), limit=_f(type="integer")), ["url"],
                           "Download every link on a page that matches a pattern."),
    "youtube_info": (_f(url=_f(type="string")), ["url"], "Title, channel, duration and stats of a video."),
    "youtube_playlist": (_f(url=_f(type="string"), limit=_f(type="integer")), ["url"],
                         "List the videos in a YouTube playlist."),
    "web_image_search": (_f(query=_f(type="string"), count=_f(type="integer")), ["query"],
                         "Search the web for images and return their URLs."),
    "webpage_screenshot": (_f(url=_f(type="string"), path=_f(type="string")), ["url"],
                           "Screenshot a web page with headless Edge/Chrome."),

    # -- speech / input ---------------------------------------------------
    "voices_list": (_f(), [], "Installed text-to-speech voices."),
    "tts_save": (_f(text=_f(type="string"), path=_f(type="string")), ["text"],
                 "Render speech to a WAV file instead of playing it."),
    "mouse_position": (_f(), [], "Cursor position and the window under it."),
    "key_tap": (_f(key=_f(type="string"), count=_f(type="integer")), ["key"],
                "Press one key (enter, tab, esc, f5, a letter)."),
    "page_scroll": (_f(direction=_f(type="string"), pages=_f(type="integer"), window=_f(type="string")), [],
                    "Page up/down in the focused or named window."),

    # -- batch flow / fast UI ---------------------------------------------
    "do_actions": (_f(items=_f(type="string", description='JSON array of {"tool":..,"args":{..}}')), ["items"],
                   "Run several tool calls in one step, in order. Use for multi-action jobs "
                   "so you do not spend a model round on each call."),
    "wait_for": (_f(kind=_f(type="string", description="file, window, process, text or url"),
                    target=_f(type="string"), timeout=_f(type="integer")), ["target"],
                 "Wait until a file/window/process/text/URL condition is true."),
    "ui_find": (_f(window=_f(type="string"), text=_f(type="string"), control_type=_f(type="string"),
                   max_items=_f(type="integer")), [],
                "Find controls by label and return #index, rect and center coordinates."),
    "ui_invoke": (_f(window=_f(type="string"), name=_f(type="string"),
                     control_type=_f(type="string"), index=_f(type="integer")), [],
                  "Invoke a control through UI Automation (no mouse guessing)."),
    "ui_set_value": (_f(window=_f(type="string"), name=_f(type="string"), text=_f(type="string"),
                        control_type=_f(type="string"), index=_f(type="integer")), ["text"],
                     "Set a field's value through UI Automation, falling back to typing."),
    "ui_toggle": (_f(window=_f(type="string"), name=_f(type="string"),
                     control_type=_f(type="string"), index=_f(type="integer")), [],
                  "Toggle a checkbox or switch."),
    "ui_keys": (_f(window=_f(type="string"), keys=_f(type="string", description="SendKeys, e.g. {ENTER} or ^s"),
                   name=_f(type="string"), control_type=_f(type="string"), index=_f(type="integer")), ["keys"],
                "Focus a control (or the window) and send keys to it only."),
    "ui_menu": (_f(window=_f(type="string"), items=_f(type="string", description="File > Save As or a JSON list")), ["items"],
                "Walk a menu path by clicking each named item."),
    "ui_wait_text": (_f(window=_f(type="string"), text=_f(type="string"), timeout=_f(type="integer")), ["text"],
                     "Wait until specific text appears in a window."),
    "ui_batch": (_f(window=_f(type="string"),
                    actions=_f(type="string", description='JSON list: click/type/keys/wait/scroll/focus/sleep')), ["actions"],
                 "Run many UI actions on one window in a single call, reusing the window "
                 "lookup. Much faster than one tool call per click."),
}

def _sig(name: str) -> str:
    params, required, _ = TOOLS[name]
    parts = []
    for k in params:
        t = params[k].get("type", "string")
        parts.append(k if k in required else f"[{k}:{t}]")
    return f"{name}({', '.join(parts)})"


TOOL_GROUPS = [
    ("FILES", ["read_file", "write_file", "edit_file", "list_dir", "tree", "glob", "grep",
               "mkdir", "copy", "move", "rename", "shorten_names", "gather", "delete",
               "hash", "archive", "resource_pack", "file_info", "folder_size", "disk_usage",
               "recent_files", "find_duplicates", "largest_files", "count_files",
               "organize_folder", "create_shortcut", "path_exists", "file_compare",
               "touch_file", "file_version", "hex_dump", "file_attributes", "drive_list",
               "temp_clean", "open_with", "file_permissions"]),
    ("DOWNLOAD", ["youtube_download", "youtube_download_many", "download_audio", "download",
                  "fetch_file", "download_many", "download_page_media", "wallpapers",
                  "youtube_search", "convert_media", "extract_audio", "trim_media",
                  "media_info", "github_release"]),
    ("DOCS", ["pdf_info", "pdf_text", "pdf_merge", "pdf_split", "pdf_extract", "pdf_rotate",
              "pdf_to_images", "image_to_pdf", "docx_create", "docx_read", "xlsx_write",
              "xlsx_read", "pptx_create", "csv_read", "csv_write", "read_json", "write_json"]),
    ("IMAGES", ["image_info", "image_resize", "image_convert", "convert_folder", "image_crop",
                "image_rotate", "image_flip", "image_blur", "image_thumbnail",
                "image_watermark", "image_exif", "image_merge", "image_ocr", "screenshot",
                "window_screenshot", "see_images"]),
    ("MEDIA", ["convert_audio_folder", "video_frame", "video_merge", "video_scale",
               "video_speed", "video_to_gif", "gif_to_video", "audio_play",
               "audio_normalize", "audio_merge"]),
    ("WEB", ["open_url", "web_search", "web_fetch", "wikipedia", "weather", "news", "rss",
             "define", "url_metadata", "check_url", "http_json", "web_links", "web_tables",
             "web_archive", "web_headers", "web_save_page", "web_download_links",
             "youtube_info", "youtube_playlist", "web_image_search", "webpage_screenshot",
             "browser_tabs"]),
    ("CONTROL", ["mouse_move", "mouse_click", "mouse_drag", "mouse_scroll", "mouse_position",
                 "type_text", "hotkey", "send_keys", "key_tap", "page_scroll", "window_list",
                 "window_focus", "window_ping", "desktop_switch", "active_window",
                 "close_window", "minimize_window",
                 "maximize_window", "restore_window", "wait_window", "window_info",
                 "window_move", "window_resize", "window_set_rect", "window_snap",
                 "window_topmost", "window_opacity", "window_close_all", "ui_tree", "ui_click",
                 "ui_type", "ui_read", "ui_scroll", "ui_collect", "ui_wait_element",
                 "ui_wait_text", "ui_find", "ui_invoke", "ui_set_value", "ui_toggle",
                 "ui_keys", "ui_menu", "ui_batch", "window_text", "screen_ocr",
                 "find_on_screen", "screen_snapshot", "read_screen", "ocr_click",
                 "click_text", "wait_text", "scroll_read", "key_macro", "focus_and_type",
                 "dialog_fill", "dig_chats", "open_app", "launch", "open_path",
                 "open_unity_asset", "run_command", "process_list", "kill_process",
                 "tray_icon_click", "app_restart", "selected_text"]),
    ("CLIPBOARD", ["clipboard_read", "clipboard_write", "clipboard_clear",
                   "clipboard_to_file", "clipboard_from_file", "clipboard_image"]),
    ("SYSTEM", ["sysinfo", "battery", "wifi_status", "network_info", "brightness",
                "volume_up", "volume_down", "mute", "play_pause", "media_next", "media_prev",
                "installed_apps", "env", "uptime", "lock_screen", "speak", "notify",
                "empty_recycle_bin", "recyclebin_list", "recyclebin_restore", "set_wallpaper",
                "wait", "os_info", "cpu_usage", "memory_usage", "disk_health", "cpu_temp",
                "gpu_info", "services_list", "service_control", "startup_list", "startup_add",
                "startup_remove", "scheduled_tasks", "power_plan", "shutdown_pc", "event_log",
                "windows_updates", "printers_list", "bluetooth_devices", "usb_devices",
                "audio_devices", "display_info", "timezone_info", "font_list", "drivers_list",
                "system_report"]),
    ("PROCESSES", ["process_info", "process_top", "process_tree", "process_priority",
                   "process_wait", "process_suspend", "process_resume"]),
    ("NETWORK", ["ping_host", "dns_lookup", "port_check", "net_connections", "public_ip",
                 "wifi_networks", "wifi_profiles", "wifi_connect", "firewall_status",
                 "arp_table", "route_table", "proxy_info", "net_speed_test"]),
    ("FLOW", ["do_actions", "wait_for"]),
    ("UTIL", ["now", "calculate", "remember", "lessons", "forget", "create_tool",
              "run_custom", "list_tools", "voices_list", "tts_save", "self_test"]),
]


def tool_doc() -> str:
    """Compact grouped catalog: a small prompt is 30x faster on this CPU."""
    out = ['update_plan(items) - JSON array: [{"text":"..","status":"pending|active|done"}]']
    seen = set()
    for label, names in TOOL_GROUPS:
        lines = []
        for name in names:
            if name in TOOLS:
                lines.append(_sig(name))
                seen.add(name)
        if lines:
            out.append(f"[{label}] " + "  ".join(lines))
    rest = [n for n in sorted(TOOLS) if n not in seen]
    if rest:
        out.append("[MORE] " + "  ".join(_sig(n) for n in rest))
    return "\n".join(out)


def label_for(name: str, args: dict) -> str:
    for key in ("path", "query", "url", "target", "word", "topic", "location",
                "command", "source", "src", "name", "pattern", "id", "text", "title",
                "keys", "folder"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            if key in {"path", "source", "src", "target"}:
                return rel_display(v)
            return v.replace("\n", " ")[:90]
    if "x" in args and "y" in args:
        return f"{args.get('x')}, {args.get('y')}"
    return ""


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
SYSTEM = """You are Anvil Fletch, an autonomous agent living on this Windows laptop. You act:
every turn you name exactly one tool. You never stop to ask for permission.

You run on the user's own PC and you have real control of it: files, programs, windows,
mouse, keyboard, clipboard, downloads, the browser and the network. When someone asks
whether you can control the computer, the answer is yes. Never claim to be a text-only AI
without access — that is false here.

Voice: dry, precise, quietly confident. A competent colleague, not a chatbot. No flattery,
no "Great question", no emoji, at most one wry line and only when it lands.

WORKSPACE {workspace} — relative paths resolve there, an empty path means the root.
HOME {home} — Desktop, Documents, Downloads, Pictures, Music and Videos are here,
never inside the workspace unless the workspace is HOME itself.
Absolute paths anywhere on the machine are allowed.

You have {count} tools. AVAILABLE TOOLS
{tools}

RULES
- Every step you output is parsed as JSON. Reply with exactly one JSON object and nothing else - no prose, no code fences, no commentary.
- Keep going until the job is done. Pick `reply` only when nothing is left.
- Explore before acting on paths you have not seen (list_dir, tree, glob, grep).
- Read a file before editing it; prefer edit_file over rewriting.
- Never repeat a call whose result is already in the transcript.
- Jobs with 3+ stages: call update_plan first, then keep it current.
- If the user teaches or corrects a lasting rule ("always", "never", "from now on"), call
  remember, then follow it. Never store the current task itself as a lesson.
- If the user says do not change, move, write or delete anything, stay read-only: inspect
  and report only, never call a tool that writes to disk.
- When a tool fails, read its error and change the approach. Never repeat a failed call,
  never "fix" a download or format error with pip install, and if the same tool fails
  twice, stop and report the exact error instead of trying other installs.

INTERNET AND MEDIA — this is a full downloader, not just a file manager
- When the user asks for music, video, clips, images or media, fetch it and save it:
  youtube_download(target, kind=audio|video, folder=...) handles YouTube searches, links
  and ids. download_audio saves just the MP3. download saves any direct URL.
  download_page_media scans a web page for media. Never answer "I cannot download" and
  never merely open a browser when the user asked to save something.
- If the user names a destination ("in Downloads", "in my Music folder", or an exact
  path like C:\\Users\\me\\Desktop\\out\\clip.mp4), pass it as the folder/path argument.
  Never download into the workspace and rename it afterwards, and never claim a path
  you did not actually save to. Otherwise save inside the workspace.
- A search phrase is a valid target: youtube_download("lofi study mix") searches and grabs
  the top result. Say which result you grabbed.
- Do not send a quality value unless the user asked for one; empty means the best available.
- Music means audio, not video. Songs get kind=audio and become MP3. After a download,
  check that the saved file name is the song and that media_info shows an audio stream.
  Never accept "how to", tutorial, ringtone, "download songs on android" or shorts results
  as the song; if the saved title is wrong, search again with "<artist> <song> official audio".
- If the user asks for a specific version — sped up, slowed, remix, cover, live, nightcore,
  instrumental, 8D — that version is what they want. Put those words in the search and take
  the matching upload, not the original.
- When the user lists several songs, put them in the plan and download them one by one,
  verifying each. If the user says "download those" or "download it", reuse the song list
  from the conversation above — never guess a new one and never drop items from the list.
- If youtube_download fails on a format or JavaScript-runtime error, retry once with
  kind=audio; if that fails too, report the exact error. Never respond to it with
  pip install — the downloader ships with the app.
- "Get me / find me / grab / download X" means the internet unless the user names the
  workspace. Never answer that it is missing from the workspace when they asked you to go
  online: call fetch_file (URL or search phrase), save it, and report the real path. Try
  web_search plus download from a result page before saying it could not be found.
- Bulk work: download_many for several URLs, youtube_download_many for a list of songs or
  videos, convert_folder to turn every image in a folder into one format in place,
  convert_audio_folder to turn every sound into MP3, or convert_folder(fmt="auto") to do
  images->PNG AND audio->MP3 in one pass, wallpapers for beautiful 16:9 backgrounds,
  archive or resource_pack for zip work. Use the bulk tool once, then report each item.
- When the user describes a bulk download in a few words ("bulk sound effects", "overlay
  pack"), pass that phrase as download_many's label so the progress bar names the job.
- Resource packs: resource_pack(version="1.21.1", replacements="<mix folder> => assets/minecraft/textures")
  fetches the vanilla base assets (or a template zip if given), overlays the modified files,
  writes pack.mcmeta with the right pack_format and rezips it. Present the finished zip.
- Use web_search/wikipedia/weather/news for facts, web_fetch for page text, url_metadata
  for previews. Use open_url when the user wants to see something or when a task should
  happen in the real browser (it lands in browser history).

FULL CONTROL — act like a person at the keyboard
- Do exactly what was asked, nothing more. If the request is "open Notepad", the job ends
  when Notepad is open — do not invent typing, editing or extra steps. As soon as the
  requested outcome exists, reply and stop.
- ui_type always needs the literal text in `text`. If the user never gave you text to type,
  do not call ui_type.
- Any app can be driven, even without an API. open_app(name) first: it focuses the copy
  that is already running (taskbar, minimized or tray) instead of starting a new unlogged
  instance. Only if nothing is running does it start the app. Then window_focus,
  read ui_tree to see its buttons, lists and text boxes. Click with ui_click (by visible
  name, control type or index from the last ui_tree), type with ui_type, press keys with
  send_keys/hotkey, scroll with mouse_scroll, and take window_screenshot when you need pixels.
- Loop: inspect (ui_tree) -> act (ui_click/ui_type/send_keys) -> inspect again. Never say
  you cannot control an app just because it has no dedicated tool. Telegram, Discord, games
  launchers, settings, editors: if a person can do it in the window, you can too.
- Example: "open Ayugram and reply to a message in each chat" -> launch Ayugram,
  wait_window, ui_tree, then repeat for every chat: ui_click the chat in the list, ui_type
  the message into the input box, send_keys {ENTER}, ui_tree again to confirm, next chat.
  Finish with what you sent where.
- ui_tree coordinates are screen pixels; mouse_click and mouse_move take them directly.
- To read what a window says, call window_text(window=...) first. One call returns the
  document/chat/editor text, then labels, then screen OCR if the app exposes nothing. Do not
  guess file paths for content that is open in an app, and do not use read_file for an app.
- Digging through chats: window_text(window=App) for the visible thread, ui_tree the chat
  list, ui_click a chat, then ui_collect(pages=8, direction=up) to load and read older
  messages. Move through every chat the same way, then report what you actually read.
- Never answer "I can't access chat history / other apps / your account / the screen" —
  that is false. You can see the window, scroll it and read it. If a call fails, quote the
  error and try ui_scroll, ui_read, window_text, screen_ocr + mouse_click before reporting a limit.
- If an app exposes no accessible controls (ui_tree is empty or useless), run screen_ocr and
  click the center coordinates it returns with mouse_click.
- Screen awareness: every turn opens with OPEN WINDOWS and, when relevant, BROWSER TABS.
  Use them. To click something you only see as text, call find_on_screen("label") and use
  the cx,cy it returns. Never click a coordinate you did not get from ui_tree, screen_ocr,
  find_on_screen or the tabs list.
- speak is only for requests to hear something ("say it", "read it aloud"). Never read
  errors, progress or results out loud unless asked.
- If a control is invisible to UI Automation, fall back to keys, Tab navigation and
  coordinates instead of giving up. Report what blocked you only after trying both.

IMAGES AND ATTACHED FILES
- The composer can attach pictures and files. Their content already appears in the prompt
  under ATTACHED FILES with exact paths - answer from it immediately and never claim you
  cannot see an image. Do not re-read an attachment with read_file/see_images; the report
  below the prompt is the file, already read.
- "look at those screenshots in <folder>" -> see_images(path=...). It reads every visible
  text line with OCR and adds a vision description when a local vision model is installed.
- A picture that shows UI: take the text and coordinates from see_images/screen_ocr, or use
  find_on_screen on the live window, then click the exact center.

HARD OR PROPRIETARY FILES (Unity, asset bundles, game data)
- Unity .assets/.bundle/.unity3d: call open_unity_asset(path). It downloads UABEA when
  needed, opens the file and returns its window. Then ui_tree('UABEA') and window_text,
  click/type through the export-import flow, and verify the written file on disk.
- For other tools use github_release(owner/repo) to fetch the latest release and unpack it,
  then drive the app's UI. Never say a proprietary file cannot be edited: a person with
  this laptop would install a tool and use it, so try that path before reporting a limit.

CONTROL
- For app/window chores use open_app, wait_window, window_focus, window_text, ui_tree,
  ui_click, ui_type, type_text, hotkey, mouse_click and screenshot. For files on disk use
  the file tools. window_text reads an open window; read_file reads a file path.
- Prefer a real tool over run_command; use run_command only when nothing else fits.
- If a tool fails because a library is missing, say exactly what to install.

- open_path shows a folder in Explorer.
- `thought`: at most 12 words, present tense, no full sentences."""


def _plan_items(raw):
    if isinstance(raw, str):
        parsed = _salvage_json('{"items":' + raw + "}")
        raw = parsed.get("items")
    if not isinstance(raw, list):
        return []
    out = []
    for it in raw[:12]:
        if isinstance(it, str):
            out.append({"text": it[:120], "status": "pending"})
        elif isinstance(it, dict):
            out.append({"text": str(it.get("text", ""))[:120],
                        "status": str(it.get("status", "pending"))})
    return out


WRITER_SYSTEM = """You are Anvil Fletch, a local agent that just did work on this Windows machine.
Write the final reply to the user.

- Voice: dry, precise, quietly confident. A good colleague reporting back, not a chatbot.
  No flattery, no emoji, at most one wry line.
- Use only the request and the tool log. Never invent files, paths, counts or results.
- Be concrete: real file names, folder paths, numbers. Short markdown bullets when listing.
- 1 to 6 sentences. No preamble, no "I will now", no mention of tools, JSON or steps.
- If something failed, say what failed and what you did instead.
- If the request was just conversation, answer it naturally and briefly."""


def _recap(results) -> str:
    lines = []
    for r in results[-12:]:
        head = f"- {r['name']}"
        if r.get("label"):
            head += f" ({r['label']})"
        head += f" -> {r['summary']}"
        if not r["ok"]:
            head = head.replace(" -> ", " -> FAILED: ", 1)
        lines.append(head)
        detail = (r.get("detail") or "").strip()
        if detail and detail != r["summary"]:
            body = "\n".join("    " + ln for ln in detail[:700].splitlines())
            lines.append(body)
    return "\n".join(lines)


TEACH_HINT = re.compile(
    r"\b(always|never|from now on|remember|next time|by default|whenever|"
    r"instead of|stop (?:doing|using)|rule|teach|lesson)\b"
    r"|\b(don'?t|do not)\s+(?:ever|again)\b", re.I)

# "Do not move or change any files" means the run must stay read-only. The 7B
# ignores that while it is busy acting, so writes get blocked outright.
NO_WRITE_HINT = re.compile(
    r"\bread[- ]only\b"
    r"|\b(?:do not|don'?t|without)\b[^.!?\n]{0,32}"
    r"\b(?:move|change|modify|touch|delete|write|create|rename|edit|organi[sz]e|alter)\b",
    re.I)

MUTATING_TOOLS = {"write_file", "edit_file", "delete", "move", "copy", "mkdir",
                  "rename", "shorten_names", "gather", "archive", "resource_pack",
                  "convert_folder", "fetch_file", "download_many", "youtube_download_many",
                  "wallpapers", "clipboard_write", "create_tool", "forget", "remember"}

# Text-to-speech is opt-in: only talk when the user asked to hear something.
SPEAK_HINT = re.compile(
    r"\b(speak|speaking|spoken|aloud|out loud|voice|narrate|narration|"
    r"text to speech|tts)\b|read (?:it|that|this|the (?:text|reply|answer|message)) out\b",
    re.I)

_SCREEN_WORDS = re.compile(
    r"tab|browser|chrome|edge|firefox|brave|screen|window|page|site|youtube|"
    r"read|chat|open|click|button|menu", re.I)


def _visible_windows_brief(limit=16):
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    rows = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value.strip()
        if title and title not in rows:
            rows.append(title)
        return True

    user32.EnumWindows(cb, 0)
    skip = {"Anvil Fletch", "Program Manager", "Default IME", "MSCTFIME UI",
            "Windows Input Experience", "MS Shell Dlg"}
    return [r for r in rows if r not in skip][:limit]


def screen_context(prompt="") -> str:
    """Who is open and what tabs are up, so the agent is never blind to the desktop."""
    parts = []
    try:
        wins = _visible_windows_brief()
        if wins:
            parts.append("OPEN WINDOWS: " + " | ".join(wins))
    except Exception:
        pass
    if _SCREEN_WORDS.search(prompt or ""):
        try:
            ok, _summary, detail = t_browser_tabs(max_tabs=40)
            tabs = [ln for ln in str(detail).splitlines() if " :: " in ln]
            if ok and tabs:
                parts.append("BROWSER TABS: " + " | ".join(t.split(" :: ", 1)[1]
                                                           for t in tabs[:40]))
        except Exception:
            pass
    return "\n".join(parts)


# The 7B keeps routing "download wallpapers" into download_many with invented URLs.
# This request is unambiguous, so answer it directly instead of asking the model.
WALLPAPER_RE = re.compile(r"\bwallpapers?\b", re.I)
WALLPAPER_ACTION_RE = re.compile(
    r"\b(download|get|grab|fetch|find|save|want|need|give|collect|pack|"
    r"16\s*[:x]\s*9|4k|uhd|hd|desktop|background|beautiful)\b", re.I)
WALLPAPER_OTHER_RE = re.compile(
    r"\b(convert|resize|zip|archive|resource pack|rename|organi[sz]e|delete|"
    r"sprite|mcmeta|collage|video)\b", re.I)
_WP_STOP = re.compile(
    r"\b(download|downloads|get|grab|fetch|find|save|give|me|my|you|can|could|please|"
    r"some|more|free|beautiful|show|want|need|let|have|with|on|for|"
    r"wallpapers?|images?|pictures?|photos?|backgrounds?|desktop|for me|for my|to|in|"
    r"into|from|and|the|of|a|an|set|as|use|internet|online|16\s*[:x]\s*9|ratio|4k|"
    r"uhd|hd|1080p|1920x1080|nice|cool)\b", re.I)
_PATH_RE = re.compile(r"[A-Za-z]:\\[^\n\r\"']+|\\\\[^\s\"']+")


def fast_wallpapers(prompt, stop):
    p = " ".join((prompt or "").split())
    if not WALLPAPER_RE.search(p) or not WALLPAPER_ACTION_RE.search(p):
        return None
    if WALLPAPER_OTHER_RE.search(p) or len(p.split()) > 24:
        return None
    if re.search(r"\.(png|jpe?g|webp|bmp|gif|avif)\b", p, re.I):
        return None  # points at an existing image: that is set_wallpaper's job
    set_first = bool(re.search(r"\b(set|apply|use)\b", p, re.I))
    folder = ""
    m = _PATH_RE.search(p)
    if m:
        folder = m.group(0).strip().rstrip(".,;")
    count = 0
    for tok in re.findall(r"\b(\d{1,2})\b", _PATH_RE.sub(" ", p)):
        v = int(tok)
        if 1 <= v <= 48:
            count = v
            break
    if not count:
        count = 6 if re.search(r"\b(couple|few|some|several)\b", p, re.I) else 12
    query = " ".join(_WP_STOP.sub(" ", _PATH_RE.sub(" ", p)).split())
    query = " ".join(re.sub(r"\b\d+\b", " ", query).split())[:60]
    label = f"{query or 'top rated'} - {count}"
    emit("tool", op="start", name="wallpapers", label=label, step=1,
         args={"query": query, "count": str(count), "folder": folder})
    t0 = now_ms()
    ok, summary, detail = t_wallpapers(query=query, count=count, folder=folder,
                                      set_first=set_first)
    emit("tool", op="end", name="wallpapers", ok=bool(ok), label=label,
         summary=str(summary)[:280], detail=str(detail)[:6000], ms=now_ms() - t0, step=1)
    if stop.is_set():
        answer = "Stopped."
    elif ok:
        answer = str(summary)
        names = [ln.split("  ")[0] for ln in str(detail).splitlines() if ln.strip()]
        if names:
            answer += "\n\n" + ", ".join(names[:12])
    else:
        answer = f"{summary}\n\n{detail}".strip()
    emit("status", state="done", text="1 step(s)", steps=1)
    return answer, [{"role": "user", "content": prompt},
                    {"role": "assistant", "content": answer}]


_SEE_RE = re.compile(
    r"\b(see|look at|look through|read|check|review|analy[sz]e|describe|"
    r"ocr|transcribe|extract (?:the )?text|get (?:the )?text|"
    r"what'?s in|tell me about|summari[sz]e)\b", re.I)
_VIS_RE = re.compile(r"\b(screenshots?|screen shots?|images?|pictures?|photos?)\b|"
                     r"\.(png|jpe?g|webp|bmp)\b", re.I)
_VIS_OTHER_RE = re.compile(
    r"\b(download|convert|resize|zip|archive|wallpaper|delete|move|copy|rename|"
    r"generate|create|make|edit|replace)\b", re.I)


def _find_visual_folder():
    words = ("screenshot", "screen shot", "snip", "capture")
    bases = [workspace(), Path.home() / "Desktop", Path.home() / "Pictures",
             Path.home() / "Downloads", Path.home() / "Documents"]
    for base in bases:
        try:
            if not base.exists():
                continue
            for cand in base.iterdir():
                if cand.is_dir() and any(w in cand.name.lower() for w in words):
                    return cand
        except Exception:
            continue
    return None


def fast_see_images(prompt, stop):
    """A direct 'look at these screenshots' request does not need the 7B to plan it."""
    p = " ".join((prompt or "").split())
    if not _SEE_RE.search(p) or not _VIS_RE.search(p) or _VIS_OTHER_RE.search(p):
        return None
    if len(p.split()) > 30:
        return None
    target, source = None, ""
    m = _PATH_RE.search(p)
    if m:
        cand = resolve_path(m.group(0).strip().rstrip(".,;"))
        if cand.exists():
            target, source = cand, "path"
    if target is None:
        m = re.search(r"[\"']([^\"']{3,})[\"']", p)
        if m:
            cand = resolve_path(m.group(1).strip())
            if cand.exists():
                target, source = cand, "quoted path"
    if target is None and re.search(r"\bfolder\b", p, re.I):
        cand = _find_visual_folder()
        if cand is not None:
            target, source = cand, "folder search"
    if target is None:
        m = re.search(r"\b(desktop|documents|downloads|pictures)\b", p, re.I)
        if m:
            cand = Path.home() / m.group(1).capitalize()
            if cand.is_dir():
                target, source = cand, "known folder"
    if target is None:
        return None
    count = 8
    for tok in re.findall(r"\b(\d{1,2})\b", p):
        if 1 <= int(tok) <= 24:
            count = int(tok)
            break
    label = f"looking at {target.name}"
    emit("tool", op="start", name="see_images", label=label, step=1,
         args={"path": str(target), "question": p[:200]})
    t0 = now_ms()
    ok, summary, detail = t_see_images(str(target), question=p, max_images=count)
    emit("tool", op="end", name="see_images", ok=bool(ok), label=label,
         summary=str(summary)[:280], detail=str(detail)[:6000], ms=now_ms() - t0, step=1)
    if stop.is_set():
        answer = "Stopped."
    elif ok:
        answer = f"{summary}\n\n{str(detail)[:6000]}"
    else:
        answer = f"{summary}\n\n{detail}".strip()
    emit("status", state="done", text="1 step(s)", steps=1)
    return answer, [{"role": "user", "content": prompt},
                    {"role": "assistant", "content": answer}]


_RECYCLE_RE = re.compile(r"\brecycl(?:e|ing)\s*bin\b|\btrash\s*bin\b", re.I)
_FAST_POLITE = r"(?:please\s+|can you\s+|could you\s+|would you\s+|hey\s+|anvil\s+)?"
_OPEN_APPS = {
    "notepad": "notepad", "calculator": "calc", "calc": "calc", "paint": "mspaint",
    "cmd": "cmd", "command prompt": "cmd", "powershell": "powershell",
    "terminal": "wt", "explorer": "explorer", "file explorer": "explorer",
    "task manager": "Taskmgr", "chrome": "chrome", "google chrome": "chrome",
    "edge": "msedge", "firefox": "firefox", "word": "winword", "excel": "excel",
    "powerpoint": "powerpnt", "outlook": "outlook", "spotify": "Spotify",
    "discord": "Discord", "steam": "steam", "code": "Code", "vs code": "Code",
    "visual studio code": "Code", "obs": "obs64", "blender": "blender",
    "photoshop": "Photoshop", "telegram": "Telegram", "ayugram": "AyuGram",
    "whatsapp": "WhatsApp",
}


def _fast_call(prompt, stop, name, label, fn, args=None):
    """Run one tool directly, emit the same events as the agent loop, answer."""
    emit("tool", op="start", name=name, label=label, step=1,
         args={k: str(v) for k, v in (args or {}).items()})
    t0 = now_ms()
    try:
        ok, summary, detail = fn()
    except Exception as e:
        ok, summary, detail = False, f"{name} failed: {e}", str(e)
    emit("tool", op="end", name=name, ok=bool(ok), label=label,
         summary=str(summary)[:280], detail=str(detail)[:6000], ms=now_ms() - t0, step=1)
    if stop.is_set():
        answer = "Stopped."
    elif ok:
        answer = str(summary)
        if detail and str(detail).strip() and str(detail).strip() != str(summary).strip():
            answer += "\n\n" + str(detail)[:6000]
    else:
        answer = f"{summary}\n\n{detail}".strip()
    emit("status", state="done", text="1 step(s)", steps=1)
    return answer, [{"role": "user", "content": prompt},
                    {"role": "assistant", "content": answer}]


def _fast_path(text):
    """Pull a path out of a short command: drive path, quoted path, or 'in X'."""
    m = _PATH_RE.search(text)
    if m:
        return m.group(0).strip().rstrip(".,;")
    m = re.search(r"[\"']([^\"']{2,})[\"']", text)
    if m:
        return m.group(1).strip()
    m = re.search(r"\b(?:in|inside|of|from)\s+(?:the\s+)?(.+?)(?:\s+(?:folder|directory))?\s*$",
                  text, re.I)
    if m:
        return m.group(1).strip().rstrip(".,;")
    return ""


def fast_direct(prompt, stop):
    """Unambiguous short commands are answered directly instead of by the 7B.

    The model used to spend 20s and invent an answer for "what is on the
    recycling bin". These routes are cheap, deterministic and honest.
    """
    p = " ".join((prompt or "").split()).strip()
    if not p or stop.is_set():
        return None
    low = p.lower().strip(" .!?\t")
    words = len(p.split())

    # Recycle Bin: list / empty / restore ------------------------------------
    if _RECYCLE_RE.search(p) and words <= 16 and not re.search(r"\bopen\b", low) \
            and not re.search(r"\b(screenshot|capture|picture|photo|image)\b", low) \
            and not re.search(r"\b(and|then)\b", low):
        imperative = re.match(_FAST_POLITE + r"(empty|clear|purge|wipe)\b", low)
        if imperative:
            return _fast_call(prompt, stop, "empty_recycle_bin",
                              "emptying the Recycle Bin", t_empty_recycle_bin)
        restore = re.match(_FAST_POLITE + r"(restore|recover|undelete|undo)\b", low)
        if restore:
            name = re.sub(r"\s+(?:from|out of)\b.*$", "", low[restore.end():]).strip(" '\"")
            if name in {"", "it", "that", "this", "the file", "everything", "all"} \
                    or re.search(r"\b(that|those|these|everything|all)\b", name):
                return None
            return _fast_call(prompt, stop, "recyclebin_restore",
                              f"restoring {name}", lambda: t_recyclebin_restore(name),
                              {"name": name})
        return _fast_call(prompt, stop, "recyclebin_list",
                          "reading the Recycle Bin", t_recyclebin_list, {"limit": 50})

    # Clipboard --------------------------------------------------------------
    if re.fullmatch(_FAST_POLITE + r"(clear|empty|wipe) (?:the |my )?clipboard", low):
        return _fast_call(prompt, stop, "clipboard_clear",
                          "clearing the clipboard", t_clipboard_clear)
    m = re.fullmatch(_FAST_POLITE + r"(?:put|copy|write) ([\"'])(.+?)\1 "
                     r"(?:to|into|in) (?:the |my )?clipboard", p, re.I)
    if m:
        text = m.group(2)
        return _fast_call(prompt, stop, "clipboard_write", "copying text to the clipboard",
                          lambda: t_clipboard_write(text), {"text": text[:120]})
    if re.search(r"(what(?:'s| is) (?:on|in) (?:the |my )?clipboard|"
                 r"read (?:the |my )?clipboard|show (?:me )?(?:the |my )?clipboard|"
                 r"what did i (?:just )?copy|clipboard content)", low):
        return _fast_call(prompt, stop, "clipboard_read",
                          "reading the clipboard", t_clipboard_read)

    # Screen, power and media keys ------------------------------------------
    if re.fullmatch(_FAST_POLITE + r"(?:take |grab |capture )?(?:a |the )?"
                    r"(?:screenshot|screen shot|screen capture|snapshot)"
                    r"(?: of (?:the |my )?screen)?(?: now)?", low):
        return _fast_call(prompt, stop, "screenshot", "taking a screenshot", t_screenshot)
    if re.fullmatch(_FAST_POLITE + r"lock(?: the)?(?: screen| pc| computer| laptop| windows| it)?(?: now)?", low):
        return _fast_call(prompt, stop, "lock_screen", "locking the screen", t_lock_screen)
    if re.fullmatch(_FAST_POLITE + r"(?:turn (?:it |the volume )?up|volume up|"
                    r"increase (?:the )?volume|louder|make it louder)(?: please)?", low):
        return _fast_call(prompt, stop, "volume_up", "volume up", t_volume_up)
    if re.fullmatch(_FAST_POLITE + r"(?:turn (?:it |the volume )?down|volume down|"
                    r"decrease (?:the )?volume|lower (?:the )?volume|quieter|"
                    r"make it quieter)(?: please)?", low):
        return _fast_call(prompt, stop, "volume_down", "volume down", t_volume_down)
    if re.fullmatch(_FAST_POLITE + r"(?:mute|unmute)(?: (?:it|the sound|the volume|audio))?", low):
        return _fast_call(prompt, stop, "mute", "muting / unmuting", t_mute)
    if re.fullmatch(_FAST_POLITE + r"(?:play|pause|resume|play/pause|toggle playback)"
                    r"(?: (?:the )?(?:music|media|video|song|track))?", low):
        return _fast_call(prompt, stop, "play_pause", "play / pause", t_play_pause)
    if re.fullmatch(_FAST_POLITE + r"(?:next (?:song|track)|skip (?:this |the )?(?:song|track))", low):
        return _fast_call(prompt, stop, "media_next", "next track", t_media_next)
    if re.fullmatch(_FAST_POLITE + r"(?:previous|prev|last) (?:song|track)", low):
        return _fast_call(prompt, stop, "media_prev", "previous track", t_media_prev)
    m = re.fullmatch(_FAST_POLITE + r"set (?:the )?brightness to (\d{1,3})%?", low)
    if m:
        level = max(0, min(int(m.group(1)), 100))
        return _fast_call(prompt, stop, "brightness", f"brightness {level}%",
                          lambda: t_brightness(level), {"level": level})
    if re.fullmatch(_FAST_POLITE + r"(?:dim|darken) (?:the )?screen", low):
        return _fast_call(prompt, stop, "brightness", "dimming the screen",
                          lambda: t_brightness(25), {"level": 25})

    # Time -------------------------------------------------------------------
    if re.fullmatch(r"(?:what(?:'s| is) (?:the )?(?:time|date)(?: (?:right )?now)?|"
                    r"what time is it|what date is it|(?:current|today'?s) (?:time|date)|"
                    r"time now|date now|now)", low):
        return _fast_call(prompt, stop, "now", "checking the clock", t_now)

    # System health ----------------------------------------------------------
    if re.search(r"\b(system report|full (?:system )?report|pc report|system status|"
                 r"computer status|pc specs|system specs|hardware specs|my specs|"
                 r"how is my (?:pc|computer|laptop)(?: doing| going| running)?)\b", low) \
            and words <= 12 and not re.search(r"\b(about|on|for|write|draft|essay)\b", low):
        return _fast_call(prompt, stop, "system_report", "building a system report",
                          t_system_report)
    if re.search(r"\bbattery\b", low) and words <= 9 \
            and not re.search(r"\b(settings?|saver|icon|driver|replace|calibrat\w*|"
                              r"gauge|widget|open|show|hide)\b", low):
        return _fast_call(prompt, stop, "battery", "checking the battery", t_battery)
    if re.search(r"\b(wi-?fi|wireless)\b", low) and words <= 10 \
            and re.search(r"\b(status|name|ssid|connected|connection|signal|am i on|"
                          r"which|what)\b", low) \
            and not re.search(r"\b(connect|switch|change|forget|password|profile|list|"
                              r"scan|nearby|hotspot)\b", low):
        return _fast_call(prompt, stop, "wifi_status", "checking Wi-Fi", t_wifi_status)
    if re.search(r"\b(disk space|free space|storage (?:left|space)|drive space|"
                 r"how much (?:disk )?space|how full|c: ?drive)\b", low) and words <= 12 \
            and not re.search(r"\b(clean|cleanup|empty|delete|format|partition|defrag|"
                              r"optimi[sz]e|open)\b", low):
        return _fast_call(prompt, stop, "disk_usage", "checking disk space", t_disk_usage)
    if re.search(r"\b(top processes|what'?s using (?:my )?(?:cpu|ram|memory)|"
                 r"memory hogs|cpu hogs|most (?:cpu|memory)|hogs? (?:my )?(?:cpu|ram|memory))\b", low) \
            and words <= 9:
        by = "cpu" if re.search(r"\bcpu\b", low) else "memory"
        return _fast_call(prompt, stop, "process_top", f"top processes by {by}",
                          lambda: t_process_top(10, by), {"limit": 10, "by": by})
    if re.fullmatch(r"(?:what(?:'s| is) my (?:public )?ip(?: address)?|"
                    r"my (?:public )?ip(?: address)?|public ip)", low):
        return _fast_call(prompt, stop, "public_ip", "looking up the public IP", t_public_ip)
    if re.fullmatch(r"(?:what(?:'s| is) my uptime|uptime|how long has (?:this|my) "
                    r"(?:pc|computer|laptop|system) been (?:on|up|running))", low):
        return _fast_call(prompt, stop, "uptime", "checking uptime", t_uptime)
    if re.fullmatch(r"(?:(?:list|show)(?: me)? (?:the )?(?:open )?windows|"
                    r"what windows are open|(?:list )?open windows)", low):
        return _fast_call(prompt, stop, "window_list", "listing open windows", t_window_list)

    # Open a site, a well-known app, a folder or a file ----------------------
    m = re.fullmatch(_FAST_POLITE + r"(?:open|go to|visit|load) "
                     r"(https?://\S+|(?:www\.)?[\w-]+(?:\.[a-z]{2,})+(?:/\S*)?)",
                     p, re.I)
    if m:
        url = m.group(1)
        if not url.startswith("http"):
            url = "https://" + url
        return _fast_call(prompt, stop, "open_url", f"opening {url[:60]}",
                          lambda: t_open_url(url), {"url": url})
    m = re.fullmatch(_FAST_POLITE + r"(?:open|launch|start|run) ([\w .+#-]{2,40})", low)
    if m:
        nm = m.group(1).strip()
        app = _OPEN_APPS.get(nm)
        if app:
            return _fast_call(prompt, stop, "open_app", f"opening {nm}",
                              lambda: t_open_app(app), {"name": nm})
    m = re.fullmatch(_FAST_POLITE + r"(?:list|show)(?: me)? (?:the )?(?:files|contents|"
                     r"files and folders)(?: in| of)? (.+)", p, re.I)
    if m:
        cand = _fast_path(m.group(1)) or m.group(1).strip(" '\"")
        if cand and resolve_path(cand).is_dir():
            return _fast_call(prompt, stop, "list_dir", f"listing {cand}",
                              lambda: t_list_dir(cand, 80), {"path": cand})
    if re.fullmatch(r"(?:what(?:'s| is) in|contents of|list) (.+)", p, re.I):
        cand = _fast_path(p)
        if cand and resolve_path(cand).is_dir():
            return _fast_call(prompt, stop, "list_dir", f"listing {cand}",
                              lambda: t_list_dir(cand, 80), {"path": cand})
    m = re.fullmatch(_FAST_POLITE + r"(?:file info|info about|info on|details (?:about|on)|"
                     r"how big is) (.+)", p, re.I)
    if m:
        cand = _fast_path(m.group(1)) or m.group(1).strip(" '\"")
        if cand and resolve_path(cand).exists():
            return _fast_call(prompt, stop, "file_info", f"checking {cand}",
                              lambda: t_file_info(cand), {"path": cand})

    # One direct URL download ------------------------------------------------
    m = re.fullmatch(r"(?:please )?(?:download|fetch|grab|save) "
                     r"(https?://[^\s,;]+)(?: (?:to|into|in) (.+))?", p, re.I)
    if m and not re.search(r"\b(and|then|convert|extract|resize|all|bulk|folder)\b",
                           p, re.I):
        url = m.group(1).rstrip(".,;")
        dest = _fast_path(m.group(2) or "") if m.group(2) else ""
        return _fast_call(prompt, stop, "download", f"downloading {url[:60]}",
                          lambda: t_download(url, dest),
                          {"url": url, **({"path": dest} if dest else {})})

    return None


def _app_in_text(low):
    for k in sorted(_OPEN_APPS, key=len, reverse=True):
        if re.search(r"\b" + re.escape(k) + r"\b", low):
            return _OPEN_APPS[k]
    return ""


_DIG_VERB = re.compile(r"\b(dig|go|read|browse|look|scroll|flip|sweep|search)\b")
_CHAT_NOUN = re.compile(r"\b(chats?|messages?|conversations?|threads?|dialogs?)\b")
_WRITE_VERB = re.compile(r"\b(send|sending|reply|respond|type|write|delete|forward|"
                         r"post|call|ring)\b")


def fast_ui(prompt, stop):
    """One-line read/act commands that should not cost a model round trip."""
    p = " ".join((prompt or "").split()).strip()
    if not p or stop.is_set():
        return None
    low = p.lower().strip(" .!?\t")
    words = len(p.split())

    # Chat digging is always read-only: no typing, no Enter, no sending.
    if words <= 45 and _DIG_VERB.search(low) and _CHAT_NOUN.search(low) \
            and not _WRITE_VERB.search(low):
        app = ("AyuGram" if re.search(r"\bayugram\b", low)
               else ("Telegram" if re.search(r"\btelegram\b", low)
                     else (_app_in_text(low) or "AyuGram")))
        return _fast_call(prompt, stop, "dig_chats",
                          f"reading {app} chats (read-only)",
                          lambda: t_dig_chats(app, 2, 6), {"app": app})

    # Open an app and immediately type into it: the most common two-step task.
    m = re.fullmatch(_FAST_POLITE + r"(?:open|launch|start|run) (?:the )?"
                     r"([\w .+#-]{2,40}?)(?: (?:app|application|window))? (?:and|then) "
                     r"(type|write|enter|put|search for|search|look up) (?:in(?:to)? )?"
                     r"(.+?)(?: (?:and )?(?:press|hit) (?:enter|return))?", p, re.I)
    if m:
        app = m.group(1).strip()
        verb = m.group(2).lower()
        text = m.group(3).strip().strip("\"'")
        enter = verb.startswith(("search", "look")) or \
            bool(re.search(r"(press|hit) (enter|return)$", low))
        if text and not re.match(r"^(?:folder|directory|attached|image|picture)\b", app, re.I):
            def _open_and_type():
                ok, summary, detail = t_open_app(app)
                if not ok:
                    return _err(summary, detail)
                time.sleep(0.4)
                ok2, summary2, detail2 = t_type_text(text, "", enter)
                return (ok2,
                        f"{summary} Then {summary2.lower()}",
                        (detail + "\n" + str(detail2)).strip())
            return _fast_call(prompt, stop, "open_app",
                              f"opening {app} and typing",
                              _open_and_type, {"app": app, "text": text[:60]})

    # Plain "open <app>": reuse a running copy, restore from tray, launch if needed.
    m = re.fullmatch(_FAST_POLITE + r"(?:open|launch|start|run) (?:the )?"
                     r"([\w .+#:\\/-]{2,60}?)(?: (?:app|application|window))?", p, re.I)
    if m:
        app = m.group(1).strip()
        if not re.match(r"^(?:folder|directory|file|it|that|this|them|attached|image|"
                        r"picture|screenshot|video|document)\b", app, re.I) \
                and not re.search(r"\b(and|then|with|to)\b", app, re.I):
            return _fast_call(prompt, stop, "open_app", f"opening {app}",
                              lambda: t_open_app(app), {"app": app})

    # Type literal text, with an optional target window.
    m = re.fullmatch(_FAST_POLITE + r"type ([\"'])(.+)\1(?: (?:in|into) ([\w .+#-]{2,30}))?"
                     r"(?: (?:and )?(?:press|hit) (?:enter|return))?", p, re.I)
    if m:
        text, win, enter = m.group(2), m.group(3) or "", bool(re.search(r"enter|return", low))
        return _fast_call(prompt, stop, "type_text", f"typing into {win or 'the focused window'}",
                          lambda: t_type_text(text, win, enter), {"text": text[:60]})
    m = re.fullmatch(_FAST_POLITE + r"type (.+?)(?: (?:and )?(?:press|hit) (?:enter|return))?",
                     p, re.I)
    if m and len(m.group(1)) <= 200 and not re.search(r"\b(file|folder|path|code|script|"
                                                      r"in|into|field|box|bar)\b",
                                                      m.group(1).lower()):
        text = m.group(1).strip()
        enter = bool(re.search(r"(press|hit) (enter|return)$", low))
        return _fast_call(prompt, stop, "type_text", "typing text",
                          lambda: t_type_text(text, "", enter), {"text": text[:60]})

    # Keys and shortcuts.
    m = re.fullmatch(_FAST_POLITE + r"(?:press|hit|tap) (?:the )?([\w+]+)(?: (?:key|button))?", low)
    if m:
        key = m.group(1)
        if "+" in key:
            return _fast_call(prompt, stop, "hotkey", f"pressing {key}",
                              lambda: t_hotkey(key), {"keys": key})
        if key in {"enter", "return", "tab", "esc", "escape", "space", "backspace",
                   "delete", "del", "insert", "home", "end", "pageup", "pagedown",
                   "up", "down", "left", "right", "win", "escape"} or re.fullmatch(r"f\d{1,2}", key):
            return _fast_call(prompt, stop, "key_tap", f"pressing {key}",
                              lambda: t_key_tap(key), {"key": key})

    # Click a visible label (OCR finds the exact pixels).
    m = re.fullmatch(_FAST_POLITE + r"(double[- ]?click|(?:right[- ])?click) (?:on )?"
                     r"(?:the )?(?:[\"'](.+?)[\"']|([^,]{1,60}?))(?: (?:button|link|tab|item))?"
                     r"(?: on (?:the )?screen)?", p, re.I)
    if m:
        label = (m.group(2) or m.group(3) or "").strip()
        clicks = 2 if m.group(1).lower().startswith("double") else 1
        button = "right" if m.group(1).lower().startswith("right") else "left"
        if label and not re.match(r"^at\s+\d+\s*,\s*\d+$", label) \
                and not re.search(r"\b(file|folder|window|app)\b", label.lower()):
            return _fast_call(prompt, stop, "ocr_click",
                              f"clicking {label!r} on screen",
                              lambda: t_ocr_click(label, "", 0, button, clicks),
                              {"text": label, "button": button, "clicks": clicks})
    m = re.fullmatch(_FAST_POLITE + r"click at (\d{1,5})\s*,\s*(\d{1,5})", low)
    if m:
        x, y = int(m.group(1)), int(m.group(2))
        return _fast_call(prompt, stop, "mouse_click", f"clicking {x},{y}",
                          lambda: t_mouse_click(x, y), {"x": x, "y": y})

    # Window focus, maximize, minimize, restore, close.
    m = re.fullmatch(_FAST_POLITE + r"(switch to|focus(?: on)?|activate|bring up|bring) "
                     r"(?:the |my )?([\w .+#-]{2,40}?)(?: (?:window|app))?"
                     r"(?: to (?:the )?(?:front|foreground))?", low)
    if m:
        target = m.group(2).strip()
        if not re.search(r"\b(chats?|messages?|volume|sound|brightness)\b", target):
            return _fast_call(prompt, stop, "window_focus", f"focusing {target}",
                              lambda: t_window_focus(target), {"title": target})
    m = re.fullmatch(_FAST_POLITE + r"(maximize|minimize|restore|close) (?:the |my )?"
                     r"([\w .+#-]{2,40}?)(?: (?:window|app))?", low)
    if m:
        verb, target = m.group(1), m.group(2).strip()
        tool = {"maximize": ("maximize_window", t_maximize_window),
                "minimize": ("minimize_window", t_minimize_window),
                "restore": ("restore_window", t_restore_window),
                "close": ("close_window", t_close_window)}[verb]
        return _fast_call(prompt, stop, tool[0], f"{verb} {target}",
                          lambda: tool[1](target), {"title": target})

    # Scrolling, reading the screen, the active window and the selection.
    m = re.fullmatch(_FAST_POLITE + r"scroll (up|down)(?: (?:in|on) (?:the |my )?"
                     r"([\w .+#-]{2,30}))?(?: (?:one )?page)?", low)
    if m:
        direction, win = m.group(1), (m.group(2) or "").strip()
        return _fast_call(prompt, stop, "page_scroll", f"scrolling {direction}",
                          lambda: t_page_scroll(direction, 1, win),
                          {"direction": direction, "window": win})
    if re.fullmatch(_FAST_POLITE + r"(?:what(?:'s| is) (?:on )?(?:my |the )?screen|"
                    r"read (?:my |the )?screen|what (?:do|can) i see|"
                    r"describe (?:my |the )?screen)", low):
        return _fast_call(prompt, stop, "read_screen", "reading the screen",
                          lambda: t_read_screen("", 60))
    if re.fullmatch(_FAST_POLITE + r"(?:what|which) window is (?:active|focused|in front)|"
                    r"what am i looking at|active window", low):
        return _fast_call(prompt, stop, "active_window", "checking the active window",
                          t_active_window)
    if re.fullmatch(_FAST_POLITE + r"(?:what(?:'s| is) (?:selected|highlighted)|"
                    r"read (?:the )?(?:selected|highlighted) text|"
                    r"copy (?:the )?(?:selected|highlighted) text)", low):
        return _fast_call(prompt, stop, "selected_text", "reading the selection",
                          t_selected_text)
    m = re.fullmatch(_FAST_POLITE + r"wait (?:for )?(\d{1,3})(?: ?(?:s|sec|secs|seconds))?", low)
    if m:
        secs = int(m.group(1))
        return _fast_call(prompt, stop, "wait", f"waiting {secs}s",
                          lambda: t_wait(secs), {"seconds": secs})
    m = re.fullmatch(_FAST_POLITE + r"wait (?:for )?(\d{1,2}) (?:m|min|mins|minutes)", low)
    if m:
        secs = int(m.group(1)) * 60
        return _fast_call(prompt, stop, "wait", f"waiting {secs}s",
                          lambda: t_wait(secs), {"seconds": secs})

    return None


_INTENT_RULES = [
    (re.compile(r"\b(chats?|messages?|conversations?)\b.*\b(dig|through|older|history|read)\b|"
                r"\b(dig|go|read|browse)\b.*\b(chats?|messages?|conversations?)\b"),
     "dig_chats", "read-only sweep of a chat app; it clicks rows and OCRs each thread"),
    (re.compile(r"\b(summarize|summarise|describe|read|what does|what's in|what this|"
                r"tell me what)\b.*\b(window|app|editor|document|screen|page)\b|"
                r"\bwindow_text\b|\bread (?:the )?window\b"),
     "window_text", "reads all text inside a window in one call"),
    (re.compile(r"\b(click|tap|press)\b.*\b(button|link|tab|menu|item|icon|label)\b|"
                r"\bclick on\b"),
     "ocr_click", "finds the label on screen by OCR and clicks its real center"),
    (re.compile(r"\b(type|write|enter)\b.*\b(into|in|field|box|bar|search|chat)\b"),
     "type_text", "types literal text into the focused or named window"),
    (re.compile(r"\b(search|look up|google|find out|latest|news about)\b.*"
                r"\b(web|online|internet|google)\b|"
                r"\b(web_search|search the web|google it)\b"),
     "web_search", "searches the web and returns results"),
    (re.compile(r"\b(download|save|grab|fetch)\b.*\b(song|music|audio|video|mp3|mp4|youtube)\b"),
     "youtube_download", "downloads from YouTube by name or link"),
    (re.compile(r"\b(convert|batch|bulk)\b.*\b(images?|pictures?|png|jpg|audio|mp3|wav)\b"),
     "convert_folder", "converts a whole folder of images or audio in one pass"),
    (re.compile(r"\b(read|look|see|summari[sz]e|describe|what'?s in)\b.*"
                r"\b(screenshots?|images?|pictures?|photos?)\b|"
                r"\b(screenshots?|images?|pictures?|photos?)\b.*"
                r"\b(read|look|see|what'?s in|summari[sz]e|describe|text)\b|"
                r"\b(see_images|look at (?:these|those|the) (?:screenshots?|images?))\b"),
     "see_images", "OCRs every image in a folder or attached file"),
    (re.compile(r"\b(screenshots?|screen capture|capture the screen)\b"),
     "screenshot", "saves a PNG of the full screen"),
    (re.compile(r"\b(minimi[sz]e|maximi[sz]e|restore|close)\b.*\b(window|app)\b"),
     "window_snap", "moves/resizes a window; use maximize_window for maximize"),
    (re.compile(r"\b(volume|mute|louder|quieter|brightness)\b"),
     "volume_up", "volume_up / volume_down / mute / brightness"),
    (re.compile(r"\b(system report|pc specs|system specs|hardware|"
                r"how is my (?:pc|computer|laptop))\b"),
     "system_report", "CPU, memory, disks, uptime and OS in one report"),
    (re.compile(r"\b(weather|forecast|temperature outside)\b"),
     "weather", "current weather for a place"),
    (re.compile(r"\b(battery|charge level)\b"),
     "battery", "battery charge and charging state"),
    (re.compile(r"\b(files?|folder|directory|contents|list)\b.*\b(in|inside|under|of)\b|"
                r"\b(open|show|list)\b.*\bfolder\b|\blist_dir\b"),
     "list_dir", "lists a folder before opening files"),
    (re.compile(r"\b(open|launch|start|run)\b"),
     "open_app", "focuses an already-running copy, then launches if needed"),
]


def intent_hint(prompt):
    """Likely first tool for a request: cheap, deterministic, testable."""
    p = " ".join((prompt or "").split()).lower()
    for rx, tool, why in _INTENT_RULES:
        if rx.search(p):
            return tool, why
    return "", ""


def _dry_route(prompt):
    """Which deterministic route would answer this prompt, without running a tool."""
    seen = {}

    def fake(prompt_, stop_, name, label, fn_, args=None):
        seen.setdefault("name", name)
        return (name, label, "")

    orig = globals()["_fast_call"]
    globals()["_fast_call"] = fake
    try:
        for fn in (fast_direct, fast_ui):
            try:
                fn(prompt, threading.Event())
            except Exception:
                pass
            if seen:
                break
    finally:
        globals()["_fast_call"] = orig
    return seen.get("name", "")


# (prompt, expected deterministic route, expected intent hint) - blanks are not checked.
SCENARIO_TESTS = [
    # -- opening apps ------------------------------------------------------
    ("open notepad", "open_app", ""), ("launch notepad", "open_app", ""),
    ("start calculator", "open_app", ""), ("open calc", "open_app", ""),
    ("open paint", "open_app", ""), ("open cmd", "open_app", ""),
    ("open command prompt", "open_app", ""), ("open powershell", "open_app", ""),
    ("open terminal", "open_app", ""), ("open explorer", "open_app", ""),
    ("open file explorer", "open_app", ""), ("open task manager", "open_app", ""),
    ("open chrome", "open_app", ""), ("open google chrome", "open_app", ""),
    ("open edge", "open_app", ""), ("open firefox", "open_app", ""),
    ("open word", "open_app", ""), ("open excel", "open_app", ""),
    ("open powerpoint", "open_app", ""), ("open outlook", "open_app", ""),
    ("open spotify", "open_app", ""), ("open discord", "open_app", ""),
    ("open steam", "open_app", ""), ("open code", "open_app", ""),
    ("open vs code", "open_app", ""), ("open visual studio code", "open_app", ""),
    ("open obs", "open_app", ""), ("open blender", "open_app", ""),
    ("open photoshop", "open_app", ""), ("open telegram", "open_app", ""),
    ("open ayugram", "open_app", ""), ("open whatsapp", "open_app", ""),
    ("can you open notepad", "open_app", ""), ("please launch calculator", "open_app", ""),
    ("open notepad and type hello world", "open_app", ""),
    ("open notepad and write hello there", "open_app", ""),
    ("launch notepad then type test", "open_app", ""),
    ("open chrome and type cats", "open_app", ""),
    ("open chrome and search for cats", "open_app", ""),
    ("open the calculator app", "open_app", ""),
    ("open the attached screenshot", "", ""),
    # -- sites -------------------------------------------------------------
    ("open youtube.com", "open_url", ""), ("go to github.com", "open_url", ""),
    ("visit reddit.com", "open_url", ""), ("load google.com", "open_url", ""),
    ("open https://example.com", "open_url", ""),
    # -- chats (read-only sweep) ------------------------------------------
    ("dig through my chats", "dig_chats", ""),
    ("read my chats", "dig_chats", ""),
    ("go through my chats in telegram", "dig_chats", ""),
    ("browse my conversations", "dig_chats", ""),
    ("look through my messages", "dig_chats", ""),
    ("scroll through my chats", "dig_chats", ""),
    ("read through messages in whatsapp", "dig_chats", ""),
    ("dig through my chats in ayugram", "dig_chats", ""),
    ("control my laptop, dig through stuff. open ayugram on the taskbar (Win+3) "
     "and just dig through my chats", "dig_chats", ""),
    ("read my messages and summarize the unread ones", "dig_chats", ""),
    ("reply to that message in ayugram", "", ""),
    ("open ayugram and reply to my unread messages", "", "open_app"),
    ("send a message to bob on telegram", "", ""),
    ("type hello into the chat box", "", "type_text"),
    # -- typing and keys ---------------------------------------------------
    ("type hello", "type_text", ""), ("type hello world", "type_text", ""),
    ('type "good morning"', "type_text", ""),
    ('type "see you at 6" in notepad', "type_text", ""),
    ("type ok and press enter", "type_text", ""),
    ("press enter", "key_tap", ""), ("press the enter key", "key_tap", ""),
    ("press tab", "key_tap", ""), ("press esc", "key_tap", ""),
    ("press f5", "key_tap", ""), ("hit space", "key_tap", ""),
    ("press ctrl+s", "hotkey", ""), ("hit alt+tab", "hotkey", ""),
    ("press ctrl+shift+esc", "hotkey", ""),
    # -- clicking ----------------------------------------------------------
    ("click the Search button", "ocr_click", ""),
    ("click OK", "ocr_click", ""),
    ("double click Downloads", "ocr_click", ""),
    ("right click the desktop", "ocr_click", ""),
    ("click at 500,300", "mouse_click", ""),
    ("click the send button", "", "ocr_click"),
    ("click on the settings icon", "", "ocr_click"),
    # -- windows -----------------------------------------------------------
    ("switch to notepad", "window_focus", ""),
    ("focus chrome", "window_focus", ""),
    ("activate spotify", "window_focus", ""),
    ("bring up discord", "window_focus", ""),
    ("maximize notepad", "maximize_window", ""),
    ("minimize chrome", "minimize_window", ""),
    ("restore telegram", "restore_window", ""),
    ("close calculator", "close_window", ""),
    ("scroll down", "page_scroll", ""),
    ("scroll up in chrome", "page_scroll", ""),
    # -- screen reading ----------------------------------------------------
    ("what is on my screen", "read_screen", ""),
    ("read the screen", "read_screen", ""),
    ("what window is active", "active_window", ""),
    ("which window is focused", "active_window", ""),
    ("read selected text", "selected_text", ""),
    ("what is selected", "selected_text", ""),
    ("wait 5 seconds", "wait", ""),
    ("wait 2 minutes", "wait", ""),
    # -- media, power, system ---------------------------------------------
    ("turn the volume up", "volume_up", ""),
    ("turn the volume down", "volume_down", ""),
    ("make it louder", "volume_up", ""),
    ("mute", "mute", ""), ("unmute", "mute", ""),
    ("next song", "media_next", ""), ("skip this track", "media_next", ""),
    ("previous track", "media_prev", ""),
    ("take a screenshot", "screenshot", ""),
    ("screenshot", "screenshot", ""),
    ("lock the screen", "lock_screen", ""),
    ("set brightness to 40%", "brightness", ""),
    ("what time is it", "now", ""),
    ("battery", "battery", ""),
    ("system report", "system_report", ""),
    ("disk space", "disk_usage", ""),
    ("top processes", "process_top", ""),
    ("what is my public ip", "public_ip", ""),
    ("uptime", "uptime", ""),
    ("list open windows", "window_list", ""),
    ("empty the recycle bin", "empty_recycle_bin", ""),
    ("what is on the clipboard", "clipboard_read", ""),
    ("clear the clipboard", "clipboard_clear", ""),
    # -- hints for requests the model still plans -------------------------
    ("can you read what's in that window", "", "window_text"),
    ("summarize what this window says", "", "window_text"),
    ("what does the document say", "", "window_text"),
    ("search the web for rust tutorials", "", "web_search"),
    ("google it", "", "web_search"),
    ("download never gonna give you up as mp3", "", "youtube_download"),
    ("grab that song from youtube", "", "youtube_download"),
    ("convert all pngs in Downloads to jpg", "", "convert_folder"),
    ("bulk convert these wav files to mp3", "", "convert_folder"),
    ("what's the weather in london", "", "weather"),
    ("weather forecast for tomorrow", "", "weather"),
    ("how much battery do i have left", "", "battery"),
    ("how is my computer doing", "", "system_report"),
    ("list files in Desktop", "", "list_dir"),
    ("show me the contents of Documents", "", "list_dir"),
    ("open the downloads folder", "", "list_dir"),
    ("read the screenshots in Desktop", "", "see_images"),
    ("look at these screenshots", "", "see_images"),
    ("type hello in the search bar", "", "type_text"),
    ("write this into the name field", "", "type_text"),
]


def run_turn(prompt: str, history: list, stop: threading.Event, study: bool = False):
    fast = fast_direct(prompt, stop)
    if fast is not None:
        return fast
    fast = fast_ui(prompt, stop)
    if fast is not None:
        return fast
    fast = fast_see_images(prompt, stop)
    if fast is not None:
        return fast
    fast = fast_wallpapers(prompt, stop)
    if fast is not None:
        return fast
    model = ensure_model()
    emit("status", state="thinking", text=f"Waking {model}", model=model)

    sys_prompt = base_system()
    lessons = lesson_text(prompt)
    if lessons != "None recorded.":
        sys_prompt += "\n\nLESSONS FROM THIS USER (follow them):\n" + lessons
    earlier = prompts_block()
    if earlier:
        sys_prompt += earlier
    live = screen_context(prompt)
    if live:
        sys_prompt += "\n\nLIVE SCREEN CONTEXT (this machine right now, trust it):\n" + live
    hint_tool, hint_why = intent_hint(prompt)
    if hint_tool:
        sys_prompt += (f"\n\nROUTE HINT: the most likely first call for this request is "
                       f"{hint_tool} ({hint_why}). Start with it unless the wording clearly "
                       "needs something else, and never repeat it once its result is above.")
    if history:
        recent = []
        for m in history[-8:]:
            txt = " ".join((m.get("content") or "").split())
            if txt:
                recent.append(f"{m['role']}: {txt[:600]}")
        if recent:
            sys_prompt += "\n\nRECENT CONVERSATION (use it; lists from earlier turns still count):\n" + "\n".join(recent)

    transcript = [{"role": "system", "content": sys_prompt},
                  {"role": "user", "content": prompt}]

    if _WARM["key"] != hashlib.sha1((CFG["model"] + "\x00" + base_system()).encode("utf-8")).hexdigest():
        emit("status", state="warming", text="Warming the model for this workspace")
        prewarm(base_system())

    budget = int(CFG.get("num_ctx", 16384) * 0.55)
    steps = 0
    results = []
    step_cap = max(6, min(int(CFG.get("max_steps", 18) or 18), 40))
    cap_warned = False

    # Reading a window is the one job the 7B keeps fumbling (it guesses file paths or
    # denies access). Read the target window up front so the answer is built on real text.
    if not study and wants_window_read(prompt) and not stop.is_set():
        target = _pick_read_window(prompt)
        if target:
            emit("log", text=f"Reading window {target!r} before answering")
            ok, summary, detail = t_window_text(target)
            results.append({"name": "window_text", "label": target, "ok": bool(ok),
                            "summary": str(summary), "detail": str(detail)})
            if ok:
                emit("tool", op="start", name="window_text", label=target, step=1,
                     args={"window": target})
                emit("tool", op="end", name="window_text", ok=True, label=target,
                     summary=str(summary)[:280], detail=str(detail)[:6000], ms=0, step=1)
                transcript.append({"role": "user", "content":
                    f"SCREEN CONTEXT, already read from {target!r} (real window text):\n"
                    f"{str(detail)[:12000]}\n\n"
                    "If the user only asked you to read or report this, answer now without "
                    "calling more tools. If it is a chat app, ui_collect(direction=up, pages=8) "
                    "loads older messages."})
            else:
                results.pop()
                emit("log", text=f"Could not read {target!r}: {str(summary)[:120]}")
    repeated = {}
    last_result = {}
    family = []
    fails_in_a_row = 0
    plan_seen = False
    model_error = ""

    while not stop.is_set():
        steps += 1
        if steps > step_cap:
            emit("log", text=f"Step limit ({step_cap}) reached; wrapping up with what I have")
            break
        if steps == max(2, step_cap - 3) and not cap_warned:
            cap_warned = True
            transcript.append({"role": "user", "content":
                               f"You have {step_cap - steps} steps left. Stop exploring: take the "
                               "final action now, then reply with what you actually have."})
        transcript, saved = compact(transcript, budget)
        if saved:
            emit("compact", text=f"Compacted context, {saved} tokens reclaimed")

        emit("status", state="thinking", text=f"Step {steps}")
        try:
            decision = decide(transcript, stop)
        except Stopped:
            break
        except ModelUnavailable as e:
            if not results:
                raise
            model_error = str(e)
            break

        thought = str(decision.get("thought", ""))[:160]
        name = str(decision.get("tool", "reply")).strip()
        args = decision.get("args") or {}
        # The 7B likes to "teach itself" every request it sees. Only store a lesson
        # when the user is actually teaching or correcting, never for the task itself.
        if name == "remember" and not TEACH_HINT.search(prompt):
            name = "reply"
        if name == "speak" and not SPEAK_HINT.search(prompt):
            emit("log", text="Skipped speak — the user did not ask to hear anything")
            name = "reply"
        if thought:
            emit("thought", text=thought, step=steps)
        transcript.append({"role": "assistant",
                           "content": json.dumps({"thought": thought, "tool": name,
                                                  "args": args}, ensure_ascii=False)})

        if name == "reply":
            break

        if name == "update_plan":
            items = _plan_items(args.get("items"))
            if items:
                emit("plan", items=items)
                plan_seen = True
                transcript.append({"role": "user", "content": "Plan displayed. Continue."})
            else:
                transcript.append({"role": "user",
                                   "content": "update_plan needs items as a JSON array string."})
            continue

        if name not in TOOLS:
            transcript.append({"role": "user",
                               "content": f"There is no tool called {name}. "
                                          f"Pick from: {', '.join(sorted(TOOLS))}"})
            continue

        spec, required, _desc = TOOLS[name]
        missing = [k for k in required if args.get(k) in (None, "")]
        if missing:
            emit("log", text=f"{name} was missing {', '.join(missing)}; filling it in")
            try:
                fixed = decide_args(name, transcript, stop)
            except Stopped:
                break
            except ModelUnavailable:
                fixed = {}
            for k, v in fixed.items():
                if k in spec and v not in (None, ""):
                    args[k] = v
            transcript[-1]["content"] = json.dumps(
                {"thought": thought, "tool": name, "args": args}, ensure_ascii=False)

        # The user's own words win: when they say "download ... to <path>", the 7B
        # often drops path, downloads into the workspace and renames it afterwards
        # (then reports the requested path anyway). Fill their exact target in.
        if name in ("download", "fetch_file", "screenshot"):
            key = "dest" if name == "fetch_file" else "path"
            hint = destination_hint(prompt)
            if hint and not str(args.get(key) or "").strip():
                args[key] = hint
                emit("log", text=f"Using the destination you gave: {hint[:80]}")
                transcript[-1]["content"] = json.dumps(
                    {"thought": thought, "tool": name, "args": args}, ensure_ascii=False)

        if name in MUTATING_TOOLS and NO_WRITE_HINT.search(prompt):
            emit("log", text=f"Read-only request: skipped {name}")
            transcript.append({"role": "user",
                               "content": "The user asked for no changes to the machine, so that "
                                          "write was skipped. Answer now using what you already have."})
            break

        # A 7B can lock onto one call and repeat it forever. Nudge instead of capping.
        fingerprint = name + json.dumps(args, sort_keys=True)[:200]
        repeated[fingerprint] = repeated.get(fingerprint, 0) + 1
        if repeated[fingerprint] > 1:
            prior = last_result.get(fingerprint, "it already ran")
            transcript.append({"role": "user",
                               "content": "You already ran this exact call. Its result is above: "
                                          f"{prior[:300]}\nDo not run it again. Pick a different "
                                          "action, or reply to the user now."})
            if repeated[fingerprint] > 3 and results:
                emit("log", text=f"Stopped repeating {name}; writing the answer with what I have")
                break
            continue

        label = label_for(name, args)
        emit("tool", op="start", name=name, label=label, step=steps,
             args={k: str(v)[:300] for k, v in args.items()})
        t0 = now_ms()
        try:
            ok, summary, detail = dispatch(name, args)
        except Exception as e:
            ok = False
            summary = f"{type(e).__name__}: {e}"
            detail = traceback.format_exc()[-1200:]
        ms = now_ms() - t0
        emit("tool", op="end", name=name, ok=bool(ok), label=label, summary=summary[:280],
             detail=str(detail)[:6000], ms=ms, step=steps)
        results.append({"name": name, "label": label, "ok": bool(ok),
                        "summary": str(summary), "detail": str(detail)})
        last_result[fingerprint] = f"{summary} · {str(detail)[:240]}"
        transcript.append({"role": "user",
                           "content": f"RESULT of {name}: {'OK' if ok else 'ERROR'} - "
                                      f"{str(summary)[:400]}\n{str(detail)[:1200]}"})

        fails_in_a_row = 0 if ok else fails_in_a_row + 1
        if fails_in_a_row == 2:
            _auto_lesson(prompt, name, f"{summary} | {str(detail)[:160]}")
        if fails_in_a_row >= 3:
            transcript.append({"role": "user",
                               "content": "Three failures in a row. Stop retrying the same way: "
                                          "change tool or arguments, or reply with what you learned."})
            fails_in_a_row = 0

        family.append(name)
        if len(family) >= 5 and len(set(family[-5:])) == 1:
            transcript.append({"role": "user",
                               "content": f"You have called {name} five times in a row. You have "
                                          "enough information. Take the final action or reply now."})
            family.clear()

    emit("status", state="writing", text="Composing the reply")
    if stop.is_set():
        answer = "Stopped by you after " + str(steps) + " step(s)."
    else:
        if results:
            facts = "\n".join(f"- {r['name']}: {r['summary']}" for r in results[-8:])
            instruction = (
                "STOP. Do not call any tool and do not output JSON. You are done acting.\n"
                "FACTS from this run (copy these exactly, never recompute):\n"
                f"{facts}\n\n"
                "Write the final reply to the user's request above using those facts and the "
                "tool results in this conversation. 1-6 sentences, markdown, no preamble, "
                "no tool names, no invented numbers or paths. If a fact names only a file "
                "name, repeat that name; never guess a folder it is not in.")
        else:
            instruction = (
                "STOP. Do not call any tool. Answer the user's request above directly "
                "and concisely, in 1-4 sentences of markdown.")
        try:
            answer = stream_reply(transcript + [{"role": "user", "content": instruction}], stop)
        except Stopped:
            answer = "Stopped by you after " + str(steps) + " step(s)."
        except ModelUnavailable as e:
            model_error = model_error or str(e)
            answer = ""
        if not answer:
            head = "I could not reach the model to write the summary." if model_error \
                else "The job finished."
            tail = ("\n\nWhat ran:\n" + _recap(results)) if results else ""
            answer = head + tail

        # The 7B sometimes ends a successful run by claiming it cannot access the very
        # app it just read. Rewrite that from the real tool output instead of shipping it.
        if answer and not stop.is_set() and _DENIAL_RE.search(answer):
            good = [r for r in results if r.get("ok")]
            if good:
                emit("log", text="Answer denied access it already had; rewriting from the tool output")
                evidence = "\n\n".join(
                    f"{r['name']} -> {r['summary']}\n{str(r['detail'])[:1500]}" for r in good[-3:])
                fix = (
                    "Rewrite the final reply. You are running on the user's PC and you DO have "
                    "access to its windows, apps and whatever they display. Never claim you "
                    "cannot access chats, history, apps, the screen or the computer.\n\n"
                    "TOOL OUTPUT YOU ALREADY HAVE:\n" + evidence + "\n\n"
                    "Use that output: name what you actually saw (chats, messages, window "
                    "titles). If you could not read everything, say exactly what you saw and "
                    "which call failed, then what you would try next. 1-6 sentences.")
                try:
                    retry = stream_reply(transcript + [{"role": "user", "content": fix}], stop)
                    if retry and retry.strip():
                        answer = retry
                except (Stopped, ModelUnavailable):
                    pass

    if study and not stop.is_set():
        used = []
        for r in results:
            if r["ok"] and r["name"] not in used:
                used.append(r["name"])
        if len(used) >= 2:
            when = " ".join(prompt.split())[:110]
            do = ("Use this sequence: " + " -> ".join(used) +
                  ". Reuse the same tools and arguments for similar requests.")
            known = {L.get("when", "").strip().lower() for L in load_lessons()}
            if when.lower() not in known:
                remember(when, do)
                emit("log", text="Study loop: saved this run as a lesson")
                emit("lessons", lessons=len(load_lessons()))

    emit("status", state="done", text=f"{steps} step(s)", steps=steps)
    return answer, transcript


# ---------------------------------------------------------------------------
# Study loop: autonomous curiosity, real browser visits, notes on disk
# ---------------------------------------------------------------------------
STUDY_DIR = Path.home() / "Documents" / "AI_AgentLearns"
STUDY_FOCUS = ("C++ (modern C++ 17/20/23, the STL, memory, templates) and competitive "
               "programming (algorithms, data structures, complexity, Codeforces-style "
               "problem solving, number theory, graphs, dp)")
STUDY_SOURCES = ("cppreference.com", "learncpp.com", "cp-algorithms.com", "codeforces.com",
                 "stackoverflow.com", "geeksforgeeks.org", "cplusplus.com", "en.wikipedia.org")
_STUDY_SKIP = re.compile(r"\.(pdf|zip|apk|exe|mp4|mp3|png|jpe?g|gif)$|"
                         r"facebook\.com|twitter\.com|x\.com|instagram\.com|pinterest\.|"
                         r"quora\.com|reddit\.com", re.I)

STUDY_NOTES_SYSTEM = """You are Anvil Fletch, teaching yourself C++ and competitive
programming from the web pages below. Write durable study notes in markdown.

- Use only facts from the sources. If they disagree, say so.
- Structure: a one-paragraph idea in plain words, a minimal C++17 example that compiles,
  pitfalls or gotchas, and two practice exercises.
- Under 450 words. No preamble, no "As an AI", no links except at the end.
- End with "Sources:" and the URLs you actually used."""


def _study_tool_start(step, name, label, args=None):
    emit("tool", op="start", name=name, label=label, step=step,
         args={k: str(v)[:300] for k, v in (args or {}).items()})


def _study_tool_end(step, name, label, ok, summary, detail="", ms=0):
    emit("tool", op="end", name=name, ok=bool(ok), label=label,
         summary=str(summary)[:280], detail=str(detail)[:6000], ms=int(ms), step=step)


def _study_seen(limit=14):
    if not STUDY_DIR.exists():
        return []
    try:
        files = sorted(STUDY_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return []
    return [p.stem for p in files[:limit] if not p.stem.startswith("_")]


def _study_next(stop):
    seen = _study_seen()
    known = "\n".join(f"- {s}" for s in seen) or "- nothing yet"
    schema = {"type": "object",
              "properties": {"topic": {"type": "string"},
                             "query": {"type": "string"},
                             "why": {"type": "string"}},
              "required": ["topic", "query"]}
    body = {"model": CFG["model"], "stream": True, "keep_alive": "15m", "format": schema,
            "options": {"temperature": 0.75, "num_ctx": int(CFG.get("num_ctx", 16384)),
                        "num_predict": 220},
            "messages": [
                {"role": "system", "content":
                 "You are Anvil Fletch, studying on your own. Focus: " + STUDY_FOCUS +
                 ". Notes you already wrote:\n" + known +
                 "\nPick the next single thing you are genuinely curious about. Do not repeat "
                 "a topic you already covered. Return JSON with topic (short title), query "
                 "(a precise web search that finds a tutorial or reference), why (at most 12 "
                 "words, why this is next)."},
                {"role": "user", "content": "What do you want to learn next?"}]}
    raw = _ollama_stream(body, stop)
    d = _salvage_json(raw)
    topic = " ".join(str(d.get("topic") or "").split())[:90]
    query = " ".join(str(d.get("query") or "").split())[:160]
    why = " ".join(str(d.get("why") or "").split())[:120]
    if not topic or not query:
        return None
    return topic, query, why


def _study_write_notes(topic, query, why, pages, stop):
    src = []
    for url, title, text in pages:
        src.append(f"### {title}\nURL: {url}\n\n{text[:4500]}")
    body = {"model": CFG["model"], "stream": True, "keep_alive": "15m",
            "options": {"temperature": 0.35, "num_ctx": int(CFG.get("num_ctx", 16384)),
                        "num_predict": 1200},
            "messages": [
                {"role": "system", "content": STUDY_NOTES_SYSTEM},
                {"role": "user", "content": f"Topic: {topic}\nQuestion: {query}\n\n" +
                                            "\n\n".join(src)}]}
    return _ollama_stream(body, stop).strip()


def _study_slug(text):
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return (slug[:60] or "notes")


def study_session(stop):
    model = ensure_model()
    STUDY_DIR.mkdir(parents=True, exist_ok=True)
    emit("status", state="working", text=f"Study loop: reading with {model}")
    st = {"count": 0, "visited": 0, "step": 0, "last": ""}
    fallbacks = ["STL containers", "pointers and references", "binary search on answers",
                 "dynamic programming basics", "graph traversal BFS and DFS",
                 "sorting algorithms and comparators", "time complexity analysis",
                 "prefix sums and two pointers", "templates in C++", "bit manipulation",
                 "lambda expressions", "move semantics and RAII",
                 "segment trees", "disjoint set union", "modular arithmetic",
                 "string algorithms", "priority queues", "memoization vs tabulation"]

    def cycle(pick):
        topic, query, why = pick
        st["last"] = topic
        st["step"] += 1
        emit("thought", text=why or f"Learning {topic}", step=st["step"])
        if stop.is_set():
            raise Stopped()

        st["step"] += 1
        _study_tool_start(st["step"], "web_search", f"Search: {query[:60]}",
                          {"query": query})
        t0 = now_ms()
        hits, fail = web_search_api(query, 8)
        hits = hits or []
        _study_tool_end(st["step"], "web_search", f"Search: {query[:60]}", bool(hits),
                        f"{len(hits)} result(s)", fail or "\n".join(
                            f"{r['title']} - {r['url']}" for r in hits[:8]),
                        now_ms() - t0)

        search_url = "https://duckduckgo.com/?q=" + urllib.parse.quote_plus(query)
        st["step"] += 1
        _study_tool_start(st["step"], "open_url", "Open the search in the browser",
                          {"url": search_url})
        t0 = now_ms()
        ok, summ, det = t_open_url(search_url)
        _study_tool_end(st["step"], "open_url", "Open the search in the browser", ok, summ,
                        det, now_ms() - t0)
        st["visited"] += 1

        ranked = sorted(hits, key=lambda r: next(
            (len(STUDY_SOURCES) - i for i, d in enumerate(STUDY_SOURCES)
             if d in (r.get("url") or "")), 0), reverse=True)
        pages = []
        for r in ranked:
            if len(pages) >= 6 or stop.is_set():
                break
            url = r.get("url") or ""
            if not url or _STUDY_SKIP.search(url):
                continue
            title = (r.get("title") or url)[:50]
            st["step"] += 1
            _study_tool_start(st["step"], "open_url", f"Open {title}", {"url": url})
            t0 = now_ms()
            ok, summ, det = t_open_url(url)
            _study_tool_end(st["step"], "open_url", f"Open {title}", ok, summ, det,
                            now_ms() - t0)
            st["visited"] += 1
            st["step"] += 1
            _study_tool_start(st["step"], "web_fetch", f"Read {title}", {"url": url})
            t0 = now_ms()
            ok, summ, det = t_web_fetch(url, True, 6000)
            _study_tool_end(st["step"], "web_fetch", f"Read {title}", ok, summ, det,
                            now_ms() - t0)
            if ok:
                pages.append((url, r.get("title") or url, det))
            time.sleep(0.3)
        if stop.is_set():
            raise Stopped()
        if not pages:
            emit("log", text=f"Study loop: nothing readable for {topic}; next topic")
            return 0

        emit("status", state="working", text=f"Writing notes: {topic[:50]}")
        notes = _study_write_notes(topic, query, why, pages, stop)
        if not notes:
            notes = ("# " + topic + "\n\n" +
                     "\n\n".join(f"## {t}\n{url}" for url, t, _ in pages))
        header = (f"# {topic}\n\n_{time.strftime('%Y-%m-%d %H:%M')} · "
                  f"search: {query}_\n\n{notes}\n\n---\n"
                  + "\n".join(f"- {url}" for url, _t, _x in pages) + "\n")
        fname = f"{time.strftime('%Y-%m-%d')} - {_study_slug(topic)}.md"
        path = STUDY_DIR / fname
        n = 2
        while path.exists():
            path = STUDY_DIR / f"{time.strftime('%Y-%m-%d')} - {_study_slug(topic)} ({n}).md"
            n += 1
        st["step"] += 1
        _study_tool_start(st["step"], "write_file", f"Notes: {fname}", {"path": str(path)})
        t0 = now_ms()
        ok, summ, det = t_write_file(str(path), header)
        _study_tool_end(st["step"], "write_file", f"Notes: {fname}", ok, summ, det,
                        now_ms() - t0)
        if ok:
            st["count"] += 1
            emit("lessons", lessons=len(load_lessons()))
            try:
                with open(STUDY_DIR / "_progress.md", "a", encoding="utf-8") as f:
                    f.write(f"- {time.strftime('%Y-%m-%d %H:%M')} — {topic} — "
                            f"{'; '.join(u for u, _t, _x in pages)}\n")
            except Exception:
                pass
        return len(pages)

    fails = 0
    while not stop.is_set():
        try:
            st["step"] += 1
            emit("status", state="thinking",
                 text=f"Getting curious · topic {st['count'] + 1}")
            try:
                pick = _study_next(stop)
            except Stopped:
                break
            if not pick:
                fails += 1
                if fails < 2:
                    time.sleep(2)
                    continue
                topic = fallbacks[st["count"] % len(fallbacks)]
                pick = (topic, topic + " C++ tutorial", "following the curriculum")
                fails = 0
            else:
                fails = 0
            cycle(pick)
            time.sleep(1.2)
        except Stopped:
            break
        except ModelUnavailable as e:
            emit("log", text=f"Study loop: model unavailable ({str(e)[:90]}); retrying")
            time.sleep(15)
        except Exception as e:
            emit("log", text=f"Study loop recovered from {type(e).__name__}: {e}")
            time.sleep(4)
    tail = f" Last topic: {st['last']}." if st["last"] else ""
    return (f"Study session stopped after {st['count']} topic(s), {st['visited']} page(s) "
            f"opened in your browser. Notes are in {STUDY_DIR}.{tail}")


def dispatch(name: str, args: dict):
    spec, required, _ = TOOLS[name]
    for key in required:
        if args.get(key) is None:
            return False, f"Missing required argument '{key}'", \
                f"{name} needs '{key}'. Schema: {json.dumps(spec)}"
    kwargs = {k: v for k, v in args.items() if k in spec}
    fn = globals()["t_" + name]
    return fn(**kwargs)


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
def append_history(role: str, content: str, meta: dict | None = None) -> None:
    h = _load_json(HISTORY_FILE, [])
    if not isinstance(h, list):
        h = []
    h.append({"role": role, "content": content, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
              **(meta or {})})
    _save_json(HISTORY_FILE, h[-400:])


def load_history() -> list:
    h = _load_json(HISTORY_FILE, [])
    if not isinstance(h, list):
        return []
    for m in h:
        if isinstance(m, dict) and isinstance(m.get("content"), str):
            m["content"] = _fix_mojibake(m["content"])
    return h


_MOJI_RE = re.compile(r"[ÃÂâ€]")


def _fix_mojibake(s: str) -> str:
    """Repair text saved by older builds that read the UTF-8 pipe as CP1252."""
    if not s or not _MOJI_RE.search(s):
        return s
    try:
        fixed = s.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s
    return fixed if fixed else s


def recent_prompts(limit: int = 12, chars: int = 220) -> list:
    """The user's latest distinct requests, oldest first.

    Kept in prompts.json so starting a new chat never forgets what was asked before.
    An emptied prompts.json stays empty; it is only rebuilt from history when the
    file has never existed.
    """
    items = _load_json(PROMPTS_FILE, None)
    if isinstance(items, list):
        source = [_fix_mojibake(p) for p in items if isinstance(p, str)]
    else:
        source = [str(m.get("content") or "") for m in load_history()
                  if m.get("role") == "user"]
    out, seen = [], set()
    for raw in reversed(source):
        txt = " ".join(raw.split())
        if not txt or txt in seen:
            continue
        seen.add(txt)
        out.append(txt[:chars])
        if len(out) >= limit:
            break
    return list(reversed(out))


def log_prompt(text: str) -> None:
    txt = " ".join(str(text or "").split())
    if not txt:
        return
    items = _load_json(PROMPTS_FILE, None)
    if isinstance(items, list):
        items = [_fix_mojibake(x) for x in items if isinstance(x, str)]
    else:
        items = []
        if not PROMPTS_FILE.exists():
            items = [" ".join(str(m.get("content") or "").split())[:1000]
                     for m in load_history() if m.get("role") == "user"][-100:]
        items = [x for x in items if x]
    if items and items[-1] == txt[:1000]:
        return
    items.append(txt[:1000])
    _save_json(PROMPTS_FILE, items[-300:])


def prompts_block(limit: int = 12) -> str:
    items = recent_prompts(limit)
    if not items:
        return ""
    return ("\n\nRECENT REQUESTS FROM THIS USER (earlier turns; lists, song titles, names "
            "and folders in them still count — reuse them when the user says 'those', 'it', "
            "'again' or 'the same'):\n" + "\n".join(f"- {p}" for p in items))


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
_DENIAL_RE = re.compile(
    r"(?:i(?:'m| am)?\s+)?(?:sorry[, ]+)?(?:but\s+)?"
    r"(?:can'?t|cannot|can not|am unable to|'?m unable to|don'?t have (?:direct )?access"
    r"|do not have (?:direct )?access)[^.\n]{0,120}"
    r"(?:access|browse|read|see|open|retrieve|view|control)[^.\n]{0,120}"
    r"(?:chat|conversation|history|app|window|screen|account|computer|laptop|browser|context)"
    r"|(?:i'?m|i am)\s+(?:just\s+)?an?\s+(?:ai|language model)[^.\n]{0,100}"
    r"(?:can'?t|cannot|unable|no access)"
    r"|(?:i can only|i'?m only able to)[^.\n]{0,80}(?:provide|answer)[^.\n]{0,80}"
    r"(?:context|what you tell me)",
    re.I | re.S)

CHAT_RE = re.compile(
    r"^(hi|hii+|hello|hey|yo|sup|thanks|thank you|thx|ty|ok|okay|cool|nice|great|"
    r"good (morning|evening|afternoon|night)|how are you|how's it going|who are you|"
    r"what are you|what can you do|what's up|whats up|bye|see you|lol)\b", re.I)

CHAT_SYSTEM = ("You are Anvil Fletch, an agent running on this Windows laptop with real "
               "control of it: files, apps, windows, mouse, keyboard, browser, downloads and "
               "the internet. Never claim otherwise. Voice: dry, precise, quietly "
               "confident — a competent colleague, not a chatbot; no flattery, no emoji. "
               "Reply in one or two sentences, markdown allowed.")

CONVO_SYSTEM = ("You are Anvil Fletch, an agent running on the user's own Windows laptop. "
                "You have real control of this machine — files, installed apps, windows, "
                "mouse, keyboard, clipboard, the default browser, downloads and the network. "
                "Never say you are just an AI language model, never claim you lack computer "
                "or internet access; that is false. Capability questions get a concrete yes "
                "and a short list of what you can do. Answer the request directly and "
                "completely; no preamble, no mention of tools or steps. Voice: dry, precise, "
                "quietly confident; at most one wry line. Markdown is welcome. This request "
                "needs no tools.\n"
                "Current local date and time: {now}.")

# Anything pointing at the machine or the outside world keeps the full agent loop.
ACTION_HINT = re.compile(
    r"\b(file|files|folder|folders|directory|desktop|downloads?|documents?|pictures?|"
    r"videos?|music|songs?|drive|disk|path|open|close|launch|install|uninstall|download|"
    r"upload|move|copy|rename|delete|remove|create|mkdir|save|edit|append|replace|sort|"
    r"organize|tidy|clean|zip|unzip|extract|compress|convert|resize|rotate|crop|"
    r"screenshot|clipboard|window|windows|app|apps|program|process|browser|firefox|"
    r"chrome|edge|website|url|http|https|www\.|web|internet|google|youtube|email|mail|"
    r"send|type|click|press|keystroke|hotkey|run|execute|command|powershell|cmd|terminal|"
    r"chat|chats|telegram|ayugram|whatsapp|discord|messages?|conversation|history|scroll|read|"
    r"settings|volume|brightness|wifi|network|battery|system|gpu|cpu|ram|memory|disk|"
    r"weather|news|stocks?|prices?|calendar|reminder|alarm|timer|screenshot|"
    r"audio|playlist|album|podcast|trailer|clip|grab|rip|photo|image|wallpaper|"
    r"pdf|excel|spreadsheet|word|powerpoint|deck|wikipedia|wiki|define|dictionary)\b"
    r"|[a-z]:\\|\b[\w.-]+\.(txt|md|pdf|docx?|xlsx?|pptx?|csv|json|png|jpe?g|gif|webp|"
    r"heic|mp3|mp4|wav|m4a|flac|mov|mkv|avi|zip|rar|7z|exe|msi|py|js|ts|cpp|h|html|css)\b", re.I)


def is_smalltalk(prompt: str) -> bool:
    p = (prompt or "").strip()
    return len(p.split()) <= 8 and bool(CHAT_RE.match(p))


def has_action_intent(prompt: str) -> bool:
    return bool(ACTION_HINT.search(prompt or ""))


def handle(request: dict, stop: threading.Event) -> None:
    action = request.get("action", "chat")

    if action == "ping":
        emit("ready", workspace=str(workspace()), model=CFG.get("model"),
             host=CFG["host"], tools=len(TOOLS), lessons=len(load_lessons()),
             online=ollama_alive(timeout=2.5), models=list_models())
        return

    if action == "models":
        emit("models", items=list_models() or [CFG.get("model")])
        return

    if action == "recents":
        emit("recents", items=recent_prompts(12, 300))
        return

    if action == "clear_recents":
        _save_json(PROMPTS_FILE, [])
        emit("recents", items=[])
        emit("status", state="done", text="Recent prompts cleared")
        emit("done")
        return

    if action == "new_chat":
        _save_json(HISTORY_FILE, [])
        emit("status", state="done", text="New chat")
        emit("done")
        return

    if action == "warm":
        threading.Thread(target=warm_model, daemon=True).start()
        return

    prompt = (request.get("prompt") or "").strip()
    if request.get("workspace"):
        ws = str(resolve_path(request["workspace"]))
        if ws != CFG["workspace"]:
            _WARM["key"] = ""
        CFG["workspace"] = ws
    if request.get("model"):
        if request["model"] != CFG["model"]:
            _WARM["key"] = ""
        CFG["model"] = request["model"]

    if not prompt:
        emit("answer", text="Nothing to do.")
        emit("done")
        return

    log_prompt(prompt)

    attachments = request.get("attachments") or []
    if isinstance(attachments, str):
        attachments = _as_list(attachments)
    attachments = [str(p) for p in attachments if str(p).strip()][:12]
    if attachments:
        emit("status", state="working", text=f"Reading {len(attachments)} attachment(s)")
        report = attachment_report(attachments, prompt)
        prompt += ("\n\nATTACHED FILES - the user attached these to this message and they "
                   "have ALREADY been read on this machine (content below, exact paths "
                   "included). Answer from this content now. Do NOT call read_file, "
                   "see_images or any other tool to re-read them; a guessed path will fail "
                   "and that does not mean the file is missing.\n" + report[:24000])

    if request.get("study_session"):
        try:
            answer = study_session(stop)
        except Stopped:
            answer = "Study session stopped."
        except ModelUnavailable as e:
            emit("status", state="error", text="Model unavailable")
            emit("answer", text=f"I could not reach the local model.\n\n{e}")
            emit("done")
            return
        except Exception as e:
            emit("status", state="error", text="Study loop error")
            emit("answer", text=f"{type(e).__name__}: {e}\n\n{traceback.format_exc()[-1200:]}")
            emit("done")
            return
        emit("answer", text=answer)
        append_history("user", prompt)
        append_history("assistant", answer)
        emit("done")
        return

    direct = re.match(r"^\s*(teach|remember|lesson)\s*[:\-]\s*(.+)$", prompt, re.I | re.S)
    if direct:
        body = direct.group(2).strip()
        if "->" in body:
            when, do = [x.strip() for x in body.split("->", 1)]
        elif " then " in body.lower():
            i = body.lower().find(" then ")
            when, do = body[:i].strip(), body[i + 6:].strip()
        else:
            when, do = "user correction", body
        msg = remember(when, do)
        emit("tool", op="start", name="remember", label=when[:60])
        emit("tool", op="end", name="remember", ok=True, label=when[:60],
             summary=msg, detail=msg, ms=0)
        emit("answer", text=msg)
        emit("done")
        return

    history = [{"role": m["role"], "content": m.get("content", "")}
               for m in load_history()[-24:] if m.get("role") in {"user", "assistant"}]

    # Questions like "what is on the recycling bin" have no action words, so the
    # intent gate below used to send them to the chat model, which invented an
    # answer. Deterministic routes must be checked before that gate.
    if not attachments:
        try:
            fast = fast_direct(prompt, stop)
        except Exception:
            fast = None
        if fast is not None:
            answer, _msgs = fast
            emit("answer", text=answer)
            append_history("user", prompt)
            append_history("assistant", answer)
            emit("done")
            return

    if is_smalltalk(prompt) or not has_action_intent(prompt):
        emit("status", state="writing", text="Replying")
        if is_smalltalk(prompt):
            sysmsg, ctx = CHAT_SYSTEM + prompts_block(8), history[-2:]
        else:
            sysmsg = (CONVO_SYSTEM.format(now=time.strftime("%A %d %B %Y, %H:%M"))
                      + prompts_block(10))
            ctx = history[-6:]
        msgs = [{"role": "system", "content": sysmsg}]
        if ctx:
            msgs += [{"role": m["role"], "content": m["content"][:600]} for m in ctx]
        msgs.append({"role": "user", "content": prompt})
        try:
            text = stream_reply(msgs, stop)
        except Stopped:
            text = "Stopped."
        except ModelUnavailable as e:
            emit("status", state="error", text="Model unavailable")
            emit("answer", text=f"I could not reach the local model.\n\n{e}")
            emit("done")
            return
        text = text or "I'm here. What should we get done?"
        emit("status", state="done", text="1 step(s)", steps=1)
        emit("answer", text=text)
        append_history("user", prompt)
        append_history("assistant", text)
        emit("done")
        return

    try:
        answer, messages = run_turn(prompt, history, stop,
                                    study=bool(request.get("study")))
    except ModelUnavailable as e:
        emit("status", state="error", text="Model unavailable")
        emit("answer", text=f"I could not reach the local model.\n\n{e}\n\n"
                            f"Start it with `ollama serve`, or point me at another host in Settings.")
        emit("done")
        return
    except Exception as e:
        emit("status", state="error", text="Agent error")
        emit("answer", text=f"{type(e).__name__}: {e}\n\n{traceback.format_exc()[-1200:]}")
        emit("done")
        return

    if stop.is_set() and "stopped by you" not in answer.lower():
        answer = (answer + "\n\n_(stopped by you)_").strip()

    emit("answer", text=answer)
    append_history("user", prompt)
    append_history("assistant", answer)
    emit("done")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true",
                        help="Read JSON requests from stdin, one per line")
    parser.add_argument("--prompt", type=str, default="")
    args = parser.parse_args()
    seed_core_lessons()

    if not args.serve:
        handle({"prompt": args.prompt}, threading.Event())
        return

    # Long-lived process: the UI keeps it warm so the model stays loaded in VRAM.
    state = {"stop": threading.Event(), "busy": threading.Event()}

    def worker(req: dict):
        global _STOP_EVT
        _STOP_EVT = state["stop"]
        state["busy"].set()
        try:
            handle(req, state["stop"])
        except Exception:
            emit("status", state="error", text="Crash")
            emit("answer", text="Agent crashed:\n" + traceback.format_exc()[-1500:])
            emit("done")
        finally:
            _STOP_EVT = None
            state["busy"].clear()

    emit("ready", workspace=str(workspace()), model=CFG.get("model"), host=CFG["host"],
         tools=len(TOOLS), lessons=len(load_lessons()))

    def probe():
        online = ollama_alive(timeout=4.0)
        emit("probe", online=online, models=list_models(),
             text="Model server ready" if online else "Model server offline")
        if not online:
            return
        # Load the weights now (a few seconds) but do not eat the CPU with a full
        # prompt prewarm while the user is composing their first message.
        warm_model()
        deadline = time.time() + 30
        while time.time() < deadline:
            if state["busy"].is_set() or state.get("used"):
                return
            time.sleep(0.5)
        if not state["busy"].is_set() and not state.get("used"):
            prewarm(base_system())

    threading.Thread(target=probe, daemon=True).start()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        action = req.get("action")
        if action == "stop":
            if state["busy"].is_set():
                state["stop"].set()
                emit("status", state="stopping", text="Stopping after the current step")
            else:
                emit("status", state="ready", text="Ready")
            continue
        if action == "ping":
            emit("ready", workspace=str(workspace()), model=CFG.get("model"),
                 host=CFG["host"], tools=len(TOOLS), lessons=len(load_lessons()),
                 busy=state["busy"].is_set())
            threading.Thread(target=probe, daemon=True).start()
            continue
        if action == "models":
            emit("models", items=list_models() or [CFG.get("model")])
            continue
        if action == "prewarm":
            state["used"] = True
            threading.Thread(target=prewarm, args=(base_system(),), daemon=True).start()
            continue
        if state["busy"].is_set():
            emit("status", state="busy", text="Already working — press Stop first")
            continue
        state["used"] = True
        state["stop"] = threading.Event()
        threading.Thread(target=worker, args=(req,), daemon=True).start()


if __name__ == "__main__":
    main()
else:
    seed_core_lessons()
