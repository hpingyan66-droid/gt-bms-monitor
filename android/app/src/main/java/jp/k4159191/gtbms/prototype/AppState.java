package jp.k4159191.gtbms.prototype;
import jp.k4159191.gtbms.prototype.core.Sample;
final class AppState {
    static volatile Sample latest;
    static volatile String deviceKey="";
    static volatile String status="停止中", detail="Bluetooth接続、またはデモを選んでください", storageError="";
    static volatile boolean active=false, monitoring=false;
    static volatile long recorded=0, storageLosses=0;
    static volatile int reconnects=0, invalidFrames=0;
    private AppState(){}
}
