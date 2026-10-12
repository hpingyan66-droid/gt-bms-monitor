import jp.k4159191.gtbms.prototype.core.*;
import java.util.*;

public final class TripCoreTests {
    static int checks;
    static void check(boolean b,String m){checks++;if(!b)throw new AssertionError(m);}
    static void near(double a,double b,double tolerance,String m){check(Double.isFinite(a)&&Math.abs(a-b)<=tolerance,m+": "+a+" != "+b);}
    static TripEngine.Fix fix(long t,double east,double north,double speed){TripEngine.Fix f=new TripEngine.Fix();double[] v=TripEngine.offset(35,139,east,north);f.lat=v[0];f.lon=v[1];f.elapsed=t;f.accuracy=3;f.speed=speed;f.speedAccuracy=.1;f.bearing=90;f.altitude=20;f.verticalAccuracy=2;return f;}
    static TripEngine.Update gps(TripEngine e,long t,double east,double north,double speed){check(e.offer(fix(t,east,north,speed),t),"accept fixture");return e.step(t,1700000000000L+t);}
    static Sample bms(long t,double amps,String source,String direction){return new Sample(1700000000000L+t,t,source,direction,60,amps,80,40,50,7,new double[]{3.9},new double[]{20},true,true,0,0,1,Collections.emptyList());}
    static TripEngine.Point drive(long t){TripEngine.Point p=new TripEngine.Point();p.elapsed=t;p.seconds=1;p.speed=5;p.meters=5;p.learnable=true;return p;}
    public static void main(String[] args){
        TripEngine waiting=new TripEngine(40,true);for(int i=1;i<=5;i++){waiting.imu(i*1000,1,0,0);check(waiting.step(i*1000,0).point.source.equals("waiting_gps"),"no fictitious anchor");}near(waiting.totalMeters(),0,0,"no distance before fix");
        TripEngine stationary=new TripEngine(40,true);gps(stationary,1000,0,0,0);
        for(int i=2;i<=60;i++)gps(stationary,i*1000,(i%2==0?1:-1)*2,0,0);
        near(stationary.totalMeters(),0,1e-8,"stationary GPS noise not accumulated");
        for(int i=61;i<70;i++)stationary.step(i*1000,0);
        gps(stationary,70000,1,0,0);near(stationary.totalMeters(),0,1e-8,"stationary GPS return does not create distance");
        TripEngine positionOnly=new TripEngine(40,false);
        for(int i=0;i<=100;i++){TripEngine.Fix f=fix(1000+i*1000,i,0,1);f.speed=f.speedAccuracy=Double.NaN;positionOnly.offer(f,f.elapsed);positionOnly.step(f.elapsed,0);}
        check(positionOnly.totalMeters()>90&&positionOnly.totalMeters()<=101,"position-only low-speed movement accumulated beyond noise gate");
        TripEngine straight=new TripEngine(40,false);gps(straight,1000,0,0,5);
        for(int i=2;i<=101;i++)gps(straight,i*1000,(i-1)*5,0,5);
        near(straight.totalMeters(),500,.001,"Doppler integration over 100s");
        check(!straight.offer(fix(102000,10000,0,5),102000),"teleport rejected");
        check(!straight.offer(fix(100000,495,0,5),102000),"out of order rejected");
        TripEngine.Fix weak=fix(102000,505,0,5);weak.accuracy=100;check(!straight.offer(weak,102000),"poor accuracy rejected");
        check(!straight.offer(fix(102000,505,0,5),110000),"cached fix rejected");
        check(!straight.offer(fix(200000,505,0,5),110000),"future fix rejected");
        TripEngine recovery=new TripEngine(40,false);gps(recovery,1000,0,0,5);gps(recovery,2000,5,0,5);
        for(int i=3;i<=11;i++)recovery.step(i*1000,0);
        double original=recovery.totalMeters();near(original,50,.01,"provisional distance before return");
        TripEngine.Update corrected=gps(recovery,12000,70,0,5);
        check(corrected.recovered&&corrected.revisions.size()==9,"all provisional points revised");
        near(recovery.totalMeters(),70,.02,"distance recalculated to recovered endpoint");
        near(corrected.revisions.get(0).rawMeters,5,1e-5,"original interval retained");
        check(corrected.revisions.get(0).meters>5,"corrected interval separate from raw");
        near(corrected.point.totalMeters,recovery.totalMeters(),1e-8,"consistent final cumulative distance");
        check(!corrected.point.learnable&&corrected.point.source.equals("corrected_estimate"),"recovery never enters learning");
        gps(recovery,13000,75,0,5);near(recovery.totalMeters(),75,.02,"next interval not double counted");
        TripEngine noHeading=new TripEngine(40,true);TripEngine.Fix nh=fix(1000,0,0,0);nh.bearing=Double.NaN;noHeading.offer(nh,1000);noHeading.step(1000,0);
        nh=fix(2000,0,0,0);nh.bearing=Double.NaN;noHeading.offer(nh,2000);near(noHeading.step(2000,0).point.meters,0,0,"stationary without bearing");
        TripEngine imu=new TripEngine(40,true);gps(imu,1000,0,0,3);
        for(int i=0;i<=150;i++)imu.imu(2500+i*20,1,0,0);
        TripEngine.Point accelerated=imu.step(5600,0).point;
        check(accelerated.speed>5&&accelerated.speed<7,"projected acceleration increases eastbound speed");
        // Same IMU input is ignored when the phone is not declared fixed to the vehicle.
        TripEngine loose=new TripEngine(40,false);gps(loose,1000,0,0,3);for(int i=0;i<=150;i++)loose.imu(2500+i*20,1,0,0);
        near(loose.step(5600,0).point.speed,3,1e-8,"mount gate");
        TripEngine turn=new TripEngine(40,true);gps(turn,1000,0,0,5);
        for(int i=0;i<=100;i++)turn.imu(2500+i*20,0,0,Math.PI/4);
        near(turn.step(4510,0).point.bearing,0,.03,"gyro sign: east to north");
        TripEngine loop=new TripEngine(40,true);gps(loop,1000,0,0,5);
        for(int sec=2;sec<=23;sec++){for(int i=0;i<50;i++)loop.imu((sec-1)*1000+i*20,0,0,Math.PI/10);loop.step(sec*1000,0);}
        TripEngine.Update loopEnd=gps(loop,24000,0,0,5);check(loopEnd.recovered&&loop.totalMeters()>70,"closed-loop recovery preserves a curved path, not just endpoint chord");
        TripEngine longGap=new TripEngine(40,true);gps(longGap,1000,0,0,5);longGap.step(62000,0);
        for(int i=0;i<100;i++)longGap.imu(62100+i*20,2,0,1);
        TripEngine.Point longPoint=longGap.step(65000,0).point;near(longPoint.speed,5,1e-8,"IMU integration disabled after 60s");check(longPoint.error>500,"uncertainty grows across outage");
        TripEngine clock=new TripEngine(40,false);gps(clock,1000,0,0,5);near(clock.step(31000,0).point.meters,0,0,"process stall not silently bridged");
        boolean throwsBack=false;try{clock.step(30000,0);}catch(IllegalArgumentException ex){throwsBack=true;}check(throwsBack,"backwards clock rejected");
        TripEngine altitude=new TripEngine(40,false);altitude.pressure(1000,1013.25);gps(altitude,1000,0,0,5);
        for(int i=0;i<80;i++)altitude.pressure(1100+i*20,1012.05);
        check(altitude.step(2800,0).point.altitude>28,"relative barometric ascent anchored to GPS");
        near(TripEngine.distance(0,179.999,0,-179.999),222.39,.1,"dateline distance");
        EnergyModel m=new EnergyModel();for(int i=1;i<=81;i++)m.update(drive(i*1000),bms(i*1000,10,"ble","discharge_positive"),true,"a",i*1000);
        near(m.consumedWh,600*80/3600.0,1e-6,"trapezoid energy integration");check(m.bins[0].ready()&&m.bins[2].ready(),"learned speed-specific bin");near(m.bins[0].mean,600/18.0,.001,"learned Wh/km");
        EnergyModel.Forecast f=m.forecast(bms(81000,10,"ble","discharge_positive"),true,81000,18,10,Double.NaN,true);
        check(f.available,"learned forecast available");near(f.km,63,.01,"reserve energy / learned consumption");near(f.reserveHours,3.5,.01,"runtime to reserve");near(f.emptyHours,4,.01,"reference runtime to zero");check(f.lowKm<f.km&&f.highKm>f.km,"prediction spread");
        check(!m.forecast(bms(0,10,"ble","discharge_positive"),true,81000,18,10,20,true).available,"stale battery blocks forecast");
        check(!m.forecast(bms(81000,10,"demo","discharge_positive"),true,81000,18,10,20,true).available,"demo excluded");
        check(!m.forecast(bms(81000,10,"ble","discharge_positive"),true,81000,18,10,20,false).available,"sign confirmation required");
        check(Double.isNaN(m.forecast(bms(81000,10,"ble","discharge_positive"),true,81000,0,10,20,true).reserveHours),"stationary runtime not infinite");
        EnergyModel empty=new EnergyModel();check(!empty.forecast(bms(1,10,"ble","discharge_positive"),true,1,18,10,Double.NaN,true).available,"no invented training result");
        check(empty.forecast(bms(1,10,"ble","discharge_positive"),true,1,18,10,25,true).basis.contains("手入力"),"explicit manual fallback");
        for(String source:new String[]{"demo","ble"}){EnergyModel invalid=new EnergyModel();for(int i=1;i<100;i++)invalid.update(drive(i*1000),bms(i*1000,10,source,"unconfirmed"),true,"a",i*1000);check(invalid.bins[0].windows==0&&invalid.consumedWh==0,"unconfirmed current never trains");}
        EnergyModel estimated=new EnergyModel();for(int i=1;i<100;i++){TripEngine.Point p=drive(i*1000);p.learnable=false;estimated.update(p,bms(i*1000,10,"ble","discharge_positive"),true,"a",i*1000);}check(estimated.bins[0].windows==0&&estimated.consumedWh>0,"energy retained but estimated distance excluded from learning");
        EnergyModel charging=new EnergyModel();for(int i=1;i<100;i++)charging.update(drive(i*1000),bms(i*1000,-10,"ble","discharge_positive"),true,"a",i*1000);check(charging.bins[0].windows==0&&charging.regenWh>0,"regen separated and not trained as traction");
        EnergyModel crossing=new EnergyModel();
        crossing.update(drive(1000),bms(1000,-10,"ble",JbdProtocol.CURRENT_DIRECTION),true,"jbd",1000);
        crossing.update(drive(2000),bms(2000,10,"ble",JbdProtocol.CURRENT_DIRECTION),true,"jbd",2000);
        near(crossing.consumedWh,150/3600.0,1e-10,"zero-crossing preserves discharge triangle");
        near(crossing.regenWh,150/3600.0,1e-10,"zero-crossing preserves incoming-charge triangle");
        near(crossing.eligibleMeters(),0,0,"mixed charging interval excluded from discharge-only learning");
        crossing.update(drive(3000),bms(3000,-20,"ble",JbdProtocol.CURRENT_DIRECTION),true,"jbd",3000);
        near(crossing.consumedWh,(150+400)/3600.0,1e-10,"asymmetric reverse crossing discharge");
        near(crossing.regenWh,(150+100)/3600.0,1e-10,"asymmetric reverse crossing charge");
        EnergyModel nativeDischarge=new EnergyModel();for(int i=1;i<=81;i++)nativeDischarge.update(drive(i*1000),bms(i*1000,-10,"ble",JbdProtocol.CURRENT_DIRECTION),true,"jbd",i*1000);
        check(nativeDischarge.bins[0].ready()&&nativeDischarge.regenWh==0,"native negative discharge learns");
        near(nativeDischarge.bins[0].mean,m.bins[0].mean,1e-8,"learned rate independent of display sign convention");
        EnergyModel sparse=new EnergyModel();for(int i=1;i<=120;i++){TripEngine.Point p=drive(i*1000);if(i%3==0)p.learnable=false;sparse.update(p,bms(i*1000,10,"ble","discharge_positive"),true,"a",i*1000);}check(sparse.bins[0].ready(),"valid subintervals can accumulate across excluded GPS gaps");near(sparse.bins[0].mean,600/18.0,.001,"excluded distances and energy do not enter sparse learning");
        EnergyModel mock=new EnergyModel();for(int i=1;i<100;i++){TripEngine.Point p=drive(i*1000);p.mock=true;mock.update(p,bms(i*1000,10,"ble","discharge_positive"),true,"a",i*1000);}check(mock.bins[0].windows==0,"mock locations independently excluded by learning");
        EnergyModel boundary=new EnergyModel();boundary.update(drive(1000),bms(1000,10,"ble","discharge_positive"),true,"a",1000);boundary.update(drive(2000),bms(2000,10,"ble","discharge_positive"),true,"b",2000);near(boundary.consumedWh,0,0,"no integration across device switch");
        boundary.update(drive(20000),bms(20000,10,"ble","discharge_positive"),true,"b",20000);near(boundary.consumedWh,0,0,"no integration across scheduler gap");
        double hill=EnergyModel.climbGrade(150,1000,.75,5,.015,.6);check(hill>30&&hill<45,"steady-state climb power balance");check(EnergyModel.climbGrade(300,1000,.75,5,.015,.6)<hill,"heavier vehicle climbs less");
        check(Double.isNaN(EnergyModel.climbGrade(Double.NaN,1000,.75,5,.015,.6)),"missing mass gives no claim");check(Double.isNaN(EnergyModel.climbGrade(150,1000,.75,0,.015,.6)),"zero-speed formula rejected");near(EnergyModel.climbGrade(150,50,.2,20,.015,.6),0,1e-8,"insufficient power");
        System.out.println("PASS: "+checks+" trip/energy assertions (synthetic sensors; no physical route validation)");
    }
}
