"""Server-rendered PUBLIC music showcase page (no auth — social-shareable).

Reads ``index.json`` from the showcase directory and emits a single
self-contained HTML page: responsive cards, one-at-a-time <audio> players with
an animated equalizer, genre/instrument/family filters, and Open Graph +
Twitter card metadata so it previews nicely when shared. Audio is served from
the same /api/public/music path (also unauthenticated).
"""

import html
import json
import os


def load_clips(showcase_dir):
    path = os.path.join(showcase_dir, "index.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("clips", [])
    except Exception:
        return []


def _card(c):
    kind = c.get("kind", "")
    if kind == "genre":
        meta = f"{html.escape(str(c.get('key','')))} · {c.get('tempo','?')} BPM · {c.get('structure','')}"
        lanes = c.get("lanes", [])
        tags = "".join(
            f'<span class="tag">{html.escape(l)}</span>' for l in lanes)
        sub = c.get("desc", "")
    else:
        meta = f"{html.escape(str(c.get('family','')))} timbre"
        tags = ""
        sub = "Same phrase, different voice."
    filec = html.escape(c.get("file", ""))
    title = html.escape(c.get("title", ""))
    return f"""<article class="card" data-kind="{kind}" data-family="{html.escape(c.get('family',''))}" data-title="{title.lower()}">
  <div class="eq" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></div>
  <h3>{title}</h3>
  <p class="meta">{meta}</p>
  <div class="tags">{tags}</div>
  <p class="sub">{html.escape(sub)}</p>
  <audio controls preload="none" src="/api/public/music/{filec}"></audio>
  <div class="row">
    <a class="dl" href="/api/public/music/{filec}" download="{filec}">Download</a>
    <button class="copy" type="button" data-url="/api/public/music/{filec}">Copy link</button>
  </div>
</article>"""


_CSS = """
:root{--bg0:#0b0f1a;--bg1:#131a2b;--fg:#e7ecf5;--mut:#93a1bd;--acc:#7c5cff;--acc2:#22d3ee;--card:#161f36;--line:#26314f}
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Inter;color:var(--fg);
 background:radial-gradient(1200px 600px at 12% -8%,#1b2444 0,transparent 60%),
            radial-gradient(1000px 500px at 100% 0,#241b44 0,transparent 55%),var(--bg0);min-height:100vh}
header{padding:56px 20px 24px;text-align:center;max-width:900px;margin:0 auto}
.badge{display:inline-block;font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--acc2);
 border:1px solid var(--line);padding:5px 12px;border-radius:999px;background:#0f1830}
h1{font-size:clamp(30px,6vw,52px);margin:16px 0 8px;font-weight:800;letter-spacing:-.02em;
 background:linear-gradient(90deg,#fff,#b9a8ff 55%,#7ff0ff);-webkit-background-clip:text;background-clip:text;color:transparent}
.lede{color:var(--mut);font-size:clamp(15px,2.4vw,18px);max-width:640px;margin:0 auto}
.toolbar{position:sticky;top:0;z-index:5;display:flex;flex-wrap:wrap;gap:8px;justify-content:center;align-items:center;
 padding:14px 16px;background:rgba(11,15,26,.82);backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}
.chip{border:1px solid var(--line);background:#0f1830;color:var(--mut);padding:7px 14px;border-radius:999px;
 font-size:13px;cursor:pointer;transition:.15s}
.chip:hover{color:var(--fg);border-color:var(--acc)}
.chip.active{background:linear-gradient(90deg,var(--acc),#5b8cff);color:#fff;border-color:transparent}
input.search{margin-left:8px;background:#0f1830;border:1px solid var(--line);color:var(--fg);
 padding:8px 12px;border-radius:10px;min-width:180px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px;max-width:1120px;
 margin:26px auto 80px;padding:0 20px}
.card{position:relative;background:linear-gradient(180deg,var(--card),#111a30);border:1px solid var(--line);
 border-radius:16px;padding:18px;overflow:hidden;transition:.18s}
.card:hover{transform:translateY(-3px);border-color:#3a497a;box-shadow:0 12px 40px rgba(0,0,0,.4)}
.card.hidden{display:none}
h3{margin:2px 34px 4px 0;font-size:17px}
.meta{color:var(--mut);font-size:12.5px;margin:0 0 10px}
.sub{color:#7f8db0;font-size:12px;margin:0 0 10px;min-height:28px}
.tags{display:flex;flex-wrap:wrap;gap:5px;margin:0 0 12px}
.tag{font-size:10.5px;letter-spacing:.02em;color:#c9d3ee;background:#1c2748;border:1px solid #2b3a63;
 padding:2px 7px;border-radius:6px}
audio{width:100%;height:38px}
.row{display:flex;gap:8px;margin-top:12px}
.dl,.copy{flex:1;text-align:center;text-decoration:none;font-size:13px;padding:8px;border-radius:10px;
 border:1px solid var(--line);color:var(--fg);background:#0f1830;cursor:pointer;transition:.15s}
.dl:hover,.copy:hover{border-color:var(--acc);color:#fff}
.eq{position:absolute;top:14px;right:14px;display:flex;gap:2px;align-items:flex-end;height:20px;opacity:0;transition:.2s}
.playing .eq{opacity:1}
.eq i{width:3px;height:6px;background:linear-gradient(var(--acc2),var(--acc));border-radius:2px;animation:eq .8s ease-in-out infinite}
.eq i:nth-child(2){animation-delay:.1s}.eq i:nth-child(3){animation-delay:.2s}.eq i:nth-child(4){animation-delay:.3s}.eq i:nth-child(5){animation-delay:.15s}
@keyframes eq{0%,100%{height:5px}50%{height:18px}}
footer{text-align:center;color:#5b6a8f;font-size:12.5px;padding:30px 20px 60px}
a.p{color:var(--acc2)}
"""

_JS = """
const chips=[...document.querySelectorAll('.chip[data-filter]')];
let mode='all';
function apply(){
  const q=(document.getElementById('q').value||'').toLowerCase();
  document.querySelectorAll('.card').forEach(c=>{
    const k=c.dataset.kind, fam=c.dataset.family, t=c.dataset.title;
    let ok = mode==='all' || (mode==='genre'&&k==='genre') || (mode==='instrument'&&k==='instrument') || (mode.startsWith('f:')&&fam===mode.slice(2));
    if(ok && q) ok = t.includes(q);
    c.classList.toggle('hidden', !ok);
  });
  updateCount();
}
function updateCount(){const n=[...document.querySelectorAll('.card:not(.hidden)')].length;
  document.getElementById('count').textContent = n + ' track' + (n===1?'':'s');}
chips.forEach(b=>b.onclick=()=>{chips.forEach(x=>x.classList.remove('active'));b.classList.add('active');mode=b.dataset.filter;apply();});
document.getElementById('q').addEventListener('input',apply);
// one-at-a-time + highlight
let cur=null;
document.querySelectorAll('audio').forEach(a=>{
  a.addEventListener('play',()=>{ if(cur&&cur!==a)cur.pause(); cur=a; a.closest('.card').classList.add('playing'); });
  a.addEventListener('pause',()=>a.closest('.card').classList.remove('playing'));
  a.addEventListener('ended',()=>a.closest('.card').classList.remove('playing'));
});
document.querySelectorAll('.copy').forEach(b=>b.onclick=async()=>{
  const url=location.origin+b.dataset.url; try{await navigator.clipboard.writeText(url);b.textContent='Copied!';setTimeout(()=>b.textContent='Copy link',1400);}catch{prompt('Copy',url)}
});
apply();
"""


def build_page(showcase_dir, site_origin="", seed=7):
    clips = load_clips(showcase_dir)
    n_genre = sum(1 for c in clips if c.get("kind") == "genre")
    n_inst = sum(1 for c in clips if c.get("kind") == "instrument")
    fams = sorted({c.get("family") for c in clips if c.get("kind") == "instrument" and c.get("family")})
    cards = "\n".join(_card(c) for c in clips)
    fam_chips = "".join(
        f'<button class="chip" data-filter="f:{html.escape(f)}">{html.escape(f)}</button>' for f in fams)
    origin = site_origin.rstrip("/")
    share_url = f"{origin}/api/public/music/showcase" if origin else "/api/public/music/showcase"
    og = f"""<meta property="og:type" content="website">
 <meta property="og:title" content="AI Music Showcase — every genre & instrument">
 <meta property="og:description" content="{n_genre} genres + {n_inst} instruments, composed and rendered entirely on-device. Tap to listen.">
 <meta property="og:url" content="{html.escape(share_url)}">
 <meta property="og:site_name" content="Local AI">
 <meta name="twitter:card" content="summary_large_image">
 <meta name="twitter:title" content="AI Music Showcase">
 <meta name="twitter:description" content="{n_genre} genres + {n_inst} instruments generated on-device. Tap to listen.">"""
    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Music Showcase — every genre &amp; instrument</title>
{og}
<style>{_CSS}</style></head>
<body>
<header>
  <span class="badge">on-device generative music</span>
  <h1>The Music Machine</h1>
  <p class="lede">Every clip here is composed from a score DSL and rendered to audio
  locally — no cloud. {n_genre} genre pieces (full band: melody, harmony, bass, drums,
  song form &amp; dynamics) and {n_inst} instrument timbres. Press play.</p>
</header>
<div class="toolbar">
  <button class="chip active" data-filter="all">All <span id="count"></span></button>
  <button class="chip" data-filter="genre">Genres</button>
  <button class="chip" data-filter="instrument">Instruments</button>
  {fam_chips}
  <input class="search" id="q" placeholder="search…" autocomplete="off">
</div>
<main class="grid">{cards}</main>
<footer>Generated &amp; rendered on-device · FluidSynth + GM soundfont&nbsp;banks ·
 <a class="p" href="/">back to the app</a></footer>
<script>{_JS}</script>
</body></html>"""
