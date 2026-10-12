package jp.k4159191.gtbms.prototype;

import android.Manifest;
import android.app.*;
import android.bluetooth.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.location.LocationManager;
import android.os.*;
import java.util.*;
import jp.k4159191.gtbms.prototype.core.BleDiagnostics;

final class ConnectionAccess {
    static boolean allowed(Context c,String p){return c.checkSelfPermission(p)==PackageManager.PERMISSION_GRANTED;}
    static BluetoothAdapter adapter(Context c){BluetoothManager m=c.getSystemService(BluetoothManager.class);return m==null?null:m.getAdapter();}
    static boolean nearby(Context c){return Build.VERSION.SDK_INT<31 || (allowed(c,Manifest.permission.BLUETOOTH_SCAN)&&allowed(c,Manifest.permission.BLUETOOTH_CONNECT));}
    static boolean connectAllowed(Context c){return Build.VERSION.SDK_INT<31 || allowed(c,Manifest.permission.BLUETOOTH_CONNECT);}
    static boolean location(Context c){return allowed(c,Manifest.permission.ACCESS_FINE_LOCATION);}
    static boolean locationOn(Context c){try{
        LocationManager m=c.getSystemService(LocationManager.class);if(m==null)return false;
        return Build.VERSION.SDK_INT>=28?m.isLocationEnabled():m.isProviderEnabled(LocationManager.GPS_PROVIDER)||m.isProviderEnabled(LocationManager.NETWORK_PROVIDER);
    }catch(RuntimeException e){return false;}}
    static boolean bluetoothOn(Context c){try{BluetoothAdapter a=adapter(c);return a!=null&&a.isEnabled();}catch(SecurityException e){return false;}}
    static String[] missing(Context c){ArrayList<String> p=new ArrayList<>();
        if(Build.VERSION.SDK_INT>=31){if(!allowed(c,Manifest.permission.BLUETOOTH_SCAN))p.add(Manifest.permission.BLUETOOTH_SCAN);if(!allowed(c,Manifest.permission.BLUETOOTH_CONNECT))p.add(Manifest.permission.BLUETOOTH_CONNECT);}
        // Android 12+ requires requesting coarse and fine together, even for an upgrade.
        if(!location(c)){p.add(Manifest.permission.ACCESS_COARSE_LOCATION);p.add(Manifest.permission.ACCESS_FINE_LOCATION);}
        return p.toArray(new String[0]);
    }
    static boolean scanReady(Context c){return BleDiagnostics.scanReady(Build.VERSION.SDK_INT,
        Build.VERSION.SDK_INT<31||allowed(c,Manifest.permission.BLUETOOTH_SCAN),connectAllowed(c),location(c),locationOn(c),bluetoothOn(c));}
    static String summary(Context c){
        NotificationManager n=c.getSystemService(NotificationManager.class);boolean notifications=n!=null&&n.areNotificationsEnabled();
        NotificationChannel channel=n==null?null:n.getNotificationChannel("bms_monitor");if(channel!=null&&channel.getImportance()==NotificationManager.IMPORTANCE_NONE)notifications=false;
        PowerManager power=c.getSystemService(PowerManager.class);
        return "付近のデバイス："+(Build.VERSION.SDK_INT<31?"旧AndroidのBluetooth権限を使用":nearby(c)?"許可済み":"未許可")+
            "\n正確な位置情報："+(location(c)?"許可済み":allowed(c,Manifest.permission.ACCESS_COARSE_LOCATION)?"おおよそのみ（正確の許可が必要）":"未許可")+
            "\n端末の位置情報："+(locationOn(c)?"ON":"OFF")+
            "\nBluetooth："+(!connectAllowed(c)?"権限待ち":adapter(c)==null?"利用不可":bluetoothOn(c)?"ON":"OFF")+
            "\n通知："+(notifications?"許可済み":"未許可／無効（BLE接続自体は可能）")+
            "\n省電力モード："+(power!=null&&power.isPowerSaveMode()?"ON":"OFF")+
            "\n電池の最適化："+(power!=null&&power.isIgnoringBatteryOptimizations(c.getPackageName())?"対象外":"対象（長時間監視時に確認）");
    }
    private ConnectionAccess(){}
}
