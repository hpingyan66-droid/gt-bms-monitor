"""Build versioned source ZIPs from an allowlist, without operating data.

Edition manifests and public onboarding docs are generated inside each ZIP.
The current checkout, configuration, interpreter and processes are untouched.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import tomllib
import zipfile

EDITIONS = ('corporate', 'individual', 'full_access')
LABELS = {'corporate': '法人利用版', 'individual': '個人利用版', 'full_access': 'フルアクセス版'}
ROOT_FILES = ('.gitignore', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'pyproject.toml',
              'requirements.txt', 'config.example.json', 'Quick-Install.cmd', 'Start.cmd')
SOURCE_MODULES = ('__init__', 'analysis', 'app', 'cli', 'config', 'delivery_analysis', 'diagnostics',
                  'editions', 'mobile', 'models', 'monitor', 'network', 'network_platform',
                  'plugin_probe', 'protocol', 'storage', 'sync', 'transport', 'workers')
STATIC_FILES = ('app.js', 'index.html', 'style.css', 'mobile.html', 'mobile.js', 'mobile.css',
                'network.html', 'network.js', 'network.css')
STANDARD_SCRIPTS = ('Setup.ps1', 'Quick-Install.ps1', 'Start.ps1')
FULL_SCRIPTS = ('Build-Release.py', 'Collect-Licenses.py', 'Crash-OwnedServer.py', 'Environment.ps1',
                'Inspect-MobileNetwork.ps1', 'Measure-Delivery.py', 'Mobile-Firewall.ps1', 'Network-Control.ps1',
                'Network-Firewall.ps1', 'Register-NetworkStartup.ps1', 'Register-Startup.ps1', 'soak.py',
                'Start-Mobile.ps1', 'Start-Network.ps1', 'Watchdog.ps1')
PUBLIC_DOCS = ('PACKAGES.md', 'TROUBLESHOOTING.md', 'CORPORATE_OSS_DELIVERY.md',
               'ELEVATED_ACCESS_PROPOSAL.md', 'OPERATING_MODES_PROPOSAL.md')
FULL_DOCS = ()
TEST_FILES = ('conftest.py', 'mobile_frontend.cjs', 'network_frontend.cjs', 'network_control.ps1',
    'network_firewall.ps1', 'test_analysis.py', 'test_api_sync.py', 'test_config_additional.py',
    'test_delivery_analysis.py', 'test_diagnostics.py', 'test_editions.py', 'test_installers.py',
    'test_mobile.py', 'test_monitor_transport.py', 'test_network.py', 'test_network_platform.py',
    'test_plugin_probe.py', 'test_protocol.py', 'test_release.py', 'test_storage.py', 'test_workers.py')
LICENSE_PACKAGES = {'aiobmsble', 'annotated-doc', 'annotated-types', 'anyio', 'bleak', 'bleak-retry-connector',
    'certifi', 'click', 'colorama', 'fastapi', 'gt-bms-monitor', 'h11', 'httpcore', 'httpx', 'idna',
    'iniconfig', 'opentelemetry-api', 'packaging', 'pip', 'pluggy', 'psutil', 'pydantic', 'pydantic_core',
    'Pygments', 'pytest', 'pytest-asyncio', 'qrcode', 'setuptools', 'starlette', 'typing-inspection',
    'typing_extensions', 'uvicorn', 'websockets', 'wheel'}
MAX_FILE_BYTES = 4 * 1024 * 1024
MAC = re.compile(rb'(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f])')
PRIVATE_NAMES = re.compile(r'(^|[./_-])(private|secret|credentials|telemetry|runtime|local)([./_-]|$)', re.I)

class ReleaseError(ValueError):
    pass


def _read(root: Path, relative: str) -> bytes:
    path = root / relative
    if (path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve())
            or any(parent.is_symlink() for parent in path.parents if parent != root.parent)):
        raise ReleaseError('Allowed source is missing or redirects outside the source tree')
    with path.open('rb') as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ReleaseError('Source file exceeds release size limit')
    if MAC.search(data):
        raise ReleaseError('Literal Bluetooth address detected; remove private address before release')
    return data


def _json(data: object) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def _public_readme(edition: str, version: str, build_id: str) -> bytes:
    management = ('管理保守とネットワーク管理を含みます。ローカルのUIと手順を確認して操作してください。'
                  if edition == 'full_access' else
                  '監視の開始・停止、走行記録、履歴と出力を利用できます。管理保守・ネットワーク管理と補助スクリプトは含めません。')
    return f'''# GT BMS Windows — {LABELS[edition]}

バージョン {version} / ビルド {build_id} / edition `{edition}`

## 初回導入

1. Windows 11にPython 3.12以降を事前にインストールします。Pythonの自動導入は行いません。
2. ZIPを一般ユーザーが書込み可能な新しいフォルダーへ展開します。運用フォルダーへ上書きしません。
3. `Quick-Install.cmd` をダブルクリックします。依存パッケージ取得にインターネット接続が必要です。
4. 完了後 `Start.cmd` をダブルクリックします。localhost:8765を開き、新規導入は模擬BMSから始まります。
5. 実機設定は `docs/HARDWARE_VALIDATION.md` を参照し、模擬での表示・保存確認を先に完了してください。

起動直後に接続できない場合は、数秒待ってブラウザーを更新してください。

Pythonを明示する場合はPowerShellで `scripts/Quick-Install.ps1 -PythonExe 'C:\\Path\\To\\python.exe'` を使います。
導入はこのフォルダーの.venvに限定し、既存config.local.jsonは上書きしません。
Python、ドライバー、ファイアウォール、ホットスポット、タスク設定を自動変更しません。
画面を閉じるだけでは記録は止まりません。起動したコンソールでCtrl+Cを押して終了します。

## 配布範囲

{management}

法人ID管理・資格コード・支店管理・認証基盤は未実装です。法人版と個人版の利用機能は現在同一です。
同梱ソースを変更できるため、editionは改変に耐えるライセンス認証やDRMではありません。
公開ソース・テスト・管理スクリプト一式はフルアクセス版ZIPを基準とします。
ソース公開先: https://github.com/hpingyan66-droid/gt-bms-monitor 。公開・リリースの状態はリポジトリで確認してください。
限定版にはテストを同梱しません。Setupの-WithTestsはテスト依存の導入のみで、テスト本体はフル版にあります。
対象BMS、Windows ARM64/x64での実機適合は導入先ごとに確認してください。BMS通信はすべて読取り専用です。
平均電力・平均ピーク・新しい電費表示は別設計の対象で、この版で実装済みとは扱いません。

LICENSEとTHIRD_PARTY_NOTICES.mdを参照してください。取得済み依存OSSのライセンスはartifacts/licensesに同梱します。
利用者の実設定・実測データ・Bluetoothアドレス・秘密・PythonランタイムをZIPへ含めません。
詳細: docs/PACKAGES.md、docs/WINDOWS_SETUP.md、docs/TROUBLESHOOTING.md。
'''.encode('utf-8')


def _public_setup() -> bytes:
    return '''# Windowsでの新規セットアップ

Windows 11とPython 3.12以降、初回導入時のインターネット接続が必要です。
OS/PythonのアーキテクチャとBleak/WinRT、psutil、pydantic-core等のwheel適合を確認します。
ARM64ネイティブとx64の実機適合は導入先で検証し、既存の正常な環境を置き換えません。

1. 新しい書込み可能なフォルダーへZIPを展開します。
2. Quick-Install.cmdを実行します。Pythonが見つからない場合はscripts/Quick-Install.ps1の-PythonExeで明示します。
3. Start.cmdを実行し、localhost:8765の模擬データで表示・保存を確認します。
4. 実機設定はHARDWARE_VALIDATION.mdを参照します。

scripts/Setup.ps1 -CheckOnlyは導入計画だけを表示し、仮想環境・設定・OSを変更しません。
-SkipInstallは既存の依存環境の検証用です。不足パッケージを自動補完しません。
-WithTestsはテスト依存を導入します。テスト本体はフルアクセス版にあります。
新規導入時だけ模擬設定をコピーし、既存config.local.jsonは保持します。
画面オフとスリープを区別し、スリープ中のBLE収集継続を前提にしません。
自動起動やスマホ接続管理はフル版の別手順です。導入スクリプトは自動登録しません。
'''.encode('utf-8')


def _public_hardware() -> bytes:
    return '''# 導入先のBMS確認（読取り専用）

配布ZIPは模擬モードです。特定個体の実績を、この導入先の検証済みという意味で同梱していません。

1. 模擬の表示・SQLite保存を確認し、起動したコンソールをCtrl+Cで終了します。
2. スマホのBMSアプリを切断し、Windowsをバッテリーの通信圏内へ置きます。
3. Windowsの機器情報や所有者が確認したBLEアドレスをconfig.local.jsonのble_addressへ設定します。Git・報告書へ記載しません。
4. modeをbleへ変更します。current_directionはunconfirmedを維持し、生電流の符号を充放電状態と照合してからcharge_positiveまたはdischarge_positiveを選びます。
5. Start.cmdで接続・最終通信時刻・電圧・セル数・SOC・品質フラグを確認し、公式アプリ等と照合します。
6. 保存、切断時の現在値失効、再接続を確認します。スキャン・追加診断・保守CLIはフルアクセス版で実施します。

未確認の認証方式を推測で送信しません。応答しない場合は停止し、メーカーや管理担当へ確認します。
保護設定、バランス設定、保護解除、ファームウェア、配線・バッテリー内部の加工は対象外です。
温度センサー設置場所や実測使用可能容量を通信成功だけで確定しません。設定容量と実測容量を区別します。
資格コード・法人IDは未実装です。BMS保護解除コードとして使う機能はありません。
'''.encode('utf-8')


def _public_design() -> bytes:
    return '''# 公開配布版の設計概要

BMSはJBD 03/04の読取り専用。Bleakで取得し、単位・符号を正規化してSQLiteへ記録します。
SQLiteはWAL/FULL、UTC時刻、バッテリー/模擬/実機を分離して保存します。表示時刻はJSTです。
品質不良、長い欠測、接続ID変更、時計変動をまたいで電力量を補間しません。
平均電費・新しいピーク指標の設計は未実装。航続予測は放電Wh/km、走行履歴wh_per_kmは正味Wh/kmを使用します。
本体Webは127.0.0.1限定。モバイル閲覧はフル版で明示的に設定したLANと閲覧キーに限定します。
edition.jsonはfull_access/corporate/individualの配布機能を選びます。OS権限や本人認証ではありません。
個人/法人版は監視・走行・履歴・出力、フル版だけ管理保守・ネットワーク管理を許可します。
詳細な実装はsrc/gtbms、検証コードはフル版のtestsを参照してください。
この文書は公開用概要で、個別導入先の測定値や作業履歴を含めません。
'''.encode('utf-8')


def _public_mobile() -> bytes:
    return '''# スマホ閲覧と接続管理（フルアクセス版）

Pythonモバイル依存はrequirements-mobile.txtを使用します。スマホ閲覧は読取り専用です。
本体をStart.cmdで起動後、scripts/Start-Network.ps1 -OpenUIでlocalhost:8767を開きます。
モバイルルーター・ホームルーターでは既存Windows Wi-Fiプロファイルを登録します。
ローカルエリアはWindows標準ホットスポットを使い、OS設定でSSIDとパスワードを確認します。
明示的な切替では閲覧サーバー8766を専用IP/インターフェース/送信元範囲へ限定し、必要なUAC承認を求めます。
スマホを同じWi-Fiへ接続し、Surfaceの接続案内QRで閲覧します。QR・閲覧キーは共有しません。
完全オフラインでのホットスポット起動、スリープ復帰、各端末の適合は導入先で検証します。
接続管理の変更中もBMSの本体記録を別プロセスで保持します。failed/recovery_requiredは実接続も確認してください。
比較診断は停車中に端末・経路・アダプター・モーターを記録し120秒観測します。
保存JSONの解析はpython -m gtbms.delivery_analysis input.json --output summary.jsonで実施できます。
HTTP往復・DOM反映・受信値の年齢推定は別の指標で、片道無線遅延や画面描画完了とは同一視しません。
この公開手順に個別導入先の測定結果は含めません。
'''.encode('utf-8')


def collect_payload(root: Path, edition: str, version: str, build_id: str) -> dict[str, bytes]:
    if edition not in EDITIONS:
        raise ReleaseError('Unknown edition')
    selected = list(ROOT_FILES)
    selected += [f'src/gtbms/{name}.py' for name in SOURCE_MODULES]
    selected += [f'src/gtbms/static/{name}' for name in STATIC_FILES]
    selected += [f'scripts/{name}' for name in STANDARD_SCRIPTS]
    selected += [f'docs/{name}' for name in PUBLIC_DOCS]
    # Test dependencies may be requested in Setup even in a limited edition.
    selected += ['requirements-dev.txt']
    if edition == 'full_access':
        selected += [f'scripts/{name}' for name in FULL_SCRIPTS]
        selected += [f'docs/{name}' for name in FULL_DOCS]
        selected += ['requirements-aiobmsble.txt', 'requirements-mobile.txt',
                     'requirements-lock-windows-x64.txt', 'fixtures/jbd-synthetic.json']
        selected += [f'tests/{name}' for name in TEST_FILES]
    files = {name: _read(root, name) for name in selected}
    config = json.loads(files['config.example.json'].decode('utf-8-sig'))
    if (config.get('mode') != 'mock' or config.get('ble_address') != '' or config.get('pair') is not False
            or config.get('current_direction') != 'unconfirmed' or config.get('sync_enabled') is not False
            or config.get('sync_url') != '' or config.get('gps_enabled') is not False):
        raise ReleaseError('Public example must start in mock mode without an address or external services')
    for package in sorted(LICENSE_PACKAGES):
        folder = root / 'artifacts/licenses' / package
        if folder.is_dir():
            for path in sorted(folder.iterdir()):
                if path.is_file() and re.fullmatch(r'(?:LICENSE|COPYING|NOTICE)(?:\.[A-Za-z]+)?', path.name):
                    name = path.relative_to(root).as_posix()
                    files[name] = _read(root, name)
    files['README.md'] = _public_readme(edition, version, build_id)
    files['docs/WINDOWS_SETUP.md'] = _public_setup()
    files['docs/HARDWARE_VALIDATION.md'] = _public_hardware()
    files['docs/DESIGN.md'] = _public_design()
    files['docs/PRODUCT_SCOPE.md'] = _public_design()
    files['docs/BATMON_COMPATIBILITY.md'] = '''# BatMONと任意コネクター

本アプリはJBD読取り専用の独立実装を使用します。参考元の現行対応条件とライセンスは公式資料で確認してください。
- [BatMON repository](https://github.com/fl4p/batmon-ha)
- [Standalone guide](https://github.com/fl4p/batmon-ha/blob/master/doc/Standalone.md)
- [BatMON license](https://github.com/fl4p/batmon-ha/blob/master/LICENSE)
- [aiobmsble](https://github.com/patman15/aiobmsble)

任意診断コネクターはフルアクセス版の診断用です。対象BMSの互換性・認証方式は導入先で確認します。
この公開文書に特定個体の通信結果・接続アドレス・認証情報は含めません。
通知は [THIRD_PARTY_NOTICES](../THIRD_PARTY_NOTICES.md) を参照してください。
'''.encode('utf-8')
    files['docs/AUTH_TIMEOUT_INVESTIGATION.md'] = '''# BMS接続認証が完了しない場合

接続・通知受信・読取り応答・データ解析は別の段階です。接続だけでBMS互換性を確定しません。
公式スマホアプリとの接続競合、受信強度、Windowsドライバー、サービスUUIDと読取り対応を確認します。
パスワードはチャット・ログ・報告へ出さず、対象が確認できる公式手順でのみ扱います。
繰り返し失敗した場合は同じ試行を続けず、モニターを停止しメーカーや管理担当に相談します。
フルアクセス版の追加診断は [実機確認](HARDWARE_VALIDATION.md) と [参考コネクター](BATMON_COMPATIBILITY.md) を参照します。
この公開文書に特定個体の認証結果・測定値・アドレスは含めません。
'''.encode('utf-8')
    if edition == 'full_access':
        for name in ('NETWORK_MODES.md', 'MOBILE_VIEWER.md', 'PHONE_LATENCY.md'):
            files[f'docs/{name}'] = _public_mobile()
    files['src/gtbms/edition.json'] = _json({'schema_version': 1, 'id': edition})
    files['PACKAGE.json'] = _json({
        'schema_version': 1, 'edition': edition, 'version': version, 'build_id': build_id,
        'source_only': True, 'default_bms_mode': 'mock', 'python_requires': '>=3.12',
        'corporate_identity_implemented': False, 'qualification_code_implemented': False,
        'files_sha256': {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
    })
    return files


def build_releases(root: Path, output: Path, editions: tuple[str, ...] = EDITIONS,
                   build_id: str | None = None) -> list[dict]:
    root = root.resolve()
    build_id = build_id or datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', build_id):
        raise ReleaseError('Invalid build identifier')
    if not editions or len(set(editions)) != len(editions) or any(e not in EDITIONS for e in editions):
        raise ReleaseError('Invalid edition selection')
    version = tomllib.loads(_read(root, 'pyproject.toml').decode('utf-8'))['project']['version']
    if not isinstance(version, str) or not re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,3}(?:[A-Za-z0-9.-]+)?', version):
        raise ReleaseError('Invalid project version')
    prepared = [(edition, f'gtbms-windows-{version}-{edition}-{build_id}-source.zip',
                 collect_payload(root, edition, version, build_id)) for edition in editions]
    if any((output / name).exists() for _, name, _ in prepared):
        raise ReleaseError('Release already exists; select a new build identifier')
    output.mkdir(parents=True, exist_ok=True)
    results = []
    for edition, filename, files in prepared:
        target = output / filename
        with zipfile.ZipFile(target, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in sorted(files.items()):
                archive.writestr(name, data)
        with zipfile.ZipFile(target) as archive:
            if archive.testzip() is not None or set(archive.namelist()) != set(files):
                raise ReleaseError('Archive integrity verification failed')
            for name, data in files.items():
                if archive.read(name) != data:
                    raise ReleaseError('Archive content verification failed')
        results.append({'edition': edition, 'file': filename, 'version': version, 'build_id': build_id,
                        'files': len(files), 'bytes': target.stat().st_size,
                        'sha256': hashlib.sha256(target.read_bytes()).hexdigest()})
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--edition', choices=(*EDITIONS, 'all'), default='all')
    parser.add_argument('--build-id')
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    try:
        results = build_releases(root, args.output_dir or root / 'artifacts/releases',
                                 EDITIONS if args.edition == 'all' else (args.edition,), args.build_id)
    except (ReleaseError, OSError, ValueError, KeyError) as error:
        print(json.dumps({'ok': False, 'error': str(error) if isinstance(error, ReleaseError) else 'Build input/output failed'}))
        return 1
    print(json.dumps({'ok': True, 'releases': results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
