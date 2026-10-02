#!/usr/bin/env python3
import json, os, re, sys, threading, time, urllib.error, urllib.request
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
    "不要只回「在」。对着别人刚才说的那句话回。"
)
def http_json(url, payload, headers, timeout=45):
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
                "params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"shiyu","version":"0.4"}},
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
    return "\n".join(c.get("text","") for c in content if isinstance(c, dict))
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
def context_for(inv):
    data = json.loads(mcp_call("leto_get_invocation", {"invocation_id": inv}))
    messages = (data.get("snapshot") or {}).get("messages") or []
    want = (data.get("trigger") or {}).get("message_id")
    chosen = None
    for m in messages:
        if want and m.get("id") == want:
            chosen = m
            break
    if chosen is None and messages:
        chosen = messages[-1]
    chosen = chosen or {}
    author = ((chosen.get("author") or {}).get("display_name")) or "?"
    body = (chosen.get("text") or "").strip()
    quote = chosen.get("quote")
    extra = ""
    if isinstance(quote, dict) and (quote.get("text") or "").strip():
        extra = "\n引用：" + quote["text"].strip()[:200]
    elif isinstance(quote, str) and quote.strip():
        extra = "\n引用：" + quote.strip()[:200]
    return "%s：%s%s" % (author, body[:400], extra)
def ask(context):
    last = ""
    for _ in range(2):
        status, raw = http_json(
            "https://api.x.ai/v1/chat/completions",
            {
                "model": MODEL,
                "temperature": 0.5,
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": "点到你的这句：\n%s\n\n写一两句回复。禁止只回在。" % (context or "有人叫了你。")},
                ],
            },
            {"Authorization": "Bearer " + XAI, "Content-Type": "application/json"},
        )
        if status >= 300:
            raise RuntimeError(raw[:400])
        last = (json.loads(raw)["choices"][0]["message"].get("content") or "").strip()
        last = last.strip("\"“”").split("\n")[0][:80].strip()
        if last and last not in ("在", "在。", "在吗", "在吗？"):
            return last
    raise RuntimeError("no usable reply: " + last)
def reply_one(inv):
    reply = ask(context_for(inv))
    mcp_call("leto_reply", {"invocation_id": inv, "text": reply})
    print("replied", inv, flush=True)
def wait_ids(message):
    found = re.findall(r"inv_[0-9A-Z]+", message)
    if found:
        return found
    for _ in range(8):
        ids = pending_ids()
        if ids:
            return ids
        time.sleep(1)
    return []
def run_turn(message):
    if "本机自检" in message:
        return "pong"
    ids = wait_ids(message)
    if not ids:
        raise RuntimeError("no pending")
    reply_one(ids[-1])
def drain_pending():
    print("drain start", flush=True)
    time.sleep(0.5)
    try:
        ids = pending_ids()
        print("pending %s" % ",".join(ids), flush=True)
    except Exception as e:
        print("pending failed %s" % str(e)[:300], flush=True)
        return
    with LOCK:
        for inv in ids:
            try:
                reply_one(inv)
            except Exception as e:
                print("drain failed %s %s" % (inv, str(e)[:300]), flush=True)
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
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    if not TOKEN or not XAI:
        raise SystemExit("missing secrets")
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("shiyu listening on %s:%s" % (HOST, PORT), flush=True)
    threading.Thread(target=drain_pending, daemon=True).start()
    server.serve_forever()
if __name__ == "__main__":
    main()
