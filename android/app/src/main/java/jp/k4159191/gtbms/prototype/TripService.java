package jp.k4159191.gtbms.prototype;

import android.Manifest;
import android.app.*;
import android.content.*;
import android.content.pm.*;
import android.hardware.*;
import android.location.*;
import android.os.*;
import java.util.*;
import jp.k4159191.gtbms.prototype.core.*;

/** User-started location foreground service; no background launch, no network, no BMS writes. */
public final class TripService extends Service implements SensorEventListener,LocationListener {
    public static final String START="trip_start",STOP="trip_stop";
    private HandlerThread thread;private Handler worker;
    private LocationManager locations;private SensorManager sensors;private PowerManager.WakeLock wake;
    private TripEngine engine;private EnergyModel model;private TripProfile profile;private TripStore store;
    private String id="",modelKey="";private boolean running,hasLinear;private volatile boolean starting,stopping;
    private final float[] rotation=new float[9];private long rotationTime=-1,gyroTime=-1;private double gyroUp,declination;
    private long began,lastNotification,lastModelSave,lastWake;
    private double wh,regen,covered,uncovered;
    private String sensorDescription="";
    @Override public void onCreate(){super.onCreate();
        thread=new HandlerThread("bms-trip");thread.start();worker=new Handler(thread.getLooper());
        locations=getSystemService(LocationManager.class);sensors=getSystemService(SensorManager.class);
        wake=getSystemService(PowerManager.class).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK,"gtbms:trip");wake.setReferenceCounted(false);
        NotificationChannel ch=new NotificationChannel("bms_trip","走行記録",NotificationManager.IMPORTANCE_LOW);ch.setSound(null,null);ch.enableVibration(false);getSystemService(NotificationManager.class).createNotificationChannel(ch);
    }
    @Override public int onStartCommand(Intent i,int flags,int startId){
        if(i==null||STOP.equals(i.getAction())){stopSelf();return START_NOT_STICKY;}
        if(!START.equals(i.getAction())||starting)return START_NOT_STICKY;
        if(TripState.exporting){TripState.error="CSV保存が終わってから記録を開始してください";stopSelf();return START_NOT_STICKY;}
        if(checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)!=PackageManager.PERMISSION_GRANTED||!ConnectionAccess.locationOn(this)){
            TripState.error="正確な位置情報の許可と位置情報ONが必要です";stopSelf();return START_NOT_STICKY;}
        starting=true;TripState.active=true;TripState.error="";TripState.status="走行記録を開始しています";
        try{if(Build.VERSION.SDK_INT>=29)startForeground(20,notification(),ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION);else startForeground(20,notification());worker.post(this::begin);}
        catch(RuntimeException e){TripState.error="走行開始失敗: "+e.getClass().getSimpleName()+"。画面を表示して位置情報の権限を確認してください";stopSelf();}
        return START_NOT_STICKY;
    }
    private void begin(){try{
        if(stopping)return;profile=new TripProfile(this);engine=new TripEngine(profile.maxKmh,profile.mounted);model=new EnergyModel();store=TripStore.get(this);store.recoverInterrupted();
        id=UUID.randomUUID().toString();began=SystemClock.elapsedRealtime();store.begin(id,System.currentTimeMillis(),profile);running=true;
        hasLinear=register(Sensor.TYPE_LINEAR_ACCELERATION,40000);boolean accel=register(Sensor.TYPE_ACCELEROMETER,40000);
        boolean gyro=register(Sensor.TYPE_GYROSCOPE,40000),rotation=register(Sensor.TYPE_ROTATION_VECTOR,40000),pressure=register(Sensor.TYPE_PRESSURE,200000);
        sensorDescription="加速度 "+yn(accel)+" / 線形加速度 "+yn(hasLinear)+"\nジャイロ "+yn(gyro)+" / 姿勢融合 "+yn(rotation)+" / 気圧 "+yn(pressure)+"\n高度: GPS"+(pressure?"＋気圧による相対高度":"（気圧なし）")+"\n端末固定を前提に測定"+(!rotation||!gyro?"\n慣性推定に必要なセンサー不足: 速度保持へ切替":"");
        TripState.sensors=sensorDescription;
        if(locations.getAllProviders().contains(LocationManager.GPS_PROVIDER))locations.requestLocationUpdates(LocationManager.GPS_PROVIDER,1000,0,this,thread.getLooper());
        else TripState.error="GPSプロバイダーがありません。座標・距離は測位待ちになります";
        worker.post(tick);
    }catch(Exception e){fail("開始処理: "+e.getClass().getSimpleName());}}
    private boolean register(int type,int period){Sensor s=sensors.getDefaultSensor(type);return s!=null&&sensors.registerListener(this,s,period,0,worker);}
    private static String yn(boolean b){return b?"あり":"なし";}
    @Override public void onSensorChanged(SensorEvent event){
        if(!running)return;long now=event.timestamp/1000000;float[] v=event.values;
        switch(event.sensor.getType()){
            case Sensor.TYPE_ROTATION_VECTOR:
                if(event.accuracy==SensorManager.SENSOR_STATUS_UNRELIABLE){rotationTime=-1;break;}
                SensorManager.getRotationMatrixFromVector(rotation,v);rotationTime=now;break;
            case Sensor.TYPE_PRESSURE:engine.pressure(now,v[0]);break;
            case Sensor.TYPE_GYROSCOPE:
                if(now-rotationTime<1000&&rotationTime>=0){gyroUp=rotation[6]*v[0]+rotation[7]*v[1]+rotation[8]*v[2];gyroTime=now;}break;
            case Sensor.TYPE_LINEAR_ACCELERATION:
            case Sensor.TYPE_ACCELEROMETER:
                if(event.sensor.getType()==Sensor.TYPE_ACCELEROMETER&&hasLinear)break;
                if(rotationTime<0||now-rotationTime>1000||gyroTime<0||now-gyroTime>1000)break;
                double e=rotation[0]*v[0]+rotation[1]*v[1]+rotation[2]*v[2],n=rotation[3]*v[0]+rotation[4]*v[1]+rotation[5]*v[2];
                // Rotation-vector north is magnetic; GNSS bearing is true north.
                engine.imu(now,e*Math.cos(declination)+n*Math.sin(declination),n*Math.cos(declination)-e*Math.sin(declination),gyroUp);break;
        }
    }
    @Override public void onAccuracyChanged(Sensor s,int accuracy){}
    @Override public void onLocationChanged(Location l){
        if(!running)return;TripEngine.Fix f=new TripEngine.Fix();f.elapsed=l.getElapsedRealtimeNanos()/1000000;f.lat=l.getLatitude();f.lon=l.getLongitude();f.accuracy=l.hasAccuracy()?l.getAccuracy():Double.NaN;
        f.speed=l.hasSpeed()?l.getSpeed():Double.NaN;f.speedAccuracy=l.hasSpeedAccuracy()?l.getSpeedAccuracyMetersPerSecond():Double.NaN;
        f.bearing=l.hasBearing()&&(!l.hasBearingAccuracy()||l.getBearingAccuracyDegrees()<=45)?l.getBearing():Double.NaN;
        f.altitude=l.hasAltitude()?l.getAltitude():Double.NaN;f.verticalAccuracy=l.hasVerticalAccuracy()?l.getVerticalAccuracyMeters():Double.NaN;f.mock=l.isFromMockProvider();
        if(engine.offer(f,SystemClock.elapsedRealtime()))declination=Math.toRadians(new GeomagneticField((float)f.lat,(float)f.lon,(float)(Double.isFinite(f.altitude)?f.altitude:0),System.currentTimeMillis()).getDeclination());
    }
    @Override public void onProviderDisabled(String provider){TripState.error="GPSがOFFです。再開すると記録区間を補正します";}
    @Override public void onProviderEnabled(String provider){TripState.error="";}
    @Override public void onStatusChanged(String provider,int status,Bundle extras){}
    private final Runnable tick=new Runnable(){public void run(){if(!running)return;try{
        long now=SystemClock.elapsedRealtime();TripEngine.Update update=engine.step(now,System.currentTimeMillis());TripEngine.Point p=update.point;
        Sample s=AppState.latest;String device=AppState.deviceKey;
        String nextKey=s!=null&&!device.isEmpty()?device+"|"+s.direction+"|"+s.configuredAh+"|"+profile.revision:"";
        if(!nextKey.isEmpty()&&!nextKey.equals(modelKey)){
            store.saveModel(modelKey,model);model=store.loadModel(nextKey);modelKey=nextKey;getSharedPreferences("trip_profile",0).edit().putString("last_model_key",modelKey).apply();
        }
        double oldWh=model.consumedWh,oldRegen=model.regenWh,oldCovered=model.coveredSeconds,oldUncovered=model.uncoveredSeconds;
        model.update(p,s,AppState.monitoring&&profile.signConfirmed,device,now);
        wh+=model.consumedWh-oldWh;regen+=model.regenWh-oldRegen;covered+=model.coveredSeconds-oldCovered;uncovered+=model.uncoveredSeconds-oldUncovered;
        TripState.meters=engine.totalMeters();TripState.status="記録中 · "+sourceName(p.source);
        TripState.details=fmt("距離  %.3f km\n推定・補正推定を含む距離  %.3f km\n速度  %s\n経過  %s\n放電  %.1f Wh / 回生・充電 %.1f Wh\nBMS有効 %.0f秒 / 欠測 %.0f秒\n高度（GPS基準）  %s / 気圧 %s\n参考勾配  %s\nGPS復旧補正 %d回（距離差 %+.1fm）\nGPS状態: %s\nGPS除外 %d件",
            engine.totalMeters()/1000,engine.estimatedTotal()/1000,number(p.speed*3.6,"%.1f km/h"),duration((now-began)/3600000.0),wh,regen,covered,uncovered,number(p.altitude,"%.1f m"),number(p.pressure,"%.1f hPa"),number(p.grade,"%+.1f %%"),engine.corrections,engine.correctedMeters,engine.gpsAge(now)>3000?"GPS欠測中":engine.fixIssue,engine.rejectedFixes);
        if(engine.gpsAge(now)>3000)TripState.details+=fmt("\n最終GPS %.0f秒前 · 位置の誤差目安 ±%.0fm\n停止・旋回は判別できない場合があります",engine.gpsAge(now)/1000.0,p.error);
        EnergyModel.Forecast forecast=model.forecast(s,AppState.monitoring,now,p.speed*3.6,profile.reserve,profile.manualRate,profile.signConfirmed);
        TripState.prediction=forecast.available?fmt("予備 %.0f%% まで %.1f km\n参考幅 %.1f〜%.1f km\n予想連続運転 %s\n残量0%%までの参考時間 %s\n現在 %s / 予備 %.0f%%\n%s · %.1f Wh/km",profile.reserve,forecast.km,forecast.lowKm,forecast.highKm,duration(forecast.reserveHours),duration(forecast.emptyHours),s==null?"—":fmt("%.0f%%",s.soc),profile.reserve,forecast.basis,forecast.whPerKm):"予測待ち\n"+forecast.reason;
        EnergyModel.Bin b=model.bins[0];TripState.learning=fmt("この車両・電池の学習  %.2f km / %.0f秒\n100m単位の有効区間 %d回\n%s",model.eligibleMeters()/1000,model.eligibleSeconds(),b.windows,profile.signConfirmed?model.reason:"電流方向の実測確認を待っています");
        store.save(id,update,TripStore.telemetry(s,AppState.monitoring&&s!=null&&s.fresh(now),device),engine,wh,regen,covered,uncovered,TripState.details);
        if(now-lastModelSave>=15000||update.recovered){store.saveModel(modelKey,model);lastModelSave=now;}
        if(now-lastWake>=60000){if(wake.isHeld())wake.release();wake.acquire(120000);lastWake=now;}
        if(now-lastNotification>=15000){getSystemService(NotificationManager.class).notify(20,notification());lastNotification=now;}
        worker.postDelayed(this,1000);
    }catch(Exception e){fail("走行保存・計算: "+e.getClass().getSimpleName());}}};
    private void fail(String message){TripState.error=message;TripState.status="記録を中断しました";ConnectionLog.add("走行記録",message);stopSelf();}
    private Notification notification(){
        PendingIntent open=PendingIntent.getActivity(this,20,new Intent(this,TripActivity.class),PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
        PendingIntent stop=PendingIntent.getService(this,21,new Intent(this,TripService.class).setAction(STOP),PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this,"bms_trip").setSmallIcon(R.drawable.ic_battery).setContentTitle("GT BMS · 走行記録中").setContentText(fmt("%.2f km · タップで詳細",TripState.meters/1000)).setContentIntent(open).setOnlyAlertOnce(true).setOngoing(true).addAction(new Notification.Action.Builder(null,"走行記録を終了",stop).build()).build();
    }
    private void finish(){
        running=false;worker.removeCallbacksAndMessages(null);
        try{locations.removeUpdates(this);}catch(RuntimeException ignored){}sensors.unregisterListener(this);
        try{if(store!=null&&!id.isEmpty()){store.saveModel(modelKey,model);store.end(id,System.currentTimeMillis(),TripState.error.isEmpty()?"complete":"interrupted");}}catch(Exception e){TripState.error="終了時の保存に失敗: "+e.getClass().getSimpleName();}
        if(wake.isHeld())wake.release();TripState.active=false;TripState.status=TripState.error.isEmpty()?"走行記録を終了しました":"走行記録は中断しています";thread.quitSafely();
    }
    @Override public void onDestroy(){stopping=true;worker.post(this::finish);stopForeground(STOP_FOREGROUND_REMOVE);super.onDestroy();}
    @Override public IBinder onBind(Intent i){return null;}
    static String fmt(String f,Object... values){return String.format(Locale.JAPAN,f,values);}
    static String number(double x,String f){return Double.isFinite(x)?fmt(f,x):"—";}
    static String duration(double hours){if(!Double.isFinite(hours))return "—（速度2km/h以上で表示）";if(hours>999)return "999時間超（参考）";long min=Math.max(0,Math.round(hours*60));return (min/60)+"時間 "+(min%60)+"分";}
    static String sourceName(String s){switch(s){case "gps_speed":return "GPS速度";case "gps_position":return "GPS位置";case "gps_anchor":return "GPS初期測位";case "corrected_estimate":return "GPS復旧・補正推定";case "imu_estimate":return "加速度・ジャイロ推定";case "speed_hold":return "速度保持の推定";case "hold_uncertain":return "長いGPS欠測・誤差大";case "clock_gap":case "unresolved_gap":return "欠測区間あり";default:return "GPS待ち";}}
}
