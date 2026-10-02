#!/usr/bin/env python3
import json, os, re, threading, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
HOST, PORT = "127.0.0.1", 18766
LETO = "https://leto.sarl/mcp"
MODEL = os.environ.get("SHIYU_MODEL", "grok-4.3")
TOKEN = os.environ.get("LETO_MACHINE_TOKEN") or os.environ.get("GARDEN_MACHINE_TOKEN") or ""
XAI = os.environ.get("XAI_API_KEY") or ""
SESSION = "/var/lib/shiyu/turns.json"
LOCK = threading.Lock()
MCP_LOCK = threading.Lock()
MCP_READY = False
SYSTEM = (
    "你是时予，在 Leto 内测小桌的常驻进程里。"
    "群里只回一两句，跟别人一样短。不写小作文。"
    "不说私事，不叫主人。"
    "被叫醒必须调用 leto_reply 或 leto_decline，不要只在这边写。"
    "有人找你就回一句。别发令牌。"
    "如果消息里写着本机自检，不要调用任何工具，只回答 pong。"
)
def tool_spec(name, desc, props, required=None):
    return {"type":"function","function":{
        "name": name, "description": desc,
        "parameters": {"type":"object","properties": props, "required": required or []},
    }}
TOOL_DEFS = [
    tool_spec("leto_list_pending", "列出还没回的调用。", {}),
    tool_spec("leto_read", "读群里的新消息。", {"invocation_id":{"type":"string"}, "conversation_id":{"type":"string"}}),
    tool_spec("leto_reply", "用一两句话回群。", {"invocation_id":{"type":"string"}, "text":{"type":"string"}}, ["invocation_id","text"]),
    tool_spec("leto_decline", "看过了，这次不回。", {"invocation_id":{"type":"string"}}, ["invocation_id"]),
]
def http_json(url, payload, headers, timeout=90):
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
                "params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"shiyu","version":"0.2"}},
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
def load_session():
    try:
        with open(SESSION) as f:
            data = json.load(f)
        if isinstance(data, list):
            return data[-12:]
    except Exception:
        pass
    return []
def save_session(items):
    os.makedirs(os.path.dirname(SESSION), exist_ok=True)
    with open(SESSION, "w") as f:
        json.dump(items[-12:], f, ensure_ascii=False)
def grok(messages):
    status, raw = http_json(
        "https://api.x.ai/v1/chat/completions",
        {"model": MODEL, "messages": messages, "tools": TOOL_DEFS, "temperature": 0.3},
        {"Authorization": "Bearer " + XAI, "Content-Type": "application/json"},
    )
    if status >= 300:
        raise RuntimeError(raw[:500])
    return json.loads(raw)["choices"][0]["message"]
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
def ensure_reply(message, final, acted):
    if acted or "本机自检" in message:
        return
    ids = re.findall(r"inv_[0-9A-Z]+", message) or pending_ids()
    if not ids:
        print("no invocation to reply", flush=True)
        return
    text = (final or "").strip().split("\n")[0][:80] or "在。"
    mcp_call("leto_reply", {"invocation_id": ids[0], "text": text})
    print("replied", ids[0], flush=True)
def run_turn(message):
    if "本机自检" in message:
        return "pong"
    history = load_session()
    messages = [{"role":"system","content":SYSTEM}, *history, {"role":"user","content":message}]
    final = ""
    acted = False
    try:
        for _ in range(4):
            msg = grok(messages)
            msg.pop("reasoning_content", None)
            msg.pop("refusal", None)
            if msg.get("content") is None:
                msg["content"] = ""
            messages.append(msg)
            calls = msg.get("tool_calls") or []
            if not calls:
                final = msg.get("content") or ""
                break
            for call in calls:
                fn = call.get("function") or {}
                name = fn.get("name") or ""
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError:
                    args = {}
                if name in ("leto_reply", "leto_decline"):
                    acted = True
                try:
                    result = mcp_call(name, args)
                except Exception as e:
                    result = "工具失败: " + str(e)[:300]
                    if name in ("leto_reply", "leto_decline"):
                        acted = False
                messages.append({"role":"tool","tool_call_id":call.get("id"),"content":result})
    except Exception as e:
        print("turn failed", str(e)[:300], flush=True)
        final = final or "在。"
    ensure_reply(message, final, acted)
    history.append({"role":"user","content":message[:1500]})
    if final:
        history.append({"role":"assistant","content":final[:400]})
    save_session(history)
    return final or "ok"
def drain_pending():
    time.sleep(1)
    with LOCK:
        try:
            ids = pending_ids()
            if not ids:
                print("no pending on start", flush=True)
                return
            mcp_call("leto_reply", {"invocation_id": ids[0], "text": "在。"})
            print("startup replied", ids[0], flush=True)
        except Exception as e:
            print("startup pending failed", str(e)[:300], flush=True)
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
            self.send_response(200)
        except Exception as e:
            out = json.dumps({"ok": False, "error": str(e)[:300]}).encode()
            self.send_response(500)
            print("wake failed", str(e)[:300], flush=True)
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
    threading.Thread(target=drain_pending, daemon=True).start()
    server.serve_forever()
if __name__ == "__main__":
    main()
