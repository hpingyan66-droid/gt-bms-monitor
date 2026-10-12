package jp.k4159191.gtbms.prototype.core;
import java.util.Collections;
public final class Demo {
    public static Sample sample(long at,long elapsed,int tick){
        double[] cells=new double[16];double total=0;
        for(int i=0;i<16;i++){cells[i]=3.88+0.008*Math.sin(i+tick*.08);total+=cells[i];}
        return new Sample(at,elapsed,"demo","charge_positive",total,-7.5-1.5*Math.sin(tick*.15),82,
            41,50,7,cells,new double[]{25.2,26.8},true,true,0,0,41,Collections.emptyList());
    }
}
