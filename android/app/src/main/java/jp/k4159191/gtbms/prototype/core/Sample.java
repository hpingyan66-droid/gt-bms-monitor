package jp.k4159191.gtbms.prototype.core;

import java.util.*;

/** One complete 03/04 pair. No assumption about chemistry or current direction. */
public final class Sample {
    public final long receivedAt, receivedElapsed;
    public final String source, direction;
    public final double voltage, rawCurrent, soc, remainingAh, configuredAh;
    public final double[] cells, temperatures;
    public final int cycles, protection, version;
    public final long balancing;
    public final boolean chargeMos, dischargeMos;
    public final List<String> flags;
    public Sample(long at, long elapsed, String src, String dir, double v, double a, double soc,
                  double remaining, double configured, int cycles, double[] cells, double[] temps,
                  boolean cm, boolean dm, int protection, long balancing, int version, List<String> flags) {
        receivedAt=at; receivedElapsed=elapsed; source=src; direction=dir; voltage=v; rawCurrent=a;
        this.soc=soc; remainingAh=remaining; configuredAh=configured; this.cycles=cycles;
        this.cells=cells.clone(); temperatures=temps.clone(); chargeMos=cm; dischargeMos=dm;
        this.protection=protection; this.balancing=balancing; this.version=version;
        this.flags=Collections.unmodifiableList(new ArrayList<>(flags));
    }
    public Double dischargeCurrent() {
        if ("charge_positive".equals(direction)) return -rawCurrent;
        if ("discharge_positive".equals(direction)) return rawCurrent;
        return null;
    }
    /** Voltage times the original signed BMS current: no display sign conversion. */
    public double rawPower() { return voltage*rawCurrent; }
    /** Legacy calculation/CSV convention: positive discharge, negative incoming charge. */
    public Double power() { Double a=dischargeCurrent(); return a==null?null:voltage*a; }
    public double minCell() { double v=Double.POSITIVE_INFINITY; for(double x:cells)v=Math.min(v,x);return v; }
    public double maxCell() { double v=0; for(double x:cells)v=Math.max(v,x);return v; }
    public double deltaMv() { return (maxCell()-minCell())*1000; }
    public boolean fresh(long now) { return now>=receivedElapsed && now-receivedElapsed<=6000; }
    public String operatingState() {
        Double a=dischargeCurrent(); return a==null?"方向未確認":a>0.1?"放電":a< -0.1?"充電":"待機";
    }
}
