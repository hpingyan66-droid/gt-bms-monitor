package jp.k4159191.gtbms.prototype.core;

/** Pure decision helpers, also exercised without a Bluetooth radio. */
public final class BleDiagnostics {
    private BleDiagnostics() {}
    public static boolean nearbyRequired(int api) { return api >= 31; }
    public static boolean notificationRequired(int api) { return api >= 33; }
    public static boolean scanReady(int api, boolean scan, boolean connect, boolean fine, boolean locationOn, boolean bluetoothOn) {
        return (!nearbyRequired(api) || (scan && connect)) && fine && locationOn && bluetoothOn;
    }
    public static String scanError(int code) {
        switch(code) {
            case 1:return "探索が既に実行中です";
            case 2:return "Androidに探索を登録できません。Bluetoothを入れ直して再試行してください";
            case 3:return "Bluetooth内部エラーです。少し待って再試行してください";
            case 4:return "この端末では要求したBLE探索が使えません";
            case 5:return "Bluetoothの処理枠が不足しています。他の接続アプリを閉じてください";
            case 6:return "探索を繰り返したため一時制限中です。30秒ほど待ってください";
            default:return "Bluetooth探索エラー";
        }
    }
    public static String gattStatus(int code) {
        switch(code) {
            case 0:return "成功";
            case 5:return "認証不足。機器側のペアリング要件を確認してください";
            case 8:return "接続タイムアウト／認可不足（発生段階を確認）";
            case 13:return "データ長が不適合";
            case 15:return "暗号化が必要です";
            case 19:return "相手機器から切断されました";
            case 22:return "端末側で接続が終了しました";
            case 62:return "接続を確立できません。他のBMSアプリ・PCの接続を解除してください";
            case 133:return "AndroidのGATT接続エラー。他の接続・距離・Bluetooth状態を確認してください";
            case 143:return "Bluetooth通信が混雑しています";
            case 257:return "GATT処理失敗";
            default:return "GATTエラー";
        }
    }
    // PROPERTY_WRITE=8, WRITE_NO_RESPONSE=4. Never use an unsupported mode.
    public static int writeMode(int properties, String preference) {
        if("without_response".equals(preference))return (properties&4)!=0?1:0;
        if("with_response".equals(preference))return (properties&8)!=0?2:0;
        return (properties&8)!=0?2:(properties&4)!=0?1:0;
    }
    // PROPERTY_NOTIFY=16 / INDICATE=32. CCCD notification=1 / indication=2.
    public static int subscription(int properties) {
        return (properties&16)!=0?1:(properties&32)!=0?2:0;
    }
}
