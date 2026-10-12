package jp.k4159191.gtbms.prototype;

import android.Manifest;
import android.app.*;
import android.content.*;
import android.database.Cursor;
import android.location.*;
import android.os.*;
import android.view.*;
import android.widget.*;
import org.json.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import jp.k4159191.gtbms.prototype.core.*;

/** Android lifecycle, mock location callback, SQLite corrections and CSV tests. No physical ride. */
public final class TripTests extends Instrumentation {
    private Activity activity;private int checks;private String scenario;
    @Override public void onCreate(Bundle b){super.onCreate(b);scenario=b.getString("scenario","full");start();}
    private void check(boolean b,String m){checks++;if(!b)throw new AssertionError(m);}
    private TextView find(View v,String s){if(v instanceof TextView&&s.equals(((TextView)v).getText().toString()))return (TextView)v;if(v instanceof ViewGroup)for(int i=0;i<((ViewGroup)v).getChildCount();i++){TextView r=find(((ViewGroup)v).getChildAt(i),s);if(r!=null)return r;}return null;}
    private void click(String s){runOnMainSync(()->{TextView t=find(activity.getWindow().getDecorView(),s);check(t!=null,"UI "+s);t.performClick();});waitForIdleSync();}
    private void waitActive(boolean want)throws Exception{long end=SystemClock.elapsedRealtime()+8000;while(SystemClock.elapsedRealtime()<end){if(TripState.active==want)return;Thread.sleep(100);}check(false,"active="+want+" / "+TripState.error);}
    private long count(){try(Cursor c=HistoryStore.get(getTargetContext()).getReadableDatabase().rawQuery("SELECT COUNT(*) FROM samples",null)){c.moveToFirst();return c.getLong(0);}}
    private void inject(LocationManager lm,double east)throws Exception{
        double[] ll=TripEngine.offset(35,139,east,0);Location l=new Location("gps");l.setLatitude(ll[0]);l.setLongitude(ll[1]);l.setAltitude(20);l.setAccuracy(3);l.setVerticalAccuracyMeters(2);l.setSpeed(5);l.setSpeedAccuracyMetersPerSecond(.1f);l.setBearing(90);l.setBearingAccuracyDegrees(2);l.setTime(System.currentTimeMillis());l.setElapsedRealtimeNanos(SystemClock.elapsedRealtimeNanos());lm.setTestProviderLocation("gps",l);Thread.sleep(1150);
    }
    @Override public void onStart(){Bundle result=new Bundle();LocationManager lm=null;try{
        Context c=getTargetContext();c.getSharedPreferences("TripActivity",0).edit().putBoolean("notification_asked",true).commit();
        activity=startActivitySync(new Intent(c,TripActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));waitForIdleSync();check(activity!=null,"trip launch");
        if(!"full".equals(scenario)){
            if("coarse".equals(scenario)){check(!ConnectionAccess.location(c),"fine missing");check(ConnectionAccess.allowed(c,Manifest.permission.ACCESS_COARSE_LOCATION),"coarse granted");}
            if("location_off".equals(scenario))check(!ConnectionAccess.locationOn(c),"system location off");
            runOnMainSync(()->c.startService(new Intent(c,TripService.class).setAction(TripService.START)));Thread.sleep(800);
            check(!TripState.active,"location gate prevents recording");check(TripState.error.contains("位置情報"),"explanatory error");
        }else{
            check(ConnectionAccess.location(c)&&ConnectionAccess.locationOn(c),"location prerequisites");
            check(!ConnectionAccess.allowed(c,Manifest.permission.POST_NOTIFICATIONS),"test notification denial");
            long oldCount=count();TripStore db=TripStore.get(c);
            c.getSharedPreferences("trip_profile",0).edit().putBoolean("mounted",false).putBoolean("sign_confirmed",false).commit();
            TripProfile upgraded=new TripProfile(c);
            check(upgraded.mounted&&upgraded.json().getBoolean("mounted"),"legacy mounted=false uses fixed mount for new trips");
            check(upgraded.signConfirmed&&upgraded.json().getString("sign_basis").equals("jbd_protocol"),"legacy confirmation no longer blocks JBD protocol interpretation");
            c.getSharedPreferences("settings",0).edit().putString("direction","discharge_positive").commit();
            lm=c.getSystemService(LocationManager.class);lm.addTestProvider("gps",false,true,false,false,true,true,true,Criteria.POWER_LOW,Criteria.ACCURACY_FINE);lm.setTestProviderEnabled("gps",true);
            click("走行記録を開始");waitActive(true);Thread.sleep(1300);check(TripState.error.isEmpty(),"service initialized: "+TripState.error);
            check(TripState.sensors.contains("端末固定を前提に測定")&&!TripState.sensors.contains("固定: OFF"),"service reports fixed-mount operation");
            for(int i=0;i<7;i++)inject(lm,i*5);
            check(TripState.meters>10&&TripState.meters<80,"mock GPS callback drives distance");check(TripState.details.contains("模擬GPS"),"mock locations labelled");
            check(!AppState.active,"trip did not start BLE or demo");check(count()==oldCount,"old BMS database unchanged");
            runOnMainSync(()->activity.moveTaskToBack(true));Thread.sleep(4100);check(TripState.active,"location foreground service continues with screen UI hidden");
            inject(lm,60);inject(lm,65);check(TripState.details.contains("GPS復旧補正 1回"),"GPS return corrected gap");
            runOnMainSync(()->c.stopService(new Intent(c,TripService.class)));waitActive(false);
            check(TripState.error.isEmpty(),"clean stop");check(db.summary().contains("完了"),"completed trip persisted");
            long rows;String trip;try(Cursor q=db.getReadableDatabase().rawQuery("SELECT trip,COUNT(*) FROM points GROUP BY trip ORDER BY MAX(wall) DESC LIMIT 1",null)){check(q.moveToFirst(),"stored points");trip=q.getString(0);rows=q.getLong(1);check(rows>=8,"one-second persistence");}
            boolean correction=false,original=false,telemetry=false;
            try(Cursor q=db.getReadableDatabase().rawQuery("SELECT motion,telemetry FROM points WHERE trip=?",new String[]{trip})){while(q.moveToNext()){JSONObject j=new JSONObject(q.getString(0));if("corrected_estimate".equals(j.getString("source"))){correction=true;original=j.has("raw_segment_m")&&j.has("raw_lat");telemetry=new JSONObject(q.getString(1)).has("valid_bms");check(!j.getBoolean("learnable"),"correction excluded from training");}}}
            check(correction&&original&&telemetry,"correction preserves originals and telemetry");
            ByteArrayOutputStream out=new ByteArrayOutputStream();long exported=db.export(out);String csv=new String(out.toByteArray(),StandardCharsets.UTF_8);
            check(exported>=rows&&csv.contains("raw_lat")&&csv.contains("corrected_estimate"),"CSV includes raw and corrected route");check(csv.contains("mock_gps")&&csv.contains("profile_json")&&csv.contains("raw_power_w"),"CSV provenance and native signed power");
            EnergyModel model=new EnergyModel();for(int i=1;i<=81;i++){TripEngine.Point p=new TripEngine.Point();p.seconds=1;p.speed=5;p.meters=5;p.learnable=true;Sample s=new Sample(i*1000,i*1000,"ble","discharge_positive",60,10,80,40,50,0,new double[]{3.9},new double[0],true,true,0,0,1,Collections.emptyList());model.update(p,s,true,"fixture",i*1000);}
            db.saveModel("test-persistence-only",model);EnergyModel restored=db.loadModel("test-persistence-only");check(restored.bins[0].ready()&&Math.abs(restored.bins[0].mean-600/18.0)<.01,"learning survives database reload");
            check(db.loadModel("different-battery").bins[0].windows==0,"battery histories isolated");
            // Remove test-only learning keys; these never touch a production device.
            db.getWritableDatabase().delete("models","key=?",new String[]{"test-persistence-only"});
            db.begin("interruption-fixture",System.currentTimeMillis(),new TripProfile(c));db.recoverInterrupted();try(Cursor q=db.getReadableDatabase().rawQuery("SELECT state FROM trips WHERE id='interruption-fixture'",null)){q.moveToFirst();check("interrupted".equals(q.getString(0)),"abandoned session recovered as interrupted");}
            db.getWritableDatabase().delete("trips","id=?",new String[]{"interruption-fixture"});
        }
        result.putString("stream","PASS: trip "+scenario+" / "+checks+" assertions; Android emulator and mock GPS, no physical ride.\n");finish(Activity.RESULT_OK,result);
    }catch(Throwable e){result.putString("stream","FAIL: trip "+scenario+" / "+e+" / "+TripState.error+" / "+TripState.details+" / "+TripState.sensors);finish(Activity.RESULT_CANCELED,result);}
    finally{try{getTargetContext().stopService(new Intent(getTargetContext(),TripService.class));if(lm!=null)lm.removeTestProvider("gps");}catch(Exception ignored){}}}
}
