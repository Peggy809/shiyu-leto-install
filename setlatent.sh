#!/bin/bash
set -eu
python3 - << 'PY'
import os, pathlib, re, subprocess, json, urllib.request, urllib.error

def read_proc(pid, name):
    try:
        return pathlib.Path("/proc/%s/%s" % (pid, name)).read_bytes()
    except Exception:
        return b""

token = ""
source = ""
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    cmd = read_proc(pid, "cmdline").replace(b"\0", b" ").decode("utf-8", "replace")
    if "mcp_server" not in cmd and "8765" not in cmd:
        continue
    m = re.search(r"--token(?:=|\s+)(\S+)", cmd)
    if m and not m.group(1).startswith("$") and not m.group(1).startswith("-"):
        token = m.group(1).strip("\"'")
        source = "cmdline"
        break
    env = read_proc(pid, "environ").split(b"\0")
    for item in env:
        if b"=" not in item:
            continue
        k, v = item.split(b"=", 1)
        key = k.decode("utf-8", "replace")
        if key in ("XAI_API_KEY", "LETO_MACHINE_TOKEN", "GARDEN_MACHINE_TOKEN"):
            continue
        if "TOKEN" in key.upper() or key.upper() in ("AUTH", "MCP_AUTH"):
            val = v.decode("utf-8", "replace").strip()
            if len(val) >= 8:
                token = val
                source = key
                break
    if token:
        break

if not token:
    try:
        text = subprocess.check_output(["systemctl", "cat", "latent-memory"], text=True, errors="replace")
    except Exception:
        text = ""
    m = re.search(r"--token(?:=|\s+)(\S+)", text)
    if m and not m.group(1).startswith("$") and not m.group(1).startswith("-"):
        token = m.group(1).strip("\"'")
        source = "unit"

if not token:
    print("token_missing")
    raise SystemExit(1)

p = pathlib.Path("/etc/shiyu/leto.env")
lines = p.read_text().splitlines() if p.exists() else []
out = []
found = False
for line in lines:
    if line.startswith("LATENT_TOKEN="):
        out.append("LATENT_TOKEN=" + token)
        found = True
    else:
        out.append(line)
if not found:
    out.append("LATENT_TOKEN=" + token)
p.write_text("\n".join(out) + "\n")
p.chmod(0o600)
print("token_found", source, "len", len(token))

def post(payload, tok):
    data = json.dumps(payload).encode()
    req = urllib.request.Request("http://127.0.0.1:8765/mcp", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code

status = post({
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "shiyu", "version": "0.6"}},
}, token)
print("latent_http", status)
PY
systemctl restart shiyu-leto
sleep 2
systemctl is-active shiyu-leto
