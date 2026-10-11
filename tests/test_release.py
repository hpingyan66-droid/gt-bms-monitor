import hashlib
import importlib.util
import json
from pathlib import Path
import posixpath
import re
from urllib.parse import unquote, urlsplit
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('gtbms_release', ROOT / 'scripts/Build-Release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.fixture
def source_tree(tmp_path):
    root = tmp_path / 'source'
    names = set(release.ROOT_FILES)
    names.update(f'src/gtbms/{name}.py' for name in release.SOURCE_MODULES)
    names.update(f'src/gtbms/static/{name}' for name in release.STATIC_FILES)
    names.update(f'scripts/{name}' for name in release.STANDARD_SCRIPTS + release.FULL_SCRIPTS)
    names.update(f'docs/{name}' for name in release.PUBLIC_DOCS + release.FULL_DOCS)
    names.update(f'tests/{name}' for name in release.TEST_FILES)
    names.update(('requirements-dev.txt', 'requirements-mobile.txt', 'requirements-aiobmsble.txt',
                  'requirements-lock-windows-x64.txt', 'fixtures/jbd-synthetic.json'))
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('public source\n', encoding='utf-8')
    (root / 'pyproject.toml').write_text('[project]\nversion="0.2.0"\n', encoding='utf-8')
    (root / 'config.example.json').write_bytes((ROOT / 'config.example.json').read_bytes())
    (root / 'src/gtbms/edition.json').write_text('{"schema_version":1,"id":"full_access"}', encoding='utf-8')
    for name in ('config.local.json', 'mobile.local.json', 'network.local.json', 'mobile-connect.local.html',
                 'data/gtbms.sqlite3', 'logs/raw.json', 'backups/db.sqlite3', '.venv/python.exe',
                 'src/gtbms/credentials.py', 'src/gtbms/local.json', 'tests/test_customer_data.py',
                 'tests/test_private_capture.py', 'docs/PHONE_LATENCY_RESULTS.md', 'docs/VALIDATION.md',
                 'docs/WINDOWS_SETUP.md', 'docs/HARDWARE_VALIDATION.md', 'docs/NETWORK_MODES.md',
                 'docs/PRODUCT_SCOPE.md', 'docs/DESIGN.md', 'docs/MOBILE_VIEWER.md', 'docs/PHONE_LATENCY.md'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('PRIVATE_OPERATIONAL_MARKER', encoding='utf-8')
    license_file = root / 'artifacts/licenses/bleak/LICENSE'
    license_file.parent.mkdir(parents=True)
    license_file.write_text('Redistribution license', encoding='utf-8')
    (license_file.parent / 'private.json').write_text('PRIVATE_OPERATIONAL_MARKER', encoding='utf-8')
    return root


def test_three_editions_have_correct_manifest_and_keep_checkout_full(source_tree, tmp_path):
    before = (source_tree / 'src/gtbms/edition.json').read_bytes()
    outputs = release.build_releases(source_tree, tmp_path / 'out', build_id='test-001')
    assert [item['edition'] for item in outputs] == list(release.EDITIONS)
    assert (source_tree / 'src/gtbms/edition.json').read_bytes() == before
    for item in outputs:
        path = tmp_path / 'out' / item['file']
        assert '0.2.0' in path.name and 'test-001' in path.name and item['edition'] in path.name
        assert item['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
        with zipfile.ZipFile(path) as archive:
            assert json.loads(archive.read('src/gtbms/edition.json')) == {'schema_version': 1, 'id': item['edition']}
            assert json.loads(archive.read('config.example.json'))['mode'] == 'mock'
            metadata = json.loads(archive.read('PACKAGE.json'))
            assert metadata['source_only'] is True
            assert not metadata['corporate_identity_implemented']
            assert not metadata['qualification_code_implemented']
            assert set(metadata['files_sha256']) == set(archive.namelist()) - {'PACKAGE.json'}
            for name, digest in metadata['files_sha256'].items():
                assert hashlib.sha256(archive.read(name)).hexdigest() == digest
            assert 'LICENSE' in archive.namelist() and 'THIRD_PARTY_NOTICES.md' in archive.namelist()
            assert archive.read('artifacts/licenses/bleak/LICENSE') == b'Redistribution license'
            for name in archive.namelist():
                assert 'PRIVATE_OPERATIONAL_MARKER' not in archive.read(name).decode('utf-8')
                assert str(source_tree) not in archive.read(name).decode('utf-8')
                assert not name.endswith('.local.json')


@pytest.mark.parametrize('edition', ['corporate', 'individual'])
def test_limited_editions_have_only_install_and_start_scripts(source_tree, edition):
    files = release.collect_payload(source_tree, edition, '0.2.0', 'test')
    assert sorted(name for name in files if name.startswith('scripts/')) == sorted('scripts/' + name for name in release.STANDARD_SCRIPTS)
    assert not any(name.startswith('tests/') for name in files)
    assert '管理保守・ネットワーク管理' in files['README.md'].decode()
    assert 'README.md' in files and 'Quick-Install.cmd' in files and 'Start.cmd' in files


def test_full_has_source_tests_synthetic_fixture_and_admin_scripts(source_tree):
    files = release.collect_payload(source_tree, 'full_access', '0.2.0', 'test')
    assert all('scripts/' + name in files for name in release.FULL_SCRIPTS)
    assert all('tests/' + name in files for name in release.TEST_FILES)
    assert 'fixtures/jbd-synthetic.json' in files
    assert 'src/gtbms/credentials.py' not in files
    assert 'tests/test_customer_data.py' not in files


def test_existing_archive_never_overwritten_and_precheck_prevents_partial_batch(source_tree, tmp_path):
    output = tmp_path / 'out'
    output.mkdir()
    target = output / 'gtbms-windows-0.2.0-individual-same-source.zip'
    target.write_bytes(b'older verified release')
    with pytest.raises(release.ReleaseError, match='already exists'):
        release.build_releases(source_tree, output, build_id='same')
    assert list(output.iterdir()) == [target]
    assert target.read_bytes() == b'older verified release'


def test_repeat_build_keeps_previous_hash(source_tree, tmp_path):
    output = tmp_path / 'out'
    results = release.build_releases(source_tree, output, ('corporate',), 'first')
    original = (output / results[0]['file']).read_bytes()
    release.build_releases(source_tree, output, ('corporate',), 'second')
    assert (output / results[0]['file']).read_bytes() == original
    with pytest.raises(release.ReleaseError):
        release.build_releases(source_tree, output, ('corporate',), 'first')


@pytest.mark.parametrize('identifier', ['../outside', 'name/path', 'a\\b', '', 'A' * 65])
def test_build_identifier_cannot_escape_target(source_tree, tmp_path, identifier):
    # Empty explicit input is treated as automatic timestamp by the public API.
    if not identifier:
        result = release.build_releases(source_tree, tmp_path / 'out', ('corporate',), identifier)
        assert result[0]['build_id'].endswith('Z')
    else:
        with pytest.raises(release.ReleaseError, match='identifier'):
            release.build_releases(source_tree, tmp_path / 'out', build_id=identifier)


@pytest.mark.parametrize('field,value', [('mode', 'ble'), ('ble_address', 'private-value'), ('sync_enabled', True),
                                        ('sync_url', 'https://example.invalid'), ('gps_enabled', True),
                                        ('current_direction', 'charge_positive'), ('pair', True)])
def test_non_mock_or_external_example_is_rejected(source_tree, field, value):
    path = source_tree / 'config.example.json'
    data = json.loads(path.read_bytes())
    data[field] = value
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(release.ReleaseError, match='mock mode'):
        release.collect_payload(source_tree, 'corporate', '0.2.0', 'test')


def test_literal_address_rejected_without_echo(source_tree):
    value = ':'.join(['AB', 'CD', '12', '34', '56', '78'])
    (source_tree / 'src/gtbms/models.py').write_text(value, encoding='utf-8')
    with pytest.raises(release.ReleaseError) as error:
        release.collect_payload(source_tree, 'corporate', '0.2.0', 'test')
    assert value not in str(error.value)


def test_allowed_source_symlink_does_not_read_outside_tree(source_tree, tmp_path):
    external = tmp_path / 'not-public.txt'
    external.write_text('outside-private', encoding='utf-8')
    target = source_tree / 'src/gtbms/models.py'
    target.unlink()
    try:
        target.symlink_to(external)
    except OSError:
        pytest.skip('Symlink creation unavailable to this user')
    with pytest.raises(release.ReleaseError, match='redirects'):
        release.collect_payload(source_tree, 'corporate', '0.2.0', 'test')


def test_invalid_or_duplicate_edition_selection(source_tree, tmp_path):
    for choices in ((), ('unknown',), ('corporate', 'corporate')):
        with pytest.raises(release.ReleaseError, match='selection'):
            release.build_releases(source_tree, tmp_path / 'out', choices, 'test')


def test_required_source_missing_rejects_before_writing_zip(source_tree, tmp_path):
    (source_tree / 'LICENSE').unlink()
    with pytest.raises(release.ReleaseError, match='missing'):
        release.build_releases(source_tree, tmp_path / 'out', build_id='test')
    assert not (tmp_path / 'out').exists()


@pytest.mark.parametrize('edition', release.EDITIONS)
def test_current_public_source_payload_has_complete_markdown_links(edition):
    # Reads the source allowlist only; does not create a release or touch runtime data.
    files = release.collect_payload(ROOT, edition, '0.2.0', 'validation-only')
    for name, data in files.items():
        if not name.endswith('.md'):
            continue
        for target in re.findall(r'\]\(([^\s)]+)(?:\s+[^)]*)?\)', data.decode('utf-8-sig')):
            parsed = urlsplit(target.strip('<>'))
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(parsed.path)))
            assert resolved in files, (edition, name, resolved)
