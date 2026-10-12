package jp.k4159191.gtbms.prototype;

final class TripState {
    static volatile boolean active,exporting;
    static volatile String status="走行記録は停止中",details="",prediction="",sensors="",learning="",error="";
    static volatile double meters;
    private TripState(){}
}
