package jp.k4159191.gtbms.prototype;
import android.content.*;
import java.util.*;
import jp.k4159191.gtbms.prototype.core.*;

public final class ProfileMigrationTests {
    private static int checks;
    private static void check(boolean ok,String message){checks++;if(!ok)throw new AssertionError(message);}
    private static final class Preferences implements SharedPreferences {
        final Map<String,Object> values=new HashMap<>();
        public String getString(String k,String f){return (String)values.getOrDefault(k,f);}
        public int getInt(String k,int f){return (Integer)values.getOrDefault(k,f);}
        public boolean getBoolean(String k,boolean f){return (Boolean)values.getOrDefault(k,f);}
    }
    public static void main(String[] args)throws Exception {
        Preferences prefs=new Preferences();prefs.values.put("mounted",false);prefs.values.put("sign_confirmed",false);
        prefs.values.put("revision",7);prefs.values.put("mass","150");prefs.values.put("reserve","15");
        prefs.values.put("last_model_key","original-model-key");Map<String,Object> original=new HashMap<>(prefs.values);
        Context context=new Context(){public SharedPreferences getSharedPreferences(String name,int mode){if(!"trip_profile".equals(name))throw new AssertionError(name);return prefs;}};
        TripProfile profile=new TripProfile(context);
        check(profile.mounted,"legacy mounted=false no longer disables IMU");
        check(profile.signConfirmed,"JBD protocol supplies current interpretation");
        check(profile.revision==7&&profile.mass==150&&profile.reserve==15,"vehicle settings preserved");
        check(prefs.values.equals(original),"reading new profile does not rewrite stored legacy data");
        check(profile.json().getBoolean("mounted"),"new trip exports fixed premise");
        check(profile.json().getString("sign_basis").equals("jbd_protocol"),"export distinguishes protocol interpretation from user measurement");
        check(profile.json().getString("current_direction").equals(JbdProtocol.CURRENT_DIRECTION),"profile and protocol agree");
        TripEngine engine=new TripEngine(profile.maxKmh,profile.mounted);
        TripEngine.Fix fix=new TripEngine.Fix();fix.elapsed=1000;fix.lat=35;fix.lon=139;fix.accuracy=3;fix.speed=3;fix.speedAccuracy=.1;fix.bearing=90;
        check(engine.offer(fix,1000),"anchor accepted");engine.step(1000,0);
        for(int i=0;i<=150;i++){long t=2500+i*20;engine.imu(t,1,0,0);if(t%1000==0)engine.step(t,0);}
        TripEngine.Point point=engine.step(5600,0).point;
        check(point.speed>5&&point.speed<7,"legacy false preference reaches active inertial acceleration branch");
        check(point.source.equals("imu_estimate"),"new point carries inertial provenance");
        System.out.println("PASS: "+checks+" profile migration assertions (host API stubs; not Android integration)");
    }
}
