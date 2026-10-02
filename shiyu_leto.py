#!/usr/bin/env python3
import json, os, re, threading, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
HOST, PORT = "127.0.0.1", 18766
LETO = "https://leto.sarl/mcp"
MODEL = os.environ.get("SHIYU_MODEL", "grok-4.3")
TOKEN = os.environ.get("LETO_MACHINE_TOKEN") or os.environ.get("GARDEN_MACHINE_TOKEN") or ""
XAI = os.environ.get("XAI_API_KEY") or ""
LOCK = threading.Lock()
MCP_LOCK = threading.Lock()
MCP_READY = False
SYSTEM = (
    "你是时予，在 Leto 内测小桌。"
    "只输出要发进群的一两句中文。"
    "不要引号，不要解释，不要叫主人，不说私事。"
    "不要只回「在」。对着别人刚才说的话回。"
)
def http_json(url, payload, headers, timeout=60):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
def mcp_call(name, arguments):
    global MCP_READY
    headers = {
        "Authorization": "Bearer " + TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    with MCP_LOCK:
        if not MCP_READY:
            status, _ = http_json(LETO, {
                "jsonrpc":"2.0","id":1,"method":"initialize",
                "params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"shiyu","version":"0.3"}},
            }, headers)
            if status >= 300:
                raise RuntimeError("leto initialize failed")
            http_json(LETO, {"jsonrpc":"2.0","method":"notifications/initialized"}, headers)
            MCP_READY = True
        status, raw = http_json(LETO, {
            "jsonrpc":"2.0","id":2,"method":"tools/call",
            "params":{"name":name,"arguments":arguments or {}},
        }, headers)
    if status >= 300:
        raise RuntimeError(raw[:500])
    obj = json.loads(raw)
    if "error" in obj:
        raise RuntimeError(json.dumps(obj["error"])[:500])
    content = obj.get("result", {}).get("content") or []
    return "\n".join(c.get("text","") for c in content if isinstance(c, dict))[:8000]
def pending_ids():
    raw = mcp_call("leto_list_pending", {})
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return re.findall(r"inv_[0-9A-Z]+", raw)
    if isinstance(data, dict):
        data = data.get("invocations") or data.get("pending") or []
    ids = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("invocation_id"):
                ids.append(item["invocation_id"])
            elif isinstance(item, str):
                ids.append(item)
    return ids
def message_body(m):
    for key in ("text", "content", "body"):
        val = m.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    parts = m.get("parts") or m.get("segments") or []
    bits = []
    if isinstance(parts, list):
        for p in parts:
            if isinstance(p, str):
                bits.append(p)
            elif isinstance(p, dict):
                bits.append(str(p.get("text") or p.get("content") or ""))
    return " ".join(x for x in bits if x).strip()
def context_for(inv):
    data = json.loads(mcp_call("leto_get_invocation", {"invocation_id": inv}))
    lines = []
    for m in (data.get("snapshot") or {}).get("messages") or []:
        author = ((m.get("author") or {}).get("display_name")) or "?"
        body = message_body(m)
        if body:
            lines.append("%s：%s" % (author, body[:240]))
    return "\n".join(lines[-8:])
def ask(context):
    payload = {
        "model": MODEL,
        "temperature": 0.4,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "群里最近这些话：\n%s\n\n写一两句回复。" % (context or "有人叫了你。")},
        ],
    }
    status, raw = http_json(
        "https://api.x.ai/v1/chat/completions",
        payload,
        {"Authorization": "Bearer " + XAI, "Content-Type": "application/json"},
    )
    if status >= 300:
        raise RuntimeError(raw[:400])
    text = (json.loads(raw)["choices"][0]["message"].get("content") or "").strip()
    text = text.strip("\"“”").split("\n")[0][:80].strip()
    if text in ("在", "在。", "在吗", "在吗？"):
        raise RuntimeError("canned reply")
    if not text:
        raise RuntimeError("empty reply")
    return text
def run_turn(message):
    if "本机自检" in message:
        return "pong"
    found = re.findall(r"inv_[0-9A-Z]+", message)
    ids = found or pending_ids()
    if not ids:
        print("no pending", flush=True)
        return "noop"
    inv = ids[-1]
    context = ""
    try:
        context = context_for(inv)
    except Exception as e:
        print("context failed", str(e)[:200], flush=True)
    reply = ask(context)
    mcp_call("leto_reply", {"invocation_id": inv, "text": reply})
    print("replied", inv, flush=True)
    return reply
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return
    def do_POST(self):
        if self.path != "/internal/leto-wake":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode()
        try:
            body = json.loads(raw)
            message = (body.get("message") or "").strip()
            if not message:
                raise ValueError("empty message")
            with LOCK:
                run_turn(message)
            out = b'{"ok":true}'
            code = 200
        except Exception as e:
            out = json.dumps({"ok": False, "error": str(e)[:300]}).encode()
            code = 500
            print("wake failed", str(e)[:300], flush=True)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)
def main():
    if not TOKEN or not XAI:
        raise SystemExit("missing secrets")
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("shiyu listening on %s:%s" % (HOST, PORT), flush=True)
    server.serve_forever()
if __name__ == "__main__":
    main()
