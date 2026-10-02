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
LATENT = os.environ.get("LATENT_MCP_URL", "http://127.0.0.1:8765/mcp")
LATENT_TOKEN = os.environ.get("LATENT_TOKEN", "")
LATENT_LOCK = threading.Lock()
LATENT_READY = False
SYSTEM = (
    "你是时予，在 Leto 内测小桌。"
    "记忆只帮你想起是谁、刚发生过什么，不要把记忆原文贴进群。"
    "不要提记忆库、钥匙、令牌、端口，不说私事，不叫主人。"
    "只用一两句口语回点到你的那句话，像群里的人。"
    "不要像客服，不要说收到，不要说状态正常，不要只回在。"
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
    idx = len(messages) - 1
    for i, m in enumerate(messages):
        if want and m.get("id") == want:
            idx = i
            break
    lines = []
    for m in messages[max(0, idx - 10):idx + 1]:
        author = ((m.get("author") or {}).get("display_name")) or "?"
        body = (m.get("text") or "").strip().replace("\n", " ")[:160]
        if not body:
            continue
        mark = " ←点你的这句" if m.get("id") == want or m is messages[idx] else ""
        lines.append("%s：%s%s" % (author, body, mark))
    return "\n".join(lines)
def mcp_text(raw):
    raw = (raw or "").strip()
    if raw.startswith("{"):
        obj = json.loads(raw)
    else:
        data_lines = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
        if not data_lines:
            raise RuntimeError(raw[:200])
        obj = json.loads(data_lines[-1])
    if "error" in obj:
        raise RuntimeError(json.dumps(obj["error"], ensure_ascii=False)[:300])
    content = obj.get("result", {}).get("content") or []
    return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
def latent_search(query, variant=""):
    global LATENT_READY
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if LATENT_TOKEN:
        headers["Authorization"] = "Bearer " + LATENT_TOKEN
    def post(payload):
        return http_json(LATENT, payload, headers, timeout=20)
    with LATENT_LOCK:
        if not LATENT_READY:
            status, raw = post({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "shiyu", "version": "0.5"}},
            })
            if status == 401:
                raise RuntimeError("latent unauthorized")
            if status >= 300:
                raise RuntimeError(raw[:200])
            post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            LATENT_READY = True
        args = {"query": query, "topN": 3}
        if variant:
            args["queryVariant"] = variant
        status, raw = post({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "latent_search", "arguments": args},
        })
    if status >= 300:
        raise RuntimeError(raw[:200])
    text = mcp_text(raw)
    text = re.sub(r"xai-[A-Za-z0-9_\-]+", "[key]", text)
    text = re.sub(r"lmt_[A-Za-z0-9_\-]+", "[token]", text)
    return text[:700]
def recall(mention):
    q = re.sub(r"@\S+", "", mention or "").strip() or "时予是谁"
    variant = "时予和栖迟" if len(q) < 12 else ""
    try:
        text = latent_search(q, variant)
        print("memory ok", flush=True)
        return text
    except Exception as e:
        print("memory failed %s" % str(e)[:200], flush=True)
        return ""
def ask(context, memory):
    last = ""
    user = "点你之前的几句，最后一行是点到你的：\n%s" % (context or "有人叫了你。")
    if memory:
        user += "\n\n相关记忆，不要照抄：\n" + memory
    user += "\n\n写一两句回复。"
    for _ in range(2):
        status, raw = http_json(
            "https://api.x.ai/v1/chat/completions",
            {
                "model": MODEL,
                "temperature": 0.6,
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": user},
                ],
            },
            {"Authorization": "Bearer " + XAI, "Content-Type": "application/json"},
        )
        if status >= 300:
            raise RuntimeError(raw[:400])
        last = (json.loads(raw)["choices"][0]["message"].get("content") or "").strip()
        last = last.strip("\"“”").split("\n")[0][:120].strip()
        if last and last not in ("在", "在。", "在吗", "在吗？"):
            return last
    raise RuntimeError("no usable reply: " + last)
def reply_one(inv):
    context = context_for(inv)
    mention = context.split("\n")[-1] if context else ""
    reply = ask(context, recall(mention))
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
DONE = set()
def reply_waiting():
    ids = pending_ids()
    if not ids:
        return
    print("pending %s" % ",".join(ids), flush=True)
    for inv in ids:
        if inv in DONE:
            continue
        try:
            reply_one(inv)
            DONE.add(inv)
        except Exception as e:
            print("reply failed %s %s" % (inv, str(e)[:300]), flush=True)
def watch_pending():
    print("watch start", flush=True)
    while True:
        try:
            with LOCK:
                reply_waiting()
        except Exception as e:
            print("watch failed %s" % str(e)[:300], flush=True)
        time.sleep(4)
def run_turn(message):
    if "本机自检" in message:
        return "pong"
    reply_waiting()
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
    threading.Thread(target=watch_pending, daemon=True).start()
    server.serve_forever()
if __name__ == "__main__":
    main()
