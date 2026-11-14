"""Exercise real iptables exclusively inside a disposable network namespace."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest


def inside_namespace():
    spec = importlib.util.spec_from_file_location('firewall_panel', Path(__file__).resolve().parents[1] / 'app/server.py')
    panel = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(panel)
    panel.PUBLIC_IF = 'test-wan'
    panel.BIND_IP = '10.0.0.5'
    subprocess.run(['ip', 'link', 'add', 'wg0', 'type', 'dummy'], check=True)
    state = {'clients': [{'name': 'fixture', 'ip': '10.77.0.2', 'start': 20000, 'end': 20999}]}
    panel.apply_rules(state)
    if not panel.rules_healthy(state):
        for table in ('nat', 'filter'):
            print(subprocess.run(['iptables', '-t', table, '-S'], capture_output=True, text=True, check=True).stdout)
        raise AssertionError('Fresh rules must match real kernel output')
    # Simulate a firewall reload that flushes rules while the interface stays up.
    subprocess.run(['iptables', '-t', 'nat', '-F', 'SIPT_DNAT'], check=True)
    assert not panel.rules_healthy(state), 'Missing port mappings must be detected'
    panel.apply_rules(state)
    assert panel.rules_healthy(state), 'Port mappings must recover without interface restart'
    subprocess.run(['iptables', '-t', 'filter', '-D', 'FORWARD', '-j', 'SIPT_FORWARD'], check=True)
    assert not panel.rules_healthy(state), 'A lost forwarding jump must be detected'
    panel.apply_rules(state)
    assert panel.rules_healthy(state), 'Forwarding jump must recover'
    print('PASS: real firewall inspection and recovery in an isolated network namespace')


class FirewallIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(os.geteuid() == 0, 'Requires root for an isolated network namespace')
    def test_real_kernel_rule_format_and_recovery(self):
        result = subprocess.run(['unshare', '--net', sys.executable, '-B', str(Path(__file__).resolve()), '--inside'],
                                capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    if sys.argv[1:] == ['--inside']:
        inside_namespace()
    else:
        unittest.main()
