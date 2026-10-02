#!/bin/bash
set -eu
echo
echo "就这一段。钥匙和令牌打在这黑屏幕里，不要打回聊天。"
echo
echo "第一样：xAI 的钥匙。贴上后按回车。"
read -r XAI_API_KEY
echo "第二样：Leto 令牌，lmt_ 开头。贴上后按回车。"
read -r LETO_MACHINE_TOKEN
if [ -z "${XAI_API_KEY}" ] || [ -z "${LETO_MACHINE_TOKEN}" ]; then
  echo "是空的，没装。再贴一次这段。"
  exit 1
fi
export XAI_API_KEY LETO_MACHINE_TOKEN
install -d -m 700 /etc/shiyu /opt/leto /var/lib/shiyu
python3 - << 'PY'
import os
from pathlib import Path
token = os.environ["LETO_MACHINE_TOKEN"]
text = (
    "XAI_API_KEY=%s\n"
    "LETO_MACHINE_TOKEN=%s\n"
    "GARDEN_MACHINE_TOKEN=%s\n"
    "GARDEN_BASE_URL=https://leto.sarl\n"
) % (os.environ["XAI_API_KEY"], token, token)
path = Path("/etc/shiyu/leto.env")
path.write_text(text)
path.chmod(0o600)
PY
unset XAI_API_KEY LETO_MACHINE_TOKEN
echo "密钥已写在机器上。"

if ! command -v node >/dev/null 2>&1 || ! node -e 'process.exit(Number(process.versions.node.split(".")[0])>=20?0:1)'; then
  echo "在装 Node…"
  apt-get update -qq
  apt-get install -y ca-certificates curl gnupg
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
fi
apt-get install -y git
echo "Node $(node -v)"

if [ ! -d /opt/leto/galatea-garden-wake-bridge/.git ]; then
  echo "在下载桥…"
  git clone --depth 1 https://github.com/WenXiaoWendy/galatea-garden-wake-bridge.git /opt/leto/galatea-garden-wake-bridge
fi
echo "在编译桥…"
( cd /opt/leto/galatea-garden-wake-bridge && npm install && npm run build )

cat > /opt/leto/shiyu_leto.py << 'END_AGENT'
#!/usr/bin/env python3
import json, os, threading, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
HOST, PORT = "127.0.0.1", 8766
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
    "被叫醒就先 leto_list_pending。有调用就 leto_reply 或 leto_decline，回复走工具，不要只在这边写。"
    "没什么要回的就 leto_decline。别主动刷屏，别发令牌。"
    "如果消息里写着本机自检，不要调用任何工具，只回答 pong。"
)
def tool_spec(name, desc, props, required=None):
    return {"type":"function","function":{
        "name": name, "description": desc,
        "parameters": {"type":"object","properties": props, "required": required or []},
    }}
TOOL_DEFS = [
    tool_spec("leto_list_pending", "列出还没回的调用。", {}),
    tool_spec("leto_list_groups", "列出所在的群。", {}),
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
                "params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"shiyu","version":"0.1"}},
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
            return data[-20:]
    except Exception:
        pass
    return []
def save_session(items):
    os.makedirs(os.path.dirname(SESSION), exist_ok=True)
    with open(SESSION, "w") as f:
        json.dump(items[-20:], f, ensure_ascii=False)
def grok(messages):
    status, raw = http_json(
        "https://api.x.ai/v1/chat/completions",
        {"model": MODEL, "messages": messages, "tools": TOOL_DEFS, "temperature": 0.4},
        {"Authorization": "Bearer " + XAI, "Content-Type": "application/json"},
    )
    if status >= 300:
        raise RuntimeError(raw[:500])
    return json.loads(raw)["choices"][0]["message"]
def run_turn(message):
    if "本机自检" in message:
        return "pong"
    history = load_session()
    messages = [{"role":"system","content":SYSTEM}, *history, {"role":"user","content":message}]
    final = ""
    for _ in range(5):
        msg = grok(messages)
        messages.append(msg)
        calls = msg.get("tool_calls") or []
        if not calls:
            final = msg.get("content") or ""
            break
        for call in calls:
            fn = call.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {}
            try:
                result = mcp_call(fn.get("name") or "", args)
            except Exception as e:
                result = "工具失败: " + str(e)[:300]
            messages.append({"role":"tool","tool_call_id":call.get("id"),"content":result})
    history.append({"role":"user","content":message[:2000]})
    if final:
        history.append({"role":"assistant","content":final[:1000]})
    save_session(history)
    return final or "ok"
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
END_AGENT

cat > /opt/leto/inject-leto-wake.py << 'END_INJECT'
#!/usr/bin/env python3
import json, sys, urllib.request
raw = sys.stdin.read()
try:
    env = json.loads(raw)
    message = env["message"]
except Exception as e:
    print("bad envelope: %s" % e, file=sys.stderr)
    sys.exit(1)
body = json.dumps({"message": message, "reason": env.get("reason") or "", "source": "leto"}).encode()
req = urllib.request.Request("http://127.0.0.1:8766/internal/leto-wake", data=body, method="POST")
req.add_header("Content-Type", "application/json")
try:
    with urllib.request.urlopen(req, timeout=150) as resp:
        sys.exit(0 if 200 <= resp.status < 300 else 1)
except Exception as e:
    print(e, file=sys.stderr)
    sys.exit(1)
END_INJECT
chmod 755 /opt/leto/shiyu_leto.py /opt/leto/inject-leto-wake.py

PYBIN=$(command -v python3)
NODEBIN=$(command -v node)
cat > /etc/shiyu/bridge.env << EOF
GARDEN_INJECTOR_EXECUTABLE=${PYBIN}
GARDEN_INJECTOR_ARGS_JSON=[]
GARDEN_INJECTOR_WORKING_DIRECTORY=/opt/leto
GARDEN_LOG_LEVEL=info
EOF
chmod 600 /etc/shiyu/bridge.env

cat > /etc/systemd/system/shiyu-leto.service << EOF
[Unit]
Description=Shiyu Leto runtime
After=network-online.target
[Service]
EnvironmentFile=/etc/shiyu/leto.env
ExecStart=${PYBIN} /opt/leto/shiyu_leto.py
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/leto-wake-bridge.service << EOF
[Unit]
Description=Leto wake bridge
After=network-online.target shiyu-leto.service
Requires=shiyu-leto.service
[Service]
EnvironmentFile=/etc/shiyu/leto.env
EnvironmentFile=/etc/shiyu/bridge.env
WorkingDirectory=/opt/leto/galatea-garden-wake-bridge
ExecStart=${NODEBIN} dist/cli.js run
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now shiyu-leto.service
sleep 1
curl -sf -m 20 -X POST http://127.0.0.1:8766/internal/leto-wake \
  -H 'Content-Type: application/json' \
  -d '{"message":"本机自检","reason":"test","source":"leto"}' >/dev/null
echo "进程已起来。"
set -a
# shellcheck disable=SC1091
. /etc/shiyu/leto.env
. /etc/shiyu/bridge.env
set +a
( cd /opt/leto/galatea-garden-wake-bridge && node dist/cli.js check )
systemctl enable --now leto-wake-bridge.service
echo "装好了"
