package jp.k4159191.gtbms.prototype;

import android.content.*;
import org.json.*;
import jp.k4159191.gtbms.prototype.core.JbdProtocol;

final class TripProfile {
    final double maxKmh,reserve,manualRate,mass,watts,efficiency,climbKmh,crr,cda;
    // The app is used with the phone fixed to the vehicle. Ignore the retired
    // preference, including mounted=false left by 0.2.0 installations.
    final boolean mounted=true;
    // Direction is established by the JBD protocol, not a retired user toggle.
    final boolean signConfirmed=true;
    final int revision;
    TripProfile(Context c){SharedPreferences p=c.getSharedPreferences("trip_profile",0);
        maxKmh=number(p,"max_kmh",40);reserve=number(p,"reserve",10);manualRate=number(p,"manual_rate",Double.NaN);
        mass=number(p,"mass",Double.NaN);watts=number(p,"watts",Double.NaN);efficiency=number(p,"efficiency",.75);
        climbKmh=number(p,"climb_kmh",5);crr=number(p,"crr",.015);cda=number(p,"cda",.6);
        revision=p.getInt("revision",1);
    }
    static double number(SharedPreferences p,String k,double fallback){try{return Double.parseDouble(p.getString(k,Double.toString(fallback)));}catch(Exception e){return fallback;}}
    JSONObject json()throws JSONException{JSONObject j=new JSONObject();j.put("max_kmh",maxKmh).put("reserve_soc",reserve).put("mounted",mounted).put("sign_confirmed",signConfirmed).put("current_direction",JbdProtocol.CURRENT_DIRECTION).put("sign_basis","jbd_protocol").put("revision",revision);
        TripStore.put(j,"manual_wh_km",manualRate);TripStore.put(j,"total_kg",mass);TripStore.put(j,"continuous_electric_w",watts);j.put("efficiency",efficiency).put("climb_kmh",climbKmh).put("crr",crr).put("cda",cda);return j;}
}
