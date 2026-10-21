#!/bin/bash
# 换成 Leto 官方唤醒桥。沿用 /etc/shiyu/leto.env 里现有的令牌，不重新生成。
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo "NEED_ROOT"
  exit 1
fi
TOKEN=$(python3 - << 'PY'
import pathlib
text = pathlib.Path("/etc/shiyu/leto.env").read_text()
val = ""
for line in text.splitlines():
    if line.startswith("LETO_MACHINE_TOKEN="):
        val = line.split("=", 1)[1].strip().strip('"').strip("'")
        break
if not val.startswith("lmt_"):
    raise SystemExit("NO_TOKEN")
print(val)
PY
)
install -d -m 755 /opt/leto /opt/leto-wake-bridge
if [ -d /opt/leto-wake-bridge/.git ]; then
  git -C /opt/leto-wake-bridge pull --ff-only
else
  rm -rf /opt/leto-wake-bridge
  git clone --depth 1 https://github.com/oliscatt/leto-wake-bridge.git /opt/leto-wake-bridge
fi
NODEBIN=$(command -v node || true)
ok=0
if [ -n "$NODEBIN" ]; then
  major=$("$NODEBIN" -p "process.versions.node.split('.')[0]" 2>/dev/null || echo 0)
  if [ "$major" -ge 22 ]; then
    ok=1
  fi
fi
if [ "$ok" -ne 1 ]; then
  ver=$(python3 - << 'PY'
import json, urllib.request
idx = json.load(urllib.request.urlopen("https://nodejs.org/dist/index.json", timeout=30))
print(next(x["version"] for x in idx if x["version"].startswith("v22.")))
PY
)
  curl -fsSL -o /tmp/node22.tar.xz "https://nodejs.org/dist/${ver}/node-${ver}-linux-x64.tar.xz"
  rm -rf "/opt/node-${ver}-linux-x64"
  tar -xJf /tmp/node22.tar.xz -C /opt
  NODEBIN="/opt/node-${ver}-linux-x64/bin/node"
fi
cat > /opt/leto/leto_inject.py << 'PY'
#!/usr/bin/env python3
import json, sys, urllib.request
line = sys.stdin.readline()
try:
    message = (json.loads(line).get("message") or "").strip()
except Exception:
    message = line.strip()
if not message:
    sys.exit("empty wake")
data = json.dumps({"message": message}, ensure_ascii=False).encode()
req = urllib.request.Request(
    "http://127.0.0.1:8766/internal/leto-wake", data=data, method="POST"
)
req.add_header("Content-Type", "application/json")
with urllib.request.urlopen(req, timeout=540) as resp:
    if not (200 <= resp.status < 300):
        sys.exit(1)
PY
chmod 755 /opt/leto/leto_inject.py
umask 077
PYBIN=$(command -v python3)
cat > /etc/leto-wake-bridge.env << EOF
LETO_BASE_URL=https://leto.sarl
LETO_MACHINE_TOKEN=${TOKEN}
LETO_INJECTOR_EXECUTABLE=${PYBIN}
LETO_INJECTOR_ARGS_JSON=["/opt/leto/leto_inject.py"]
LETO_INJECTOR_WORKING_DIRECTORY=/opt/leto
EOF
chmod 600 /etc/leto-wake-bridge.env
unset TOKEN
systemctl stop leto-wake-bridge || true
cat > /etc/systemd/system/leto-wake-bridge.service << EOF
[Unit]
Description=Leto wake bridge
Wants=network-online.target
After=network-online.target shiyu-leto.service
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=root
EnvironmentFile=/etc/leto-wake-bridge.env
ExecStart=${NODEBIN} /opt/leto-wake-bridge/bridge.mjs run
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl reset-failed leto-wake-bridge || true
systemctl enable leto-wake-bridge
set +e
"${NODEBIN}" --env-file=/etc/leto-wake-bridge.env /opt/leto-wake-bridge/bridge.mjs check
check_rc=$?
set -e
if [ "$check_rc" -ne 0 ]; then
  echo "CHECK_FAILED ${check_rc}"
  exit "$check_rc"
fi
systemctl restart shiyu-leto
systemctl start leto-wake-bridge
sleep 2
echo "NODE $($NODEBIN -v)"
systemctl is-active shiyu-leto
systemctl is-active leto-wake-bridge
