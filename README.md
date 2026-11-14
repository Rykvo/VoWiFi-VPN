# VoWiFi VPN

Ubuntu 24.04 LTS · amd64 / arm64

隧道使用 25 秒保活与 MTU 1380。云端每 30 秒检查容器和 WireGuard 接口；连续 3 次异常后恢复容器，两次恢复至少间隔 5 分钟。客户端离线不会触发云端重启。普通页面更新不再主动重启正常运行的 VPN 容器。

同时检查 IP 转发、隧道回程校验、端口映射和本项目入口规则。防火墙重载或转发参数被重置后，自动修复本项目配置，无需重启正常运行的 VPN 容器；正常规则不反复重写。

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

## 连接诊断

```bash
sudo vowifi-vpn diagnose
sudo vowifi-vpn diagnose 11
```

第一条检查云端容器、隧道接口、转发参数和规则；第二条检查指定客户端（示例 ID 为 11），显示最近握手，并进行 5 次隧道内 ICMP 探测，输出丢包和往返时间。诊断不会重启服务或修改规则，也不会因为客户端不响应而重启整个 VPN。客户端可能屏蔽 ICMP，探测结果不能代替实际 RTP 通话质量数据。

本地设备休眠或切换网络后的恢复、SIP 注册保活和语音抖动缓冲，需要在本地程序中配置。公网持续丢包需要排查线路；云端不会擅自修改网卡队列或按未知带宽限速。

## 一键卸载

```bash
sudo vowifi-vpn uninstall
```

完整删除本项目程序、配置和全部数据，执行前需确认。Docker、Caddy 等共享依赖保留。
