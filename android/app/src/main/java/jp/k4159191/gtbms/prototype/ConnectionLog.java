package jp.k4159191.gtbms.prototype;

import android.content.*;
import android.os.Build;
import java.text.SimpleDateFormat;
import java.util.*;

/** Bounded local diagnostics. No MAC address, device name, coordinates or raw packets. */
final class ConnectionLog {
    private static SharedPreferences prefs;
    private static final ArrayDeque<String> events=new ArrayDeque<>();
    static synchronized void init(Context c){if(prefs!=null)return;prefs=c.getApplicationContext().getSharedPreferences("connection_diagnostics",0);
        String saved=prefs.getString("events","");for(String line:saved.split("\n"))if(!line.isEmpty())events.add(line);trim();}
    private static void trim(){while(events.size()>120)events.removeFirst();}
    static synchronized void add(String stage,String message){
        String safe=message.replaceAll("(?i)([0-9a-f]{2}:){5}[0-9a-f]{2}","[機器アドレス非表示]").replace('\n',' ');
        if(safe.length()>360)safe=safe.substring(0,360);
        events.add(new SimpleDateFormat("MM-dd HH:mm:ss",Locale.US).format(new Date())+" ["+stage+"] "+safe);trim();
        if(prefs!=null)prefs.edit().putString("events",String.join("\n",events)).apply();
    }
    static synchronized String report(Context c){init(c);
        String version="不明";try{version=c.getPackageManager().getPackageInfo(c.getPackageName(),0).versionName;}catch(Exception ignored){}
        return "GT BMS 接続診断 / "+version+"\nAndroid "+Build.VERSION.RELEASE+" / API "+Build.VERSION.SDK_INT+
            "\n端末："+Build.MANUFACTURER+" "+Build.MODEL+"\n\n"+ConnectionAccess.summary(c)+
            "\n\n現在："+AppState.status+"\n"+AppState.detail+"\n再接続："+AppState.reconnects+" / 不正フレーム："+AppState.invalidFrames+
            "\n\n直近の接続経過（最大120件）\n"+String.join("\n",events)+
            "\n\nこの診断にはMACアドレス・機器名・位置座標・BMS生データを含めません。\n";
    }
    private ConnectionLog(){}
}
