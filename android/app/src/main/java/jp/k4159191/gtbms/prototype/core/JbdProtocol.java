package jp.k4159191.gtbms.prototype.core;

import java.util.*;

/** Port of the user's GT BMS Windows read-only protocol.py (MIT). */
public final class JbdProtocol {
    // JBD universal protocol, 0x03: signed current; charge positive, discharge negative.
    public static final String CURRENT_DIRECTION="charge_positive";
    public static final UUID RX=UUID.fromString("0000ff01-0000-1000-8000-00805f9b34fb");
    public static final UUID TX=UUID.fromString("0000ff02-0000-1000-8000-00805f9b34fb");
    private static int u(byte b){return b&255;}
    private static int u16(byte[] b,int n){return u(b[n])*256+u(b[n+1]);}
    public static byte[] request(int command) {
        if(command!=3 && command!=4)throw new IllegalArgumentException("読み取り03/04のみ対応");
        int check=(-command)&65535;
        return new byte[]{(byte)0xdd,(byte)0xa5,(byte)command,0,(byte)(check>>8),(byte)check,0x77};
    }
    public static byte[] payload(byte[] f,int expected) {
        if(f==null || f.length<7 || u(f[0])!=221 || u(f[f.length-1])!=119)throw new IllegalArgumentException("フレーム境界不正");
        int cmd=u(f[1]);
        if((cmd!=3 && cmd!=4) || (expected!=0 && cmd!=expected))throw new IllegalArgumentException("応答コマンド不一致");
        if(f.length!=u(f[3])+7 || f[2]!=0)throw new IllegalArgumentException("応答長/ステータス不正");
        int sum=0;for(int i=2;i<f.length-3;i++)sum+=u(f[i]);
        if(u16(f,f.length-3)!=((-sum)&65535))throw new IllegalArgumentException("チェックサム不一致");
        return Arrays.copyOfRange(f,4,f.length-3);
    }
    public static Sample parseLive(byte[] basic,byte[] cells,long at,long elapsed,int expectedCells) {
        return parse(basic,cells,at,elapsed,"ble",CURRENT_DIRECTION,expectedCells);
    }
    /** Explicit direction is retained for legacy records and test fixtures, not live preferences. */
    public static Sample parse(byte[] basic,byte[] cells,long at,long elapsed,String source,String direction,int expectedCells) {
        if(!Arrays.asList("unconfirmed","charge_positive","discharge_positive").contains(direction))throw new IllegalArgumentException("電流方向不正");
        if(expectedCells<1 || expectedCells>32)throw new IllegalArgumentException("想定セル数不正");
        byte[] b=payload(basic,3), c=payload(cells,4);
        if(b.length<23)throw new IllegalArgumentException("基本情報が不足");
        int n=u(b[21]),nt=u(b[22]);
        if(n<1 || n>32 || nt>16 || b.length<23+2*nt || c.length!=2*n)throw new IllegalArgumentException("セル/温度数不一致");
        double[] v=new double[n],t=new double[nt]; double sum=0;
        List<String> flags=new ArrayList<>();
        if(n!=expectedCells)flags.add("想定セル数と不一致");
        for(int i=0;i<n;i++){v[i]=u16(c,2*i)/1000.0;sum+=v[i];}
        for(double x:v)if(x<=0 || x>5){flags.add("セル電圧が解析範囲外");break;}
        for(int i=0;i<nt;i++)t[i]=(u16(b,23+2*i)-2731)/10.0;
        for(double x:t)if(x< -60 || x>150){flags.add("温度が解析範囲外");break;}
        if(u(b[19])>100)flags.add("残量が範囲外");
        double pack=u16(b,0)/100.0;
        if(Math.abs(pack-sum)>Math.max(1,pack*.03))flags.add("総電圧とセル合計が不一致");
        if(u16(b,16)!=0)flags.add("BMS保護フラグあり");
        return new Sample(at,elapsed,source,direction,pack,((short)u16(b,2))/100.0,u(b[19]),
            u16(b,4)/100.0,u16(b,6)/100.0,u16(b,8),v,t,(b[20]&1)!=0,(b[20]&2)!=0,
            u16(b,16),(long)u16(b,12)|((long)u16(b,14)<<16),u(b[18]),flags);
    }
    public static final class Decoder {
        private byte[] buffer=new byte[0];
        public int invalidFrames;
        public int buffered(){return buffer.length;}
        public List<byte[]> feed(byte[] data) {
            List<byte[]> frames=new ArrayList<>();
            if(data.length+buffer.length>4096){buffer=new byte[0];invalidFrames++;return frames;}
            int old=buffer.length; buffer=Arrays.copyOf(buffer,old+data.length);System.arraycopy(data,0,buffer,old,data.length);
            while(buffer.length>0) {
                int pos=0;while(pos<buffer.length && u(buffer[pos])!=221)pos++;
                if(pos>0)buffer=Arrays.copyOfRange(buffer,pos,buffer.length);
                if(buffer.length<4)break;
                int n=u(buffer[3])+7;if(buffer.length<n)break;
                byte[] f=Arrays.copyOf(buffer,n);
                try {payload(f,0);} catch(IllegalArgumentException e){invalidFrames++;buffer=Arrays.copyOfRange(buffer,1,buffer.length);continue;}
                frames.add(f);buffer=Arrays.copyOfRange(buffer,n,buffer.length);
            }
            return frames;
        }
    }
    private JbdProtocol(){}
}
