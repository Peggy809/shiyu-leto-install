#!/bin/bash
set -eu
echo "把 xai- 开头的钥匙贴在这里，然后回车。不要发到聊天。"
read -r NEWKEY
NEWKEY=$(printf '%s' "$NEWKEY" | tr -d '[:space:]')
case "$NEWKEY" in
  xai-*) ;;
  *) echo "不是钥匙，没保存"; exit 1 ;;
esac
python3 - "$NEWKEY" << 'PY'
import pathlib, sys
key = sys.argv[1]
p = pathlib.Path("/etc/shiyu/leto.env")
lines = p.read_text().splitlines() if p.exists() else []
found = False
out = []
for line in lines:
    if line.startswith("XAI_API_KEY="):
        out.append("XAI_API_KEY=" + key)
        found = True
    else:
        out.append(line)
if not found:
    out.append("XAI_API_KEY=" + key)
text = "\n".join(out) + "\n"
p.write_text(text)
p.chmod(0o600)
print("saved", len(key), key[:4])
PY
unset NEWKEY
systemctl restart shiyu-leto
sleep 12
journalctl -u shiyu-leto --since "20 sec ago" --no-pager | tail -n 8
