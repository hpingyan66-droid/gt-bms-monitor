package jp.k4159191.gtbms.prototype.core;

import java.util.*;

/** Single-threaded, monotonic-clock odometry. Coordinates are never treated as exact.
 * GPS gaps retain their original estimates; endpoint correction does not recover the true route. */
public final class TripEngine {
    public static final double R=6371008.8;
    public static final int MAX_PENDING=7200;
    public static final class Fix {
        public long elapsed;
        public double lat,lon,accuracy,speed=Double.NaN,speedAccuracy=Double.NaN,bearing=Double.NaN;
        public double altitude=Double.NaN,verticalAccuracy=Double.NaN;
        public boolean mock;
    }
    public static final class Point {
        public long seq,wall,elapsed,gpsElapsed;
        public double rawLat=Double.NaN,rawLon=Double.NaN,lat=Double.NaN,lon=Double.NaN;
        public double rawAltitude=Double.NaN,altitude=Double.NaN,pressure=Double.NaN;
        public double speed=Double.NaN,bearing=Double.NaN,error=Double.NaN;
        public double rawMeters,meters,totalMeters,grade=Double.NaN;
        public double gpsLat=Double.NaN,gpsLon=Double.NaN,gpsAccuracy=Double.NaN;
        public double gpsAltitude=Double.NaN,gpsVerticalAccuracy=Double.NaN;
        public String source="waiting_gps";
        public boolean learnable,mock;
        public double seconds;
    }
    public static final class Update {
        public final Point point;
        public final List<Point> revisions;
        public final boolean recovered;
        Update(Point p,List<Point> r,boolean recovered){point=p;revisions=r;this.recovered=recovered;}
    }
    private final double maxSpeed;
    private final boolean mounted;
    private long seq,lastStep=-1,lastImu=-1,lastImuValid=-1,lastFixElapsed=-1,anchorTime;
    private Fix queued,lastFix;
    private double lat=Double.NaN,lon=Double.NaN,speed=Double.NaN,heading=Double.NaN;
    private double biasE,biasN,biasYaw,baseMeters,pendingMeters,anchorLat,anchorLon;
    private double positionAnchorLat,positionAnchorLon,positionAnchorAccuracy;
    private long positionAnchorTime;
    private double pressure=Double.NaN,baro=Double.NaN,altOffset=Double.NaN,alt=Double.NaN;
    private long pressureTime=-1;
    private int stationaryFixes;
    private boolean lostHistory;
    private final ArrayList<Point> pending=new ArrayList<>();
    private final ArrayDeque<Point> gradeWindow=new ArrayDeque<>();
    public int rejectedFixes,corrections;
    public double correctedMeters,estimatedMeters,maxObservedGrade=Double.NaN;
    public String fixIssue="最初のGPS測位を待っています";

