package jp.k4159191.gtbms.prototype;

import android.content.*;
import android.database.Cursor;
import android.database.sqlite.*;
import org.json.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.time.*;
import java.time.format.DateTimeFormatter;
import java.util.*;
import jp.k4159191.gtbms.prototype.core.*;

/** Separate schema preserves the original BMS history database without migration. */
final class TripStore extends SQLiteOpenHelper {
    private static TripStore instance;
    static synchronized TripStore get(Context c){if(instance==null)instance=new TripStore(c.getApplicationContext());return instance;}
    private TripStore(Context c){super(c,"gtbms-trips.db",null,1);setWriteAheadLoggingEnabled(true);}
    @Override public void onCreate(SQLiteDatabase db){
        db.execSQL("CREATE TABLE trips(id TEXT PRIMARY KEY,started INTEGER,ended INTEGER,state TEXT,profile TEXT,meters REAL DEFAULT 0,estimated REAL DEFAULT 0,wh REAL DEFAULT 0,regen REAL DEFAULT 0,covered REAL DEFAULT 0,uncovered REAL DEFAULT 0,summary TEXT)");
        db.execSQL("CREATE TABLE points(trip TEXT,seq INTEGER,wall INTEGER,motion TEXT,telemetry TEXT,PRIMARY KEY(trip,seq))");
        db.execSQL("CREATE TABLE models(key TEXT PRIMARY KEY,payload TEXT)");
    }
    @Override public void onUpgrade(SQLiteDatabase db,int a,int b){throw new IllegalStateException("Unsupported trip schema");}
    synchronized void recoverInterrupted(){ContentValues v=new ContentValues();v.put("state","interrupted");getWritableDatabase().update("trips",v,"state='recording'",null);}
    synchronized void begin(String id,long at,TripProfile profile)throws JSONException{
        ContentValues v=new ContentValues();v.put("id",id);v.put("started",at);v.put("state","recording");v.put("profile",profile.json().toString());getWritableDatabase().insertOrThrow("trips",null,v);
    }
    static void put(JSONObject j,String k,double x)throws JSONException{j.put(k,Double.isFinite(x)?x:JSONObject.NULL);}
    static JSONObject motion(TripEngine.Point p)throws JSONException{
        JSONObject j=new JSONObject();j.put("seq",p.seq).put("wall",p.wall).put("elapsed",p.elapsed).put("gps_elapsed",p.gpsElapsed).put("source",p.source).put("learnable",p.learnable).put("mock_gps",p.mock);
        put(j,"raw_lat",p.rawLat);put(j,"raw_lon",p.rawLon);put(j,"lat",p.lat);put(j,"lon",p.lon);put(j,"raw_altitude_m",p.rawAltitude);put(j,"altitude_m",p.altitude);
        put(j,"gps_lat",p.gpsLat);put(j,"gps_lon",p.gpsLon);put(j,"gps_accuracy_m",p.gpsAccuracy);
        put(j,"gps_altitude_m",p.gpsAltitude);put(j,"gps_vertical_accuracy_m",p.gpsVerticalAccuracy);
        put(j,"pressure_hpa",p.pressure);put(j,"speed_kmh",p.speed*3.6);put(j,"bearing_deg",Math.toDegrees(p.bearing));put(j,"position_error_envelope_m",p.error);
        put(j,"raw_segment_m",p.rawMeters);put(j,"segment_m",p.meters);put(j,"total_m",p.totalMeters);put(j,"grade_percent",p.grade);put(j,"seconds",p.seconds);return j;
    }
    static JSONObject telemetry(Sample s,boolean fresh,String key)throws JSONException{
        JSONObject j=new JSONObject();j.put("valid_bms",fresh).put("device_key",key);
        if(s!=null&&"ble".equals(s.source)){j.put("received_at",s.receivedAt).put("received_elapsed",s.receivedElapsed).put("direction",s.direction);put(j,"soc",s.soc);put(j,"voltage_v",s.voltage);put(j,"raw_current_a",s.rawCurrent);put(j,"raw_power_w",s.rawPower());if(s.power()!=null)put(j,"power_w",s.power());}
        return j;
    }
    synchronized void save(String id,TripEngine.Update update,JSONObject telemetry,TripEngine engine,double wh,double regen,double covered,double uncovered,String summary)throws JSONException{
        SQLiteDatabase db=getWritableDatabase();db.beginTransaction();try{
            for(TripEngine.Point p:update.revisions){ContentValues v=new ContentValues();v.put("motion",motion(p).toString());db.update("points",v,"trip=? AND seq=?",new String[]{id,Long.toString(p.seq)});}
            TripEngine.Point p=update.point;ContentValues v=new ContentValues();v.put("trip",id);v.put("seq",p.seq);v.put("wall",p.wall);v.put("motion",motion(p).toString());v.put("telemetry",telemetry.toString());db.insertOrThrow("points",null,v);
            ContentValues t=new ContentValues();t.put("meters",engine.totalMeters());t.put("estimated",engine.estimatedTotal());t.put("wh",wh);t.put("regen",regen);t.put("covered",covered);t.put("uncovered",uncovered);t.put("summary",summary);db.update("trips",t,"id=?",new String[]{id});db.setTransactionSuccessful();
        }finally{db.endTransaction();}
    }
    synchronized void end(String id,long at,String state){ContentValues v=new ContentValues();v.put("ended",at);v.put("state",state);getWritableDatabase().update("trips",v,"id=?",new String[]{id});}
    synchronized EnergyModel loadModel(String key)throws JSONException{
        EnergyModel m=new EnergyModel();try(Cursor c=getReadableDatabase().rawQuery("SELECT payload FROM models WHERE key=?",new String[]{key})){
            if(c.moveToFirst()){JSONArray a=new JSONObject(c.getString(0)).getJSONArray("bins");for(int i=0;i<Math.min(4,a.length());i++){JSONObject j=a.getJSONObject(i);EnergyModel.Bin b=m.bins[i];b.meters=j.getDouble("meters");b.wh=j.getDouble("wh");b.seconds=j.getDouble("seconds");b.mean=j.getDouble("mean");b.variance=j.getDouble("variance");b.windows=j.getInt("windows");}}}
        return m;
    }
    synchronized void saveModel(String key,EnergyModel m)throws JSONException{
        if(key.isEmpty())return;JSONArray a=new JSONArray();for(EnergyModel.Bin b:m.bins)a.put(new JSONObject().put("meters",b.meters).put("wh",b.wh).put("seconds",b.seconds).put("mean",b.mean).put("variance",b.variance).put("windows",b.windows));
        ContentValues v=new ContentValues();v.put("key",key);v.put("payload",new JSONObject().put("schema",1).put("bins",a).toString());getWritableDatabase().insertWithOnConflict("models",null,v,SQLiteDatabase.CONFLICT_REPLACE);
    }
    synchronized String summary(){
        SQLiteDatabase db=getReadableDatabase();StringBuilder s=new StringBuilder();
        try(Cursor c=db.rawQuery("SELECT COUNT(*),COALESCE(SUM(meters),0),COALESCE(SUM(estimated),0) FROM trips",null)){c.moveToFirst();s.append(String.format(Locale.JAPAN,"全%d走行 · 合計 %.2f km\nうち推定・補正推定 %.2f km\n",c.getLong(0),c.getDouble(1)/1000,c.getDouble(2)/1000));}
        try(Cursor c=db.rawQuery("SELECT started,state,meters,wh,summary FROM trips ORDER BY started DESC LIMIT 10",null)){
            while(c.moveToNext()){String state=c.getString(1);String label="recording".equals(state)?(TripState.active?"記録中":"中断・最終保存値"):"complete".equals(state)?"完了":"中断";
                s.append("\n").append(Instant.ofEpochMilli(c.getLong(0)).atZone(ZoneId.systemDefault()).format(DateTimeFormatter.ofPattern("MM/dd HH:mm"))).append(String.format(Locale.JAPAN," · %s\n%.2f km / 放電 %.1f Wh\n",label,c.getDouble(2)/1000,c.getDouble(3)));}}
        return s.toString();
    }
    synchronized String lastSummary(){try(Cursor c=getReadableDatabase().rawQuery("SELECT summary FROM trips ORDER BY started DESC LIMIT 1",null)){return c.moveToFirst()&&!c.isNull(0)?c.getString(0):"記録を開始すると距離・速度・センサー状態を表示します。";}}
    long export(OutputStream output)throws Exception{
        // Called only after recording stops: a stable, streaming export, including original estimates.
        String[] motion={"elapsed","gps_elapsed","source","learnable","mock_gps","raw_lat","raw_lon","lat","lon","gps_lat","gps_lon","gps_accuracy_m","gps_altitude_m","gps_vertical_accuracy_m","raw_altitude_m","altitude_m","pressure_hpa","speed_kmh","bearing_deg","position_error_envelope_m","raw_segment_m","segment_m","total_m","grade_percent","seconds"};
        String[] telemetry={"valid_bms","device_key","received_at","received_elapsed","direction","soc","voltage_v","raw_current_a","power_w","raw_power_w"};
        long n=0;try(Writer w=new BufferedWriter(new OutputStreamWriter(output,StandardCharsets.UTF_8));Cursor c=getReadableDatabase().rawQuery("SELECT p.trip,p.seq,p.wall,p.motion,p.telemetry,t.state,t.profile FROM points p JOIN trips t ON t.id=p.trip ORDER BY t.started,p.seq",null)){
            w.write("trip_id,seq,utc_time,trip_state,profile_json");for(String k:motion)w.write(","+k);for(String k:telemetry)w.write(","+k);w.write("\r\n");
            while(c.moveToNext()){w.write(csv(c.getString(0))+","+c.getLong(1)+","+csv(Instant.ofEpochMilli(c.getLong(2)).toString())+","+csv(c.getString(5))+","+csv(c.getString(6)));JSONObject m=new JSONObject(c.getString(3)),t=new JSONObject(c.getString(4));for(String k:motion)w.write(","+csv(m.isNull(k)?"":m.optString(k,"")));for(String k:telemetry)w.write(","+csv(t.isNull(k)?"":t.optString(k,"")));w.write("\r\n");n++;}
        }return n;
    }
    private static String csv(String s){return "\""+s.replace("\"","\"\"")+"\"";}
}
