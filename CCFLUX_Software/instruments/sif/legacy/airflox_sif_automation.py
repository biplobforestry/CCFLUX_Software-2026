#!/usr/bin/env python
from __future__ import annotations
import argparse,csv,math,shutil,struct,sys
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
from pathlib import Path
import numpy as np
import pandas as pd
from noseboom_gimbal_for_sif import prepare_sif_log_from_hatchbox, yes_no

DEFAULT_ESSENTIALS=Path(r"C:\My_PC\Zeppelin\3_Quick_look\SIF\SIF_Essentials")
DEFAULT_OUTPUT=Path(r"C:\My_PC\Zeppelin\3_Quick_look\SIF")
RUN_FROM_SPYDER=True
SPYDER_DIRECTORY=Path("D:\\")
SPYDER_FLIGHT_NAME="Flight_2124"
SPYDER_OUTPUT_DIR=DEFAULT_OUTPUT
SPYDER_ESSENTIALS_DIR=DEFAULT_ESSENTIALS
SPYDER_LOG=None
SPYDER_ALTITUDE_FILTER="no"
SPYDER_APPLY_NONLINEARITY_CORRECTION="no"
SPYDER_SPECTRAL_SHIFT_CORRECTION="no"
SPYDER_RAW_MIN_KB=100
SPYDER_TIME_FILTER="default"
SPYDER_TIME_START_UTC=None
SPYDER_TIME_END_UTC=None
SPYDER_PLATFORM_MODE="uav_airship"
SPYDER_STATIC_LAT=None
SPYDER_STATIC_LON=None
SPYDER_STATIC_ALT=None

@dataclass
class AirFloXRaw:
    e:np.ndarray; dc_e:np.ndarray; e2:np.ndarray; l:np.ndarray; dc_l:np.ndarray
    it_e_ms:np.ndarray; it_l_ms:np.ndarray; date:list[str]; time:list[str]
    temp1:np.ndarray; humidity:np.ndarray; gps_time:list[str]; gps_date:list[str]
    gps_lat:list[str]; gps_lon:list[str]; cpu1:np.ndarray; cpu2:np.ndarray

def read_semicolon_csv(p):
    rows=[]; max_cols=0
    with Path(p).open('r',encoding='utf-8-sig',errors='replace',newline='') as fh:
        for row in csv.reader(fh,delimiter=';'):
            rows.append(row); max_cols=max(max_cols,len(row))
    if not rows: return pd.DataFrame()
    return pd.DataFrame([r+['']*(max_cols-len(r)) for r in rows],dtype=str).replace('#N/D',np.nan)

def numeric_frame(f): return f.apply(pd.to_numeric,errors='coerce')
def header_value_positions(d):
    h=[str(x) for x in d.iloc[0].tolist()]
    def pos(a,b=None):
        if a in h: return h.index(a)+1
        if b and b in h: return h.index(b)+1
        raise KeyError(a)
    return {'it_e':pos('IT_WR[us]='),'it_l':pos('IT_VEG[us]='),'temp1':pos('mainboard_temp[C]=','outside_temp[C]='),'humidity':pos('mainboard_humidity=','relative_humidity='),'gps_time':pos('GPS_TIME_UTC='),'gps_date':pos('GPS_date='),'gps_lat':pos('GPS_lat='),'gps_lon':pos('GPS_lon='),'cpu1':pos('gps_CPU='),'cpu2':pos('veg_CPU=')}

def repair_raw_blocks(r,source=None):
    """Bring block metadata to a state the calculation can rely on.

    A block whose spectra are complete can still carry unusable metadata: the
    AirFloX writes each block as it goes, so an interrupted write leaves the
    tail fields blank or half-formed. Flight_CC0807 has three such blocks in
    1 310, and left alone each one broke a different calculation downstream - a
    missing integration offset ended the run outright, and a coordinate that
    parsed as latitude 2495 was copied over all 1 307 rows that held no
    position, putting the exported solar zenith angle at 156 degrees.

    Doing it here, once, is what keeps that from being three separate defences
    that each have to think about corrupt input. Past this point every metadata
    field is either usable or unambiguously absent.

    Nothing is dropped: the spectra of these blocks are good, and a block is
    worth more than the field it is missing. What cannot be trusted is cleared,
    so a later step reads "absent" instead of a plausible wrong number.
    """
    count=len(r.date); notes=[]

    # Coordinates. Off the globe is not a position, and neither is a value that
    # will not parse; both must read as absent rather than as somewhere.
    lat=parse_gps_coord(r.gps_lat,'N','S'); lon=parse_gps_coord(r.gps_lon,'E','W')
    unusable=(np.abs(lat)>90)|(np.abs(lon)>180)
    written=np.array([bool(str(v).strip()) for v in r.gps_lat],dtype=bool)
    unusable|=written&(~np.isfinite(lat)|~np.isfinite(lon))
    if unusable.any():
        r.gps_lat=['' if bad else v for v,bad in zip(r.gps_lat,unusable)]
        r.gps_lon=['' if bad else v for v,bad in zip(r.gps_lon,unusable)]
        notes.append(f'{int(unusable.sum())} coordinate pair(s) were not a position on '
                     'the globe and are read as no fix')

    # The two CPU stamps give the sub-second gap between the GPS read and the
    # spectrum. One without the other says nothing, so neither is kept.
    cpu1=np.asarray(r.cpu1,dtype=float); cpu2=np.asarray(r.cpu2,dtype=float)
    half=np.isfinite(cpu1)^np.isfinite(cpu2)
    if half.any():
        cpu1[half]=np.nan; cpu2[half]=np.nan
        r.cpu1=cpu1; r.cpu2=cpu2
        notes.append(f'{int(half.sum())} block(s) carried one CPU stamp without the other')

    # Integration time divides the counts. Zero or negative is not a shutter,
    # and silently produced an infinity where a NaN says "not measured".
    for field,label in (('it_e_ms','incoming'),('it_l_ms','reflected')):
        values=np.asarray(getattr(r,field),dtype=float)
        impossible=np.isfinite(values)&(values<=0)
        if impossible.any():
            values[impossible]=np.nan
            setattr(r,field,values)
            notes.append(f'{int(impossible.sum())} block(s) reported a {label} integration '
                         'time of zero or less')

    if notes:
        where=f' in {Path(source).name}' if source else ''
        print(f'warning=Repaired the metadata of incomplete AirFloX block(s){where}, out of '
              f'{count}: '+'; '.join(notes)+'. The spectra themselves are unchanged and '
              'those blocks are kept.')
    return r

def filter_raw(r,k):
    return AirFloXRaw(r.e[:,k],r.dc_e[:,k],r.e2[:,k],r.l[:,k],r.dc_l[:,k],r.it_e_ms[k],r.it_l_ms[k],[v for v,x in zip(r.date,k) if x],[v for v,x in zip(r.time,k) if x],r.temp1[k],r.humidity[k],[v for v,x in zip(r.gps_time,k) if x],[v for v,x in zip(r.gps_date,k) if x],[v for v,x in zip(r.gps_lat,k) if x],[v for v,x in zip(r.gps_lon,k) if x],r.cpu1[k],r.cpu2[k])

def read_drox_full(p,n,drop_e500_zero=False,drop_zero_gps=True):
    d=read_semicolon_csv(p)
    if d.empty: raise ValueError(f'Empty AirFloX raw file: {p}')
    candidate_rows=np.flatnonzero((d.iloc[:,0]=='WR').to_numpy())-1
    complete=[]; rejected=[]; expected=['WR','VEG','WR2','DC_WR','DC_VEG']
    for r in candidate_rows:
        if r<0 or r+5>=len(d): rejected.append((int(r),'truncated block')); continue
        labels=[str(d.iloc[r+i,0]) for i in range(1,6)]
        if labels!=expected: rejected.append((int(r),f'bad row sequence {labels}')); continue
        ok=True
        for off,label in enumerate(expected,1):
            vals=pd.to_numeric(d.iloc[r+off,1:n+1],errors='coerce')
            if len(vals)<n or vals.isna().all(): rejected.append((int(r),f'invalid {label} spectrum')); ok=False; break
        if ok: complete.append(int(r))
    if rejected: print(f'warning=Skipped {len(rejected)} incomplete/corrupted AirFloX measurement block(s) in {p}')
    rows=np.asarray(complete,dtype=int)
    if len(rows)==0: raise ValueError(f'No complete AirFloX measurement blocks found in {p}')
    pos=header_value_positions(d); mv=lambda k:[str(v) for v in d.iloc[rows,pos[k]].tolist()]; mn=lambda k:pd.to_numeric(d.iloc[rows,pos[k]],errors='coerce').to_numpy(float); spec=lambda off:numeric_frame(d.iloc[rows+off,1:]).iloc[:,:n].to_numpy(float).T
    r=AirFloXRaw(spec(1),spec(4),spec(3),spec(2),spec(5),mn('it_e')/1000,mn('it_l')/1000,[str(v) for v in d.iloc[rows,1].tolist()],[str(v) for v in d.iloc[rows,2].tolist()],mn('temp1'),mn('humidity'),mv('gps_time'),mv('gps_date'),mv('gps_lat'),mv('gps_lon'),mn('cpu1'),mn('cpu2'))
    # CCFLUX: the AirFloX GPS row filter is skipped when position comes from
    # the Noseboom/gimbal telemetry log, because the spectrometer's own GPS is
    # then unused. Flight_2707 has no AirFloX fix at all, so this filter would
    # discard every measurement. On flights that do carry a fix it is a no-op.
    r=repair_raw_blocks(r,p)
    keep=np.ones(len(r.date),dtype=bool)
    if drop_zero_gps:
        keep&=~(pd.to_numeric(pd.Series(r.gps_lat),errors='coerce').to_numpy(float)==0)
    if drop_e500_zero and r.e.shape[0]>=500:
        keep=keep & ~(r.e[499,:]==0)
    return filter_raw(r,keep)

