# =============================================================================
# EggyGrab — single-file YouTube downloader (serves its own UI + does the work)
#
# DEPLOY ON RENDER
#   1. Put this file + a requirements.txt in a repo. requirements.txt:
#         flask
#         yt-dlp
#         imageio-ffmpeg
#         gunicorn
#   2. New Web Service on Render -> connect the repo.
#         Build command:  pip install -r requirements.txt
#         Start command:  gunicorn app:app --timeout 600
#   3. Open the Render URL. Done. (No Cloudflare needed — this file IS the
#      frontend too. If you later want the UI on Cloudflare, deploy preview.html
#      there and point its fetch() calls at this Render URL.)
#
# IF YOUTUBE BLOCKS THE SERVER ("Sign in to confirm you're not a bot"):
#   Export cookies.txt from a logged-in browser (use a "Get cookies.txt"
#   extension), commit it next to this file, and it'll be picked up
#   automatically. Refresh it when it stops working.
#
# KEEP IT ALIVE: run `pip install -U yt-dlp` and redeploy whenever YouTube
# changes and downloads start failing.
# =============================================================================

import os
import re
import shutil
import tempfile

from flask import Flask, request, jsonify, Response, render_template_string
import yt_dlp
import imageio_ffmpeg

app = Flask(__name__)

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
COOKIES = "cookies.txt" if os.path.exists("cookies.txt") else None

# label -> max height fed to yt-dlp
QUALITY_MAP = {"2K": 1440, "1080p": 1080, "720p": 720, "480p": 480}
QUALITY_TAG = {"2K": "1440p", "1080p": "Full HD", "720p": "HD", "480p": "SD"}


def base_opts():
    opts = {"quiet": True, "no_warnings": True, "ffmpeg_location": FFMPEG}
    if COOKIES:
        opts["cookiefile"] = COOKIES
    return opts


def human_size(num):
    if not num:
        return ""
    for unit in ["B", "KB", "MB", "GB"]:
        if num < 1024:
            return f"~{num:.0f} {unit}" if unit != "B" else f"~{num} B"
        num /= 1024
    return f"~{num:.1f} TB"


def safe_name(text):
    return re.sub(r'[\\/*?:"<>|]', "", text)[:80].strip() or "video"


@app.route("/")
def home():
    return render_template_string(PAGE)


@app.route("/info", methods=["POST"])
def info():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify(error="Paste a YouTube link first."), 400
    try:
        with yt_dlp.YoutubeDL(base_opts()) as ydl:
            data = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify(error=clean_error(str(e))), 502

    heights = [f.get("height") or 0 for f in data.get("formats", [])]
    max_h = max(heights) if heights else 0

    qualities = []
    for label, h in QUALITY_MAP.items():
        if max_h >= h:
            # rough size estimate from the best video format at/under this height
            est = best_filesize(data.get("formats", []), h)
            qualities.append({"label": label, "tag": QUALITY_TAG[label],
                              "size": human_size(est)})
    if not qualities:  # very low-res source — still offer the max available
        qualities.append({"label": "480p", "tag": "SD", "size": ""})

    dur = data.get("duration") or 0
    return jsonify(
        title=data.get("title", "Untitled"),
        channel=data.get("uploader", ""),
        duration=f"{dur // 60}:{dur % 60:02d}" if dur else "",
        thumbnail=data.get("thumbnail", ""),
        qualities=qualities,
    )


def best_filesize(formats, max_h):
    best = 0
    audio = max((f.get("filesize") or f.get("filesize_approx") or 0)
                for f in formats if f.get("vcodec") == "none") if formats else 0
    for f in formats:
        h = f.get("height") or 0
        if 0 < h <= max_h:
            v = f.get("filesize") or f.get("filesize_approx") or 0
            if v > best:
                best = v
    return (best + audio) if best else 0


