#!/bin/bash
echo "READY"
echo "看到 READY 之后，再把 xai- 开头的钥匙贴上，回车。"
while true; do
  read -r NEWKEY || exit 1
  NEWKEY=$(printf '%s' "$NEWKEY" | tr -d '[:space:]' | tr -d '"' | tr -d "'")
  NEWKEY=${NEWKEY#Bearer}
  NEWKEY=${NEWKEY#bearer}
  case "$NEWKEY" in
    xai-*)
      break
      ;;
    "")
      echo "EMPTY 再贴一次钥匙"
      ;;
    *)
      echo "NOT_A_KEY len=${#NEWKEY}"
      ;;
  esac
done
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
p.write_text("\n".join(out) + "\n")
p.chmod(0o600)
print("SAVED", len(key), key[:4])
PY
unset NEWKEY
systemctl restart shiyu-leto
echo "RESTARTED"
