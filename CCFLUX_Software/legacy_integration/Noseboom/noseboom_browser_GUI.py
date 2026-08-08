
from __future__ import annotations
import json, math, os, queue, re, subprocess, threading, time, webbrowser, urllib.request, io, tempfile, sys
import socket
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from html import escape

import numpy as np
import pandas as pd
from PIL import Image

HOST='127.0.0.1'; PORT=8765
DEFAULT_FLIGHT_ROOT=os.environ.get('NOSEBOOM_FLIGHT_ROOT', '')
DEFAULT_OUTPUT_ROOT=os.environ.get('NOSEBOOM_OUTPUT_ROOT', '')
MIN_FILE_SIZE_MB=2.0; CHUNKSIZE=100_000; MAX_MAP_POINTS=7000
TERRAIN_ZOOM=12
TERRAIN_TILE_URL='https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'

FIELDS={
 'time_ns':'Airflow_UTCcorr_Nanoseconds_ns','time':'TIMESTAMP',
 'lat':'INS_Filter_LLHPos_Latitude_deg','lon':'INS_Filter_LLHPos_Longitude_deg',
 'gnss_lat':'GNSSRecv1_LLHPos_Latitude_deg','gnss_lon':'GNSSRecv1_LLHPos_Longitude_deg',
 'alt_msl_m':'GNSSRecv1_LLHPos_MSLHeight_m','height_m':'INS_Filter_LLHPos_ElipsoidHeight_m',
 'ground_speed_mps':'GNSSRecv1_vNED_GroundSpeed_m/s','heading_deg':'GNSSRecv1_vNED_Heading_deg',
 'roll_rad':'INS_Filter_EulerAngles_Roll_rad','down_mps':'GNSSRecv1_vNED_z_m/s',
 'wind_mps':'WIND_vWind_m/s','wind_u_mps':'WIND_vWind_x_m/s','wind_v_mps':'WIND_vWind_y_m/s','wind_w_mps':'WIND_vWind_z_m/s',
 'wind_dir_deg':'WIND_dir_deg','air_temp_degC':'Airflow_Flow_OAT_degC','rel_humidity_pct':'Airflow_Flow_rel_humidity_',
 'pressure_hpa':'Airflow_Sensor_pstat_hPa'}

# Some Noseboom exports carry every column behind a 'NoseBoom_' prefix and some
# do not, depending on how the logger was configured. The names above are the
# unprefixed form; the prefix is stripped on the way in so both files read
# identically and no calculation below needs to know which kind it was given.
NOSEBOOM_COLUMN_PREFIX='NoseBoom_'

def normalize_column_name(column: str) -> str:
    return str(column).removeprefix(NOSEBOOM_COLUMN_PREFIX)

def normalized_column_map(columns):
    """Original column name -> normalized name, refusing an ambiguous header.

    A file holding both 'NoseBoom_WIND_vWind_x_m/s' and 'WIND_vWind_x_m/s'
    collapses them onto one name, and there is no way to tell which of the two
    the science should use. That is reported here, against the header, rather
    than discovered after the rows have been read.
    """
    mapping={}; sources={}
    for column in columns:
        normalized=normalize_column_name(column)
        sources.setdefault(normalized,[]).append(str(column))
        mapping[column]=normalized
    duplicates={name:found for name,found in sources.items() if len(found)>1}
    if duplicates:
        detail='; '.join(f"{name} (from {' and '.join(found)})" for name,found in sorted(duplicates.items()))
        raise ValueError(
            'Duplicate Noseboom column name(s) after removing the '
            f'{NOSEBOOM_COLUMN_PREFIX!r} prefix: {detail}. '
            'Keep only the prefixed or only the unprefixed copy of each column.'
        )
    return mapping


# Noseboom CSVs are not always UTF-8: acquisition software on Windows writes
# headers in cp1252, where a degree sign is the single byte 0xb0 that UTF-8
# rejects outright. The encoding is decided from the head of the file, which is
# where a unit in a column name lives.
TEXT_PROBE_BYTES=1<<20
def detect_encoding(path):
    try:
        with Path(path).open('rb') as probe: head=probe.read(TEXT_PROBE_BYTES)
    except OSError: return 'utf-8-sig'
    try: head.decode('utf-8')
    except UnicodeDecodeError: return 'cp1252'   # no invalid bytes; 0xb0 is the degree sign
    return 'utf-8-sig'

def safe_name(s): return ''.join(c if c.isalnum() or c in '-_' else '_' for c in ((s or '').strip() or 'Flight'))
def natural_key(p): return [int(x) if x.isdigit() else x.lower() for x in re.split(r'(\d+)', p.name)]
def looks_like_noseboom(p): return 'noseboom' in re.sub(r'[\s_\-]+','',str(p).lower())
def is_data_file(p): return p.is_file() and p.suffix.lower()=='.csv' and p.stat().st_size > MIN_FILE_SIZE_MB*1024**2

@dataclass
class DetectedData:
    files:list[Path]; data_folder:Path|None; message:str; flight_name:str

class AppState:
    def __init__(self):
        self.detected=None; self.data=None; self.export_data=None; self.d1=None; self.straight=None; self.freq=None; self.spectra={}; self.summary={}; self.payload_cache=None; self.project_path=None; self.last_export=None; self.status={'busy':False,'percent':0,'message':'Ready'}; self.logs=[]; self.output=Path(DEFAULT_OUTPUT_ROOT); self.flight_name=''; self.server=None
STATE=AppState()

