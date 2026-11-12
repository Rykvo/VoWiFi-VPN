# VoWiFi VPN

Ubuntu 24.04 LTS · amd64 / arm64

隧道使用 25 秒保活与 MTU 1380。云端每 30 秒检查容器和 WireGuard 接口；连续 3 次异常后恢复容器，两次恢复至少间隔 5 分钟。客户端离线不会触发云端重启。普通页面更新不再主动重启正常运行的 VPN 容器。

这能改善服务故障后的恢复，公网线路丢包、运营商限制和本地设备休眠仍会影响连接；本地客户端需启用 WireGuard 开机启动。云端恢复记录可用 `journalctl -u vowifi-vpn-watchdog.service` 查看。

## 一键部署

```bash
curl -fsSL https://raw.githubusercontent.com/Rykvo/VoWiFi-VPN/main/deploy.sh | sudo bash -s -- install
```

在 SSH 终端执行，按提示填写管理员账号和密码；完成后访问 `https://服务器IP/gly`。

## 一键更新

```bash
sudo vowifi-vpn update
```

## 一键卸载

```bash
sudo vowifi-vpn uninstall
```

完整删除本项目程序、配置和全部数据，执行前需确认。Docker、Caddy 等共享依赖保留。