@app.route("/download")
def download():
    url = request.args.get("url", "").strip()
    label = request.args.get("quality", "720p")
    if not url:
        return "Missing url", 400
    max_h = QUALITY_MAP.get(label, 720)

    tmp = tempfile.mkdtemp(prefix="eggygrab_")
    out_tmpl = os.path.join(tmp, "%(title)s.%(ext)s")
    opts = base_opts()
    opts.update({
        "format": f"bestvideo[height<={max_h}]+bestaudio/best[height<={max_h}]/best",
        "merge_output_format": "mp4",
        "outtmpl": out_tmpl,
        "noplaylist": True,
    })

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=True)
        title = safe_name(data.get("title", "video"))
        files = [f for f in os.listdir(tmp)]
        if not files:
            raise RuntimeError("Download produced no file.")
        path = os.path.join(tmp, files[0])
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        return clean_error(str(e)), 502

    def stream_and_cleanup():
        try:
            with open(path, "rb") as fh:
                while chunk := fh.read(262144):
                    yield chunk
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    fname = f"{title} [{label}].mp4"
    return Response(
        stream_and_cleanup(),
        mimetype="video/mp4",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


def clean_error(msg):
    msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)  # strip ANSI colour codes
    if "Sign in to confirm" in msg or "bot" in msg.lower():
        return ("YouTube blocked this server (bot check). Add a cookies.txt "
                "file next to app.py — see the notes at the top of the file.")
    if "Video unavailable" in msg:
        return "That video is unavailable, private, or region-locked."
    return msg.split("\n")[0][:200]


