import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = (Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
pytestmark = pytest.mark.skipif(sys.platform != 'win32' or not POWERSHELL.is_file(), reason='Windows PowerShell required')


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


@pytest.fixture
def install_root(tmp_path):
    root = tmp_path / 'package with spaces'
    (root / 'scripts').mkdir(parents=True)
    (root / 'src/gtbms').mkdir(parents=True)
    for name in ('Setup.ps1', 'Quick-Install.ps1', 'Start.ps1'):
        shutil.copy2(ROOT / 'scripts' / name, root / 'scripts' / name)
    shutil.copy2(ROOT / 'config.example.json', root / 'config.example.json')
    (root / 'src/gtbms/edition.json').write_text(json.dumps({'schema_version': 1, 'id': 'corporate'}), encoding='utf-8')
    return root


def run_ps(code, cwd):
    return subprocess.run([str(POWERSHELL), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                           '-Command', code], cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)


def test_check_only_probes_explicit_python_without_creating_environment_or_config(install_root):
    result = run_ps('& ' + quote(install_root / 'scripts/Quick-Install.ps1') + ' -CheckOnly -PythonExe ' + quote(sys.executable), install_root)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan['edition'] == 'corporate'
    assert plan['configuration'] == 'create_mock_example'
    assert plan['environment'] == 'create_local_venv'
    assert plan['changes_applied'] is False
    assert not (install_root / '.venv').exists()
    assert not (install_root / 'config.local.json').exists()


def test_existing_configuration_is_preserved_by_install_plan(install_root):
    config = install_root / 'config.local.json'
    config.write_text('EXISTING_PRIVATE_CONTENT', encoding='utf-8')
    result = run_ps('& ' + quote(install_root / 'scripts/Setup.ps1') + ' -CheckOnly -PythonExe ' + quote(sys.executable), install_root)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['configuration'] == 'preserve_existing'
    assert config.read_text() == 'EXISTING_PRIVATE_CONTENT'
    assert 'EXISTING_PRIVATE_CONTENT' not in result.stdout + result.stderr


@pytest.mark.parametrize('value', [None, {'schema_version': 1, 'id': 'unknown'}, {'schema_version': True, 'id': 'full_access'}])
def test_invalid_edition_stops_before_environment_changes(install_root, value):
    path = install_root / 'src/gtbms/edition.json'
    if value is None:
        path.unlink()
    else:
        path.write_text(json.dumps(value), encoding='utf-8')
    result = run_ps('& ' + quote(install_root / 'scripts/Setup.ps1') + ' -CheckOnly -PythonExe ' + quote(sys.executable), install_root)
    assert result.returncode != 0
    assert not (install_root / '.venv').exists()
    assert not (install_root / 'config.local.json').exists()


def test_invalid_existing_environment_is_never_replaced(install_root):
    environment = install_root / '.venv'
    environment.mkdir()
    marker = environment / 'keep.txt'
    marker.write_text('existing environment', encoding='utf-8')
    result = run_ps('& ' + quote(install_root / 'scripts/Setup.ps1') + ' -CheckOnly -PythonExe ' + quote(sys.executable), install_root)
    assert result.returncode != 0
    assert marker.read_text() == 'existing environment'
    assert list(environment.iterdir()) == [marker]


def test_non_mock_example_is_rejected_without_creating_config(install_root):
    path = install_root / 'config.example.json'
    data = json.loads(path.read_bytes())
    data['mode'] = 'ble'
    path.write_text(json.dumps(data), encoding='utf-8')
    result = run_ps('& ' + quote(install_root / 'scripts/Setup.ps1') + ' -CheckOnly -PythonExe ' + quote(sys.executable), install_root)
    assert result.returncode != 0
    assert not (install_root / 'config.local.json').exists()


def test_start_does_not_open_an_existing_other_edition_or_kill_process(install_root):
    python = install_root / '.venv/Scripts/python.exe'
    python.parent.mkdir(parents=True)
    python.write_bytes(b'never execute')
    (install_root / 'config.local.json').write_text('{"port":8765}', encoding='utf-8')
    code = "function Get-NetTCPConnection { [pscustomobject]@{LocalPort=8765} }; function Start-Process { throw 'UNEXPECTED_BROWSER' }; function Stop-Process { throw 'UNEXPECTED_STOP' }; & "
    result = run_ps(code + quote(install_root / 'scripts/Start.ps1') + ' -OpenBrowser', install_root)
    assert result.returncode != 0
    assert 'already in use' in result.stderr
    assert 'UNEXPECTED_BROWSER' not in result.stderr
    assert 'UNEXPECTED_STOP' not in result.stderr


def test_scripts_parse_on_windows_powershell_and_have_no_codex_discovery(install_root):
    for name in ('Setup.ps1', 'Quick-Install.ps1', 'Start.ps1'):
        code = '$tokens=$null; $errors=$null; [Management.Automation.Language.Parser]::ParseFile(' + quote(install_root / 'scripts' / name) + ',[ref]$tokens,[ref]$errors) | Out-Null; if ($errors.Count) {exit 1}'
        assert run_ps(code, install_root).returncode == 0
    assert 'codex-runtimes' not in (install_root / 'scripts/Setup.ps1').read_text(encoding='utf-8-sig')