def read_full_calibration(p):
    d=pd.read_csv(p,sep=';',engine='python')
    out=pd.DataFrame({'wl':pd.to_numeric(d['wl'],errors='coerce'),'up_coef':pd.to_numeric(d['up_coef'],errors='coerce'),'dw_coef':pd.to_numeric(d['dw_coef'],errors='coerce')})
    out=out.dropna(subset=['wl','up_coef','dw_coef']).reset_index(drop=True)
    out.attrs['nl_coeff']=extract_nl_coefficients(d)
    return out

def extract_nl_coefficients(d):
    # R GUI expects row 8 of the calibration metadata to be "NL COEF", followed by 8 polynomial coefficients.
    meta_cols=[c for c in d.columns if str(c).strip().lower().replace('.',' ') in {'device id','device id '} or 'device' in str(c).lower()]
    if not meta_cols:
        return None
    col=meta_cols[0]
    vals=d[col].astype(str).str.strip().tolist()
    idx=[i for i,v in enumerate(vals) if str(v).strip().upper()=='NL COEF']
    if not idx:
        return None
    start=idx[0]+1
    numeric_cols=[c for c in d.columns if pd.api.types.is_numeric_dtype(pd.to_numeric(d[c],errors='coerce'))]
    coeff=[]
    for r in range(start,min(start+8,len(d))):
        row=[]
        for c in d.columns:
            val=pd.to_numeric(pd.Series([d.loc[r,c]]),errors='coerce').iloc[0]
            if pd.notna(val): row.append(float(val))
        if row:
            coeff.append(row[0])
    return np.asarray(coeff,float) if len(coeff)==8 else None

def get_radiance(counts,it_ms,coef):
    with np.errstate(divide='ignore',invalid='ignore'):
        return counts/(it_ms[None,:])*coef[:,None]

def parse_gps_coord(vals,pos_hemi,neg_hemi):
    out=[]
    for v in vals:
        s=str(v).strip(); sign=1
        if s.endswith(neg_hemi): sign=-1
        s=s.rstrip(pos_hemi+neg_hemi).strip()
        try: out.append(sign*float(s))
        except Exception: out.append(np.nan)
    return np.asarray(out,float)

# R's getcoordinates() rounds each parsed coordinate to 4 decimals and then, for
# whichever axis has no negative-hemisphere letter anywhere in the file, assigns
# the flight mean to *every* row. The latitude branch indexes with nalatn (rows
# that failed to parse) but the longitude branch indexes with nalonw (rows with
# no "W"), which for an all-East flight is every row, so all longitudes collapse
# to one number. It is a defect in the reference implementation.
#
# CC-FLUX keeps the per-row longitude. A Zeppelin transect covers kilometres and
# assigning it a single longitude is not defensible, even though the only thing
# these coordinates feed is the solar zenith angle - the Lat/Lon columns written
# out are overwritten by the telemetry match. The cost is that our SZA differs
# from an R run by about 2e-4 degrees; the 4-decimal rounding above is kept
# because that part of getcoordinates() is deliberate.
#
# Set this True to reproduce the R output exactly, e.g. when re-checking against
# an archived R result.
GETCOORDINATES_R_LONGITUDE_MEAN=False

def r_round_half_even(values,digits=4):
    """R's round(): IEC 60559 round-half-to-even, which np.round also uses."""
    return np.round(np.asarray(values,float),digits)

def fill_bad_gps(lat,lon,*,r_getcoordinates=True):
    lat=np.asarray(lat,float).copy(); lon=np.asarray(lon,float).copy()
    # A coordinate off the globe is not a position. Two truncated blocks in
    # Flight_CC0807's FULL channel parse as latitude 2495 and 2601, and without
    # this they were the only rows that looked good: the fill below then copied
    # 2495 into all 1 307 rows that held 0.00000, the file appeared to carry a
    # fix, the Noseboom recomputation never ran, and the exported solar zenith
    # angle reached 156 degrees at midday. gps_position_mask already rejects
    # these; this is the same judgement applied where the value is used.
    # read_drox_full has already cleared any coordinate that was not a position,
    # so this is R's getcoordinates() and nothing more. The guard stays as an
    # invariant rather than a repair: a value off the globe reaching here would
    # be copied over every row that has no fix, which is how Flight_CC0807's
    # latitude 2495 became the whole file's position.
    off_globe=(np.abs(lat)>90)|(np.abs(lon)>180)
    lat[off_globe]=np.nan; lon[off_globe]=np.nan
    bad=(~np.isfinite(lat))|(~np.isfinite(lon))|(lat==0)|(lon==0)
    good=~bad
    if good.any():
        lat[bad]=lat[good][0]; lon[bad]=lon[good][0]
    if not r_getcoordinates:
        return lat,lon
    lat=r_round_half_even(lat,4); lon=r_round_half_even(lon,4)
    if GETCOORDINATES_R_LONGITUDE_MEAN and np.isfinite(lon).any():
        mean_lon=float(np.nanmean(lon))
        if not np.allclose(lon[np.isfinite(lon)],mean_lon,atol=0,rtol=0):
            print(f'warning=AirFloX longitude replaced by the flight mean ({mean_lon:.7f}) '
                  'to reproduce the R getcoordinates() reference. This affects the solar '
                  'zenith angle only; the written Lat/Lon come from the telemetry match.')
        lon=np.full_like(lon,mean_lon)
    return lat,lon
def _r_time_to_hms(value):
    text=str(value).strip().split('.')[0]
    if not text or text.lower().startswith('nan'): return None
    text=text.zfill(6)
    try: return int(text[0:2]),int(text[2:4]),int(text[4:6])
    except Exception: return None

def _r_date_to_ymd(value):
    text=str(value).strip().split('.')[0]
    if not text or text.lower().startswith('nan'): return None
    if len(text)==5:
        text='0'+text
    elif len(text)<6:
        text=text.zfill(6)
    try:
        return 2000+int(text[4:6]),int(text[2:4]),int(text[0:2])
    except Exception:
        return None

def _r_datetime(time_value,date_value):
    hms=_r_time_to_hms(time_value); ymd=_r_date_to_ymd(date_value)
    if hms is None or ymd is None: return pd.NaT
    try: return datetime(ymd[0],ymd[1],ymd[2],hms[0],hms[1],hms[2],tzinfo=timezone.utc)
    except Exception: return pd.NaT

def _record_clock_datetime(time_value,date_value):
    """CCFLUX: parse the AirFloX record clock, whose date field is YYMMDD.

    Used only when the GPS field is unusable (see _gps_is_unusable). The GPS
    parser _r_datetime is left untouched so flights with a real fix are
    unaffected.
    """
    hms=_r_time_to_hms(time_value)
    text=str(date_value).strip().split('.')[0]
    if hms is None or not text or text.lower().startswith('nan'): return pd.NaT
    text=text.zfill(6)
    try: return datetime(2000+int(text[0:2]),int(text[2:4]),int(text[4:6]),hms[0],hms[1],hms[2],tzinfo=timezone.utc)
    except Exception: return pd.NaT

def _gps_is_unusable(utc,clock):
    """CCFLUX: whether the GPS clock never left its power-on default.

    An AirFloX with no fix stamps the GPS epoch (05/06-01-1980), which the
    two-digit year maps to 2080. Flight_2707 shows this on every FULL block, so
    every measurement lands 54 years after the flight and is discarded during
    telemetry matching. Detected by comparing against the record clock rather
    than by hard-coding the default, so any implausible GPS date is caught.
    """
    disagreeing=usable=0
    for a,b in zip(utc,clock):
        if pd.isna(a) or pd.isna(b): continue
        usable+=1
        if abs((a-b).total_seconds())>86400: disagreeing+=1
    return usable>0 and disagreeing==usable

# A GPS date this far from the record-clock date is the receiver's power-on
# default, not a fix. Two days rather than one, so a flight crossing midnight
# is not rejected.
GPS_FIX_MAX_DATE_GAP_SECONDS=2*86400

# FULL and FLUO are two channels of one AirFloX box and share its record clock,
# so an offset measured on the channel that got a fix is the offset for the one
# that did not. run_flight() resolves the channels in whichever order gives an
# anchor and clears this between flights.
# The timezone the AirFloX record clock is set to, declared by the operator
# when the scan finds SIF. FLOX and FULL write campaign local time, and with no
# GPS fix nothing in the file says how far that is from UTC. "offset_seconds"
# is what to subtract to reach UTC: 7200 for CEST, 0 when the clock is UTC.
RECORD_CLOCK_TIMEZONE: dict = {"offset_seconds": None, "label": None}


RECORD_CLOCK_OFFSET_HINT={}

def gps_position_mask(raw):
    """Which rows carry a position, and so a receiver that actually locked.

    A receiver that never acquires a fix still emits a GPS_TIME_UTC field. On
    Flight_CCT0803 every AirFloX row of both channels reads GPS_lat=0.00000
    while GPS_TIME_UTC sits at the 23:59:5x/00:00:xx rollover, with a handful
    of rows two hours from the record clock. Judging a fix by the timestamp
    alone accepted those rows and shifted the whole flight by two hours.
    A time is only trusted where the receiver reported where it was.
    """
    if raw is None:
        return None
    latitude=parse_gps_coord(getattr(raw,'gps_lat',[]),'N','S')
    longitude=parse_gps_coord(getattr(raw,'gps_lon',[]),'E','W')
    if latitude.size==0 or longitude.size!=latitude.size:
        return None
    return (np.isfinite(latitude)&np.isfinite(longitude)
            &(np.abs(latitude)>1e-6)&(np.abs(longitude)>1e-6)
            &(np.abs(latitude)<=90)&(np.abs(longitude)<=180))

def real_gps_fix_mask(utc,clock,raw=None):
    """Which rows carry a genuine GPS fix rather than the 1980 default.

    The date is compared against the record clock rather than hard-coding
    1980/2080, so any implausible date is caught whatever the receiver stamps;
    and where the file carries coordinates, a row must also report a position.
    """
    mask=[]
    for a,b in zip(utc,clock):
        if pd.isna(a) or pd.isna(b):
            mask.append(False); continue
        mask.append(abs((a-b).total_seconds())<=GPS_FIX_MAX_DATE_GAP_SECONDS)
    mask=np.asarray(mask,dtype=bool)
    position=gps_position_mask(raw)
    if position is not None and position.size==mask.size:
        mask&=position
    return mask

