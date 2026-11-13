#!/usr/bin/env bash
# Install the polkit rule that lets the desktop user start/stop vnstat
# without a password prompt (needed for datameter's daemon toggle on Hyprland,
# where no polkit authentication agent is running).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RULE="50-datameter-vnstat.rules"
DEST="/usr/share/polkit-1/rules.d/$RULE"

echo "正在安装 $RULE -> $DEST（需要 sudo 权限）"
sudo install -m 0644 "$HERE/$RULE" "$DEST"
echo "安装完成。可用以下命令测试：systemctl stop vnstat && systemctl start vnstat"
