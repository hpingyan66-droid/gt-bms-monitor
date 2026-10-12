package jp.k4159191.gtbms.prototype;
import org.json.*;
/** Host-test-only finite-number helper; does not exercise Android SQLite. */
final class TripStore {
    static void put(JSONObject j,String k,double v)throws JSONException {j.put(k,Double.isFinite(v)?v:JSONObject.NULL);}
}
