package jp.k4159191.gtbms.prototype;
import android.content.Context;
import android.graphics.*;
import android.view.View;
import java.util.*;
import jp.k4159191.gtbms.prototype.core.Sample;

final class TrendView extends View {
    private final ArrayList<Sample> points=new ArrayList<>();private final Paint p=new Paint(3);
    TrendView(Context c){super(c);setMinimumHeight((int)(150*c.getResources().getDisplayMetrics().density));setContentDescription("直近の総電圧。縦軸は自動調整、横軸は受信時刻");}
    void add(Sample s){
        if(s==null)return;
        if(!points.isEmpty() && !points.get(0).source.equals(s.source))points.clear();
        if(!points.isEmpty() && points.get(points.size()-1).receivedElapsed>=s.receivedElapsed)return;
        points.add(s);if(points.size()>120)points.remove(0);invalidate();
    }
    @Override protected void onDraw(Canvas c){super.onDraw(c);float d=getResources().getDisplayMetrics().density;
        p.setTypeface(Typeface.create("sans-serif",Typeface.NORMAL));p.setTextSize(11*d);p.setColor(Color.rgb(165,182,195));
        if(points.size()<2){c.drawText("2回以上の受信で推移を表示します",12*d,65*d,p);return;}
        double lo=Double.POSITIVE_INFINITY,hi=0;for(Sample s:points){lo=Math.min(lo,s.voltage);hi=Math.max(hi,s.voltage);}
        if(hi-lo<.1){double mid=(hi+lo)/2;lo=mid-.05;hi=mid+.05;}
        float left=57*d,right=getWidth()-14*d,top=15*d,bottom=getHeight()-26*d;
        c.drawText(String.format(Locale.US,"%.2f V",hi),0,top+4*d,p);c.drawText(String.format(Locale.US,"%.2f V",lo),0,bottom,p);
        long start=points.get(0).receivedElapsed,end=points.get(points.size()-1).receivedElapsed;
        p.setStrokeWidth(d);p.setColor(Color.rgb(50,67,80));c.drawLine(left,top,right,top,p);c.drawLine(left,bottom,right,bottom,p);
        p.setColor(Color.rgb(120,218,193));p.setStrokeWidth(2*d);Path path=new Path();Sample prev=null;
        for(Sample s:points){float x=left+(right-left)*(s.receivedElapsed-start)/Math.max(1,end-start);float y=bottom-(float)((s.voltage-lo)/(hi-lo))*(bottom-top);
            if(prev==null || s.receivedElapsed-prev.receivedElapsed>6000)path.moveTo(x,y);else path.lineTo(x,y);prev=s;}
        p.setStyle(Paint.Style.STROKE);c.drawPath(path,p);p.setStyle(Paint.Style.FILL);p.setColor(Color.rgb(165,182,195));
        c.drawText(String.format(Locale.JAPAN,"%.0f秒前",(end-start)/1000.0),left,getHeight()-5*d,p);c.drawText("最新",right-24*d,getHeight()-5*d,p);
    }
}
