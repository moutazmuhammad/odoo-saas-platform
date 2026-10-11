"""BYOS install script: static and sourced-function checks. Nothing is installed."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/byos/install.sh'


def bash(snippet):
    """Source the script (main does not run when sourced) and run ``snippet``."""
    return subprocess.run(
        ['bash', '-c', f'source "{SCRIPT}"; {snippet}'],
        capture_output=True, text=True, timeout=30)


class ByosInstallScriptTests(unittest.TestCase):
    def test_syntax(self):
        subprocess.run(['bash', '-n', str(SCRIPT)], check=True)

    @unittest.skipUnless(shutil.which('shellcheck'), 'shellcheck not installed')
    def test_shellcheck_clean(self):
        subprocess.run(['shellcheck', str(SCRIPT)], check=True)

    def test_help_and_unknown_option(self):
        out = subprocess.run(['bash', str(SCRIPT), '--help'], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0)
        self.assertIn('--detach', out.stdout)
        bad = subprocess.run(['bash', str(SCRIPT), '--nope'], capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)

    def test_no_moving_tags_or_eval(self):
        text = SCRIPT.read_text()
        self.assertNotRegex(text, r'^\s*eval\b', 'eval is never used')
        self.assertNotRegex(text, r'[:/][a-z0-9_.-]+:(latest|stable)\b', 'images are pinned, never latest/stable')

    def test_unpinned_image_is_refused(self):
        self.assertNotEqual(bash('pinned traefik:v3.6').returncode, 0)
        self.assertEqual(bash('pinned "traefik@sha256:' + 'a' * 64 + '"').returncode, 0)

    def test_json_helper_escapes_values(self):
        out = bash('json name "a\\"b" cpu_int 4 ok_bool true')
        self.assertEqual(json.loads(out.stdout), {'name': 'a"b', 'cpu': 4, 'ok': True})

    def test_json_get_reads_fields_and_lists(self):
        doc = json.dumps({'server_id': 's1', 'platform_ips': ['1.2.3.4/32', '5.6.7.8/32']})
        out = bash(f"json_get platform_ips <<<'{doc}'")
        self.assertEqual(out.stdout.strip(), '1.2.3.4/32,5.6.7.8/32')

    def test_platform_key_is_restricted_and_per_server(self):
        text = SCRIPT.read_text()
        self.assertIn('from=\\"${PLATFORM_IPS}\\",no-agent-forwarding,no-port-forwarding,no-X11-forwarding', text)
        self.assertIn('${KEY_MARK}:${SERVER_ID}', text)
        self.assertTrue(re.search(r'ssh-ed25519', text), 'only ed25519 keys are accepted')

    def test_firewall_reset_needs_preflight_and_confirmation(self):
        text = SCRIPT.read_text()
        main = text[text.index('main() {'):]
        self.assertLess(main.index('preflight'), main.index('confirm_changes'))
        self.assertLess(main.index('confirm_changes'), main.index('configure_firewall'))

    def test_check_mode_needs_root(self):
        if subprocess.run(['id', '-u'], capture_output=True, text=True).stdout.strip() == '0':
            self.skipTest('running as root')
        out = subprocess.run(['bash', str(SCRIPT), '--check'], capture_output=True, text=True)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn('root', out.stderr)


if __name__ == '__main__':
    unittest.main()