def measure_record_clock_offset(utc,clock,raw=None):
    """Seconds the AirFloX record clock runs ahead of GPS UTC.

    The record clock is set to campaign local time and is not disciplined, so
    the offset is neither zero nor a whole timezone step: on Flight_2707 it is
    2 h 0 m 40 s. Measuring it from the rows that do have a fix, instead of
    assuming a timezone, keeps this correct when the clock is simply set wrong.

    Returns (median_offset_seconds, count, spread_seconds) or (None, 0, 0.0).
    """
    fix=real_gps_fix_mask(utc,clock,raw)
    offsets=[(clock[i]-utc[i]).total_seconds() for i in np.flatnonzero(fix)]
    if not offsets: return None,0,0.0
    return float(np.median(offsets)),len(offsets),float(np.max(offsets)-np.min(offsets))

def _apply_record_clock_offset(record_clock,offset_seconds):
    shift=timedelta(seconds=float(offset_seconds))
    return [pd.NaT if pd.isna(t) else t-shift for t in record_clock]

def _backfill_missing_times(times):
    """CCFLUX: carry the previous good time over a missing one.

    A record clock with an unreadable block leaves NaT, and solar() calls
    timetuple() on whatever it is given, so a NaT reaches it as
    "NaTType does not support timetuple" and kills the whole channel. The
    GPS-derived path already backfills from the neighbour; the declared-clock
    path returned early and did not, so declaring the clock turned a skipped
    block into a crash.
    """
    result=list(times)
    for i in range(len(result)):
        if pd.isna(result[i]):
            result[i]=result[i-1] if i>0 and not pd.isna(result[i-1]) else pd.NaT
    # A leading run of NaT has no earlier value to inherit; take the first good
    # one that follows so the block is placed in the flight rather than in 1970.
    first_good=next((t for t in result if not pd.isna(t)),None)
    if first_good is not None:
        for i in range(len(result)):
            if pd.isna(result[i]): result[i]=first_good
            else: break
    return result

def get_gps_utc(raw):
    clock_dates=pd.to_numeric(pd.Series(raw.date),errors='coerce').to_numpy(float)
    invalid=(clock_dates==181818)|(clock_dates==4516585)|(clock_dates==191919)|(clock_dates<180000)|(~np.isfinite(clock_dates))
    gps_time=[('111111' if str(v).strip() in {'no GPS fix','no GPS  fix','no GPS   fix','0','0.0'} else v) for v in raw.gps_time]
    gps_date=[('111111' if len(str(v).strip().split('.')[0]) not in (5,6) else v) for v in raw.gps_date]
    utc=[_r_datetime(t,d) for t,d in zip(gps_time,gps_date)]
    if len(raw.date)>0 and invalid.all():
        if len(utc)>1:
            utc[0]=utc[1]
        return utc
    fixed_clock_dates=list(raw.date)
    bad_idx=np.flatnonzero(invalid)
    for i in bad_idx:
        if 0<i<len(fixed_clock_dates): fixed_clock_dates[i]=fixed_clock_dates[i-1]
    # R reads the two date fields with two different functions: DateTimeFloX
    # takes the record clock as YYMMDD, DateTimeRoXGPS takes the GPS date as
    # DDMMYY. Parsing the record clock with the GPS convention turned 240828
    # into 2028-08-24, which is 1457 days from the GPS date, so the day-jump
    # repair below fired on good rows and replaced their GPS time with an
    # interpolated one.
    record_clock=[_record_clock_datetime(t,d) for t,d in zip(raw.time,fixed_clock_dates)]
    clock=record_clock
    # The GPS may be absent for the whole file, or acquire a fix part-way
    # through. Both happen on Flight_2707: FULL never gets one, FLUO gets one
    # after 177 spectra. Where there is a fix, use it and measure how far ahead
    # the record clock runs; elsewhere, correct the record clock by that amount.
    offset,fixes,spread=measure_record_clock_offset(utc,record_clock,raw)
    # CCFLUX: a receiver that reported no position never locked, so its clock
    # cannot calibrate anything either, and the declaration is the only source.
    # _gps_is_unusable alone was not enough: it compares timestamps against the
    # record clock with a one-day threshold, and Flight_CCT0803's rollover rows
    # sit two hours away - the CEST offset being resolved - so they counted as
    # agreeing, its unanimity test failed, and the whole file was treated as
    # having a usable GPS. The 2080-01-05 power-on dates then survived into the
    # time filter, which discarded every row of the flight. fixes comes from
    # real_gps_fix_mask, which already requires a reported position, so the two
    # judgements now agree.
    if _gps_is_unusable(utc,record_clock) or fixes==0:
        # FLOX and FULL write their record clock in campaign local time, and a
        # receiver that never locks cannot say how far that is from UTC. The
        # operator declares it once for the flight, and that declaration is
        # authoritative: it is the only reliable source when there is no fix.
        declared=RECORD_CLOCK_TIMEZONE.get('offset_seconds')
        if declared is not None:
            label=RECORD_CLOCK_TIMEZONE.get('label') or f'{declared:+.0f} s'
            if declared:
                print(f'warning=AirFloX GPS never acquired a fix. The record clock is read as '
                      f'{label} and converted to UTC by {declared:.0f} s, as declared for this flight.')
            else:
                print('warning=AirFloX GPS never acquired a fix. The record clock is read as UTC, '
                      'as declared for this flight.')
            return _backfill_missing_times(_apply_record_clock_offset(record_clock,declared))
        hint=getattr(raw,'record_clock_offset_seconds',None)
        if hint is None: hint=RECORD_CLOCK_OFFSET_HINT.get('seconds')
        if hint is not None:
            print(f'warning=AirFloX GPS never acquired a fix; the record clock is used for UTC, '
                  f'corrected by {hint:.1f} s measured from the other AirFloX channel of this flight.')
            return _backfill_missing_times(_apply_record_clock_offset(record_clock,hint))
        print('warning=AirFloX GPS clock never acquired a fix, and no channel of this flight has one; '
              'the instrument record clock is used for UTC uncorrected. It is set to campaign local '
              'time, so timestamps may be offset by the local UTC difference.')
        return _backfill_missing_times(record_clock)
    if offset is not None:
        RECORD_CLOCK_OFFSET_HINT['seconds']=offset
    diffs=[]
    for i in range(len(clock)-1):
        if pd.isna(clock[i]) or pd.isna(clock[i+1]): diffs.append(0.0)
        else: diffs.append((clock[i+1]-clock[i]).total_seconds())
    if diffs: diffs.append(diffs[-1])
    else: diffs=[0.0]*len(clock)
    for i in range(min(len(utc),len(clock))):
        if pd.isna(utc[i]) or pd.isna(clock[i]): continue
        if abs((utc[i].date()-clock[i].date()).days)>1 and i+1<len(utc) and not pd.isna(utc[i+1]):
            utc[i]=utc[i+1]-timedelta(seconds=diffs[i])
    if len(utc)>1 and not pd.isna(utc[1]):
        utc[0]=utc[1]-timedelta(seconds=diffs[0])
    for i in range(len(utc)):
        if pd.isna(utc[i]):
            utc[i]=utc[i-1] if i>0 and not pd.isna(utc[i-1]) else datetime(1970,1,1,tzinfo=timezone.utc)
    # R's repair above rewrites a bad row from its neighbour, which works for an
    # isolated one. Flight_2707's FLUO channel has no fix for its first 177
    # spectra, and a neighbour taken from inside that block is itself in 1980, so
    # the repair propagates the wrong year instead of correcting it. Only rows
    # still implausible after R has had its turn are corrected here, using the
    # record-clock offset measured from the rows that do have a fix - so a file
    # R handles correctly is left exactly as R leaves it.
    still_wrong=np.flatnonzero(~real_gps_fix_mask(utc,record_clock,raw))
    if len(still_wrong):
        correction=offset if offset is not None else RECORD_CLOCK_OFFSET_HINT.get('seconds')
        if correction is None:
            print(f'warning=AirFloX GPS is unusable on {len(still_wrong)}/{len(utc)} spectra and no '
                  'row of this flight has a fix to calibrate the record clock. Their timestamps are '
                  'left as recorded.')
        else:
            source='this channel' if offset is not None else 'the other AirFloX channel'
            print(f'warning=AirFloX GPS acquired no fix on {len(still_wrong)}/{len(utc)} spectra. '
                  f'The record clock runs {correction:.1f} s ahead of GPS UTC '
                  f'(spread {spread:.1f} s, measured on {fixes} spectra from {source}); '
                  'those spectra are corrected by that amount.')
            corrected=_apply_record_clock_offset(record_clock,correction)
            for i in still_wrong:
                if not pd.isna(corrected[i]): utc[i]=corrected[i]
    return utc
def solar(times):
    """Port of the GUI's solar(): the Astronomical Almanac solar position.

    The earlier port used the Spencer/NOAA Fourier approximation instead, which
    is a different algorithm and left the solar zenith angle up to 0.23 degrees
    away from the reference.
    """
    rad=math.pi/180
    epoch=np.array([np.nan if pd.isna(t) else pd.Timestamp(t).timestamp() for t in times],dtype=float)
    jd=epoch/86400.0+2440587.5
    jc=(jd-2451545.0)/36525.0
    l0=(280.46646+jc*(36000.76983+0.0003032*jc))%360.0
    m=357.52911+jc*(35999.05029-0.0001537*jc)
    e=0.016708634-jc*(4.2037e-05+1.267e-07*jc)
    eqctr=(np.sin(rad*m)*(1.914602-jc*(0.004817+1.4e-05*jc))
           +np.sin(rad*2*m)*(0.019993-0.000101*jc)
           +np.sin(rad*3*m)*0.000289)
    lambda0=l0+eqctr
    omega=125.04-1934.136*jc
    lam=lambda0-0.00569-0.00478*np.sin(rad*omega)
    seconds=21.448-jc*(46.815+jc*(0.00059-jc*0.001813))
    obliq0=23+(26+(seconds/60))/60
    obliq=obliq0+0.00256*np.cos(rad*omega)
    y=np.tan(rad*obliq/2)**2
    eqn_time=4/rad*(y*np.sin(rad*2*l0)-2*e*np.sin(rad*m)
                    +4*e*y*np.sin(rad*m)*np.cos(rad*2*l0)
                    -0.5*y**2*np.sin(rad*4*l0)-1.25*e**2*np.sin(rad*2*m))
    solar_dec=np.arcsin(np.sin(rad*obliq)*np.sin(rad*lam))
    solar_time=((jd-0.5)%1.0*1440+eqn_time)/4
    return {'solarTime':solar_time,'eqnTime':eqn_time,
            'sinSolarDec':np.sin(solar_dec),'cosSolarDec':np.cos(solar_dec)}

