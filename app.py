"""
app.py
──────
Web GUI — submits upload jobs to the shared queue.
All transcription is handled by worker.py.
Branding, colors and languages are read from config.yaml via settings.py.

Start:
    uvicorn app:app --host 0.0.0.0 --port 8000
"""

import hmac
import logging
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Header, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from jobstore import delete_job, delete_jobs, get_job, get_jobs, init_db, retry_job, submit_job
from watchfolder import get_batch_config
from transcribe import OUTPUT_DIR, SUPPORTED_EXTENSIONS
from settings import cfg

app = FastAPI(title=f"{cfg.branding.organization} | {cfg.branding.app_name}")
init_db()

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")


# ─── HTML helpers (config-driven) ────────────────────────────────────────────

def _css_vars() -> str:
    c = cfg.colors
    return (
        f"--bg:{c.bg};--surface:{c.surface};--surface2:{c.surface2};"
        f"--border:{c.border};--border2:{c.border2};--text:{c.text};"
        f"--muted:{c.muted};--green:{c.green};--amber:{c.amber};--red:{c.red};"
        f"--mono:'IBM Plex Mono',monospace;--sans:'IBM Plex Sans',sans-serif;"
    )

def _common_css() -> str:
    return """
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@300;400;500&display=swap');
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
  body{background:var(--bg);color:var(--text);font-family:var(--sans);font-weight:300;min-height:100vh;}
  header{border-bottom:1px solid var(--border);padding:18px 40px;display:flex;align-items:center;justify-content:space-between;}
  .hl{display:flex;align-items:center;gap:16px;}
  h1{font-family:var(--mono);font-size:12px;font-weight:500;letter-spacing:.15em;text-transform:uppercase;}
  .sep{color:var(--border2);font-family:var(--mono);font-size:12px;}
  .sub{font-family:var(--mono);font-size:11px;color:var(--muted);letter-spacing:.05em;}
  .hr{display:flex;align-items:center;gap:20px;}
  .nav{font-family:var(--mono);font-size:10px;color:var(--muted);text-decoration:none;letter-spacing:.1em;text-transform:uppercase;padding:4px 10px;border:1px solid var(--border);border-radius:2px;transition:color .15s,border-color .15s;}
  .nav:hover,.nav.on{color:var(--text);border-color:var(--border2);}
  footer{border-top:1px solid var(--border);padding:16px 40px;font-family:var(--mono);font-size:10px;color:var(--border2);text-align:right;}
  .sh{font-family:var(--mono);font-size:10px;font-weight:500;letter-spacing:.15em;text-transform:uppercase;color:var(--muted);padding-bottom:12px;border-bottom:1px solid var(--border);margin-bottom:2px;}
  .badge{font-family:var(--mono);font-size:10px;letter-spacing:.08em;padding:3px 8px;border-radius:2px;white-space:nowrap;display:flex;align-items:center;gap:5px;}
  .dot{width:5px;height:5px;border-radius:50%;display:inline-block;flex-shrink:0;}
  @keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}
  @keyframes pp{0%,100%{opacity:.45}50%{opacity:.9}}"""

def _nav(active: str) -> str:
    b = cfg.branding
    subtitles = {"upload": b.app_name, "queue": b.queue_subtitle, "system": b.system_subtitle}
    links = [
        ("upload", "/", "Upload"),
        ("queue", "/queue", "Queue"),
        ("system", "/system", "Status"),
    ]
    nav_html = "\n    ".join(
        f'<a class="nav{" on" if k == active else ""}" href="{path}">{label}</a>'
        for k, path, label in links
    )
    return f"""<header>
  <div class="hl">
    <h1>{b.organization}</h1>
    <span class="sep">|</span>
    <span class="sub">{subtitles.get(active, b.app_name)}</span>
  </div>
  <div class="hr">
    {nav_html}
  </div>
</header>"""

def _footer() -> str:
    return f"<footer>{cfg.branding.footer} · v{cfg.version}</footer>"

def _lang_options() -> str:
    opts = "\n".join(
        f'      <option value="{code}">{label}</option>'
        for code, label in cfg.model.upload_languages
    )
    return f'      <option value="auto">Auto-detect</option>\n{opts}'


# ─── API endpoints ────────────────────────────────────────────────────────────

