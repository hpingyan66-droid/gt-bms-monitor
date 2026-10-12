package jp.k4159191.gtbms.prototype;

import android.app.*;
import android.content.*;
import android.os.*;
import android.view.*;
import android.widget.*;
import java.util.concurrent.atomic.AtomicReference;

/** Isolated emulator UI smoke tests. Does not connect to any BLE device. */
public class SmokeTests extends Instrumentation {
    private Activity activity;private int checks=0;
    @Override public void onCreate(Bundle args){super.onCreate(args);start();}
    private void check(boolean b,String s){checks++;if(!b)throw new AssertionError(s);}
    private TextView find(View v,String s){if(v instanceof TextView && s.equals(((TextView)v).getText().toString()))return (TextView)v;if(v instanceof ViewGroup){ViewGroup g=(ViewGroup)v;for(int i=0;i<g.getChildCount();i++){TextView r=find(g.getChildAt(i),s);if(r!=null)return r;}}return null;}
    private void click(String s){runOnMainSync(()->{TextView v=find(activity.getWindow().getDecorView(),s);check(v!=null,"missing "+s);v.performClick();});waitForIdleSync();}
    @Override public void onStart(){Bundle result=new Bundle();try{
        Intent launch=new Intent(getTargetContext(),MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);activity=startActivitySync(launch);waitForIdleSync();
        check(activity!=null,"launch");click("デモを試す");SystemClock.sleep(1200);waitForIdleSync();
        runOnMainSync(()->check(find(activity.getWindow().getDecorView(),"82 %")!=null,"demo SOC"));
        click("セル");runOnMainSync(()->check(find(activity.getWindow().getDecorView(),"セル電圧")!=null,"cell page"));
        click("記録");SystemClock.sleep(300);check(AppState.recorded==0,"demo must not create real history");
        click("設定");click("設定を保存");check(getTargetContext().getSharedPreferences("settings",0).getInt("cells",0)==16,"persist settings");
        click("モニター");click("デモ終了");check(!AppState.active,"no BLE service started for demo");
        ConnectionLog.add("検証","synthetic AA:BB:CC:DD:EE:FF");String report=ConnectionLog.report(getTargetContext());
        check(!report.contains("AA:BB:CC:DD:EE:FF"),"diagnostics redact MAC");check(report.contains("API "+Build.VERSION.SDK_INT),"diagnostics include runtime API");
        click("接続診断");check(!AppState.active,"diagnostics must not connect");
        result.putString("stream","PASS: "+checks+" UI assertions. Android emulator; no BLE hardware.\n");finish(Activity.RESULT_OK,result);
    }catch(Throwable e){result.putString("stream","FAIL: "+e.toString());finish(Activity.RESULT_CANCELED,result);}}
}
