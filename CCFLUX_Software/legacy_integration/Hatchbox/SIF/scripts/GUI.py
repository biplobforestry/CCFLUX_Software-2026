#!/usr/bin/env python
from __future__ import annotations
import argparse, importlib, importlib.util, json, math, struct, subprocess, sys, threading, time, traceback, webbrowser
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

REQUIRED=(('numpy','numpy'),('pandas','pandas'),('matplotlib','matplotlib'))
FULL_VARIABLES=['temp1 [C]','h1 [%]','Incoming at 750nm Full [W m-2nm-1sr-1]','Reflected 750nm full [W m-2nm-1sr-1]','Incoming at 750nm FLUO [W m-2nm-1sr-1]','Reflected 750nm FLUO [W m-2nm-1sr-1]','SIF_A_ifld [mW m-2nm-1sr-1]','SIF_B_ifld [mW m-2nm-1sr-1]','PAR inc [W m-2]','PAR ref [W m-2]','APAR [umol m-2 s-1]','E_stability full [%]','E_stability FLUO [%]','sat value L full','sat value E full','sat value E2 full','sat value L FLUO','sat value E FLUO','sat value E2 FLUO','Dynamic range E full [%]','Dynamic range L full [%]','Dynamic range E FLUO [%]','Dynamic range L FLUO [%]','NDVI','PRI','MTCI','SR','EVI','REP','TCARI','REDCl','MCRI']
MAP_VARIABLES=['NDVI','PRI','EVI']

BG='#edf4f3'
PANEL='#f8fbfb'
HEADER='#12343b'
HEADER_ACCENT='#2aa198'
TEXT='#17313a'
MUTED='#5d747b'
SELECTED='#0f766e'
SELECTED_ACTIVE='#0b5f59'
UNSELECTED='#e7eef0'
UNSELECTED_ACTIVE='#d6e4e6'
BORDER='#b8c9cc'
FOOTER='#050505'
APP_TITLE='SIF Automation Zeppelin Campaign 2026'
APP_TOPIC='Solar-induced fluorescence'
FONT_BASE=('Arial',10)
FONT_LABEL=('Arial',10)
FONT_HEADER=('Arial',20,'bold')
FONT_TOPIC=('Arial',12,'bold')
FONT_SUBTITLE=('Arial',10)
FONT_MONO=('Consolas',10)

def ensure_libraries(log):
    for import_name,pip_name in REQUIRED:
        if importlib.util.find_spec(import_name) is None:
            log(f'Installing missing library: {pip_name}')
            subprocess.check_call([sys.executable,'-m','pip','install',pip_name])

def read_result_csv(path: Path):
    import pandas as pd
    df=pd.read_csv(path,sep=';',engine='python',na_values=['#N/D'],keep_default_na=True)
    if 'datetime [UTC]' in df.columns: df['datetime [UTC]']=pd.to_datetime(df['datetime [UTC]'],errors='coerce')
    return df

def numeric_series(df,column):
    import pandas as pd
    return pd.to_numeric(df[column],errors='coerce')

def color_for(value,vmin,vmax):
    if value is None or not math.isfinite(value) or vmax<=vmin: return '#888888'
    r=max(0,min(1,(value-vmin)/(vmax-vmin)))
    if r<.5:
        q=r*2; red=int(44+(49-44)*q); green=int(123+(163-123)*q); blue=int(182+(84-182)*q)
    else:
        q=(r-.5)*2; red=int(49+(215-49)*q); green=int(163+(25-163)*q); blue=int(84+(28-84)*q)
    return f'#{red:02x}{green:02x}{blue:02x}'

def valid_map_variables(df, candidates=None):
    import pandas as pd
    candidates=candidates or MAP_VARIABLES
    valid=[]; invalid=[]
    for name in candidates:
        if name not in df.columns:
            invalid.append((name,'missing'))
            continue
        s=pd.to_numeric(df[name],errors='coerce')
        if s.notna().any(): valid.append(name)
        else: invalid.append((name,'no valid numeric values'))
    return valid,invalid

def make_variable_map(df,csv_path:Path,variable:str=None,variables=None,palette='RdYlGn',buffer_m=500):
    import html as html_lib
    import pandas as pd
    variables=variables or valid_map_variables(df)[0]
    if not variables: raise ValueError('No map variables available')
    missing=[c for c in ('Lat','Lon') if c not in df.columns]
    if missing: raise ValueError(f'Missing map column(s): {missing}')
    work=df[['Lat','Lon']+variables].copy()
    work['Lat']=pd.to_numeric(work['Lat'],errors='coerce'); work['Lon']=pd.to_numeric(work['Lon'],errors='coerce')
    for v in variables: work[v]=pd.to_numeric(work[v],errors='coerce')
    work=work.dropna(subset=['Lat','Lon'])
    if work.empty: raise ValueError('No valid Lat/Lon rows for map')
    records=[]; ranges={}
    for v in variables:
        vals=work[v].dropna(); ranges[v]=[float(vals.min()) if not vals.empty else 0.0,float(vals.max()) if not vals.empty else 1.0]
    for _,row in work.iterrows():
        rec={'lat':float(row['Lat']),'lon':float(row['Lon']),'values':{}}
        for v in variables:
            val=row[v]; rec['values'][v]=None if pd.isna(val) else float(val)
        records.append(rec)
    lat=sum(p['lat'] for p in records)/len(records); lon=sum(p['lon'] for p in records)/len(records)
    html=csv_path.with_name(csv_path.stem+'_interactive_map.html')
    data=json.dumps(records); ranges_json=json.dumps(ranges); variables_json=json.dumps(variables)
    initial=variable if variable in variables else variables[0]
    path_text=str(csv_path).upper()
    product='FLUO' if 'FLUO' in path_text else ('FULL' if 'FULL' in path_text or 'FLOX' in path_text else 'SIF')
    map_title=f'AirFloX MAP({product})'
    option_html=''.join(f"<option value='{html_lib.escape(v, quote=True)}'{' selected' if v==initial else ''}>{html_lib.escape(v)}</option>" for v in variables)
    try: buffer_m=float(buffer_m)
    except Exception: buffer_m=500.0
    buffer_m=max(0.0,buffer_m)
    html.write_text(f"""<!doctype html><html><head><meta charset='utf-8'><title>{map_title}</title><link rel='stylesheet' href='https://unpkg.com/leaflet@1.9.4/dist/leaflet.css'><style>html,body,#map{{height:100%;margin:0}}.panel{{position:absolute;z-index:1000;background:white;padding:8px 10px;border-radius:4px;font:13px Arial;box-shadow:0 1px 5px #777}}.controls{{top:10px;left:50px}}.legend{{bottom:24px;right:20px;width:210px}}.bar{{height:12px;margin:6px 0}} select{{margin-right:8px}}</style></head><body><div id='map'></div><div class='panel controls'><b>{map_title}</b><br>Variable <select id='varSel'>{option_html}</select> Color bar <select id='palSel'><option value='PiYG'>PiYG</option><option value='PRGn'>PRGn</option><option value='RdYlGn'>RdYlGn</option></select><label><input id='krigSel' type='checkbox'> Kriging</label> Buffer <input id='bufSel' type='number' min='0' step='10' value='{buffer_m:g}' list='bufChoices' style='width:76px'> m<datalist id='bufChoices'><option value='50'><option value='500'><option value='1300'></datalist></div><div class='panel legend'><b id='legtitle'></b><div id='bar' class='bar'></div><span id='vmin'></span><span id='vmax' style='float:right'></span></div><script src='https://unpkg.com/leaflet@1.9.4/dist/leaflet.js'></script><script>
const pts={data}; const ranges={ranges_json}; const variables={variables_json};
const varSel=document.getElementById('varSel'), palSel=document.getElementById('palSel'), krigSel=document.getElementById('krigSel'), bufSel=document.getElementById('bufSel');
if (!varSel.options.length) variables.forEach(v=>{{const o=document.createElement('option'); o.value=v; o.textContent=v; varSel.appendChild(o);}}); varSel.value='{initial}'; palSel.value='{palette if palette in ['PiYG','PRGn','RdYlGn'] else 'RdYlGn'}';
if (typeof L === 'undefined') {{ document.getElementById('map').innerHTML = "<div style='padding:180px 42px;font:18px Arial;color:#333'>OpenStreetMap library could not load. Check internet connection and refresh this page. Variable selection is still available above.</div>"; document.getElementById('legtitle').textContent = varSel.value || 'No variable'; }} else {{
const map=L.map('map').setView([{lat},{lon}],14); L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{maxZoom:19,attribution:'&copy; OpenStreetMap'}}).addTo(map); L.control.scale({{imperial:false}}).addTo(map);
let pointLayer=L.layerGroup().addTo(map), gridLayer=L.layerGroup().addTo(map), lineLayer=L.layerGroup().addTo(map); const line=pts.map(p=>[p.lat,p.lon]); if(line.length>1){{L.polyline(line,{{color:'#333',weight:2,opacity:.35}}).addTo(lineLayer); map.fitBounds(line);}}
function interp(a,b,t){{return Math.round(a+(b-a)*t)}} function hex(r,g,b){{return '#'+[r,g,b].map(x=>x.toString(16).padStart(2,'0')).join('')}}
function stops(pal){{if(pal==='PiYG')return [[197,27,125],[247,247,247],[77,146,33]]; if(pal==='PRGn')return [[118,42,131],[247,247,247],[27,120,55]]; return [[215,25,28],[255,255,191],[26,150,65]];}}
function color(v,vmin,vmax,pal){{if(v===null||isNaN(v)||vmax<=vmin)return '#888'; let r=Math.max(0,Math.min(1,(v-vmin)/(vmax-vmin))); const s=stops(pal); if(r<.5){{let q=r*2; return hex(interp(s[0][0],s[1][0],q),interp(s[0][1],s[1][1],q),interp(s[0][2],s[1][2],q));}} let q=(r-.5)*2; return hex(interp(s[1][0],s[2][0],q),interp(s[1][1],s[2][1],q),interp(s[1][2],s[2][2],q));}}
function idw(lat,lon,v){{let num=0,den=0; for(const p of pts){{const val=p.values[v]; if(val===null)continue; const d=Math.hypot((p.lat-lat)*111000,(p.lon-lon)*71000); const w=1/Math.pow(Math.max(d,1),2); num+=w*val; den+=w;}} return den?num/den:null;}}
function distPointSegmentM(lat,lon,a,b){{const x=(lon-a.lon)*71000, y=(lat-a.lat)*111000, x2=(b.lon-a.lon)*71000, y2=(b.lat-a.lat)*111000; const len2=x2*x2+y2*y2; if(len2<=0)return Math.hypot(x,y); const t=Math.max(0,Math.min(1,(x*x2+y*y2)/len2)); return Math.hypot(x-t*x2,y-t*y2);}}
function distToTrackM(lat,lon){{if(pts.length===1)return Math.hypot((lon-pts[0].lon)*71000,(lat-pts[0].lat)*111000); let best=Infinity; for(let i=1;i<pts.length;i++)best=Math.min(best,distPointSegmentM(lat,lon,pts[i-1],pts[i])); return best;}}
function bufferedBounds(bufferM){{let latMin=Math.min(...pts.map(p=>p.lat)), latMax=Math.max(...pts.map(p=>p.lat)), lonMin=Math.min(...pts.map(p=>p.lon)), lonMax=Math.max(...pts.map(p=>p.lon)); const latPad=bufferM/111000, lonPad=bufferM/71000; return {{lat0:latMin-latPad,lat1:latMax+latPad,lon0:lonMin-lonPad,lon1:lonMax+lonPad}};}}
function drawGrid(v,pal,vmin,vmax,bufferM){{gridLayer.clearLayers(); const b=bufferedBounds(bufferM); const rows=64, cols=64; for(let i=0;i<rows;i++){{for(let j=0;j<cols;j++){{const laA=b.lat0+(b.lat1-b.lat0)*i/rows, laB=b.lat0+(b.lat1-b.lat0)*(i+1)/rows, loA=b.lon0+(b.lon1-b.lon0)*j/cols, loB=b.lon0+(b.lon1-b.lon0)*(j+1)/cols; const la=(laA+laB)/2, lo=(loA+loB)/2; if(distToTrackM(la,lo)>bufferM)continue; const val=idw(la,lo,v); L.rectangle([[laA,loA],[laB,loB]],{{stroke:false,fillColor:color(val,vmin,vmax,pal),fillOpacity:.42}}).addTo(gridLayer);}}}}}}
function redraw(){{const v=varSel.value, pal=palSel.value, krig=krigSel.checked, bufferM=Math.max(0,Number(bufSel.value)||0); const rr=ranges[v]||[0,1]; const vmin=rr[0], vmax=rr[1]; pointLayer.clearLayers(); gridLayer.clearLayers(); document.getElementById('legtitle').textContent=v+(krig?' | Kriging '+bufferM+' m':''); document.getElementById('vmin').textContent=Number(vmin).toPrecision(4); document.getElementById('vmax').textContent=Number(vmax).toPrecision(4); const gradients={{PiYG:'linear-gradient(90deg,#c51b7d,#f7f7f7,#4d9221)',PRGn:'linear-gradient(90deg,#762a83,#f7f7f7,#1b7837)',RdYlGn:'linear-gradient(90deg,#d7191c,#ffffbf,#1a9641)'}}; document.getElementById('bar').style.background=gradients[pal]; if(krig)drawGrid(v,pal,vmin,vmax,bufferM); const step=Math.max(1,Math.floor(pts.length/1400)); for(let i=0;i<pts.length;i+=step){{const p=pts[i], val=p.values[v]; L.circleMarker([p.lat,p.lon],{{radius:4,color:color(val,vmin,vmax,pal),fillColor:color(val,vmin,vmax,pal),fill:true,fillOpacity:.95,weight:1}}).addTo(pointLayer).bindPopup(v+': '+(val===null?'NA':Number(val).toFixed(5)));}}}}
varSel.addEventListener('change',redraw); palSel.addEventListener('change',redraw); krigSel.addEventListener('change',redraw); bufSel.addEventListener('input',redraw); map.on('moveend',()=>{{if(krigSel.checked)redraw();}}); redraw();
}}
</script></body></html>""",encoding='utf-8')
    return html
