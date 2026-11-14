#!/usr/bin/env python3
"""Manage project-owned services and data."""
import getpass
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path('/opt/vowifi-vpn')
LEGACY_ROOT = Path('/opt/sip-tunnel')
LEGACY_PROJECT = 'Rykvo-Voice-SIP'
LEGACY_MARKER = '.rykvo-managed.json'


def installation_root():
    current = DEFAULT_ROOT / '.vowifi-managed.json'
    legacy = LEGACY_ROOT / LEGACY_MARKER
    if current.is_file() and legacy.is_file():
        raise ValueError('发现两套安装，请先核对实例；不会自动合并数据。')
    return LEGACY_ROOT if legacy.is_file() else DEFAULT_ROOT


ROOT = installation_root()
MARKER = ROOT / (LEGACY_MARKER if ROOT == LEGACY_ROOT else '.vowifi-managed.json')
CLI = Path('/usr/local/bin/vowifi-vpn')
CLI_MARKER = '# VoWiFi VPN command'
LEGACY_CLI = Path('/usr/local/bin/rykvo-sip')
LEGACY_CLI_MARKER = '# Rykvo Voice SIP command'
SOURCE_REPLACED = False
SERVICES = ('vowifi-vpn-caddy', 'vowifi-vpn-panel', 'vowifi-vpn-portmap', 'vowifi-vpn-wireguard')
WATCHDOG_UNITS = ('vowifi-vpn-watchdog.timer', 'vowifi-vpn-watchdog.service')
CHAINS = {'nat': ('SIPT_DNAT', 'SIPT_SNAT'), 'filter': ('SIPT_FORWARD', 'SIPT_INPUT')}
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def runtime_name(value):
    """Keep existing runtime identities so rebranding never recreates a live VPN."""
    if ROOT != LEGACY_ROOT:
        return value
    replacements = {
        '/opt/vowifi-vpn': str(LEGACY_ROOT),
        'vowifi-vpn-wg-easy': 'sip-wg-easy',
        'vowifi-vpn-panel': 'sip-simple-panel',
        'vowifi-vpn-wireguard': 'sip-wireguard',
        'vowifi-vpn-portmap': 'sip-portmap',
        'vowifi-vpn-caddy': 'sip-caddy',
        'vowifi-vpn-watchdog': 'sip-watchdog',
        'name: vowifi-vpn': 'name: sip-tunnel',
        '90-vowifi-vpn.conf': '90-rykvo-sip.conf',
        'modules-load.d/vowifi-vpn.conf': 'modules-load.d/rykvo-sip.conf',
    }
    for current, legacy in replacements.items():
        value = value.replace(current, legacy)
    return value


def owned_cli(path):
    return path.is_file() and not path.is_symlink() and any(
        marker in path.read_text() for marker in (CLI_MARKER, LEGACY_CLI_MARKER))


def install_commands():
    script = (SOURCE / 'deploy.sh').read_text()
    # Do not overwrite an unrelated command that happens to use an old name.
    update_alias = owned_cli(LEGACY_CLI)
    write(CLI, script, 0o755)
    if update_alias:
        write(LEGACY_CLI, script, 0o755)


def run(args, *, check=True, capture=True, timeout=900):
    return subprocess.run([runtime_name(arg) if isinstance(arg, str) else arg for arg in args],
                          check=check, text=True, capture_output=capture, timeout=timeout)


