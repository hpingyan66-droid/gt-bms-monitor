import jp.k4159191.gtbms.prototype.core.*;
import java.util.*;

public final class CoreTests {
    static int checks=0;
    static void check(boolean b,String m){checks++;if(!b)throw new AssertionError(m);}
    static void rejects(Runnable r){boolean thrown=false;try{r.run();}catch(IllegalArgumentException e){thrown=true;}check(thrown,"should reject");}
    static byte[] hex(String s){byte[] b=new byte[s.length()/2];for(int i=0;i<b.length;i++)b[i]=(byte)Integer.parseInt(s.substring(i*2,i*2+2),16);return b;}
    static byte[] frame(int cmd,byte[] b){byte[] f=new byte[b.length+7];f[0]=(byte)221;f[1]=(byte)cmd;f[3]=(byte)b.length;System.arraycopy(b,0,f,4,b.length);int sum=b.length;for(byte x:b)sum+=x&255;int chk=(-sum)&65535;f[f.length-3]=(byte)(chk>>8);f[f.length-2]=(byte)chk;f[f.length-1]=119;return f;}
    static byte[] join(byte[]... parts){int n=0;for(byte[] b:parts)n+=b.length;byte[] r=new byte[n];int i=0;for(byte[] b:parts){System.arraycopy(b,0,r,i,b.length);i+=b.length;}return r;}
    public static void main(String[] args){
        check(Arrays.equals(JbdProtocol.request(3),hex("dda50300fffd77")),"03 request");check(Arrays.equals(JbdProtocol.request(4),hex("dda50400fffc77")),"04 request");
        for(int cmd=-1;cmd<257;cmd++){final int c=cmd;if(c!=3&&c!=4)rejects(()->JbdProtocol.request(c));}
        byte[] payload=hex("1a35fc18138813880007000000010000000029640310020ba50bb9"), cells=new byte[32];for(int i=0;i<16;i++){int v=i==15?4195:4193;cells[i*2]=(byte)(v>>8);cells[i*2+1]=(byte)v;}
        byte[] b=frame(3,payload),c=frame(4,cells);
        Sample s=JbdProtocol.parse(b,c,12345,1000,"ble","unconfirmed",16);
        check(s.voltage==67.09 && s.rawCurrent==-10,"units/signed current");check(s.soc==100&&s.cycles==7,"SOC/cycles");check(s.temperatures[0]==25&&s.temperatures[1]==27,"NTC units");
        check(s.power()==null&&s.dischargeCurrent()==null,"unknown direction");check(s.balancing==1&&s.chargeMos&&s.dischargeMos,"MOS and balance");check(Math.abs(s.deltaMv()-2)<1e-6,"cell spread");
        check(s.receivedAt==12345&&s.fresh(7000)&&!s.fresh(7001)&&!s.fresh(999),"monotonic freshness boundary");
        check(JbdProtocol.parse(b,c,0,0,"ble","charge_positive",16).dischargeCurrent()==10,"charge positive");check(JbdProtocol.parse(b,c,0,0,"ble","discharge_positive",16).dischargeCurrent()==-10,"discharge positive");
        Sample live=JbdProtocol.parseLive(b,c,0,0,16);
        check(live.rawCurrent==-10&&Math.abs(live.rawPower()+670.9)<1e-8,"live discharge preserves raw signs");
        check(live.direction.equals("charge_positive")&&live.operatingState().equals("放電")&&Math.abs(live.power()-670.9)<1e-8,"JBD discharge interpretation");
        byte[] chargePayload=payload.clone();chargePayload[2]=0;chargePayload[3]=100;
        Sample incoming=JbdProtocol.parseLive(frame(3,chargePayload),c,0,0,16);
        check(incoming.rawCurrent==1&&incoming.rawPower()==67.09&&incoming.operatingState().equals("充電"),"live incoming charge preserves positive signs");
        chargePayload[3]=0;Sample idle=JbdProtocol.parseLive(frame(3,chargePayload),c,0,0,16);
        check(idle.rawCurrent==0&&idle.rawPower()==0&&idle.operatingState().equals("待機"),"zero current remains idle");
        for(int index:new int[]{0,1,2,3,b.length-3,b.length-2,b.length-1}){byte[] bad=b.clone();bad[index]^=1;rejects(()->JbdProtocol.payload(bad,3));}
        rejects(()->JbdProtocol.parse(b,c,0,0,"ble","unknown",16));rejects(()->JbdProtocol.parse(b,frame(4,new byte[2]),0,0,"ble","unconfirmed",16));
        byte[] shortNtc=payload.clone();shortNtc[22]=16;rejects(()->JbdProtocol.parse(frame(3,shortNtc),c,0,0,"ble","unconfirmed",16));
        byte[] zero=cells.clone();zero[0]=zero[1]=0;Sample alarm=JbdProtocol.parse(b,frame(4,zero),0,0,"ble","unconfirmed",16);check(alarm.cells[0]==0&&!alarm.flags.isEmpty(),"preserve zero voltage");
        check(!JbdProtocol.parse(b,c,0,0,"ble","unconfirmed",17).flags.isEmpty(),"cell mismatch");
        for(int split=0;split<=b.length;split++){
            JbdProtocol.Decoder d=new JbdProtocol.Decoder();List<byte[]> frames=new ArrayList<>();frames.addAll(d.feed(join(new byte[]{1,2,3},Arrays.copyOf(b,split))));frames.addAll(d.feed(join(Arrays.copyOfRange(b,split,b.length),c)));
            check(frames.size()==2&&Arrays.equals(frames.get(0),b)&&Arrays.equals(frames.get(1),c),"all fragmentation splits");
        }
        JbdProtocol.Decoder d=new JbdProtocol.Decoder();byte[] broken=b.clone();broken[broken.length-2]^=1;List<byte[]> recovered=d.feed(join(broken,c));check(recovered.size()==1&&Arrays.equals(recovered.get(0),c),"resync after corruption");
        d.feed(new byte[5000]);check(d.buffered()==0,"bounded buffer");Random r=new Random(43);for(int i=0;i<2000;i++){byte[] x=new byte[r.nextInt(300)+1];r.nextBytes(x);for(byte[] f:d.feed(x))JbdProtocol.payload(f,0);check(d.buffered()<=4096,"fuzz bound");}
        Sample demo=Demo.sample(0,0,1);check(demo.source.equals("demo")&&demo.flags.isEmpty(),"explicit synthetic source");
        for(int api:new int[]{26,30,31,33,35,37}){
            check(BleDiagnostics.scanReady(api,true,true,true,true,true),"fully authorized scan API "+api);
            check(!BleDiagnostics.scanReady(api,true,true,false,true,true),"approximate-only location blocks full scan API "+api);
            check(!BleDiagnostics.scanReady(api,true,true,true,false,true),"location switch off API "+api);
            check(!BleDiagnostics.scanReady(api,true,true,true,true,false),"Bluetooth off API "+api);
        }
        check(BleDiagnostics.scanReady(30,false,false,true,true,true),"legacy Bluetooth uses install permissions");
        check(!BleDiagnostics.scanReady(31,false,true,true,true,true),"missing scan permission");
        check(!BleDiagnostics.scanReady(37,true,false,true,true,true),"missing connect permission");
        check(!BleDiagnostics.notificationRequired(32)&&BleDiagnostics.notificationRequired(33),"notification API boundary");
        check(BleDiagnostics.writeMode(12,"auto")==2,"automatic acknowledged write when both present");
        check(BleDiagnostics.writeMode(12,"without_response")==1,"explicit compatible write without ack");
        check(BleDiagnostics.writeMode(4,"auto")==1,"no-response-only device");
        check(BleDiagnostics.writeMode(4,"with_response")==0,"reject unsupported acknowledged mode");
        check(BleDiagnostics.writeMode(8,"without_response")==0,"reject unsupported unacknowledged mode");
        check(BleDiagnostics.writeMode(0,"auto")==0,"unwritable characteristic");
        check(BleDiagnostics.subscription(16)==1&&BleDiagnostics.subscription(32)==2,"notify and indication-only peripherals");
        check(BleDiagnostics.subscription(0)==0,"reject no subscription");
        check(BleDiagnostics.scanError(6).contains("30秒"),"scan throttle guidance");
        check(BleDiagnostics.gattStatus(133).contains("GATT"),"GATT generic error remains distinct");
        System.out.println("PASS: "+checks+" assertions (synthetic vectors, no hardware)");
    }
}
