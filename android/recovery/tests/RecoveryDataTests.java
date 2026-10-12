package jp.k4159191.gtbms.prototype;

import android.app.*;
import android.content.*;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.os.*;
import android.view.*;
import android.widget.*;
import org.json.*;

/** Synthetic emulator data only. No Bluetooth connection or real journeys. */
public class RecoveryDataTests extends Instrumentation {
    private Activity activity; private int checks;
    @Override public void onCreate(Bundle args){super.onCreate(args);start();}
    private void check(boolean value,String label){checks++;if(!value)throw new AssertionError(label);}
    private TextView find(View v,String text){if(v instanceof TextView && text.equals(((TextView)v).getText().toString()))return (TextView)v;if(v instanceof ViewGroup){ViewGroup g=(ViewGroup)v;for(int i=0;i<g.getChildCount();i++){TextView r=find(g.getChildAt(i),text);if(r!=null)return r;}}return null;}
    private void click(String text){runOnMainSync(()->{TextView v=find(activity.getWindow().getDecorView(),text);check(v!=null,"button "+text);v.performClick();});waitForIdleSync();}
    private long count(SQLiteDatabase db,String table){try(Cursor c=db.rawQuery("SELECT COUNT(*) FROM "+table,null)){c.moveToFirst();return c.getLong(0);}}
    @Override public void onStart(){Bundle result=new Bundle();try{
        Context c=getTargetContext();check(c.getPackageName().equals("jp.k4159191.gtbms.recovery"),"separate package");
        check(c.getSharedPreferences("settings",0).getString("recovery_marker","").equals("合成設定・保持確認"),"settings retained");
        TripProfile p=new TripProfile(c);check(p.revision==7,"profile revision");check(p.reserve==12.5,"reserve preference");check(p.mounted,"fixed mount ignores retired preference");check(p.signConfirmed,"protocol sign direction");
        try(SQLiteDatabase h=SQLiteDatabase.openDatabase(c.getDatabasePath("gtbms-android.db").toString(),null,SQLiteDatabase.OPEN_READONLY)){
            check(count(h,"samples")==3,"all samples retained");
            try(Cursor cur=h.rawQuery("SELECT payload FROM samples WHERE id=1",null)){cur.moveToFirst();JSONObject j=new JSONObject(cur.getString(0));check(j.getDouble("current")==-2.5,"raw negative current retained");check(j.getString("marker").equals("合成データ・復旧試験"),"unicode payload");}
        }
        try(SQLiteDatabase t=SQLiteDatabase.openDatabase(c.getDatabasePath("gtbms-trips.db").toString(),null,SQLiteDatabase.OPEN_READONLY)){
            check(count(t,"trips")==1,"trip retained");check(count(t,"points")==2,"points retained");check(count(t,"models")==1,"model retained");
            try(Cursor cur=t.rawQuery("SELECT payload FROM models WHERE key='synthetic-recovery-model'",null)){cur.moveToFirst();JSONArray bins=new JSONObject(cur.getString(0)).getJSONArray("bins");check(bins.getJSONObject(0).getDouble("meters")==1234.5,"learned distance retained");check(bins.getJSONObject(0).getInt("windows")==8,"learning windows retained");}
        }
        check(c.getFileStreamPath("synthetic-note.txt").isFile(),"files retained");
        activity=startActivitySync(new Intent(c,MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));waitForIdleSync();
        for(int i=0;i<50 && AppState.recorded!=3;i++)SystemClock.sleep(200);
        check(AppState.recorded==3,"history loaded into UI");click("記録");
        Activity trip=startActivitySync(new Intent(c,TripActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));waitForIdleSync();
        runOnMainSync(()->check(find(trip.getWindow().getDecorView(),"走行記録")!=null,"trip screen opens"));
        runOnMainSync(trip::finish);waitForIdleSync();
        click("モニター");click("デモを試す");SystemClock.sleep(1200);waitForIdleSync();
        runOnMainSync(()->check(find(activity.getWindow().getDecorView(),"82 %")!=null,"demo SOC"));
        check(AppState.recorded==3,"demo does not append real history");click("デモ終了");check(!AppState.active && !TripState.active,"no hardware services started");
        result.putString("stream","PASS: "+checks+" recovery runtime assertions. Synthetic data only.\n");finish(Activity.RESULT_OK,result);
    }catch(Throwable e){result.putString("stream","FAIL: "+e.toString());finish(Activity.RESULT_CANCELED,result);}}
}
