import configparser
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POWERSHELL = shutil.which('powershell.exe')
BASH = shutil.which('bash')
if not BASH and os.name == 'nt':
    candidate = Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Git/bin/bash.exe'
    BASH = str(candidate) if candidate.is_file() else None


class DeploymentConfigTest(unittest.TestCase):
    def test_scripts_have_required_encoding_and_unit_uses_one_worker(self):
        for name in ('iniciar_cads_pro.ps1', 'generate_admin_key.ps1'):
            self.assertTrue((ROOT / name).read_bytes().startswith(b'\xef\xbb\xbf'))
        for name in ('start_cads_pro.sh', 'generate_admin_key.sh'):
            self.assertNotIn(b'\r', (ROOT / name).read_bytes())
        unit = configparser.ConfigParser(interpolation=None, strict=False)
        unit.read(ROOT / 'cads-pro.service', encoding='utf-8')
        service = unit['Service']
        self.assertIn('--workers 1', service['ExecStart'])
        self.assertNotIn('--reload', service['ExecStart'])
        self.assertIn('--no-server-header --no-proxy-headers', service['ExecStart'])
        self.assertEqual(service['ProtectSystem'], 'strict')
        self.assertEqual(service['ReadWritePaths'], '/var/lib/cads-pro')
        self.assertEqual(service['StateDirectory'], 'cads-pro')

    def test_service_preflight_rejects_missing_short_and_space_keys(self):
        text = (ROOT / 'cads-pro.service').read_text(encoding='utf-8')
        line = next(line for line in text.splitlines() if line.startswith('ExecStartPre='))
        code = shlex.split(line.split('=', 1)[1])[2]
        for key, expected in [('', 1), ('short', 1), ('a' * 32 + ' ', 1), (secrets.token_hex(32), 0)]:
            env = {**os.environ, 'ADMIN_API_KEY': key, 'LOG_LEVEL': 'info'}
            result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, expected)
            if len(key) >= 32:
                self.assertNotIn(key.encode(), result.stdout + result.stderr)
        result = subprocess.run([sys.executable, '-c', code],
                                env={**os.environ, 'ADMIN_API_KEY': secrets.token_hex(32), 'LOG_LEVEL': 'trace'},
                                capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)

    def test_example_has_no_default_password_and_secrets_are_ignored(self):
        example = (ROOT / '.env.example').read_text(encoding='utf-8')
        settings = dict(line.split('=', 1) for line in example.splitlines() if line and not line.startswith('#'))
        self.assertEqual(settings['ADMIN_API_KEY'], '')
        self.assertEqual(settings['PLAN_LIMIT_DEFAULT'], '5000')
        self.assertTrue(all('#' not in value for value in settings.values()))
        ignored = (ROOT / '.gitignore').read_text().splitlines()
        for name in ['.env', '.env.local', '.env.*.local', '*.key', '*.pem', 'secrets/']:
            self.assertIn(name, ignored)

    def test_configured_state_directory_and_plan_work_with_authentication(self):
        with tempfile.TemporaryDirectory(prefix='cads-state-') as directory:
            env = {**os.environ, 'CADS_DATA_DIR': directory, 'PLAN_LIMIT_DEFAULT': '777',
                   'ADMIN_API_KEY': secrets.token_hex(32), 'PYTHONDONTWRITEBYTECODE': '1'}
            code = '''
import os
from pathlib import Path
from fastapi.testclient import TestClient
from server import main, product_video
root = Path(os.environ['CADS_DATA_DIR'])
assert Path(main.DB_PATH) == root / 'cads_auth.db'
assert Path(main.PROCESSED_DIR) == product_video.PROCESSED_DIR == root / 'processed'
assert root.joinpath('cads_auth.db').is_file()
from server import accounts
owner_id = accounts.create_customer('Owner', 'owner@example.test', 'test-owner-password', role='admin', status='approved')
client = TestClient(main.app)
client.cookies.set(accounts.COOKIE, accounts.new_session(owner_id))
assert client.get('/api/stats').json()['plan_limit'] == 777
assert client.post('/api/auth/create-key', data={'plan':'pro'}).status_code == 401
assert client.post('/api/auth/create-key', data={'plan':'pro'}, headers={'X-Admin-Key':os.environ['ADMIN_API_KEY']}).status_code == 200
print('state_and_admin_OK')
'''
            result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            self.assertIn(b'state_and_admin_OK', result.stdout)
            self.assertNotIn(env['ADMIN_API_KEY'].encode(), result.stdout + result.stderr)


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cads-launcher-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ['iniciar_cads_pro.ps1', 'start_cads_pro.sh']:
            shutil.copyfile(ROOT / name, self.root / name)
        # Um módulo isolado verifica argumentos e herança do ambiente sem ocupar a porta real.
        package = self.root / 'uvicorn'
        package.mkdir()
        (package / '__init__.py').touch()
        (package / '__main__.py').write_text('''
import os, sys
if '--version' in sys.argv:
    print('fixture_uvicorn_version')
else:
    assert len(os.environ['ADMIN_API_KEY']) >= 32
    for flag in ['--no-server-header','--no-proxy-headers','--reload']:
        assert flag in sys.argv
    assert all(int(os.environ[key]) > 0 for key in ['RATE_LIMIT_PUBLIC','RATE_LIMIT_PROCESSING','RATE_LIMIT_ADMIN'])
    assert os.environ['ADMIN_API_KEY'] not in ' '.join(sys.argv)
    print('fixture_configuration_OK', flush=True)
    if os.environ.get('CADS_TEST_WAIT'):
        import time
        time.sleep(120)
    sys.exit(int(os.environ.get('CADS_TEST_EXIT','0')))
''', encoding='utf-8')
        self.key = secrets.token_hex(32)
        self.env = {**os.environ, 'CADS_TEST_KEY': self.key, 'CADS_REAL_PYTHON': sys.executable.replace('\\', '/'),
                    'PYTHONIOENCODING': 'utf-8', 'LOG_LEVEL': 'info'}
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        relay = bin_dir / 'python3'
        relay.write_text('#!/usr/bin/env bash\nexec "$CADS_REAL_PYTHON" "$@"\n', encoding='utf-8')
        relay.chmod(0o755)
        self.bin_dir = bin_dir

    def assert_no_key_output(self, result):
        self.assertNotIn(self.key.encode(), result.stdout + result.stderr)

    def powershell_case(self, key_expression='$env:CADS_TEST_KEY', rate='', expected=0, native_exit=0):
        script = self.root / 'harness.ps1'
        script.write_text('''
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$global:CadsTestAnswers = New-Object 'System.Collections.Generic.Queue[string]'
$global:CadsTestAnswers.Enqueue(KEY_EXPRESSION)
$global:CadsTestAnswers.Enqueue('RATE_INPUT')
$global:CadsTestAnswers.Enqueue('')
$global:CadsTestAnswers.Enqueue('')
function Read-Host {
    param([string]$Prompt, [switch]$AsSecureString)
    $answer = $global:CadsTestAnswers.Dequeue()
    if ($AsSecureString) {
        $secure = New-Object System.Security.SecureString
        foreach ($character in $answer.ToCharArray()) { $secure.AppendChar($character) }
        return $secure
    }
    return $answer
}
& (Join-Path $PSScriptRoot 'iniciar_cads_pro.ps1')
if ($LASTEXITCODE -ne EXPECTED_CODE) { throw 'Unexpected launcher exit code' }
foreach ($name in @('ADMIN_API_KEY','RATE_LIMIT_PUBLIC','RATE_LIMIT_PROCESSING','RATE_LIMIT_ADMIN')) {
    if ($null -ne [Environment]::GetEnvironmentVariable($name, 'Process')) { throw 'Environment not cleaned' }
}
Write-Output 'fixture_cleanup_OK'
'''.replace('KEY_EXPRESSION', key_expression).replace('RATE_INPUT', rate).replace('EXPECTED_CODE', str(expected)), encoding='utf-8-sig')
        env = {**self.env, 'CADS_TEST_EXIT': str(native_exit)}
        result = subprocess.run([POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)],
                                cwd=self.root, env=env, capture_output=True, timeout=30)
        self.assert_no_key_output(result)
        self.assertEqual(result.returncode, 0, result.stdout.decode('utf-8', 'replace') + result.stderr.decode('utf-8', 'replace'))
        self.assertIn(b'fixture_cleanup_OK', result.stdout)
        return result

    @unittest.skipUnless(POWERSHELL, 'PowerShell 5.1 indisponivel')
    def test_powershell_start_defaults_and_cleanup(self):
        result = self.powershell_case()
        self.assertIn(b'fixture_configuration_OK', result.stdout)

    @unittest.skipUnless(POWERSHELL, 'PowerShell 5.1 indisponivel')
    def test_powershell_rejects_short_key_and_bad_rate_and_cleans_failure(self):
        for kwargs in [dict(key_expression="'short'", expected=1), dict(rate='invalid', expected=1),
                       dict(rate='0', expected=1), dict(rate='2147483648', expected=1)]:
            with self.subTest(kwargs=kwargs):
                result = self.powershell_case(**kwargs)
                self.assertNotIn(b'fixture_configuration_OK', result.stdout)
        self.assertIn(b'fixture_configuration_OK', self.powershell_case(native_exit=7, expected=7).stdout)

    @unittest.skipUnless(POWERSHELL, 'PowerShell 5.1 indisponivel')
    def test_powershell_generator_has_64_hex_chars(self):
        result = subprocess.run([POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                                 str(ROOT / 'generate_admin_key.ps1')], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(re.findall(rb'(?m)^[0-9a-f]{64}\r?$', result.stdout)), 1)

    def bash_env(self):
        # No Git Bash, PATH usa caminhos POSIX; converte o diretório de teste.
        if os.name == 'nt':
            converted = subprocess.check_output([BASH, '-c', 'cygpath -u "$1"', 'bash', str(self.bin_dir)], timeout=15).decode().strip()
            current_path = subprocess.check_output([BASH, '-c', 'printf "%s" "$PATH"'], timeout=15).decode()
            return {**self.env, 'PATH': converted + ':' + current_path}
        return {**self.env, 'PATH': str(self.bin_dir) + ':' + os.environ['PATH']}

    @unittest.skipUnless(BASH, 'Bash indisponivel')
    def test_bash_start_and_validation(self):
        for key, rate, expected in [(self.key, '', 0), ('short', '', 1), (self.key, '0', 1),
                                    (self.key, 'invalid', 1), (self.key, '2147483648', 1)]:
            with self.subTest(rate=rate, expected=expected):
                result = subprocess.run([BASH, str(self.root / 'start_cads_pro.sh')], cwd=self.root,
                                        env=self.bash_env(), input=f'{key}\n{rate}\n\n\n'.encode(),
                                        capture_output=True, timeout=30)
                self.assert_no_key_output(result)
                self.assertEqual(result.returncode, expected, result.stderr.decode('utf-8', 'replace'))
                self.assertIn('Variáveis da sessão removidas'.encode(), result.stdout)
                if expected == 0:
                    self.assertIn(b'fixture_configuration_OK', result.stdout)

    @unittest.skipUnless(BASH, 'Bash indisponivel')
    def test_bash_generator_and_syntax(self):
        for name in ['start_cads_pro.sh', 'generate_admin_key.sh']:
            result = subprocess.run([BASH, '-n', str(ROOT / name)], capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0)
        result = subprocess.run([BASH, str(ROOT / 'generate_admin_key.sh')], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(re.findall(rb'(?m)^[0-9a-f]{64}\r?$', result.stdout)), 1)

    @unittest.skipUnless(BASH, 'Bash indisponivel')
    def test_bash_sigterm_is_forwarded_and_cleanup_runs(self):
        relay = self.bin_dir / 'python3'
        relay.write_text('''#!/usr/bin/env bash
if [[ "$1" == '-m' && "$*" != *'--version'* ]]; then
    printf '%s' "$$" > "$CADS_TEST_CHILD_PID"
    trap 'printf stopped > "$CADS_TEST_CHILD_STOPPED"; exit 0' TERM
    while :; do sleep 0.1; done
else
    exec "$CADS_REAL_PYTHON" "$@"
fi
''', encoding='utf-8')
        relay.chmod(0o755)
        child_pid = self.root / 'child.pid'
        stopped = self.root / 'child.stopped'
        launcher_pid = self.root / 'launcher.pid'
        env = {**self.bash_env(), 'CADS_TEST_CHILD_PID': child_pid.as_posix(),
               'CADS_TEST_CHILD_STOPPED': stopped.as_posix(), 'CADS_TEST_LAUNCHER_PID': launcher_pid.as_posix()}
        process = subprocess.Popen([BASH, '-c', 'printf "%s" "$$" > "$CADS_TEST_LAUNCHER_PID"; exec bash "$1"',
                                    'bash', str(self.root / 'start_cads_pro.sh')], cwd=self.root, env=env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            process.stdin.write((self.key + '\n\n\n\n').encode())
            process.stdin.flush()
            deadline = time.monotonic() + 15
            while not child_pid.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(child_pid.exists(), 'Child did not become ready')
            subprocess.run([BASH, '-c', 'kill -TERM "$(cat "$CADS_TEST_LAUNCHER_PID")"'],
                           env=env, check=True, capture_output=True, timeout=10)
            output, errors = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 143, errors.decode(errors='replace'))
            self.assertTrue(stopped.exists())
            self.assertIn('Variáveis da sessão removidas'.encode(), output)
            self.assertNotIn(self.key.encode(), output + errors)
        finally:
            if process.poll() is None:
                if launcher_pid.exists():
                    subprocess.run([BASH, '-c', 'kill -TERM "$(cat "$CADS_TEST_LAUNCHER_PID")"'],
                                   env=env, capture_output=True, timeout=10)
                try:
                    process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()


if __name__ == '__main__':
    unittest.main()
