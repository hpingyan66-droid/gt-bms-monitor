package jp.k4159191.gtbms.prototype.core;

/** Local, auditable learning. Only fresh live BLE + reliable GNSS intervals enter training. */
public final class EnergyModel {
    public static final class Bin {
        public double meters,wh,seconds,mean,variance;
        public int windows;
        public boolean ready(){return meters>=300&&seconds>=60&&windows>=3&&mean>1;}
    }
    public final Bin[] bins={new Bin(),new Bin(),new Bin(),new Bin()}; // overall, <10, 10-20, >=20 km/h
    public double consumedWh,regenWh,coveredSeconds,uncoveredSeconds;
    private double windowMeters,windowWh,windowSeconds,windowSpeed;
    private Sample previous;
    private String previousKey="";
    private String windowKey="";
    private long previousTick=-1;
    public String reason="BMS接続と電流方向の確認が必要です";

    public void update(TripEngine.Point point,Sample sample,boolean monitoring,String key,long now){
        double dt=previousTick<0?0:(now-previousTick)/1000.0;previousTick=now;
        if(key!=null&&!key.isEmpty()&&!key.equals(windowKey)){clearWindow();windowKey=key;}
        boolean live=monitoring&&sample!=null&&"ble".equals(sample.source)&&sample.fresh(now)
            &&sample.power()!=null&&Double.isFinite(sample.power())&&sample.flags.isEmpty()
            &&sample.protection==0&&sample.dischargeMos&&key!=null&&!key.isEmpty();
        boolean pair=live&&previous!=null&&previous.fresh(now)&&key.equals(previousKey)
            &&sample.receivedElapsed>=previous.receivedElapsed&&dt>0&&dt<=2.5;
        if(pair){
            double before=previous.power(),after=sample.power(),watts=(before+after)*.5;
            // Split a linearly interpolated interval at zero: never cancel charge against discharge.
            consumedWh+=positiveMean(before,after)*dt/3600;regenWh+=positiveMean(-before,-after)*dt/3600;coveredSeconds+=dt;
            boolean charging=before<0||after<0;
            boolean usable=point.learnable&&!point.mock&&point.speed>.7&&!charging&&Math.abs(point.seconds-dt)<.25;
            if(usable){
                windowMeters+=point.meters;windowWh+=watts*dt/3600;windowSeconds+=dt;windowSpeed+=point.speed*dt;
                reason="走行データを学習中";
                if(windowMeters>=100&&windowSeconds>=15){
                    double rate=windowWh/windowMeters*1000;
                    if(rate>=1&&rate<=1000){int b=speedBin(windowSpeed/windowSeconds*3.6);train(bins[0],rate,windowMeters,windowWh,windowSeconds);train(bins[b],rate,windowMeters,windowWh,windowSeconds);}
                    clearWindow();
                }
            }else{reason=charging?"充電・回生を含む区間は航続学習を保留":point.mock?"模擬GPSは学習対象外":!point.learnable?"良好なGPS走行区間を待っています":"走行開始を待っています";}
        }else{
            if(dt>0)uncoveredSeconds+=dt;
            reason=!live?"BMS鮮度・電流方向・保護状態を確認してください":"連続した有効データを待っています";
        }
        previous=live?sample:null;previousKey=live?key:"";
    }
    private static double positiveMean(double a,double b){
        if(a>=0&&b>=0)return (a+b)*.5;
        if(a<=0&&b<=0)return 0;
        double positive=Math.max(a,b);return positive*positive/(2*Math.abs(b-a));
    }
    private void clearWindow(){windowMeters=windowWh=windowSeconds=windowSpeed=0;}
    public static int speedBin(double kmh){return kmh<10?1:kmh<20?2:3;}
    private static void train(Bin b,double rate,double m,double wh,double sec){
        double alpha=b.windows==0?1:.15,delta=rate-b.mean;
        b.mean+=alpha*delta;b.variance=(1-alpha)*(b.variance+alpha*delta*delta);
        b.meters+=m;b.wh+=wh;b.seconds+=sec;b.windows++;
    }
    public void resetIntegration(){previous=null;previousKey="";clearWindow();}
    public double eligibleMeters(){return bins[0].meters+windowMeters;}
    public double eligibleSeconds(){return bins[0].seconds+windowSeconds;}
    public static final class Forecast {
        public boolean available;
        public String basis="学習中",reason="";
        public double whPerKm=Double.NaN,km=Double.NaN,lowKm=Double.NaN,highKm=Double.NaN;
        public double reserveHours=Double.NaN,emptyHours=Double.NaN,usableWh=Double.NaN,reserve;
    }
    public Forecast forecast(Sample s,boolean monitoring,long now,double kmh,double reserve,double manualRate,boolean signConfirmed){
        Forecast f=new Forecast();f.reserve=reserve;
        if(!signConfirmed){f.reason="放電時の電流方向を確認し、走行設定で確認済みにしてください";return f;}
        if(!monitoring||s==null||!"ble".equals(s.source)||!s.fresh(now)||s.power()==null||s.power()<0||!s.flags.isEmpty()||s.protection!=0||!s.dischargeMos){f.reason="有効なBMS放電データ待ち";return f;}
        if(s.configuredAh<=0||s.remainingAh<0||s.remainingAh>s.configuredAh*1.05||s.voltage<=0||reserve<0||reserve>40){f.reason="容量・電圧・予備残量の設定を確認してください";return f;}
        Bin b=Double.isFinite(kmh)?bins[speedBin(kmh)]:bins[0];
        if(!b.ready())b=bins[0];double spread=.35;
        if(b.ready()){f.whPerKm=b.mean;spread=Math.max(.2,Math.min(.8,2*Math.sqrt(b.variance)/b.mean));f.basis=b==bins[0]?"全走行の学習値":"速度帯の学習値";}
        else if(Double.isFinite(manualRate)&&manualRate>=1&&manualRate<=1000){f.whPerKm=manualRate;f.basis="手入力の電費（未学習）";}
        else{f.reason="良好なGPSで300m以上・60秒以上の放電走行を記録してください";return f;}
        // Present terminal voltage is an approximation, not an identified battery discharge curve.
        double remaining=Math.min(s.remainingAh,s.configuredAh*s.soc/100.0);
        double energy=remaining*s.voltage;
        f.usableWh=Math.max(0,remaining-s.configuredAh*reserve/100)*s.voltage;
        f.km=f.usableWh/f.whPerKm;f.lowKm=f.usableWh/(f.whPerKm*(1+spread));f.highKm=f.usableWh/(f.whPerKm*(1-spread));
        if(Double.isFinite(kmh)&&kmh>=2){f.reserveHours=f.km/kmh;f.emptyHours=energy/(f.whPerKm*kmh);}
        f.available=true;return f;
    }
    /** Steady climb power balance; not a traction, torque, stability or manufacturer limit. */
    public static double climbGrade(double totalKg,double electricW,double efficiency,double speedKmh,double crr,double cda){
        if(!Double.isFinite(totalKg)||!Double.isFinite(electricW)||!Double.isFinite(efficiency)||!Double.isFinite(speedKmh)
            ||!Double.isFinite(crr)||!Double.isFinite(cda)||totalKg<30||totalKg>1000||electricW<50||electricW>20000
            ||efficiency<.2||efficiency>1||speedKmh<2||speedKmh>80||crr<0||crr>.1||cda<0||cda>3)return Double.NaN;
        double v=speedKmh/3.6,force=electricW*efficiency/v-.5*1.225*cda*v*v;
        if(force<=totalKg*9.80665*crr)return 0;
        double lo=0,hi=Math.atan(.6);
        for(int i=0;i<60;i++){double mid=(lo+hi)/2,need=totalKg*9.80665*(Math.sin(mid)+crr*Math.cos(mid));if(need>force)hi=mid;else lo=mid;}
        return Math.tan(lo)*100;
    }
}