    public TripEngine(double maxKmh,boolean mounted){
        if(!Double.isFinite(maxKmh)||maxKmh<5||maxKmh>180)throw new IllegalArgumentException("max speed");
        maxSpeed=maxKmh/3.6;this.mounted=mounted;
    }
    public boolean offer(Fix f,long now){
        if(f==null||!Double.isFinite(f.lat)||!Double.isFinite(f.lon)||Math.abs(f.lat)>85||Math.abs(f.lon)>180
            ||!Double.isFinite(f.accuracy)||f.accuracy<=0||f.accuracy>35||f.elapsed>now+100||now-f.elapsed>3000
            ||f.elapsed<=lastFixElapsed||(queued!=null&&f.elapsed<=queued.elapsed)
            ||(Double.isFinite(f.speed)&&(f.speed<0||f.speed>maxSpeed))){rejectedFixes++;fixIssue="GPSの精度・時刻・速度を確認中";return false;}
        if(lastFix!=null){double dt=(f.elapsed-lastFix.elapsed)/1000.0;
            if(distance(lastFix.lat,lastFix.lon,f.lat,f.lon)>maxSpeed*dt+3*(f.accuracy+lastFix.accuracy)){
                rejectedFixes++;fixIssue="GPS位置の急変を除外";return false;}}
        queued=f;return true;
    }
    /** East/north acceleration and upward-axis angular velocity, after Android attitude fusion. */
    public void imu(long now,double east,double north,double yawRate){
        double dt=lastImu<0?0:(now-lastImu)/1000.0;lastImu=now;
        if(!mounted||!Double.isFinite(east)||!Double.isFinite(north)||!Double.isFinite(yawRate)
            ||Math.hypot(east,north)>6||Math.abs(yawRate)>2.5||dt<=0||dt>.2)return;
        lastImuValid=now;
        if(stationaryFixes>=3&&now-lastFixElapsed<2000&&Math.hypot(east,north)<.4&&Math.abs(yawRate)<.08){
            biasE=.98*biasE+.02*east;biasN=.98*biasN+.02*north;biasYaw=.98*biasYaw+.02*yawRate;
        }
        long age=now-lastFixElapsed;
        if(!Double.isFinite(speed)||!Double.isFinite(heading)||age<1500||age>60000)return;
        // Positive Android world Z rotation is counterclockwise; GPS bearing is clockwise.
        heading=wrap(heading-(yawRate-biasYaw)*dt);
        double forward=(east-biasE)*Math.sin(heading)+(north-biasN)*Math.cos(heading);
        if(Math.abs(forward)<.08)forward=0;
        speed=clamp(speed+clamp(forward,-2,2)*dt,0,maxSpeed);
    }
    public void pressure(long now,double hpa){
        if(!Double.isFinite(hpa)||hpa<300||hpa>1100)return;
        pressure=Double.isFinite(pressure)?.9*pressure+.1*hpa:hpa;pressureTime=now;
        baro=44330*(1-Math.pow(pressure/1013.25,.190294957));
        if(Double.isFinite(altOffset))alt=baro+altOffset;
    }
    public Update step(long now,long wall){
        if(lastStep>=0&&now<=lastStep)throw new IllegalArgumentException("non-monotonic step");
        Point p=new Point();p.seq=++seq;p.wall=wall;p.elapsed=now;
        p.seconds=lastStep<0?0:(now-lastStep)/1000.0;lastStep=now;
        boolean clockGap=p.seconds>3;double dt=clockGap?0:p.seconds;
        if(Double.isFinite(lat)&&Double.isFinite(speed)&&Double.isFinite(heading)){
            double d=speed*dt;double[] xy=offset(lat,lon,d*Math.sin(heading),d*Math.cos(heading));lat=xy[0];lon=xy[1];p.rawMeters=d;
        }
        p.rawLat=p.lat=lat;p.rawLon=p.lon=lon;p.rawAltitude=p.altitude=currentAltitude(now);
        p.pressure=now-pressureTime<3000?pressure:Double.NaN;p.speed=speed;p.bearing=heading;p.mock=lastFix!=null&&lastFix.mock;
        p.meters=p.rawMeters;p.error=lastFix==null?Double.NaN:lastFix.accuracy+2*maxSpeed*Math.max(0,(now-lastFixElapsed)/1000.0);
        p.source=lastFix==null?"waiting_gps":clockGap?"clock_gap":now-lastFixElapsed>60000?"hold_uncertain":mounted&&now-lastImuValid<1500?"imu_estimate":"speed_hold";
        if(clockGap){lostHistory=true;gradeWindow.clear();}
        if(lastFix!=null){pending.add(p);pendingMeters+=p.meters;}
        Fix f=queued;queued=null;
        if(f==null||now-f.elapsed>4000){
            if(pending.size()>MAX_PENDING){baseMeters+=pending.get(0).meters;estimatedMeters+=pending.get(0).meters;pendingMeters-=pending.remove(0).meters;lostHistory=true;}
            p.totalMeters=totalMeters();return new Update(p,Collections.emptyList(),false);
        }
        boolean first=lastFix==null;double interval=first?0:(now-anchorTime)/1000.0;
        boolean recovery=!first&&(interval>3||lostHistory);
        double fixedSpeed=goodSpeed(f)?f.speed:Double.NaN;
        double positionMeters=first?0:distance(positionAnchorLat,positionAnchorLon,f.lat,f.lon);
        boolean positionMoved=!first&&positionMeters>Math.max(4,f.accuracy+positionAnchorAccuracy);
        if(!Double.isFinite(fixedSpeed)&&!first&&interval<=3&&positionMoved)fixedSpeed=Math.min(maxSpeed,positionMeters/Math.max(.001,(now-positionAnchorTime)/1000.0));
        if(Double.isFinite(fixedSpeed)&&fixedSpeed<.35)fixedSpeed=0;
        if(Double.isFinite(f.bearing)&&Double.isFinite(fixedSpeed)&&fixedSpeed>.8)heading=Math.toRadians(f.bearing);
        else if(!first&&distance(anchorLat,anchorLon,f.lat,f.lon)>Math.max(5,f.accuracy+lastFix.accuracy))heading=bearing(anchorLat,anchorLon,f.lat,f.lon);
        speed=fixedSpeed;
        stationaryFixes=goodSpeed(f)&&f.speed<.3?stationaryFixes+1:0;
        boolean baroFresh=now-pressureTime<3000&&Double.isFinite(baro);
        boolean goodAlt=Double.isFinite(f.altitude)&&Double.isFinite(f.verticalAccuracy)&&f.verticalAccuracy<=10;
        if(goodAlt){
            if(baroFresh){double target=f.altitude-baro;altOffset=Double.isFinite(altOffset)&&!recovery?.98*altOffset+.02*target:target;alt=baro+altOffset;}
            else alt=f.altitude;
        }else if(!baroFresh)alt=Double.NaN;
        p.gpsElapsed=f.elapsed;p.gpsLat=f.lat;p.gpsLon=f.lon;p.gpsAccuracy=f.accuracy;p.gpsAltitude=f.altitude;p.gpsVerticalAccuracy=f.verticalAccuracy;p.mock=f.mock;
        ArrayList<Point> revisions=new ArrayList<>();
        if(!first){
            double before=pendingMeters;
            double[] residual=vector(p.lat,p.lon,f.lat,f.lon);
            double prevLat=anchorLat,prevLon=anchorLon,sum=0;
            boolean doppler=!recovery&&goodSpeed(f)&&goodSpeed(lastFix);
            double gpsMeters=doppler?(fixedSpeed+(lastFix.speed<.35?0:lastFix.speed))*.5*interval:recovery?distance(anchorLat,anchorLon,f.lat,f.lon):positionMoved?positionMeters:0;
            boolean stationaryNoise=recovery&&before<.01&&goodSpeed(f)&&goodSpeed(lastFix)&&fixedSpeed==0&&lastFix.speed<.35&&gpsMeters<=f.accuracy+lastFix.accuracy;
            double zResidual=goodAlt&&Double.isFinite(p.rawAltitude)?alt-p.rawAltitude:Double.NaN;
            for(Point q:pending){
                double fraction=clamp((q.elapsed-anchorTime)/1000.0/Math.max(.001,interval),0,1);
                if(!lostHistory&&(!recovery||(Double.isFinite(residual[0])&&Double.isFinite(q.rawLat)))){
                    double[] adjusted;
                    if(Double.isFinite(residual[0])&&Double.isFinite(q.rawLat))adjusted=offset(q.rawLat,q.rawLon,residual[0]*fraction,residual[1]*fraction);
                    else{double[] v=vector(anchorLat,anchorLon,f.lat,f.lon);adjusted=offset(anchorLat,anchorLon,v[0]*fraction,v[1]*fraction);}
                    q.lat=adjusted[0];q.lon=adjusted[1];
                    if(Double.isFinite(zResidual)&&Double.isFinite(q.rawAltitude))q.altitude=q.rawAltitude+zResidual*fraction;
                    q.meters=stationaryNoise?0:recovery?distance(prevLat,prevLon,q.lat,q.lon):gpsMeters*q.seconds/Math.max(.001,interval);
                    q.source=recovery?"corrected_estimate":doppler?"gps_speed":"gps_position";
                }else{q.source="unresolved_gap";}
                q.learnable=false;sum+=q.meters;q.totalMeters=baseMeters+sum;
                prevLat=q.lat;prevLon=q.lon;if(q!=p)revisions.add(q);
            }
            baseMeters+=sum;pendingMeters=0;
            if(recovery){if(!lostHistory){corrections++;correctedMeters+=sum-before;}estimatedMeters+=sum;gradeWindow.clear();}
            p.learnable=!recovery&&doppler&&f.accuracy<=15&&lastFix.accuracy<=15&&!f.mock&&!lastFix.mock&&p.meters>.2&&p.seconds>0&&p.seconds<=2.5;
        }else {p.source="gps_anchor";p.rawMeters=p.meters=0;p.rawLat=f.lat;p.rawLon=f.lon;}
        p.lat=lat=f.lat;p.lon=lon=f.lon;p.altitude=alt;p.speed=speed;p.bearing=heading;
        p.error=f.accuracy;p.totalMeters=baseMeters;
        if(first||recovery||(goodSpeed(f)&&goodSpeed(lastFix))||positionMoved){positionAnchorLat=f.lat;positionAnchorLon=f.lon;positionAnchorAccuracy=f.accuracy;positionAnchorTime=now;}
        lastFix=f;lastFixElapsed=f.elapsed;anchorLat=lat;anchorLon=lon;anchorTime=now;pending.clear();lostHistory=false;
        if(p.learnable&&Double.isFinite(p.altitude)&&(baroFresh||(goodAlt&&f.verticalAccuracy<=3))){
            while(!gradeWindow.isEmpty()&&(now-gradeWindow.peekFirst().elapsed>90000||p.totalMeters-gradeWindow.peekFirst().totalMeters>80))gradeWindow.removeFirst();
            Point old=gradeWindow.peekFirst();
            if(old!=null&&p.totalMeters-old.totalMeters>=(baroFresh?30:60)){
                double grade=(p.altitude-old.altitude)/(p.totalMeters-old.totalMeters)*100;
                if(Math.abs(grade)<=40)p.grade=grade;
            }
            gradeWindow.addLast(p);
        }else gradeWindow.clear();
        fixIssue=f.mock?"模擬GPS（学習対象外）":"unresolved_gap".equals(p.source)?"GPS復旧・中断区間は未確定":recovery?"GPS復旧・区間を再計算（経路は推定）":"GPS測位中";
        return new Update(p,revisions,recovery);
    }
    private double currentAltitude(long now){if(now-pressureTime<3000&&Double.isFinite(altOffset))return baro+altOffset;return lastFix!=null&&now-lastFixElapsed<3000?alt:Double.NaN;}
    public double totalMeters(){return baseMeters+pendingMeters;}
    public double provisionalMeters(){return pendingMeters;}
    public double estimatedTotal(){return estimatedMeters+pendingMeters;}
    public long gpsAge(long now){return lastFixElapsed<0?-1:Math.max(0,now-lastFixElapsed);}
    public static boolean goodSpeed(Fix f){return f!=null&&Double.isFinite(f.speed)&&f.speed>=0&&Double.isFinite(f.speedAccuracy)&&f.speedAccuracy>=0&&f.speedAccuracy<=1;}
    public static double clamp(double x,double lo,double hi){return Math.max(lo,Math.min(hi,x));}
    private static double wrap(double a){a%=2*Math.PI;return a<0?a+2*Math.PI:a;}
    public static double distance(double a,double b,double c,double d){
        double p=Math.toRadians(c-a),q=Math.toRadians(d-b);double x=Math.sin(p/2)*Math.sin(p/2)+Math.cos(Math.toRadians(a))*Math.cos(Math.toRadians(c))*Math.sin(q/2)*Math.sin(q/2);
        return 2*R*Math.asin(Math.sqrt(clamp(x,0,1)));
    }
    public static double[] offset(double lat,double lon,double east,double north){return new double[]{lat+Math.toDegrees(north/R),normalizeLon(lon+Math.toDegrees(east/(R*Math.cos(Math.toRadians(lat)))))};}
    private static double normalizeLon(double x){return ((x+540)%360)-180;}
    public static double[] vector(double a,double b,double c,double d){return new double[]{Math.toRadians(normalizeLon(d-b))*R*Math.cos(Math.toRadians((a+c)/2)),Math.toRadians(c-a)*R};}
    public static double bearing(double a,double b,double c,double d){double[] v=vector(a,b,c,d);return Math.atan2(v[0],v[1]);}
}
