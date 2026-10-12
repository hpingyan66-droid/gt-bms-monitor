package jp.k4159191.gtbms.prototype;

import android.app.*;
import android.bluetooth.*;
import android.content.*;
import android.content.pm.ServiceInfo;
import android.os.*;
import java.security.MessageDigest;
import java.nio.charset.StandardCharsets;
import java.util.*;
import jp.k4159191.gtbms.prototype.core.*;

/** All GATT operations run on one main-looper queue; one request in flight. */
public final class MonitorService extends Service {
    public static final String START="start", STOP="stop";
    private final Handler h=new Handler(Looper.getMainLooper());
    private BluetoothGatt gatt; private BluetoothGattCharacteristic tx;
    private JbdProtocol.Decoder decoder=new JbdProtocol.Decoder();
    private byte[] basic,reply; private int expected=0,attempt=0,expectedCells=16;
    private boolean running=false,writeAck=false,responseWrite=true;
    private String address,session,deviceKey,writePreference="auto",phase="準備";
    private long notificationBytes=0;private boolean firstSample=true,subscriptionPending=false;
    private long basicWall,basicElapsed,cycleStarted,lastNotification=0;
    private PowerManager.WakeLock wake;
    private final Runnable timeout=()->fail(String.format(Locale.US,"BMS応答待ち（cmd=%02X, writeAck=%s, RX=%d bytes）。送信方式や他アプリの接続を確認してください",expected,writeAck,notificationBytes));
    private final Runnable connectTimeout=()->fail(phase+"が20秒以内に完了しませんでした。他のBMSアプリ・PCの接続を確認してください");
    private final Runnable retry=this::connect;
    private final Runnable poll=()->{cycleStarted=SystemClock.elapsedRealtime();send(3);};
    private final Runnable heartbeat=new Runnable(){public void run(){
        if(!running)return;
        if(wake!=null){if(wake.isHeld())wake.release();wake.acquire(120000);}
        if(AppState.latest!=null && !AppState.latest.fresh(SystemClock.elapsedRealtime()) && AppState.monitoring){
            AppState.monitoring=false;AppState.status="更新待ち";AppState.detail="最後の受信から6秒以上経過";updateNotification();
        }
        h.postDelayed(this,30000);
    }};
    @Override public void onCreate(){super.onCreate();
        ConnectionLog.init(this);
        NotificationChannel ch=new NotificationChannel("bms_monitor","BMS監視",NotificationManager.IMPORTANCE_LOW);
        ch.setSound(null,null);ch.enableVibration(false);getSystemService(NotificationManager.class).createNotificationChannel(ch);
        wake=((PowerManager)getSystemService(POWER_SERVICE)).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK,"gtbms:monitor");wake.setReferenceCounted(false);
    }
    @Override public int onStartCommand(Intent intent,int flags,int id){
        if(intent==null || STOP.equals(intent.getAction())){stopMonitor();stopSelf();return START_NOT_STICKY;}
        if(!START.equals(intent.getAction()))return START_NOT_STICKY;
        stopMonitor();
        address=intent.getStringExtra("address");
        if(address==null || !BluetoothAdapter.checkBluetoothAddress(address)){stopSelf();return START_NOT_STICKY;}
        SharedPreferences prefs=getSharedPreferences("settings",MODE_PRIVATE);expectedCells=prefs.getInt("cells",16);writePreference=prefs.getString("write_mode","auto");
        session=UUID.randomUUID().toString();deviceKey=key(address);AppState.deviceKey=deviceKey;running=true;AppState.active=true;AppState.latest=null;AppState.reconnects=0;AppState.invalidFrames=0;attempt=0;
        AppState.status="接続準備";AppState.detail="BMSを読み取ります";
        try{
            if(Build.VERSION.SDK_INT>=29)startForeground(10,notification(),ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE);else startForeground(10,notification());
            h.post(heartbeat);connect();
        }catch(RuntimeException e){fatal("監視開始失敗: "+e.getClass().getSimpleName()+" / 付近のデバイスの権限とアプリの表示状態を確認してください");}
        return START_NOT_STICKY;
    }
    private static String key(String address){try{
        byte[] b=MessageDigest.getInstance("SHA-256").digest(address.getBytes(StandardCharsets.UTF_8));StringBuilder s=new StringBuilder();for(int i=0;i<8;i++)s.append(String.format(Locale.US,"%02x",b[i]&255));return s.toString();
    }catch(Exception e){return "unknown";}}
    private void connect(){
        if(!running)return;closeGatt();AppState.monitoring=false;firstSample=true;notificationBytes=0;
        state("Bluetooth接続","選択した機器へ接続しています");
        try{
            BluetoothAdapter a=((BluetoothManager)getSystemService(BLUETOOTH_SERVICE)).getAdapter();
            if(a==null || !a.isEnabled()){fail("Bluetoothがオフです。オンになると再接続します");return;}
            decoder=new JbdProtocol.Decoder();gatt=a.getRemoteDevice(address).connectGatt(this,false,callbacks,BluetoothDevice.TRANSPORT_LE);
            if(gatt==null){fail("Bluetooth接続を開始できません（GATT=null）");return;}armConnectTimeout();
        }catch(SecurityException e){fatal("Bluetoothの権限が取り消されました（SecurityException）");}catch(RuntimeException e){fail("Bluetooth接続を開始できません: "+e.getClass().getSimpleName());}
    }
    private void state(String stage,String detail){phase=stage;AppState.status=stage;AppState.detail=detail;ConnectionLog.add(stage,detail);updateNotification();}
    private void armConnectTimeout(){h.removeCallbacks(connectTimeout);h.postDelayed(connectTimeout,20000);}
    private void gattFailure(String stage,int code){String reason=stage+": status="+code+" / "+BleDiagnostics.gattStatus(code);if(code==5||code==15)fatal(reason);else fail(reason);}
    private final BluetoothGattCallback callbacks=new BluetoothGattCallback(){
        @Override public void onConnectionStateChange(BluetoothGatt g,int status,int state){h.post(()->{
            if(g!=gatt || !running)return;
            ConnectionLog.add("GATT接続結果","status="+status+" / state="+state+" / "+BleDiagnostics.gattStatus(status));
            if(status!=BluetoothGatt.GATT_SUCCESS){gattFailure("Bluetooth接続",status);return;}
            if(state==BluetoothProfile.STATE_DISCONNECTED){fail("Bluetoothが切断されました（status=0）");return;}
            if(state==BluetoothProfile.STATE_CONNECTED)try{state("サービス探索","Bluetooth接続成立。JBDサービスを確認しています");armConnectTimeout();if(!g.discoverServices())fail("サービス探索を開始できません（discoverServices=false）");}catch(SecurityException e){fatal("Bluetooth権限を確認してください（サービス探索）");}
        });}
        @Override public void onServicesDiscovered(BluetoothGatt g,int status){h.post(()->{
            if(g!=gatt || !running)return;
            if(status!=BluetoothGatt.GATT_SUCCESS){gattFailure("サービス探索",status);return;}
            int listed=0;for(BluetoothGattService s:g.getServices())for(BluetoothGattCharacteristic c:s.getCharacteristics())if(listed++<32)ConnectionLog.add("GATT構成",s.getUuid()+" / "+c.getUuid()+" / props=0x"+Integer.toHexString(c.getProperties()));
            BluetoothGattCharacteristic rx=null;tx=null;
            for(BluetoothGattService s:g.getServices())if(s.getCharacteristic(JbdProtocol.RX)!=null && s.getCharacteristic(JbdProtocol.TX)!=null){rx=s.getCharacteristic(JbdProtocol.RX);tx=s.getCharacteristic(JbdProtocol.TX);break;}
            if(rx==null || tx==null){fatal("通常JBDのFF01/FF02が見つかりません。接続診断のGATT構成を確認してください");return;}
            int subscription=BleDiagnostics.subscription(rx.getProperties());
            if(subscription==0){fatal("FF01が通知・Indicationに対応していません");return;}
            int mode=BleDiagnostics.writeMode(tx.getProperties(),writePreference);responseWrite=mode==2;
            if(mode==0){fatal("選択した送信方式は機器が未対応です。設定のBLE送信方式を自動に戻してください");return;}
            BluetoothGattDescriptor cccd=rx.getDescriptor(UUID.fromString("00002902-0000-1000-8000-00805f9b34fb"));
            if(cccd==null){fatal("通知設定が見つかりません");return;}
            try{
                state("通知設定","JBDのFF01/FF02を確認。"+(subscription==1?"Notify":"Indicate")+" / 送信="+(responseWrite?"応答あり":"応答なし"));armConnectTimeout();
                if(!g.setCharacteristicNotification(rx,true)){fail("通知を登録できません");return;}
                byte[] enable=subscription==1?BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE:BluetoothGattDescriptor.ENABLE_INDICATION_VALUE;subscriptionPending=true;
                boolean ok;
                if(Build.VERSION.SDK_INT>=33){int result=g.writeDescriptor(cccd,enable);ok=result==BluetoothStatusCodes.SUCCESS;if(!ok)ConnectionLog.add("通知設定開始失敗","BluetoothStatusCodes="+result);}
                else{cccd.setValue(enable);ok=g.writeDescriptor(cccd);}
                if(!ok)fail("通知設定を送信できません");
            }catch(SecurityException e){fatal("Bluetooth権限を確認してください");}
        });}
        @Override public void onDescriptorWrite(BluetoothGatt g,BluetoothGattDescriptor d,int status){h.post(()->{
            if(g!=gatt || !running || !subscriptionPending || !UUID.fromString("00002902-0000-1000-8000-00805f9b34fb").equals(d.getUuid()))return;
            subscriptionPending=false;if(status!=BluetoothGatt.GATT_SUCCESS){gattFailure("通知設定",status);return;}
            h.removeCallbacks(connectTimeout);state("BMS応答待ち","通知設定が完了。03/04の応答を確認しています");h.postDelayed(poll,250);
        });}
        @Override public void onCharacteristicWrite(BluetoothGatt g,BluetoothGattCharacteristic c,int status){h.post(()->{
            if(g!=gatt || !running || !JbdProtocol.TX.equals(c.getUuid()))return;
            if(status!=BluetoothGatt.GATT_SUCCESS){gattFailure("読取要求の送信",status);return;}if(!responseWrite||expected==0)return;writeAck=true;finishRequest();
        });}
        @Override public void onCharacteristicChanged(BluetoothGatt g,BluetoothGattCharacteristic c,byte[] value){byte[] copy=value.clone();h.post(()->receive(g,c.getUuid(),copy));}
        @Override public void onCharacteristicChanged(BluetoothGatt g,BluetoothGattCharacteristic c){if(Build.VERSION.SDK_INT<33){byte[] v=c.getValue();if(v!=null){byte[] copy=v.clone();h.post(()->receive(g,c.getUuid(),copy));}}}
    };
    private void receive(BluetoothGatt g,UUID id,byte[] data){
        if(g!=gatt || !running || !JbdProtocol.RX.equals(id))return;
        if(notificationBytes==0)ConnectionLog.add("BLE受信","通知を受信。データ長="+data.length);notificationBytes+=data.length;
        for(byte[] f:decoder.feed(data)){
            if(expected!=0 && (f[1]&255)==expected && reply==null){
                reply=f;if(expected==3){basicWall=System.currentTimeMillis();basicElapsed=SystemClock.elapsedRealtime();}finishRequest();
            }
        }AppState.invalidFrames=decoder.invalidFrames;
    }
    private void send(int command){
        if(!running || gatt==null || tx==null)return;
        if(expected!=0){fail("読取要求が重複しました");return;}
        byte[] bytes=JbdProtocol.request(command);expected=command;reply=null;writeAck=false;
        if(firstSample)ConnectionLog.add("読取要求",String.format(Locale.US,"cmd=%02X / %s",command,responseWrite?"応答あり":"応答なし"));
        int type=responseWrite?BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT:BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE;
        try{
            boolean ok;
            if(Build.VERSION.SDK_INT>=33){int result=gatt.writeCharacteristic(tx,bytes,type);ok=result==BluetoothStatusCodes.SUCCESS;if(!ok)ConnectionLog.add("送信開始失敗","BluetoothStatusCodes="+result);}
            else{tx.setWriteType(type);tx.setValue(bytes);ok=gatt.writeCharacteristic(tx);}
            if(!ok){fail("読取要求を送信できません");return;}
            h.postDelayed(timeout,3500);
            if(!responseWrite){writeAck=true;finishRequest();}
        }catch(SecurityException e){fatal("Bluetooth権限を確認してください");}catch(RuntimeException e){fail("読み取り中に通信エラーが発生しました");}
    }
    private void finishRequest(){
        if(!writeAck || reply==null || expected==0)return;
        int command=expected;byte[] f=reply;expected=0;reply=null;h.removeCallbacks(timeout);
        if(command==3){basic=f;BluetoothGatt connection=gatt;h.postDelayed(()->{if(running && gatt==connection && connection!=null)send(4);},80);return;}
        try{
            Sample sample=JbdProtocol.parseLive(basic,f,basicWall,basicElapsed,expectedCells);
            if(firstSample){ConnectionLog.add("読取成功","有効な03/04ペアを確認 / セル="+sample.cells.length);firstSample=false;}
            AppState.latest=sample;AppState.status="受信中";AppState.detail="1秒周期で読み取り中";AppState.monitoring=true;attempt=0;
            HistoryStore.get(this).save(sample,session,deviceKey);
            if(SystemClock.elapsedRealtime()-lastNotification>15000)updateNotification();
            h.postDelayed(poll,Math.max(100,1000-(SystemClock.elapsedRealtime()-cycleStarted)));
        }catch(IllegalArgumentException e){fail("測定データの形式が一致しません: "+e.getMessage());}
    }
    private void fail(String message){
        if(!running)return;ConnectionLog.add(phase+"失敗",message);closeGatt();AppState.monitoring=false;AppState.status="再接続待ち";
        int delay=Math.min(30,1<<Math.min(++attempt,5));AppState.reconnects++;AppState.detail=message+"（"+delay+"秒後）";updateNotification();h.removeCallbacks(retry);h.postDelayed(retry,delay*1000L);
    }
    private void fatal(String message){ConnectionLog.add(phase+"停止",message);stopMonitor();AppState.status="接続停止";AppState.detail=message;stopSelf();}
    private void closeGatt(){
        h.removeCallbacks(timeout);h.removeCallbacks(connectTimeout);h.removeCallbacks(poll);
        // Delayed 04 tasks capture the retired GATT identity and cannot use a new connection.
        BluetoothGatt old=gatt;gatt=null;tx=null;expected=0;reply=null;basic=null;subscriptionPending=false;
        if(old!=null){try{old.disconnect();}catch(RuntimeException ignored){}try{old.close();}catch(RuntimeException ignored){}}
    }
    private void stopMonitor(){
        running=false;h.removeCallbacksAndMessages(null);closeGatt();if(wake!=null && wake.isHeld())wake.release();
        AppState.active=false;AppState.monitoring=false;AppState.status="停止中";AppState.detail="監視を停止しました。表示値は最終受信値です";stopForeground(STOP_FOREGROUND_REMOVE);
    }
    private Notification notification(){
        PendingIntent open=PendingIntent.getActivity(this,0,new Intent(this,MainActivity.class),PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
        PendingIntent stop=PendingIntent.getService(this,1,new Intent(this,MonitorService.class).setAction(STOP),PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
        String text=AppState.status;Sample s=AppState.latest;
        if(s!=null && !s.fresh(SystemClock.elapsedRealtime()) && running)text="更新待ち · 最終受信値";
        if(s!=null && AppState.monitoring && s.fresh(SystemClock.elapsedRealtime()))text+=String.format(Locale.JAPAN," · %.0f%% / %.2f V",s.soc,s.voltage);
        return new Notification.Builder(this,"bms_monitor").setSmallIcon(R.drawable.ic_battery).setContentTitle("GT BMS · 読み取り監視").setContentText(text).setContentIntent(open).setOnlyAlertOnce(true).setOngoing(true).addAction(new Notification.Action.Builder(null,"停止",stop).build()).build();
    }
    private void updateNotification(){lastNotification=SystemClock.elapsedRealtime();getSystemService(NotificationManager.class).notify(10,notification());}
    @Override public void onDestroy(){if(running)stopMonitor();h.removeCallbacksAndMessages(null);closeGatt();super.onDestroy();}
    @Override public IBinder onBind(Intent i){return null;}
}
