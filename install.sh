#!/usr/bin/env bash
# 安装/更新 codex-auto-resume：代码复制到固定位置，systemd unit 只依赖该位置。
set -euo pipefail

src_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install_dir="${CODEX_GUARDIAN_INSTALL_DIR:-$HOME/.local/share/codex-auto-resume}"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
guardian_unit="codex-auto-resume-all.service"
web_unit="codex-auto-resume-web.service"

mkdir -p "$install_dir" "$unit_dir" "$install_dir/webui"
install -m 644 "$src_dir/guardian.py" "$src_dir/probe_transport.py" "$src_dir/web.py" "$install_dir/"
install -m 644 "$src_dir"/webui/* "$install_dir/webui/"
install -m 644 "$src_dir/$guardian_unit" "$src_dir/$web_unit" "$unit_dir/"

python3 -m py_compile "$install_dir/guardian.py" "$install_dir/probe_transport.py" "$install_dir/web.py"

systemctl --user daemon-reload
systemctl --user enable "$guardian_unit" >/dev/null
systemctl --user restart "$guardian_unit"
systemctl --user enable "$web_unit" >/dev/null
if ! systemctl --user restart "$web_unit"; then
  echo "warning: $web_unit failed to start; check: journalctl --user -u $web_unit" >&2
fi

echo "installed to: $install_dir"
systemctl --user --no-pager status "$guardian_unit" | head -8
echo
echo "web console: http://127.0.0.1:8787/ (tunnel: ssh -L 8787:127.0.0.1:8787 $(whoami)@<host>)"
