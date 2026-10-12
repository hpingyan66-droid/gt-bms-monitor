package jp.k4159191.gtbms.prototype;

import android.Manifest;
import android.app.*;
import android.content.*;
import android.os.*;
import android.view.*;
import android.view.accessibility.AccessibilityNodeInfo;
import android.widget.*;
import java.util.*;

/** Run against emulator permission states configured by the QA driver. No real device. */
public class PermissionTests extends Instrumentation {
    private Activity activity;private String scenario;private int checks;
    @Override public void onCreate(Bundle args){super.onCreate(args);scenario=args.getString("scenario","missing");start();}
    private void check(boolean value,String what){checks++;if(!value)throw new AssertionError(what);}
    private TextView find(View v,String s){if(v instanceof TextView&&s.equals(((TextView)v).getText().toString()))return (TextView)v;if(v instanceof ViewGroup){ViewGroup g=(ViewGroup)v;for(int i=0;i<g.getChildCount();i++){TextView r=find(g.getChildAt(i),s);if(r!=null)return r;}}return null;}
    private void click(String label){runOnMainSync(()->{TextView v=find(activity.getWindow().getDecorView(),label);check(v!=null,"button "+label);v.performClick();});waitForIdleSync();}
    private AccessibilityNodeInfo node(String text){long end=SystemClock.uptimeMillis()+5000;while(SystemClock.uptimeMillis()<end){AccessibilityNodeInfo root=getUiAutomation().getRootInActiveWindow();if(root!=null)for(AccessibilityNodeInfo n:root.findAccessibilityNodeInfosByText(text))if(n.getText()!=null&&text.equals(n.getText().toString()))return n;SystemClock.sleep(150);}return null;}
    private void visible(String text){check(node(text)!=null,"dialog text "+text);}
    @Override public void onStart(){Bundle result=new Bundle();try{
        Context c=getTargetContext();c.getSharedPreferences("settings",0).edit().putBoolean("notification_prompted",true).apply();
        if("missing".equals(scenario)||"approximate".equals(scenario)){
            android.content.SharedPreferences p=c.getSharedPreferences("settings",0);android.content.SharedPreferences.Editor e=p.edit();
            for(String key:p.getAll().keySet())if(key.startsWith("asked."))e.remove(key);e.commit();
        }
        activity=startActivitySync(new Intent(c,MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));waitForIdleSync();
        if("missing".equals(scenario)||"approximate".equals(scenario)){
            Set<String> missing=new HashSet<>(Arrays.asList(ConnectionAccess.missing(c)));
            check(missing.contains(Manifest.permission.ACCESS_FINE_LOCATION),"fine location requested");
            check(missing.contains(Manifest.permission.ACCESS_COARSE_LOCATION),"coarse and fine requested together");
            check(!ConnectionAccess.scanReady(c),"scan prohibited before grants");
            if("approximate".equals(scenario))check(ConnectionAccess.allowed(c,Manifest.permission.ACCESS_COARSE_LOCATION),"approximate actually granted by OS");
            click("Bluetooth接続");visible("Bluetooth接続の準備");check(!AppState.active,"missing permission cannot start monitor");
        }else if("blocked".equals(scenario)){
            for(String p:ConnectionAccess.missing(c))c.getSharedPreferences("settings",0).edit().putBoolean("asked."+p,true).commit();
            click("Bluetooth接続");visible("接続に使う権限");visible("アプリ設定");check(!AppState.active,"blocked permission cannot connect");
        }else if("location_off".equals(scenario)){
            check(ConnectionAccess.missing(c).length==0,"runtime permissions granted");check(!ConnectionAccess.locationOn(c),"location switch actually off");
            click("Bluetooth接続");visible("端末の位置情報");check(!AppState.active,"location gate before scan");
        }else if("ready_notification_denied".equals(scenario)){
            check(ConnectionAccess.scanReady(c),"all scan prerequisites ready");check(!ConnectionAccess.allowed(c,Manifest.permission.POST_NOTIFICATIONS),"notification actually denied");
            click("Bluetooth接続");visible("Bluetooth機器");check(!AppState.active,"selection still required");
            check(ConnectionLog.report(c).contains("[探索中]"),"notification denial does not block scan");
        }else throw new AssertionError("unknown scenario");
        result.putString("stream","PASS: "+scenario+" / "+checks+" assertions; Android permission state and native UI, no real BMS.\n");finish(Activity.RESULT_OK,result);
    }catch(Throwable e){result.putString("stream","FAIL: "+scenario+" / "+e);finish(Activity.RESULT_CANCELED,result);}}
}