def zenith(times,lon,lat):
    rad=math.pi/180
    sun=solar(times)
    lon=np.asarray(lon,dtype=float); lat=np.asarray(lat,dtype=float)
    hour_angle=sun['solarTime']+lon-180
    with np.errstate(invalid='ignore'):
        cos_zenith=(np.sin(rad*lat)*sun['sinSolarDec']
                    +np.cos(rad*lat)*sun['cosSolarDec']*np.cos(rad*hour_angle))
        cos_zenith=np.clip(cos_zenith,-1.0,1.0)
        return np.arccos(cos_zenith)/rad

def stats_mean(wl,sp,a,b):
    m=(wl>=a)&(wl<=b)
    return np.full(sp.shape[1],np.nan) if not m.any() else np.nanmean(sp[m,:],axis=0)
def trapz_area(wl,sp,a,b):
    m=(wl>=a)&(wl<=b)
    return np.full(sp.shape[1],np.nan) if not m.any() else (np.trapezoid(sp[m,:],wl[m],axis=0) if hasattr(np,'trapezoid') else np.trapz(sp[m,:],wl[m],axis=0))
def band_values(wl,sp,centers,fwhm):
    out=[]
    for c,w in zip(centers,fwhm):
        m=(wl>=c-w/2)&(wl<=c+w/2); out.append(np.full(sp.shape[1],np.nan) if not m.any() else np.nanmean(sp[m,:],axis=0))
    return out
def eval_index_expression(expr,vals): return eval(expr,{'__builtins__':{},'np':np,'math':math},{chr(97+i):v for i,v in enumerate(vals)})
def load_indices(p):
    d=pd.read_csv(p,sep=';',engine='python')
    return [{'name':str(r['Index']),'wl':[float(x) for x in str(r['wl']).split(';')],'fwhm':[float(x) for x in str(r['fwhm']).split(';')],'expr':str(r['expr']),'spec':str(r.get('Spec','R'))} for _,r in d.iterrows()]
def apply_nonlinearity(data,coeff):
    coeff=np.asarray(coeff,float)
    scale=sum(coeff[i]*(data**i) for i in range(8))
    with np.errstate(divide='ignore',invalid='ignore'):
        return data/scale

def dark_subtracted_signals(raw,cal,apply_nl):
    """(E, E2, L) with the dark current removed, and the nonlinearity if asked.

    R subtracts the dark current first and corrects the difference:

        data<-list(DCSubtraction(E,dcE),DCSubtraction(L,dcL),DCSubtraction(E2,dcE))
        res <- lapply(data, Non_linearity, coeffnl=NL_coeff)

    The order matters, because the correction is a degree-7 polynomial and
    NL(E) - NL(dcE) is not NL(E - dcE). Correcting each array on its own, as
    this used to, would have diverged from the reference GUI on any instrument
    that ships coefficients. The saturation flags stay on the untouched counts,
    which is where R reads them (apply(dat$L,2,max)).
    """
    de=raw.e-raw.dc_e; de2=raw.e2-raw.dc_e; dl=raw.l-raw.dc_l
    if not apply_nl:
        return de,de2,dl
    coeff=cal.attrs.get('nl_coeff')
    if coeff is None or len(coeff)!=8:
        raise ValueError('apply_nonlinearity_correction=yes requested, but the calibration file does not contain an 8-value NL COEF block. Use no for non-NL calibration files.')
    return apply_nonlinearity(de,coeff),apply_nonlinearity(de2,coeff),apply_nonlinearity(dl,coeff)

def stats_on_spectra(wl,start,end,sp,fun='mean'):
    # R: which(wl >= wlStart & wl <= wlEnd) - the endpoints are inside the range.
    m=(wl>=start)&(wl<=end)
    if not np.any(m):
        return np.full(sp.shape[1],np.nan)
    sub=sp[m,:]
    out=np.full(sp.shape[1],np.nan)
    good=np.isfinite(sub).any(axis=0)
    if not good.any():
        return out
    with np.errstate(invalid='ignore'):
        out[good]=np.nanmin(sub[:,good],axis=0) if fun=='min' else np.nanmean(sub[:,good],axis=0)
    return out

def nknots_smspl(n):
    """R stats:::.nknots.smspl - how many knots smooth.spline uses for n points."""
    n=int(n)
    if n<50: return n
    a1,a2,a3,a4=math.log2(50),math.log2(100),math.log2(140),math.log2(200)
    if n<200: v=2**(a1+(a2-a1)*(n-50)/150)
    elif n<800: v=2**(a2+(a3-a2)*(n-200)/600)
    elif n<3200: v=2**(a3+(a4-a3)*(n-800)/2400)
    else: v=200+(n-3200)**0.2
    return int(v)  # R trunc()

def _bspline_design(knots,nk,xs):
    from scipy.interpolate import BSpline
    B=np.zeros((len(xs),nk))
    eye=np.eye(nk)
    for i in range(nk):
        B[:,i]=np.nan_to_num(BSpline(knots,eye[i],3,extrapolate=False)(xs))
    return B

def _bspline_penalty(knots,nk):
    """Omega_ij = integral of B_i''(t) B_j''(t) dt, the sgram() matrix in R."""
    from scipy.interpolate import BSpline
    eye=np.eye(nk)
    second=[BSpline(knots,eye[i],3,extrapolate=False).derivative(2) for i in range(nk)]
    breaks=np.unique(knots)
    # The second derivative of a cubic B-spline is piecewise linear, so the
    # integrand is piecewise quadratic and 3-point Gauss-Legendre is exact.
    gx,gw=np.polynomial.legendre.leggauss(3)
    omega=np.zeros((nk,nk))
    for a,b in zip(breaks[:-1],breaks[1:]):
        if not b>a: continue
        t=0.5*(b-a)*gx+0.5*(a+b); w=0.5*(b-a)*gw
        V=np.column_stack([np.nan_to_num(f(t)) for f in second])
        omega+=V.T@(w[:,None]*V)
    return omega

_SMOOTH_SPLINE_CACHE={}

def _smooth_spline_basis(x,df,spar_low=-1.5,spar_high=1.5,tol=1e-4,maxit=500):
    """Set up R's smooth.spline(x, y, df=df) for a fixed x: knots, basis, lambda.

    Everything here depends only on x and df, so the factorisation is shared by
    every spectrum with the same gap pattern. Reproduces stats::smooth.spline:
    x is scaled to [0,1], .nknots.smspl chooses the knot count, the penalty is
    the Gram matrix of the second derivatives, and lambda is searched on R's
    spar scale until the hat-matrix trace equals the requested df.
    """
    key=(x.tobytes(),len(x),float(df))
    hit=_SMOOTH_SPLINE_CACHE.get(key)
    if hit is not None: return hit
    nx=len(x); x0=float(x[0]); r_ux=float(x[-1]-x[0])
    xbar=(x-x0)/r_ux
    nknots=nknots_smspl(nx)
    # R: xbar[seq.int(1, nx, length.out = nknots)] - a double index, truncated.
    idx=np.trunc(np.linspace(1,nx,nknots)).astype(int)-1
    knots=np.concatenate([np.repeat(xbar[0],3),xbar[idx],np.repeat(xbar[-1],3)])
    nk=nknots+2
    B=_bspline_design(knots,nk,xbar)
    omega=_bspline_penalty(knots,nk)
    xtx=B.T@B
    # sbart.c forms the ratio from the middle of the diagonal band only, not
    # from the full trace: for(i = 3-1; i < nk-3; ++i) { t1 += hs0[i]; t2 += sg0[i]; }
    lo_i,hi_i=2,max(3,nk-3)
    ratio=float(np.sum(np.diag(xtx)[lo_i:hi_i])/np.sum(np.diag(omega)[lo_i:hi_i]))

    def trace_for(spar):
        lam=ratio*256.0**(3.0*spar-1.0)
        return float(np.trace(np.linalg.solve(xtx+lam*omega,xtx))),lam

    lo,hi=spar_low,spar_high
    t_lo,_=trace_for(lo); t_hi,_=trace_for(hi)
    if df>=t_lo: spar=lo
    elif df<=t_hi: spar=hi
    else:
        for _ in range(maxit):
            mid=0.5*(lo+hi)
            t_mid,_=trace_for(mid)
            if abs(t_mid-df)<tol or (hi-lo)<1e-12: lo=hi=mid; break
            if t_mid>df: lo=mid
            else: hi=mid
        spar=0.5*(lo+hi)
    trace,lam=trace_for(spar)
    factor=np.linalg.inv(xtx+lam*omega)@B.T
    fit=(knots,nk,x0,r_ux,factor,spar,lam,trace)
    _SMOOTH_SPLINE_CACHE[key]=fit
    return fit

def _smooth_spline_predict(fit,wl,y):
    from scipy.interpolate import BSpline
    knots,nk,x0,r_ux,factor,_spar,_lam,_trace=fit
    coef=factor@y
    xs=(np.asarray(wl,float)-x0)/r_ux
    spline=BSpline(knots,coef,3,extrapolate=False)
    out=spline(np.clip(xs,0.0,1.0))
    # R's predict.smooth.spline continues linearly beyond the fitted range.
    slope=spline.derivative(1)
    left=xs<0.0; right=xs>1.0
    if left.any(): out[left]=float(spline(0.0))+float(slope(0.0))*xs[left]
    if right.any(): out[right]=float(spline(1.0))+float(slope(1.0))*(xs[right]-1.0)
    return out