class App(tk.Tk):
    def __init__(self):
        super().__init__(); self.title(APP_TITLE); self.geometry('1120x820'); self.minsize(980,720); self.configure(bg=BG)
        self.vars={'directory':tk.StringVar(value='D:\\'),'flight':tk.StringVar(value='Flight_2124'),'output':tk.StringVar(value=r'C:\My_PC\Zeppelin\3_Quick_look\SIF'),'essentials':tk.StringVar(value=r'C:\My_PC\Zeppelin\3_Quick_look\SIF\SIF_Essentials'),'log':tk.StringVar(value=''),'platform':tk.StringVar(value='uav_airship'),'static_lat':tk.StringVar(value=''),'static_lon':tk.StringVar(value=''),'static_alt':tk.StringVar(value=''),'altitude':tk.BooleanVar(value=False),'nonlinear':tk.BooleanVar(value=False),'spectral_shift':tk.StringVar(value='no'),'raw_min_kb':tk.StringVar(value='100'),'time_filter':tk.StringVar(value='default'),'time_start_utc':tk.StringVar(value=''),'time_end_utc':tk.StringVar(value='')}
        self.start_time=None; self.running=False; self.progress_value=0.0; self.last_made=[]; self.result_files={}; self.checking_popup=None; self.step_window=None; self.step_rows={}; self.step_start_times={}
        self.report_callback_exception=self.handle_tk_exception
        self.build(); self.after(300,self.enable_existing_results)
    def handle_tk_exception(self,exc,value,tb):
        message=''.join(traceback.format_exception(exc,value,tb))
        self._log_apply(message)
        try: messagebox.showerror('GUI error','A GUI error occurred. See log window for details.')
        except Exception: pass
    def build(self):
        shell=tk.Frame(self,bg=BG)
        shell.pack(fill='both',expand=True)
        header=tk.Frame(shell,bg=HEADER,padx=18,pady=12)
        header.pack(fill='x')
        title_block=tk.Frame(header,bg=HEADER)
        title_block.pack(side='left',fill='x',expand=True)
        tk.Label(title_block,text=APP_TOPIC,bg=HEADER,fg='#c8e6e2',font=FONT_TOPIC).pack(anchor='w')
        tk.Label(title_block,text=APP_TITLE,bg=HEADER,fg='white',font=FONT_HEADER).pack(anchor='w')
        action_panel=tk.Frame(header,bg=HEADER)
        action_panel.pack(side='right',padx=(12,0),pady=0)
        top_actions=tk.Frame(action_panel,bg=HEADER)
        top_actions.pack(anchor='e')
        self.variables_button=tk.Button(top_actions,text='Variables',command=self.show_variables,width=12,bg='#d9f4ef',fg=HEADER,activebackground='white',activeforeground=HEADER)
        self.variables_button.pack(side='left',padx=(0,12),pady=(0,5))
        self.vi_button=tk.Button(top_actions,text='Vegetation Index',command=self.show_vegetation_index,width=18,bg='#d9f4ef',fg=HEADER,activebackground='white',activeforeground=HEADER)
        self.vi_button.pack(side='left',padx=(0,12),pady=(0,5))
        self.about_button=tk.Button(top_actions,text='About',command=self.show_about,width=10,bg='#d9f4ef',fg=HEADER,activebackground='white',activeforeground=HEADER)
        self.about_button.pack(side='left',pady=(0,5))
        self.manual_button=tk.Button(action_panel,text='User Manual',command=self.show_user_manual,width=10,bg='#d9f4ef',fg=HEADER,activebackground='white',activeforeground=HEADER)
        self.manual_button.pack(anchor='e',pady=(4,0))
        footer=tk.Frame(shell,bg=FOOTER,height=30)
        footer.pack(fill='x',side='bottom')
        footer.pack_propagate(False)
        tk.Label(footer,text='\u00a9 2026 Biplob Dey - Python GUI adaptation and automation logic. GNU GPL v3.0.',bg=FOOTER,fg='white',font=('Arial',10,'bold')).pack(expand=True)
        frm=tk.Frame(shell,padx=16,pady=14,bg=PANEL,highlightbackground=BORDER,highlightthickness=1)
        frm.pack(fill='both',expand=True,padx=14,pady=14)
        rows=[('Input directory','directory',True),('Flight name','flight',False),('Output directory','output',True),('Essentials folder','essentials',True),('Custom SIF log (optional)','log',True)]
        for r,(label,key,browse) in enumerate(rows):
            tk.Label(frm,text=label,anchor='w').grid(row=r,column=0,sticky='w',pady=3)
            tk.Entry(frm,textvariable=self.vars[key],width=90).grid(row=r,column=1,sticky='ew',pady=3)
            if browse: tk.Button(frm,text='Browse',command=lambda k=key:self.pick(k)).grid(row=r,column=2,padx=4)
        tk.Label(frm,text='Platform / position mode',anchor='w').grid(row=5,column=0,sticky='w',pady=3)
        mode_frame=tk.Frame(frm); mode_frame.grid(row=5,column=1,sticky='w',pady=3)
        self.airship_button=tk.Button(mode_frame,text='UAV/Airship',width=16,command=lambda:self.set_platform_mode('uav_airship'))
        self.airship_button.pack(side='left')
        self.tower_button=tk.Button(mode_frame,text='Tower',width=16,command=lambda:self.set_platform_mode('tower'))
        self.tower_button.pack(side='left',padx=8)
        static_frame=tk.Frame(frm); static_frame.grid(row=6,column=1,sticky='w',pady=3)
        self.static_labels=[]; self.static_entries=[]
        lbl=tk.Label(static_frame,text='Tower static Lat'); lbl.pack(side='left'); self.static_labels.append(lbl)
        ent=tk.Entry(static_frame,textvariable=self.vars['static_lat'],width=12); ent.pack(side='left',padx=(4,10)); self.static_entries.append(ent)
        lbl=tk.Label(static_frame,text='Lon'); lbl.pack(side='left'); self.static_labels.append(lbl)
        ent=tk.Entry(static_frame,textvariable=self.vars['static_lon'],width=12); ent.pack(side='left',padx=(4,10)); self.static_entries.append(ent)
        lbl=tk.Label(static_frame,text='Alt m'); lbl.pack(side='left'); self.static_labels.append(lbl)
        ent=tk.Entry(static_frame,textvariable=self.vars['static_alt'],width=10); ent.pack(side='left',padx=(4,10)); self.static_entries.append(ent)
        self.coord_info_button=tk.Button(static_frame,text='i',width=2,command=self.show_coordinate_help,bg='#d9f4ef',fg=HEADER,activebackground='white',activeforeground=HEADER)
        self.coord_info_button.pack(side='left')
        self.refresh_platform_controls()
        tk.Checkbutton(frm,text='Altitude_filter',variable=self.vars['altitude']).grid(row=7,column=1,sticky='w')
        tk.Checkbutton(frm,text='apply_nonlinearity_correction',variable=self.vars['nonlinear']).grid(row=8,column=1,sticky='w')
        tk.Label(frm,text='Spectral shift correction',anchor='w').grid(row=9,column=0,sticky='w',pady=3)
        shift_frame=tk.Frame(frm); shift_frame.grid(row=9,column=1,sticky='w',pady=3)
        self.shift_yes_button=tk.Button(shift_frame,text='Yes',width=10,command=lambda:self.set_spectral_shift('yes'))
        self.shift_yes_button.pack(side='left')
        self.shift_no_button=tk.Button(shift_frame,text='No',width=10,command=lambda:self.set_spectral_shift('no'))
        self.shift_no_button.pack(side='left',padx=8)
        tk.Label(frm,text='Drop raw files smaller than KB',anchor='w').grid(row=10,column=0,sticky='w',pady=3)
        raw_frame=tk.Frame(frm); raw_frame.grid(row=10,column=1,sticky='w',pady=3)
        tk.Entry(raw_frame,textvariable=self.vars['raw_min_kb'],width=10).pack(side='left')
        tk.Label(raw_frame,text='Default 100 KB').pack(side='left',padx=8)
        tk.Label(frm,text='Time filter (UTC)',anchor='w').grid(row=11,column=0,sticky='w',pady=3)
        time_frame=tk.Frame(frm); time_frame.grid(row=11,column=1,sticky='w',pady=3)
        self.time_default_button=tk.Button(time_frame,text='Default',width=12,command=lambda:self.set_time_filter('default'))
        self.time_default_button.pack(side='left')
        self.time_custom_button=tk.Button(time_frame,text='Custom',width=12,command=lambda:self.set_time_filter('custom'))
        self.time_custom_button.pack(side='left',padx=8)
        self.time_entries=[]
        tk.Label(time_frame,text='Start').pack(side='left',padx=(12,2))
        ent=tk.Entry(time_frame,textvariable=self.vars['time_start_utc'],width=21); ent.pack(side='left'); self.time_entries.append(ent)
        tk.Label(time_frame,text='End').pack(side='left',padx=(8,2))
        ent=tk.Entry(time_frame,textvariable=self.vars['time_end_utc'],width=21); ent.pack(side='left'); self.time_entries.append(ent)
        tk.Label(time_frame,text='YYYY-MM-DD HH:MM:SS').pack(side='left',padx=8)
        self.refresh_option_controls()
        buttons=tk.Frame(frm); buttons.grid(row=12,column=1,sticky='w',pady=8)
        self.process_button=tk.Button(buttons,text='Process',command=self.process,width=18,bg=HEADER_ACCENT,fg='white',activebackground=SELECTED,activeforeground='white'); self.process_button.pack(side='left')
        self.results_button=tk.Button(buttons,text='Results',command=self.open_results,width=18,state='disabled',bg=UNSELECTED,fg=TEXT,activebackground=UNSELECTED_ACTIVE); self.results_button.pack(side='left',padx=8)
        self.progress=ttk.Progressbar(frm,orient='horizontal',mode='determinate',maximum=100); self.progress.grid(row=13,column=0,columnspan=3,sticky='ew',pady=(4,2))
        self.status=tk.StringVar(value='Ready'); tk.Label(frm,textvariable=self.status,anchor='w').grid(row=14,column=0,columnspan=3,sticky='ew',pady=(0,6))
        self.text=scrolledtext.ScrolledText(frm,height=24); self.text.grid(row=15,column=0,columnspan=3,sticky='nsew')
        frm.columnconfigure(1,weight=1); frm.rowconfigure(15,weight=1)
        self.apply_scientific_style(self)
        self.refresh_platform_controls()
        self.refresh_option_controls()
    def apply_scientific_style(self,widget):
        try:
            style=ttk.Style(self)
            style.configure('TProgressbar',troughcolor='#d8e6e7',background=HEADER_ACCENT,bordercolor=BORDER,lightcolor=HEADER_ACCENT,darkcolor=HEADER_ACCENT)
        except Exception:
            pass
        for child in widget.winfo_children():
            try:
                parent_bg=child.master.cget('bg') if hasattr(child.master,'cget') else PANEL
                current_bg=child.cget('bg') if hasattr(child,'cget') else parent_bg
                if isinstance(child,tk.Frame):
                    if current_bg in (HEADER,FOOTER):
                        pass
                    else:
                        child.configure(bg=PANEL if child.master is not self else BG)
                elif isinstance(child,tk.Label):
                    if parent_bg in (HEADER,FOOTER):
                        pass
                    else:
                        child.configure(bg=parent_bg,fg=TEXT,font=FONT_LABEL)
                elif isinstance(child,tk.Entry):
                    child.configure(bg='white',fg=TEXT,insertbackground=TEXT,relief='solid',bd=1,highlightthickness=1,highlightbackground=BORDER,highlightcolor=HEADER_ACCENT,font=FONT_BASE)
                elif isinstance(child,tk.Button):
                    child.configure(font=FONT_BASE,bd=1,relief='raised',cursor='hand2')
                elif isinstance(child,tk.Checkbutton):
                    child.configure(bg=PANEL,fg=TEXT,activebackground=PANEL,activeforeground=TEXT,selectcolor='white',font=FONT_BASE)
                elif isinstance(child,scrolledtext.ScrolledText):
                    child.configure(bg='#0b1f24',fg='#d6f3ed',insertbackground='white',font=FONT_MONO,relief='solid',bd=1)
            except Exception:
                pass
            self.apply_scientific_style(child)

    def show_coordinate_help(self):
        message=(
            'Tower coordinate format\n\n'
            'Latitude: decimal degrees in WGS 84, north positive.\n'
            'Example: 50.9134\n\n'
            'Longitude: decimal degrees in WGS 84, east positive.\n'
            'Example: 6.9821\n\n'
            'Altitude: meters (m), numeric value only.\n'
            'Example: 320.5\n\n'
            'Do not enter kilometers. If altitude is 0.32 km, enter 320.\n'
            'In Tower mode, these static coordinates are used for the GIS output.'
        )
        messagebox.showinfo('Tower Coordinate Format',message)

    def show_user_manual(self):
        manual_text = """
SIF AUTOMATION ZEPPELIN CAMPAIGN 2026 - USER MANUAL

Purpose
=======
This GUI automates AirFloX SIF processing for FULL and FLUO products, synchronizes
position/orientation information from airship or tower workflows, and exports CSV,
spectral tables, GIS shapefiles, quick-look time series, and maps.

Recommended Folder Structure
============================
Input directory and Flight name should point to this type of campaign structure:

D:\\n  Flight_2124\\
    20260710_153723\\
      influxdb\\
        HATCH-BOX\\
          Gremsy_T3V3_Gimbal.csv
          noseboom_ins_100hz.csv
          log_..._conv_ang.csv              optional, used directly if present
    FLOXINSIDE_260710\\                    name can vary, but contains AirFloX data
      Full\\
        F*.CSV                            FULL raw files, usually starting with F
      Flox\\ or FLUO\\
        *.CSV                             FLUO raw files, usually numeric names

Essentials folder, normally:
C:\\My_PC\\Zeppelin\\3_Quick_look\\SIF\\SIF_Essentials\\
  CAL_*FULL*.csv                          FULL calibration file
  CAL_*FLUO*.csv                          FLUO calibration file
  Indices_ICOS.txt                        vegetation index definition file

Output folder, normally:
C:\\My_PC\\Zeppelin\\3_Quick_look\\SIF\\
  Flight_2124\\
    _combined\\                            concatenated raw and generated log files
    FLOX\\                                 FULL/FLOX results and GIS exports
    FLUO\\                                 FLUO results and GIS exports

Workflow Diagram
================

  User inputs
      |
      v
  Locate flight folder
      |
      +--> Find AirFloX FULL raw files
      +--> Find AirFloX FLUO raw files
      +--> Find calibration and indices files
      |
      v
  Position mode
      |
      +--> UAV/Airship: use HATCH-BOX log, or build it from gimbal + noseboom
      |        Required final log variables:
      |        lat, lon, alt_above_ground_m, date_time_utc, pitch, roll, yaw
      |
      +--> Tower: use static Lat/Lon/Alt, or raw AirFloX GPS if static is empty
      |
      v
  Process FULL and FLUO scientifically
      |
      +--> Radiance calibration
      +--> Optional nonlinearity correction
      +--> Optional spectral shift correction for FULL
      +--> Vegetation indices
      +--> SIF iFLD for FLUO
      +--> Time matching or static position assignment
      |
      v
  Export CSV, spectra, GIS shapefile, map preview, interactive map

Main Inputs
===========
Input directory
  Usually the drive or parent folder containing the flight folder, for example D:\\.
  If you browse directly to D:\\Flight_2124, the code also handles that.

Flight name
  Folder name of the flight, for example Flight_2124.

Output directory
  Parent folder where results will be written. The software creates a subfolder
  with the flight name.

Essentials folder
  Folder containing calibration files and Indices_ICOS.txt. The software detects
  FULL and FLUO calibration automatically from file names containing FULL or FLUO.

Custom SIF log
  Optional. If selected, the software uses this log and skips automatic generation
  from gimbal and noseboom. Required columns are:
  lat, lon, alt_above_ground_m, date_time_utc, pitch, roll, yaw.

Platform / Position Mode
========================
UAV/Airship
  Use this for moving measurements. The software searches HATCH-BOX for existing
  log files such as *log* or *conv_ang*. Otherwise gimbal and noseboom files are
  matched by nearest time.

Tower
  Use this for static measurements. Tower coordinate fields become editable.
  Coordinate format:
  - Latitude: WGS84 decimal degrees, north positive, e.g. 50.9134
  - Longitude: WGS84 decimal degrees, east positive, e.g. 6.9821
  - Altitude: meters, numeric only, e.g. 320.5
  If Lat/Lon are empty, AirFloX raw GPS is used where available.

Processing Options
==================
Altitude_filter
  For UAV/Airship, attempts to keep only flight periods based on altitude behavior.

apply_nonlinearity_correction
  Applies nonlinearity correction only when calibration contains required coefficients.
  Default is off.

Spectral shift correction
  Optional FULL spectrometer shift correction. Default is No for reproducibility.

Drop raw files smaller than KB
  Default is 100 KB. Files below this size are skipped because they are commonly
  incomplete or corrupted. If you enter more than 150 KB, the GUI asks for confirmation.

Time filter (UTC)
  Default: process all valid times.
  Custom: scans FULL and FLUO raw timestamps, finds common overlap, and fills Start/End.
  Format: YYYY-MM-DD HH:MM:SS, UTC.

Buttons
=======
Process
  Starts automated processing and writes outputs.

Results
  Opens result tools after processing. Timeseries shows selected variables, histogram,
  and statistics. Map shows NDVI, PRI, or EVI with color bar and optional interpolation.

Variables
  Opens a copyable reference list of output variables and descriptions.

Vegetation Index
  Opens a copyable reference table for vegetation index wavelength definitions.

User Manual
  Opens this manual.

About
  Shows author, institution, license, and source package attribution.

Outputs
=======
For FULL/FLOX:
  Incoming_radiance_FULL_*.csv
  Reflected_radiance_FULL_*.csv
  Reflectance_FULL_*.csv
  ALL_INDEX_AIRFLOX_FULL_*.csv
  GIS\\AIRFLOX_*.shp, .shx, .dbf, .prj

For FLUO:
  Incoming_radiance_FLUO_*.csv
  Reflected_radiance_FLUO_*.csv
  Reflectance_FLUO_*.csv
  ALL_INDEX_AIRFLOX_FLUO_*.csv
  GIS\\AIRFLOX_*.shp, .shx, .dbf, .prj

Scientific Notes
================
- More telemetry rows than AirFloX rows is normal. The code takes telemetry values
  according to AirFloX time matching.
- If a FLUO spectrum has no valid radiance in an absorption band, SIF for that row
  is written as #N/D instead of inventing a value.
- If FULL and FLUO have no common time range, custom time auto-fill warns the user.
- Locked CSV or GIS files may cause alternate timestamped output names to be used.

Troubleshooting
===============
No FLOXINSIDE folder found
  The software also accepts a direct folder containing FULL and FLOX/FLUO. Check
  that your flight folder is correct and that raw subfolders exist.

No FULL or FLUO raw files found
  Check raw file names and size filter. FULL files usually start with F. FLUO files
  are often numeric. Lower the size filter if needed.

No valid Lat/Lon for GIS export
  In UAV/Airship mode, check HATCH-BOX log/gimbal/noseboom time coverage. In Tower
  mode, enter static WGS84 Lat/Lon.

Map empty in browser
  Check that result CSV contains valid Lat/Lon and selected variable has numeric values.
  Internet access may be needed for OpenStreetMap tiles.
"""
        win=tk.Toplevel(self)
        win.title('User Manual')
        win.configure(bg='#eef7f0')
        win.geometry('980x720')
        win.minsize(760,520)
        body=tk.Frame(win,bg='#eef7f0',padx=14,pady=14)
        body.pack(fill='both',expand=True)
        tk.Label(body,text='User Manual',bg='#eef7f0',fg='#153f2a',font=('Arial',16,'bold'),anchor='w').pack(fill='x',pady=(0,8))
        text=scrolledtext.ScrolledText(body,wrap='word',font=('Consolas',10),relief='solid',bd=1)
        text.pack(fill='both',expand=True)
        text.insert('1.0',manual_text.strip())
        text.tag_configure('heading',foreground='#153f2a',font=('Consolas',11,'bold'))
        for heading in ['Purpose','Recommended Folder Structure','Workflow Diagram','Main Inputs','Platform / Position Mode','Processing Options','Buttons','Outputs','Scientific Notes','Troubleshooting']:
            start='1.0'
            while True:
                pos=text.search(heading,start,stopindex='end')
                if not pos:
                    break
                text.tag_add('heading',pos,f'{pos}+{len(heading)}c')
                start=f'{pos}+{len(heading)}c'
        btns=tk.Frame(body,bg='#eef7f0')
        btns.pack(fill='x',pady=(10,0))
        def copy_all():
            win.clipboard_clear()
            win.clipboard_append(text.get('1.0','end-1c'))
        tk.Button(btns,text='Copy All',command=copy_all,width=14,bg='#d9efe0',fg='#153f2a',activebackground='white',activeforeground='#153f2a',font=FONT_BASE).pack(side='left')
        tk.Button(btns,text='Close',command=win.destroy,width=14,bg=HEADER_ACCENT,fg='white',activebackground=SELECTED,activeforeground='white',font=FONT_BASE).pack(side='right')
        win.transient(self)

    def show_variables(self):
        rows=[
            ('datetime [UTC]','Date time, format UTC'),
            ('SZA','Solar Zenith Angle'),
            ('Lat','Latitude'),
            ('Lon','Longitude'),
            ('temp1 [C]','Temperature of QEPro CCD'),
            ('temp2 [C]','Temperature of QEPro housing'),
            ('temp3 [C]','Temperature of FloX mainboard'),
            ('temp4 [C]','Temperature of FloX spectrometer compartment'),
            ('h1 [%]','Humidity at the main controller'),
            ('h2 [%]','Humidity inside main spectrometer compartment'),
            ('Incoming at 750n [W m-2nm-1sr-1]','Incoming radiance at wavelength 750 nm'),
            ('Reflected 750 [W m-2nm-1sr-1]','Reflected radiance at wavelength 750 nm'),
            ('Reflected 760 [W m-2nm-1sr-1]','Reflected radiance at wavelength 760 nm'),
            ('Reflected 687 [W m-2nm-1sr-1]','Reflected radiance at wavelength 687 nm'),
            ('Reflectance 750 [-]','Reflectance at wavelength 750 nm'),
            ('Reflectance 760 [-]','Reflectance at wavelength 760 nm'),
            ('E_stability [%]','Percentage difference between WR1 and WR2. Fluo range'),
            ('sat value L [boolean]','Saturation value of downward channel. Fluo range'),
            ('sat value E [boolean]','Saturation value of upward channel 1. Fluo range'),
            ('sat value E2 [boolean]','Saturation value of upward channel2. Fluo range'),
            ('Dynamic range E [%]','Dynamic range cover of upward channel. Fluo range'),
            ('Dynamic range L [%]','Dynamic range cover of downward channel. Fluo range'),
            ('SIF_A_ifld [mW m-2nm-1sr-1]','SIF at O2A band (760 nm). iFLD method'),
            ('SIF_B_ifld [mW m-2nm-1sr-1]','SIF at O2B band (687 nm). iFLD method'),
            ('Incoming at 750nm Full [W m-2nm-1sr-1]','Incoming radiance at wavelength 750 nm. Full range'),
            ('Reflected 750nm full [W m-2nm-1sr-1]','Reflected radiance at wavelength 750 nm. Full range'),
            ('PAR tot [W m-2]','Photosynthetically Active Radiation'),
            ('PAR [umol m-2 s-1]','Reflected Photosynthetically Active Radiation'),
            ('APAR [umol m-2 s-1]','Absorbed Photosynthetically Active Radiation'),
            ('E_stability full [%]','Percentage difference between WR1 and WR2. Full range'),
            ('sat value L full [boolean]','Saturation value of downward channel. Full range'),
            ('sat value E full [boolean]','Saturation value of upward channel 1. Full range'),
            ('sat value E2 full [boolean]','Saturation value of upward channel2. Full range'),
            ('Dynamic range E full [%]','Dynamic range cover of upward channel. Full range'),
            ('Dynamic range L full [%]','Dynamic range cover of downward channel. Full range'),
            ('SSHIFT [nm]','Spectral shift of Full range spectrometer'),
        ]
        win=tk.Toplevel(self)
        win.title('Variables')
        win.configure(bg='#eef7f0')
        win.geometry('980x620')
        win.minsize(760,420)
        body=tk.Frame(win,bg='#eef7f0',padx=14,pady=14)
        body.pack(fill='both',expand=True)
        tk.Label(body,text='Variables Reference',bg='#eef7f0',fg='#153f2a',font=('Arial',15,'bold'),anchor='w').pack(fill='x',pady=(0,8))
        table=scrolledtext.ScrolledText(body,wrap='none',height=22,font=('Consolas',11),relief='solid',bd=1,undo=False)
        table.pack(fill='both',expand=True)
        header_bg='#2f6846'; row_odd='#e7f4ea'; row_even='#f5fbf6'; fg='#10291c'
        table.tag_configure('header',background=header_bg,foreground='white',font=('Consolas',11,'bold'))
        table.tag_configure('odd',background=row_odd,foreground=fg)
        table.tag_configure('even',background=row_even,foreground=fg)
        line_fmt='{:<46}  {}\n'
        table.insert('end',line_fmt.format('Variables','Description'),'header')
        table.insert('end',line_fmt.format('-'*45,'-'*72),'header')
        for i,(var,desc) in enumerate(rows,1):
            table.insert('end',line_fmt.format(var,desc),'odd' if i%2 else 'even')
        table.configure(insertbackground=fg)
        btns=tk.Frame(body,bg='#eef7f0')
        btns.pack(fill='x',pady=(10,0))
        def copy_all():
            win.clipboard_clear()
            win.clipboard_append(table.get('1.0','end-1c'))
        tk.Button(btns,text='Copy All',command=copy_all,width=14,bg='#d9efe0',fg='#153f2a',activebackground='white',activeforeground='#153f2a',font=FONT_BASE).pack(side='left')
        tk.Button(btns,text='Close',command=win.destroy,width=14,bg=HEADER_ACCENT,fg='white',activebackground=SELECTED,activeforeground='white',font=FONT_BASE).pack(side='right')
        win.transient(self)

    def show_vegetation_index(self):
        rows=[
            ('1','NDVI','Normalized Difference Vegetation Index','800; 670'),
            ('2','PRI','Photochemical Reflectance Index','531; 570'),
            ('3','MTCI','Meris Terrestrial Chlorophyll Index','754; 709; 681'),
            ('4','EVI','Enhanced Vegetation Index','800; 670; 480'),
            ('5','RedCl','Red Edge Chlorophyll Index','785; 725'),
            ('6','mCRI','Modified Carotenoid Index','510; 725; 785'),
            ('7','NRIv','Near-Infrared Reflectance of Vegetation','L800'),
            ('8','FO2A','Sun-Induced Fluorescence','O2A'),
        ]
        win=tk.Toplevel(self)
        win.title('Vegetation Index')
        win.configure(bg='#eef7f0')
        win.geometry('900x470')
        win.minsize(760,380)
        body=tk.Frame(win,bg='#eef7f0',padx=14,pady=14)
        body.pack(fill='both',expand=True)
        tk.Label(body,text='Vegetation Index Reference',bg='#eef7f0',fg='#153f2a',font=('Arial',15,'bold'),anchor='w').pack(fill='x',pady=(0,8))
        table=scrolledtext.ScrolledText(body,wrap='none',height=12,font=('Consolas',12),relief='solid',bd=1,undo=False)
        table.pack(fill='both',expand=True)
        header_bg='#2f6846'; index_bg='#3f7955'; row_odd='#e7f4ea'; row_even='#f5fbf6'; fg='#10291c'
        table.tag_configure('header',background=header_bg,foreground='white',font=('Consolas',12,'bold'))
        table.tag_configure('index',background=index_bg,foreground='white',font=('Consolas',12,'bold'))
        table.tag_configure('odd',background=row_odd,foreground=fg)
        table.tag_configure('even',background=row_even,foreground=fg)
        line_fmt='{:<4} {:<9} {:<49} {:<18}\n'
        table.insert('end',line_fmt.format('', 'Indices', 'Description', 'Wavelength'),'header')
        table.insert('end',line_fmt.format('-'*3, '-'*8, '-'*47, '-'*17),'header')
        for i,(idx,name,desc,wl) in enumerate(rows,1):
            tag='odd' if i%2 else 'even'
            start=table.index('end')
            table.insert('end',line_fmt.format(idx,name,desc,wl),tag)
            line_start=start
            line_end=f'{line_start} + {len(idx)} chars'
            table.tag_add('index',line_start,line_end)
        table.configure(insertbackground=fg)
        btns=tk.Frame(body,bg='#eef7f0')
        btns.pack(fill='x',pady=(10,0))
        def copy_all():
            win.clipboard_clear()
            win.clipboard_append(table.get('1.0','end-1c'))
        tk.Button(btns,text='Copy All',command=copy_all,width=14,bg='#d9efe0',fg='#153f2a',activebackground='white',activeforeground='#153f2a',font=FONT_BASE).pack(side='left')
        tk.Button(btns,text='Close',command=win.destroy,width=14,bg=HEADER_ACCENT,fg='white',activebackground=SELECTED,activeforeground='white',font=FONT_BASE).pack(side='right')
        win.transient(self)

    def show_about(self):
        def add_text(parent,text,font=FONT_BASE,fg=TEXT,pady=(0,4)):
            tk.Label(parent,text=text,bg=PANEL,fg=fg,font=font,justify='left',anchor='w',wraplength=700).pack(fill='x',pady=pady)
        def add_link(parent,text,url):
            lbl=tk.Label(parent,text=text,bg=PANEL,fg='#075985',font=('Arial',10,'underline'),cursor='hand2',anchor='w')
            lbl.pack(fill='x',pady=(0,4))
            lbl.bind('<Button-1>',lambda _e,u=url:webbrowser.open(u))
            return lbl
        win=tk.Toplevel(self)
        win.title('About '+APP_TITLE)
        win.geometry('780x520')
        win.minsize(640,430)
        win.configure(bg=BG)
        body=tk.Frame(win,bg=PANEL,padx=20,pady=18,highlightbackground=BORDER,highlightthickness=1)
        body.pack(fill='both',expand=True,padx=14,pady=14)
        tk.Label(body,text=APP_TOPIC,bg=PANEL,fg=HEADER_ACCENT,font=FONT_TOPIC,anchor='w').pack(fill='x')
        tk.Label(body,text=APP_TITLE,bg=PANEL,fg=HEADER,font=FONT_HEADER,anchor='w').pack(fill='x',pady=(0,12))
        add_text(body,'Python GUI, processing adaptation, and automation logic:',font=('Arial',10,'bold'))
        add_text(body,'Biplob Dey',font=('Arial',11,'bold'))
        add_text(body,'Forschungszentrum J\u00fclich GmbH, ICE-3 Troposphere')
        add_link(body,'biplobforestry@gmail.com','mailto:biplobforestry@gmail.com')
        add_link(body,'b.dey@fz-juelich.de','mailto:b.dey@fz-juelich.de')
        add_link(body,'https://www.fz-juelich.de/profile/dey_b','https://www.fz-juelich.de/profile/dey_b')
        add_text(body,'This Python version adapts/reimplements FloX/FieldSpectroscopy processing logic from the R packages FieldSpectroscopyCC and FieldSpectroscopyDP, originally released under GNU GPL v3.0:',pady=(12,4))
        add_link(body,'https://github.com/tommasojulitta','https://github.com/tommasojulitta')
        add_text(body,'Additional automation was implemented to synchronize data/workflows between the Airship Noseboom, Gimbal, and SIF instrument for the Zeppelin Campaign 2026.',pady=(12,4))
        add_text(body,'Distributed under GNU GPL v3.0. The original authors are not responsible for modifications, bugs, automation behavior, or scientific differences in this version.')
        tk.Button(body,text='Close',command=win.destroy,width=14,bg=HEADER_ACCENT,fg='white',activebackground=SELECTED,activeforeground='white',font=FONT_BASE).pack(anchor='e',pady=(12,0))
        win.transient(self)
        win.grab_set()

    def button_styles(self,selected):
        return {'bg':SELECTED,'fg':'white','activebackground':SELECTED_ACTIVE,'activeforeground':'white','relief':'sunken','bd':1} if selected else {'bg':UNSELECTED,'fg':TEXT,'activebackground':UNSELECTED_ACTIVE,'activeforeground':TEXT,'relief':'raised','bd':1}
    def set_spectral_shift(self,value):
        self.vars['spectral_shift'].set(value)
        self.refresh_option_controls()
    def set_time_filter(self,value):
        self.vars['time_filter'].set(value)
        self.refresh_option_controls()
        if value=='custom':
            self.start_common_time_check()
    def refresh_option_controls(self):
        if hasattr(self,'shift_yes_button'):
            self.shift_yes_button.config(**self.button_styles(self.vars['spectral_shift'].get()=='yes'))
            self.shift_no_button.config(**self.button_styles(self.vars['spectral_shift'].get()=='no'))
        if hasattr(self,'time_default_button'):
            custom=self.vars['time_filter'].get()=='custom'
            self.time_default_button.config(**self.button_styles(not custom))
            self.time_custom_button.config(**self.button_styles(custom))
            for entry in getattr(self,'time_entries',[]): entry.config(state='normal' if custom else 'disabled')
    def show_checking_popup(self):
        if self.checking_popup is not None and self.checking_popup.winfo_exists():
            return
        win=tk.Toplevel(self)
        win.title('Checking')
        win.geometry('330x120')
        win.resizable(False,False)
        win.configure(bg=PANEL)
        tk.Label(win,text='Checking available common time...',bg=PANEL,fg=HEADER,font=('Arial',11,'bold')).pack(expand=True,pady=(18,4))
        tk.Label(win,text='Scanning FULL and FLUO raw timestamps',bg=PANEL,fg=MUTED,font=FONT_BASE).pack(pady=(0,14))
        bar=ttk.Progressbar(win,orient='horizontal',mode='indeterminate',length=230)
        bar.pack(pady=(0,14))
        bar.start(12)
        win.transient(self)
        try:
            win.grab_set()
        except Exception:
            pass
        self.checking_popup=win

    def close_checking_popup(self):
        win=self.checking_popup
        self.checking_popup=None
        if win is not None:
            try:
                win.grab_release()
                win.destroy()
            except Exception:
                pass

    def get_raw_min_kb_confirmed(self):
        raw_text=(self.vars['raw_min_kb'].get() or '100').strip()
        try:
            value=float(raw_text)
        except ValueError:
            messagebox.showerror('Invalid value','Raw file size filter must be a numeric value in KB.')
            self.vars['raw_min_kb'].set('100')
            return None
        if value<0:
            messagebox.showerror('Invalid value','Raw file size filter cannot be negative.')
            self.vars['raw_min_kb'].set('100')
            return None
        if value>150:
            ok=messagebox.askyesno('!Warning','You entered a raw file size filter greater than 150 KB. Are you sure?')
            if not ok:
                self.vars['raw_min_kb'].set('100')
                return None
        return value

    def start_common_time_check(self):
        if self.running:
            return
        if self.get_raw_min_kb_confirmed() is None:
            return
        self.show_checking_popup()
        threading.Thread(target=self.common_time_check_worker,daemon=True).start()

    def format_utc_for_entry(self,dt):
        return dt.strftime('%Y-%m-%d %H:%M:%S')

    def raw_time_range(self,proc,files,cal_path,mode):
        cal=proc.read_full_calibration(cal_path)
        nwl=len(cal['wl'])
        times=[]
        for raw_path in files:
            raw=proc.read_drox_full(raw_path,nwl,drop_e500_zero=(mode=='FLUO'))
            base=proc.get_gps_utc(raw)
            offsets=(raw.cpu2-raw.cpu1)/1000
            for dt,offset in zip(base,offsets):
                if dt is not None:
                    times.append(dt+proc.timedelta(seconds=float(offset)))
        if not times:
            raise ValueError(f'No valid {mode} timestamps found in raw files.')
        return min(times),max(times),len(times)

    def common_time_check_worker(self):
        try:
            import airflox_sif_automation as proc
            raw_min_kb=float(self.vars['raw_min_kb'].get() or 100)
            directory=Path(self.vars['directory'].get())
            flight=self.vars['flight'].get().strip()
            flox=proc.find_floxinside(directory,flight)
            full_folder=proc.find_named_folder(flox,{'FULL'})
            fluo_folder=proc.find_named_folder(flox,{'FLOX','FLUO'})
            full_files=proc.raw_files(full_folder,'FULL',raw_min_kb)
            fluo_files=proc.raw_files(fluo_folder,'FLUO',raw_min_kb)
            if not full_files:
                raise FileNotFoundError(f'No FULL raw files found in {full_folder}')
            if not fluo_files:
                raise FileNotFoundError(f'No FLUO/FLOX raw files found in {fluo_folder}')
            full_cal,_idx=proc.detect_essential_file(Path(self.vars['essentials'].get()),'FULL')
            fluo_cal,_idx=proc.detect_essential_file(Path(self.vars['essentials'].get()),'FLUO')
            full_min,full_max,full_n=self.raw_time_range(proc,full_files,full_cal,'FULL')
            fluo_min,fluo_max,fluo_n=self.raw_time_range(proc,fluo_files,fluo_cal,'FLUO')
            common_start=max(full_min,fluo_min)
            common_end=min(full_max,fluo_max)
            if common_start>common_end:
                raise ValueError('No common time overlap between FULL and FLUO raw files.\nFULL: '+self.format_utc_for_entry(full_min)+' to '+self.format_utc_for_entry(full_max)+'\nFLUO: '+self.format_utc_for_entry(fluo_min)+' to '+self.format_utc_for_entry(fluo_max))
            def apply_success():
                self.close_checking_popup()
                self.vars['time_start_utc'].set(self.format_utc_for_entry(common_start))
                self.vars['time_end_utc'].set(self.format_utc_for_entry(common_end))
                self.log(f'Common FULL/FLUO time window set: {self.format_utc_for_entry(common_start)} to {self.format_utc_for_entry(common_end)} (FULL n={full_n}, FLUO n={fluo_n})')
            self.after(0,apply_success)
        except Exception as exc:
            message=str(exc)
            def apply_error():
                self.close_checking_popup()
                self.log('warning=Could not determine common FULL/FLUO time window: '+message)
                messagebox.showwarning('Common Time Check',message)
            self.after(0,apply_error)

    def set_platform_mode(self,mode):
        self.vars['platform'].set(mode)
        self.refresh_platform_controls()
    def refresh_platform_controls(self):
        mode=self.vars['platform'].get()
        active=self.button_styles(True)
        inactive=self.button_styles(False)
        if hasattr(self,'airship_button'):
            self.airship_button.config(**(active if mode=='uav_airship' else inactive))
        if hasattr(self,'tower_button'):
            self.tower_button.config(**(active if mode=='tower' else inactive))
        state='normal' if mode=='tower' else 'disabled'
        fg='black' if mode=='tower' else '#777777'
        for entry in getattr(self,'static_entries',[]): entry.config(state=state)
        for label in getattr(self,'static_labels',[]): label.config(fg=fg)
    def pick(self,key):
        val=filedialog.askopenfilename(filetypes=[('CSV','*.csv'),('All','*.*')]) if key=='log' else filedialog.askdirectory()
        if val: self.vars[key].set(val)
    def format_duration(self,seconds):
        seconds=max(0,int(seconds)); minutes,sec=divmod(seconds,60); hour,minutes=divmod(minutes,60)
        return f'{hour:d}:{minutes:02d}:{sec:02d}' if hour else f'{minutes:02d}:{sec:02d}'
    def create_step_window(self):
        if self.step_window is not None and self.step_window.winfo_exists():
            self.step_window.lift()
            return
        self.step_rows={}; self.step_start_times={}
        win=tk.Toplevel(self)
        win.title('Processing Details')
        win.geometry('860x430')
        win.minsize(760,360)
        win.configure(bg='#eef7f0')
        self.step_window=win
        tk.Label(win,text='Processing Details',bg='#eef7f0',fg=HEADER,font=('Arial',14,'bold')).pack(anchor='w',padx=14,pady=(12,4))
        tk.Label(win,text='Each step is updated by the processing engine. Scientific calculations are unchanged.',bg='#eef7f0',fg=MUTED,font=FONT_BASE).pack(anchor='w',padx=14,pady=(0,8))
        frame=tk.Frame(win,bg='#eef7f0')
        frame.pack(fill='both',expand=True,padx=14,pady=8)
        headers=('Step','Status','Progress','Elapsed','Message')
        widths=(28,11,16,10,52)
        for c,(h,w) in enumerate(zip(headers,widths)):
            tk.Label(frame,text=h,bg='#2f6846',fg='white',font=('Arial',10,'bold'),anchor='w',width=w,padx=6,pady=5).grid(row=0,column=c,sticky='ew',padx=1,pady=1)
        steps=[
            ('setup','Setup and folder detection'),
            ('position','Position/log preparation'),
            ('full_raw','FULL raw files'),
            ('full_process','FULL calculation and export'),
            ('fluo_raw','FLUO raw files'),
            ('fluo_process','FLUO calculation and export'),
            ('finalize','Finalize outputs'),
        ]
        for r,(key,label) in enumerate(steps,1):
            bg='#eef2f3'
            step_lab=tk.Label(frame,text=label,bg=bg,fg=TEXT,font=FONT_BASE,anchor='w',width=widths[0],padx=6,pady=5,relief='solid',bd=1)
            status_lab=tk.Label(frame,text='Pending',bg=bg,fg=TEXT,font=FONT_BASE,anchor='w',width=widths[1],padx=6,pady=5,relief='solid',bd=1)
            bar_holder=tk.Frame(frame,bg=bg,relief='solid',bd=1)
            bar=ttk.Progressbar(bar_holder,orient='horizontal',mode='determinate',maximum=100,length=120)
            bar.pack(fill='x',expand=True,padx=6,pady=6)
            elapsed_lab=tk.Label(frame,text='--',bg=bg,fg=TEXT,font=FONT_BASE,anchor='w',width=widths[3],padx=6,pady=5,relief='solid',bd=1)
            msg_lab=tk.Label(frame,text='',bg=bg,fg=TEXT,font=FONT_BASE,anchor='w',width=widths[4],padx=6,pady=5,relief='solid',bd=1)
            for c,wid in enumerate((step_lab,status_lab,bar_holder,elapsed_lab,msg_lab)):
                wid.grid(row=r,column=c,sticky='ew',padx=1,pady=1)
            self.step_rows[key]={'labels':[step_lab,status_lab,elapsed_lab,msg_lab],'bar':bar,'bar_holder':bar_holder,'name':label}
        frame.columnconfigure(4,weight=1)
        btns=tk.Frame(win,bg='#eef7f0')
        btns.pack(fill='x',padx=14,pady=(0,12))
        tk.Button(btns,text='Close',command=win.withdraw,width=12,bg=HEADER_ACCENT,fg='white',activebackground=SELECTED,activeforeground='white').pack(side='right')
        win.protocol('WM_DELETE_WINDOW',win.withdraw)

    def reset_step_window(self):
        self.create_step_window()
        self.step_start_times={}
        for key,row in self.step_rows.items():
            labels=row['labels']; bar=row['bar']
            labels[1].config(text='Pending')
            labels[2].config(text='--')
            labels[3].config(text='')
            bar.stop(); bar.config(mode='determinate',value=0)
            for lab in labels:
                lab.config(bg='#eef2f3',fg=TEXT)
            row['bar_holder'].config(bg='#eef2f3')

    def progress_step(self,key,status='running',message=''):
        def apply():
            if self.step_window is None or not self.step_window.winfo_exists():
                self.create_step_window()
            row=self.step_rows.get(key)
            if not row:
                return
            labels=row['labels']; bar=row['bar']
            now=time.time()
            if status=='running':
                self.step_start_times[key]=now
                bar.stop(); bar.config(mode='indeterminate',value=0); bar.start(12)
            else:
                bar.stop(); bar.config(mode='determinate')
                bar['value']=100 if status in ('done','warning') else 0
            elapsed='--'
            if key in self.step_start_times:
                elapsed=self.format_duration(now-self.step_start_times[key])
            status_text={'running':'Running','done':'Done','warning':'Warning','failed':'Failed','pending':'Pending'}.get(status,status.title())
            bg={'running':'#fff7cc','done':'#e3f6e8','warning':'#fff0d6','failed':'#fde2e2','pending':'#eef2f3'}.get(status,'#eef2f3')
            labels[1].config(text=status_text)
            labels[2].config(text=elapsed)
            labels[3].config(text=str(message)[:95])
            for lab in labels:
                lab.config(bg=bg)
            row['bar_holder'].config(bg=bg)
            progress_map={'setup':8,'position':18,'full_raw':30,'full_process':52,'fluo_raw':64,'fluo_process':86,'finalize':96}
            if status=='running':
                self.progress_value=max(self.progress_value,progress_map.get(key,self.progress_value))
                self.progress['value']=self.progress_value
            elif status in ('done','warning'):
                self.progress_value=max(self.progress_value,progress_map.get(key,self.progress_value)+4)
                self.progress['value']=min(96,self.progress_value)
            if status in ('running','warning','failed'):
                self.status.set(f'{status_text}: {row["name"]}')
        self.after(0,apply)

    def set_progress(self,value,message=None):
        def apply():
            self.progress_value=max(0.0,min(100.0,float(value))); self.progress['value']=self.progress_value
            if message: self.status.set(message)
        self.after(0,apply)
    def update_eta(self):
        if not self.running or self.start_time is None: return
        elapsed=time.time()-self.start_time
        if 0<self.progress_value<82:
            eta=elapsed*(82-self.progress_value)/max(self.progress_value,1)
            self.status.set(f'Processing... {self.progress_value:.0f}% | elapsed {self.format_duration(elapsed)} | estimated time to final export stage {self.format_duration(eta)}')
            self.progress_value=min(82,self.progress_value+1.2)
        elif self.progress_value<96:
            self.status.set(f'Final processing and exporting files... {self.progress_value:.0f}% | elapsed {self.format_duration(elapsed)} | still running')
            self.progress_value=min(96,self.progress_value+0.25)
        else:
            self.status.set(f'Final processing and exporting files... elapsed {self.format_duration(elapsed)} | still running')
        self.progress['value']=self.progress_value
        self.after(1000,self.update_eta)
    def log(self,msg): self.after(0,lambda:self._log_apply(msg))
    def _log_apply(self,msg): self.text.insert('end',str(msg)+'\n'); self.text.see('end'); self.update_idletasks()
    def process(self):
        if self.running: return
        if self.get_raw_min_kb_confirmed() is None:
            return
        self.running=True; self.start_time=time.time(); self.progress_value=0.0; self.last_made=[]
        self.reset_step_window()
        self.progress['value']=0; self.status.set('Starting...'); self.process_button.config(state='disabled'); self.results_button.config(state='disabled')
        self.update_eta(); threading.Thread(target=self.worker,daemon=True).start()
    def worker(self):
        try:
            self.set_progress(5,'Checking required libraries...'); ensure_libraries(self.log)
            self.set_progress(12,'Loading processing code...'); import airflox_sif_automation as proc
            raw_min_kb=float(self.vars['raw_min_kb'].get() or 100)
            args=argparse.Namespace(directory=Path(self.vars['directory'].get()),flight_name=self.vars['flight'].get().strip(),output=Path(self.vars['output'].get()),essentials=Path(self.vars['essentials'].get()),log=Path(self.vars['log'].get()) if self.vars['log'].get().strip() else None,platform_mode=self.vars['platform'].get(),static_lat=self.vars['static_lat'].get().strip() or None,static_lon=self.vars['static_lon'].get().strip() or None,static_alt=self.vars['static_alt'].get().strip() or None,altitude_filter='yes' if self.vars['altitude'].get() else 'no',apply_nonlinearity_correction='yes' if self.vars['nonlinear'].get() else 'no',spectral_shift_correction=self.vars['spectral_shift'].get(),raw_min_kb=raw_min_kb,time_filter=self.vars['time_filter'].get(),time_start_utc=self.vars['time_start_utc'].get().strip() or None,time_end_utc=self.vars['time_end_utc'].get().strip() or None,progress_callback=self.progress_step)
            buf=StringIO(); self.set_progress(25,'Processing FULL/FLUO data, matching telemetry, and exporting files...')
            with redirect_stdout(buf), redirect_stderr(buf): made=proc.run_flight(args)
            self.last_made=[Path(p) for p in made]; self.set_progress(86,'Writing processing log...'); self.log(buf.getvalue())
            self.after(0,self.complete_success)
        except Exception:
            self.progress_step('finalize','failed','Processing failed; see log window')
            self.log(traceback.format_exc()); self.finish_progress(False); self.after(0,lambda:messagebox.showerror('Processing failed','See log window for details.'))
    def complete_success(self):
        self.refresh_result_files()
        self.finish_progress(True)
    def finish_progress(self,ok):
        def apply():
            self.running=False
            if ok: self.progress_value=100.0; self.progress['value']=100
            elapsed=self.format_duration(time.time()-self.start_time) if self.start_time else '00:00'
            self.status.set(('Done' if ok else 'Failed')+f' | elapsed {elapsed}')
            self.process_button.config(state='normal')
            if ok and self.result_files: self.results_button.config(state='normal')
        self.after(0,apply)
    def output_flight_root(self): return Path(self.vars['output'].get())/self.vars['flight'].get().strip()
    def refresh_result_files(self):
        root=self.output_flight_root(); found={}
        for mode in ('FULL','FLUO'):
            files=sorted(root.rglob(f'ALL_INDEX_AIRFLOX_{mode}_*.csv'),key=lambda p:p.stat().st_mtime if p.exists() else 0,reverse=True)
            if files: found[mode]=files[0]
        self.result_files=found
    def enable_existing_results(self):
        self.refresh_result_files()
        if self.result_files: self.results_button.config(state='normal')
    def open_results(self):
        self.refresh_result_files()
        if not self.result_files:
            messagebox.showwarning('No results','No ALL_INDEX result files found. Process a flight first.'); return
        win=tk.Toplevel(self); win.title('Results'); win.geometry('560x260')
        tk.Label(win,text='Select data product',font=('TkDefaultFont',11,'bold')).pack(pady=(16,8))
        current=tk.StringVar(value='')
        mode_frame=tk.Frame(win); mode_frame.pack(pady=4)
        buttons={}
        def set_mode(mode):
            current.set('' if current.get()==mode else mode)
            refresh_buttons()
        def refresh_buttons():
            for mode,btn in buttons.items():
                selected=current.get()==mode
                btn.config(bg='#1b8f3a' if selected else 'SystemButtonFace',fg='white' if selected else 'black',relief='sunken' if selected else 'raised')
            info.set(f'{current.get()}: {self.result_files.get(current.get(), "select FULL or FLUO")}' if current.get() else 'No data product selected')
        for mode in ('FULL','FLUO'):
            btn=tk.Button(mode_frame,text=mode,width=14,state='normal' if mode in self.result_files else 'disabled',command=lambda m=mode:set_mode(m))
            btn.pack(side='left',padx=6); buttons[mode]=btn
        action=tk.Frame(win); action.pack(pady=18)
        def require_mode(callback):
            if not current.get(): messagebox.showwarning('Select product','Please select FULL or FLUO first.'); return
            callback(current.get())
        tk.Button(action,text='Timeseries',width=18,command=lambda:require_mode(self.open_timeseries)).pack(side='left',padx=8)
        tk.Button(action,text='MAP',width=18,command=lambda:require_mode(self.open_map_view)).pack(side='left',padx=8)
        info=tk.StringVar(); tk.Label(win,textvariable=info,wraplength=500,justify='left').pack(pady=8)
        if 'FULL' in self.result_files: set_mode('FULL')
        elif self.result_files: set_mode(next(iter(self.result_files)))
        else: refresh_buttons()
    def available_variables(self,df,preferred):
        cols=[c for c in preferred if c in df.columns]
        extras=[c for c in df.columns if c not in {'datetime [UTC]','Lat','Lon','Alt','ID','radius_nocos','doy.dayfract'} and c not in cols]
        return cols+extras
    def open_timeseries(self,mode):
        path=self.result_files.get(mode)
        if not path: messagebox.showwarning('Missing result',f'No {mode} result file found.'); return
        ensure_libraries(self.log)
        import numpy as np
        import matplotlib.dates as mdates
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
        df=read_result_csv(path); variables=self.available_variables(df,FULL_VARIABLES)
        if not variables: messagebox.showwarning('No variables','No plottable variables found.'); return
        win=tk.Toplevel(self); win.title(f'{mode} Timeseries'); win.geometry('1220x760')
        top=tk.Frame(win); top.pack(fill='x',padx=10,pady=8)
        tk.Label(top,text='Variable').grid(row=0,column=0,sticky='w')
        var=tk.StringVar(value=variables[0]); combo=ttk.Combobox(top,textvariable=var,values=variables,width=56,state='readonly'); combo.grid(row=0,column=1,padx=8,sticky='w')
        time_ok='datetime [UTC]' in df.columns and df['datetime [UTC]'].notna().any()
        min_time=df['datetime [UTC]'].min() if time_ok else None; max_time=df['datetime [UTC]'].max() if time_ok else None
        start_var=tk.StringVar(value=min_time.strftime('%Y-%m-%d %H:%M:%S') if time_ok else '')
        end_var=tk.StringVar(value=max_time.strftime('%Y-%m-%d %H:%M:%S') if time_ok else '')
        tk.Label(top,text='Start').grid(row=1,column=0,sticky='w',pady=(8,0)); tk.Entry(top,textvariable=start_var,width=22).grid(row=1,column=1,sticky='w',padx=8,pady=(8,0))
        tk.Label(top,text='End').grid(row=1,column=1,sticky='e',pady=(8,0)); tk.Entry(top,textvariable=end_var,width=22).grid(row=1,column=2,sticky='w',padx=8,pady=(8,0))
        stats=tk.StringVar(); tk.Label(top,textvariable=stats,justify='left').grid(row=0,column=2,columnspan=3,sticky='w',padx=14)
        fig=Figure(figsize=(11.5,6.2),dpi=100); ax=fig.add_subplot(121); hx=fig.add_subplot(122)
        canvas=FigureCanvasTkAgg(fig,master=win); canvas.get_tk_widget().pack(fill='both',expand=True)
        toolbar=NavigationToolbar2Tk(canvas,win); toolbar.update()
        def filtered_frame():
            data=df.copy()
            if time_ok:
                st=pd_to_datetime(start_var.get()); en=pd_to_datetime(end_var.get())
                if st is not None: data=data[data['datetime [UTC]']>=st]
                if en is not None: data=data[data['datetime [UTC]']<=en]
            return data
        def kde_curve(vals):
            vals=np.asarray(vals,dtype=float); vals=vals[np.isfinite(vals)]
            if len(vals)<2: return None,None
            xs=np.linspace(vals.min(),vals.max(),220); std=vals.std(ddof=1) if len(vals)>1 else 0
            bw=1.06*std*(len(vals)**(-1/5)) if std>0 else (vals.max()-vals.min())/20
            if not np.isfinite(bw) or bw<=0: return None,None
            dens=np.exp(-0.5*((xs[:,None]-vals[None,:])/bw)**2).sum(axis=1)/(len(vals)*bw*np.sqrt(2*np.pi))
            return xs,dens
        def pd_to_datetime(text):
            try:
                import pandas as pd
                val=pd.to_datetime(text,errors='coerce')
                return None if pd.isna(val) else val
            except Exception: return None
        def reset_time():
            if time_ok:
                start_var.set(min_time.strftime('%Y-%m-%d %H:%M:%S')); end_var.set(max_time.strftime('%Y-%m-%d %H:%M:%S'))
            redraw()
        def redraw(*_):
            ax.clear(); hx.clear(); data=filtered_frame(); col=var.get(); y=numeric_series(data,col); ok=y.notna()
            x=data['datetime [UTC]'] if time_ok else np.arange(len(data))
            ax.plot(x[ok],y[ok],color='#1464a5',linewidth=1.25); ax.set_title(col); ax.set_xlabel('datetime [UTC]' if time_ok else 'sample'); ax.set_ylabel(col); ax.grid(True,alpha=.25)
            if time_ok:
                loc=mdates.AutoDateLocator(minticks=4,maxticks=8); ax.xaxis.set_major_locator(loc); ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc)); fig.autofmt_xdate(rotation=0)
            vals=y.dropna(); counts,bins,_=hx.hist(vals,bins=30,color='#0b8f88',edgecolor='black',alpha=.95); hx.set_title('Histogram'); hx.set_xlabel('Values'); hx.grid(True,axis='y',alpha=.22)
            xs,dens=kde_curve(vals)
            if xs is not None and len(counts):
                scale=(bins[1]-bins[0])*len(vals); hx.plot(xs,dens*scale,color='#d45555',linewidth=2.0)
            if len(vals):
                mode_val=vals.mode().iloc[0] if len(vals.mode()) else vals.median()
                stats.set(f'Mean      {vals.mean():.5g}\nMedian    {vals.median():.5g}\nMode      {mode_val:.5g}\nn         {len(vals)}\nMin/Max   {vals.min():.5g} / {vals.max():.5g}')
            else: stats.set('No numeric data')
            fig.tight_layout(); canvas.draw_idle()
        ttk.Button(top,text='Apply time',command=redraw).grid(row=1,column=3,padx=4,pady=(8,0))
        ttk.Button(top,text='Reset time',command=reset_time).grid(row=1,column=4,padx=4,pady=(8,0))
        combo.bind('<<ComboboxSelected>>',redraw); redraw()
    def open_map_view(self,mode):
        path=self.result_files.get(mode)
        if not path: messagebox.showwarning('Missing result',f'No {mode} result file found.'); return
        ensure_libraries(self.log)
        import numpy as np
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
        from matplotlib.ticker import FuncFormatter
        df=read_result_csv(path); variables,invalid_variables=valid_map_variables(df)
        if not variables: messagebox.showwarning('No map variables','No valid numeric map variables found. FLUO may not support PRI/EVI because required wavelengths are outside its spectral range.'); return
        win=tk.Toplevel(self); win.title(f'{mode} MAP'); win.geometry('1160x780')
        controls=tk.Frame(win); controls.pack(fill='x',padx=10,pady=8)
        tk.Label(controls,text='Variable').pack(side='left'); var=tk.StringVar(value=variables[0]); ttk.Combobox(controls,textvariable=var,values=variables,width=18,state='readonly').pack(side='left',padx=6)
        tk.Label(controls,text='Color bar').pack(side='left',padx=(12,0)); palette=tk.StringVar(value='RdYlGn'); ttk.Combobox(controls,textvariable=palette,values=['PiYG','PRGn','RdYlGn'],width=10,state='readonly').pack(side='left',padx=6)
        krig=tk.BooleanVar(value=False); tk.Checkbutton(controls,text='Kriging',variable=krig).pack(side='left',padx=10)
        tk.Label(controls,text='Buffer m').pack(side='left'); buffer_m=tk.StringVar(value='500'); ttk.Combobox(controls,textvariable=buffer_m,values=['50','500','1300'],width=8,state='normal').pack(side='left',padx=6)
        invalid_note=', '.join(f'{n} ({reason})' for n,reason in invalid_variables)
        status=tk.StringVar(value=(str(path) if not invalid_note else f'{path} | hidden from map: {invalid_note}')); tk.Label(controls,textvariable=status,wraplength=680,anchor='w').pack(side='left',padx=10)
        nb=ttk.Notebook(win); nb.pack(fill='both',expand=True)
        preview=tk.Frame(nb); osm=tk.Frame(nb); nb.add(preview,text='Map preview'); nb.add(osm,text='OpenStreetMap')
        fig=Figure(figsize=(9.8,6.2),dpi=160); canvas=FigureCanvasTkAgg(fig,master=preview); canvas.get_tk_widget().pack(fill='both',expand=True); toolbar=NavigationToolbar2Tk(canvas,preview); toolbar.update()
        osm_msg=tk.Label(osm,text='Click the button to open the interactive OpenStreetMap in your browser. The browser map includes variable, color bar, Kriging Yes/No, buffer in meters, and map scale controls.',wraplength=850,justify='left'); osm_msg.pack(pady=22)
        def palette_cmap(): return palette.get()
        def parse_buffer():
            try: return max(0.0,float(buffer_m.get()))
            except Exception: return 500.0
        def lat_label(x,pos): return f'{abs(x):.1f}{chr(176)} {"N" if x>=0 else "S"}'
        def lon_label(x,pos): return f'{abs(x):.1f}{chr(176)} {"E" if x>=0 else "W"}'
        def draw_preview(*_):
            fig.clear(); ax=fig.add_subplot(111); import pandas as pd
            work=df[['Lat','Lon',var.get()]].copy(); work['Lat']=pd.to_numeric(work['Lat'],errors='coerce'); work['Lon']=pd.to_numeric(work['Lon'],errors='coerce'); work[var.get()]=pd.to_numeric(work[var.get()],errors='coerce'); work=work.dropna(subset=['Lat','Lon',var.get()])
            if work.empty: ax.set_title('No valid map data'); canvas.draw_idle(); return
            x=work['Lon'].to_numpy(float); y=work['Lat'].to_numpy(float); z=work[var.get()].to_numpy(float)
            if krig.get() and len(z)>3:
                buf=parse_buffer(); lat_pad=buf/111000; lon_pad=buf/71000
                xi=np.linspace(x.min()-lon_pad,x.max()+lon_pad,140); yi=np.linspace(y.min()-lat_pad,y.max()+lat_pad,140); xx,yy=np.meshgrid(xi,yi); zz=np.zeros_like(xx); ww=np.zeros_like(xx)
                for px,py,pz in zip(x,y,z):
                    d=np.hypot((xx-px)*71000,(yy-py)*111000); w=1/np.maximum(d,1)**2; zz+=w*pz; ww+=w
                def dist_segment(px,py,ax0,ay0,bx0,by0):
                    vx=(bx0-ax0)*71000; vy=(by0-ay0)*111000; wx=(px-ax0)*71000; wy=(py-ay0)*111000; den=vx*vx+vy*vy
                    t=np.zeros_like(px) if den<=0 else np.clip((wx*vx+wy*vy)/den,0,1)
                    return np.hypot(wx-t*vx,wy-t*vy)
                dist=np.full_like(xx,np.inf,dtype=float)
                if len(x)==1: dist=np.hypot((xx-x[0])*71000,(yy-y[0])*111000)
                else:
                    for k in range(1,len(x)): dist=np.minimum(dist,dist_segment(xx,yy,x[k-1],y[k-1],x[k],y[k]))
                surf=np.where(dist<=buf,zz/ww,np.nan)
                ax.imshow(surf,extent=[xi.min(),xi.max(),yi.min(),yi.max()],origin='lower',cmap=palette_cmap(),alpha=.58,aspect='auto')
                ax.set_title(f'{var.get()} | Kriging surface ({buf:g} m buffer) | WGS 84')
            sc=ax.scatter(x,y,c=z,cmap=palette_cmap(),s=32,edgecolors='black',linewidths=.25)
            ax.plot(x,y,color='0.35',linewidth=.8,alpha=.4); ax.set_xlabel('Longitude (WGS 84)'); ax.set_ylabel('Latitude (WGS 84)'); ax.grid(True,alpha=.25); ax.set_title(ax.get_title() or f'{var.get()} | WGS 84')
            ax.xaxis.set_major_formatter(FuncFormatter(lon_label)); ax.yaxis.set_major_formatter(FuncFormatter(lat_label))
            try: fig.colorbar(sc,ax=ax,label=var.get())
            except Exception: pass
            fig.tight_layout(); canvas.draw_idle()
        def open_osm():
            try:
                html=make_variable_map(df,path,var.get(),variables,palette.get(),parse_buffer()); status.set(f'Created: {html}'); webbrowser.open(html.as_uri())
            except Exception as exc: messagebox.showerror('Map failed',str(exc))
        ttk.Button(osm,text='Open interactive OSM map in browser',command=open_osm,width=34).pack(pady=8)
        ttk.Button(controls,text='Refresh preview',command=draw_preview).pack(side='right',padx=5)
        ttk.Button(controls,text='Open OSM',command=open_osm).pack(side='right',padx=5)
        for control in (var,palette,buffer_m): control.trace_add('write',lambda *_:draw_preview())
        krig.trace_add('write',lambda *_:draw_preview())
        draw_preview()

if __name__=='__main__': App().mainloop()
















