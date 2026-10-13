package jp.k4159191.gtbms.prototype;

import android.Manifest;
import android.app.*;
import android.bluetooth.*;
import android.bluetooth.le.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.location.LocationManager;
import android.os.*;
import android.provider.Settings;
import android.text.InputType;
import android.view.*;
import android.widget.*;
import org.json.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.time.*;
import java.time.format.DateTimeFormatter;
import java.util.*;
import java.util.concurrent.*;
import jp.k4159191.gtbms.prototype.core.*;

public final class MainActivity extends Activity {
    private static final int BG=0xff101820,CARD=0xff1a2631,TEXT=0xffe8f0f5,MUTED=0xffa5b6c3,ACCENT=0xff78dac1,WARN=0xffffca83;
    private final Handler h=new Handler(Looper.getMainLooper());
    private final ExecutorService io=Executors.newSingleThreadExecutor();
    private LinearLayout root,body,cellsBox;private TextView status,substatus,navNote,accessStatus;
    private TextView soc,volts,amps,power,temps,delta,extra,quality,history;
    private TrendView trend;private ProgressBar chargeBar;private int tab=0,demoTick=0;
    private boolean demo=false,resumed=false;private Sample demoSample;private long lastDemo=0;
    private SharedPreferences prefs;
    private BluetoothLeScanner scanner;private ScanCallback scanCallback;private AlertDialog scanDialog;
    private final LinkedHashMap<String,String> found=new LinkedHashMap<>();private ArrayAdapter<String> scanAdapter;
    private final ArrayList<String> addresses=new ArrayList<>();private int scanGeneration=0;
    private boolean pendingScan=false,scanAfterNotifications=false;private String diagnosticExport;
    private long lastScanStart=0;private TextView scanHint;
    private final Runnable tick=new Runnable(){public void run(){if(!resumed)return;refresh();h.postDelayed(this,500);}};
    @Override public void onCreate(Bundle state){super.onCreate(state);prefs=getSharedPreferences("settings",MODE_PRIVATE);
        ConnectionLog.init(this);
        if(state!=null){tab=state.getInt("tab",0);demo=state.getBoolean("demo",false);}
        HistoryStore.get(this).loadCount();build();
    }
    @Override protected void onSaveInstanceState(Bundle b){super.onSaveInstanceState(b);b.putInt("tab",tab);b.putBoolean("demo",demo);}
    @Override protected void onResume(){super.onResume();resumed=true;if(AppState.active)demo=false;applyScreen();h.removeCallbacks(tick);h.post(tick);}
    @Override protected void onPostResume(){super.onPostResume();resumePendingScan();}
    private void resumePendingScan(){if(pendingScan&&resumed){pendingScan=false;h.post(this::requestScan);}}
    private void deferScan(){pendingScan=true;h.post(this::resumePendingScan);}
    @Override protected void onPause(){resumed=false;h.removeCallbacks(tick);stopScan();if(scanDialog!=null)scanDialog.dismiss();super.onPause();}
    @Override protected void onDestroy(){stopScan();io.shutdown();super.onDestroy();}
    private int dp(float x){return (int)(x*getResources().getDisplayMetrics().density+.5f);}
    private TextView text(String s,int size,int color){TextView t=new TextView(this);t.setText(s);t.setTextSize(size);t.setTextColor(color);t.setLineSpacing(dp(3),1);t.setIncludeFontPadding(true);return t;}
    private GradientDrawable shape(int color,int radius){GradientDrawable d=new GradientDrawable();d.setColor(color);d.setCornerRadius(dp(radius));return d;}
    private LinearLayout vertical(){LinearLayout l=new LinearLayout(this);l.setOrientation(LinearLayout.VERTICAL);return l;}
    private void space(LinearLayout l,int height){Space s=new Space(this);l.addView(s,new LinearLayout.LayoutParams(1,dp(height)));}
    private LinearLayout card(LinearLayout parent){LinearLayout c=vertical();c.setPadding(dp(18),dp(16),dp(18),dp(16));c.setBackground(shape(CARD,20));LinearLayout.LayoutParams p=new LinearLayout.LayoutParams(-1,-2);p.bottomMargin=dp(14);parent.addView(c,p);return c;}
    private TextView button(String label,Runnable action){TextView t=text(label,14,TEXT);t.setTypeface(null,Typeface.BOLD);t.setGravity(Gravity.CENTER);t.setPadding(dp(12),dp(13),dp(12),dp(13));t.setMinHeight(dp(48));t.setBackground(shape(0xff2a3e4a,13));t.setClickable(true);t.setFocusable(true);t.setOnClickListener(v->action.run());return t;}
    private void fullButton(LinearLayout l,String label,Runnable action){TextView b=button(label,action);LinearLayout.LayoutParams p=new LinearLayout.LayoutParams(-1,-2);p.topMargin=dp(10);l.addView(b,p);}
    private void pair(LinearLayout l,View a,View b){LinearLayout row=new LinearLayout(this);LinearLayout.LayoutParams p=new LinearLayout.LayoutParams(0,-2,1);p.rightMargin=dp(5);row.addView(a,p);LinearLayout.LayoutParams q=new LinearLayout.LayoutParams(0,-2,1);q.leftMargin=dp(5);row.addView(b,q);l.addView(row);}
    private TextView metric(LinearLayout parent,String label){LinearLayout c=vertical();c.setPadding(dp(12),dp(12),dp(12),dp(12));c.setBackground(shape(0xff202f3b,14));c.addView(text(label,12,MUTED));TextView val=text("—",25,TEXT);val.setTypeface(null,Typeface.BOLD);c.addView(val);parent.addView(c,new LinearLayout.LayoutParams(0,-2,1));return val;}
    private LinearLayout metricRow(LinearLayout c){LinearLayout r=new LinearLayout(this);c.addView(r);return r;}
    private void build(){
        root=vertical();root.setBackgroundColor(BG);setContentView(root);
        root.setOnApplyWindowInsetsListener((v,insets)->{v.setPadding(insets.getSystemWindowInsetLeft(),insets.getSystemWindowInsetTop(),insets.getSystemWindowInsetRight(),insets.getSystemWindowInsetBottom());return insets;});root.requestApplyInsets();
        LinearLayout header=vertical();header.setPadding(dp(22),dp(18),dp(22),dp(12));root.addView(header);
        TextView title=text("GT BMS",25,TEXT);title.setTypeface(null,Typeface.BOLD);header.addView(title);
        header.addView(text("ANDROID  /  0.2.1 PROTOTYPE",11,MUTED));space(header,12);
        status=text("停止中",14,ACCENT);header.addView(status);substatus=text("",12,MUTED);header.addView(substatus);
        LinearLayout nav=new LinearLayout(this);nav.setPadding(dp(14),0,dp(14),dp(10));root.addView(nav);
        String[] tabs={"モニター","セル","記録","設定","走行"};
        for(int i=0;i<tabs.length;i++){final int index=i;TextView n=button(tabs[i],()->{if(index==4){startActivity(new Intent(this,TripActivity.class));return;}tab=index;build();});n.setTextSize(12);n.setPadding(dp(3),dp(8),dp(3),dp(8));n.setBackground(shape(i==tab?0xff31574f:BG,12));nav.addView(n,new LinearLayout.LayoutParams(0,dp(48),1));}
        ScrollView scroll=new ScrollView(this);scroll.setFillViewport(true);root.addView(scroll,new LinearLayout.LayoutParams(-1,0,1));body=vertical();body.setPadding(dp(16),dp(4),dp(16),dp(26));scroll.addView(body);
        soc=volts=amps=power=temps=delta=extra=quality=history=accessStatus=null;cellsBox=null;trend=null;chargeBar=null;
        if(tab==0)monitorPage();else if(tab==1)cellsPage();else if(tab==2)historyPage();else settingsPage();refresh();
    }
    private void monitorPage(){
        pair(body,button("Bluetooth接続",this::requestScan),button("接続診断",this::showDiagnostics));space(body,12);
        LinearLayout top=card(body);top.addView(text("バッテリー残量",13,MUTED));soc=text("— %",56,TEXT);soc.setTypeface(null,Typeface.BOLD);top.addView(soc);
        chargeBar=new ProgressBar(this,null,android.R.attr.progressBarStyleHorizontal);chargeBar.setMax(100);top.addView(chargeBar,new LinearLayout.LayoutParams(-1,dp(8)));space(top,14);
        navNote=text("未接続",13,MUTED);top.addView(navNote);
        LinearLayout metrics=card(body),r=metricRow(metrics);volts=metric(r,"総電圧 / V");spaceHorizontal(r);amps=metric(r,"電流・生値 / A");space(metrics,10);
        r=metricRow(metrics);power=metric(r,"電力（BMS符号）/ W");spaceHorizontal(r);delta=metric(r,"最大セル差 / mV");space(metrics,12);temps=text("温度 —",14,MUTED);metrics.addView(temps);
        LinearLayout graph=card(body);graph.addView(text("総電圧の推移 · この画面の直近120受信",12,MUTED));trend=new TrendView(this);graph.addView(trend,new LinearLayout.LayoutParams(-1,dp(155)));
        LinearLayout state=card(body);quality=text("",14,WARN);state.addView(quality);extra=text("",13,MUTED);state.addView(extra);
        fullButton(body,demo?"デモ終了":"デモを試す",this::toggleDemo);
        fullButton(body,"監視を停止",this::stopMonitoring);
        space(body,14);body.addView(text("試作版 · 実機での値照合は未完了\n保護設定はBMS側で管理されます。",12,MUTED));
    }
    private void spaceHorizontal(LinearLayout r){Space s=new Space(this);r.addView(s,new LinearLayout.LayoutParams(dp(10),1));}
    private void cellsPage(){LinearLayout c=card(body);c.addView(text("セル電圧",22,TEXT));c.addView(text("BMSが返した値をそのまま表示",12,MUTED));delta=text("セル差 —",20,ACCENT);c.addView(delta);temps=text("",13,MUTED);c.addView(temps);cellsBox=vertical();c.addView(cellsBox);}
    private void historyPage(){
        LinearLayout c=card(body);c.addView(text("端末内の記録",22,TEXT));history=text("読み込み中…",14,MUTED);c.addView(history);
        fullButton(c,"記録情報を更新",this::loadHistory);fullButton(c,"実機の記録をCSVに出力",()->{
            Intent i=new Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("text/csv");i.putExtra(Intent.EXTRA_TITLE,"gtbms-android-"+LocalDate.now()+".csv");startActivityForResult(i,42);
        });c.addView(text("接続中の有効な03/04ペアを保存します。デモは保存しません。出力開始時までの記録が対象です。",12,MUTED));
        LinearLayout d=card(body);d.addView(text("以前の表示速度ログ",19,TEXT));d.addView(text("Windows版のschema 2 JSONを選ぶと、保持観測から通信成功率とHTTP往復時間を再集計します。電圧・電流の履歴としては取り込みません。",13,MUTED));
        fullButton(d,"表示速度JSONを確認",()->{Intent i=new Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("*/*");startActivityForResult(i,43);});
        space(body,5);body.addView(text("アプリを削除すると端末内の記録も削除されます。必要な記録は先にCSVへ保存してください。",12,MUTED));loadHistory();
    }
    private void loadHistory(){io.execute(()->{try{String s=HistoryStore.get(this).summary();h.post(()->{if(history!=null)history.setText(s);});}catch(Exception e){h.post(()->toast("記録情報を読み込めませんでした"));}});}
    private void settingsPage(){
        LinearLayout permissions=card(body);permissions.addView(text("接続の準備",22,TEXT));accessStatus=text(ConnectionAccess.summary(this),14,MUTED);permissions.addView(accessStatus);
        fullButton(permissions,"権限を確認して機器を探す",this::requestScan);
        fullButton(permissions,"通知を許可・設定",this::requestNotifications);
        fullButton(permissions,"アプリ設定を開く",()->openSetting(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,android.net.Uri.parse("package:"+getPackageName()))));
        fullButton(permissions,"位置情報設定を開く",()->openSetting(new Intent(Settings.ACTION_LOCATION_SOURCE_SETTINGS)));
        fullButton(permissions,"Bluetooth設定を開く",()->openSetting(new Intent(Settings.ACTION_BLUETOOTH_SETTINGS)));
        fullButton(permissions,"接続診断",this::showDiagnostics);
        permissions.addView(text("探索中の互換性のため、正確な位置情報と端末の位置情報ONを使います。走行記録を開始したときだけGPS座標を取得・保存します。通知は接続・走行の状態と停止ボタンに使います。",12,MUTED));
        LinearLayout c=card(body);c.addView(text("表示・解析の設定",22,TEXT));c.addView(text("この設定はアプリ内だけに保存します。",12,MUTED));space(c,12);
        c.addView(text("電流・電力はBMSの符号で表示\nJBD仕様: ＋は充電、−は放電。生値の符号は変更しません。",14,TEXT));
        c.addView(text("想定セル数",14,TEXT));EditText cells=new EditText(this);cells.setText(Integer.toString(prefs.getInt("cells",16)));cells.setInputType(InputType.TYPE_CLASS_NUMBER);cells.setSingleLine(true);c.addView(cells);
        Switch keep=new Switch(this);keep.setText("表示中は画面を点灯したままにする");keep.setTextColor(TEXT);keep.setChecked(prefs.getBoolean("screen",false));keep.setMinHeight(dp(56));c.addView(keep);
        fullButton(c,"設定を保存",()->{
            if(AppState.active||TripState.active){toast("監視と走行記録を停止してから設定を変更してください");return;}
            try{int n=Integer.parseInt(cells.getText().toString());if(n<1 || n>32)throw new NumberFormatException();prefs.edit().remove("direction").putInt("cells",n).putBoolean("screen",keep.isChecked()).apply();applyScreen();toast("保存しました。次の接続から適用します");}catch(Exception e){toast("セル数は1〜32で入力してください");}
        });
        c.addView(text("走行中の正電流は回生の候補です。BMSの電流だけでは充電の発生源までは確定できません。",12,MUTED));
        LinearLayout compat=card(body);compat.addView(text("BLE送信方式",19,TEXT));
        compat.addView(text("権限が許可済みで、読取要求の応答待ちになる場合の切り分け用です。監視を停止してから変更します。",12,MUTED));
        final String[] modes={"auto","without_response","with_response"};Spinner mode=new Spinner(this);
        mode.setAdapter(new ArrayAdapter<>(this,android.R.layout.simple_spinner_dropdown_item,new String[]{"自動（初期値）","応答なし書き込み","応答あり書き込み"}));mode.setSelection(Math.max(0,Arrays.asList(modes).indexOf(prefs.getString("write_mode","auto"))));compat.addView(mode);
        fullButton(compat,"送信方式を保存",()->{if(AppState.active){toast("監視を停止してから変更してください");return;}prefs.edit().putString("write_mode",modes[mode.getSelectedItemPosition()]).apply();ConnectionLog.add("設定","送信方式="+modes[mode.getSelectedItemPosition()]);toast("次回接続から適用します");});
        String previous=prefs.getString("last_address","");if(BluetoothAdapter.checkBluetoothAddress(previous))fullButton(compat,"前回選んだ機器に再接続",()->confirmConnection(previous,"前回選択した機器"));
        fullButton(compat,"アドレスを指定して接続",this::connectByAddress);
        LinearLayout a=card(body);a.addView(text("接続と保存",19,TEXT));a.addView(text("通常JBD BLE（FF01 / FF02）\n基本03 + セル04を約1秒周期で取得\n最終受信から6秒で鮮度切れ\n切断後は最大30秒間隔で再接続\n実機記録は端末内SQLite\n通信・広告・アカウント登録なし",13,MUTED));
        LinearLayout b=card(body);b.addView(text("試作版の対応範囲",19,TEXT));b.addView(text("Android 8以降を対象とした試作です。BMS設定変更、認証が必要な独自通信、Windows DBの移行、クラウド同期は未実装です。GPS走行記録・センサー推定・航続学習は「走行」から利用できます。\n\nNTCはセンサー番号で表示し、取付位置や電池の化学系は推定しません。50Ah等の値はBMSの設定値で、実測容量ではありません。",13,MUTED));
        fullButton(b,"ライセンス",()->new AlertDialog.Builder(this).setTitle("ライセンス").setMessage(asset("LICENSES.txt")).setPositiveButton("閉じる",null).show());
    }
    private void applyScreen(){if(prefs.getBoolean("screen",false))getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);else getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);}
    private Sample current(){if(demo){long now=SystemClock.elapsedRealtime();if(demoSample==null || now-lastDemo>=1000){lastDemo=now;demoSample=Demo.sample(System.currentTimeMillis(),now,demoTick++);}return demoSample;}return AppState.latest;}
    private static String f(String format,Object... x){return String.format(Locale.JAPAN,format,x);}
    private void refresh(){
        if(accessStatus!=null)accessStatus.setText(ConnectionAccess.summary(this));
        if(status==null)return;Sample s=current();long now=SystemClock.elapsedRealtime();boolean fresh=demo || (AppState.monitoring && s!=null && s.fresh(now));
        String label=demo?"デモ表示 · 模擬データ":AppState.status;
        if(!demo && AppState.monitoring && s!=null && !fresh)label="更新が止まっています";
        if(!demo && s!=null && !fresh)label+=" · 最終受信値";
        status.setText(label);status.setTextColor(fresh?ACCENT:WARN);
        substatus.setText(demo?"接続・保存なしで画面をお試し中":AppState.detail);
        if(soc!=null){soc.setText(s==null?"— %":f("%.0f %%",s.soc));soc.setTextColor(fresh?TEXT:MUTED);chargeBar.setProgress(s==null?0:Math.max(0,Math.min(100,(int)s.soc)));
            navNote.setText(s==null?"読み取り待ち":s.operatingState()+" · "+(demo?"模擬":f("受信 %.1f秒前",Math.max(0,now-s.receivedElapsed)/1000.0)));
        }
        if(volts!=null)volts.setText(s==null?"—":f("%.2f",s.voltage));
        if(amps!=null)amps.setText(s==null?"—":f("%+.2f",s.rawCurrent));
        if(power!=null)power.setText(s==null?"—":f("%+.0f",s.rawPower()));
        if(delta!=null)delta.setText(s==null?"—":tab==1?f("セル差 %.0f mV  /  %dセル",s.deltaMv(),s.cells.length):f("%.0f",s.deltaMv()));
        if(temps!=null){StringBuilder t=new StringBuilder();if(s!=null){for(int i=0;i<s.temperatures.length;i++)t.append(f("NTC %d  %.1f°C%s",i+1,s.temperatures[i],i<s.temperatures.length-1?"   ":""));}temps.setText(t.length()==0?"温度データなし":t.toString());}
        if(extra!=null)extra.setText(s==null?"接続後にBMSの状態を表示します":f("残容量 %.2f Ah / 設定容量 %.2f Ah\nサイクル %d   ·   充電MOS %s / 放電MOS %s\n保護コード 0x%04X\n受信 %s",s.remainingAh,s.configuredAh,s.cycles,s.chargeMos?"ON":"OFF",s.dischargeMos?"ON":"OFF",s.protection,Instant.ofEpochMilli(s.receivedAt).atZone(ZoneId.systemDefault()).format(DateTimeFormatter.ofPattern("MM/dd HH:mm:ss"))));
        if(quality!=null){String q=s==null?"":s.flags.isEmpty()?"BMS保護フラグなし（応答値）":String.join(" / ",s.flags);if(!AppState.storageError.isEmpty())q+="\n"+AppState.storageError;if(AppState.storageLosses>0)q+="\nアプリ起動中の未保存: "+AppState.storageLosses+"件";quality.setText(q);}
        if(trend!=null)trend.add(s);
        if(cellsBox!=null)renderCells(s);
    }
    private void renderCells(Sample s){
        int n=s==null?0:s.cells.length;
        if(cellsBox.getChildCount()!=n){cellsBox.removeAllViews();for(int i=0;i<n;i++){TextView t=text("",16,TEXT);t.setPadding(dp(12),dp(10),dp(12),dp(10));t.setMinHeight(dp(48));cellsBox.addView(t);}}
        for(int i=0;i<n;i++){TextView t=(TextView)cellsBox.getChildAt(i);String suffix=Math.abs(s.cells[i]-s.minCell())<.00001?"  最低":Math.abs(s.cells[i]-s.maxCell())<.00001?"  最高":"";
            if((s.balancing&(1L<<i))!=0)suffix+="  BAL";t.setText(f("%02d     %.3f V%s",i+1,s.cells[i],suffix));t.setBackground(shape(i%2==0?0xff23333f:CARD,8));t.setTextColor(s.cells[i]<=0 || s.cells[i]>5?WARN:TEXT);}
    }
    private void toggleDemo(){
        if(AppState.active){new AlertDialog.Builder(this).setTitle("実機監視を停止してデモへ").setMessage("現在のBluetooth監視と記録を停止します。").setNegativeButton("戻る",null).setPositiveButton("デモへ",(d,w)->{stopMonitoring();demo=true;demoSample=null;build();}).show();return;}
        demo=!demo;demoSample=null;build();
    }
    private void stopMonitoring(){stopService(new Intent(this,MonitorService.class));AppState.active=false;AppState.monitoring=false;demo=false;demoSample=null;refresh();}
    private BluetoothAdapter adapter(){return ConnectionAccess.adapter(this);}
    private void problem(String stage,String message){
        ConnectionLog.add(stage,message);if(!AppState.active){AppState.status=stage;AppState.detail=message;}refresh();
    }
    private void openSetting(Intent intent){try{startActivity(intent);}catch(ActivityNotFoundException e){toast("設定画面を開けません。Androidの設定から開いてください");}}
    private void permissionHelp(){
        problem("権限待ち","付近のデバイスと正確な位置情報の許可を確認してください");
        new AlertDialog.Builder(this).setTitle("接続に使う権限").setMessage(ConnectionAccess.summary(this)+"\n\nアプリの権限で「付近のデバイス」と「位置情報 → アプリの使用中のみ・正確な位置情報」を許可してください。許可済みならBluetoothと端末の位置情報をONにします。")
            .setPositiveButton("アプリ設定",(d,w)->openSetting(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,android.net.Uri.parse("package:"+getPackageName())))).setNegativeButton("閉じる",null).show();
    }
    private void requestScan(){
        if(AppState.active){new AlertDialog.Builder(this).setTitle("接続先を選び直す").setMessage("現在の監視を停止して機器を探します。").setNegativeButton("戻る",null).setPositiveButton("探す",(d,w)->{stopMonitoring();requestScan();}).show();return;}
        if(demo){demo=false;demoSample=null;build();}
        String[] permissions=ConnectionAccess.missing(this);
        if(permissions.length>0){
            for(String p:permissions)if(!ConnectionAccess.allowed(this,p)&&prefs.getBoolean("asked."+p,false)&&!shouldShowRequestPermissionRationale(p)){permissionHelp();return;}
            problem("権限の確認","Bluetooth探索のため、付近のデバイスと正確な位置情報を確認します");
            new AlertDialog.Builder(this).setTitle("Bluetooth接続の準備").setMessage("付近のデバイスと位置情報の許可を求めます。位置情報はBLE探索の互換性のために使います。「走行記録」を開始するとGPS座標も取得・保存します。\n\n位置情報は「アプリの使用中のみ」と「正確」を選んでください。")
                .setNegativeButton("戻る",null).setPositiveButton("許可を確認",(d,w)->{SharedPreferences.Editor e=prefs.edit();for(String p:permissions)e.putBoolean("asked."+p,true);e.apply();requestPermissions(permissions,31);}).show();return;
        }
        BluetoothAdapter a=adapter();if(a==null){problem("Bluetooth未対応","この端末にはBluetooth機能がありません");return;}
        try{if(!a.isEnabled()){problem("Bluetooth待ち","BluetoothをONにしてください");startActivityForResult(new Intent(BluetoothAdapter.ACTION_REQUEST_ENABLE),32);return;}}
        catch(SecurityException e){permissionHelp();return;}catch(ActivityNotFoundException e){openSetting(new Intent(Settings.ACTION_BLUETOOTH_SETTINGS));return;}
        if(!ConnectionAccess.locationOn(this)){
            problem("位置情報待ち","BLE探索のため端末の位置情報をONにしてください");
            new AlertDialog.Builder(this).setTitle("端末の位置情報").setMessage("この版はBLE探索の互換性を優先するため、端末の位置情報ONを確認します。GPS座標は走行記録中に取得・保存します。")
                .setPositiveButton("設定を開く",(d,w)->{try{startActivityForResult(new Intent(Settings.ACTION_LOCATION_SOURCE_SETTINGS),34);}catch(ActivityNotFoundException e){toast("端末の設定から位置情報をONにしてください");}}).setNegativeButton("戻る",null).show();return;
        }
        if(Build.VERSION.SDK_INT>=33&&!ConnectionAccess.allowed(this,Manifest.permission.POST_NOTIFICATIONS)&&!prefs.getBoolean("notification_prompted",false)){
            scanAfterNotifications=true;prefs.edit().putBoolean("notification_prompted",true).apply();requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS},33);return;
        }
        scan();
    }
    private void requestNotifications(){
        scanAfterNotifications=false;
        if(Build.VERSION.SDK_INT>=33&&!ConnectionAccess.allowed(this,Manifest.permission.POST_NOTIFICATIONS)&&(!prefs.getBoolean("notification_prompted",false)||shouldShowRequestPermissionRationale(Manifest.permission.POST_NOTIFICATIONS))){
            prefs.edit().putBoolean("notification_prompted",true).apply();requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS},33);
        }else openSetting(new Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE,getPackageName()));
    }
    @Override public void onRequestPermissionsResult(int code,String[] permissions,int[] grants){super.onRequestPermissionsResult(code,permissions,grants);
        if(code==31){ConnectionLog.add("権限","探索に必要な未許可数="+ConnectionAccess.missing(this).length);if(ConnectionAccess.missing(this).length==0)deferScan();else permissionHelp();}
        if(code==33){ConnectionLog.add("通知",ConnectionAccess.allowed(this,Manifest.permission.POST_NOTIFICATIONS)?"許可済み":"未許可（BLEを妨げません）");if(scanAfterNotifications){scanAfterNotifications=false;deferScan();}else refresh();}
        if(code==35){if(ConnectionAccess.connectAllowed(this))toast("付近のデバイスを許可しました。もう一度、接続する機器を選んでください");else permissionHelp();}
    }
    private void scan(){
        if(!ConnectionAccess.scanReady(this)){permissionHelp();return;}
        long now=SystemClock.elapsedRealtime();if(lastScanStart!=0&&now-lastScanStart<6000){toast("少し待ってからもう一度探索してください");return;}lastScanStart=now;
        stopScan();found.clear();addresses.clear();scanAdapter=new ArrayAdapter<>(this,android.R.layout.simple_list_item_1,new ArrayList<>());
        LinearLayout box=vertical();box.setPadding(dp(16),dp(8),dp(16),dp(8));TextView hint=text("近くの機器を探しています（15秒）\n通常JBD対応機器を選んでください。",13,MUTED);scanHint=hint;box.addView(hint);ListView list=new ListView(this);list.setAdapter(scanAdapter);box.addView(list,new LinearLayout.LayoutParams(-1,dp(270)));
        scanDialog=new AlertDialog.Builder(this).setTitle("Bluetooth機器").setView(box).setNegativeButton("閉じる",null).create();scanDialog.setOnDismissListener(d->stopScan());
        list.setOnItemClickListener((parent,view,pos,id)->{if(pos<addresses.size()){String address=addresses.get(pos),description=found.get(address);scanDialog.dismiss();confirmConnection(address,description);}});scanDialog.show();
        final int generation=++scanGeneration;
        scanCallback=new ScanCallback(){
            @Override public void onScanResult(int type,ScanResult r){h.post(()->addScanResult(generation,r,hint));}
            @Override public void onBatchScanResults(List<ScanResult> results){h.post(()->{for(ScanResult r:results)addScanResult(generation,r,hint);});}
            @Override public void onScanFailed(int error){h.post(()->{if(generation==scanGeneration){stopScan();String msg=BleDiagnostics.scanError(error)+"（code="+error+"）";hint.setText(msg);problem("探索失敗",msg);}});}
        };
        try{scanner=adapter().getBluetoothLeScanner();if(scanner==null){stopScan();hint.setText("Bluetooth探索を開始できません");problem("探索失敗","scanner=null / Bluetooth状態を確認してください");return;}
            scanner.startScan(null,new ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).setReportDelay(0).build(),scanCallback);
            problem("探索中","近くのBluetooth機器を探しています（15秒）");
            h.postDelayed(()->{if(generation==scanGeneration){stopScan();String msg=found.isEmpty()?"見つかりませんでした。BMSの電源と、Windows・他のBMSアプリの接続を確認してください。":"探索終了（"+found.size()+"件）。機器を選んでください。";hint.setText(msg);problem("探索終了",msg);}},15000);
        }catch(SecurityException e){stopScan();hint.setText("探索の権限がありません。接続診断を確認してください");problem("探索権限エラー",e.getClass().getSimpleName());}
        catch(RuntimeException e){stopScan();hint.setText("探索を開始できません。接続診断を確認してください");problem("探索エラー",e.getClass().getSimpleName());}
    }
    private void addScanResult(int generation,ScanResult r,TextView hint){
        if(generation!=scanGeneration||scanner==null)return;
        try{String address=r.getDevice().getAddress();String name=r.getScanRecord()==null?null:r.getScanRecord().getDeviceName();if(name==null||name.trim().isEmpty())name=r.getDevice().getName();if(name==null||name.trim().isEmpty())name="名称なし";
            if(found.size()>=100&&!found.containsKey(address))return;
            found.put(address,name+"\n"+address+"  /  "+r.getRssi()+" dBm");addresses.clear();addresses.addAll(found.keySet());scanAdapter.clear();scanAdapter.addAll(found.values());scanAdapter.notifyDataSetChanged();
        }catch(SecurityException e){stopScan();hint.setText("Bluetooth権限がありません");problem("探索権限エラー","探索中に権限が取り消されました");}
    }
    private void stopScan(){
        scanGeneration++;
        if(scanner!=null&&scanCallback!=null){
            try{scanner.stopScan(scanCallback);}
            catch(SecurityException ignored){/* Permission may be revoked while scanning. */}
            catch(RuntimeException ignored){}
            ConnectionLog.add("探索停止","候補="+found.size()+"件");
        }
        scanner=null;scanCallback=null;
    }
    private void confirmConnection(String address,String description){
        new AlertDialog.Builder(this).setTitle("この機器から読み取りますか？").setMessage(description+"\n\nWindows版・Xiaoxiangなど、接続中の他のBMSアプリがあれば切断してください。BLE機器は通常、Androidの設定で事前ペアリングせずアプリ内から接続します。")
            .setNegativeButton("戻る",null).setPositiveButton("接続",(d,w)->startMonitor(address)).show();
    }
    private void connectByAddress(){
        EditText input=new EditText(this);input.setSingleLine(true);input.setHint("AA:BB:CC:DD:EE:FF");input.setText(prefs.getString("last_address",""));
        AlertDialog d=new AlertDialog.Builder(this).setTitle("既知のBLEアドレス").setMessage("BMSのアドレスが分かる場合に使います。探索を省略して接続します。").setView(input).setNegativeButton("戻る",null).setPositiveButton("確認",null).create();
        d.setOnShowListener(v->d.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(b->{String value=input.getText().toString().trim().toUpperCase(Locale.US);if(!BluetoothAdapter.checkBluetoothAddress(value)){input.setError("コロン区切りのBLEアドレスを入力してください");return;}d.dismiss();confirmConnection(value,"指定した機器\n"+value);}));d.show();
    }
    private void startMonitor(String address){
        stopScan();demo=false;demoSample=null;
        if(!ConnectionAccess.connectAllowed(this)){requestPermissions(new String[]{Manifest.permission.BLUETOOTH_CONNECT},35);return;}
        if(!ConnectionAccess.bluetoothOn(this)){problem("Bluetooth待ち","BluetoothをONにして、接続する機器を選び直してください");return;}
        prefs.edit().putString("last_address",address).apply();ConnectionLog.add("接続開始","利用者が機器を選択 / "+prefs.getString("write_mode","auto"));
        try{startForegroundService(new Intent(this,MonitorService.class).setAction(MonitorService.START).putExtra("address",address));tab=0;build();}
        catch(RuntimeException e){problem("監視開始失敗",e.getClass().getSimpleName()+" / アプリを表示した状態で再試行してください");}
    }
    private void showDiagnostics(){
        ScrollView scroll=new ScrollView(this);TextView report=text(ConnectionLog.report(this),12,TEXT);report.setTextIsSelectable(true);report.setPadding(dp(18),dp(12),dp(18),dp(12));scroll.addView(report);
        AlertDialog dialog=new AlertDialog.Builder(this).setTitle("接続診断").setView(scroll).setPositiveButton("閉じる",null).setNeutralButton("コピー",null).setNegativeButton("ファイル保存",null).create();
        dialog.setOnShowListener(d->{dialog.getButton(AlertDialog.BUTTON_NEUTRAL).setOnClickListener(v->{((android.content.ClipboardManager)getSystemService(CLIPBOARD_SERVICE)).setPrimaryClip(ClipData.newPlainText("GT BMS 接続診断",ConnectionLog.report(this)));toast("診断をコピーしました");});
            dialog.getButton(AlertDialog.BUTTON_NEGATIVE).setOnClickListener(v->{diagnosticExport=ConnectionLog.report(this);Intent i=new Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("text/plain");i.putExtra(Intent.EXTRA_TITLE,"gtbms-connection-"+LocalDate.now()+".txt");startActivityForResult(i,44);});});dialog.show();
    }
    @Override protected void onActivityResult(int request,int result,Intent data){super.onActivityResult(request,result,data);
        if(request==32){if(result==RESULT_OK)deferScan();else problem("Bluetooth待ち","Bluetoothが有効になっていません");return;}
        if(request==34){if(ConnectionAccess.locationOn(this))deferScan();else problem("位置情報待ち","端末の位置情報がOFFです");return;}
        if(result!=RESULT_OK || data==null || data.getData()==null)return;
        if(request==44){final String report=diagnosticExport==null?ConnectionLog.report(this):diagnosticExport;diagnosticExport=null;io.execute(()->{try(OutputStream out=getContentResolver().openOutputStream(data.getData())){if(out==null)throw new IOException();out.write(report.getBytes(StandardCharsets.UTF_8));h.post(()->toast("診断を書き出しました"));}catch(Exception e){h.post(()->toast("診断を書き出せませんでした"));}});}
        if(request==42){toast("CSVを書き出しています");io.execute(()->{try{long count=HistoryStore.get(this).export(this,data.getData());h.post(()->toast(count+"件を書き出しました"));}catch(Exception e){h.post(()->toast("出力に失敗しました。作成途中のファイルが残る場合があります"));}});}
        if(request==43)io.execute(()->{try(InputStream in=getContentResolver().openInputStream(data.getData())){
            if(in==null)throw new IOException();ByteArrayOutputStream out=new ByteArrayOutputStream();byte[] b=new byte[8192];int n;while((n=in.read(b))!=-1){if(out.size()+n>1024*1024)throw new IOException();out.write(b,0,n);}
            String report=latency(new JSONObject(new String(out.toByteArray(),StandardCharsets.UTF_8)));h.post(()->{if(!isFinishing() && !isDestroyed())new AlertDialog.Builder(this).setTitle("表示速度ログ（参考集計）").setMessage(report).setPositiveButton("閉じる",null).show();});
        }catch(Exception e){h.post(()->toast("schema 2の表示速度ログを読み込めません（最大1 MiB / 600観測）"));}});
    }
    private static String latency(JSONObject j)throws JSONException{
        if(j.getInt("schema")!=2)throw new JSONException("schema");JSONArray a=j.getJSONArray("observations");if(a.length()==0 || a.length()>600)throw new JSONException("size");
        int success=0;ArrayList<Double> rtt=new ArrayList<>();Set<String> sources=new TreeSet<>();
        for(int i=0;i<a.length();i++){JSONObject o=a.getJSONObject(i);Object value=o.get("success");if(!(value instanceof Boolean))throw new JSONException("success");sources.add(o.optString("source_mode","unknown"));
            if(Boolean.TRUE.equals(value)){success++;Object raw=o.opt("http_rtt_ms");if(raw instanceof Number){double x=((Number)raw).doubleValue();if(Double.isFinite(x)&&x>=0&&x<60000)rtt.add(x);}}}
        Collections.sort(rtt);String stats=rtt.isEmpty()?"有効なHTTP RTTなし":f("HTTP往復 p50 %.1f ms / p95 %.1f ms",rtt.get((int)Math.ceil(rtt.size()*.5)-1),rtt.get((int)Math.ceil(rtt.size()*.95)-1));
        return "保持観測 "+a.length()+"\n鮮度内表示成功 "+success+" / "+a.length()+"\n"+stats+"\n計測源 "+String.join(", ",sources)+"\n\n保存済みsuccessフラグに基づく参考集計です。実機の電池履歴への追加や、改善率・BLE欠測率への換算は行いません。";
    }
    private String asset(String file){try(InputStream in=getAssets().open(file);ByteArrayOutputStream b=new ByteArrayOutputStream()){byte[] c=new byte[4096];int n;while((n=in.read(c))!=-1)b.write(c,0,n);return new String(b.toByteArray(),StandardCharsets.UTF_8);}catch(Exception e){return "GT BMS Monitor · MIT License";}}
    private void toast(String s){if(!isDestroyed())Toast.makeText(this,s,Toast.LENGTH_LONG).show();}
}