def spline_gapfill_matrix(wl,sp,df=80):
    """FieldSpectroscopyCC::SplineSmoothGapfilling, column by column.

    R drops the NA rows, fits smooth.spline(df=80) on what is left, and predicts
    back onto the full wavelength grid. The earlier port approximated that with
    a least-squares regression spline, which is a different estimator: it left
    the smoothed reflectance and irradiance ~1e-1 away from R and moved every
    retrieved SIF value.
    """
    sp=np.asarray(sp,dtype=float)
    out=np.empty_like(sp,dtype=float)
    masks={}
    for j in range(sp.shape[1]):
        ok=np.isfinite(sp[:,j])
        masks.setdefault(ok.tobytes(),(ok,[]))[1].append(j)
    for ok,columns in masks.values():
        count=int(ok.sum())
        if count<4:
            for j in columns:
                y=sp[:,j]
                out[:,j]=np.interp(wl,wl[ok],y[ok]) if count>=2 else y
            continue
        try:
            fit=_smooth_spline_basis(wl[ok],float(df))
            for j in columns:
                out[:,j]=_smooth_spline_predict(fit,wl,sp[ok,j])
        except Exception as exc:
            print(f'warning=Smoothing spline failed ({exc}); falling back to linear gap filling.')
            for j in columns:
                out[:,j]=np.interp(wl,wl[ok],sp[ok,j])
    return out

def ifld_band(wl,e,l,band):
    # Port of FieldSpectroscopyDP::iFLD used by the FloX GUI default SIF method.
    r=np.divide(l,e,out=np.full_like(l,np.nan,dtype=float),where=e!=0)
    rs=r.copy(); rs[(wl>686)&(wl<688),:]=np.nan; rs[(wl>757)&(wl<768),:]=np.nan
    r_sm=spline_gapfill_matrix(wl,rs,df=80)
    es=e.copy(); es[(wl>680)&(wl<711),:]=np.nan; es[(wl>753)&(wl<784),:]=np.nan
    e_sm=spline_gapfill_matrix(wl,es,df=80)
    if str(band).upper()=='A':
        wl_in=760.0; buffer_in=5.0; buffer_out=1.0; out_in=0.7535*0.3+2.8937
    else:
        wl_in=687.0; buffer_in=5.0; buffer_out=2.0; out_in=0.697*0.3+1.245
    ein=stats_on_spectra(wl,wl_in-buffer_in,wl_in+buffer_in,e,'min')
    lin=stats_on_spectra(wl,wl_in-buffer_in,wl_in+buffer_in,l,'min')
    n=e.shape[1]; r_in_sm=np.full(n,np.nan); wl_out=np.full(n,np.nan); out_idx=np.zeros(n,dtype=int); valid=np.zeros(n,dtype=bool)
    for j in range(n):
        col=e[:,j]
        finite_col=np.isfinite(col)
        if not finite_col.any() or not np.isfinite(ein[j]):
            continue
        matches=np.flatnonzero(np.isfinite(col) & (col==ein[j]))
        idx=int(matches[0]) if len(matches) else int(np.nanargmin(np.abs(col-ein[j])))
        if not np.isfinite(r_sm[idx,j]):
            continue
        r_in_sm[j]=r_sm[idx,j]; wl_out[j]=wl[idx]-out_in; out_idx[j]=int(np.nanargmin(np.abs(wl-wl_out[j]))); valid[j]=True
    skipped=int((~valid).sum())
    if skipped:
        print(f'warning=iFLD {str(band).upper()} skipped {skipped} FLUO spectrum/spectra with no valid radiance in the absorption band; SIF is written as #N/D for those rows.')
    if not valid.any():
        return np.full(n,np.nan)
    e_in_sm=stats_on_spectra(wl,wl_in-buffer_in,wl_in+buffer_in,e_sm,'mean')
    rout=np.full(n,np.nan); rout[valid]=r[out_idx[valid],np.arange(n)[valid]]
    mean_wl_out=float(np.nanmean(wl_out[valid]))
    eout=stats_on_spectra(wl,mean_wl_out-buffer_out,mean_wl_out,e,'mean')
    lout=stats_on_spectra(wl,mean_wl_out-buffer_out,mean_wl_out,l,'mean')
    with np.errstate(divide='ignore',invalid='ignore'):
        alpha_r=rout/r_in_sm
        alpha_f=eout/e_in_sm*alpha_r
        fluo=(alpha_r*eout*lin - ein*lout)/(alpha_r*eout-alpha_f*ein)
    fluo[~np.isfinite(fluo)]=np.nan
    return fluo

def estimate_full_shift_nm(wl,e):
    region=(wl>=755)&(wl<=765)
    if not np.any(region):
        return np.nan
    w=wl[region]; vals=[]
    for j in range(e.shape[1]):
        y=e[region,j]
        if not np.isfinite(y).any():
            continue
        i=int(np.nanargmin(y))
        if 0<i<len(w)-1 and np.isfinite(y[i-1:i+2]).all():
            denom=(y[i-1]-2*y[i]+y[i+1])
            delta=0.5*(y[i-1]-y[i+1])/denom if denom!=0 else 0.0
            vals.append(float(w[i]+delta*(w[1]-w[0])))
        else:
            vals.append(float(w[i]))
    if not vals:
        return np.nan
    return float(np.nanmedian(vals)-760.0)

def shift_spectra_to_nominal(wl,sp,shift_nm):
    out=np.empty_like(sp,dtype=float)
    for j in range(sp.shape[1]):
        out[:,j]=np.interp(wl+shift_nm,wl,sp[:,j],left=np.nan,right=np.nan)
    return out

def apply_full_spectral_shift(wl,e,e2,l,enabled):
    if not enabled:
        return e,e2,l,0.0
    shift=estimate_full_shift_nm(wl,e)
    if not np.isfinite(shift):
        print('warning=Spectral shift correction requested, but no O2-A feature could be estimated. No shift applied.')
        return e,e2,l,0.0
    if abs(shift)<0.05:
        print(f'spectral_shift_correction=enabled; estimated_shift_nm={shift:.4f}; below 0.05 nm, no interpolation applied.')
        return e,e2,l,0.0
    if abs(shift)>3.0:
        print(f'warning=Spectral shift correction requested, but estimated shift {shift:.3f} nm is outside the safe range. No shift applied.')
        return e,e2,l,0.0
    print(f'spectral_shift_correction=enabled; estimated_shift_nm={shift:.4f}')
    return shift_spectra_to_nominal(wl,e,shift),shift_spectra_to_nominal(wl,e2,shift),shift_spectra_to_nominal(wl,l,shift),shift

def parse_utc_arg(value):
    if value is None or str(value).strip()=='':
        return None
    ts=pd.to_datetime(str(value).strip(),utc=True,errors='coerce')
    if pd.isna(ts):
        raise ValueError(f'Invalid UTC time value: {value}')
    return ts.tz_convert(None)

def apply_time_window(m,r,start_utc=None,end_utc=None):
    start=parse_utc_arg(start_utc); end=parse_utc_arg(end_utc)
    if start is None and end is None:
        return m,r
    t=pd.to_datetime(m['datetime [UTC]'],utc=True,errors='coerce').dt.tz_convert(None)
    keep=t.notna().to_numpy(copy=True)
    if start is not None: keep &= (t>=start).to_numpy()
    if end is not None: keep &= (t<=end).to_numpy()
    kept=int(np.count_nonzero(keep)); total=len(m)
    print(f'time_filter=custom; kept {kept}/{total} row(s); start_utc={start_utc}; end_utc={end_utc}')
    if kept==0:
        raise ValueError('Custom UTC time filter removed all rows. Check the selected time range.')
    m=m.loc[keep].reset_index(drop=True)
    r['E']=r['E'][:,keep]; r['L']=r['L'][:,keep]; r['Ref']=r['Ref'][:,keep]
    return m,r

def cpu_time_offsets(raw):
    """Seconds between the GPS read and the spectrum, per block.

    R writes CPU1sec<-(CPU2-CPU1)/1000 and adds it, so a block whose CPU fields
    are empty simply carries NA through. Python's timedelta refuses: it answers
    "cannot convert float NaN to integer", and one truncated block took the
    whole channel with it - Flight_CC0807 failed three minutes into a 1 310
    block FULL file on a single row whose metadata fields were blank.

    The correction is sub-second and refines a time that is already known, so a
    block that lacks it keeps its own time rather than ending the flight.
    """
    offsets=(np.asarray(raw.cpu2,dtype=float)-np.asarray(raw.cpu1,dtype=float))/1000.0
    missing=~np.isfinite(offsets)
    count=int(np.count_nonzero(missing))
    if count:
        print(f'warning=The CPU timestamp pair is missing or unreadable on {count} of '
              f'{len(offsets)} spectra, so the sub-second offset between the GPS read and '
              'the spectrum is unknown there. Those blocks keep their recorded time; '
              'everything else about them is unchanged.')
    return np.where(missing,0.0,offsets)

