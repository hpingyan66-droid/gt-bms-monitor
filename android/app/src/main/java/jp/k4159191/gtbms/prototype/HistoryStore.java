package jp.k4159191.gtbms.prototype;

import android.content.*;
import android.database.Cursor;
import android.database.sqlite.*;
import android.net.Uri;
import org.json.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.concurrent.*;
import jp.k4159191.gtbms.prototype.core.Sample;

final class HistoryStore extends SQLiteOpenHelper {
    private static HistoryStore instance;
    private final ThreadPoolExecutor writes=new ThreadPoolExecutor(1,1,0,TimeUnit.SECONDS,new ArrayBlockingQueue<>(128));
    static synchronized HistoryStore get(Context c){if(instance==null)instance=new HistoryStore(c.getApplicationContext());return instance;}
    private HistoryStore(Context c){super(c,"gtbms-android.db",null,1);setWriteAheadLoggingEnabled(true);}
    @Override public void onCreate(SQLiteDatabase db){
        db.execSQL("CREATE TABLE samples (id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT NOT NULL, source TEXT NOT NULL, session TEXT NOT NULL, device_key TEXT NOT NULL, payload TEXT NOT NULL)");
        db.execSQL("CREATE INDEX samples_session ON samples(session,id)");
    }
    @Override public void onUpgrade(SQLiteDatabase db,int old,int next){throw new IllegalStateException("未対応のDB更新");}
    void loadCount(){try{writes.execute(()->{try(Cursor c=getReadableDatabase().rawQuery("SELECT COUNT(*) FROM samples",null)){if(c.moveToFirst())AppState.recorded=c.getLong(0);}catch(Exception e){AppState.storageError="記録DBを開けません";}});}catch(RejectedExecutionException e){AppState.storageError="保存処理が混雑しています";}}
    void save(Sample s,String session,String deviceKey){
        if(!"ble".equals(s.source))return;
        try{writes.execute(()->{
            try {
                ContentValues v=new ContentValues();v.put("received_at",Instant.ofEpochMilli(s.receivedAt).toString());v.put("source",s.source);
                v.put("session",session);v.put("device_key",deviceKey);v.put("payload",json(s).toString());
                getWritableDatabase().insertOrThrow("samples",null,v);AppState.recorded++;AppState.storageError="";
            }catch(Exception e){AppState.storageLosses++;AppState.storageError="保存に失敗しました。空き容量を確認してください";}
        });}catch(RejectedExecutionException e){AppState.storageLosses++;AppState.storageError="記録待ちが上限に達しました。一部未保存です";}
    }
    private static JSONArray array(double[] a)throws JSONException{JSONArray j=new JSONArray();for(double x:a)j.put(x);return j;}
    static JSONObject json(Sample s)throws JSONException{
        JSONObject j=new JSONObject();j.put("voltage_v",s.voltage);j.put("raw_current_a",s.rawCurrent);j.put("current_direction",s.direction);
        j.put("discharge_current_a",s.dischargeCurrent()==null?JSONObject.NULL:s.dischargeCurrent());j.put("power_w",s.power()==null?JSONObject.NULL:s.power());j.put("raw_power_w",s.rawPower());
        j.put("soc_pct",s.soc);j.put("remaining_ah",s.remainingAh);j.put("configured_ah",s.configuredAh);j.put("cycles",s.cycles);
        j.put("cell_voltages_v",array(s.cells));j.put("temperatures_c",array(s.temperatures));j.put("charge_mos",s.chargeMos);
        j.put("discharge_mos",s.dischargeMos);j.put("protection_code",s.protection);j.put("balancing_mask",s.balancing);
        j.put("quality_flags",new JSONArray(s.flags));j.put("bms_version",s.version);j.put("received_elapsed_ms",s.receivedElapsed);return j;
    }
    String summary(){
        try(Cursor c=getReadableDatabase().rawQuery("SELECT COUNT(*), MIN(received_at), MAX(received_at), COUNT(DISTINCT session) FROM samples",null)){
            c.moveToFirst();return "実機の保存件数  "+c.getLong(0)+"\n接続セッション  "+c.getLong(3)+"\n\n最初の受信（UTC）\n"+(c.isNull(1)?"—":c.getString(1))+"\n\n最後の受信（UTC）\n"+(c.isNull(2)?"—":c.getString(2));
        }
    }
    long export(Context context,Uri uri)throws Exception {
        SQLiteDatabase db=getReadableDatabase();long end=0,after=0,count=0;
        try(Cursor c=db.rawQuery("SELECT COALESCE(MAX(id),0) FROM samples",null)){if(c.moveToFirst())end=c.getLong(0);}
        OutputStream stream=context.getContentResolver().openOutputStream(uri,"wt");if(stream==null)throw new IOException("出力先を開けません");
        String[] keys={"voltage_v","raw_current_a","current_direction","discharge_current_a","power_w","soc_pct","remaining_ah","configured_ah","cycles","charge_mos","discharge_mos","protection_code","balancing_mask","bms_version","cell_voltages_v","temperatures_c","quality_flags","received_elapsed_ms","raw_power_w"};
        try(Writer w=new BufferedWriter(new OutputStreamWriter(stream,StandardCharsets.UTF_8))){
            w.write('\ufeff');w.write("id,received_at_utc,source,session,device_key");for(String k:keys)w.write(","+k);w.write("\r\n");
            while(after<end){
                try(Cursor c=db.rawQuery("SELECT id,received_at,source,session,device_key,payload FROM samples WHERE id>? AND id<=? ORDER BY id LIMIT 500",new String[]{Long.toString(after),Long.toString(end)})){
                    if(!c.moveToFirst())break;
                    do{after=c.getLong(0);JSONObject j=new JSONObject(c.getString(5));w.write(Long.toString(after));
                        for(int i=1;i<5;i++)w.write(","+csv(c.getString(i)));
                        for(String k:keys)w.write(","+csv(j.isNull(k)?"":j.optString(k,"")));w.write("\r\n");count++;
                    }while(c.moveToNext());
                }
            }
        }return count;
    }
    static String csv(String s){return "\""+s.replace("\"","\"\"")+"\"";}
}