# ---------------------------------------------------------------------------
# Frontend (same look as preview.html, wired to the endpoints above)
# ---------------------------------------------------------------------------
PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>EggyGrab</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet" />
<style>
  :root{--bg:#0B0608;--card:#190E12;--border:#341A20;--maroon:#7B1226;
    --maroon-bright:#A81D3A;--glow:rgba(168,29,58,.35);--text:#F3E9EB;
    --muted:#9E8086;--radius:14px;}
  *{box-sizing:border-box;margin:0;padding:0;}
  body{min-height:100dvh;background:radial-gradient(900px 500px at 50% -10%,rgba(123,18,38,.22),transparent 70%),var(--bg);
    color:var(--text);font-family:"Inter",system-ui,sans-serif;display:flex;flex-direction:column;align-items:center;padding:28px 18px 60px;}
  .wordmark{font-family:"Space Grotesk",sans-serif;font-weight:700;letter-spacing:-.5px;font-size:18px;display:flex;align-items:center;gap:9px;margin-bottom:52px;}
  .dot{width:11px;height:11px;border-radius:50%;background:var(--maroon-bright);box-shadow:0 0 14px var(--glow);}
  .wordmark span{color:var(--muted);font-weight:500;}
  .stage{width:100%;max-width:560px;}
  h1{font-family:"Space Grotesk",sans-serif;font-size:clamp(30px,7vw,46px);font-weight:600;line-height:1.05;letter-spacing:-1.2px;text-align:center;margin-bottom:10px;}
  h1 em{font-style:normal;color:var(--maroon-bright);}
  .sub{text-align:center;color:var(--muted);font-size:15px;margin-bottom:30px;}
  .bar{display:flex;gap:9px;background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:7px;transition:border-color .18s,box-shadow .18s;}
  .bar:focus-within{border-color:var(--maroon-bright);box-shadow:0 0 0 4px var(--glow);}
  .bar input{flex:1;background:transparent;border:none;outline:none;color:var(--text);font-size:15px;padding:12px;min-width:0;}
  .bar input::placeholder{color:#6c595d;}
  button.go{font-family:"Space Grotesk",sans-serif;font-weight:600;font-size:15px;color:#fff;background:var(--maroon);border:none;border-radius:9px;padding:0 22px;cursor:pointer;transition:background .15s,transform .05s;white-space:nowrap;}
  button.go:hover{background:var(--maroon-bright);}
  button.go:active{transform:scale(.98);}
  button.go:disabled{opacity:.6;cursor:default;}
  .hint{text-align:center;color:#6c595d;font-size:12.5px;margin-top:12px;min-height:18px;}
  .hint.err{color:#e06a82;}
  .result{margin-top:26px;opacity:0;transform:translateY(8px);}
  .result.show{animation:rise .35s ease forwards;}
  @keyframes rise{to{opacity:1;transform:none;}}
  .meta{display:flex;gap:14px;align-items:center;background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:14px;margin-bottom:14px;}
  .thumb{width:122px;height:70px;border-radius:9px;flex-shrink:0;object-fit:cover;background:linear-gradient(135deg,#2a1015,#5e0f20);}
  .meta-text{min-width:0;}
  .title{font-weight:600;font-size:15px;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
  .channel{color:var(--muted);font-size:13px;margin-top:4px;}
  .qrow{display:flex;align-items:center;justify-content:space-between;background:var(--card);border:1px solid var(--border);border-left:3px solid var(--maroon);border-radius:11px;padding:15px 16px;margin-bottom:9px;cursor:pointer;transition:transform .14s,border-color .14s,background .14s;}
  .qrow:hover{transform:translateX(4px);border-left-color:var(--maroon-bright);background:#20121a;}
  .qrow.busy{opacity:.55;cursor:default;transform:none;}
  .qleft{display:flex;align-items:baseline;gap:10px;}
  .qlabel{font-family:"Space Grotesk",sans-serif;font-weight:600;font-size:16px;}
  .qtag{font-size:11px;color:var(--maroon-bright);font-weight:600;border:1px solid var(--border);border-radius:20px;padding:2px 8px;}
  .qsize{font-family:"Space Grotesk",sans-serif;color:var(--muted);font-size:13px;}
  .dl{display:flex;align-items:center;gap:7px;color:var(--text);font-size:13px;font-weight:500;}
  .dl svg{width:17px;height:17px;stroke:var(--maroon-bright);}
  .spinner{width:16px;height:16px;border:2px solid var(--border);border-top-color:var(--maroon-bright);border-radius:50%;animation:spin .7s linear infinite;}
  @keyframes spin{to{transform:rotate(360deg);}}
  @media (prefers-reduced-motion:reduce){.result.show{animation:none;opacity:1;transform:none;}.qrow:hover{transform:none;}}
</style>
</head>
<body>
  <div class="wordmark"><span class="dot"></span>EggyGrab <span>/ video downloader</span></div>
  <div class="stage">
    <h1>Paste a link.<br><em>Pick a quality.</em></h1>
    <p class="sub">Drop any YouTube URL below and grab it in the resolution you want.</p>
    <div class="bar">
      <input id="url" type="text" placeholder="https://www.youtube.com/watch?v=..."
             onkeydown="if(event.key==='Enter')getInfo()" />
      <button class="go" id="go" onclick="getInfo()">Get</button>
    </div>
    <p class="hint" id="hint"></p>
    <div class="result" id="result">
      <div class="meta">
        <img class="thumb" id="thumb" alt="" />
        <div class="meta-text">
          <div class="title" id="title"></div>
          <div class="channel" id="channel"></div>
        </div>
      </div>
      <div id="rows"></div>
    </div>
  </div>
<script>
  const arrow=`<svg viewBox="0 0 24 24" fill="none" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v13"/><path d="m7 11 5 5 5-5"/><path d="M5 21h14"/></svg>`;
  let currentUrl="";

  async function getInfo(){
    const url=document.getElementById("url").value.trim();
    const go=document.getElementById("go"), hint=document.getElementById("hint");
    if(!url){setHint("Paste a YouTube link first.",true);return;}
    currentUrl=url;
    go.disabled=true; go.textContent="…"; setHint("Reading video…",false);
    document.getElementById("result").classList.remove("show");
    try{
      const r=await fetch("/info",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url})});
      const d=await r.json();
      if(!r.ok){setHint(d.error||"Couldn't read that video.",true);return;}
      render(d); setHint("",false);
    }catch(e){setHint("Network error. Try again.",true);}
    finally{go.disabled=false; go.textContent="Get";}
  }

  function render(d){
    document.getElementById("thumb").src=d.thumbnail||"";
    document.getElementById("title").textContent=d.title;
    document.getElementById("channel").textContent=[d.channel,d.duration].filter(Boolean).join(" · ");
    document.getElementById("rows").innerHTML=d.qualities.map(q=>`
      <div class="qrow" onclick="grab(this,'${q.label}')">
        <div class="qleft"><span class="qlabel">${q.label}</span><span class="qtag">${q.tag}</span></div>
        <div style="display:flex;align-items:center;gap:16px">
          <span class="qsize">${q.size||""}</span>
          <span class="dl" data-dl>${arrow} MP4</span>
        </div>
      </div>`).join("");
    const res=document.getElementById("result");
    res.classList.remove("show"); void res.offsetWidth; res.classList.add("show");
  }

  function grab(row,quality){
    if(row.classList.contains("busy"))return;
    row.classList.add("busy");
    const tag=row.querySelector("[data-dl]");
    tag.innerHTML=`<span class="spinner"></span> Preparing…`;
    // hidden navigation triggers the browser's own download
    const a=document.createElement("a");
    a.href="/download?url="+encodeURIComponent(currentUrl)+"&quality="+encodeURIComponent(quality);
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(()=>{row.classList.remove("busy");tag.innerHTML=`${arrow} MP4`;},9000);
  }

  function setHint(msg,isErr){const h=document.getElementById("hint");h.textContent=msg;h.classList.toggle("err",!!isErr);}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