def process_common(raw_path,cal,index_path,mode,apply_nl=False,spectral_shift_correction=False,retain_zero_gps=False):
    wl=cal['wl'].to_numpy(float); raw=read_drox_full(raw_path,len(wl),drop_e500_zero=(mode=='FLUO'),drop_zero_gps=not retain_zero_gps)
    de,de2,dl=dark_subtracted_signals(raw,cal,apply_nl)
    e=get_radiance(de,raw.it_e_ms,cal['up_coef'].to_numpy(float)); e2=get_radiance(de2,raw.it_e_ms,cal['up_coef'].to_numpy(float)); l=get_radiance(dl,raw.it_l_ms,cal['dw_coef'].to_numpy(float))
    if mode=='FULL':
        e,e2,l,_shift_nm=apply_full_spectral_shift(wl,e,e2,l,spectral_shift_correction)
    with np.errstate(divide='ignore',invalid='ignore'): ref=l/e
    lat=parse_gps_coord(raw.gps_lat,'N','S'); lon=parse_gps_coord(raw.gps_lon,'E','W'); lat,lon=fill_bad_gps(lat,lon); base=get_gps_utc(raw)
    utc=[dt+timedelta(seconds=float(o)) for dt,o in zip(base,cpu_time_offsets(raw))]
    ein750=stats_mean(wl,e,748,750); lin750=stats_mean(wl,l,748,750); e2in750=stats_mean(wl,e2,748,750)
    with np.errstate(divide='ignore',invalid='ignore'): est=np.abs(ein750-e2in750)*100/ein750
    idx={}
    for it in load_indices(index_path):
        sp=ref if it['spec']=='R' else l if it['spec']=='L' else e
        with np.errstate(divide='ignore',invalid='ignore'): idx[it['name']]=eval_index_expression(it['expr'],band_values(wl,sp,it['wl'],it['fwhm']))
    doy=np.array([dt.timetuple().tm_yday+(dt.hour*3600+dt.minute*60+dt.second)/86400 for dt in base])
    out={'doy.dayfract':doy,'datetime [UTC]':[dt.replace(tzinfo=None) for dt in utc],'SZA':zenith(base,lon,lat),'Lat':lat,'Lon':lon,'temp1 [C]':raw.temp1,'h1 [%]':raw.humidity}
    if mode=='FULL':
        ein_par=trapz_area(wl,e*math.pi,400,700); lin_par=trapz_area(wl,l*math.pi,400,700); nd=idx.get('NDVI')
        if nd is None:
            v=band_values(wl,ref,[800,670],[10,10]); nd=(v[0]-v[1])/(v[0]+v[1])
        apar=(ein_par*4.57-lin_par*4.57)*(0.105-0.323*nd+1.468*nd**2)
        out.update({'Incoming at 750nm Full [W m-2nm-1sr-1]':ein750,'Reflected 750nm full [W m-2nm-1sr-1]':lin750,'PAR inc [W m-2]':ein_par,'PAR ref [W m-2]':lin_par,'APAR [umol m-2 s-1]':apar,'E_stability full [%]':est,'sat value L full':(np.nanmax(raw.l,axis=0)>=200000).astype(int),'sat value E full':(np.nanmax(raw.e,axis=0)>=200000).astype(int),'sat value E2 full':(np.nanmax(raw.e2,axis=0)>=200000).astype(int),'Dynamic range E full [%]':np.nanmax(raw.e,axis=0)*100/200000,'Dynamic range L full [%]':np.nanmax(raw.l,axis=0)*100/200000})
    else:
        sif_a=ifld_band(wl,e,l,'A')*1000
        sif_b=ifld_band(wl,e,l,'B')*1000
        out.update({'Incoming at 750nm FLUO [W m-2nm-1sr-1]':ein750,'Reflected 750nm FLUO [W m-2nm-1sr-1]':lin750,'E_stability FLUO [%]':est,'sat value L FLUO':(np.nanmax(raw.l,axis=0)>=200000).astype(int),'sat value E FLUO':(np.nanmax(raw.e,axis=0)>=200000).astype(int),'sat value E2 FLUO':(np.nanmax(raw.e2,axis=0)>=200000).astype(int),'Dynamic range E FLUO [%]':np.nanmax(raw.e,axis=0)*100/200000,'Dynamic range L FLUO [%]':np.nanmax(raw.l,axis=0)*100/200000,'SIF_A_ifld [mW m-2nm-1sr-1]':sif_a,'SIF_B_ifld [mW m-2nm-1sr-1]':sif_b})
    out.update(idx)
    # Whether the AirFloX's own receiver gave a position at all. On Flight_2707
    # it reports 0.00000 for every row, so the solar zenith angle above was
    # computed at latitude 0, longitude 0 and read 101 degrees at 05:20 UTC
    # instead of 75. process_to_files() recomputes it from the Noseboom position
    # once the telemetry match has supplied one.
    # One row has to be finite and non-zero at once. Testing the two separately
    # let NaN answer the second: an unreadable coordinate is not equal to zero,
    # so Flight_CC0807's single truncated block made a file of 1 310 zeroes look
    # as though the receiver had a position. The recomputation never ran and the
    # solar zenith angle, taken at latitude 0, reached 156 degrees at midday.
    position_usable=bool(np.any(np.isfinite(lat)&np.isfinite(lon)
                                &((lat!=0)|(lon!=0))))
    return {'out':pd.DataFrame(out),'wl':wl,'E':e,'L':l,'Ref':ref,
            'utc_base':list(base),'position_usable':position_usable}
def process_full(raw,cal,idx,apply_nonlinearity_correction=False,spectral_shift_correction=False,retain_zero_gps=False): return process_common(raw,read_full_calibration(cal),idx,'FULL',apply_nonlinearity_correction,spectral_shift_correction,retain_zero_gps)
def process_fluo(raw,cal,idx,apply_nonlinearity_correction=False,spectral_shift_correction=False,retain_zero_gps=False): return process_common(raw,read_full_calibration(cal),idx,'FLUO',apply_nonlinearity_correction,False,retain_zero_gps)

def match_data(air,log):
    """Match AirFloX rows to telemetry exactly like the R MATCH_data() routine.

    R chooses the first telemetry timestamp at or after each AirFloX timestamp,
    overwrites datetime/Lat/Lon, adds Alt, ID, and radius_nocos, then downstream
    processing drops only rows where no future telemetry timestamp exists.
    """
    t=pd.read_csv(log)
    t['_match_time']=pd.to_datetime(t['date_time_utc'],utc=True,errors='coerce')
    # A stable sort, because the telemetry is logged at ~10 Hz but timestamped to
    # the whole second: R's which.min() picks the first row of that second in file
    # order, and an unstable sort picked an arbitrary one of the ten. Within a
    # single second the log moves up to 2.5 m in altitude, so the choice showed up
    # directly in Alt, Lat, Lon and the footprint radius.
    t=t.dropna(subset=['_match_time']).sort_values('_match_time',kind='stable').reset_index(drop=True)
    if t.empty: raise ValueError(f'No valid date_time_utc rows found in log: {log}')
    rt=t['_match_time'].dt.tz_convert(None).to_numpy(dtype='datetime64[ns]')
    # R compares the AirFloX time at full precision: bf <- as.numeric(floxtime),
    # where floxtime is UTC_time + CPU1sec and always carries a fractional part.
    # Flooring it to the second matched the telemetry sample one second early.
    air_time=pd.to_datetime(air['datetime [UTC]'],utc=True,errors='coerce').dt.tz_convert(None)
    ft=air_time.to_numpy(dtype='datetime64[ns]'); valid=~pd.isna(air_time).to_numpy()
    ind=np.searchsorted(rt,ft,side='left')
    matched=valid & (ind < len(rt))
    m=air.copy()
    m.loc[:,'datetime [UTC]']=pd.NaT; m.loc[:,'Lat']=np.nan; m.loc[:,'Lon']=np.nan; m.loc[:,'Alt']=np.nan
    m.loc[matched,'datetime [UTC]']=pd.to_datetime(rt[ind[matched]])
    m.loc[matched,'Lat']=pd.to_numeric(t['lat'],errors='coerce').to_numpy(float)[ind[matched]]
    m.loc[matched,'Lon']=pd.to_numeric(t['lon'],errors='coerce').to_numpy(float)[ind[matched]]
    alt_col='alt_above_ground_m' if 'alt_above_ground_m' in t.columns else 'Alt'
    if alt_col in t.columns: m.loc[matched,'Alt']=pd.to_numeric(t[alt_col],errors='coerce').to_numpy(float)[ind[matched]]
    m['ID']=np.arange(1,len(m)+1)
    m['radius_nocos']=pd.to_numeric(m['Alt'],errors='coerce')*math.tan(math.radians(11.5))
    if matched.any():
        gaps=np.abs((rt[ind[matched]]-ft[matched])/np.timedelta64(1,'s'))
        if np.nanmax(gaps)>10:
            print(f'warning=Telemetry matching has max future gap {np.nanmax(gaps):.1f} sec. Check log coverage if unexpected.')
    if (~matched).any(): print(f'warning=Telemetry matching dropped {int((~matched).sum())} AirFloX row(s) after the last telemetry timestamp.')
    return m,matched
def apply_static_position(air,static_lat=None,static_lon=None,static_alt=None):
    m=air.copy(); m['ID']=np.arange(1,len(m)+1)
    if static_lat is not None: m.loc[:,'Lat']=float(static_lat)
    else: m.loc[:,'Lat']=pd.to_numeric(m.get('Lat'),errors='coerce')
    if static_lon is not None: m.loc[:,'Lon']=float(static_lon)
    else: m.loc[:,'Lon']=pd.to_numeric(m.get('Lon'),errors='coerce')
    if static_alt is not None: m.loc[:,'Alt']=float(static_alt)
    elif 'Alt' not in m.columns: m.loc[:,'Alt']=np.nan
    else: m.loc[:,'Alt']=pd.to_numeric(m['Alt'],errors='coerce')
    valid=m['datetime [UTC]'].notna().to_numpy(); bad_pos=m[['Lat','Lon']].isna().any(axis=1).to_numpy()
    if bad_pos.any(): print(f'warning=Tower/static mode has {int(bad_pos.sum())} row(s) without valid Lat/Lon. Provide static Lat/Lon if AirFloX GPS is missing.')
    return m,valid & ~bad_pos

def unlocked_path(p): return p.with_name(f'{p.stem}_{datetime.now().strftime("%Y%m%d_%H%M%S")}{p.suffix}')
def write_r_table(df,p):
    p=Path(p); p.parent.mkdir(parents=True,exist_ok=True); df=df.copy()
    for i in range(df.shape[1]):
        if np.issubdtype(df.iloc[:,i].dtype,np.datetime64): df[df.columns[i]]=pd.to_datetime(df.iloc[:,i]).dt.strftime('%Y-%m-%d %H:%M:%S').astype(object)
    try:
        df.to_csv(p,sep=';',index=False,na_rep='#N/D',quoting=csv.QUOTE_NONNUMERIC); return p
    except PermissionError:
        alt=unlocked_path(p); print(f'warning=Output file is locked, writing alternate file: {alt}'); df.to_csv(alt,sep=';',index=False,na_rep='#N/D',quoting=csv.QUOTE_NONNUMERIC); return alt
