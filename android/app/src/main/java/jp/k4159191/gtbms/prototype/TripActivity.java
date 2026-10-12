package jp.k4159191.gtbms.prototype;

import android.Manifest;
import android.app.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.*;
import android.provider.Settings;
import android.text.InputType;
import android.view.*;
import android.widget.*;
import java.io.*;
import java.time.LocalDate;
import java.util.*;
import java.util.concurrent.*;
import jp.k4159191.gtbms.prototype.core.*;

public final class TripActivity extends Activity {
    private static final int BG=0xff101820,CARD=0xff1a2631,TEXT=0xffe8f0f5,MUTED=0xffa5b6c3,ACCENT=0xff78dac1;
    private final Handler h=new Handler(Looper.getMainLooper());private final ExecutorService io=Executors.newSingleThreadExecutor();
    private LinearLayout body;private TextView state,details,prediction,learning,sensors,climb,saved,start;
    private boolean resumed,lastActive;private String savedText="読み込み中…",lastSummary="";
    private final Runnable tick=new Runnable(){public void run(){if(!resumed)return;refresh();h.postDelayed(this,1000);}};
    @Override public void onCreate(Bundle b){super.onCreate(b);build();loadSaved();}
    @Override protected void onResume(){super.onResume();resumed=true;h.post(tick);}
    @Override protected void onPause(){resumed=false;h.removeCallbacks(tick);super.onPause();}
    @Override protected void onDestroy(){io.shutdown();super.onDestroy();}
    private int dp(int n){return Math.round(n*getResources().getDisplayMetrics().density);}
    private TextView text(String value,int size,int color){TextView t=new TextView(this);t.setText(value);t.setTextSize(size);t.setTextColor(color);t.setLineSpacing(dp(4),1);return t;}
    private LinearLayout vertical(){LinearLayout l=new LinearLayout(this);l.setOrientation(LinearLayout.VERTICAL);return l;}
    private GradientDrawable shape(int color){GradientDrawable g=new GradientDrawable();g.setColor(color);g.setCornerRadius(dp(14));return g;}
    private TextView button(LinearLayout parent,String label,Runnable action){TextView t=text(label,16,TEXT);t.setGravity(Gravity.CENTER);t.setPadding(dp(12),dp(14),dp(12),dp(14));t.setMinHeight(dp(50));t.setBackground(shape(0xff2a3e4a));t.setFocusable(true);t.setOnClickListener(v->action.run());LinearLayout.LayoutParams lp=new LinearLayout.LayoutParams(-1,-2);lp.setMargins(0,dp(8),0,dp(10));parent.addView(t,lp);return t;}
    private LinearLayout card(String title){LinearLayout c=vertical();c.setPadding(dp(16),dp(16),dp(16),dp(16));c.setBackground(shape(CARD));LinearLayout.LayoutParams lp=new LinearLayout.LayoutParams(-1,-2);lp.bottomMargin=dp(14);body.addView(c,lp);TextView t=text(title,20,TEXT);t.setTypeface(null,Typeface.BOLD);c.addView(t);return c;}
    private void build(){
        LinearLayout root=vertical();root.setBackgroundColor(BG);setContentView(root);root.setOnApplyWindowInsetsListener((v,i)->{v.setPadding(i.getSystemWindowInsetLeft(),i.getSystemWindowInsetTop(),i.getSystemWindowInsetRight(),i.getSystemWindowInsetBottom());return i;});root.requestApplyInsets();
        LinearLayout header=vertical();header.setPadding(dp(18),dp(10),dp(18),0);root.addView(header);button(header,"‹ BMSモニターへ",this::finish);
        ScrollView scroll=new ScrollView(this);root.addView(scroll,new LinearLayout.LayoutParams(-1,0,1));body=vertical();body.setPadding(dp(16),dp(6),dp(16),dp(24));scroll.addView(body);
        LinearLayout control=card("走行記録");state=text("",16,ACCENT);control.addView(state);start=button(control,"走行記録を開始",this::startOrStop);
        control.addView(text("記録中は画面を閉じても位置情報を使います。BMS接続はモニター画面で開始してください。",13,MUTED));
        button(control,"走行・車両の設定",this::settings);
        LinearLayout run=card("今回の走行");details=text("",16,TEXT);run.addView(details);
        LinearLayout forecast=card("航続距離・残り時間");prediction=text("",18,TEXT);forecast.addView(prediction);learning=text("",13,ACCENT);forecast.addView(learning);
        forecast.addView(text("今の速度帯・電費が続くと仮定した予測です。参考幅は実測精度の保証ではありません。残容量×現在電圧の近似で、負荷・温度・劣化・BMS遮断によって早く停止する場合があります。",12,MUTED));
        LinearLayout slope=card("登坂の参考計算");climb=text("",17,TEXT);slope.addView(climb);slope.addView(text("連続出力から計算した定速走行の上限です。発進トルク・路面の滑り・熱・制動・転倒限界は含みません。車両の登坂保証値ではありません。",12,MUTED));
        LinearLayout sensor=card("使っているセンサー");sensors=text("走行開始時に確認します",14,MUTED);sensor.addView(sensors);
        sensor.addView(text("端末を車体に固定して使う前提で、加速度・ジャイロをGPS欠測中の推定に使います。センサーが使えない場合や60秒を超える欠測では速度保持へ切り替えます。GPS復旧時は両端を合わせて距離を再計算し、元の推定も残します。2時間超の欠測や処理中断では区間を未確定のまま残します。",12,MUTED));
        LinearLayout records=card("保存した走行");saved=text("",14,MUTED);records.addView(saved);button(records,"走行CSVを保存",this::export);button(records,"保存記録を更新",this::loadSaved);
        records.addView(text("CSVには走行位置を含みます。記録・学習は端末内に保存されます。アプリを削除する前に書き出してください。",12,MUTED));refresh();
    }
    private void refresh(){
        boolean active=TripState.active;state.setText(TripState.status+(TripState.error.isEmpty()?"":"\n"+TripState.error));start.setText(active?"走行記録を終了":"走行記録を開始");start.setEnabled(!TripState.exporting);
        details.setText(active||!TripState.details.isEmpty()?TripState.details:lastSummary);
        prediction.setText(active?TripState.prediction:"記録を開始すると、現在のBMS値から予測します。");learning.setText(active?TripState.learning:"学習結果は次回の走行にも引き継ぎます。");
        sensors.setText(TripState.sensors.isEmpty()?"走行開始時に確認します":TripState.sensors);saved.setText(savedText);
        TripProfile p=new TripProfile(this);double grade=EnergyModel.climbGrade(p.mass,p.watts,p.efficiency,p.climbKmh,p.crr,p.cda);
        climb.setText(Double.isFinite(grade)?TripService.fmt("出力からの参考上限  %s%%（約 %.1f°）\n%.1f km/h · 総重量 %.0f kg\n連続電気入力 %.0f W / 効率 %.0f%%",grade>=59.99?"60以上":TripService.fmt("%.1f",grade),Math.toDegrees(Math.atan(grade/100)),p.climbKmh,p.mass,p.watts,p.efficiency*100):"総重量と連続電気入力を設定すると計算します。");
        if(lastActive&&!active)loadSaved();lastActive=active;
    }
    private void startOrStop(){
        if(TripState.active){stopService(new Intent(this,TripService.class));return;}
        if(TripState.exporting){toast("CSV保存が終わってから開始してください");return;}
        if(checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)!=PackageManager.PERMISSION_GRANTED){
            requestPermissions(new String[]{Manifest.permission.ACCESS_COARSE_LOCATION,Manifest.permission.ACCESS_FINE_LOCATION},71);return;}
        if(!ConnectionAccess.locationOn(this)){new AlertDialog.Builder(this).setTitle("位置情報をONにしてください").setMessage("走行記録にはGPSを使います。設定後、開始ボタンを押してください。").setPositiveButton("位置情報設定",(d,w)->startActivity(new Intent(Settings.ACTION_LOCATION_SOURCE_SETTINGS))).setNegativeButton("閉じる",null).show();return;}
        if(Build.VERSION.SDK_INT>=33&&checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)!=PackageManager.PERMISSION_GRANTED&&!getPreferences(0).getBoolean("notification_asked",false)){
            getPreferences(0).edit().putBoolean("notification_asked",true).apply();requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS},72);return;}
        try{startForegroundService(new Intent(this,TripService.class).setAction(TripService.START));}catch(RuntimeException e){toast("開始できません: "+e.getClass().getSimpleName());}
    }
    @Override public void onRequestPermissionsResult(int request,String[] names,int[] result){super.onRequestPermissionsResult(request,names,result);
        if(request==71){if(checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)==PackageManager.PERMISSION_GRANTED)h.post(this::startOrStop);else new AlertDialog.Builder(this).setTitle("正確な位置情報が必要です").setMessage("アプリの権限で「位置情報 → アプリの使用中のみ・正確な位置情報」を許可してください。").setPositiveButton("アプリ設定",(d,w)->startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,android.net.Uri.parse("package:"+getPackageName())))).setNegativeButton("閉じる",null).show();}
        if(request==72)h.post(this::startOrStop);
    }
    private EditText field(LinearLayout box,String label,String key,double fallback){box.addView(text(label,14,TEXT));EditText e=new EditText(this);e.setTextColor(TEXT);e.setSingleLine(true);e.setInputType(InputType.TYPE_CLASS_NUMBER|InputType.TYPE_NUMBER_FLAG_DECIMAL);String value=getSharedPreferences("trip_profile",0).getString(key,Double.isFinite(fallback)?Double.toString(fallback):"");e.setText(value);box.addView(e);return e;}
    private double parse(EditText e,double lo,double hi,boolean optional){String s=e.getText().toString().trim();if(s.isEmpty()&&optional)return Double.NaN;double v=Double.parseDouble(s);if(!Double.isFinite(v)||v<lo||v>hi)throw new IllegalArgumentException();return v;}
    private void settings(){
        if(TripState.active){toast("走行記録を終了してから変更してください");return;}
        TripProfile p=new TripProfile(this);LinearLayout box=vertical();box.setPadding(dp(20),dp(10),dp(20),dp(10));ScrollView scroll=new ScrollView(this);scroll.addView(box);
        box.addView(text("空欄の項目は未設定として扱います。車両・荷重を変更した際は学習を新しくします。BMS本体には書き込みません。",13,MUTED));
        box.addView(text("端末は車体に固定して使う前提です。固定の切り替え操作は不要です。",13,MUTED));
        box.addView(text("JBD仕様の符号で充放電を計算します（＋充電・−放電）。良好なGPSとBMS受信が揃うと電費を学習します。",12,MUTED));
        String[] keys={"max_kmh","reserve","manual_rate","mass","watts","efficiency","climb_kmh","crr","cda"};
        EditText[] fields={field(box,"GPS外れ値判定の速度上限 km/h（5〜180）",keys[0],40),field(box,"残しておく予備残量 %（0〜40）",keys[1],10),field(box,"未学習時の電費 Wh/km（任意、1〜1000）",keys[2],Double.NaN),field(box,"車両＋乗員＋荷物の総重量 kg（30〜1000）",keys[3],Double.NaN),field(box,"登坂時の連続電気入力 W（50〜20000）",keys[4],Double.NaN),field(box,"駆動効率（仮定 0.75、0.2〜1）",keys[5],.75),field(box,"登坂計算の速度 km/h（2〜80）",keys[6],5),field(box,"転がり抵抗係数（仮定 0.015、0〜0.1）",keys[7],.015),field(box,"空気抵抗 CdA m²（仮定 0.6、0〜3）",keys[8],.6)};
        box.addView(text("機械出力の銘板値を電気入力Wとして使わないでください。未確認の値は空欄にできます。登坂計算の効率・抵抗・無風条件は仮定です。",12,MUTED));
        AlertDialog dialog=new AlertDialog.Builder(this).setTitle("走行・車両の設定").setView(scroll).setPositiveButton("保存",null).setNegativeButton("閉じる",null).setNeutralButton("学習を新しくする",(d,w)->new AlertDialog.Builder(this).setTitle("次の走行から新しく学習").setMessage("これまでの走行記録は残ります。").setPositiveButton("新しく学習",(dd,ww)->{getSharedPreferences("trip_profile",0).edit().putInt("revision",p.revision+1).apply();toast("次回から新しく学習します");}).setNegativeButton("戻る",null).show()).create();
        dialog.setOnShowListener(d->dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v->{
            try{if(TripState.active){toast("走行記録を終了してください");return;}
                double[] lo={5,0,1,30,50,.2,2,0,0},hi={180,40,1000,1000,20000,1,80,.1,3},values=new double[9];
                for(int i=0;i<9;i++){try{values[i]=parse(fields[i],lo[i],hi[i],i==2||i==3||i==4);}catch(Exception e){fields[i].setError("表示した範囲の数値を入力してください");fields[i].requestFocus();return;}}
                SharedPreferences prefs=getSharedPreferences("trip_profile",0);SharedPreferences.Editor edit=prefs.edit();boolean changed=Double.compare(p.mass,values[3])!=0||Double.compare(p.watts,values[4])!=0;
                for(int i=0;i<9;i++)edit.putString(keys[i],Double.isFinite(values[i])?Double.toString(values[i]):"");
                edit.remove("mounted").remove("sign_confirmed").putInt("revision",p.revision+(changed?1:0)).apply();dialog.dismiss();refresh();toast(changed?"保存しました。車両条件の変更により次回から新しく学習します":"設定を保存しました");
            }catch(Exception e){toast("設定を保存できませんでした");}
        }));dialog.show();
    }
    private void loadSaved(){io.execute(()->{try{String s=TripStore.get(this).summary(),last=TripStore.get(this).lastSummary();h.post(()->{savedText=s;lastSummary=last;if(!isDestroyed())refresh();});}catch(Exception e){h.post(()->toast("走行記録を読めませんでした"));}});}
    private void export(){if(TripState.active){toast("走行記録を終了してからCSVを保存してください");return;}
        Intent i=new Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("text/csv").putExtra(Intent.EXTRA_TITLE,"gtbms-trips-"+LocalDate.now()+".csv");startActivityForResult(i,73);}
    @Override protected void onActivityResult(int request,int result,Intent data){super.onActivityResult(request,result,data);if(request!=73||result!=RESULT_OK||data==null||data.getData()==null)return;
        if(TripState.active){toast("走行を終了してから書き出してください");return;}TripState.exporting=true;refresh();io.execute(()->{try(OutputStream out=getContentResolver().openOutputStream(data.getData())){if(out==null)throw new IOException();long count=TripStore.get(this).export(out);h.post(()->toast(count+"点を書き出しました"));}catch(Exception e){h.post(()->toast("出力に失敗しました。作成途中のファイルが残る場合があります"));}finally{h.post(()->{TripState.exporting=false;if(!isDestroyed())refresh();});}});
    }
    private void toast(String s){if(!isDestroyed())Toast.makeText(this,s,Toast.LENGTH_LONG).show();}
}
