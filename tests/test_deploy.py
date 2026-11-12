import importlib.util
import contextlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('installer', REPO / 'deploy/manage.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class InstallerTests(unittest.TestCase):
    def test_network_validation(self):
        result = m.validate_network('8.8.8.8', '10.0.0.5', 'ens3')
        self.assertEqual(result['SIP_PUBLIC_IF'], 'ens3')
        self.assertEqual(result['SIP_BIND_IP'], '10.0.0.5')
        for public, bind, interface in [('127.0.0.1', '10.0.0.5', 'ens3'),
                                       ('8.8.8.8', '127.0.0.1', 'ens3'),
                                       ('8.8.8.8', '10.0.0.5', 'eth0; reboot')]:
            with self.assertRaises(ValueError):
                m.validate_network(public, bind, interface)

    def test_atomic_private_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'private.json'
            m.write(path, {'value': 'fixture'})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(path.with_name(path.name + '.tmp').exists())
            m.write(path, {'value': 'updated'})
            self.assertIn('updated', path.read_text())

    def test_remove_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'project'
            root.mkdir()
            outside = Path(directory) / 'other'
            outside.mkdir()
            owned = root / 'app-next'
            owned.mkdir()
            with patch.object(m, 'ROOT', root):
                with self.assertRaises(ValueError):
                    m.bounded_remove(outside)
                m.bounded_remove(owned)
            self.assertTrue(outside.exists())
            self.assertFalse(owned.exists())

    def test_uninstall_requires_owned_marker(self):
        with patch.object(m, 'read_marker', side_effect=ValueError('unmanaged')), patch.object(m, 'run') as run:
            with self.assertRaises(ValueError):
                m.uninstall()
            run.assert_not_called()

    def test_uninstall_cancel_changes_nothing(self):
        with patch.object(m, 'read_marker', return_value={}), patch('builtins.input', return_value='no'), patch.object(m, 'run') as run, patch.object(m.shutil, 'rmtree') as delete:
            m.uninstall()
            run.assert_not_called()
            delete.assert_not_called()

    def test_uninstall_deletes_all_project_data_by_default(self):
        with patch.object(m, 'ROOT', Path('/opt/sip-tunnel')), \
             patch.object(m, 'read_marker', return_value={'sysctl': {}}), \
             patch('builtins.input', return_value='DELETE'), \
             patch.object(m, 'run', return_value=Mock(stdout='', returncode=0)), \
             patch.object(m, 'remove_rules') as rules, \
             patch.object(m, 'compose') as compose, \
             patch.object(Path, 'exists', return_value=True), \
             patch.object(Path, 'is_file', return_value=False), \
             patch.object(Path, 'is_symlink', return_value=False), \
             patch.object(Path, 'resolve', return_value=Path('/opt/sip-tunnel')), \
             patch.object(Path, 'unlink') as unlink, \
             patch.object(m.shutil, 'rmtree') as delete:
            m.uninstall()
        compose.assert_called_once_with('down')
        rules.assert_called_once()
        self.assertEqual(unlink.call_count, 8)
        delete.assert_called_once_with(Path('/opt/sip-tunnel'))

    def test_removes_only_owned_firewall_jumps(self):
        calls = []
        def fake_run(args, **kwargs):
            calls.append(args)
            if args[-1] == '-S':
                return subprocess.CompletedProcess(args, 0, '-P INPUT DROP\n-A INPUT -j SIPT_INPUT\n-A INPUT -j OTHER\n', '')
            return subprocess.CompletedProcess(args, 0, '', '')
        with patch.object(m, 'run', side_effect=fake_run):
            m.remove_rules()
        self.assertFalse(any('-D' in args and 'OTHER' in args for args in calls))
        self.assertFalse(any(args[-1] == '-F' for args in calls))
        self.assertTrue(any('-D' in args and 'SIPT_INPUT' in args for args in calls))

    def test_service_files_are_scoped_and_loopback_api_is_preserved(self):
        written = {}
        with patch.object(m, 'write', side_effect=lambda path, data, *args: written.update({str(path): data})):
            m.write_services()
        self.assertEqual(len(written), 6)
        self.assertIn('OnUnitInactiveSec=30', written['/etc/systemd/system/sip-watchdog.timer'])
        self.assertIn('ReadWritePaths=/opt/sip-tunnel', written['/etc/systemd/system/sip-simple-panel.service'])
        compose = (REPO / 'deploy/compose.yaml').read_text()
        self.assertIn('HOST: "127.0.0.1"', compose)
        self.assertNotIn('latest', compose)
        self.assertNotIn('INIT_PASSWORD', compose)

    def test_source_update_keeps_runtime_data_and_one_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'backups').mkdir()
            (root / 'app').mkdir()
            (root / 'app/previous.py').write_text('previous')
            state = root / 'clients.json'
            state.write_text('{"clients":[{"name":"preserved"}]}')
            with patch.object(m, 'ROOT', root), patch.object(m, 'run'), patch.object(m, 'SOURCE_REPLACED', False):
                m.install_source()
                self.assertTrue(m.SOURCE_REPLACED)
            self.assertEqual((root / 'backups/app-rollback/previous.py').read_text(), 'previous')
            self.assertTrue((root / 'app/server.py').is_file())
            self.assertIn('preserved', state.read_text())

    def test_existing_administrator_never_prompts_or_resets(self):
        panel = Mock()
        panel.ADMIN.exists.return_value = True
        with patch('builtins.input') as prompt, patch.object(m, 'write') as write:
            m.administrator(panel)
        prompt.assert_not_called()
        write.assert_not_called()

    def test_bootstrap_removes_init_secret_and_preserves_split_route(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            panel = Mock(PUBLIC_IP='203.0.113.10')
            panel.authenticate_service.return_value.call.return_value = {}
            with patch.object(m, 'ROOT', root), patch.object(m, 'compose') as compose, patch.object(m, 'run', return_value=Mock(returncode=1)):
                m.initialize_wireguard(panel)
            credentials = json.loads((root / 'admin-credentials.json').read_text())
            self.assertGreaterEqual(len(credentials['password']), 32)
            self.assertFalse((root / 'bootstrap.env').exists())
            self.assertFalse((root / 'bootstrap.yaml').exists())
            compose.assert_any_call('up', '-d', '--force-recreate')
            calls = panel.authenticate_service.return_value.call.call_args_list
            config = next(call.args[1] for call in calls if call.args[0] == 'admin/userconfig' and len(call.args) == 2)
            self.assertEqual(config['defaultAllowedIps'], ['10.77.0.1/32'])
            self.assertEqual(config['defaultDns'], [])


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.panel = Mock()
        self.panel.mutation.side_effect = contextlib.nullcontext
        self.started = '2026-01-01T00:00:00Z'
        self.healthy = True
        self.commands = []
        def fake_run(args, **kwargs):
            self.commands.append(args)
            if args[:2] == ['docker', 'inspect']:
                return Mock(returncode=0, stdout='running ' + self.started)
            if args[:2] == ['wg', 'show']:
                return Mock(returncode=0 if self.healthy else 1, stdout='51820\n' if self.healthy else '')
            return Mock(returncode=0, stdout='')
        for item in [patch.object(m, 'ROOT', self.root), patch.object(m, 'run', side_effect=fake_run),
                     patch.object(m, 'input_rules'), patch.object(m.time, 'time', return_value=1000)]:
            item.start()
            self.addCleanup(item.stop)

    def restarted(self):
        return [args for args in self.commands if args[:2] == ['docker', 'restart']]

    def test_healthy_tunnel_never_restarts_for_absent_client_handshakes(self):
        for _ in range(5):
            m.watchdog(self.panel)
        self.assertFalse(self.restarted())
        self.panel.apply_rules.assert_called_once()
        self.assertIn(['systemctl', 'start', 'sip-wireguard', 'sip-portmap', 'sip-simple-panel', 'sip-caddy'], self.commands)

    def test_three_failures_restart_once_and_cooldown_prevents_a_loop(self):
        self.healthy = False
        for _ in range(2):
            m.watchdog(self.panel)
        self.assertFalse(self.restarted())
        m.watchdog(self.panel)
        self.assertEqual(len(self.restarted()), 1)
        for _ in range(8):
            m.watchdog(self.panel)
        self.assertEqual(len(self.restarted()), 1)
        with patch.object(m.time, 'time', return_value=1301):
            m.watchdog(self.panel)
        self.assertEqual(len(self.restarted()), 2)

    def test_recovery_and_external_container_restart_restore_rules(self):
        self.healthy = False
        m.watchdog(self.panel)
        self.healthy = True
        m.watchdog(self.panel)
        self.assertEqual(json.loads((self.root / 'watchdog.json').read_text())['failures'], 0)
        self.started = '2026-01-01T01:00:00Z'
        m.watchdog(self.panel)
        self.assertEqual(self.panel.apply_rules.call_count, 2)
        self.assertFalse(self.restarted())

    def test_failed_rule_restore_is_retried_without_restarting_container(self):
        self.panel.apply_rules.side_effect = RuntimeError('fixture firewall failure')
        with self.assertRaises(RuntimeError):
            m.watchdog(self.panel)
        self.assertFalse((self.root / 'watchdog.json').exists())
        self.panel.apply_rules.side_effect = None
        m.watchdog(self.panel)
        self.assertEqual(self.panel.apply_rules.call_count, 2)
        self.assertFalse(self.restarted())

    def test_docker_unavailable_does_not_issue_blind_restarts(self):
        with patch.object(m, 'run', return_value=Mock(returncode=1)) as run:
            with self.assertRaises(RuntimeError):
                m.watchdog(self.panel)
        self.assertEqual(run.call_count, 1)

    def test_rules_wait_for_the_interface_to_be_ready(self):
        with patch.object(m, 'run', side_effect=[Mock(returncode=1), Mock(returncode=0, stdout='51820\n')]), patch.object(m.time, 'sleep') as sleep:
            m.wait_wireguard()
        sleep.assert_called_once_with(1)

    def test_missing_interface_has_a_bounded_startup_wait(self):
        with patch.object(m, 'run', return_value=Mock(returncode=1)), patch.object(m.time, 'sleep') as sleep:
            with self.assertRaises(RuntimeError):
                m.wait_wireguard()
        self.assertEqual(sleep.call_count, 30)


if __name__ == '__main__':
    unittest.main()
