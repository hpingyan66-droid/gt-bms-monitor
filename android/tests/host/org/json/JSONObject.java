package org.json;
import java.util.*;
/** Host-test-only JSON field store; no parsing or serialization claim. */
public final class JSONObject {
    public static final Object NULL=new Object();
    private final Map<String,Object> values=new HashMap<>();
    public JSONObject put(String key,Object value)throws JSONException {values.put(key,value);return this;}
    public boolean getBoolean(String key){return (Boolean)values.get(key);}
    public String getString(String key){return (String)values.get(key);}
    public int getInt(String key){return ((Number)values.get(key)).intValue();}
}
