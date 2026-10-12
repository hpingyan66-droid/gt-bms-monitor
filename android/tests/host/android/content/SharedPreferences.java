package android.content;
/** Host-test-only subset. Not an Android implementation. */
public interface SharedPreferences {
    String getString(String key,String fallback);
    int getInt(String key,int fallback);
    boolean getBoolean(String key,boolean fallback);
}