@app.post("/transcribe")
async def upload(file: UploadFile = File(...), language: str = "auto"):
    ext = Path(file.filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return JSONResponse({"error": f"File format '{ext}' is not supported."}, status_code=400)

    staging = Path(cfg.runtime.output_dir) / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    job_id = str(uuid.uuid4())[:8]
    staged = staging / f"{job_id}_{file.filename}"

    with open(str(staged), "wb") as f:
        shutil.copyfileobj(file.file, f)

    lang = None if language == "auto" else language
    submit_job(job_id, filename=file.filename, filepath=str(staged),
               source="upload", language=lang)

    return JSONResponse({"job_id": job_id, "file": file.filename,
                         "language": language, "status": "queued"})


@app.get("/status/{job_id}")
async def status(job_id: str):
    job = get_job(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return JSONResponse(job)


@app.get("/download/{job_id}")
async def download(job_id: str):
    job = get_job(job_id)
    if not job or job["status"] != "done":
        return JSONResponse({"error": "Transcript not available"}, status_code=404)
    out = Path(job["output"])
    if not out.exists():
        return JSONResponse({"error": "File not found on disk"}, status_code=404)
    return FileResponse(out, filename=out.name, media_type="text/plain")


@app.post("/api/jobs/{job_id}/retry")
async def api_retry_job(job_id: str):
    """Requeue a failed job for retry."""
    max_retries = cfg.priority.max_retries
    ok, msg = retry_job(job_id, max_retries=max_retries)
    if not ok:
        return JSONResponse({"error": msg}, status_code=400)
    return JSONResponse({"retried": job_id, "message": msg})


def _delete_upload_file(job: dict):
    """
    Delete the transcript file on disk, but only for manually uploaded jobs.
    Watchfolder transcripts live at user-managed locations (NAS, same_as_source,
    etc.) and must never be touched here. Upload transcripts are only reachable
    through the Web GUI, so removing the job record without removing the file
    would leave it stranded on disk with nothing pointing to it.
    """
    if job.get("source") != "upload":
        return
    out = job.get("output")
    if not out:
        return
    try:
        p = Path(out)
        if p.exists():
            p.unlink()
            logging.getLogger("cleanup").info(f"Deleted upload transcript: {p.name}")
    except Exception as e:
        logging.getLogger("cleanup").warning(f"Could not delete upload transcript {out}: {e}")


@app.delete("/api/jobs/{job_id}")
async def api_delete_job(job_id: str):
    job = get_job(job_id)
    if job:
        _delete_upload_file(job)
    ok = delete_job(job_id)
    if not ok:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return JSONResponse({"deleted": job_id})


@app.delete("/api/jobs")
async def api_delete_jobs(status: str = "all"):
    s = None if status == "all" else status
    jobs = get_jobs(1000, source=None)
    for job in jobs:
        if job["status"] == "running":
            continue
        if s and job["status"] != s:
            continue
        _delete_upload_file(job)
    count = delete_jobs(s)
    return JSONResponse({"deleted": count, "status": status})


@app.get("/api/queue")
async def api_queue():
    return JSONResponse(get_jobs(100))


@app.get("/api/queue/upload")
async def api_queue_upload():
    return JSONResponse(get_jobs(50, source="upload"))


@app.get("/api/system")
async def api_system():
    import shutil as _shutil

    def svc_status(name: str) -> str:
        try:
            r = subprocess.run(["systemctl", "is-active", name],
                               capture_output=True, text=True, timeout=3)
            return r.stdout.strip()
        except Exception:
            return "unknown"

    services = {
        "worker":      svc_status("transcription-worker"),
        "watchfolder": svc_status("transcription-watchfolder"),
        "webgui":      svc_status("transcription-webgui"),
    }

    gpu = {}
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            parts = [p.strip() for p in r.stdout.strip().split(",")]
            gpu = {"name": parts[0], "vram_used": int(parts[1]), "vram_total": int(parts[2]),
                   "utilization": int(parts[3]), "temperature": int(parts[4])}
    except Exception:
        gpu = {"error": "nvidia-smi not available"}

    jobs = get_jobs(1000)
    counts = {"queued": 0, "running": 0, "done": 0, "error": 0}
    for j in jobs:
        if j.get("status") in counts:
            counts[j["status"]] += 1

    output_dir = cfg.runtime.output_dir
    disk = {}
    try:
        usage = _shutil.disk_usage(output_dir)
        disk = {"total_gb": round(usage.total/1e9, 1),
                "used_gb":  round(usage.used/1e9, 1),
                "free_gb":  round(usage.free/1e9, 1)}
    except Exception:
        disk = {"error": "unavailable"}

    # ── Update check ──────────────────────────────────────────────────────────
    update_info = {}
    try:
        import urllib.request
        import json as _json
        req = urllib.request.Request(
            "https://api.github.com/repos/siebentausend/studio8-transcription/releases/latest",
            headers={"User-Agent": "transcription-system"}
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = _json.loads(resp.read())
            latest = data.get("tag_name", "")
            current = cfg.version
            update_info = {
                "current": current,
                "latest": latest,
                "available": latest != current and latest != "",
                "release_date": data.get("published_at", "")[:10],
                "release_notes": data.get("body", "")[:300],
                "release_url": data.get("html_url", ""),
            }
    except Exception:
        update_info = {"current": cfg.version, "latest": "", "available": False}

    return JSONResponse({"version": cfg.version, "services": services, "gpu": gpu,
                         "queue": counts, "disk": disk, "update": update_info})


@app.post("/webhook/ingest-complete")
async def webhook_ingest_complete(
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
):
    if WEBHOOK_SECRET:
        if not x_webhook_secret:
            return JSONResponse({"error": "Missing X-Webhook-Secret header"}, status_code=401)
        if not hmac.compare_digest(x_webhook_secret, WEBHOOK_SECRET):
            return JSONResponse({"error": "Invalid webhook secret"}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    folder = body.get("folder", "").strip()
    if not folder:
        return JSONResponse({"error": "Missing 'folder' in body"}, status_code=400)

    folder_path = Path(folder)
    if not folder_path.is_dir():
        return JSONResponse({"error": f"Folder not found: {folder}"}, status_code=404)

    batch_cfg = get_batch_config(folder)
    if not batch_cfg:
        return JSONResponse({"error": f"No batch watchfolder configured for: {folder}"}, status_code=422)

    files = [p for p in folder_path.iterdir()
             if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS]
    if not files:
        return JSONResponse({"error": f"No supported media files in: {folder}"}, status_code=422)

    job_id     = str(uuid.uuid4())[:8]
    priority   = body.get("priority", batch_cfg.get("priority", 7))
    output_dir = batch_cfg.get("output", cfg.runtime.output_dir)

    submit_job(job_id, filename=folder_path.name, filepath=str(folder_path),
               source="watchfolder", priority=priority, mode="batch", output_dir=output_dir)

    return JSONResponse({"job_id": job_id, "folder": folder,
                         "files": len(files), "priority": priority,
                         "output_dir": output_dir, "status": "queued"})


@app.post("/webhook/debug")
async def webhook_debug(request: Request):
    headers = dict(request.headers)
    try:
        body = await request.json()
    except Exception:
        raw = await request.body()
        body = raw.decode("utf-8", errors="replace")
    import json, logging
    logging.getLogger("webhook.debug").warning(
        "\n── WEBHOOK DEBUG ──\nHeaders: %s\nBody: %s",
        json.dumps(headers, indent=2),
        json.dumps(body, indent=2) if isinstance(body, dict) else body
    )
    return JSONResponse({"received": True, "headers": headers, "body": body})


# ─── Upload page (/) ──────────────────────────────────────────────────────────

def make_upload_html() -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{cfg.branding.organization} | {cfg.branding.app_name}</title>
<style>
  :root{{{_css_vars()}}}
  {_common_css()}
  main{{max-width:760px;margin:0 auto;padding:52px 40px;}}
  #dz{{border:1px dashed var(--border2);border-radius:3px;padding:56px 40px;text-align:center;cursor:pointer;transition:border-color .15s,background .15s;position:relative;background:var(--surface);}}
  #dz:hover,#dz.over{{border-color:var(--text);background:var(--surface2);}}
  #dz input{{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%;}}
  .dicon{{font-size:20px;margin-bottom:14px;display:block;pointer-events:none;color:var(--muted);}}
  .dlbl{{font-size:14px;color:var(--muted);pointer-events:none;line-height:1.6;}}
  .dlbl strong{{color:var(--text);font-weight:500;}}
  .dfmt{{margin-top:12px;font-family:var(--mono);font-size:10px;color:var(--border2);letter-spacing:.08em;pointer-events:none;}}
  #up{{margin-top:14px;display:none;}} #up.on{{display:block;}}
  .plbl{{font-family:var(--mono);font-size:11px;color:var(--muted);margin-bottom:6px;display:flex;justify-content:space-between;}}
  .pw{{height:2px;background:var(--border);border-radius:1px;overflow:hidden;}}
  .pb{{height:100%;background:var(--text);border-radius:1px;transition:width .15s;width:0%;}}
  #js{{margin-top:48px;}}
  #jl{{display:flex;flex-direction:column;gap:2px;margin-top:2px;}}
  .job{{background:var(--surface);border:1px solid var(--border);border-radius:3px;overflow:hidden;}}
  .jh{{padding:14px 18px;display:grid;grid-template-columns:1fr auto auto;align-items:center;gap:16px;}}
  .jn{{font-family:var(--mono);font-size:12px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}}
  .s-running .badge{{background:#2a2200;color:var(--amber);border:1px solid #4a3a00;}}
  .s-done    .badge{{background:#0a2a1a;color:var(--green);border:1px solid #1a4a2a;}}
  .s-error   .badge{{background:#2a0a0a;color:var(--red);border:1px solid #4a1a1a;}}
  .s-queued  .badge{{background:var(--surface2);color:var(--muted);border:1px solid var(--border);}}
  .s-running .dot{{background:var(--amber);animation:blink 1s ease-in-out infinite;}}
  .s-done    .dot{{background:var(--green);}} .s-error .dot{{background:var(--red);}} .s-queued .dot{{background:var(--muted);}}
  .jp{{padding:0 18px 14px;display:none;}}
  .s-running .jp,.s-queued .jp{{display:block;}}
  .step-pips{{display:flex;gap:3px;margin-bottom:7px;}}
  .pip{{flex:1;height:3px;background:var(--border);border-radius:1px;transition:background .4s;}}
  .pip.done{{background:var(--amber);}} .pip.active{{background:var(--amber);opacity:.45;animation:pp 1.2s ease-in-out infinite;}}
  .slbls{{display:flex;gap:3px;margin-bottom:9px;}}
  .sl{{flex:1;font-family:var(--mono);font-size:9px;color:var(--border2);text-align:center;transition:color .3s;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
  .sl.done{{color:var(--amber);}} .sl.active{{color:var(--amber);opacity:.7;}}
  .jmsg{{font-family:var(--mono);font-size:10px;color:var(--muted);}}
  .dl{{font-family:var(--mono);font-size:10px;letter-spacing:.08em;color:var(--bg);background:var(--text);border:none;padding:5px 12px;cursor:pointer;border-radius:2px;text-decoration:none;display:inline-block;transition:opacity .15s;white-space:nowrap;}}
  .dl:hover{{opacity:.75;}} .dl.hidden{{visibility:hidden;}}
  .delbtn{{font-family:var(--mono);font-size:10px;color:var(--muted);background:transparent;border:1px solid var(--border);padding:4px 7px;cursor:pointer;border-radius:2px;transition:color .15s,border-color .15s;}}
  .delbtn:hover{{color:var(--red);border-color:var(--red);}}
  .empty{{font-family:var(--mono);font-size:11px;color:var(--border2);text-align:center;padding:28px 0;border:1px solid var(--border);border-radius:3px;margin-top:2px;}}
  select{{font-family:var(--mono);font-size:11px;background:var(--surface);color:var(--text);border:1px solid var(--border2);border-radius:2px;padding:4px 10px;cursor:pointer;outline:none;}}
  label{{font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:.08em;text-transform:uppercase;}}
</style>
</head>
<body>
{_nav("upload")}
<main>
  <div id="dz">
    <input type="file" id="fi" accept="audio/*,video/*,.mxf,.mov,.mts,.m2ts">
    <span class="dicon">↑</span>
    <div class="dlbl"><strong>Drop file here</strong> or click to select</div>
    <div class="dfmt">mp3 · mp4 · wav · m4a · mxf · mov · flac · ogg · mts · mkv · avi</div>
  </div>
  <div style="margin-top:12px;display:flex;align-items:center;gap:10px;">
    <label>Language</label>
    <select id="lang-select">
{_lang_options()}
    </select>
  </div>
  <div id="up">
    <div class="plbl"><span id="ul">Uploading…</span><span id="up-pct">0%</span></div>
    <div class="pw"><div class="pb" id="pb"></div></div>
  </div>
  <section id="js">
    <div class="sh">Jobs</div>
    <div id="jl"><div class="empty">No transcriptions in this session yet</div></div>
  </section>
</main>
{_footer()}
<script>
const dz=document.getElementById('dz'),fi=document.getElementById('fi'),
      jl=document.getElementById('jl'),upDiv=document.getElementById('up'),
      pb=document.getElementById('pb'),upPct=document.getElementById('up-pct'),
      ulbl=document.getElementById('ul');
const STEPS=['TC read','Model','Transcribe','Align','Diarize'];
const jobs={{}};

dz.addEventListener('dragover',e=>{{e.preventDefault();dz.classList.add('over');}});
dz.addEventListener('dragleave',()=>dz.classList.remove('over'));
dz.addEventListener('drop',e=>{{e.preventDefault();dz.classList.remove('over');if(e.dataTransfer.files.length)upload(e.dataTransfer.files[0]);}});
fi.addEventListener('change',()=>{{if(fi.files.length)upload(fi.files[0]);fi.value='';}});

function upload(file){{
  upDiv.classList.add('on');pb.style.width='0%';upPct.textContent='0%';
  ulbl.textContent=file.name+' — uploading…';
  const lang=document.getElementById('lang-select').value;
  const fd=new FormData();fd.append('file',file);
  const xhr=new XMLHttpRequest();
  xhr.upload.addEventListener('progress',e=>{{if(e.lengthComputable){{const p=Math.round(e.loaded/e.total*100);pb.style.width=p+'%';upPct.textContent=p+'%';}}}});
  xhr.addEventListener('load',()=>{{
    upDiv.classList.remove('on');
    let d;try{{d=JSON.parse(xhr.responseText);}}catch(e){{return;}}
    if(d.error){{alert('Error: '+d.error);return;}}
    addRow(d.job_id,d.file);poll(d.job_id);
  }});
  xhr.addEventListener('error',()=>{{upDiv.classList.remove('on');alert('Upload failed.');}});
  xhr.open('POST','/transcribe?language='+lang);xhr.send(fd);
}}

function deleteJob(id){{
  if(!confirm('Delete this job?'))return;
  fetch('/api/jobs/'+id,{{method:'DELETE'}}).then(()=>{{
    const el=document.querySelector(`[data-id="${{id}}"]`);
    if(el)el.remove();
    if(!document.querySelector('.job'))jl.innerHTML='<div class="empty">No transcriptions in this session yet</div>';
  }});
}}

function addRow(id,name){{
  const e=jl.querySelector('.empty');if(e)e.remove();
  const el=document.createElement('div');
  el.className='job s-queued';el.dataset.id=id;
  el.innerHTML=`<div class="jh">
    <span class="jn" title="${{name}}">${{name}}</span>
    <span class="badge"><span class="dot"></span><span class="bt">queued</span></span>
    <div style="display:flex;gap:6px;align-items:center;">
      <a class="dl hidden" href="/download/${{id}}" download>TXT ↓</a>
      <button class="delbtn" onclick="deleteJob('${{id}}')">✕</button>
    </div>
  </div>
  <div class="jp">
    <div class="step-pips">${{STEPS.map((_,i)=>`<div class="pip" data-i="${{i}}"></div>`).join('')}}</div>
    <div class="slbls">${{STEPS.map(s=>`<div class="sl">${{s}}</div>`).join('')}}</div>
    <div class="jmsg">Waiting for worker…</div>
  </div>`;
  jl.prepend(el);jobs[id]=el;
}}

function steps(el,step,msg){{
  el.querySelectorAll('.pip').forEach((p,i)=>{{p.classList.remove('done','active');if(i<step-1)p.classList.add('done');else if(i===step-1)p.classList.add('active');}});
  el.querySelectorAll('.sl').forEach((l,i)=>{{l.classList.remove('done','active');if(i<step-1)l.classList.add('done');else if(i===step-1)l.classList.add('active');}});
  el.querySelector('.jmsg').textContent=msg||'';
}}

const SL={{queued:'queued',running:'running…',done:'done',error:'error'}};
async function poll(id){{
  const el=jobs[id];if(!el)return;
  let d;try{{const r=await fetch('/status/'+id);d=await r.json();}}catch(e){{setTimeout(()=>poll(id),3000);return;}}
  const s=d.status||'queued';
  el.className='job s-'+s;
  el.querySelector('.bt').textContent=SL[s]||s;
  if(s==='running'||s==='queued'){{steps(el,d.step||0,d.message||'');setTimeout(()=>poll(id),2000);}}
  else if(s==='done'){{steps(el,5,d.message||'Complete');el.querySelector('.dl').classList.remove('hidden');}}
  else if(s==='error'){{el.querySelector('.jmsg').textContent=d.error||'Unknown error';}}
}}
</script>
</body></html>"""


# ─── Queue page (/queue) ──────────────────────────────────────────────────────

def make_queue_html() -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{cfg.branding.organization} | {cfg.branding.queue_subtitle}</title>
<style>
  :root{{{_css_vars()}}}
  {_common_css()}
  main{{max-width:900px;margin:0 auto;padding:52px 40px;}}
  .summary{{display:flex;gap:2px;margin-bottom:32px;}}
  .sc{{flex:1;background:var(--surface);border:1px solid var(--border);border-radius:3px;padding:16px 20px;}}
  .sc .cnt{{font-family:var(--mono);font-size:28px;font-weight:500;line-height:1;margin-bottom:6px;}}
  .sc .lbl{{font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:.1em;text-transform:uppercase;}}
  .cr{{color:var(--amber);}} .cd{{color:var(--green);}} .ce{{color:var(--red);}} .cq{{color:var(--muted);}}
  .sh{{display:flex;justify-content:space-between;align-items:center;}}
  .rn{{font-size:9px;color:var(--border2);font-family:var(--mono);}}
  table{{width:100%;border-collapse:collapse;margin-top:2px;}}
  th{{font-family:var(--mono);font-size:9px;font-weight:500;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);padding:10px 12px;text-align:left;border-bottom:1px solid var(--border);}}
  td{{font-family:var(--mono);font-size:11px;color:var(--text);padding:11px 12px;border-bottom:1px solid var(--border);vertical-align:middle;}}
  tr:last-child td{{border-bottom:none;}}
  tr{{background:var(--surface);transition:background .1s;}} tr:hover{{background:var(--surface2);}}
  .br{{background:#2a2200;color:var(--amber);border:1px solid #4a3a00;}}
  .bd2{{background:#0a2a1a;color:var(--green);border:1px solid #1a4a2a;}}
  .be{{background:#2a0a0a;color:var(--red);border:1px solid #4a1a1a;}}
  .bq{{background:var(--surface2);color:var(--muted);border:1px solid var(--border);}}
  .br .dot{{background:var(--amber);animation:blink 1s ease-in-out infinite;}}
  .bd2 .dot{{background:var(--green);}} .be .dot{{background:var(--red);}} .bq .dot{{background:var(--muted);}}
  .pips{{display:flex;gap:2px;width:80px;}}
  .pip{{flex:1;height:3px;background:var(--border);border-radius:1px;}}
  .pip.done{{background:var(--amber);}} .pip.active{{background:var(--amber);opacity:.5;animation:pp 1.2s ease-in-out infinite;}}
  .mc{{color:var(--muted);font-size:10px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
  .tc{{color:var(--border2);font-size:10px;white-space:nowrap;}}
  .src{{font-size:9px;letter-spacing:.05em;padding:1px 5px;border-radius:2px;}}
  .src-upload{{background:#1a1a2a;color:#8888cc;border:1px solid #2a2a4a;}}
  .src-watchfolder{{background:#1a2a1a;color:#88cc88;border:1px solid #2a4a2a;}}
  .empty{{font-family:var(--mono);font-size:11px;color:var(--border2);text-align:center;padding:40px 0;border:1px solid var(--border);border-radius:3px;}}
  .qbtn{{font-family:var(--mono);font-size:10px;letter-spacing:.06em;background:transparent;border:1px solid var(--border);color:var(--muted);padding:3px 10px;border-radius:2px;cursor:pointer;transition:color .15s,border-color .15s;}}
  .qbtn:hover{{color:var(--red);border-color:var(--red);}}
  .qbtn-del{{color:var(--muted);font-size:10px;background:transparent;border:none;cursor:pointer;padding:2px 6px;border-radius:2px;transition:color .15s;}}
  .qbtn-del:hover{{color:var(--red);}}
</style>
</head>
<body>
{_nav("queue")}
<main>
  <div class="summary">
    <div class="sc"><div class="cnt cr" id="cr">—</div><div class="lbl">Running</div></div>
    <div class="sc"><div class="cnt cq" id="cq">—</div><div class="lbl">Queued</div></div>
    <div class="sc"><div class="cnt cd" id="cd">—</div><div class="lbl">Done</div></div>
    <div class="sc"><div class="cnt ce" id="ce">—</div><div class="lbl">Errors</div></div>
  </div>
  <div class="sh" style="padding-bottom:12px;border-bottom:1px solid var(--border);margin-bottom:2px;">
    <span style="font-family:var(--mono);font-size:10px;font-weight:500;letter-spacing:.15em;text-transform:uppercase;color:var(--muted);">All jobs</span>
    <div style="display:flex;gap:6px;align-items:center;">
      <span class="rn" id="ts">—</span>
      <button class="qbtn" onclick="clearDone()">Clear done</button>
      <button class="qbtn" onclick="clearAll()">Clear all</button>
    </div>
  </div>
  <div id="tw"><div class="empty">No jobs yet</div></div>
</main>
{_footer()}
<script>
const STEPS=['TC','Model','STT','Align','Diarize'];

function pips(step){{
  return '<div class="pips">'+STEPS.map((_,i)=>{{
    let c='pip';if(i<step-1)c+=' done';else if(i===step-1)c+=' active';
    return `<div class="${{c}}"></div>`;
  }}).join('')+'</div>';
}}

function badge(s){{
  const m={{running:'running…',done:'done',error:'error',queued:'queued'}};
  const c={{running:'br',done:'bd2',error:'be',queued:'bq'}};
  return `<span class="badge ${{c[s]||'bq'}}"><span class="dot"></span>${{m[s]||s}}</span>`;
}}

function srcBadge(s){{return `<span class="src src-${{s}}">${{s}}</span>`;}}
function ft(iso){{if(!iso)return'—';return new Date(iso).toLocaleTimeString('en-GB',{{hour:'2-digit',minute:'2-digit',second:'2-digit'}});}}

async function clearDone(){{
  if(!confirm('Delete all done and error jobs?'))return;
  await fetch('/api/jobs?status=done',{{method:'DELETE'}});
  await fetch('/api/jobs?status=error',{{method:'DELETE'}});
  refresh();
}}

async function clearAll(){{
  if(!confirm('Delete ALL jobs (except running)?'))return;
  await fetch('/api/jobs',{{method:'DELETE'}});
  refresh();
}}

async function retryJob(id){{
  const r=await fetch('/api/jobs/'+id+'/retry',{{method:'POST'}});
  const d=await r.json();
  if(d.error){{alert(d.error);return;}}
  refresh();
}}

async function delJob(id){{await fetch('/api/jobs/'+id,{{method:'DELETE'}});refresh();}}

async function refresh(){{
  let jobs;
  try{{const r=await fetch('/api/queue');jobs=await r.json();}}catch(e){{return;}}
  const c={{running:0,queued:0,done:0,error:0}};
  jobs.forEach(j=>{{if(c[j.status]!==undefined)c[j.status]++;}});
  document.getElementById('cr').textContent=c.running;
  document.getElementById('cq').textContent=c.queued;
  document.getElementById('cd').textContent=c.done;
  document.getElementById('ce').textContent=c.error;
  document.getElementById('ts').textContent='refreshed '+new Date().toLocaleTimeString('en-GB');
  const tw=document.getElementById('tw');
  if(!jobs.length){{tw.innerHTML='<div class="empty">No jobs yet</div>';return;}}
  const rows=jobs.map(j=>`<tr>
    <td title="${{j.filename}}">${{j.filename.length>38?j.filename.slice(0,36)+'…':j.filename}}</td>
    <td>${{srcBadge(j.source)}}</td>
    <td>${{badge(j.status)}}</td>
    <td>${{(j.status==='running'||j.status==='queued')?pips(j.step):''}}</td>
    <td class="mc">${{j.error||j.message||''}}</td>
    <td class="tc">${{ft(j.updated_at)}}</td>
    <td>${{j.status!=='running'?`
      ${{j.status==='done'?`<a class="qbtn-del" href="/download/${{j.id}}" download title="Download transcript" style="margin-right:4px;text-decoration:none">TXT ↓</a>`:''}}
      ${{j.status==='error'?`<button class="qbtn-del" onclick="retryJob('${{j.id}}')" title="Retry" style="margin-right:4px">↺</button>`:''}}
      <button class="qbtn-del" onclick="delJob('${{j.id}}')" title="Delete">✕</button>
    `:''}}</td>
  </tr>`).join('');
  tw.innerHTML=`<table><thead><tr>
    <th>File</th><th>Source</th><th>Status</th><th>Progress</th><th>Message</th><th>Updated</th><th></th>
  </tr></thead><tbody>${{rows}}</tbody></table>`;
}}

refresh();
setInterval(refresh,3000);
</script>
</body></html>"""


# ─── System status page (/system) ────────────────────────────────────────────

def make_system_html() -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{cfg.branding.organization} | {cfg.branding.system_subtitle}</title>
<style>
  :root{{{_css_vars()}}}
  {_common_css()}
  main{{max-width:860px;margin:0 auto;padding:52px 40px;}}
  .grid{{display:grid;grid-template-columns:1fr 1fr;gap:2px;margin-bottom:2px;}}
  .card{{background:var(--surface);border:1px solid var(--border);border-radius:3px;padding:20px 24px;margin-bottom:2px;}}
  .card-title{{font-family:var(--mono);font-size:10px;font-weight:500;letter-spacing:.15em;text-transform:uppercase;color:var(--muted);margin-bottom:16px;padding-bottom:10px;border-bottom:1px solid var(--border);}}
  .svc-row{{display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border);}}
  .svc-row:last-child{{border-bottom:none;}}
  .svc-name{{font-family:var(--mono);font-size:12px;color:var(--text);}}
  .svc-badge{{font-family:var(--mono);font-size:10px;padding:2px 8px;border-radius:2px;display:flex;align-items:center;gap:5px;}}
  .active{{background:#0a2a1a;color:var(--green);border:1px solid #1a4a2a;}}
  .inactive{{background:#2a0a0a;color:var(--red);border:1px solid #4a1a1a;}}
  .unknown{{background:var(--surface2);color:var(--muted);border:1px solid var(--border);}}
  .active .svc-dot{{background:var(--green);}} .inactive .svc-dot{{background:var(--red);}} .unknown .svc-dot{{background:var(--muted);}}
  .svc-dot{{width:5px;height:5px;border-radius:50%;display:inline-block;}}
  .stat-row{{display:flex;align-items:baseline;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border);}}
  .stat-row:last-child{{border-bottom:none;}}
  .stat-label{{font-family:var(--mono);font-size:11px;color:var(--muted);}}
  .stat-val{{font-family:var(--mono);font-size:13px;color:var(--text);}}
  .vram-bar-wrap{{margin-top:12px;height:3px;background:var(--border);border-radius:2px;overflow:hidden;}}
  .vram-bar{{height:100%;border-radius:2px;transition:width .5s;background:var(--amber);}}
  .vram-bar.hot{{background:var(--red);}}
  .q-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:2px;}}
  .q-card{{background:var(--surface2);border:1px solid var(--border);border-radius:2px;padding:12px 16px;text-align:center;}}
  .q-count{{font-family:var(--mono);font-size:24px;font-weight:500;line-height:1;margin-bottom:5px;}}
  .q-label{{font-family:var(--mono);font-size:9px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);}}
  .q-running{{color:var(--amber);}} .q-queued{{color:var(--muted);}} .q-done{{color:var(--green);}} .q-error{{color:var(--red);}}
  .refresh-note{{font-family:var(--mono);font-size:10px;color:var(--border2);text-align:right;margin-top:16px;}}
</style>
</head>
<body>
{_nav("system")}
<main>
  <div id="update-banner" style="display:none;background:var(--amber-bg,#fdf3e0);border:1px solid #e0b060;border-radius:6px;padding:14px 20px;margin-bottom:2px;">
    <div style="display:flex;align-items:center;justify-content:space-between;">
      <div>
        <span style="font-family:var(--mono);font-size:12px;font-weight:600;color:var(--amber,#92600a);">⬆ Update available</span>
        <span id="update-version" style="font-family:var(--mono);font-size:11px;color:var(--muted);margin-left:10px;"></span>
      </div>
      <a id="update-link" href="#" target="_blank" style="font-family:var(--mono);font-size:10px;color:var(--amber,#92600a);text-decoration:none;border:1px solid #e0b060;padding:3px 10px;border-radius:3px;">View release ↗</a>
    </div>
    <div id="update-notes" style="font-family:var(--mono);font-size:10px;color:var(--muted);margin-top:8px;line-height:1.6;white-space:pre-wrap;"></div>
    <div style="font-family:var(--mono);font-size:10px;color:var(--muted);margin-top:8px;">
      Run on server: <code style="background:rgba(0,0,0,.06);padding:2px 6px;border-radius:3px;">sudo /opt/transcription/update.sh</code>
    </div>
  </div>
    <div class="card">
      <div class="card-title">Services</div>
      <div id="svc-list">
        <div class="svc-row"><span class="svc-name">Worker (GPU)</span><span class="svc-badge unknown"><span class="svc-dot"></span>—</span></div>
        <div class="svc-row"><span class="svc-name">Watchfolder</span><span class="svc-badge unknown"><span class="svc-dot"></span>—</span></div>
        <div class="svc-row"><span class="svc-name">Web GUI</span><span class="svc-badge unknown"><span class="svc-dot"></span>—</span></div>
      </div>
    </div>
    <div class="card">
      <div class="card-title">GPU</div>
      <div id="gpu-info"><div class="stat-row"><span class="stat-label">Loading…</span></div></div>
    </div>
  </div>
  <div class="card">
    <div class="card-title">Queue</div>
    <div class="q-grid">
      <div class="q-card"><div class="q-count q-running" id="q-running">—</div><div class="q-label">Running</div></div>
      <div class="q-card"><div class="q-count q-queued"  id="q-queued">—</div><div class="q-label">Queued</div></div>
      <div class="q-card"><div class="q-count q-done"    id="q-done">—</div><div class="q-label">Done</div></div>
      <div class="q-card"><div class="q-count q-error"   id="q-error">—</div><div class="q-label">Errors</div></div>
    </div>
  </div>
  <div class="card">
    <div class="card-title">Disk (Output)</div>
    <div id="disk-info"><div class="stat-row"><span class="stat-label">Loading…</span></div></div>
  </div>
  <div class="refresh-note" id="rn">—</div>
</main>
{_footer()}
<script>
const SVC_NAMES={{worker:'Worker (GPU)',watchfolder:'Watchfolder',webgui:'Web GUI'}};

async function refresh(){{
  let d;try{{const r=await fetch('/api/system');d=await r.json();}}catch(e){{return;}}

  document.getElementById('svc-list').innerHTML=Object.entries(d.services).map(([k,v])=>{{
    const cls=v==='active'?'active':v==='inactive'?'inactive':'unknown';
    return `<div class="svc-row"><span class="svc-name">${{SVC_NAMES[k]||k}}</span><span class="svc-badge ${{cls}}"><span class="svc-dot"></span>${{v}}</span></div>`;
  }}).join('');

  const gpu=d.gpu,gpuEl=document.getElementById('gpu-info');
  if(gpu.error){{gpuEl.innerHTML=`<div class="stat-row"><span class="stat-label">${{gpu.error}}</span></div>`;}}
  else{{
    const pct=Math.round(gpu.vram_used/gpu.vram_total*100),hot=pct>85;
    gpuEl.innerHTML=`
      <div class="stat-row"><span class="stat-label">Model</span><span class="stat-val">${{gpu.name}}</span></div>
      <div class="stat-row"><span class="stat-label">VRAM</span><span class="stat-val">${{gpu.vram_used}} / ${{gpu.vram_total}} MB (${{pct}}%)</span></div>
      <div class="stat-row"><span class="stat-label">GPU util</span><span class="stat-val">${{gpu.utilization}}%</span></div>
      <div class="stat-row"><span class="stat-label">Temp</span><span class="stat-val">${{gpu.temperature}} °C</span></div>
      <div class="vram-bar-wrap"><div class="vram-bar ${{hot?'hot':''}}" style="width:${{pct}}%"></div></div>`;
  }}

  document.getElementById('q-running').textContent=d.queue.running;
  document.getElementById('q-queued').textContent=d.queue.queued;
  document.getElementById('q-done').textContent=d.queue.done;
  document.getElementById('q-error').textContent=d.queue.error;

  const disk=d.disk,diskEl=document.getElementById('disk-info');
  if(disk.error){{diskEl.innerHTML=`<div class="stat-row"><span class="stat-label">${{disk.error}}</span></div>`;}}
  else{{
    const dpct=Math.round(disk.used_gb/disk.total_gb*100);
    diskEl.innerHTML=`
      <div class="stat-row"><span class="stat-label">Used</span><span class="stat-val">${{disk.used_gb}} GB / ${{disk.total_gb}} GB (${{dpct}}%)</span></div>
      <div class="stat-row"><span class="stat-label">Free</span><span class="stat-val">${{disk.free_gb}} GB</span></div>`;
  }}

  document.getElementById('rn').textContent='v'+d.version+' · Refreshed '+new Date().toLocaleTimeString('en-GB');

  // Update banner
  const upd = d.update || {{}};
  const banner = document.getElementById('update-banner');
  if(banner){{
    if(upd.available){{
      banner.style.display='block';
      document.getElementById('update-version').textContent=
        d.version+' → '+upd.latest+' ('+upd.release_date+')';
      const link = document.getElementById('update-link');
      if(link) link.href = upd.release_url || '#';
      const notes = document.getElementById('update-notes');
      if(notes) notes.textContent = (upd.release_notes||'').trim().slice(0,300);
    }} else {{
      banner.style.display='none';
    }}
  }}
}}

refresh();
setInterval(refresh,5000);
</script>
</body></html>"""


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return make_upload_html()


@app.get("/queue", response_class=HTMLResponse)
async def queue_page():
    return make_queue_html()


@app.get("/system", response_class=HTMLResponse)
async def system_page():
    return make_system_html()