def spectrum_columns(times): return [pd.Timestamp(t).strftime('%H_%M_%S') for t in times]
def write_spectra(wl,sp,valid,times,p):
    cols=spectrum_columns(pd.Series(times)[valid]); df=pd.DataFrame(sp[:,valid],columns=cols); df.insert(0,'WL',wl); return write_r_table(df,p)

def gis_columns(m):
    # R GUI writes compact GIS attributes; keep indices too so the Python map can color NDVI/PRI/EVI directly.
    d=m.copy()
    rename={}
    if 'Alt' in d.columns: rename['Alt']='Heigh'
    if 'radius_nocos' in d.columns: rename['radius_nocos']='Radius'
    if 'SIF_A_ifld [mW m-2nm-1sr-1]' in d.columns: rename['SIF_A_ifld [mW m-2nm-1sr-1]']='SIF A iFLD'
    if 'SIF_B_ifld [mW m-2nm-1sr-1]' in d.columns: rename['SIF_B_ifld [mW m-2nm-1sr-1]']='SIF B iFLD'
    d=d.rename(columns=rename)
    base=['ID','datetime [UTC]','SZA','Lat','Lon','Heigh','Radius']
    if 'SIF A iFLD' in d.columns: base+=['SIF A iFLD','SIF B iFLD']
    elif 'PAR ref [W m-2]' in d.columns: base+=['PAR ref [W m-2]']
    base += ['NDVI','PRI','EVI','MTCI','SR','REP','TCARI','REDCl','MCRI']
    keep=[c for c in base if c in d.columns]
    return d[keep].copy()
def write_dbf(path,df):
    fields=[]
    for c in df.columns:
        # Lat and Lon stay in the table as well as in the geometry. R writes
        # them (Write_shape: names(out) includes "Lat","Lon"), and dropping
        # them left anything that reads the DBF alone - a join, a spreadsheet -
        # with no position at all.
        is_num=pd.api.types.is_numeric_dtype(df[c])
        # ID is a row counter; R writes it with no decimals and a reader that
        # expects an integer key should get one.
        dec=0 if (is_num and c=='ID') else (8 if is_num else 0)
        fields.append((c[:10], 'N' if is_num else 'C', 18 if is_num else 40, dec, c, is_num))
    n=len(df); hlen=32+32*len(fields)+1; rlen=1+sum(f[2] for f in fields)
    with path.open('wb') as f:
        f.write(struct.pack('<BBBBLHH20x',3,datetime.now().year-1900,datetime.now().month,datetime.now().day,n,hlen,rlen))
        for name,typ,w,dec,orig,isnum in fields: f.write(name.encode('ascii','ignore').ljust(11,b'\0')+typ.encode()+b'\0'*4+bytes([w,dec])+b'\0'*14)
        f.write(b'\r')
        for _,r in df.iterrows():
            f.write(b' ')
            for name,typ,w,dec,orig,isnum in fields:
                val=r[orig]
                if pd.isna(val): s=''
                elif isnum: s=f'{float(val):.{dec}f}'
                else: s=str(val)[:w]
                f.write(s.rjust(w).encode('ascii','ignore') if isnum else s.ljust(w).encode('utf-8','ignore'))
        f.write(b'\x1a')
def write_point_shapefile(base,df):
    base=Path(base); base.parent.mkdir(parents=True,exist_ok=True)
    def write_set(b):
        d=df.dropna(subset=['Lat','Lon']).copy(); shp=b.with_suffix('.shp'); shx=b.with_suffix('.shx'); dbf=b.with_suffix('.dbf'); prj=b.with_suffix('.prj'); cpg=b.with_suffix('.cpg')
        if d.empty: print(f'warning=No valid Lat/Lon rows for GIS export: {b}'); return []
        lon=pd.to_numeric(d['Lon'],errors='coerce').to_numpy(float); lat=pd.to_numeric(d['Lat'],errors='coerce').to_numpy(float)
        xmin,xmax,ymin,ymax=lon.min(),lon.max(),lat.min(),lat.max(); shp_words=50+len(d)*14; shx_words=50+len(d)*4
        def hdr(f,words): f.write(struct.pack('>6i',9994,0,0,0,0,0)+struct.pack('>i',words)+struct.pack('<2i4d4d',1000,1,xmin,ymin,xmax,ymax,0,0,0,0))
        with shp.open('wb') as a, shx.open('wb') as x:
            hdr(a,shp_words); hdr(x,shx_words); off=50
            for n,(xx,yy) in enumerate(zip(lon,lat),1): a.write(struct.pack('>2i',n,10)+struct.pack('<i2d',1,float(xx),float(yy))); x.write(struct.pack('>2i',off,10)); off+=14
        write_dbf(dbf,d); prj.write_text('GEOGCS["GCS_unknown",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]',encoding='ascii'); cpg.write_text('UTF-8',encoding='ascii'); return [shp,shx,dbf,prj,cpg]
    try: return write_set(base)
    except PermissionError:
        alt=base.with_name(f'{base.name}_{datetime.now().strftime("%Y%m%d_%H%M%S")}'); print(f'warning=GIS file is locked, writing alternate GIS set: {alt}'); return write_set(alt)
def export_gis(m,raw,out): return write_point_shapefile(out/'GIS'/f'AIRFLOX_{raw.stem}',gis_columns(m))
def process_to_files(raw,log,cal,idx,out,mode,apply_nl=False,position_source='uav_airship',static_lat=None,static_lon=None,static_alt=None,spectral_shift_correction=False,time_start_utc=None,time_end_utc=None):
    retain_zero_gps=(position_source!='tower')
    r=process_full(raw,cal,idx,apply_nl,spectral_shift_correction,retain_zero_gps) if mode=='FULL' else process_fluo(raw,cal,idx,apply_nl,False,retain_zero_gps)
    if position_source=='tower':
        m,v=apply_static_position(r['out'],static_lat=static_lat,static_lon=static_lon,static_alt=static_alt)
    else:
        if log is None: raise ValueError('UAV/Airship mode requires a HATCH-BOX/custom SIF log.')
        m,v=match_data(r['out'],log)
        if not r.get('position_usable',True):
            # The AirFloX had no fix, so the solar zenith angle was computed at
            # latitude 0, longitude 0. Redo it now that the Noseboom has supplied
            # the real position, using the same acquisition times as before.
            base=r.get('utc_base')
            if base is not None and len(base)==len(m):
                m.loc[:,'SZA']=zenith(base,
                                      pd.to_numeric(m['Lon'],errors='coerce').to_numpy(float),
                                      pd.to_numeric(m['Lat'],errors='coerce').to_numpy(float))
                print(f'{mode} solar zenith angle recomputed from the Noseboom position '
                      '(the AirFloX reported no GPS fix).')
        kept=int(np.count_nonzero(v)); total=len(v)
        print(f'{mode} telemetry match: kept {kept}/{total} row(s).')
        m=m.loc[v].reset_index(drop=True)
        r['E']=r['E'][:,v]; r['L']=r['L'][:,v]; r['Ref']=r['Ref'][:,v]
        v=np.ones(len(m),dtype=bool)
    m,r=apply_time_window(m,r,time_start_utc,time_end_utc)
    v=np.ones(len(m),dtype=bool)
    out.mkdir(parents=True,exist_ok=True); stem=raw.stem
    targets=[out/f'Incoming_radiance_{mode}_{stem}.csv',out/f'Reflected_radiance_{mode}_{stem}.csv',out/f'Reflectance_{mode}_{stem}.csv',out/f'ALL_INDEX_AIRFLOX_{mode}_{stem}.csv']
    paths=[]; paths.append(write_spectra(r['wl'],r['E'],v,m['datetime [UTC]'],targets[0])); paths.append(write_spectra(r['wl'],r['L'],v,m['datetime [UTC]'],targets[1])); paths.append(write_spectra(r['wl'],r['Ref'],v,m['datetime [UTC]'],targets[2])); paths.append(write_r_table(m,targets[3])); paths+=export_gis(m,raw,out); return paths

def probe_record_clock_offset(raw_path,calibration_path,mode):
    """Measure the record-clock offset from one AirFloX channel, if it has a fix.

    Cheap enough to run before processing: the raw files are a few hundred KB.
    Returns (offset_seconds, fixes, total) or (None, 0, 0).
    """
    try:
        cal=read_full_calibration(calibration_path)
        raw=read_drox_full(raw_path,len(cal['wl']),drop_e500_zero=(mode=='FLUO'),drop_zero_gps=False)
    except Exception as exc:
        print(f'warning=Could not probe the AirFloX record clock in {raw_path.name}: {exc}')
        return None,0,0
    gps_time=[('111111' if str(v).strip() in {'no GPS fix','no GPS  fix','no GPS   fix','0','0.0'} else v) for v in raw.gps_time]
    gps_date=[('111111' if len(str(v).strip().split('.')[0]) not in (5,6) else v) for v in raw.gps_date]
    utc=[_r_datetime(t,d) for t,d in zip(gps_time,gps_date)]
    record=[_record_clock_datetime(t,d) for t,d in zip(raw.time,raw.date)]
    offset,fixes,_spread=measure_record_clock_offset(utc,record,raw)
    return offset,fixes,len(record)

def resolve_flight_root(directory,flight_name):
    directory=Path(directory)
    flight_name=str(flight_name).strip()
    if directory.name.lower()==flight_name.lower():
        return directory
    candidate=directory/flight_name
    return candidate if candidate.exists() else candidate

def folder_has_named_child(root,names):
    names={str(n).upper() for n in names}
    try:
        return any(p.is_dir() and p.name.upper() in names for p in Path(root).iterdir())
    except FileNotFoundError:
        return False

def find_floxinside(directory,flight_name):
    root=resolve_flight_root(directory,flight_name)
    if not root.exists():
        raise FileNotFoundError(f'Flight folder does not exist: {root}')
    c=sorted([p for p in root.rglob('*') if p.is_dir() and 'FLOXINSIDE' in p.name.upper()],key=lambda x:(len(x.parts),str(x)))
    if c:
        return c[0]
    # Some campaign exports are already rooted at the AirFloX folder and contain FULL/FLOX/FLUO directly.
    candidates=[root]+sorted([p for p in root.rglob('*') if p.is_dir()],key=lambda x:(len(x.parts),str(x)))
    for candidate in candidates:
        if folder_has_named_child(candidate,{'FULL'}) and folder_has_named_child(candidate,{'FLOX','FLUO'}):
            print(f'warning=No FLOXINSIDE folder found below {root}; using AirFloX data folder {candidate}')
            return candidate
    raise FileNotFoundError(f'No FLOXINSIDE folder or direct FULL+FLOX/FLUO data folders below {root}')
