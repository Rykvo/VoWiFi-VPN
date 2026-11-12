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
        with patch.object(m, 'ROOT', Path('/opt/vowifi-vpn')), \
             patch.object(m, 'read_marker', return_value={'sysctl': {}}), \
             patch('builtins.input', return_value='DELETE'), \
             patch.object(m, 'run', return_value=Mock(stdout='', returncode=0)), \
             patch.object(m, 'remove_rules') as rules, \
             patch.object(m, 'compose') as compose, \
             patch.object(Path, 'exists', return_value=True), \
             patch.object(Path, 'is_file', return_value=False), \
             patch.object(Path, 'is_symlink', return_value=False), \
             patch.object(Path, 'resolve', return_value=Path('/opt/vowifi-vpn')), \
             patch.object(Path, 'unlink') as unlink, \
             patch.object(m.shutil, 'rmtree') as delete:
            m.uninstall()
        compose.assert_called_once_with('down')
        rules.assert_called_once()
        self.assertEqual(unlink.call_count, 8)
        delete.assert_called_once_with(Path('/opt/vowifi-vpn'))

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
        self.assertIn('OnUnitInactiveSec=30', written['/etc/systemd/system/vowifi-vpn-watchdog.timer'])
        self.assertIn('ReadWritePaths=/opt/vowifi-vpn', written['/etc/systemd/system/vowifi-vpn-panel.service'])
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


class RebrandingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.current = self.root / 'vowifi-vpn'
        self.legacy = self.root / 'sip-tunnel'
        for name, value in [('DEFAULT_ROOT', self.current), ('LEGACY_ROOT', self.legacy)]:
            item = patch.object(m, name, value)
            item.start()
            self.addCleanup(item.stop)

    def marker(self, root, legacy=False, project=None):
        root.mkdir(parents=True, exist_ok=True)
        marker = root / (m.LEGACY_MARKER if legacy else '.vowifi-managed.json')
        marker.write_text(json.dumps({'project': project or ('Rykvo-Voice-SIP' if legacy else 'VoWiFi-VPN'),
                                      'version': 1, 'initialized': True}))
        return marker

    def test_new_installation_uses_new_directory_and_names(self):
        self.assertEqual(m.installation_root(), self.current)
        self.marker(self.current)
        self.assertEqual(m.installation_root(), self.current)
        with patch.object(m, 'ROOT', self.current):
            self.assertEqual(m.runtime_name('vowifi-vpn-panel'), 'vowifi-vpn-panel')

    def test_legacy_installation_keeps_its_directory_and_accepts_both_markers(self):
        marker = self.marker(self.legacy, legacy=True)
        data = self.legacy / 'clients.json'
        data.write_text('{"clients":[{"id":11}]}')
        self.assertEqual(m.installation_root(), self.legacy)
        with patch.object(m, 'ROOT', self.legacy), patch.object(m, 'MARKER', marker):
            self.assertEqual(m.read_marker()['project'], 'Rykvo-Voice-SIP')
            marker.write_text(json.dumps({'project':'VoWiFi-VPN','version':1,'initialized':True}))
            self.assertEqual(m.read_marker()['project'], 'VoWiFi-VPN')
        self.assertEqual(data.read_text(), '{"clients":[{"id":11}]}')

    def test_two_managed_installations_are_never_silently_combined(self):
        self.marker(self.current)
        self.marker(self.legacy, legacy=True)
        with self.assertRaisesRegex(ValueError, '两套安装'):
            m.installation_root()

    def test_legacy_service_files_keep_original_paths_and_dependencies(self):
        written = {}
        with patch.object(m, 'ROOT', self.legacy), patch.object(m, 'write', side_effect=lambda path, data, *args: written.update({str(path):data})):
            m.write_services()
            compose = m.runtime_name((REPO / 'deploy/compose.yaml').read_text())
        self.assertIn('name: sip-tunnel', compose)
        self.assertIn('container_name: sip-wg-easy', compose)
        panel = written['/etc/systemd/system/sip-simple-panel.service']
        self.assertIn('Requires=sip-portmap.service', panel)
        self.assertIn(f'ReadWritePaths={self.legacy}', panel)
        self.assertIn('Description=VoWiFi VPN', panel)
        self.assertNotIn('/opt/vowifi-vpn', ''.join(written.values()))
        self.assertIn('/etc/systemd/system/sip-watchdog.timer', written)

    def test_runtime_commands_use_existing_container_without_renaming_new_cli(self):
        with patch.object(m, 'ROOT', self.legacy), patch.object(m.subprocess, 'run') as run:
            m.run(['docker','restart','vowifi-vpn-wg-easy'])
            run.assert_called_once_with(['docker','restart','sip-wg-easy'],check=True,text=True,capture_output=True,timeout=900)
            self.assertEqual(m.runtime_name('/usr/local/bin/vowifi-vpn'), '/usr/local/bin/vowifi-vpn')

    def test_new_command_and_owned_old_alias_are_both_updated(self):
        current = self.root / 'bin/vowifi-vpn'
        legacy = self.root / 'bin/rykvo-sip'
        legacy.parent.mkdir()
        legacy.write_text('# Rykvo Voice SIP command\nold')
        with patch.object(m, 'CLI', current), patch.object(m, 'LEGACY_CLI', legacy):
            m.install_commands()
        self.assertIn('# VoWiFi VPN command', current.read_text())
        self.assertEqual(current.read_bytes(), legacy.read_bytes())
        self.assertEqual(current.stat().st_mode & 0o777, 0o755)

    def test_unrelated_old_command_is_not_overwritten(self):
        current = self.root / 'bin/vowifi-vpn'
        legacy = self.root / 'bin/rykvo-sip'
        legacy.parent.mkdir()
        legacy.write_text('# unrelated application')
        with patch.object(m, 'CLI', current), patch.object(m, 'LEGACY_CLI', legacy):
            m.install_commands()
        self.assertEqual(legacy.read_text(), '# unrelated application')
        self.assertTrue(current.is_file())

    def test_unrelated_marker_is_rejected(self):
        marker = self.marker(self.legacy, legacy=True, project='another-app')
        with patch.object(m, 'ROOT', self.legacy), patch.object(m, 'MARKER', marker):
            with self.assertRaisesRegex(ValueError, '标记不匹配'):
                m.read_marker()

    def test_legacy_update_preserves_credentials_clients_and_container_identity(self):
        marker = self.marker(self.legacy, legacy=True)
        state = json.loads(marker.read_text())
        state.update(network={'SIP_PUBLIC_IP':'8.8.8.8','SIP_BIND_IP':'10.0.0.5','SIP_PUBLIC_IF':'ens3'}, sysctl={})
        marker.write_text(json.dumps(state))
        preserved = {
            'panel-admin.json':'{"username":"existing-admin","password":"fixture"}',
            'admin-credentials.json':'{"username":"rykvo-service","password":"fixture"}',
            'clients.json':'{"clients":[{"id":11}]}',
            'enrollments.json':'{"11":{"hash":"fixture"}}',
        }
        for name, content in preserved.items():
            (self.legacy / name).write_text(content)
        panel = Mock(PUBLIC_IP='8.8.8.8', ADMIN=self.legacy / 'panel-admin.json')
        panel.load_settings.return_value = {'panelDomain':'','sipDomain':''}
        panel.caddy_config.return_value = '# fixture Caddyfile\n'
        panel.UserError = RuntimeError
        new_cli = self.legacy / 'bin/vowifi-vpn'
        old_cli = self.legacy / 'bin/rykvo-sip'
        old_cli.parent.mkdir()
        old_cli.write_text(m.LEGACY_CLI_MARKER)
        written = {}
        original_write = m.write
        def safe_write(path, data, mode=0o600):
            if path.is_relative_to(self.legacy):
                original_write(path, data, mode)
            else:
                written[str(path)] = data
        response = Mock()
        response.__enter__ = Mock(return_value=Mock(status=200))
        response.__exit__ = Mock(return_value=False)
        with patch.object(m, 'ROOT', self.legacy), patch.object(m, 'MARKER', marker), \
             patch.object(m, 'CLI', new_cli), patch.object(m, 'LEGACY_CLI', old_cli), \
             patch.object(m, 'write', side_effect=safe_write), patch.object(m, 'run'), \
             patch.object(m, 'compose') as compose, patch.object(m, 'install_source'), \
             patch.object(m, 'panel_module', return_value=panel), \
             patch.object(m.OPENER, 'open', return_value=response), patch.dict(m.os.environ, {}, clear=False):
            m.install()
        for name, content in preserved.items():
            self.assertEqual((self.legacy / name).read_text(), content)
        self.assertEqual(json.loads(marker.read_text())['project'], 'VoWiFi-VPN')
        self.assertIn('container_name: sip-wg-easy', (self.legacy / 'compose.yaml').read_text())
        self.assertIn(f'SIP_ROOT={self.legacy}', (self.legacy / 'runtime.env').read_text())
        self.assertIn('/etc/systemd/system/sip-simple-panel.service', written)
        self.assertEqual(new_cli.read_bytes(), old_cli.read_bytes())
        compose.assert_any_call('up', '-d')
        self.assertNotIn('INIT_PASSWORD', (self.legacy / 'compose.yaml').read_text())


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
        self.assertIn(['systemctl', 'start', 'vowifi-vpn-wireguard', 'vowifi-vpn-portmap', 'vowifi-vpn-panel', 'vowifi-vpn-caddy'], self.commands)

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
