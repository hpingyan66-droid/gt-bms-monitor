# GT BMS Windows — フルアクセス版

バージョン 0.2.0 / ビルド 20261012-stage1 / edition `full_access`

## 初回導入

1. Windows 11にPython 3.12以降を事前にインストールします。Pythonの自動導入は行いません。
2. ZIPを一般ユーザーが書込み可能な新しいフォルダーへ展開します。運用フォルダーへ上書きしません。
3. `Quick-Install.cmd` をダブルクリックします。依存パッケージ取得にインターネット接続が必要です。
4. 完了後 `Start.cmd` をダブルクリックします。localhost:8765を開き、新規導入は模擬BMSから始まります。
5. 実機設定は `docs/HARDWARE_VALIDATION.md` を参照し、模擬での表示・保存確認を先に完了してください。

起動直後に接続できない場合は、数秒待ってブラウザーを更新してください。

Pythonを明示する場合はPowerShellで `scripts/Quick-Install.ps1 -PythonExe 'C:\Path\To\python.exe'` を使います。
導入はこのフォルダーの.venvに限定し、既存config.local.jsonは上書きしません。
Python、ドライバー、ファイアウォール、ホットスポット、タスク設定を自動変更しません。
画面を閉じるだけでは記録は止まりません。起動したコンソールでCtrl+Cを押して終了します。

## 配布範囲

管理保守とネットワーク管理を含みます。ローカルのUIと手順を確認して操作してください。

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
