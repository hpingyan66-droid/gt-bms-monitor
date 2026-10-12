# GT BMS Android prototype 0.2.1

JBD BMSをAndroid端末から読み取る試作アプリです。Windows版とは独立したプロジェクトとして、このディレクトリに追加しています。

## 対応する機能

- JBD 03/04読み取り、BLE接続診断、セル電圧・電流・温度・保護状態の表示。
- GPS、加速度、ジャイロ、姿勢、気圧による走行記録と、短時間のGPS欠測推定。
- 良好なGPSとBMS放電区間を使う端末内の電費学習、航続距離・時間の参考予測。
- 元の推定値を残すGPS復帰時の再計算、BMS/走行CSV保存。

0.2.1では固定ON/OFFを撤去し、常に車体へ固定する前提にしました。旧mounted=falseでも新規記録では慣性推定が有効です。

BMSの生電流と符号を保存・表示し、JBD仕様の「＋充電・−放電」で解釈します。画面電力は電圧×生電流。CSV末尾のraw_power_wも同じ意味です。既存power_wは互換性のため放電を正とする計算用の意味を維持します。過去の記録を書き換えません。

放電量と充電・回生量は別積算し、サンプル間で符号が変わる場合はゼロ交差で分割します。充電方向の電流だけから発生源を回生と断定しません。BMSへの設定書込みは行いません。

## 検証

```sh
python3 scripts/test_host.py
python3 scripts/check_android.py --android-jar /path/to/sdk/platforms/android-35/android.jar --build-tools /path/to/sdk/build-tools/35.0.0
```

JDK 17を使用します。ホストテスト2,612件、Android API 35でアプリ本体・instrumentationソースのコンパイル、リソースとDEXの生成を確認しています。check_android.pyは署名鍵を使わず、未署名の検証用APKをbuild配下へ生成します。インストール・実走精度の試験ではありません。

GitHub ActionsでもホストテストとAndroidコンパイルを実行します。詳細は[VALIDATION.md](docs/VALIDATION.md)。

## 配布APKとの違い

このコミットはソースの公開・統合です。0.2.1の上書き更新用署名済みAPKは未発行です。既存アプリへ上書きするには、以前の配布版と同じ署名鍵が必要です。build_local.pyは元の証明書との一致を確認し、代替鍵を自動生成しません。

通常のGradle/Android Studioで別のdebug鍵を使う場合は、新規の開発端末・エミュレーターで確認してください。署名エラー回避のために既存の記録入りアプリを削除する必要はありません。

## 推定の範囲

初回の有効なGPS測位が必要です。欠測1.5〜60秒は条件が揃えばIMUを使い、条件不足や60秒超では速度保持へ移ります。GPS復帰後も途中経路は推定です。全IMU原波形の保存、GPS欠測中の高度な停車判定、無線測位補助は未実装です。

- [計算・保存の仕様](docs/TRIP_ALGORITHM.md)
- [GPS欠測対策の追加候補](docs/GPS_FOLLOWUP.md)
- [基地局・Wi-Fi・Bluetoothの補助案](docs/RADIO_ASSIST_PROPOSAL.md)

位置ログ、端末DB、署名秘密鍵、認証情報は公開ソースに含みません。ライセンスはLICENSEとapp/src/main/assets/LICENSES.txtを参照してください。
