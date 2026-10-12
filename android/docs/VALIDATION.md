# Android 0.2.1 source validation

2026-10-12。既存Windows版への影響を避けるため、android/と新規のAndroid検証workflowのみを追加します。既存ファイルの変更・削除はありません。

## 実行済み

| 検証 | 結果 |
| --- | --- |
| プロトコル・既存コア計算 | CoreTests 2,359件通過 |
| 走行・学習・符号切替の電力積分 | TripCoreTests 243件通過 |
| 旧設定falseから新規固定モード | ProfileMigrationTests 10件通過。限定的なホストAPIスタブ使用 |
| Java構文 | Android本体・instrumentation 21ファイル通過 |
| Android API型検査 | SDK 35 + build-tools 35.0.0で本体・instrumentationソースのコンパイル通過 |
| リソース・DEX | aapt2 compile/link、D8で未署名APK生成成功 |

検証用APKの識別: jp.k4159191.gtbms.prototype / versionCode 4 / 0.2.1-prototype / minSdk 26 / targetSdk 35。

公式Android SDK repositoryのplatform-35_r02.zip、build-tools_r35_linux.zipを、配布メタデータのチェックサムと照合して使用しました。コンパイラは非推奨API利用の注意を出しますが、エラーはありません。

## 検証の限界

本版について、instrumentationの端末上実行、画面の見た目、実機BLE、トンネルでの距離精度、削除なしのAPK更新は未確認です。前版の実行結果を本版の結果として流用しません。

通常のJava構文確認とAndroid API型検査を区別します。ホスト側の設定テストはAndroidサービスやSQLiteの実行試験ではありません。GitHub Actionsは同じ計算・コンパイルを繰り返します。

配布版の署名鍵がないため、上書き更新用APKは未発行です。未署名の検証用APKを配布用として公開しません。
