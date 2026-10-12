# 無線によるGPS欠測補助の案

この文書は設計候補です。0.2.1には未実装です。通常のAndroid APIと実行時の対応状況を確認しながら追加します。

## 実装順

1. Androidのネットワーク測位を低信頼度の補助観測として扱う。取得時刻・経過時間・精度・推定位置との残差を確認し、古い測位や急な位置変化をそのまま距離に加えない。
2. 良好なGPSがある区間で固定APの識別情報と電波強度の特徴を学習し、同じ経路での位置候補にする。地図更新・APの移動や交換・強度変動を扱う。
3. Wi-Fi RTTやBluetooth Channel Soundingの端末能力と相手機器の対応を調べ、条件が揃う場所で測距する。

加速度・ジャイロで短い時間の動きを追い、無線位置が得られたときに精度に応じて補正する。ネットワーク測位と融合測位を独立した証拠として二重に加重しない。推定・補正区間は従来のGPS放電学習へ無条件に加えない。

## 方法別の条件

| 方法 | 利用する情報 | 条件 |
| --- | --- | --- |
| ネットワーク測位 | 基地局、Wi-Fi等を用いたOSの位置推定 | プロバイダーと環境によって精度が変わり、通信が必要な場合がある |
| Wi-Fiの電波の特徴 | 周囲のAPとRSSIの組み合わせ | 学習地点が必要。単純な強度→距離換算を確定値にしない |
| Wi-Fi RTT | 対応機器との往復時間 | スマホとAPの対応が必要。位置算出にはAP座標等も必要 |
| Bluetoothビーコン | 固定機器の識別子・強度 | 設置位置が既知の機器を通過地点等の基準に使う |
| Bluetooth Channel Sounding | 位相差・往復時間 | 対応ハードウェアと相手機器、利用可能なAPIが必要 |

自車とともに移動するBMS・ナビ・アクセスポイントは、地面に対する移動の固定基準にしない。トンネル内の反射や見通しの悪さにより、電波強度・測距も誤差を持つ。平面位置だけで高架・地上・地下の階層を確定しない。

壁や路面の反射波から車速を測るレーダー方式は、対応機器間のRTT測距と別の機能。通常の権限追加だけで利用できると仮定せず、端末のハードウェア・公開APIを調べる。

Wi-Fi探索にはAndroidの頻度制限がある。全センサと同じ毎秒頻度を前提にせず、通常運用で探索制限を回避する設計にしない。必要な権限は実装した機能ごとに追加し、測位用途なのにneverForLocationを宣言しない。

## 一次資料

- Android network location: https://source.android.com/docs/automotive/location/coarse-location
- Wi-Fi RTT: https://developer.android.com/develop/connectivity/wifi/wifi-rtt
- Android ranging: https://developer.android.com/develop/connectivity/ranging
- Wi-Fi scan limitations: https://developer.android.com/develop/connectivity/wifi/wifi-scan
- Bluetooth Channel Sounding: https://www.bluetooth.com/learn-about-bluetooth/feature-enhancements/channel-sounding/