def add_log(level, msg):
    try:
        STATE.logs.append({'time':pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S'), 'level':str(level).upper(), 'message':str(msg)})
        if len(STATE.logs)>1200:
            STATE.logs=STATE.logs[-1200:]
    except Exception:
        pass

def set_status(p,msg,busy=True):
    STATE.status={'busy':busy,'percent':float(p),'message':msg}
    level='ERROR' if 'failed' in str(msg).lower() or 'error' in str(msg).lower() else ('BUSY' if busy else 'INFO')
    add_log(level, msg)

def logs_to_df(logs=None):
    rows=[]
    for x in (logs if logs is not None else STATE.logs):
        rows.append({'time':str(x.get('time','')), 'level':str(x.get('level','INFO')).upper(), 'message':str(x.get('message',''))})
    return pd.DataFrame(rows, columns=['time','level','message'])

def logs_from_store(store):
    try:
        if '/session_logs' in store.keys():
            df=store['session_logs']
            return [{'time':str(r.time), 'level':str(r.level), 'message':str(r.message)} for r in df.itertuples(index=False)]
    except Exception:
        pass
    return []

def flush_project_logs(path=None):
    target=Path(path) if path else (Path(STATE.project_path) if STATE.project_path else None)
    if target is None or not target.exists():
        return False
    with pd.HDFStore(target, mode='a', complevel=5, complib='zlib') as store:
        if '/session_logs' in store.keys():
            store.remove('session_logs')
        store.put('session_logs', logs_to_df(), format='table', data_columns=['time','level'])
    return True
def detect_files(flight_root, flight_name):
    base=Path(flight_root)/(flight_name or '')
    if not base.exists(): raise FileNotFoundError(f'Flight folder does not exist: {base}')
    date_dirs=[p for p in base.iterdir() if p.is_dir() and p.name.startswith('2026')]
    if not date_dirs and base.name.startswith('2026'): date_dirs=[base]
    if not date_dirs: date_dirs=[p for p in base.rglob('2026*') if p.is_dir()]
    roots=[]
    for d in sorted(date_dirs,key=natural_key): roots += [p for p in d.rglob('*') if p.is_dir() and p.name.lower()=='influxdb']
    if not roots: roots=[base]
    explicit=[]; fallback=[]
    for r in sorted(roots,key=natural_key):
        for f in r.rglob('*.csv'):
            if is_data_file(f):
                fallback.append(f)
                if looks_like_noseboom(f) or looks_like_noseboom(f.parent): explicit.append(f)
    files=sorted(set(explicit or fallback),key=natural_key)
    if not files: raise FileNotFoundError(f'No > {MIN_FILE_SIZE_MB:g} MB Noseboom CSV found below {base}')
    mode='explicit Noseboom CSV' if explicit else 'fallback large CSV'
    return DetectedData(files,files[0].parent,f'Detected {len(files)} CSV file(s) using {mode}. First folder: {files[0].parent}', base.name or Path(flight_root).name)

def csv_usecols(path):
    header=pd.read_csv(path,nrows=0,encoding=detect_encoding(path))
    mapping=normalized_column_map(header.columns)
    wanted=set(FIELDS.values())|set(FIELDS)
    # usecols must name the columns as the file spells them, so the original
    # name is kept here and the prefix is dropped once the rows are in.
    cols=sorted({original for original,normalized in mapping.items() if normalized in wanted})
    if not cols: raise ValueError(f'No required Noseboom columns found in {path.name}')
    return cols

def simplify(raw,src):
    out=pd.DataFrame(index=raw.index)
    for simple,source in FIELDS.items(): out[simple]=raw[source] if source in raw.columns else (raw[simple] if simple in raw.columns else np.nan)
    out['_source_csv']=src; return out
def count_rows(path, base=0, span=10):
    total=max(path.stat().st_size,1); rows=0; read=0
    with path.open('rb') as h:
        while True:
            b=h.read(8*1024*1024)
            if not b: break
            read+=len(b); rows+=b.count(b'\n'); set_status(base+span*min(read/total,1),f'Indexing {path.name}')
    return max(rows-1,0)

def load_csv_files(files):
    counts=[count_rows(p,i*10/len(files),10/len(files)) for i,p in enumerate(files)]
    total=max(sum(counts),1); loaded=0; chunks=[]
    for p in files:
        for raw in pd.read_csv(p,usecols=csv_usecols(p),encoding=detect_encoding(p),low_memory=False,chunksize=CHUNKSIZE):
            raw=raw.rename(columns=normalize_column_name)
            chunks.append(simplify(raw,p.name)); loaded+=len(raw); set_status(10+90*min(loaded/total,1),f'Loading rows {loaded:,}/{total:,}')
    data=pd.concat(chunks,ignore_index=True)
    data['time']=pd.to_datetime(data['time'],errors='coerce')
    if data['time'].isna().all(): data['time']=pd.to_datetime(pd.to_numeric(data['time_ns'],errors='coerce'),unit='ns',errors='coerce')
    for c in data.columns:
        if c not in ('time','_source_csv'): data[c]=pd.to_numeric(data[c],errors='coerce')
    data['time_ns']=data['time_ns'].fillna(-1).astype(np.int64)
    data['plot_lat']=data['lat'].where(data['lat'].between(-90,90),data['gnss_lat'])
    data['plot_lon']=data['lon'].where(data['lon'].between(-180,180),data['gnss_lon'])
    data['altitude_m']=data['alt_msl_m'].where(np.isfinite(data['alt_msl_m']),data['height_m'])
    data['vertical_speed_mps']=-data['down_mps']; data['roll_deg']=np.rad2deg(data['roll_rad'])
    valid=data['time'].notna() & data['plot_lat'].between(-90,90) & data['plot_lon'].between(-180,180)
    return data.loc[valid].sort_values(['time_ns','time']).drop_duplicates('time_ns').reset_index(drop=True)

def circular_mean_deg(values):
    a=np.deg2rad(values.dropna().to_numpy(float)); return float(np.rad2deg(np.arctan2(np.sin(a).mean(),np.cos(a).mean()))%360) if len(a) else np.nan
def circular_difference_deg(a,periods=1): return ((a.diff(periods)+180)%360)-180
def haversine_m(lat1, lon1, lat2, lon2):
    r=6371000.0; p1=np.deg2rad(lat1); p2=np.deg2rad(lat2); dp=p2-p1; dl=np.deg2rad(lon2-lon1)
    a=np.sin(dp/2)**2+np.cos(p1)*np.cos(p2)*np.sin(dl/2)**2
    return 2*r*np.arcsin(np.sqrt(np.clip(a,0,1)))

STRAIGHT_DEFAULTS={'minimum_ground_speed_mps':8.0,'minimum_segment_duration_s':60,'heading_window_s':30,'maximum_heading_std_deg':10.0,'maximum_heading_rate_dps':3.0,'maximum_roll_angle_deg':10.0,'maximum_altitude_range_m':100.0,'maximum_vertical_speed_mps':2.2}

NAVIGATION_COLUMNS=['plot_lat','plot_lon','altitude_m','height_m','ground_speed_mps','vertical_speed_mps','roll_deg','wind_mps','wind_u_mps','wind_v_mps','wind_w_mps','air_temp_degC','rel_humidity_pct']
# Angles, which cannot be averaged the way the rest are: the median of 359 and 1
# is 180, pointing south for a wind blowing from the north. Both are resampled
# through the unit circle instead, and neither belongs in NAVIGATION_COLUMNS
# because that list is passed straight to .median().
CIRCULAR_NAVIGATION_COLUMNS=['heading_deg','wind_dir_deg']

def interpolate_circular_deg(values, limit=2):
    """Fill short gaps in a bearing without going the long way round.

    Interpolating 359 to 1 in degrees passes through 180, so a wind from the
    north is briefly recorded as a wind from the south. The components are
    interpolated instead and the angle recovered from them.
    """
    radians=np.deg2rad(values.astype(float))
    across=np.sin(radians).interpolate(limit=limit)
    along=np.cos(radians).interpolate(limit=limit)
    return np.rad2deg(np.arctan2(across,along))%360

def resample_navigation(data, rule='1s'):
    """Navigation columns on a fixed grid; angles averaged through north."""
    x=data.set_index('time')
    out=x[[c for c in NAVIGATION_COLUMNS if c in x.columns]].resample(rule).median()
    out=out.interpolate(limit=2)
    for column in CIRCULAR_NAVIGATION_COLUMNS:
        if column in x.columns:
            out[column]=interpolate_circular_deg(
                x[column].resample(rule).apply(circular_mean_deg)
            )
    out=out.dropna(subset=['plot_lat','plot_lon']); out.index.name='time'; return out

def one_hz(data):
    return resample_navigation(data,'1s')

def heading_std_deg(head, window):
    """Circular standard deviation of heading inside a centred window."""
    rad=np.deg2rad(pd.to_numeric(head,errors='coerce'))
    minimum=max(5,int(window)//3)
    sin_m=pd.Series(np.sin(rad),index=head.index).rolling(window,center=True,min_periods=minimum).mean()
    cos_m=pd.Series(np.cos(rad),index=head.index).rolling(window,center=True,min_periods=minimum).mean()
    r=np.sqrt(sin_m**2+cos_m**2).clip(1e-12,1.0)
    return np.rad2deg(np.sqrt(-2.0*np.log(r)))


def detect_straight(d1, params=None):
    """Classify Straight Flight legs by a moving-window stability test.

    A sample is accepted while the Zeppelin holds forward motion and a
    quasi-steady attitude: ground speed at or above the threshold, circular
    heading standard deviation and heading rate inside their windows, roll and
    vertical speed small, and the altitude range across the window bounded.
    Consecutive accepted samples are merged into one leg, and a leg is kept
    when it lasts at least the minimum duration. Distance comes from
    consecutive latitude and longitude, duration from the recorded time.
    """
    cfg=STRAIGHT_DEFAULTS.copy()
    if params:
        cfg.update({k:float(v) for k,v in params.items() if k in cfg and v not in (None,'')})
    cfg['heading_window_s']=int(max(5,round(cfg['heading_window_s'])))
    cfg['minimum_segment_duration_s']=int(max(5,round(cfg['minimum_segment_duration_s'])))
    d=d1.copy(); idx=d.index
    seconds=idx.to_series().diff().dt.total_seconds().fillna(1.0).clip(lower=0.001)
    window=cfg['heading_window_s']
    minimum=max(5,window//3)
    heading=pd.to_numeric(d['heading_deg'],errors='coerce')
    d['heading_std_deg']=heading_std_deg(heading,window)
    d['heading_rate_dps']=(circular_difference_deg(heading).abs()/seconds)
    altitude=pd.to_numeric(d['altitude_m'],errors='coerce')
    d['altitude_range_m']=(altitude.rolling(window,center=True,min_periods=minimum).max()
                           -altitude.rolling(window,center=True,min_periods=minimum).min())
    vertical=pd.to_numeric(d['vertical_speed_mps'],errors='coerce')
    good=((d['ground_speed_mps']>=cfg['minimum_ground_speed_mps'])
          & (d['heading_std_deg']<=cfg['maximum_heading_std_deg'])
          & (d['heading_rate_dps']<=cfg['maximum_heading_rate_dps'])
          & (d['roll_deg'].abs()<=cfg['maximum_roll_angle_deg'])
          & (d['altitude_range_m']<=cfg['maximum_altitude_range_m'])
          & (vertical.abs()<=cfg['maximum_vertical_speed_mps'])).fillna(False)
    d['straight_candidate']=good
    # A gap in the record ends a leg: samples either side of it were not one
    # continuous transect however similar they look.
    breaks=good.ne(good.shift(fill_value=False)) | (seconds>2)
    run_id=breaks.cumsum()
    d['straight']=False; d['straight_leg_id']=0
    metrics=[]; leg_id=0
    for _run,g in d[good].groupby(run_id[good]):
        duration=float((g.index[-1]-g.index[0]).total_seconds())
        if duration < cfg['minimum_segment_duration_s']:
            continue
        if len(g)>1:
            step=haversine_m(g['plot_lat'].iloc[:-1].to_numpy(),g['plot_lon'].iloc[:-1].to_numpy(),
                             g['plot_lat'].iloc[1:].to_numpy(),g['plot_lon'].iloc[1:].to_numpy())
            distance=float(np.nansum(step))
        else:
            distance=0.0
        leg_id+=1
        d.loc[g.index,'straight']=True; d.loc[g.index,'straight_leg_id']=leg_id
        metrics.append({
            'leg':leg_id,'start':str(g.index[0]),'end':str(g.index[-1]),
            'duration_s':duration,'distance_km':distance/1000.0,
            'mean_heading_deg':circular_mean_deg(g['heading_deg']),
            'mean_speed_mps':float(g['ground_speed_mps'].mean()),
            'mean_wind_speed_mps':float(pd.to_numeric(g['wind_mps'],errors='coerce').mean()),
            'median_heading_std_deg':float(g['heading_std_deg'].median()),
            'max_heading_rate_dps':float(np.nanmax(g['heading_rate_dps'].to_numpy())) if len(g) else float('nan'),
            'max_abs_roll_deg':float(np.nanmax(np.abs(g['roll_deg'].to_numpy()))) if len(g) else float('nan'),
            'altitude_range_m':float(np.nanmax(altitude.loc[g.index])-np.nanmin(altitude.loc[g.index])),
            'max_abs_vertical_speed_mps':float(np.nanmax(np.abs(vertical.loc[g.index].to_numpy()))) if len(g) else float('nan'),
            'center_lat':float(g['plot_lat'].median()),'center_lon':float(g['plot_lon'].median()),
        })
    d.attrs['straight_params']=cfg; d.attrs['straight_metrics']=metrics
    return d


def welch_psd_from_time(data, value_col, trim_mins=2.0, window_seconds=120.0):
    """One-sided Welch PSD using original-resolution noseboom samples."""
    if value_col not in data or 'time_ns' not in data:
        return None
    q=data[['time_ns',value_col]].copy()
    q=q.replace([np.inf,-np.inf],np.nan).dropna()
    q=q[q['time_ns']>0].sort_values('time_ns')
    if len(q)<128:
        return None
    t=q['time_ns'].to_numpy(np.int64)
    trim=int(trim_mins*60*1e9)
    if trim>0:
        ok=(t>=t.min()+trim)&(t<=t.max()-trim)
        q=q.loc[ok]; t=q['time_ns'].to_numpy(np.int64)
    if len(q)<128:
        return None
    dt=np.diff(t); dt=dt[dt>0]
    if len(dt)<32:
        return None
    fs=float(1e9/np.nanmedian(dt))
    y=q[value_col].to_numpy(float)
    y=y[np.isfinite(y)]
    if len(y)<128 or not np.isfinite(fs) or fs<=0:
        return None
    max_n=1048576
    if len(y)>max_n:
        step=int(math.ceil(len(y)/max_n)); y=y[::step]
    target=max(128,int(round(fs*window_seconds)))
    nperseg=int(min(target, 2**int(np.floor(np.log2(len(y))))))
    nperseg=max(128,nperseg)
    noverlap=nperseg//2; step=nperseg-noverlap
    win=np.hanning(nperseg); scale=fs*np.sum(win**2)
    acc=[]
    for start in range(0,len(y)-nperseg+1,step):
        seg=y[start:start+nperseg]
        seg=seg-np.nanmean(seg)
        spec=np.fft.rfft(seg*win)
        psd=(np.abs(spec)**2)/scale
        if len(psd)>2:
            psd[1:-1]*=2.0
        acc.append(psd)
    if not acc:
        return None
    pxx=np.nanmean(np.vstack(acc),axis=0)
    f=np.fft.rfftfreq(nperseg,d=1.0/fs)
    ok=(f>0)&np.isfinite(pxx)&(pxx>0)
    f=f[ok]; pxx=pxx[ok]
    if len(f)>2500:
        bins=np.geomspace(max(f.min(),1e-4),f.max(),2501)
        fb=[]; pb=[]
        for lo,hi in zip(bins[:-1],bins[1:]):
            m=(f>=lo)&(f<hi)
            if np.any(m):
                fb.append(float(np.exp(np.mean(np.log(f[m])))))
                pb.append(float(np.exp(np.mean(np.log(pxx[m])))))
        f=np.array(fb); pxx=np.array(pb)
    return {'frequency_hz':[float(x) for x in f], 'psd':[float(x) for x in pxx], 'fs_hz':fs, 'n_samples':int(len(y)), 'nperseg':int(nperseg), 'window_seconds':float(nperseg/fs) if fs>0 else None}

def compute_wind_spectra(data, trim_mins=2.0):
    out={}
    pairs=[('wind_mps','Total wind speed'),('wind_w_mps','Vertical wind component')]
    for col,label in pairs:
        s=welch_psd_from_time(data,col,trim_mins=trim_mins)
        if s:
            s['label']=label; s['column']=col; out[col]=s
    return out
def trim_frequency(data,mins=2):
    """Frequency time series from Airflow_UTCcorr_Nanoseconds_ns only.

    The raw 100 Hz clock is first differenced sample-by-sample. Intervals much
    longer than the nominal sampling interval are classified as acquisition gaps
    and are not converted to low-frequency values. The remaining instantaneous
    frequencies are summarized in 1 s bins for a readable, scientifically honest
    browser time-series plot.
    """
    empty=pd.DataFrame({'time':[], 'frequency_hz':[], 'frequency_min_hz':[], 'frequency_max_hz':[], 'sample_count':[]})
    if 'time_ns' not in data.columns:
        return empty
    q=pd.DataFrame({'time_ns':pd.to_numeric(data['time_ns'],errors='coerce')}).dropna()
    if q.empty:
        return empty
    q['time_ns']=q['time_ns'].astype('int64')
    q=q[q['time_ns']>0].sort_values('time_ns').drop_duplicates('time_ns')
    if len(q)<10:
        return empty
    trim=int(mins*60*1e9)
    if trim>0:
        q=q[(q['time_ns']>=q['time_ns'].min()+trim)&(q['time_ns']<=q['time_ns'].max()-trim)]
    if len(q)<2:
        return empty
    ns=q['time_ns'].to_numpy(np.int64)
    dt=np.diff(ns).astype(float)
    positive=dt>0
    if not np.any(positive):
        return empty
    nominal_dt=float(np.nanmedian(dt[positive]))
    gap_limit=max(1e9,nominal_dt*5.0)
    valid=positive & np.isfinite(dt) & (dt<=gap_limit)
    freq=1e9/dt[valid]
    sample_ns=ns[1:][valid]
    if len(freq)==0:
        return empty
    inst=pd.DataFrame({'frequency_hz':freq}, index=pd.to_datetime(sample_ns,unit='ns',utc=True))
    agg=inst['frequency_hz'].resample('1s').agg(['median','min','max','count'])
    full_index=pd.date_range(inst.index.min().floor('s'), inst.index.max().ceil('s'), freq='1s', tz='UTC')
    agg=agg.reindex(full_index)
    out=pd.DataFrame({
        'time':agg.index.astype(str),
        'frequency_hz':agg['median'].to_numpy(float),
        'frequency_min_hz':agg['min'].to_numpy(float),
        'frequency_max_hz':agg['max'].to_numpy(float),
        'sample_count':agg['count'].fillna(0).astype(int).to_numpy()
    })
    return out
def latlon_to_tile(lat,lon,z):
    lat_rad=math.radians(lat); n=2**z
    x=int((lon+180)/360*n); y=int((1-math.asinh(math.tan(lat_rad))/math.pi)/2*n); return x,y

def sample_terrarium(d1, cache):
    cache.mkdir(parents=True,exist_ok=True); vals=[]; memo={}; total=len(d1); network_failed=False
    for i,(lat,lon) in enumerate(zip(d1['plot_lat'].to_numpy(float),d1['plot_lon'].to_numpy(float))):
        if i%200==0: set_status(65+25*i/max(total,1),f'Sampling satellite DTM terrain {i:,}/{total:,}')
        try:
            tx,ty=latlon_to_tile(lat,lon,TERRAIN_ZOOM); key=(tx,ty)
            if key not in memo:
                f=cache/str(TERRAIN_ZOOM)/str(tx)/f'{ty}.png'; f.parent.mkdir(parents=True,exist_ok=True)
                if not f.exists():
                    if network_failed:
                        vals.append(np.nan); continue
                    try:
                        url=TERRAIN_TILE_URL.format(z=TERRAIN_ZOOM,x=tx,y=ty)
                        req=urllib.request.Request(url,headers={'User-Agent':'NoseboomBrowserGUI/1.0'})
                        f.write_bytes(urllib.request.urlopen(req,timeout=5).read())
                    except Exception:
                        network_failed=True; vals.append(np.nan); continue
                memo[key]=Image.open(f).convert('RGB')
            img=memo[key]; n=2**TERRAIN_ZOOM
            xf=(lon+180)/360*n; yf=(1-math.asinh(math.tan(math.radians(lat)))/math.pi)/2*n
            px=int((xf-tx)*256); py=int((yf-ty)*256); r,g,b=img.getpixel((max(0,min(255,px)),max(0,min(255,py))))
            vals.append((r*256+g+b/256)-32768)
        except Exception: vals.append(np.nan)
    return np.array(vals,dtype=float)


def project_file_path(output, flight_name):
    name=safe_name(flight_name)
    return Path(output)/f'{name}_noseboom_project.h5'

def find_project_file(output, flight_name=''):
    output=Path(output)
    if flight_name:
        candidates=[output/safe_name(flight_name)/f'{safe_name(flight_name)}_noseboom_project.h5', output/f'{safe_name(flight_name)}_noseboom_project.h5']
        for c in candidates:
            if c.exists(): return c
    files=sorted(output.rglob('*_noseboom_project.h5'), key=lambda p:p.stat().st_mtime, reverse=True) if output.exists() else []
    if files: return files[0]
    raise FileNotFoundError(f'No Noseboom project file found in {output}')

def _metadata_to_chunks(meta, chunk_size=50000):
    raw=json.dumps(meta, default=str)
    return pd.DataFrame({'chunk_index':range((len(raw)+chunk_size-1)//chunk_size),'meta_json_chunk':[raw[i:i+chunk_size] for i in range(0,len(raw),chunk_size)]})

def _metadata_from_store(store):
    keys=store.keys()
    if '/metadata' in keys:
        m=store['metadata'].sort_values('chunk_index')
        return json.loads(''.join(m['meta_json_chunk'].astype(str).tolist()))
    # Backward compatibility for older project files written with metadata as an HDF5 attribute.
    try:
        return json.loads(getattr(store.get_storer('analysis_1Hz').attrs,'meta_json','{}'))
    except Exception:
        return {}

def save_project(path, d1, straight, freq, spectra, summary, export_source=None):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    meta={'summary':summary,'spectra':spectra,'version':6,'created_utc':pd.Timestamp.utcnow().isoformat(),'log_count':len(STATE.logs)}
    if isinstance(freq, pd.DataFrame):
        freq_df=freq.copy()
    else:
        freq_df=pd.DataFrame({'frequency_hz':np.asarray(freq,dtype=float)})
    with pd.HDFStore(path, mode='w', complevel=5, complib='zlib') as store:
        store.put('analysis_1Hz', d1, format='table')
        store.put('straight_1Hz', straight, format='table')
        store.put('frequency', freq_df, format='table')
        store.put('metadata', _metadata_to_chunks(meta), format='table', data_columns=['chunk_index'])
        if export_source is not None and len(export_source):
            store.put('export_source', export_source, format='table')
        store.put('session_logs', logs_to_df(), format='table', data_columns=['time','level'])
    return path

def load_project_file(path):
    path=Path(path)
    if not path.exists(): raise FileNotFoundError(f'Project file does not exist: {path}')
    with pd.HDFStore(path, mode='r') as store:
        keys=store.keys()
        d1=store['analysis_1Hz']
        straight=store['straight_1Hz']
        freq_df=store['frequency'] if '/frequency' in keys else pd.DataFrame({'frequency_hz':[]})
        export_source=store['export_source'] if '/export_source' in keys else None
        project_logs=logs_from_store(store)
        meta=_metadata_from_store(store)
    summary=meta.get('summary',{})
    spectra=meta.get('spectra',{})
    freq=freq_df.copy() if isinstance(freq_df,pd.DataFrame) else pd.DataFrame({'frequency_hz':[]})
    if isinstance(summary,dict):
        straight.attrs['straight_params']=summary.get('straight_params',STRAIGHT_DEFAULTS)
        straight.attrs['straight_metrics']=summary.get('straight_metrics',[])
    return d1,straight,freq,spectra,summary,export_source,project_logs
EXPORT_OUTPUT_COLUMNS=[
 'Airflow_UTCcorr_Nanoseconds_ns',
 'TIMESTAMP',
 'Precise_time',
 'INS_Filter_LLHPos_Latitude_deg',
 'INS_Filter_LLHPos_Longitude_deg',
 'INS_Filter_LLHPos_ElipsoidHeight_m',
 'WIND_vWind_x_m/s',
 'WIND_vWind_y_m/s',
 'WIND_vWind_z_m/s',
 'WIND_dir_deg',
 'WIND_vWind_m/s',
 'Airflow_Flow_rel_humidity_',
 'Airflow_Sensor_pstat_hPa',
 'Airflow_Flow_OAT_degC'
]
EXPORT_COLUMNS={
 'time_ns':'Airflow_UTCcorr_Nanoseconds_ns',
 'time':'TIMESTAMP',
 'precise_time':'Precise_time',
 'lat':'INS_Filter_LLHPos_Latitude_deg',
 'lon':'INS_Filter_LLHPos_Longitude_deg',
 'height_m':'INS_Filter_LLHPos_ElipsoidHeight_m',
 'wind_u_mps':'WIND_vWind_x_m/s',
 'wind_v_mps':'WIND_vWind_y_m/s',
 'wind_w_mps':'WIND_vWind_z_m/s',
 'wind_dir_deg':'WIND_dir_deg',
 'wind_mps':'WIND_vWind_m/s',
 'rel_humidity_pct':'Airflow_Flow_rel_humidity_',
 'pressure_hpa':'Airflow_Sensor_pstat_hPa',
 'air_temp_degC':'Airflow_Flow_OAT_degC'
}

PRESSURE_CANDIDATES=[
 'pressure_hpa','Airflow_Sensor_pstat_hPa','Airflow_Sensor_pstat_hpa','Airflow_Sensor_pstat_Pa','pressure_pa',
 'Airflow_pressure_hPa','Airflow_pressure_Pa','Airflow_Flow_pressure_hPa','Airflow_Flow_pressure_Pa',
 'Airflow_Flow_Pressure_hPa','Airflow_Flow_Pressure_Pa','Airflow_Flow_static_pressure_hPa','Airflow_Flow_static_pressure_Pa',
 'Airflow_Flow_p_hPa','Airflow_Flow_p_Pa','p_hPa','p_Pa','PRESSURE_hPa','PRESSURE_Pa'
]

def _first_existing(data, candidates):
    for c in candidates:
        if c in data.columns:
            return c
    lower={str(c).lower():c for c in data.columns}
    for c in candidates:
        if c.lower() in lower:
            return lower[c.lower()]
    return None

def make_export_source(data):
    cols=['time_ns','time','lat','lon','height_m','wind_u_mps','wind_v_mps','wind_w_mps','wind_dir_deg','wind_mps',
          'rel_humidity_pct','pressure_hpa','air_temp_degC']
    q=data[[c for c in cols if c in data.columns]].copy()
    pcol=_first_existing(data, PRESSURE_CANDIDATES)
    if pcol is not None:
        pnum=pd.to_numeric(data[pcol],errors='coerce')
        if 'pa' in str(pcol).lower() and 'hpa' not in str(pcol).lower():
            pnum=pnum/100.0
        q['pressure_hpa']=pnum
    for c in cols:
        if c not in q.columns: q[c]=np.nan
    ns=pd.to_numeric(q['time_ns'],errors='coerce')
    q['precise_time']=pd.to_datetime(ns,unit='ns',origin='unix',utc=True,errors='coerce')
    fallback_time=pd.to_datetime(q['time'],errors='coerce',utc=True)
    q['precise_time']=q['precise_time'].where(q['precise_time'].notna(), fallback_time)
    q['time']=fallback_time.where(fallback_time.notna(), q['precise_time'])
    q=q.dropna(subset=['precise_time']).sort_values('precise_time')
    return q.reset_index(drop=True)

def circular_resample_deg(series, rule):
    def _mean(s):
        a=np.deg2rad(pd.to_numeric(s,errors='coerce').dropna().to_numpy(float))
        if len(a)==0: return np.nan
        return float(np.rad2deg(np.arctan2(np.sin(a).mean(),np.cos(a).mean()))%360)
    return series.resample(rule).apply(_mean)

def resample_export_data(export_source, frequency_hz):
    f=float(frequency_hz)
    if not np.isfinite(f) or f<1 or f>100: raise ValueError('Export frequency must be between 1 and 100 Hz')
    if export_source is None or len(export_source)==0: raise RuntimeError('No export source data available. Load the original Noseboom CSV data first, then export.')
    q=export_source.copy()
    ns=pd.to_numeric(q.get('time_ns'),errors='coerce')
    q['precise_time']=pd.to_datetime(ns,unit='ns',origin='unix',utc=True,errors='coerce')
    if 'time' in q.columns:
        fallback=pd.to_datetime(q['time'],errors='coerce',utc=True)
        q['precise_time']=q['precise_time'].where(q['precise_time'].notna(), fallback)
    q=q.dropna(subset=['precise_time']).sort_values('precise_time').set_index('precise_time')
    if len(q)==0: raise RuntimeError('No valid Airflow_UTCcorr_Nanoseconds_ns timestamps available for export.')
    rule=f'{int(round(1e9/f))}ns'
    sample_count=q.resample(rule).size()
    out=pd.DataFrame(index=sample_count.index)
    for c in ['lat','lon','height_m','wind_u_mps','wind_v_mps','wind_w_mps','wind_mps','rel_humidity_pct','pressure_hpa','air_temp_degC']:
        out[c]=pd.to_numeric(q[c],errors='coerce').resample(rule).median()
    out['wind_dir_deg']=circular_resample_deg(q['wind_dir_deg'], rule)
    out=out.loc[sample_count>0]
    if len(out)==0: raise RuntimeError('Export produced zero rows. Check that the original 100 Hz data contain valid Airflow_UTCcorr_Nanoseconds_ns timestamps.')
    out['time_ns']=pd.to_numeric(q['time_ns'],errors='coerce').resample(rule).first().loc[out.index].astype('int64')
    utc_index=pd.to_datetime(out['time_ns'],unit='ns',origin='unix',utc=True)
    out['time']=utc_index.dt.strftime('%Y-%m-%d %H:%M:%S.%f').str[:-3]
    out['precise_time']=utc_index.dt.strftime('%Y-%m-%d %H:%M:%S.%f').str[:-3]
    order=['time_ns','time','precise_time','lat','lon','height_m','wind_u_mps','wind_v_mps','wind_w_mps','wind_dir_deg','wind_mps',
           'rel_humidity_pct','pressure_hpa','air_temp_degC']
    out=out[order].rename(columns=EXPORT_COLUMNS)
    return out[EXPORT_OUTPUT_COLUMNS].reset_index(drop=True)

def export_noseboom_data(output, flight_name, export_source, frequency_hz, fmt):
    fmt=(fmt or '').lower().strip()
    if fmt not in ('csv','txt','hdf'): raise ValueError('Export format must be csv, txt, or hdf')
    outdir=Path(output)/safe_name(flight_name or 'Flight')/'exports'
    outdir.mkdir(parents=True,exist_ok=True)
    freq_label=(f'{float(frequency_hz):g}Hz').replace('.','p')
    table=resample_export_data(export_source, frequency_hz)
    path=outdir/f'{safe_name(flight_name or "Flight")}_noseboom_export_{freq_label}.{fmt if fmt!="hdf" else "h5"}'
    if fmt=='csv': table.to_csv(path,index=False,encoding='utf-8-sig')
    elif fmt=='txt': table.to_csv(path,index=False,sep='\t',encoding='utf-8')
    else:
        with pd.HDFStore(path,mode='w',complevel=5,complib='zlib') as store:
            store.put('noseboom_export',table,format='table')
            store.get_storer('noseboom_export').attrs.frequency_hz=float(frequency_hz)
    return path, len(table)
def analyze(data, output, flight_name, trim_mins):
    output.mkdir(parents=True,exist_ok=True); name=safe_name(flight_name); set_status(10,'Creating 1 Hz table')
    d1=one_hz(data); set_status(35,'Detecting sustained straight-flight legs'); straight=detect_straight(d1); straight_attrs=dict(straight.attrs)
    set_status(50,'Calculating acquisition frequency distribution'); freq=trim_frequency(data,trim_mins)
    set_status(58,'Calculating wind power spectra'); spectra=compute_wind_spectra(data,trim_mins)
    set_status(65,'Sampling satellite DTM terrain'); d1['terrain_m']=sample_terrarium(d1, Path(tempfile.gettempdir())/'noseboom_terrain_tile_cache')
    straight=straight.join(d1[['terrain_m']], how='left'); straight.attrs.update(straight_attrs)
    summary={'raw_rows':int(len(data)),'one_hz_rows':int(len(d1)),'straight_rows':int(straight['straight'].sum()),'straight_legs':int(straight['straight_leg_id'].max()),'median_frequency_hz':float(np.nanmedian(freq['frequency_hz'].to_numpy(float))) if isinstance(freq,pd.DataFrame) and len(freq) and 'frequency_hz' in freq else None,'straight_params':straight.attrs.get('straight_params',STRAIGHT_DEFAULTS),'straight_metrics':straight.attrs.get('straight_metrics',[])}
    project_path=project_file_path(output,name)
    summary['project_file']=str(project_path)
    export_source=make_export_source(data)
    save_project(project_path,d1,straight,freq,spectra,summary,export_source)
    return d1,straight,freq,spectra,summary,export_source
def ds(d,maxn=MAX_MAP_POINTS):
    return d.copy() if len(d)<=maxn else d.iloc[::int(math.ceil(len(d)/maxn))].copy()
def plotly_series(d,col,maxn=8000):
    q=ds(d[[col]].copy(),maxn=maxn) if len(d)>maxn else d[[col]].copy()
    return [[str(t), None if not np.isfinite(v) else float(v)] for t,v in zip(q.index,q[col])]
def color_values(v):
    """Return Turbo-like colours without importing Matplotlib/Tk backends."""
    fin=v[np.isfinite(v)]
    lo,hi=(np.nanpercentile(fin,[2,98]) if len(fin) else (0,1))
    hi=hi if hi>lo else lo+1
    stops=[(48,18,59),(70,98,215),(53,171,248),(26,228,182),(162,252,60),(249,186,56),(233,75,53),(122,4,3)]
    def hex_color(x):
        if not np.isfinite(x): x=lo
        t=float(np.clip((x-lo)/(hi-lo),0,1))*(len(stops)-1)
        i=int(math.floor(t)); j=min(i+1,len(stops)-1); f=t-i
        r=round(stops[i][0]*(1-f)+stops[j][0]*f); g=round(stops[i][1]*(1-f)+stops[j][1]*f); b=round(stops[i][2]*(1-f)+stops[j][2]*f)
        return f'#{r:02x}{g:02x}{b:02x}'
    return [hex_color(x) for x in v],float(lo),float(hi)
def api_payload():
    if getattr(STATE,'payload_cache',None) is not None: return STATE.payload_cache
    if STATE.d1 is None: return {'ready':False}
    base_d=STATE.d1.dropna(subset=['plot_lat','plot_lon']).copy()
    base_d=base_d[np.isfinite(base_d['plot_lat'].to_numpy(float)) & np.isfinite(base_d['plot_lon'].to_numpy(float)) & base_d['plot_lat'].between(-90,90) & base_d['plot_lon'].between(-180,180)]
    if base_d.empty:
        add_log('ERROR','No finite latitude/longitude samples are available for map rendering after analysis.')
        return {'ready':False}
    d=ds(base_d)
    st_all=STATE.straight.loc[STATE.straight['straight']].dropna(subset=['plot_lat','plot_lon']).copy() if STATE.straight is not None else pd.DataFrame()
    if len(st_all):
        st_all=st_all[np.isfinite(st_all['plot_lat'].to_numpy(float)) & np.isfinite(st_all['plot_lon'].to_numpy(float)) & st_all['plot_lat'].between(-90,90) & st_all['plot_lon'].between(-180,180)]
    st=ds(st_all) if len(st_all) else pd.DataFrame()
    route=[[float(a),float(b)] for a,b in zip(d['plot_lat'],d['plot_lon'])]
    wind=d['wind_mps'].to_numpy(float); segv=(wind[:-1]+wind[1:])/2; colors,lo,hi=color_values(segv)
    windseg=[{'coords':[route[i],route[i+1]],'color':colors[i],'wind':None if not np.isfinite(segv[i]) else round(float(segv[i]),3)} for i in range(len(route)-1)]
    straight=[[float(a),float(b)] for a,b in zip(st.get('plot_lat',[]),st.get('plot_lon',[]))]
    straight_legs=[]
    if len(st_all):
        for leg_id,g in st_all.groupby('straight_leg_id'):
            if int(leg_id)<=0: continue
            gd=ds(g)
            coords=[[float(a),float(b)] for a,b in zip(gd['plot_lat'],gd['plot_lon'])]
            if not coords: continue
            mid=g.iloc[len(g)//2]
            wind_speed=g['wind_mps'].to_numpy(float) if 'wind_mps' in g else np.array([])
            wind_dir=(np.degrees(np.arctan2(g['wind_u_mps'].to_numpy(float), g['wind_v_mps'].to_numpy(float)))+360)%360 if {'wind_u_mps','wind_v_mps'}.issubset(g.columns) else np.array([])
            sample_step=max(1,int(math.ceil(len(g)/240)))
            wind_samples=[{'dir':float(a),'spd':float(b)} for a,b in zip(wind_dir[::sample_step],wind_speed[::sample_step]) if np.isfinite(a) and np.isfinite(b)]
            metric={}
            if isinstance(STATE.summary,dict):
                for row in STATE.summary.get('straight_metrics',[]):
                    if int(row.get('leg',-1))==int(leg_id): metric=row; break
            straight_legs.append({'id':int(leg_id),'coords':coords,'label':[float(mid['plot_lat']),float(mid['plot_lon'])],'duration_s':float((g.index[-1]-g.index[0]).total_seconds()+1),'distance_km':float(np.nansum(haversine_m(g['plot_lat'].iloc[:-1].to_numpy(),g['plot_lon'].iloc[:-1].to_numpy(),g['plot_lat'].iloc[1:].to_numpy(),g['plot_lon'].iloc[1:].to_numpy()))/1000.0) if len(g)>1 else 0.0,'mean_speed_mps':float(g['ground_speed_mps'].mean()) if 'ground_speed_mps' in g else None,'mean_wind_mps':float(np.nanmean(wind_speed)) if len(wind_speed) else None,'mean_heading_deg':float(metric.get('mean_heading_deg',np.nan)) if metric else None,'heading_std_deg':float(metric.get('median_heading_std_deg',np.nan)) if metric else None,'max_roll_deg':float(metric.get('max_abs_roll_deg',np.nan)) if metric else None,'altitude_range_m':float(metric.get('altitude_range_m',np.nan)) if metric else None,'max_vertical_speed_mps':float(metric.get('max_abs_vertical_speed_mps',np.nan)) if metric else None,'windSamples':wind_samples})
    midlat=float(np.nanmean(d['plot_lat'])); buffer=500
    bounds=[[float(d['plot_lat'].min()),float(d['plot_lon'].min())],[float(d['plot_lat'].max()),float(d['plot_lon'].max())]]
    hist={}
    for c in ['wind_mps','wind_u_mps','wind_v_mps','wind_w_mps','air_temp_degC','rel_humidity_pct']:
        if c in STATE.d1:
            arr=STATE.d1[c].to_numpy(float)
            if len(arr)>30000: arr=arr[::int(math.ceil(len(arr)/30000))]
            hist[c]=[None if not np.isfinite(x) else float(x) for x in arr]
    freq_df=STATE.freq.copy() if isinstance(STATE.freq,pd.DataFrame) else pd.DataFrame({'frequency_hz':np.asarray(STATE.freq if STATE.freq is not None else [],dtype=float)})
    if len(freq_df)>30000: freq_df=freq_df.iloc[::int(math.ceil(len(freq_df)/30000))]
    def js_float_list(series):
        return [None if not np.isfinite(x) else float(x) for x in pd.to_numeric(series,errors='coerce').to_numpy(float)]
    freq_vals=js_float_list(freq_df['frequency_hz']) if 'frequency_hz' in freq_df else []
    freq_min=js_float_list(freq_df['frequency_min_hz']) if 'frequency_min_hz' in freq_df else []
    freq_max=js_float_list(freq_df['frequency_max_hz']) if 'frequency_max_hz' in freq_df else []
    freq_count=[int(x) if np.isfinite(x) else 0 for x in pd.to_numeric(freq_df['sample_count'],errors='coerce').fillna(0).to_numpy(float)] if 'sample_count' in freq_df else []
    if 'time' in freq_df:
        freq_time=[str(x) for x in freq_df['time'].to_numpy()]
    elif 'time_ns' in freq_df:
        freq_time=[str(x) for x in pd.to_datetime(pd.to_numeric(freq_df['time_ns'],errors='coerce'),unit='ns',utc=True).astype(str).to_numpy()]
    else:
        freq_time=list(range(len(freq_vals)))
    return {'ready':True,'summary':STATE.summary,'route':route,'windSegments':windseg,'straight':straight,'straightLegs':straight_legs,'bounds':bounds,'windMin':round(lo,2),'windMax':round(hi,2),'hist':hist,'freq':freq_vals,'freqMin':freq_min,'freqMax':freq_max,'freqCount':freq_count,'freqTime':freq_time,'spectra':STATE.spectra, 'altitude':plotly_series(STATE.d1,'altitude_m'),'ellipsoid':plotly_series(STATE.d1,'height_m') if 'height_m' in STATE.d1 else [], 'terrain':plotly_series(STATE.d1,'terrain_m') if 'terrain_m' in STATE.d1 else []}
HTML=r'''
<!doctype html><html><head><meta charset="utf-8"><title>Zeppelin CCFLUX Campaign 2026</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"><script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
:root{--ink:#08262c;--nav:#123f45;--nav2:#0b2d33;--teal:#0f8278;--teal2:#27a79b;--mint:#dff7f3;--paper:#f4f8f8;--panel:#ffffff;--line:#bdd0d0;--muted:#5a6a6d;--ok:#69d985;--warn:#f0a43a;--shadow:0 10px 28px rgba(8,38,44,.10)}
*{box-sizing:border-box}body{font-family:"Segoe UI",Arial,sans-serif;margin:0;background:linear-gradient(180deg,#eef6f6 0,#f7fbfb 260px,#f4f7f8 100%);color:var(--ink);padding-bottom:38px}header{background:linear-gradient(135deg,var(--nav2),var(--nav));color:white;padding:18px 22px 20px;border-bottom:5px solid rgba(39,167,155,.35);box-shadow:0 3px 16px rgba(0,0,0,.16)}header .eyebrow{font-size:14px;font-weight:600;color:#bdf5ef;letter-spacing:.02em;margin-bottom:4px}header h1{font-size:30px;line-height:1.1;margin:0;font-weight:700}header .sub{font-size:13px;margin-top:6px;color:#d8eeee}main{padding:14px 16px 20px}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.card{background:rgba(255,255,255,.94);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:12px 0;box-shadow:var(--shadow)}.card h3{margin:0 0 12px;color:#0a363c;font-size:17px;letter-spacing:.01em}#topCard{border-top:4px solid var(--teal2)}.section-title{font-size:17px;font-weight:700;color:#0a363c;margin:0 0 10px}.project-fields{align-items:flex-end}.revision-tag{font-size:12px;font-weight:700;color:#147b77;margin-left:10px}.folder-pick{display:flex;flex-direction:column;gap:5px;min-width:300px}.folder-btn{width:max-content}.path-display{min-height:32px;max-width:520px;padding:7px 9px;border:1px solid #b9d4d4;border-radius:6px;background:#eef8f7;color:#092f35;font-size:13px;font-weight:600;word-break:break-all}.path-display.empty{color:#6a7b7e;font-style:italic;background:#f6fbfb}.top-actions{flex:1;align-items:flex-end}.right-action{margin-left:auto}.top-actions .btn{white-space:nowrap}.workflow{display:grid;grid-template-columns:minmax(420px,1.2fr) minmax(300px,.8fr);gap:12px;margin-top:12px}.workflow-panel{border:1px solid #c5dada;border-radius:10px;background:linear-gradient(180deg,#fbffff,#f1f8f8);padding:12px}.workflow-panel.load-panel{background:linear-gradient(180deg,#fffdf7,#f6f2e8);border-color:#d8cda8}.panel-title{font-weight:700;color:#08363b;margin-bottom:3px}.panel-help{font-size:12px;color:#5a6a6d;margin-bottom:10px;line-height:1.3}.status-row{margin-top:12px;align-items:flex-start}label{font-size:13px;color:#163b40;font-weight:600}label input{margin-left:6px}input{padding:7px 8px;border:1px solid #8fb1b2;border-radius:4px;background:#fbffff;color:#071f23;min-height:30px}input:focus{outline:2px solid rgba(39,167,155,.28);border-color:var(--teal)}.btn{padding:7px 13px;border:1px solid #7ca8a9;border-radius:5px;background:#eef7f6;color:#082b30;cursor:pointer;font-weight:600;min-height:31px;transition:background .12s,border-color .12s,transform .05s}.btn:hover:not(:disabled){background:#dff2ef;border-color:var(--teal)}.btn:active:not(:disabled){transform:translateY(1px)}.btn:disabled{opacity:.55;cursor:not-allowed}.btn.active{background:var(--teal);border-color:#06655e;color:white}.btn.done{background:var(--ok);border-color:#23974a;color:#082b19;font-weight:700}.context-menu{position:absolute;z-index:99999;background:white;border:1px solid #8fb1b2;border-radius:8px;box-shadow:0 10px 28px rgba(0,0,0,.22);display:none;overflow:hidden}.context-menu button{display:block;background:white;border:0;padding:9px 14px;cursor:pointer;text-align:left;width:205px;color:#082b30}.context-menu button:hover{background:#e1f3f0}.progress{height:16px;background:#d9e4e5;border-radius:999px;overflow:hidden;width:min(420px,70vw);border:1px solid #c3d4d5}.bar{height:100%;width:0;background:linear-gradient(90deg,var(--teal),#3aa0ff);transition:width .25s ease}.status-pill{display:inline-flex;align-items:flex-start;gap:6px;color:#133b42;font-weight:600;max-width:calc(100vw - 80px);line-height:1.35;white-space:normal}.status-pill:before{content:"";width:8px;height:8px;border-radius:50%;background:var(--teal2);display:inline-block}#map{height:620px;border:1px solid #c8d8d8;border-radius:8px;overflow:hidden;background:#dceaea}.grid{display:grid;grid-template-columns:repeat(2,minmax(300px,1fr));gap:12px}.plot{height:280px;border-radius:8px}.legend{padding:8px;background:transparent;border:0;border-radius:0;box-shadow:none;font-size:13px;font-weight:700;color:#061f24;text-shadow:0 1px 2px rgba(255,255,255,.95)}.legendbar{width:34px;height:240px;background:linear-gradient(to top,#30123b,#4662d7,#35abf8,#1ae4b6,#a2fc3c,#f9ba38,#e94b35,#7a0403);border:1px solid #456}.leg-label{background:#6f42c1;color:white;border:2px solid white;border-radius:50%;width:24px;height:24px;text-align:center;line-height:21px;font-weight:700;box-shadow:0 1px 5px #0006}.leg-info{background:rgba(255,255,255,.97);color:#111;border:1px solid #8fb1b2;border-radius:10px;box-shadow:0 8px 24px #0004;padding:10px;min-width:250px;max-width:320px;font-size:12px}.leg-info h4{margin:0 0 6px 0;font-size:14px;color:#0a363c}.leg-info table{border-collapse:collapse;width:100%;font-size:12px}.leg-info td{padding:3px 4px;border-bottom:1px solid #e4eeee}.leg-info svg{width:100%;height:auto}.map-info{background:rgba(255,255,255,.94);border:1px solid #8fb1b2;border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.18);padding:8px 10px;font-size:13px;color:#082b30;font-weight:600}.modal{position:fixed;inset:0;background:rgba(5,20,23,.62);z-index:100000;display:none;align-items:center;justify-content:center}.modal-card{background:white;color:#111;border-radius:12px;box-shadow:0 16px 48px rgba(0,0,0,.38);max-width:760px;width:min(92vw,760px);max-height:88vh;overflow:auto;padding:18px;border-top:5px solid var(--teal)}.modal-card h3{margin-top:0;color:#0a363c}.settings-grid{display:grid;grid-template-columns:repeat(2,minmax(290px,1fr));gap:12px}.setting-card{border:1px solid #d0dddd;border-radius:8px;padding:10px;background:#fbfefe}.setting-card label{display:block;font-weight:700;font-size:13px;margin-bottom:2px}.setting-key{font-size:11px;color:#647477;margin-bottom:5px}.setting-desc{font-size:12px;line-height:1.32;color:#1e3034;margin:6px 0}.setting-effect{font-size:11px;line-height:1.28;color:#56686b;margin-top:4px}.setting-card input{width:100%;box-sizing:border-box;margin-left:0}.modal-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:14px}.export-grid{display:grid;grid-template-columns:repeat(2,minmax(180px,1fr));gap:12px;margin-top:10px}.export-grid label{display:block}.export-grid input,.export-grid select{width:100%;margin-left:0;margin-top:5px;padding:7px 8px;border:1px solid #8fb1b2;border-radius:4px;background:#fbffff;color:#071f23;min-height:32px}.complete-box{background:#f2fbf9;border:1px solid #b8d8d2;border-radius:8px;padding:10px;margin-top:10px;word-break:break-word}.complete-box table{width:100%;border-collapse:collapse}.complete-box td{padding:4px 6px;border-bottom:1px solid #d9ece8}.log-box{height:min(68vh,620px);overflow:auto;background:#071f23;color:#eaffff;border-radius:8px;padding:10px;font-family:Consolas,'Courier New',monospace;font-size:12px;line-height:1.38;white-space:pre-wrap}.log-line{border-bottom:1px solid rgba(255,255,255,.08);padding:4px 0}.log-time{color:#9be3dc}.log-level{display:inline-block;min-width:54px;font-weight:700}.log-level.ERROR{color:#ff9b9b}.log-level.BUSY{color:#ffd37a}.log-level.INFO{color:#9ee7aa}.progress-modal .modal-card{max-width:440px}.indeterminate{height:16px;background:linear-gradient(90deg,#d8e7ff,#27a79b,#3aa0ff,#d8e7ff);background-size:220% 100%;animation:barMove 1.1s linear infinite;border-radius:999px}@keyframes barMove{from{background-position:0 0}to{background-position:220% 0}}footer{position:fixed;left:0;right:0;bottom:0;z-index:99990;background:#061f24;color:#eaffff;text-align:center;font-size:15px;font-weight:700;padding:9px 10px;box-shadow:0 -3px 14px rgba(0,0,0,.18);letter-spacing:.01em}body.focus-map{padding-bottom:38px;overflow:hidden;font-size:15px}body.focus-map header,body.focus-map #topCard,body.focus-map #statsCard,body.focus-map #mapCard h3{display:none!important}body.focus-map footer{display:block!important;font-size:15px;padding:9px 10px;font-weight:700}body.focus-map main{padding:0!important}body.focus-map #mapCard{margin:0!important;padding:0!important;border:0!important;border-radius:0!important;box-shadow:none!important;background:transparent!important}body.focus-map #mapCard>.row{display:flex!important;gap:12px;align-items:center;padding:10px 14px;background:rgba(244,251,251,.96);border-bottom:1px solid #b8d1d1;box-shadow:0 2px 12px rgba(8,38,44,.10);font-size:15px}body.focus-map #routeBtn,body.focus-map #windBtn,body.focus-map #straightBtn{display:none!important}body.focus-map label{font-size:15px}body.focus-map input{font-size:15px;min-height:34px;padding:7px 9px}body.focus-map .btn{font-size:15px;min-height:34px;padding:7px 14px}body.focus-map #map{height:calc(100vh - 94px)!important;border:0!important;border-radius:0!important}.leaflet-control{font-size:14px}.leaflet-control-scale-line{font-size:13px}.leaflet-tooltip{font-size:13px}.wind-hover-tooltip{font-size:14px;font-weight:700;color:#061f24}.leaflet-popup-content{font-size:14px}body.focus-stats header,body.focus-stats footer,body.focus-stats #topCard,body.focus-stats #mapCard{display:none!important}body.focus-stats main{padding:0!important}body.focus-stats #statsCard{margin:0!important;border:0!important;box-shadow:none!important}@media(max-width:900px){header h1{font-size:23px}main{padding:10px}.settings-grid,.grid,.workflow{grid-template-columns:1fr}input{max-width:100%}#map{height:520px}}
</style></head><body><header><div class="eyebrow">Noseboom wind measurement system</div><h1>Zeppelin CCFLUX Campaign 2026</h1></header><main>
<div id="topCard" class="card"><div class="section-title">Project setup <span class="revision-tag"></span></div><input id="root" type="hidden" value=""><input id="out" type="hidden" value=""><div class="row project-fields"><div class="folder-pick"><button id="browseRootBtn" class="btn folder-btn" onclick="pickFolder('root')">Select Flight root</button><div id="rootDisplay" class="path-display empty">No flight root selected</div></div><label>Flight name <input id="fname" value=""></label><div class="folder-pick"><button id="browseOutBtn" class="btn folder-btn" onclick="pickFolder('out')">Select Output folder</button><div id="outDisplay" class="path-display empty">No output folder selected</div></div><div class="row top-actions"><button id="detectBtn" class="btn" onclick="detect()">Detect data</button><button id="loadBtn" class="btn" onclick="loadData()">Load data</button><button id="anbtn" class="btn" onclick="analyze()">Analyze</button><button id="exportBtn" class="btn" onclick="openExportModal()">Export data</button><button id="logBtn" class="btn right-action" onclick="openLogModal()">Log</button><button id="projBtn" class="btn" onclick="loadProject()">Load project</button><button id="exitBtn" class="btn" onclick="requestExit()">Exit</button></div></div><div class="row status-row"><div class="progress"><div id="bar" class="bar"></div></div><span id="status" class="status-pill">Ready</span></div></div><div id="mapCard" class="card"><h3>Map</h3><div class="row"><label>Map buffer [m] <input id="buffer" type="number" value="500" oninput="updateMap()"></label><label>Line width <input id="lineWidth" type="number" min="1" max="20" step="1" value="5" oninput="redrawMapLines()"></label><button id="routeBtn" class="btn" data-fullscreen="#map" data-panel="map" data-view="route" onclick="showLayer('route')">Flight route</button><button id="windBtn" class="btn" data-fullscreen="#map" data-panel="map" data-view="wind" onclick="showLayer('wind')">Wind speed</button><button id="straightBtn" class="btn" data-fullscreen="#map" data-panel="map" data-view="straight" data-straight-settings="1" onclick="showLayer('straight')">Straight Flight</button><button id="resetMapBtn" class="btn" onclick="resetMap()">Reset position</button></div><div id="map"></div></div>
<div id="statsCard" class="card"><h3>Statistical overview</h3><div class="row"><button id="histBtn" class="btn" data-fullscreen="#stats" data-panel="stats" data-view="hist" onclick="showStats('hist')">Histogram</button><button id="freqBtn" class="btn" data-fullscreen="#stats" data-panel="stats" data-view="freq" onclick="showStats('freq')">Frequency</button><button id="altBtn" class="btn" data-fullscreen="#stats" data-panel="stats" data-view="alt" onclick="showStats('alt')">Altitude profile</button><button id="spectraBtn" class="btn" data-fullscreen="#stats" data-panel="stats" data-view="spectra" onclick="showStats('spectra')">Wind spectra</button></div><div id="stats"></div></div>
<div id="contextMenu" class="context-menu"><button id="ctxFull" onclick="openContextFullScreen()">Open in full screen</button><button id="ctxNewTab" onclick="openContextNewTab()">Open in new tab</button><button id="ctxCurrent" onclick="showStraightSettings()">Current settings</button><button id="ctxChange" onclick="openStraightSettingsModal()">Change settings</button></div><div id="settingsModal" class="modal"><div class="modal-card"><h3>Straight Flight settings</h3><p>Configure the objective screening criteria used to identify straight-flight measurement windows from the 1 Hz flight record. The thresholds below control the kinematic stability, geometric straightness, and minimum sampling length required for each accepted leg.</p><div id="settingsGrid" class="settings-grid"></div><div class="modal-actions"><button class="btn" onclick="closeSettingsModal()">Cancel</button><button id="straightStartBtn" class="btn done" onclick="startStraightAnalyze()">Start analyze</button></div></div></div><div id="exportModal" class="modal"><div class="modal-card"><h3>Export Noseboom data</h3><p>Export selected Noseboom variables from the original-resolution data. Choose the output sampling frequency and file format.</p><div class="export-grid"><label>Export frequency [Hz]<input id="exportFrequency" type="number" min="1" max="100" step="1" value="1"></label><label>Output format<select id="exportFormat"><option value="csv">CSV (.csv)</option><option value="txt">Text, tab separated (.txt)</option><option value="hdf">HDF5 (.h5)</option></select></label></div><div class="modal-actions"><button class="btn" onclick="closeExportModal()">Cancel</button><button class="btn done" onclick="startExportData()">Export</button></div></div></div><div id="exportCompleteModal" class="modal"><div class="modal-card"><h3>Export complete</h3><div id="exportCompleteDetails" class="complete-box"></div><div class="modal-actions"><button class="btn done" onclick="closeExportCompleteModal()">OK</button></div></div></div><div id="logModal" class="modal"><div class="modal-card"><h3>Session log</h3><div id="logBox" class="log-box">Loading log...</div><div class="modal-actions"><button class="btn" onclick="refreshLogs()">Refresh</button><button class="btn done" onclick="closeLogModal()">Close</button></div></div></div><div id="exitConfirmModal" class="modal"><div class="modal-card"><h3>Exit Noseboom Quick Look?</h3><p>This will safely save the project file and detailed session log before closing the local dashboard. Unsaved analysis results and recent errors will be written into the HDF5 project file when a project is available.</p><div class="modal-actions"><button class="btn" onclick="cancelExit()">Cancel</button><button class="btn done" onclick="confirmExit()">Save and exit</button></div></div></div><div id="progressModal" class="modal progress-modal"><div class="modal-card"><h3 id="progressTitle">Updating</h3><p id="progressText">Please wait...</p><div class="indeterminate"></div></div></div></main><footer>© 2026 Biplob Dey · Version 1.0_07_22</footer><script>
let data=null,map=null,layers={},active='route',activeStat='hist',legendControl=null,bufferRect=null,contextTarget=null,contextPanel='',contextView='',legInfoControl=null,mapInfoControl=null,selectedLegId=null,uiBusy=false,busyPollTimer=null,mapUpdateTimer=null,mapUpdating=false,initialFocusFitDone=false;
const urlParams=new URLSearchParams(window.location.search);const pathName=window.location.pathname.toLowerCase();const pathFocus=pathName.includes('/map')?'map':(pathName.includes('/stats')?'stats':'');const pathLayer=(pathName.includes('/map/wind')?'wind':(pathName.includes('/map/straight')?'straight':(pathName.includes('/map/route')?'route':'')));const pathStat=(pathName.includes('/stats/frequency')?'freq':(pathName.includes('/stats/altitude')?'alt':(pathName.includes('/stats/spectra')?'spectra':(pathName.includes('/stats/histogram')?'hist':''))));const requestedFocus=urlParams.get('focus')||pathFocus;const requestedLayer=urlParams.get('layer')||pathLayer||'';const requestedStat=urlParams.get('stat')||pathStat||'';const requestedLat=parseFloat(urlParams.get('lat')||'NaN'),requestedLon=parseFloat(urlParams.get('lon')||'NaN'),requestedZoom=parseInt(urlParams.get('zoom')||'');if(requestedFocus==='map')document.body.classList.add('focus-map');if(requestedFocus==='stats')document.body.classList.add('focus-stats');setTimeout(()=>{if(urlParams.get('buffer'))document.getElementById('buffer').value=urlParams.get('buffer');if(urlParams.get('lineWidth'))document.getElementById('lineWidth').value=urlParams.get('lineWidth')},0);
async function post(url,obj){let r=await fetch(url,{method:'POST',body:JSON.stringify(obj)});let j=await r.json();if(!r.ok||j.ok===false){await refreshLogs().catch(()=>{});throw new Error(j.message||j.error||('HTTP '+r.status));}return j}
function escapeHtml(v){return String(v??'').replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))}
async function refreshLogs(){let box=document.getElementById('logBox');if(!box)return;let r=await fetch('/logs',{cache:'no-store'}).then(x=>x.json());let logs=r.logs||[];box.innerHTML=logs.length?logs.map(x=>`<div class="log-line"><span class="log-time">${escapeHtml(x.time)}</span> <span class="log-level ${escapeHtml(x.level)}">${escapeHtml(x.level)}</span> ${escapeHtml(x.message)}</div>`).join(''):'<div class="log-line">No log entries yet.</div>';box.scrollTop=box.scrollHeight}
async function openLogModal(){document.getElementById('logModal').style.display='flex';await refreshLogs()}
function closeLogModal(){document.getElementById('logModal').style.display='none'}
function requestExit(){if(uiBusy)return;document.getElementById('exitConfirmModal').style.display='flex'}
function cancelExit(){document.getElementById('exitConfirmModal').style.display='none'}
function closeThisTabAfterExit(){try{window.open('','_self');window.close()}catch(e){}setTimeout(()=>{document.body.innerHTML='<div style="font-family:Segoe UI,Arial,sans-serif;padding:32px;color:#082b30"><h2>Noseboom Quick Look closed safely</h2><p>The project file and session log were saved. If the browser did not close this tab automatically, it is safe to close it manually.</p></div>'},400)}
window.addEventListener('storage',e=>{if(e.key==='noseboom_exit_signal')closeThisTabAfterExit()});
async function confirmExit(){if(uiBusy)return;cancelExit();setBusy(true,'Preparing for Exit','Saving the project file and detailed session log...');try{let r=await post('/exit',{fname:fname.value,out:out.value});try{localStorage.setItem('noseboom_exit_signal',String(Date.now()))}catch(e){}document.getElementById('progressText').textContent=(r&&r.message)||'Project saved. Closing dashboard...';setTimeout(closeThisTabAfterExit,900)}catch(e){setBusy(false);alert('Exit preparation failed: '+(e&&e.message?e.message:e))}}
function clientLog(level,msg){try{fetch('/client_log',{method:'POST',body:JSON.stringify({level:level,message:String(msg)})})}catch(e){}}
window.addEventListener('error',e=>clientLog('ERROR','Browser error: '+(e.message||e.error||'unknown')));
window.addEventListener('unhandledrejection',e=>clientLog('ERROR','Browser promise rejection: '+((e.reason&&e.reason.message)||e.reason||'unknown')));
function setBusy(on,title='Updating',message='Please wait...'){uiBusy=on;document.getElementById('progressTitle').textContent=title;document.getElementById('progressText').textContent=message;document.getElementById('progressModal').style.display=on?'flex':'none';['detectBtn','loadBtn','projBtn','anbtn','exportBtn','exitBtn','straightBtn','straightStartBtn','browseRootBtn','browseOutBtn'].forEach(id=>{let el=document.getElementById(id);if(el)el.disabled=on})}
function setMapUpdating(on,message='Updating map...'){if(uiBusy)return;mapUpdating=on;document.getElementById('progressTitle').textContent='Updating map';document.getElementById('progressText').textContent=message;document.getElementById('progressModal').style.display=on?'flex':'none'}
function scheduleMapUpdate(fn,message='Updating map...'){if(uiBusy||!map||!data)return;clearTimeout(mapUpdateTimer);setMapUpdating(true,message);mapUpdateTimer=setTimeout(()=>{try{fn()}finally{setTimeout(()=>setMapUpdating(false),120)}},350)}
async function waitForIdleThen(cb){clearTimeout(busyPollTimer);let s=await fetch('/status').then(r=>r.json());setStatus(s);document.getElementById('progressText').textContent=s.message||'Working...';if(s.busy){busyPollTimer=setTimeout(()=>waitForIdleThen(cb),700)}else{setBusy(false);if(cb)await cb()}}
function setStatus(s){let msg=s.message||'Ready',st=document.getElementById('status');st.title=msg;let fail=msg.toLowerCase().includes('failed');st.textContent=(msg.length>190?(fail?msg.split(':')[0]+': see full error by hovering this status line.':msg.slice(0,190)+' ...'):msg);document.getElementById('bar').style.width=s.percent+'%';if(!s.busy&&msg.startsWith('Data loaded'))document.getElementById('loadBtn').classList.add('done');if(!s.busy&&msg.startsWith('Project loaded'))document.getElementById('projBtn').classList.add('done');if(!s.busy&&msg.includes('Analysis complete'))document.getElementById('anbtn').classList.add('done')}
async function poll(){let s=await fetch('/status').then(r=>r.json());setStatus(s);if(s.busy)setTimeout(poll,500)}
function setSelectedPath(target,path){let el=document.getElementById(target);let disp=document.getElementById(target+'Display');if(el)el.value=path||'';if(disp){disp.textContent=path||((target==='root')?'No flight root selected':'No output folder selected');disp.classList.toggle('empty',!path)}}async function pickFolder(target){if(uiBusy)return;let el=document.getElementById(target);let label=(target==='root')?'Flight root':'Output folder';setBusy(true,'Select '+label,'Opening Windows folder-selection dialog...');try{let r=await post('/pick_folder',{target:target,initial:el?el.value:'',title:'Select '+label});if(r.path){setSelectedPath(target,r.path);document.getElementById('status').textContent=label+' selected.'}else{document.getElementById('status').textContent=r.message||'Folder selection cancelled.'}}catch(err){document.getElementById('status').textContent='Folder selection failed: '+err.message;await post('/client_log',{level:'ERROR',message:'Folder selection failed: '+err.message}).catch(()=>{})}finally{setBusy(false)}}async function detect(){let r=await post('/detect',{root:root.value,fname:fname.value});alert(r.message);if(r.ok)document.getElementById('detectBtn').classList.add('done')}
async function loadData(){if(uiBusy)return;document.getElementById('loadBtn').classList.remove('done');setBusy(true,'Loading data','Indexing and reading CSV. Please wait...');await post('/load',{root:root.value,fname:fname.value,out:out.value});waitForIdleThen(null)}
async function loadProject(){if(uiBusy)return;document.getElementById('projBtn').classList.remove('done');setBusy(true,'Loading project','Reading saved Noseboom project file...');await post('/load_project',{fname:fname.value,out:out.value});waitForIdleThen(async()=>{document.getElementById('projBtn').classList.add('done');await refresh()})}
async function analyze(){if(uiBusy)return;document.getElementById('anbtn').classList.remove('done');setBusy(true,'Analyzing flight','Creating 1 Hz table, statistics, wind spectra, terrain and Straight Flight legs...');await post('/analyze',{fname:fname.value,out:out.value});waitForIdleThen(async()=>{await refresh()})}
function openExportModal(){if(uiBusy)return;document.getElementById('exportModal').style.display='flex'}
function closeExportModal(){document.getElementById('exportModal').style.display='none'}
function closeExportCompleteModal(){document.getElementById('exportCompleteModal').style.display='none'}
function showExportComplete(info){let d=document.getElementById('exportCompleteDetails');if(!info||!info.ok){d.innerHTML='<b>Export status:</b> '+((info&&info.message)||'Export finished, but no details were returned.');}else{d.innerHTML=`<table><tr><td>File</td><td>${info.path}</td></tr><tr><td>Frequency</td><td>${info.frequency_hz} Hz</td></tr><tr><td>Format</td><td>${info.format.toUpperCase()}</td></tr><tr><td>Exported rows</td><td>${Number(info.rows).toLocaleString()}</td></tr><tr><td>Source</td><td>${escapeHtml(info.source||'')}</td></tr><tr><td>Variables</td><td>Airflow_UTCcorr_Nanoseconds_ns, TIMESTAMP, Precise_time, latitude, longitude, altitude, WIND_vWind_x/y/z, WIND_dir_deg, WIND_vWind_m/s, RH, pstat, OAT</td></tr></table>`}document.getElementById('exportCompleteModal').style.display='flex'}
async function startExportData(){if(uiBusy)return;let f=parseFloat(document.getElementById('exportFrequency').value);let fmt=document.getElementById('exportFormat').value;if(!Number.isFinite(f)||f<1||f>100){alert('Frequency must be between 1 and 100 Hz.');return}closeExportModal();setBusy(true,'Exporting Noseboom data',`Resampling original Noseboom data at ${f} Hz and writing ${fmt.toUpperCase()} file...`);await post('/export_data',{fname:fname.value,out:out.value,frequency:f,format:fmt});waitForIdleThen(async()=>{let info=await fetch('/export_result').then(r=>r.json()).catch(()=>null);showExportComplete(info)})}
async function exportData(){openExportModal()}const straightFields=[
 {key:'minimum_ground_speed_mps',name:'Minimum ground speed',unit:'m s-1',def:8,desc:'Forward motion the Zeppelin must hold for a sample to be screened. Slower samples are station-keeping or manoeuvring, not a transect.',effect:'Increasing this restricts the analysis to faster flight; decreasing it admits slower sections.'},
 {key:'minimum_segment_duration_s',name:'Minimum segment duration',unit:'s',def:60,desc:'Shortest continuous period of accepted samples kept as one straight-flight leg.',effect:'Increasing this keeps only long transects; decreasing it admits shorter ones.'},
 {key:'heading_window_s',name:'Heading stability window',unit:'s',def:30,desc:'Centred window over which the circular heading standard deviation and the altitude range are measured.',effect:'A longer window demands steadiness over a longer stretch; a shorter one reacts to local changes.'},
 {key:'maximum_heading_std_deg',name:'Maximum heading standard deviation',unit:'deg',def:10,desc:'Circular standard deviation of heading inside the stability window, which is how steadily the airship was pointing.',effect:'Increasing this admits more weaving; decreasing it demands a straighter course.'},
 {key:'maximum_heading_rate_dps',name:'Maximum heading rate',unit:'deg s-1',def:3,desc:'How fast the heading may change between consecutive samples, rejecting turns.',effect:'Increasing this admits gentler turns; decreasing it enforces a constant course.'},
 {key:'maximum_roll_angle_deg',name:'Maximum absolute roll',unit:'deg',def:10,desc:'Bank angle limit, so a banking airship is not counted as flying straight.',effect:'Increasing this admits more bank; decreasing it demands level wings.'},
 {key:'maximum_altitude_range_m',name:'Maximum altitude range',unit:'m',def:100,desc:'Altitude spread allowed inside the stability window, which keeps a leg quasi-level.',effect:'Increasing this admits climbing or descending legs; decreasing it demands level flight.'},
 {key:'maximum_vertical_speed_mps',name:'Maximum absolute vertical speed',unit:'m s-1',def:2.2,desc:'Climb or descent rate limit applied sample by sample.',effect:'Increasing this admits faster vertical motion; decreasing it demands steadier level flight.'}
];
function currentStraightParams(){return (data&&data.summary&&data.summary.straight_params)||Object.fromEntries(straightFields.map(f=>[f.key,f.def]))}
function settingsText(){let p=currentStraightParams();let header='Method: 1 Hz flight data are screened for kinematic stability, grouped into continuous candidate runs, segmented into distance-based measurement windows, and retained only when they satisfy the geometric and altitude-stability criteria.\n\n';return header+straightFields.map(f=>`${f.name} (${f.key}, ${f.unit}): ${p[f.key]??f.def}\nDefinition: ${f.desc}\nSensitivity: ${f.effect}`).join('\n\n')}
function showStraightSettings(){document.getElementById('contextMenu').style.display='none';alert('Current Straight Flight settings and calculation\n\n'+settingsText())}
function openStraightSettingsModal(){document.getElementById('contextMenu').style.display='none';let p=currentStraightParams(),grid=document.getElementById('settingsGrid');grid.innerHTML='';straightFields.forEach(f=>{let card=document.createElement('div');card.className='setting-card';let lab=document.createElement('label');lab.textContent=f.name+' ['+f.unit+']';let key=document.createElement('div');key.className='setting-key';key.textContent='Parameter: '+f.key;let inp=document.createElement('input');inp.type='number';inp.step='any';inp.id='sf_'+f.key;inp.value=p[f.key]??f.def;let desc=document.createElement('div');desc.className='setting-desc';desc.textContent=f.desc;let eff=document.createElement('div');eff.className='setting-effect';eff.textContent='Sensitivity: '+f.effect;card.appendChild(lab);card.appendChild(key);card.appendChild(inp);card.appendChild(desc);card.appendChild(eff);grid.appendChild(card)});document.getElementById('settingsModal').style.display='flex'}
function closeSettingsModal(){document.getElementById('settingsModal').style.display='none'}
function readStraightSettings(){let params={};for(let f of straightFields){let el=document.getElementById('sf_'+f.key);let n=parseFloat(el.value);if(!Number.isFinite(n)){alert('Invalid number: '+f.name);return null}params[f.key]=n}return params}
async function startStraightAnalyze(){if(uiBusy)return;if(!data){alert('Analyze first.');return}let params=readStraightSettings();if(!params)return;closeSettingsModal();setBusy(true,'Recalculating Straight Flight','Applying thresholds to the existing 1 Hz table...');let r=await post('/recalculate_straight',{params:params,fname:fname.value,out:out.value});document.getElementById('progressText').textContent=r.message||'Done';await refresh();showLayer('straight');setTimeout(()=>setBusy(false),500)}
function applyFocusMode(){
 if(!requestedFocus)return;
 let top=document.getElementById('topCard'),mapCard=document.getElementById('mapCard'),statsCard=document.getElementById('statsCard');
 let header=document.querySelector('header'),footer=document.querySelector('footer'),main=document.querySelector('main');
 if(header)header.style.display='none'; if(top)top.style.display='none';
 if(main)main.style.padding='0';
 if(requestedFocus==='map'){
  if(statsCard)statsCard.style.display='none';
  if(mapCard){mapCard.style.margin='0';mapCard.style.padding='0';mapCard.style.border='0';mapCard.style.borderRadius='0';mapCard.style.boxShadow='none'}
  let mh=document.querySelector('#mapCard h3'),mt=document.querySelector('#mapCard > .row'); if(mh)mh.style.display='none'; if(mt)mt.style.display='flex'; if(footer)footer.style.display='block';
  let m=document.getElementById('map'); if(m){m.style.height='calc(100vh - 94px)';m.style.border='0';m.style.borderRadius='0'}
  setTimeout(()=>{if(map){map.invalidateSize();resetMap()}},250);
 }else if(requestedFocus==='stats'){
  if(footer)footer.style.display='none';
  if(mapCard)mapCard.style.display='none';
  if(statsCard){statsCard.style.margin='0';statsCard.style.border='0';statsCard.style.boxShadow='none'}
 }
}
function showStatsPrompt(){
 let div=document.getElementById('stats');
 if(!div)return;
 div.className='';
 if(!div.dataset.prompted){
  div.innerHTML='<div style="padding:22px;color:#0a363c;font-weight:700">Analysis is ready. Select Histogram, Frequency, Altitude profile, or Wind spectra to display the statistical overview.</div>';
  div.dataset.prompted='1';
 }
}async function refresh(){
 try{
  let resp=await fetch('/data',{cache:'no-store'});
  let r=await resp.json();
  if(!resp.ok||r.error){throw new Error(r.error||('HTTP '+resp.status));}
  if(r.ready){
   data=r;
   if(requestedLayer&&['route','wind','straight'].includes(requestedLayer))active=requestedLayer;
   drawMap();
   if(requestedFocus==='stats'){
    showStats(['hist','freq','alt','spectra'].includes(requestedStat)?requestedStat:'hist')
   }else{
    if(requestedFocus==='map'&&requestedLayer)showLayer(requestedLayer);
    else showStatsPrompt();
   }
   applyFocusMode();
   if(requestedFocus==='map'&&requestedLayer)setTimeout(()=>{showLayer(requestedLayer);resetMap();if(map)map.invalidateSize()},150);
  }else{
   let s=await fetch('/status',{cache:'no-store'}).then(x=>x.json()).catch(()=>({busy:true,message:'Waiting for analysis data'}));
   setStatus(s);
   if(s.busy){setTimeout(refresh,2000);return;}
   let msg=s.message||'No analyzed data are available yet. Load data and run Analyze.';
   if(msg.toLowerCase().includes('failed'))showReadyMessage(msg);
   else setTimeout(refresh,2000);
  }
 }catch(e){
  showReadyMessage('Visualization data could not be loaded: '+(e&&e.message?e.message:e));
 }
}
function showReadyMessage(msg){
 let mapDiv=document.getElementById('map');
 if(mapDiv&&!map){mapDiv.innerHTML='<div style="padding:24px;font-weight:700;color:#0a363c">'+String(msg).replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))+'</div>';}
 let st=document.getElementById('status'); if(st){st.textContent=msg;st.title=msg;}
 setBusy(false);
}
function validLL(p){return Array.isArray(p)&&p.length>=2&&Number.isFinite(Number(p[0]))&&Number.isFinite(Number(p[1]))&&Math.abs(Number(p[0]))<=90&&Math.abs(Number(p[1]))<=180}
function cleanCoords(arr){return (arr||[]).filter(validLL).map(p=>[Number(p[0]),Number(p[1])])}
function bboxFromCoords(coords){let c=cleanCoords(coords);if(!c.length)return null;let south=90,north=-90,west=180,east=-180;c.forEach(p=>{let lat=p[0],lon=p[1];if(lat<south)south=lat;if(lat>north)north=lat;if(lon<west)west=lon;if(lon>east)east=lon});return {south:south,west:west,north:north,east:east}}
function activeCoords(k){k=k||active;if(k==='straight'){let out=[];(data.straightLegs||[]).forEach(l=>{out=out.concat(cleanCoords(l.coords||[]))});return out.length?out:cleanCoords(data.route||[])}return cleanCoords(data.route||[])}
function addBufferToBBox(b){if(!b)return null;let buf=Math.max(0,parseFloat(document.getElementById('buffer').value||0));let mid=(b.south+b.north)/2,dlat=buf/111320,dlon=buf/(111320*Math.max(0.15,Math.cos(mid*Math.PI/180)));return {south:b.south-dlat,west:b.west-dlon,north:b.north+dlat,east:b.east+dlon}}
function expandedBounds(){let b=data&&Array.isArray(data.bounds)&&validLL(data.bounds[0])&&validLL(data.bounds[1])?{south:Number(data.bounds[0][0]),west:Number(data.bounds[0][1]),north:Number(data.bounds[1][0]),east:Number(data.bounds[1][1])}:bboxFromCoords(data&&data.route);return addBufferToBBox(b||{south:47.55,west:9.15,north:47.75,east:9.65})}
function bboxCorners(b){return [[b.south,b.west],[b.south,b.east],[b.north,b.east],[b.north,b.west],[b.south,b.west]]}
function viewFromBBox(b){
 if(!b)return {center:[47.64,9.38],zoom:11};
 let center=[(b.south+b.north)/2,(b.west+b.east)/2];
 let span=Math.max(Math.abs(b.north-b.south),Math.abs(b.east-b.west)*Math.max(0.25,Math.cos(center[0]*Math.PI/180)));
 let zoom=span>2?7:span>1?8:span>0.55?9:span>0.28?10:span>0.14?11:span>0.07?12:span>0.035?13:14;
 return {center:center,zoom:zoom};
}
function drawBufferBox(){
 try{
  if(!map||!data)return;
  if(bufferRect){try{bufferRect.removeFrom(map)}catch(e){} bufferRect=null;}
  let b=expandedBounds();
  if(!b)return;
  bufferRect=L.polyline(bboxCorners(b),{color:'#111',weight:1,dashArray:'6,6',opacity:.65,interactive:false,pane:'overlayPane'}).addTo(map);
 }catch(e){clientLog('ERROR','Buffer box update failed: '+e.message)}
}
function resetMap(){
 try{
  if(!map||!data)return;
  let c=activeCoords(active);
  if(!c.length)c=cleanCoords(data.route||[]);
  if(!c.length)return;
  let b=addBufferToBBox(bboxFromCoords(c));
  if(!b)return;
  let v=viewFromBBox(b);
  map.setView(v.center,v.zoom,{animate:false});
  setTimeout(()=>{try{drawBufferBox();map.invalidateSize(false)}catch(e){}},80);
 }catch(e){clientLog('ERROR','Map reset failed: '+e.message)}
}
function mapLineWidth(){return Math.max(1,Math.min(20,parseFloat(document.getElementById('lineWidth').value||5)))}
function redrawMapLines(){scheduleMapUpdate(()=>drawMap(),'Redrawing map line widths...')}
function fmtDuration(s){s=Math.round(s||0);let m=Math.floor(s/60),r=s%60;return m+':'+String(r).padStart(2,'0')}
function windColor(v,min,max){if(!Number.isFinite(v))return '#999';let t=Math.max(0,Math.min(1,(v-min)/(max-min||1)));let r=Math.round(49+206*t),g=Math.round(130+65*(1-Math.abs(t-.5)*2)),b=Math.round(189*(1-t)+40*t);return `rgb(${r},${g},${b})`}
function windRoseSvg(samples){
 let vals=(samples||[]).filter(d=>Number.isFinite(d.dir)&&Number.isFinite(d.spd));
 let n=vals.length;if(!n)return '<div style="padding:12px;text-align:center;color:#666">No wind data for this leg</div>';
 let dirs=16,bins=6,cx=112,cy=112,R=82,inner=15;
 let speeds=vals.map(d=>d.spd).sort((a,b)=>a-b),minSpd=speeds[0],maxSpd=speeds[speeds.length-1];
 if(maxSpd<=minSpd)maxSpd=minSpd+1;
 let edges=[];for(let i=0;i<=bins;i++)edges.push(minSpd+(maxSpd-minSpd)*i/bins);
 let counts=Array.from({length:dirs},()=>Array(bins).fill(0));
 vals.forEach(d=>{let si=Math.min(bins-1,Math.max(0,Math.floor((d.spd-minSpd)/(maxSpd-minSpd)*bins)));let di=Math.floor(((d.dir+360/dirs/2)%360)/(360/dirs))%dirs;counts[di][si]++});
 let totals=counts.map(a=>a.reduce((x,y)=>x+y,0));let maxPct=Math.max(1,...totals.map(c=>100*c/n));let maxRing=Math.ceil(maxPct/2)*2;
 let rings=[.25,.5,.75,1].map(fr=>{let r=inner+(R-inner)*fr,p=(maxRing*fr).toFixed(0);return `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="#bdbdbd"/><text x="${cx+4}" y="${cy-r+10}" font-size="8" fill="#555">${p}%</text>`}).join('');
 let axes=[0,90,180,270].map(a=>{let rad=(a-90)*Math.PI/180;return `<line x1="${cx}" y1="${cy}" x2="${cx+R*Math.cos(rad)}" y2="${cy+R*Math.sin(rad)}" stroke="#999"/>`}).join('');
 function arcPath(r0,r1,a0,a1){let p=Math.PI/180,sa=(a0-90)*p,ea=(a1-90)*p,large=(a1-a0)>180?1:0;let x00=cx+r0*Math.cos(sa),y00=cy+r0*Math.sin(sa),x01=cx+r0*Math.cos(ea),y01=cy+r0*Math.sin(ea),x10=cx+r1*Math.cos(sa),y10=cy+r1*Math.sin(sa),x11=cx+r1*Math.cos(ea),y11=cy+r1*Math.sin(ea);return `M${x00},${y00} L${x10},${y10} A${r1},${r1} 0 ${large} 1 ${x11},${y11} L${x01},${y01} A${r0},${r0} 0 ${large} 0 ${x00},${y00} Z`}
 let sectors='';for(let di=0;di<dirs;di++){let pct=100*totals[di]/n;if(!pct)continue;let a0=di*360/dirs-360/dirs/2+2,a1=(di+1)*360/dirs-360/dirs/2-2;let r0=inner;for(let bi=0;bi<bins;bi++){let c=counts[di][bi];if(!c)continue;let dr=(R-inner)*(100*c/n)/maxRing;let r1=Math.min(R,r0+dr);sectors+=`<path d="${arcPath(r0,r1,a0,a1)}" fill="${windColor((edges[bi]+edges[bi+1])/2,minSpd,maxSpd)}" stroke="#333" stroke-width="0.5"/>`;r0=r1}}
 let labels=`<text x="${cx}" y="14" text-anchor="middle" font-size="13" font-weight="600">N</text><text x="${cx}" y="222" text-anchor="middle" font-size="13" font-weight="600">S</text><text x="8" y="116" font-size="13" font-weight="600">W</text><text x="208" y="116" font-size="13" font-weight="600">E</text>`;
 let bar='',legend='';for(let i=0;i<48;i++){let f=1-i/47,y=35+i*3;bar+=`<rect x="248" y="${y}" width="12" height="3" fill="${windColor(minSpd+f*(maxSpd-minSpd),minSpd,maxSpd)}"/>`}for(let i=0;i<=bins;i++){let y=35+(1-i/bins)*144,val=edges[i];legend+=`<line x1="246" x2="263" y1="${y}" y2="${y}" stroke="#333"/><text x="268" y="${y+3}" font-size="9">${val.toFixed(1)}</text>`}
 return `<svg viewBox="0 0 305 235" aria-label="Windrose"><text x="248" y="20" font-size="11" font-weight="600">Wind Speed</text><text x="248" y="32" font-size="10">m/s</text>${rings}${axes}${sectors}${labels}<circle cx="${cx}" cy="${cy}" r="${inner}" fill="#cfe8f3" stroke="#555"/><text x="${cx}" y="${cy+3}" text-anchor="middle" font-size="9">${(100*counts.reduce((a,b)=>a+(b[0]||0),0)/n).toFixed(1)}%</text>${bar}${legend}</svg>`
}
function hideLegInfo(){if(legInfoControl){map.removeControl(legInfoControl);legInfoControl=null}selectedLegId=null}
function showLegInfo(leg){if(selectedLegId===leg.id){hideLegInfo();return}selectedLegId=leg.id;if(!legInfoControl){legInfoControl=L.control({position:'topright'});legInfoControl.onAdd=()=>L.DomUtil.create('div','leg-info');legInfoControl.addTo(map)}let el=legInfoControl.getContainer();L.DomEvent.disableClickPropagation(el);el.innerHTML=`<h4>Straight Flight leg ${leg.id}</h4><table><tr><td>Length</td><td>${(leg.distance_km||0).toFixed(2)} km</td></tr><tr><td>Duration</td><td>${fmtDuration(leg.duration_s)} mm:ss</td></tr><tr><td>Mean aircraft speed</td><td>${Number.isFinite(leg.mean_speed_mps)?leg.mean_speed_mps.toFixed(2):'n/a'} m/s</td></tr><tr><td>Mean wind</td><td>${Number.isFinite(leg.mean_wind_mps)?leg.mean_wind_mps.toFixed(2):'n/a'} m/s</td></tr><tr><td>Mean heading</td><td>${Number.isFinite(leg.mean_heading_deg)?leg.mean_heading_deg.toFixed(1):'n/a'} deg</td></tr><tr><td>Heading SD</td><td>${Number.isFinite(leg.heading_std_deg)?leg.heading_std_deg.toFixed(1):'n/a'} deg</td></tr><tr><td>Max roll</td><td>${Number.isFinite(leg.max_roll_deg)?leg.max_roll_deg.toFixed(1):'n/a'} deg</td></tr><tr><td>Altitude range</td><td>${Number.isFinite(leg.altitude_range_m)?leg.altitude_range_m.toFixed(0):'n/a'} m</td></tr></table>${windRoseSvg(leg.windSamples)}<div style="font-size:11px;color:#555">Click same leg again to close.</div>`}
function showConnectorInfo(legs){if(selectedLegId==='connector'){hideLegInfo();return}selectedLegId='connector';if(!legInfoControl){legInfoControl=L.control({position:'topright'});legInfoControl.onAdd=()=>L.DomUtil.create('div','leg-info');legInfoControl.addTo(map)}let el=legInfoControl.getContainer();L.DomEvent.disableClickPropagation(el);let totalKm=legs.reduce((a,l)=>a+(l.distance_km||0),0),totalS=legs.reduce((a,l)=>a+(l.duration_s||0),0);let allWind=legs.flatMap(l=>l.windSamples||[]);let meanWind=allWind.length?allWind.reduce((a,w)=>a+(w.spd||0),0)/allWind.length:NaN;el.innerHTML=`<h4>Measured flight-track reference</h4><table><tr><td>Accepted Straight Flight legs</td><td>${legs.length}</td></tr><tr><td>Total accepted-leg length</td><td>${totalKm.toFixed(2)} km</td></tr><tr><td>Total accepted-leg duration</td><td>${fmtDuration(totalS)} mm:ss</td></tr><tr><td>Mean wind in accepted legs</td><td>${Number.isFinite(meanWind)?meanWind.toFixed(2):'n/a'} m/s</td></tr></table>${windRoseSvg(allWind)}<div style="font-size:11px;color:#555">The black dashed curve is the measured flight-track reference. It is not itself a classified Straight Flight leg; only orange numbered sections passed the straight-flight criteria. Click the dashed curve again to close.</div>`}
function ensureMap(){
 if(map)return;
 let initial=expandedBounds();let v=viewFromBBox(initial);
 map=L.map('map',{preferCanvas:true}).setView(v.center,v.zoom);
 L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© OpenStreetMap contributors'}).addTo(map);
 L.control.scale({metric:true,imperial:false}).addTo(map);
 legendControl=L.control({position:'bottomright'});legendControl.onAdd=()=>{let d=L.DomUtil.create('div','legend');d.innerHTML='<b>Wind speed [m/s]</b><div class="row"><div class="legendbar"></div><div><div>'+((data&&data.windMax)!=null?data.windMax:'')+'</div><div style="height:180px"></div><div>'+((data&&data.windMin)!=null?data.windMin:'')+'</div></div></div>';return d};
 map.on('resize',()=>{try{map.invalidateSize(false)}catch(e){}});
}
function makeLayerGroup(items){return L.layerGroup(items.filter(Boolean))}
function routeLayer(route,lw){return makeLayerGroup([L.polyline(route,{color:'#1565c0',weight:lw,opacity:.95,interactive:false}).bindTooltip('Flight route')])}
function windLayer(route,lw){let visible=[],hover=[];if(route.length>1)visible.push(L.polyline(route,{color:'#062c33',weight:lw+5,opacity:.35,interactive:false}).bindTooltip('Wind-speed flight track'));(data.windSegments||[]).forEach(s=>{let c=cleanCoords(s.coords||[]);if(c.length>=2){let w=Number(s.wind);let label='Wind speed: '+(Number.isFinite(w)?w.toFixed(2):((s.wind!==undefined&&s.wind!==null)?s.wind:'n/a'))+' m/s';visible.push(L.polyline(c,{color:s.color||'#1565c0',weight:lw+2,opacity:.98,interactive:false}));hover.push(L.polyline(c,{color:'#000000',weight:Math.max(18,lw+14),opacity:0,fillOpacity:0,interactive:true,bubblingMouseEvents:false}).bindTooltip(label,{sticky:true,direction:'top',opacity:0.98,className:'wind-hover-tooltip'}))}});return makeLayerGroup(visible.concat(hover))}
function straightLayer(route,lw){let legs=data.straightLegs||[];let items=[];if(route.length>1)items.push(L.polyline(route,{color:'#111',weight:4,opacity:.62,dashArray:'5,9',interactive:true}).bindTooltip('Measured flight-track reference').on('click',()=>showConnectorInfo(legs)));legs.forEach(leg=>{let c=cleanCoords(leg.coords||[]);if(c.length>=2)items.push(L.polyline(c,{color:'#ff7f0e',weight:lw+3,opacity:1,interactive:true}).bindTooltip('Straight Flight leg '+leg.id).on('click',()=>showLegInfo(leg)));if(validLL(leg.label))items.push(L.marker(leg.label,{icon:L.divIcon({className:'leg-label',html:String(leg.id),iconSize:[24,24],iconAnchor:[12,12]})}).bindTooltip('Straight Flight leg '+leg.id).on('click',()=>showLegInfo(leg)))});if(route.length){items.push(L.circleMarker(route[0],{radius:7,fillColor:'#2ca02c',color:'#111',weight:2,fillOpacity:1,interactive:false}).bindTooltip('Takeoff'));items.push(L.circleMarker(route[route.length-1],{radius:7,fillColor:'#d62728',color:'#111',weight:2,fillOpacity:1,interactive:false}).bindTooltip('Landing'))}return makeLayerGroup(items)}
function drawMap(){
 try{
  ensureMap();
  let route=cleanCoords(data.route||[]);if(!route.length){clientLog('ERROR','Map drawing failed: no valid latitude/longitude points');return}
  Object.values(layers).forEach(l=>{try{l.removeFrom(map)}catch(e){}});layers={};
  hideLegInfo();
  if(mapInfoControl){try{map.removeControl(mapInfoControl)}catch(e){} mapInfoControl=null;}
  let lw=mapLineWidth();
  layers.route=routeLayer(route,lw);
  layers.wind=windLayer(route,lw);
  layers.straight=straightLayer(route,lw);
  showLayer(active);
  if(!initialFocusFitDone){initialFocusFitDone=true;setTimeout(()=>{resetMap();if(map)map.invalidateSize(false)},200)}
 }catch(e){clientLog('ERROR','Map drawing failed: '+e.message);throw e}
}
function updateMap(){scheduleMapUpdate(()=>resetMap(),'Updating map buffer...')}
function showMapInfo(k){if(!map)return;if(!mapInfoControl){mapInfoControl=L.control({position:'topright'});mapInfoControl.onAdd=()=>L.DomUtil.create('div','map-info');mapInfoControl.addTo(map)}let n=0;if(k==='wind')n=(data.windSegments||[]).length;else if(k==='straight')n=(data.straightLegs||[]).length;else n=(data.route||[]).length;let label=k==='wind'?'Wind-speed segments':(k==='straight'?'Straight Flight legs':'Route points');mapInfoControl.getContainer().innerHTML=label+': '+n.toLocaleString();}
function showLayer(k){
 active=['route','wind','straight'].includes(k)?k:'route';
 if(!data||!map)return;
 Object.values(layers).forEach(l=>{try{l.removeFrom(map)}catch(e){}});
 try{if(layers[active])layers[active].addTo(map)}catch(e){clientLog('ERROR','Layer display failed for '+active+': '+e.message)}
 drawBufferBox();
 if(legendControl){if(active==='wind'){try{legendControl.addTo(map)}catch(e){}}else{try{map.removeControl(legendControl)}catch(e){}}}
 if(requestedFocus==='map'){showMapInfo(active)}
 ['route','wind','straight'].forEach(x=>{let b=document.getElementById(x+'Btn');if(b)b.classList.toggle('active',x==active)})
}function finiteArray(a){return a.filter(v=>v!==null && Number.isFinite(v)).sort((x,y)=>x-y)}
function pct(a,p){let v=finiteArray(a);if(!v.length)return null;let i=(v.length-1)*p/100,lo=Math.floor(i),hi=Math.ceil(i);return v[lo]+(v[hi]-v[lo])*(i-lo)}
function showStats(k){
 if(!data)return;activeStat=k;
 ['hist','freq','alt','spectra'].forEach(x=>{let b=document.getElementById(x+'Btn');if(b)b.classList.toggle('active',x==k)});
 let div=document.getElementById('stats');div.innerHTML='';
 if(k==='hist'){
  div.className='grid';
  let labels={wind_mps:'Wind speed [m/s]',wind_u_mps:'u [m/s]',wind_v_mps:'v [m/s]',wind_w_mps:'w vertical [m/s]',air_temp_degC:'Air temperature [Â°C]',rel_humidity_pct:'Relative humidity [%]'};
  for(let c in labels){
   if(!data.hist[c])continue;
   let el=document.createElement('div');el.className='plot';div.appendChild(el);
   Plotly.newPlot(el,[{x:data.hist[c].filter(v=>v!==null),type:'histogram',marker:{color:'#9fd3e9',line:{color:'#555',width:1}}}],{title:labels[c],margin:{l:50,r:20,t:35,b:45},font:{size:11},yaxis:{title:'Count'},xaxis:{title:labels[c]}},{responsive:true,scrollZoom:true,displayModeBar:true,displaylogo:false,doubleClick:'reset'})
  }
 }
 else if(k==='freq'){
  div.className='';let el=document.createElement('div');el.style.height='520px';div.appendChild(el);
  let f=(data.freq||[]).map(v=>v===null?null:Number(v));let x=(data.freqTime&&data.freqTime.length===f.length)?data.freqTime:f.map((_,i)=>i+1);
  let valid=f.filter(v=>Number.isFinite(v)).sort((a,b)=>a-b);
  let med=valid.length?valid[Math.floor(valid.length/2)]:null;
  let tr=[{x:x,y:f,type:'scattergl',mode:'markers',name:'Acquisition frequency',marker:{color:'rgba(21,101,192,0.70)',size:4,symbol:'circle'},hovertemplate:'Time/sample=%{x}<br>Frequency=%{y:.3f} Hz<extra></extra>'}];
  Plotly.newPlot(el,tr,{title:'Acquisition frequency time series',xaxis:{title:(data.freqTime&&data.freqTime.length===f.length?'Time from Airflow_UTCcorr_Nanoseconds_ns':'Sample number'),showgrid:true},yaxis:{title:'Frequency [Hz]',showgrid:true},legend:{orientation:'h',x:0.02,y:1.10},margin:{l:75,r:30,t:55,b:65},paper_bgcolor:'white',plot_bgcolor:'white'},{responsive:true,scrollZoom:true,displayModeBar:true,displaylogo:false,doubleClick:'reset'}) }
 else if(k==='spectra'){
  div.className='';let el=document.createElement('div');el.style.height='650px';div.appendChild(el);
  let spectra=data.spectra||{};
  let total=spectra.wind_mps, vertical=spectra.wind_w_mps;
  let specs=[total,vertical].filter(s=>s&&s.frequency_hz&&s.frequency_hz.length);
  if(!specs.length){el.innerHTML='<p style="padding:20px">No wind spectra available. Re-run Analyze after loading original-resolution noseboom data containing wind speed columns.</p>';return}
  let traces=[];
  if(total&&total.frequency_hz&&total.frequency_hz.length){traces.push({x:total.frequency_hz,y:total.psd,mode:'lines',name:'Total wind speed',line:{color:'#111',width:2.2},hovertemplate:'Total wind speed<br>Frequency=%{x:.3g} Hz<br>PSD=%{y:.3e} [(m/s)^2 Hz^-1]<extra></extra>'})}
  if(vertical&&vertical.frequency_hz&&vertical.frequency_hz.length){traces.push({x:vertical.frequency_hz,y:vertical.psd,mode:'lines',name:'Vertical wind component',line:{color:'#2c7fb8',width:2.0},hovertemplate:'Vertical wind component<br>Frequency=%{x:.3g} Hz<br>PSD=%{y:.3e} [(m/s)^2 Hz^-1]<extra></extra>'})}
  let ref=total||vertical;
  let fs=(ref.fs_hz||0), win=(ref.window_seconds||0), n=(ref.n_samples||0);
  let xMin=0.01, xMax=Math.max(0.02,fs/2);
  if(ref.frequency_hz.length>10){
   let anchorIdx=ref.frequency_hz.findIndex(v=>v>=0.02);
   if(anchorIdx<0)anchorIdx=0;
   let f0=ref.frequency_hz[anchorIdx], p0=ref.psd[anchorIdx];
   let fx=[]; for(let f=xMin; f<=xMax*1.001; f*=1.035){fx.push(f)}
   let py=fx.map(f=>p0*Math.pow(f/f0,-5/3));
   traces.push({x:fx,y:py,mode:'lines',name:'f<sup>-5/3</sup> reference',line:{color:'rgba(100,100,100,0.85)',width:2.6,dash:'dash'},hoverinfo:'skip'});
  }
  let note='Median sampling rate: '+fs.toFixed(3)+' Hz<br>Welch window: '+win.toFixed(1)+' s<br>Samples after trim: '+n.toLocaleString();
  let xTicks=[0.01,0.1,1,10].filter(v=>v<=xMax*1.01);
  let xTickText=xTicks.map(v=>v===0.01?'10<sup>-2</sup>':v===0.1?'10<sup>-1</sup>':v===1?'10<sup>0</sup>':'10<sup>1</sup>');
  Plotly.newPlot(el,traces,{title:{text:'Noseboom wind power spectrum',font:{size:24}},xaxis:{title:{text:'Frequency [Hz]',font:{size:18}},type:'log',range:[Math.log10(xMin),Math.log10(xMax)],tickmode:'array',tickvals:xTicks,ticktext:xTickText,showgrid:true,gridcolor:'rgba(140,140,140,0.30)',minor:{showgrid:true,gridcolor:'rgba(140,140,140,0.14)'},tickfont:{size:14}},yaxis:{title:{text:'Power spectral density [(m s<sup>-1</sup>)<sup>2</sup> Hz<sup>-1</sup>]',font:{size:18}},type:'log',showgrid:true,gridcolor:'rgba(140,140,140,0.30)',minor:{showgrid:true,gridcolor:'rgba(140,140,140,0.14)'},tickfont:{size:14}},legend:{x:0.68,y:0.98,bgcolor:'rgba(255,255,255,0.86)',bordercolor:'rgba(70,70,70,0.7)',borderwidth:1,font:{size:16}},annotations:[{xref:'paper',yref:'paper',x:0.02,y:0.06,showarrow:false,align:'left',text:note,font:{size:15,color:'#111'},bgcolor:'rgba(255,255,255,0.88)',bordercolor:'rgba(90,90,90,0.75)',borderwidth:1,borderpad:8}],margin:{l:105,r:35,t:70,b:80},paper_bgcolor:'white',plot_bgcolor:'white'},{responsive:true,scrollZoom:true,displayModeBar:true,displaylogo:false,doubleClick:'reset'}) }
 else{
  div.className='';let el=document.createElement('div');el.style.height='560px';div.appendChild(el);
  let tx=data.terrain.map(p=>p[0]),ty=data.terrain.map(p=>p[1]);let ay=data.altitude.map(p=>p[1]),ey=data.ellipsoid.map(p=>p[1]);let t2=pct(ty,2),t98=pct(ty,98),a2=pct(ay.concat(ey),2),a98=pct(ay.concat(ey),98);let floor=(t2===null?null:Math.max(0,t2-20));let yMin=Math.min(...[floor,a2].filter(v=>v!==null))-20;let yMax=Math.max(...[t98,a98].filter(v=>v!==null))+30;let terrainBase=ty.map(v=>(v===null||!Number.isFinite(v)||floor===null)?null:floor);let tr=[{x:tx,y:terrainBase,name:'DTM display floor',mode:'lines',line:{color:'rgba(0,0,0,0)',width:0},showlegend:false,hoverinfo:'skip'},{x:tx,y:ty,name:'Satellite DTM terrain',mode:'lines',fill:'tonexty',fillcolor:'rgba(145,145,145,0.32)',line:{color:'rgba(85,85,85,0.95)',width:1.5}},{x:data.altitude.map(p=>p[0]),y:ay,name:'Flight altitude (GNSS MSL)',mode:'lines',line:{color:'#1f77b4',width:2}},{x:data.ellipsoid.map(p=>p[0]),y:ey,name:'INS ellipsoid height',mode:'lines',line:{color:'#ff7f0e',width:2}}];
  Plotly.newPlot(el,tr,{title:'Altitude profile with satellite DTM',xaxis:{title:'Time'},yaxis:{title:'Height [m]',range:[yMin,yMax]},margin:{l:70,r:30,t:45,b:55}},{responsive:true,scrollZoom:true,displayModeBar:true,displaylogo:false,doubleClick:'reset'})
 }
}
function setupContextMenu(){document.querySelectorAll('button[data-fullscreen],button[data-panel]').forEach(btn=>btn.addEventListener('contextmenu',e=>{e.preventDefault();e.stopPropagation();contextTarget=btn.dataset.fullscreen||'';contextPanel=btn.dataset.panel||'';contextView=btn.dataset.view||'';if(!contextPanel&&contextTarget==='#map')contextPanel='map';if(!contextPanel&&contextTarget==='#stats')contextPanel='stats';let isStraight=btn.id==='straightBtn'||btn.dataset.straightSettings==='1';document.getElementById('ctxFull').style.display=contextTarget?'block':'none';document.getElementById('ctxCurrent').style.display=isStraight?'block':'none';document.getElementById('ctxChange').style.display=isStraight?'block':'none';let m=document.getElementById('contextMenu'),nt=document.getElementById('ctxNewTab');m.dataset.panel=contextPanel;m.dataset.view=contextView;m.dataset.target=contextTarget;if(nt){nt.dataset.panel=contextPanel;nt.dataset.view=contextView;nt.dataset.target=contextTarget}m.style.left=e.pageX+'px';m.style.top=e.pageY+'px';m.style.display='block'}));document.getElementById('contextMenu').addEventListener('contextmenu',e=>{e.preventDefault();e.stopPropagation()});document.addEventListener('click',e=>{if(!e.target.closest('#contextMenu'))document.getElementById('contextMenu').style.display='none'});document.addEventListener('fullscreenchange',()=>{if(map)setTimeout(()=>map.invalidateSize(),250);document.querySelectorAll('.js-plotly-plot').forEach(el=>Plotly.Plots.resize(el))})}
function openContextFullScreen(){let el=document.querySelector(contextTarget||'#stats');document.getElementById('contextMenu').style.display='none';if(el&&el.requestFullscreen)el.requestFullscreen()}
function openContextNewTab(){
 let m=document.getElementById('contextMenu'),nt=document.getElementById('ctxNewTab');m.style.display='none';
 let panel=contextPanel||nt?.dataset.panel||m.dataset.panel||'';
 let view=contextView||nt?.dataset.view||m.dataset.view||'';
 let target=contextTarget||nt?.dataset.target||m.dataset.target||'';
 if(!panel&&target==='#map')panel='map'; if(!panel&&target==='#stats')panel='stats';
 let u;
 if(panel==='map'){
  view=(['route','wind','straight'].includes(view)?view:(active||'route'));
  u=new URL('/map/'+view,window.location.origin);u.searchParams.set('focus','map');u.searchParams.set('layer',view);u.searchParams.set('buffer',document.getElementById('buffer').value||'500');u.searchParams.set('lineWidth',document.getElementById('lineWidth').value||'5');
 }else if(panel==='stats'){
  view=(['hist','freq','alt','spectra'].includes(view)?view:(activeStat||'hist'));
  let statPath={hist:'histogram',freq:'frequency',alt:'altitude',spectra:'spectra'}[view]||'histogram';
  u=new URL('/stats/'+statPath,window.location.origin);u.searchParams.set('focus','stats');u.searchParams.set('stat',view);
 }else{alert('Please right-click a map or statistical plot button first.');return}
 window.open(u.toString(),'_blank','noopener');
}setupContextMenu();refresh();
</script></body></html>
'''

def save_current_project_snapshot(output=None, flight_name=''):
    """Safely write the current analysis state and session log into the project HDF5 file."""
    project=None
    flight=flight_name or STATE.flight_name or 'Flight'
    if STATE.d1 is not None and STATE.straight is not None:
        if STATE.project_path:
            project=Path(STATE.project_path)
        else:
            base=Path(output or STATE.output or DEFAULT_OUTPUT_ROOT)/safe_name(flight)
            project=project_file_path(base, safe_name(flight))
        STATE.summary=STATE.summary or {}
        STATE.summary['project_file']=str(project)
        STATE.summary['last_saved_utc']=pd.Timestamp.utcnow().isoformat()
        save_project(project, STATE.d1, STATE.straight, STATE.freq if STATE.freq is not None else pd.DataFrame({'frequency_hz':[]}), STATE.spectra, STATE.summary, STATE.export_data)
        STATE.project_path=project
        return project
    if STATE.project_path and Path(STATE.project_path).exists():
        flush_project_logs(STATE.project_path)
        return Path(STATE.project_path)
    return None

def choose_windows_folder(title, initial):
    """Open a native Windows folder-selection dialog from the local server process."""
    title = str(title or 'Select folder')
    initial = str(initial or '')
    # Primary method: launch a tiny separate Python/Tk dialog. This is more reliable from a browser server thread.
    py_code = (
        "import sys, os, tkinter as tk; from tkinter import filedialog; "
        "title=sys.argv[1]; initial=sys.argv[2] if len(sys.argv)>2 else ''; "
        "root=tk.Tk(); root.withdraw(); root.attributes('-topmost', True); root.update(); "
        "kw={'title': title}; "
        "\nif initial and os.path.isdir(initial): kw['initialdir']=initial\n"
        "path=filedialog.askdirectory(**kw); "
        "print(path or ''); root.destroy()"
    )
    try:
        cp = subprocess.run([sys.executable, '-c', py_code, title, initial], capture_output=True, text=True, timeout=300)
        if cp.returncode == 0 and cp.stdout.strip():
            return cp.stdout.strip().splitlines()[-1]
        if cp.returncode != 0:
            add_log('ERROR', 'Python folder picker failed: ' + (cp.stderr.strip() or 'Tk dialog returned an error.'))
    except Exception as exc:
        add_log('ERROR', 'Python folder picker failed: ' + str(exc))
    # Fallback method: PowerShell FolderBrowserDialog.
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$d=New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$d.Description=" + json.dumps(title) + "; "
        "$d.ShowNewFolderButton=$true; "
        "$p=" + json.dumps(initial) + "; "
        "if($p -and (Test-Path -LiteralPath $p)){$d.SelectedPath=$p}; "
        "if($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK){$d.SelectedPath}"
    )
    try:
        cp = subprocess.run(['powershell', '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass', '-Command', script], capture_output=True, text=True, timeout=300)
        if cp.returncode == 0:
            return cp.stdout.strip()
        add_log('ERROR', 'PowerShell folder picker failed: ' + (cp.stderr.strip() or 'Dialog returned an error.'))
    except Exception as exc:
        add_log('ERROR', 'PowerShell folder picker failed: ' + str(exc))
    return ''

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return
    def send_json(self, obj, status=200):
        body=json.dumps(obj).encode('utf-8')
        self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def send_html(self):
        body=HTML.encode('utf-8')
        self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def read_json(self):
        n=int(self.headers.get('Content-Length','0') or 0)
        if not n: return {}
        return json.loads(self.rfile.read(n).decode('utf-8'))
    def do_GET(self):
        path=urlparse(self.path).path
        try:
            if path=='/' or path.startswith('/map') or path.startswith('/stats'): return self.send_html()
            if path=='/status': return self.send_json(STATE.status)
            if path=='/logs': return self.send_json({'logs':STATE.logs[-1200:]})
            if path=='/data': return self.send_json(api_payload())
            if path=='/export_result': return self.send_json(STATE.last_export or {'ok':False,'message':'No export has completed in this session.'})
            self.send_response(404); self.end_headers()
        except Exception as exc:
            add_log('ERROR', str(exc))
            self.send_json({'error':str(exc)},500)
    def do_POST(self):
        path=urlparse(self.path).path
        try:
            req=self.read_json()
            root_text=str(req.get('root') or DEFAULT_FLIGHT_ROOT or '').strip()
            out_text=str(req.get('out') or DEFAULT_OUTPUT_ROOT or '').strip()
            root=Path(root_text) if root_text else None
            fname=req.get('fname') or ''
            out=Path(out_text) if out_text else None
            if path=='/client_log':
                add_log(req.get('level','INFO'), req.get('message',''))
                return self.send_json({'ok':True})
            if path=='/pick_folder':
                target=req.get('target') or ''
                initial=req.get('initial') or (DEFAULT_FLIGHT_ROOT if target=='root' else DEFAULT_OUTPUT_ROOT) or ''
                title=req.get('title') or ('Select Flight root' if target=='root' else 'Select Output folder')
                selected=choose_windows_folder(title, initial)
                if selected:
                    add_log('INFO', f'Folder selected for {target}: {selected}')
                    return self.send_json({'ok':True,'path':selected})
                return self.send_json({'ok':True,'path':'','message':'Folder selection cancelled.'})
            if path in ('/detect','/load') and root is None:
                return self.send_json({'ok':False,'message':'Please select Flight root first using the Select Flight root button.'},400)
            if path in ('/load','/analyze','/export_data','/load_project','/exit') and out is None:
                return self.send_json({'ok':False,'message':'Please select Output folder first using the Select Output folder button.'},400)
            if path=='/detect':
                det=detect_files(root,fname); STATE.detected=det; STATE.flight_root=root; STATE.flight_name=fname or det.flight_name; STATE.output_root=out
                add_log('INFO', det.message)
                return self.send_json({'ok':True,'message':f'Detected {len(det.files)} CSV file(s): '+', '.join(p.name for p in det.files[:3])})
            if path=='/load':
                def job():
                    try:
                        det=STATE.detected or detect_files(root,fname); STATE.detected=det; STATE.flight_name=fname or det.flight_name; STATE.output_root=out
                        STATE.data=load_csv_files(det.files); STATE.export_data=make_export_source(STATE.data); STATE.payload_cache=None; set_status(100,f'Data loaded: {len(STATE.data):,} rows',False)
                    except Exception as exc: set_status(100,'Load failed: '+str(exc),False)
                threading.Thread(target=job,daemon=True).start(); return self.send_json({'ok':True,'message':'Loading started'})
            if path=='/load_project':
                def job():
                    try:
                        flight=req.get('fname') or STATE.flight_name or ''
                        project=find_project_file(out,flight)
                        set_status(20,'Loading Noseboom project')
                        loaded_logs=[]
                        STATE.d1,STATE.straight,STATE.freq,STATE.spectra,STATE.summary,STATE.export_data,loaded_logs=load_project_file(project)
                        STATE.project_path=project; STATE.output_root=project.parent; STATE.flight_name=flight or project.stem.replace('_noseboom_project','')
                        if loaded_logs:
                            STATE.logs=(loaded_logs+STATE.logs)[-1200:]
                        STATE.data=None
                        set_status(85,'Preparing browser visualizations')
                        STATE.payload_cache=None; STATE.payload_cache=api_payload()
                        set_status(100,f'Project loaded: {project.name}',False)
                    except Exception as exc: set_status(100,'Project load failed: '+str(exc),False)
                threading.Thread(target=job,daemon=True).start(); return self.send_json({'ok':True,'message':'Project loading started'})
            if path=='/recalculate_straight':
                if STATE.d1 is None: return self.send_json({'ok':False,'message':'Run Analyze first'},400)
                params=req.get('params') or {}
                STATE.straight=detect_straight(STATE.d1, params)
                straight_attrs=dict(STATE.straight.attrs)
                if 'terrain_m' in STATE.d1.columns and 'terrain_m' not in STATE.straight.columns:
                    STATE.straight=STATE.straight.join(STATE.d1[['terrain_m']], how='left'); STATE.straight.attrs.update(straight_attrs)
                metrics=STATE.straight.attrs.get('straight_metrics',[])
                cfg=STATE.straight.attrs.get('straight_params',STRAIGHT_DEFAULTS)
                STATE.summary.update({'straight_rows':int(STATE.straight['straight'].sum()),'straight_legs':int(STATE.straight['straight_leg_id'].max()),'straight_params':cfg,'straight_metrics':metrics})
                STATE.payload_cache=None
                try:
                    flight=req.get('fname') or STATE.flight_name or 'Flight'; out=(Path(req.get('out') or DEFAULT_OUTPUT_ROOT) if (req.get('out') or DEFAULT_OUTPUT_ROOT) else Path.cwd())/safe_name(flight)
                    project=STATE.project_path or project_file_path(out,safe_name(flight))
                    STATE.summary['project_file']=str(project)
                    save_project(project,STATE.d1,STATE.straight,STATE.freq if STATE.freq is not None else np.array([]),STATE.spectra,STATE.summary,STATE.export_data)
                    STATE.project_path=project
                except Exception:
                    pass
                STATE.payload_cache=api_payload()
                return self.send_json({'ok':True,'message':f'Straight-flight legs recalculated: {STATE.summary["straight_legs"]} leg(s), {STATE.summary["straight_rows"]} one-second samples.'})
            if path=='/export_data':
                def job():
                    try:
                        flight=req.get('fname') or STATE.flight_name or 'Flight'
                        fmt=req.get('format') or 'csv'
                        frequency=float(req.get('frequency') or 1)
                        STATE.last_export=None
                        set_status(10,'Preparing Noseboom export')
                        if STATE.detected is not None and getattr(STATE.detected,'files',None):
                            set_status(20,'Reading original detected Noseboom CSV files for export')
                            export_raw=load_csv_files(STATE.detected.files)
                            source=make_export_source(export_raw); source_label='fresh original detected Noseboom CSV data'
                            STATE.export_data=source
                        elif STATE.data is not None and len(STATE.data):
                            source=make_export_source(STATE.data); source_label='original loaded Noseboom CSV data'
                            STATE.export_data=source
                        elif STATE.export_data is not None and len(STATE.export_data):
                            source=make_export_source(STATE.export_data); source_label='stored original-resolution project export source upgraded to current export format'
                        else:
                            raise RuntimeError('No original-resolution export source is available. Use Detect data and Load data first, then export.')
                        set_status(40,f'Resampling original data at {frequency:g} Hz')
                        path_out,nrows=export_noseboom_data(out,flight,source,frequency,fmt)
                        STATE.last_export={'ok':True,'path':str(path_out),'file':path_out.name,'rows':int(nrows),'frequency_hz':float(frequency),'format':fmt,'source':source_label}
                        set_status(100,f'Export complete: {path_out.name} ({nrows:,} rows)',False)
                    except Exception as exc:
                        STATE.last_export={'ok':False,'message':str(exc)}
                        set_status(100,'Export failed: '+str(exc),False)
                threading.Thread(target=job,daemon=True).start(); return self.send_json({'ok':True,'message':'Export started'})
            if path=='/analyze':
                def job():
                    try:
                        if STATE.data is None: raise RuntimeError('Load data first')
                        flight=req.get('fname') or STATE.flight_name or 'Flight'; target=out/safe_name(flight)
                        STATE.d1,STATE.straight,STATE.freq,STATE.spectra,STATE.summary,STATE.export_data=analyze(STATE.data,target,flight,trim_mins=2.0)
                        STATE.output_root=out
                        STATE.project_path=Path(STATE.summary.get('project_file')) if STATE.summary.get('project_file') else None
                        set_status(96,'Preparing browser visualizations')
                        STATE.payload_cache=None
                        STATE.payload_cache=api_payload()
                        set_status(100,'Analysis complete',False)
                    except Exception as exc: set_status(100,'Analysis failed: '+str(exc),False)
                threading.Thread(target=job,daemon=True).start(); return self.send_json({'ok':True,'message':'Analysis started'})
            if path=='/exit':
                def job():
                    try:
                        add_log('INFO','Exit requested by user. Preparing safe project save.')
                        set_status(20,'Preparing for Exit: saving project file and session log')
                        project=save_current_project_snapshot(out, fname or STATE.flight_name or 'Flight')
                        if project:
                            add_log('INFO',f'Project and session log saved before exit: {project}')
                            flush_project_logs(project)
                            set_status(100,f'Exit preparation complete. Saved: {Path(project).name}',False)
                            flush_project_logs(project)
                        else:
                            add_log('INFO','Exit requested before an analyzed project existed; no HDF5 project file was available to update.')
                            set_status(100,'Exit preparation complete. No analyzed project file was available to save.',False)
                        time.sleep(0.8)
                        if STATE.server is not None:
                            STATE.server.shutdown()
                    except Exception as exc:
                        set_status(100,'Exit failed: '+str(exc),False)
                threading.Thread(target=job,daemon=True).start()
                return self.send_json({'ok':True,'message':'Preparing for Exit. Project and session log are being saved.'})
            self.send_response(404); self.end_headers()
        except Exception as exc:
            add_log('ERROR', str(exc))
            self.send_json({'ok':False,'message':str(exc)},500)

def find_free_port(host, start_port):
    for port in range(int(start_port), int(start_port)+20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            if sock.connect_ex((host, port)) != 0:
                return port
    raise RuntimeError(f'No free local port found from {start_port} to {start_port+19}')

def run():
    port=find_free_port(HOST,PORT)
    if port!=PORT:
        print(f'Port {PORT} is already in use, probably by an older Noseboom GUI. Using {port} for this handover copy.')
    server=ThreadingHTTPServer((HOST,port),Handler)
    STATE.server=server
    url=f'http://{HOST}:{port}/?handover_build=folder-button-export-v2'
    print('Zeppelin CCFLUX Campaign 2026')
    print('Open:',url)
    print('Press Ctrl+C in this terminal to stop the local server.')
    try:
        webbrowser.open(url)
    except Exception:
        pass
    server.serve_forever()

if __name__=='__main__':
    run()

















