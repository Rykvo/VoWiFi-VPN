#!/usr/bin/env bash
# VoWiFi VPN command
set -Eeuo pipefail
umask 077

cleanup() {
  if [[ ${workspace:-} == /tmp/vowifi-vpn.* && -d $workspace && ! -L $workspace ]]; then
    rm -rf -- "$workspace"
  fi
}

main() {
  local action=${1:-install}
  case "$action" in
    install|update|uninstall) [[ $# -le 1 ]] || { echo '参数错误。'; return 1; } ;;
    --help|-h) echo 'sudo vowifi-vpn {install|update|uninstall}'; return ;;
    *) echo '支持 install、update、uninstall。'; return 1 ;;
  esac
  [[ $EUID == 0 ]] || { echo '请使用 sudo 执行。'; return 1; }
  # Read prompts from SSH's terminal, never from the downloaded script stream.
  if ! ( : </dev/tty ) 2>/dev/null; then
    echo '请在交互式 SSH 终端执行；远程命令请使用 ssh -t。'
    return 1
  fi
  local installed_root=''
  if [[ -f /opt/vowifi-vpn/.vowifi-managed.json ]]; then
    installed_root=/opt/vowifi-vpn
  fi
  # Compatibility for installations made before the project was renamed.
  if [[ -f /opt/sip-tunnel/.rykvo-managed.json ]]; then
    [[ -z $installed_root ]] || { echo '发现两套安装，请先核对实例。'; return 1; }
    installed_root=/opt/sip-tunnel
  fi
  if [[ $action == uninstall ]]; then
    [[ -n $installed_root && -f $installed_root/manager.py ]] || {
      echo '未发现此安装器管理的实例。'; return 1;
    }
    python3 -B "$installed_root/manager.py" uninstall </dev/tty
    return
  fi
  if [[ $action == update && -z $installed_root ]]; then
    echo '请先执行 install。'; return 1
  fi
  command -v curl >/dev/null && command -v tar >/dev/null || {
    echo '请先安装 curl 和 tar。'; return 1;
  }
  workspace=$(mktemp -d /tmp/vowifi-vpn.XXXXXXXX)
  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  curl --proto '=https' --tlsv1.2 -fsSL --retry 3 \
    https://codeload.github.com/Rykvo/VoWiFi-VPN/tar.gz/refs/heads/main \
    -o "$workspace/source.tar.gz" || {
      echo '下载失败，请检查网络及仓库是否已公开。'; return 1;
    }
  mkdir "$workspace/source"
  tar -xzf "$workspace/source.tar.gz" --strip-components=1 --no-same-owner -C "$workspace/source"
  [[ -f $workspace/source/install.sh && -f $workspace/source/deploy/manage.py ]] || {
    echo '安装包不完整。'; return 1;
  }
  bash "$workspace/source/install.sh" </dev/tty
}

main "$@"