def find_named_folder(root,names):
    names={n.upper() for n in names}; m=[p for p in Path(root).rglob('*') if p.is_dir() and p.name.upper() in names]
    if not m: raise FileNotFoundError(f'No folder {names} below {root}')
    return m[0]
def raw_files(folder,mode,min_kb=100):
    fs=sorted(set(folder.rglob('*.CSV'))|set(folder.rglob('*.csv')))
    fs=[p for p in fs if p.name.upper().startswith('F')] if mode=='FULL' else [p for p in fs if not p.name.upper().startswith('F')]
    min_kb=max(0,float(min_kb))
    kept=[]; skipped=[]
    for p in fs:
        size_kb=p.stat().st_size/1024
        if size_kb>=min_kb: kept.append(p)
        else: skipped.append((p,size_kb))
    if skipped:
        print(f'warning=Skipped {len(skipped)} {mode} raw file(s) smaller than {min_kb:g} KB.')
        for p,size in skipped[:12]: print(f'  skipped_raw={p} size_kb={size:.1f}')
    return kept
def concat(files,dst):
    if not files: raise FileNotFoundError(dst)
    dst.parent.mkdir(parents=True,exist_ok=True)
    if len(files)==1: shutil.copyfile(files[0],dst); return dst
    with dst.open('wb') as o:
        for p in files:
            with Path(p).open('rb') as f: shutil.copyfileobj(f,o)
            o.write(b'\n')
    return dst
def detect_essential_file(e,mode):
    c=sorted(p for p in Path(e).glob('*.csv') if 'CAL' in p.name.upper() and mode in p.name.upper()); idx=sorted(Path(e).glob('*.txt'))
    if not c: raise FileNotFoundError(f'No {mode} calibration file found in {e}')
    if not idx: raise FileNotFoundError(f'No index .txt file found in {e}')
    return c[0],idx[0]
def maybe_float(x):
    if x is None: return None
    s=str(x).strip()
    if not s: return None
    return float(s)
def run_flight(args):
    progress=getattr(args,'progress_callback',None)
    def step(key,status='running',message=''):
        if progress:
            try: progress(key,status,message)
            except Exception: pass
    step('setup','running','Resolving flight folders and options')
    directory=Path(args.directory); root=resolve_flight_root(directory,args.flight_name); flox=find_floxinside(directory,args.flight_name); out=Path(args.output)/args.flight_name; apply_nl=yes_no(args.apply_nonlinearity_correction)
    spectral_shift=yes_no(getattr(args,'spectral_shift_correction',SPYDER_SPECTRAL_SHIFT_CORRECTION)); raw_min_kb=float(getattr(args,'raw_min_kb',SPYDER_RAW_MIN_KB))
    if raw_min_kb>200: print(f'warning=Raw minimum size is {raw_min_kb:g} KB. This is high and may drop valid AirFloX data.')
    time_filter=str(getattr(args,'time_filter',SPYDER_TIME_FILTER)).lower(); time_start=getattr(args,'time_start_utc',SPYDER_TIME_START_UTC) if time_filter=='custom' else None; time_end=getattr(args,'time_end_utc',SPYDER_TIME_END_UTC) if time_filter=='custom' else None
    position_source=getattr(args,'platform_mode',SPYDER_PLATFORM_MODE)
    position_source='tower' if str(position_source).lower().startswith('tower') else 'uav_airship'
    static_lat=maybe_float(getattr(args,'static_lat',SPYDER_STATIC_LAT)); static_lon=maybe_float(getattr(args,'static_lon',SPYDER_STATIC_LON)); static_alt=maybe_float(getattr(args,'static_alt',SPYDER_STATIC_ALT))
    step('setup','done',f'Flight folder: {flox}')
    log=None
    step('position','running','Preparing position source')
    if position_source=='uav_airship':
        log=prepare_sif_log_from_hatchbox(root,out,custom_log=args.log,altitude_filter=yes_no(args.altitude_filter)); print(f'Position mode: UAV/Airship\nFlight folder: {flox}\nGimbal/SIF log: {log}\nOutput folder: {out}')
        step('position','done',f'Gimbal/SIF log: {log}')
    else:
        if getattr(args,'log',None): print('warning=Tower mode selected; custom SIF log is ignored. AirFloX/static coordinates are used.')
        print(f'Position mode: Tower/static\nFlight folder: {flox}\nOutput folder: {out}')
        if static_lat is not None and static_lon is not None: print(f'Static coordinates: lat={static_lat}, lon={static_lon}, alt={static_alt}')
        else: print('Static coordinates: using AirFloX GPS Lat/Lon from raw file.')
        step('position','done','Tower/static position mode ready')
    made=[]
    step('full_raw','running','Finding and combining FULL raw files')
    full_folder=find_named_folder(flox,{'FULL'}); full=raw_files(full_folder,'FULL',raw_min_kb)
    if not full: raise FileNotFoundError(f'warning=No FULL raw files matched in {full_folder}')
    fraw=concat(full,out/'_combined'/f'{args.flight_name}_FULL.CSV'); fcal,idx=detect_essential_file(args.essentials,'FULL')
    step('full_raw','done',f'{len(full)} FULL raw file(s) combined')
    # Establish the record-clock offset before anything is processed. FULL is
    # written first but on Flight_2707 it is FLUO that has the fix, so probing
    # only the channel about to run would leave FULL two hours out and drop most
    # of it during telemetry matching.
    RECORD_CLOCK_OFFSET_HINT.clear()
    try: fluo_folder_probe=find_named_folder(flox,{'FLOX','FLUO'}); fluo_probe=raw_files(fluo_folder_probe,'FLUO',raw_min_kb)
    except FileNotFoundError: fluo_probe=[]
    probe_sources=[(fraw,fcal,'FULL')]
    if fluo_probe:
        fluo_raw_probe=concat(fluo_probe,out/'_combined'/f'{args.flight_name}_FLUO.CSV')
        fluo_cal_probe,_=detect_essential_file(args.essentials,'FLUO')
        probe_sources.append((fluo_raw_probe,fluo_cal_probe,'FLUO'))
    for path,calibration,channel in probe_sources:
        offset,fixes,total=probe_record_clock_offset(path,calibration,channel)
        if offset is not None and fixes:
            RECORD_CLOCK_OFFSET_HINT['seconds']=offset
            print(f'AirFloX record clock: {channel} has a GPS fix on {fixes}/{total} spectra; '
                  f'the record clock runs {offset:.1f} s ({offset/3600:.3f} h) ahead of UTC.')
            break
    step('full_process','running','Calculating FULL radiance, reflectance, indices and GIS')
    made+=process_to_files(fraw,log,fcal,idx,out/'FLOX','FULL',apply_nl,position_source,static_lat,static_lon,static_alt,spectral_shift,time_start,time_end)
    step('full_process','done','FULL processing and export complete')
    step('fluo_raw','running','Finding and combining FLUO raw files')
    try: fluo_folder=find_named_folder(flox,{'FLOX','FLUO'}); fluo=raw_files(fluo_folder,'FLUO',raw_min_kb)
    except FileNotFoundError: fluo=[]
    if fluo:
        raw=concat(fluo,out/'_combined'/f'{args.flight_name}_FLUO.CSV'); cal,idx=detect_essential_file(args.essentials,'FLUO')
        step('fluo_raw','done',f'{len(fluo)} FLUO raw file(s) combined')
        step('fluo_process','running','Calculating FLUO radiance, SIF iFLD, indices and GIS')
        made+=process_to_files(raw,log,cal,idx,out/'FLUO','FLUO',apply_nl,position_source,static_lat,static_lon,static_alt,False,time_start,time_end)
        step('fluo_process','done','FLUO processing and export complete')
    else:
        print('warning=No FLUO/FLOX raw files found; FULL processing completed only.')
        step('fluo_raw','warning','No FLUO/FLOX raw files found')
        step('fluo_process','warning','Skipped FLUO processing')
    step('finalize','done',f'{len(made)} output file(s) written')
    return made

def build_parser():
    p=argparse.ArgumentParser(); p.add_argument('--directory',type=Path,default=SPYDER_DIRECTORY); p.add_argument('--flight-name',default=SPYDER_FLIGHT_NAME); p.add_argument('--output',type=Path,default=SPYDER_OUTPUT_DIR); p.add_argument('--essentials',type=Path,default=SPYDER_ESSENTIALS_DIR); p.add_argument('--log',type=Path,default=SPYDER_LOG); p.add_argument('--altitude-filter',default=SPYDER_ALTITUDE_FILTER); p.add_argument('--apply-nonlinearity-correction',default=SPYDER_APPLY_NONLINEARITY_CORRECTION); p.add_argument('--spectral-shift-correction',default=SPYDER_SPECTRAL_SHIFT_CORRECTION); p.add_argument('--raw-min-kb',type=float,default=SPYDER_RAW_MIN_KB); p.add_argument('--time-filter',choices=['default','custom'],default=SPYDER_TIME_FILTER); p.add_argument('--time-start-utc',default=SPYDER_TIME_START_UTC); p.add_argument('--time-end-utc',default=SPYDER_TIME_END_UTC); p.add_argument('--platform-mode',choices=['uav_airship','tower'],default=SPYDER_PLATFORM_MODE); p.add_argument('--static-lat',default=SPYDER_STATIC_LAT); p.add_argument('--static-lon',default=SPYDER_STATIC_LON); p.add_argument('--static-alt',default=SPYDER_STATIC_ALT); p.set_defaults(func=run_flight); return p
def spyder_argv(): return []
def main():
    p=build_parser(); a=p.parse_args(spyder_argv() if RUN_FROM_SPYDER and len(sys.argv)==1 else None); r=a.func(a)
    if isinstance(r,list):
        for x in r: print(x)
if __name__=='__main__': main()