def write(path, value, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    temporary = path.with_name(path.name + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(descriptor, 'w') as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def read_marker():
    if ROOT.is_symlink() or ROOT.resolve() not in (DEFAULT_ROOT, LEGACY_ROOT):
        raise ValueError('安装目录路径异常。')
    if not MARKER.is_file():
        raise ValueError('未发现本项目安装标记；现有目录保持不变。')
    value = json.loads(MARKER.read_text())
    if value.get('project') not in ('VoWiFi-VPN', LEGACY_PROJECT) or value.get('version') != 1:
        raise ValueError('安装标记不匹配。')
    return value


def validate_network(public_ip, bind_ip, interface):
    public = ipaddress.IPv4Address(public_ip)
    bind = ipaddress.IPv4Address(bind_ip)
    if not public.is_global or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', interface):
        raise ValueError('需要有效的公网 IPv4 和出口网卡。')
    if bind.is_loopback or bind.is_multicast or bind.is_unspecified:
        raise ValueError('出口网卡地址无效。')
    return {'SIP_PUBLIC_IP': str(public), 'SIP_BIND_IP': str(bind), 'SIP_PUBLIC_IF': interface}


def preflight():
    if os.geteuid() != 0:
        raise ValueError('请使用 sudo 执行。')
    release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    if release.get('ID', '').strip('"') != 'ubuntu' or release.get('VERSION_ID', '').strip('"') != '24.04':
        raise ValueError('此版本部署脚本支持 Ubuntu 24.04 LTS。')
    if platform.machine() not in ('x86_64', 'aarch64') or not Path('/run/systemd/system').exists():
        raise ValueError('需要 amd64/arm64、systemd 和支持 WireGuard 的云主机。')
    if ROOT.is_symlink():
        raise ValueError('安装目录是符号链接，已停止。')
    if CLI.is_symlink() or (CLI.exists() and not owned_cli(CLI)):
        raise ValueError('vowifi-vpn 命令已被其他程序占用。')
    if MARKER.exists():
        read_marker()
        print('环境检查通过，将保留已有数据。')
        return
    if ROOT.exists() and any(ROOT.iterdir()):
        raise ValueError('安装目录已有其他部署。请先备份并迁移；脚本不会覆盖。')
    for port, kind in [(80, socket.SOCK_STREAM), (443, socket.SOCK_STREAM),
                       (2019, socket.SOCK_STREAM), (51821, socket.SOCK_STREAM),
                       (51822, socket.SOCK_STREAM), (51820, socket.SOCK_DGRAM)]:
        with socket.socket(socket.AF_INET, kind) as listener:
            try:
                listener.bind(('0.0.0.0', port))
            except OSError as error:
                raise ValueError(f'端口 {port} 已占用；现有业务保持不变。') from error
    if Path('/sys/class/net/wg0').exists():
        raise ValueError('wg0 已存在，选择没有其他 WireGuard 业务的主机。')
    if shutil.which('docker') and run(['docker', 'container', 'inspect', 'vowifi-vpn-wg-easy'], check=False).returncode == 0:
        raise ValueError('vowifi-vpn-wg-easy 容器已存在，已停止以保护现有数据。')
    for service in SERVICES:
        if Path(runtime_name(f'/etc/systemd/system/{service}.service')).exists():
            raise ValueError(f'{service} 已存在，已停止以保护现有服务。')
    for unit in WATCHDOG_UNITS:
        if Path(runtime_name(f'/etc/systemd/system/{unit}')).exists():
            raise ValueError(f'{unit} 已存在，已停止以保护现有服务。')
    print('环境检查通过。')


def detect_network():
    routes = json.loads(run(['ip', '-j', '-4', 'route', 'get', '1.1.1.1']).stdout)
    route = routes[0]
    interface, bind_ip = route['dev'], route['prefsrc']
    public_ip = bind_ip
    if not ipaddress.IPv4Address(bind_ip).is_global:
        try:
            with OPENER.open('https://api.ipify.org', timeout=10) as response:
                public_ip = response.read(64).decode().strip()
        except (urllib.error.URLError, TimeoutError):
            public_ip = ''
    entered = input(f'公网 IPv4 [{public_ip}]: ').strip() or public_ip
    settings = validate_network(entered, bind_ip, interface)
    for route in json.loads(run(['ip', '-j', '-4', 'route', 'show']).stdout):
        destination = route.get('dst', 'default')
        if destination != 'default' and ipaddress.ip_network(destination, strict=False).overlaps(ipaddress.ip_network('10.77.0.0/24')):
            raise ValueError('10.77.0.0/24 与现有路由冲突，已停止。')
    return settings


def panel_module(path=None):
    spec = importlib.util.spec_from_file_location('vowifi_panel', path or ROOT / 'app/server.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compose(*args, bootstrap=False):
    command = ['docker', 'compose', '--project-directory', str(ROOT), '-f', str(ROOT / 'compose.yaml')]
    if bootstrap:
        command += ['-f', str(ROOT / 'bootstrap.yaml')]
    return run(command + list(args), capture=False)


def bounded_remove(path):
    if path.is_symlink() or path.resolve().parent not in (ROOT.resolve(), (ROOT / 'backups').resolve()):
        raise ValueError('清理路径超出项目目录。')
    if path.exists():
        shutil.rmtree(path)


def install_source():
    global SOURCE_REPLACED
    # Run tests before stopping the existing app.
    run([sys.executable, '-B', str(SOURCE / 'tests/test_server.py')], capture=False)
    stage = ROOT / 'app-next'
    bounded_remove(stage)
    shutil.copytree(SOURCE / 'app', stage, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for path in stage.rglob('*'):
        path.chmod(0o700 if path.is_dir() else 0o600)
    run(['systemctl', 'stop', 'vowifi-vpn-panel'], check=False)
    old = ROOT / 'app'
    backup = ROOT / 'backups/app-rollback'
    if old.exists():
        bounded_remove(backup)
        old.rename(backup)
    stage.rename(old)
    SOURCE_REPLACED = True


def input_rules():
    prefix = ['iptables', '-w', '10']
    if run(prefix + ['-S', 'SIPT_INPUT'], check=False).returncode:
        run(prefix + ['-N', 'SIPT_INPUT'])
    run(prefix + ['-F', 'SIPT_INPUT'])
    for protocol, ports in [('tcp', '80,443'), ('udp', '51820')]:
        run(prefix + ['-A', 'SIPT_INPUT', '-p', protocol, '-m', 'multiport', '--dports', ports, '-j', 'ACCEPT'])
    run(prefix + ['-A', 'SIPT_INPUT', '-j', 'RETURN'])
    if run(prefix + ['-C', 'INPUT', '-j', 'SIPT_INPUT'], check=False).returncode:
        run(prefix + ['-I', 'INPUT', '1', '-j', 'SIPT_INPUT'])


def input_rules_healthy():
    checks = [
        ['INPUT', '-j', 'SIPT_INPUT'],
        ['SIPT_INPUT', '-p', 'tcp', '-m', 'multiport', '--dports', '80,443', '-j', 'ACCEPT'],
        ['SIPT_INPUT', '-p', 'udp', '-m', 'multiport', '--dports', '51820', '-j', 'ACCEPT'],
        ['SIPT_INPUT', '-j', 'RETURN'],
    ]
    return all(run(['iptables', '-w', '2', '-C'] + rule, check=False, timeout=5).returncode == 0
               for rule in checks)


def forwarding_healthy():
    return all(run(['sysctl', '-n', key], timeout=5).stdout.strip() == value for key, value in (
        ('net.ipv4.ip_forward', '1'), ('net.ipv4.conf.all.src_valid_mark', '1'),
        ('net.ipv4.conf.wg0.rp_filter', '2')))


def initialize_wireguard(panel):
    credentials = ROOT / 'admin-credentials.json'
    if not credentials.exists():
        write(credentials, {'username': 'vowifi-service', 'password': secrets.token_urlsafe(32)})
    account = json.loads(credentials.read_text())
    write(ROOT / 'bootstrap.env', '\n'.join([
        'INIT_ENABLED=true', 'INIT_USERNAME=' + account['username'], 'INIT_PASSWORD=' + account['password'],
        'INIT_HOST=' + panel.PUBLIC_IP, 'INIT_PORT=51820', 'INIT_IPV4_CIDR=10.77.0.0/24',
        'INIT_IPV6_CIDR=fd77:77:77::/64', 'INIT_ALLOWED_IPS=10.77.0.1/32', '']))
    write(ROOT / 'bootstrap.yaml', 'services:\n  wg-easy:\n    env_file:\n      - bootstrap.env\n')
    compose('up', '-d', bootstrap=True)
    for attempt in range(45):
        try:
            upstream = panel.authenticate_service()
            break
        except (panel.UserError, urllib.error.URLError, OSError):
            if attempt == 44:
                raise RuntimeError('WireGuard 管理服务尚未就绪；检查容器日志后重试。')
            time.sleep(2)
    config = upstream.call('admin/userconfig')
    config.update(defaultDns=[], defaultAllowedIps=['10.77.0.1/32'], defaultPersistentKeepalive=25, defaultMtu=1380)
    upstream.call('admin/userconfig', config)
    upstream.call('admin/hooks', dict(preUp='', postUp='', preDown='', postDown=''))
    upstream.call('session', method='DELETE')
    # Remove only wg-easy's first-start broad defaults, never flush shared chains.
    rules = [('nat', ['POSTROUTING', '-s', '10.77.0.0/24', '-o', 'eth0', '-j', 'MASQUERADE']),
             ('filter', ['INPUT', '-p', 'udp', '-m', 'udp', '--dport', '51820', '-j', 'ACCEPT']),
             ('filter', ['FORWARD', '-i', 'wg0', '-j', 'ACCEPT']),
             ('filter', ['FORWARD', '-o', 'wg0', '-j', 'ACCEPT'])]
    for table, rule in rules:
        prefix = ['docker', 'exec', 'vowifi-vpn-wg-easy', 'iptables', '-w', '10', '-t', table]
        while run(prefix + ['-C'] + rule, check=False).returncode == 0:
            run(prefix + ['-D'] + rule)
    compose('up', '-d', '--force-recreate')
    (ROOT / 'bootstrap.env').unlink()
    (ROOT / 'bootstrap.yaml').unlink()


def administrator(panel):
    if panel.ADMIN.exists():
        print('保留现有管理员账号和密码。')
        return
    print('设置管理员账号（仅保存在此服务器）：')
    while True:
        username = input('账号: ').strip()
        password = getpass.getpass('密码（8–128 位）: ')
        confirm = getpass.getpass('确认密码: ')
        if re.fullmatch(r'[\w.@-]{2,48}', username) and 8 <= len(password) <= 128 and password == confirm:
            break
        print('请检查账号格式、密码长度和两次输入。')
    write(panel.ADMIN, {'username': username, 'password': panel.hash_password(password), 'revision': 0})


def write_services():
    common = ('Restart=on-failure\nRestartSec=3\nNoNewPrivileges=true\nProtectSystem=strict\n'
              'ProtectHome=true\nPrivateTmp=true\nUMask=0077\nReadWritePaths=/opt/vowifi-vpn\n')
    definitions = {
        'vowifi-vpn-wireguard': ('After=network-online.target docker.service\nRequires=docker.service\n',
                         'Type=oneshot\nRemainAfterExit=yes\nWorkingDirectory=/opt/vowifi-vpn\n'
                         'ExecStart=/usr/bin/docker compose up -d\nExecStop=/usr/bin/docker compose down\n'),
        'vowifi-vpn-portmap': ('After=vowifi-vpn-wireguard.service\nRequires=vowifi-vpn-wireguard.service\n',
                        'Type=oneshot\nRemainAfterExit=yes\nEnvironmentFile=/opt/vowifi-vpn/runtime.env\n'
                        'Environment=PYTHONDONTWRITEBYTECODE=1\n'
                        'ExecStart=/usr/bin/python3 -B /opt/vowifi-vpn/manager.py rules\n'),
        'vowifi-vpn-panel': ('After=vowifi-vpn-portmap.service\nRequires=vowifi-vpn-portmap.service\n',
                             'EnvironmentFile=/opt/vowifi-vpn/runtime.env\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n'
                             'ExecStart=/usr/bin/python3 -B /opt/vowifi-vpn/app/server.py\n'
                             'CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW\n' + common),
        'vowifi-vpn-caddy': ('After=vowifi-vpn-panel.service\nRequires=vowifi-vpn-panel.service\n',
                     'Environment=XDG_DATA_HOME=/opt/vowifi-vpn/caddy-data\n'
                     'Environment=XDG_CONFIG_HOME=/opt/vowifi-vpn/caddy-config\n'
                     'ExecStart=/usr/bin/caddy run --config /opt/vowifi-vpn/Caddyfile\n'
                     'ExecReload=/usr/bin/caddy reload --config /opt/vowifi-vpn/Caddyfile\n'
                     'CapabilityBoundingSet=CAP_NET_BIND_SERVICE\nAmbientCapabilities=CAP_NET_BIND_SERVICE\n' + common),
    }
    for name, (dependencies, service) in definitions.items():
        write(Path(runtime_name(f'/etc/systemd/system/{name}.service')),
              runtime_name(f'[Unit]\nDescription=VoWiFi VPN - {name}\nWants=network-online.target\n{dependencies}\n'
              f'[Service]\n{service}\n[Install]\nWantedBy=multi-user.target\n'), 0o644)
    write(Path(runtime_name('/etc/systemd/system/vowifi-vpn-watchdog.service')),
          runtime_name('[Unit]\nDescription=VoWiFi VPN tunnel recovery\nAfter=docker.service\n'
          '[Service]\nType=oneshot\nEnvironmentFile=/opt/vowifi-vpn/runtime.env\n'
          'ExecStart=/usr/bin/python3 -B /opt/vowifi-vpn/manager.py watchdog\nTimeoutStartSec=90\n'), 0o644)
    write(Path(runtime_name('/etc/systemd/system/vowifi-vpn-watchdog.timer')),
          '[Unit]\nDescription=VoWiFi VPN tunnel checks\n'
          '[Timer]\nOnBootSec=90\nOnUnitInactiveSec=30\nAccuracySec=5\n'
          '[Install]\nWantedBy=timers.target\n', 0o644)


def watchdog(panel):
    # Missing client handshakes are never a reason to restart the shared tunnel.
    status = run(['docker', 'inspect', '--format', '{{.State.Status}} {{.State.StartedAt}}',
                  'vowifi-vpn-wg-easy'], check=False, timeout=10)
    if status.returncode:
        raise RuntimeError('无法检查隧道容器，请检查 Docker 服务。')
    container_status, started = status.stdout.strip().split(maxsplit=1)
    interface = run(['wg', 'show', 'wg0', 'listen-port'], check=False, timeout=10)
    healthy = container_status == 'running' and interface.returncode == 0 and interface.stdout.strip() == '51820'
    path = ROOT / 'watchdog.json'
    now = time.time()
    with panel.mutation():
        health = json.loads(path.read_text()) if path.exists() else {'failures': 0, 'lastRestart': 0}
        if healthy:
            # A firewall reload or sysctl reset can break calls while wg0 stays up.
            records = panel.load_state()
            forwarding = forwarding_healthy()
            if (health.get('started') != started or health['failures'] or not forwarding
                    or not panel.rules_healthy(records) or not input_rules_healthy()):
                if not forwarding:
                    run(['sysctl', '-w', 'net.ipv4.ip_forward=1',
                         'net.ipv4.conf.all.src_valid_mark=1'], timeout=10)
                panel.apply_rules(records)
                input_rules()
                health['lastRepair'] = now
                print('已恢复隧道转发配置，未重启 VPN 容器。', flush=True)
            health.update(failures=0, started=started)
        else:
            health['failures'] += 1
        recover = not healthy and health['failures'] >= 3 and now - health['lastRestart'] >= 300
        if recover:
            health.update(failures=0, lastRestart=now)
            health.pop('started', None)
        write(path, health)
    if healthy:
        # Requires=docker stops dependents when Docker goes away; start them again.
        run(['systemctl', 'start', 'vowifi-vpn-wireguard', 'vowifi-vpn-portmap', 'vowifi-vpn-panel', 'vowifi-vpn-caddy'], timeout=45)
    if recover:
        print('隧道接口连续异常，正在恢复容器。', flush=True)
        run(['docker', 'restart', 'vowifi-vpn-wg-easy'], timeout=45)


def diagnose(panel, client_id=None):
    """Read-only local checks; optional ICMP probing never restarts a tunnel."""
    records = panel.load_state()
    client = None
    if client_id is not None:
        if not re.fullmatch(r'[0-9]+', client_id):
            raise ValueError('客户端 ID 必须为整数。')
        client = next((row for row in records['clients'] if row['id'] == int(client_id)), None)
        if client is None:
            raise ValueError('没有这个客户端。')
        address = ipaddress.IPv4Address(client['ip'])
        if address not in ipaddress.ip_network('10.77.0.0/24') or int(str(address).split('.')[-1]) not in range(2, 255):
            raise ValueError('客户端隧道地址异常。')
    container = run(['docker', 'inspect', '--format', '{{.State.Status}}', 'vowifi-vpn-wg-easy'], check=False, timeout=5)
    interface = run(['wg', 'show', 'wg0', 'listen-port'], check=False, timeout=5)
    ready = interface.returncode == 0 and interface.stdout.strip() == '51820'
    print('容器：' + (container.stdout.strip() if container.returncode == 0 else '检查失败'))
    print('WireGuard 接口：' + ('正常' if ready else '异常'))
    print('转发参数：' + ('正常' if ready and forwarding_healthy() else '异常'))
    print('端口映射：' + ('正常' if panel.rules_healthy(records) else '异常'))
    print('入口规则：' + ('正常' if input_rules_healthy() else '异常'))
    if client is None:
        print('检查指定客户端：sudo vowifi-vpn diagnose 客户端ID')
        return
    print(f'客户端：{client["id"]}（{client["ip"]}）')
    if not ready:
        print('接口异常，跳过客户端探测。')
        return
    allowed = run(['wg', 'show', 'wg0', 'allowed-ips'], check=False, timeout=5)
    peer = next((parts[0] for line in allowed.stdout.splitlines()
                 if len(parts := line.split(maxsplit=1)) == 2 and client['ip'] + '/32' in re.split(r'[,\s]+', parts[1])), None) if allowed.returncode == 0 else None
    handshakes = run(['wg', 'show', 'wg0', 'latest-handshakes'], check=False, timeout=5)
    stamp = next((int(parts[1]) for line in handshakes.stdout.splitlines()
                  if len(parts := line.split()) == 2 and parts[0] == peer and parts[1].isdigit()), 0) if handshakes.returncode == 0 else 0
    print('最近握手：' + (f'{max(0, int(time.time()) - stamp)} 秒前' if stamp else '无记录'))
    try:
        probe = run(['ping', '-n', '-I', 'wg0', '-c', '5', '-W', '1', client['ip']], check=False, timeout=8)
        print(probe.stdout.strip())
        if probe.returncode:
            print('探测未全部成功，也可能客户端屏蔽 ICMP；不能仅凭此判断通话失败。')
    except (OSError, subprocess.SubprocessError):
        print('探测无法完成，请检查 ping 工具与本地防火墙。')
    print('以上仅检查隧道；通话的 RTP 丢包和抖动需要在本地 SIP 软件中查看。')


def wait_wireguard():
    for attempt in range(30):
        interface = run(['wg', 'show', 'wg0', 'listen-port'], check=False, timeout=5)
        if interface.returncode == 0 and interface.stdout.strip() == '51820':
            return
        time.sleep(1)
    raise RuntimeError('WireGuard 隧道接口尚未就绪，请检查容器日志。')


def install():
    if MARKER.exists():
        state = read_marker()
    else:
        network = detect_network()
        ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        state = {'project': 'VoWiFi-VPN', 'version': 1, 'network': network, 'initialized': False,
                 'sysctl': {key: run(['sysctl', '-n', key]).stdout.strip()
                            for key in ('net.ipv4.ip_forward', 'net.ipv4.conf.all.src_valid_mark')}}
        write(MARKER, state)
    state['project'] = 'VoWiFi-VPN'
    write(MARKER, state)
    ROOT.chmod(0o700)
    for directory in ('data', 'backups', 'caddy-data', 'caddy-config'):
        (ROOT / directory).mkdir(mode=0o700, exist_ok=True)
    os.environ.update(state['network'], SIP_ROOT=str(ROOT))
    write(ROOT / 'runtime.env', f'SIP_ROOT={ROOT}\n' + ''.join(f'{key}={value}\n' for key, value in state['network'].items()))
    write(ROOT / 'compose.yaml', runtime_name((SOURCE / 'deploy/compose.yaml').read_text()))
    compose('pull')
    run(['systemctl', 'stop', *WATCHDOG_UNITS], check=False)
    install_source()
    shutil.copyfile(__file__, ROOT / 'manager.py')
    (ROOT / 'manager.py').chmod(0o600)
    panel = panel_module()
    for name, value in [('clients.json', {'clients': []}), ('settings.json', {'sipDomain': '', 'panelDomain': ''}),
                        ('enrollments.json', {}), ('panel-sessions.json', {})]:
        if not (ROOT / name).exists():
            write(ROOT / name, value)
    write(Path(runtime_name('/etc/sysctl.d/90-vowifi-vpn.conf')), 'net.ipv4.ip_forward=1\nnet.ipv4.conf.all.src_valid_mark=1\n', 0o644)
    write(Path(runtime_name('/etc/modules-load.d/vowifi-vpn.conf')), 'wireguard\n', 0o644)
    run(['sysctl', '-p', '/etc/sysctl.d/90-vowifi-vpn.conf'])
    input_rules()
    if not state['initialized']:
        initialize_wireguard(panel)
        state['initialized'] = True
        write(MARKER, state)
    else:
        compose('up', '-d')
    administrator(panel)
    settings = panel.load_settings()
    write(ROOT / 'Caddyfile', panel.caddy_config(settings['panelDomain'], settings['sipDomain']))
    run(['caddy', 'validate', '--config', str(ROOT / 'Caddyfile')])
    write_services()
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', *SERVICES])
    run(['systemctl', 'start', 'vowifi-vpn-wireguard'])
    run(['systemctl', 'restart', 'vowifi-vpn-portmap', 'vowifi-vpn-panel', 'vowifi-vpn-caddy'])
    run(['systemctl', 'enable', '--now', 'vowifi-vpn-watchdog.timer'])
    for attempt in range(30):
        try:
            with OPENER.open('http://127.0.0.1:51822/gly', timeout=3) as response:
                assert response.status == 200
            panel.authenticate_service().call('session', method='DELETE')
            break
        except (OSError, urllib.error.URLError, panel.UserError):
            if attempt == 29:
                raise RuntimeError('服务启动检查未通过，请检查 systemd 日志。')
            time.sleep(2)
    run(['systemctl', 'is-active', *SERVICES], capture=False)
    install_commands()
    print(f'部署完成：https://{settings["panelDomain"] or panel.PUBLIC_IP}/gly')
    print('HTTPS 证书自动申请；请确保云防火墙已开放 TCP 80/443、UDP 51820 和客户端端口段。')


def remove_rules():
    for table, chains in CHAINS.items():
        prefix = ['iptables', '-w', '10', '-t', table]
        for line in run(prefix + ['-S']).stdout.splitlines():
            rule = shlex.split(line)
            if rule[:1] == ['-A'] and '-j' in rule and rule[rule.index('-j') + 1] in chains:
                run(prefix + ['-D'] + rule[1:])
        for chain in chains:
            if run(prefix + ['-S', chain], check=False).returncode == 0:
                run(prefix + ['-F', chain])
                run(prefix + ['-X', chain])


def uninstall():
    state = read_marker()
    if input('将完整删除本项目程序、配置、账号、密钥、证书、客户端数据和备份。输入 DELETE 确认: ') != 'DELETE':
        print('已取消。')
        return
    run(['systemctl', 'disable', '--now', *WATCHDOG_UNITS, *SERVICES], check=False)
    if (ROOT / 'compose.yaml').exists():
        compose('down')
    remove_rules()
    for service in SERVICES:
        Path(runtime_name(f'/etc/systemd/system/{service}.service')).unlink(missing_ok=True)
    for unit in WATCHDOG_UNITS:
        Path(runtime_name(f'/etc/systemd/system/{unit}')).unlink(missing_ok=True)
    for name in ['/etc/sysctl.d/90-vowifi-vpn.conf', '/etc/modules-load.d/vowifi-vpn.conf']:
        Path(runtime_name(name)).unlink(missing_ok=True)
    other_containers = run(['docker', 'ps', '-q']).stdout.strip()
    other_wireguard = run(['wg', 'show', 'interfaces']).stdout.strip()
    if not other_containers and not other_wireguard:
        for key, value in state['sysctl'].items():
            if run(['sysctl', '-n', key]).stdout.strip() == '1':
                run(['sysctl', '-w', key + '=' + value])
    run(['systemctl', 'daemon-reload'])
    if ROOT.resolve() not in (DEFAULT_ROOT, LEGACY_ROOT) or ROOT.is_symlink():
        raise ValueError('删除路径异常。')
    shutil.rmtree(ROOT)
    for command in (CLI, LEGACY_CLI):
        if owned_cli(command):
            command.unlink()
    print('本项目已完整卸载。Docker、Caddy 等共享依赖保留。')


def main():
    if os.geteuid() != 0:
        raise ValueError('需要 root 权限。')
    command = sys.argv[1]
    if command == 'preflight':
        preflight()
    elif command == 'install':
        was_active = run(['systemctl','is-active','--quiet','vowifi-vpn-panel'],check=False).returncode == 0
        try:
            install()
        except (Exception, KeyboardInterrupt):
            backup = ROOT/'backups/app-rollback'
            if SOURCE_REPLACED and backup.exists():
                run(['systemctl','stop','vowifi-vpn-panel'],check=False)
                bounded_remove(ROOT/'app')
                backup.rename(ROOT/'app')
                if was_active:
                    run(['systemctl','start','vowifi-vpn-panel'],check=False)
            run(['systemctl','start','vowifi-vpn-watchdog.timer'],check=False)
            raise
    elif command in ('rules', 'watchdog', 'diagnose'):
        state = read_marker()
        os.environ.update(state['network'], SIP_ROOT=str(ROOT))
        panel = panel_module()
        if command == 'diagnose':
            if len(sys.argv) > 3:
                raise ValueError('用法：vowifi-vpn diagnose [客户端ID]')
            diagnose(panel, sys.argv[2] if len(sys.argv) == 3 else None)
            return
        if command == 'watchdog':
            watchdog(panel)
            return
        wait_wireguard()
        with panel.mutation():
            panel.apply_rules(panel.load_state())
            input_rules()
    elif command == 'uninstall':
        if len(sys.argv) != 2:
            raise ValueError('卸载无需额外参数。')
        uninstall()
    else:
        raise ValueError('未知操作。')


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print(f'操作未完成：{error or "已取消"}。请排查后重试。', file=sys.stderr)
        sys.exit(1)
