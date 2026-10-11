# Windowsでの新規セットアップ

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
