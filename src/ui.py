"""科技感界面：深色终端风主题、顶部状态栏与滚动快讯、HUD 指标卡、旋转的「人才星球」。

只被 app.py 使用（run.py 不会导入），改界面不影响正在跑的数据流水线。
"""
from __future__ import annotations

import html
import json
import math
import random
from datetime import datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import plotly.io as pio
import streamlit as st
import streamlit.components.v1 as components

NEON, CYAN, AMBER, RED, VIOLET = "#00F0A8", "#22D3EE", "#FBBF24", "#FF4D6D", "#A78BFA"
BG, PANEL, LINE, TEXT, DIM = "#04070D", "#0A111C", "#1B2A3D", "#C8D3E6", "#6B7C93"
MONO = '"JetBrains Mono","SF Mono","Menlo","Consolas","Roboto Mono",monospace'
SEQ = [NEON, CYAN, AMBER, RED, VIOLET, "#60A5FA", "#F472B6", "#34D399", "#F59E0B", "#94A3B8"]

CSS = f"""
<style>
:root {{ --bg:{BG}; --panel:{PANEL}; --line:{LINE}; --neon:{NEON}; --cyan:{CYAN}; --amber:{AMBER};
        --red:{RED}; --text:{TEXT}; --dim:{DIM}; --mono:{MONO}; }}
.stApp {{ background:
    radial-gradient(1100px 520px at 50% -8%, rgba(34,211,238,.10), transparent 60%),
    radial-gradient(900px 500px at 100% 100%, rgba(0,240,168,.05), transparent 60%), var(--bg); }}
.stApp::before {{ content:""; position:fixed; inset:0; pointer-events:none; z-index:0;
    background-image: linear-gradient(rgba(34,211,238,.035) 1px, transparent 1px),
                      linear-gradient(90deg, rgba(34,211,238,.035) 1px, transparent 1px);
    background-size: 28px 28px; }}
html, body, .stApp, .stMarkdown, p, li, label, input, textarea, button, [data-baseweb] {{
    font-family: var(--mono) !important; }}
.stApp, .stMarkdown, p, li, label {{ color: var(--text); }}
h1, h2, h3, h4 {{ font-family: var(--mono) !important; letter-spacing: .06em; color:#E6F1FF !important; }}
h4 {{ font-size: .95rem !important; text-transform: uppercase; border-bottom:1px solid var(--line);
      padding-bottom:.35rem; margin-top:.6rem; }}
h4::before {{ content:"▣ "; color: var(--neon); }}
[data-testid="stHeader"] {{ background: transparent; }}
[data-testid="stSidebar"] {{ background: #060B14; border-right: 1px solid var(--line); }}
[data-testid="stSidebar"] h1 {{ font-size: 1.05rem !important; }}
[data-testid="stCaptionContainer"], .stCaption {{ color: var(--dim) !important; }}
/* 指标卡：HUD 风格 */
[data-testid="stMetric"] {{ background: linear-gradient(180deg, rgba(10,17,28,.95), rgba(6,11,20,.95));
    border:1px solid var(--line); border-radius:4px; padding:10px 14px;
    box-shadow: inset 0 1px 0 rgba(34,211,238,.08), 0 0 22px rgba(0,240,168,.04); }}
[data-testid="stMetricValue"] {{ color: var(--neon); font-weight: 800; text-shadow: 0 0 14px rgba(0,240,168,.45); }}
[data-testid="stMetricLabel"] p {{ color: var(--dim) !important; text-transform: uppercase; letter-spacing:.12em; font-size:.7rem; }}
/* 面板、表格、展开框 */
[data-testid="stDataFrame"], [data-testid="stPlotlyChart"], details, [data-testid="stForm"] {{
    border:1px solid var(--line) !important; border-radius:4px; background: rgba(10,17,28,.55); }}
[data-testid="stPlotlyChart"] {{ padding: 4px; }}
details summary {{ text-transform: uppercase; letter-spacing:.06em; }}
/* 页面导航与单选：做成终端标签 */
[data-testid="stMain"] div[role="radiogroup"] {{ gap: 6px; flex-wrap: wrap; }}
[data-testid="stMain"] div[role="radiogroup"] > label {{ border:1px solid var(--line); background: var(--panel);
    padding: 4px 12px 4px 8px; border-radius:3px; margin:0 !important; transition: all .15s; }}
[data-testid="stMain"] div[role="radiogroup"] > label:hover {{ border-color: var(--cyan); }}
[data-testid="stMain"] div[role="radiogroup"] > label:has(input:checked) {{ border-color: var(--neon);
    box-shadow: 0 0 12px rgba(0,240,168,.35), inset 0 0 12px rgba(0,240,168,.08); }}
[data-testid="stMain"] div[role="radiogroup"] > label:has(input:checked) p {{ color: var(--neon) !important; }}
/* 按钮 */
.stButton button, .stDownloadButton button, [data-testid="stPopover"] button {{
    border:1px solid var(--line); background: var(--panel); border-radius:3px; letter-spacing:.03em; }}
.stButton button:hover, .stDownloadButton button:hover {{ border-color: var(--cyan); color: var(--cyan); }}
.stButton button[kind="primary"], .stFormSubmitButton button {{ background: rgba(0,240,168,.08);
    border-color: var(--neon); color: var(--neon); box-shadow: 0 0 12px rgba(0,240,168,.3); }}
.stButton button[kind="tertiary"] {{ border:none; background:transparent; color: var(--cyan); padding:0; }}
.stButton button[kind="tertiary"]:hover {{ color: var(--neon); text-shadow: 0 0 8px rgba(0,240,168,.6); }}
/* 输入框 */
[data-baseweb="select"] > div, [data-baseweb="input"] > div, textarea {{ background: var(--panel) !important;
    border-color: var(--line) !important; border-radius:3px !important; }}
/* 顶部状态栏 */
.hud-top {{ display:flex; align-items:center; gap:14px; border:1px solid var(--line); border-radius:4px;
    background: linear-gradient(90deg, rgba(10,17,28,.95), rgba(4,7,13,.95)); padding:8px 14px; margin-bottom:6px; }}
.hud-logo {{ width:34px; height:34px; border-radius:6px; display:grid; place-items:center; font-weight:800;
    background: linear-gradient(135deg, #1d4ed8, #22d3ee); color:#fff; box-shadow:0 0 14px rgba(34,211,238,.45); }}
.hud-title {{ font-weight:800; letter-spacing:.08em; color:#E6F1FF; font-size:1rem; }}
.hud-title b {{ color: var(--neon); }}
.hud-sub {{ color: var(--dim); font-size:.68rem; letter-spacing:.12em; text-transform:uppercase; }}
.hud-kpis {{ margin-left:auto; display:flex; gap:18px; align-items:center; }}
.hud-kpi {{ text-align:right; }} .hud-kpi .k {{ color:var(--dim); font-size:.62rem; letter-spacing:.12em; }}
.hud-kpi .v {{ color:#E6F1FF; font-weight:800; font-size:1rem; }} .hud-kpi .v.g {{ color:var(--neon); }}
.hud-kpi .v.a {{ color:var(--amber); }} .hud-kpi .v.r {{ color:var(--red); }}
.hud-live {{ background: var(--red); color:#fff; font-weight:800; font-size:.65rem; padding:2px 7px; border-radius:2px;
    letter-spacing:.12em; animation: blink 1.6s infinite; }}
@keyframes blink {{ 50% {{ opacity:.45; }} }}
.hud-clock {{ color:#E6F1FF; font-weight:800; font-size:1.05rem; letter-spacing:.06em; }}
/* 滚动快讯 */
.ticker {{ display:flex; align-items:center; border:1px solid var(--line); border-radius:3px; overflow:hidden;
    background: rgba(6,11,20,.9); margin-bottom:10px; height:28px; }}
.ticker .win {{ flex:1; overflow:hidden; height:100%; display:flex; align-items:center; }}
.ticker .tag {{ position:relative; z-index:2; flex-shrink:0; background: var(--red); color:#fff; font-weight:800; font-size:.68rem; padding:0 10px; height:100%;
    display:flex; align-items:center; letter-spacing:.12em; white-space:nowrap; }}
.ticker .track {{ display:inline-flex; white-space:nowrap; animation: scroll 70s linear infinite; padding-left:12px; }}
.ticker:hover .track {{ animation-play-state: paused; }}
.ticker .it {{ font-size:.74rem; margin-right:34px; color:var(--text); }}
.ticker .it b {{ color: var(--neon); }} .ticker .it i {{ color: var(--amber); font-style:normal; }}
@keyframes scroll {{ from {{ transform: translateX(0); }} to {{ transform: translateX(-50%); }} }}
/* HUD 大数字卡 */
.hud-cards {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap:8px; margin:6px 0 10px; }}
.hud-card {{ border:1px solid var(--line); border-radius:4px; padding:10px 12px;
    background: linear-gradient(180deg, rgba(10,17,28,.95), rgba(6,11,20,.95)); position:relative; overflow:hidden; }}
.hud-card::after {{ content:""; position:absolute; left:0; top:0; height:2px; width:100%;
    background: linear-gradient(90deg, transparent, var(--c, var(--neon)), transparent); opacity:.8; }}
.hud-card .k {{ color:var(--dim); font-size:.64rem; letter-spacing:.14em; text-transform:uppercase; }}
.hud-card .v {{ color: var(--c, var(--neon)); font-weight:800; font-size:1.7rem; line-height:1.15;
    text-shadow: 0 0 16px color-mix(in srgb, var(--c, var(--neon)) 55%, transparent); }}
.hud-card .s {{ color:var(--dim); font-size:.68rem; }}
</style>
"""


def inject():
    """注入全局样式和 Plotly 图表主题。每次运行调用一次。"""
    st.markdown(CSS, unsafe_allow_html=True)
    if "hud" not in pio.templates:
        t = go.layout.Template(pio.templates["plotly_dark"])
        t.layout.update(
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(family=MONO.replace('"', ""), color=TEXT, size=12),
            colorway=SEQ, title=dict(font=dict(size=13, color="#E6F1FF")),
            xaxis=dict(gridcolor="rgba(34,211,238,.08)", zerolinecolor=LINE, linecolor=LINE),
            yaxis=dict(gridcolor="rgba(34,211,238,.08)", zerolinecolor=LINE, linecolor=LINE),
            legend=dict(bgcolor="rgba(0,0,0,0)"), hoverlabel=dict(bgcolor=PANEL, bordercolor=CYAN,
                                                                 font=dict(family=MONO.replace('"', ""))),
            geo=dict(bgcolor="rgba(0,0,0,0)", landcolor="#0B1626", oceancolor="#04070D", showocean=True,
                     coastlinecolor=LINE, countrycolor=LINE, lakecolor="#04070D"),
            coloraxis=dict(colorscale=[[0, "#0B1626"], [0.5, "#0E7490"], [1, NEON]]),
        )
        pio.templates["hud"] = t
    pio.templates.default = "hud"
    px.defaults.template = "hud"
    px.defaults.color_discrete_sequence = SEQ
    px.defaults.color_continuous_scale = [[0, "#0B1626"], [0.5, "#0E7490"], [1, NEON]]


def top_bar(title: str, sub: str, kpis: list[tuple[str, str, str]], updated: str):
    """顶部状态栏。kpis: [(标签, 数值, 颜色类 g/a/r/空)]"""
    k = "".join(f'<div class="hud-kpi"><div class="k">{html.escape(a)}</div>'
                f'<div class="v {c}">{html.escape(b)}</div></div>' for a, b, c in kpis)
    st.markdown(f"""<div class="hud-top"><div class="hud-logo">AI</div>
      <div><div class="hud-title">{title}</div><div class="hud-sub">{html.escape(sub)}</div></div>
      <div class="hud-kpis">{k}<div class="hud-kpi"><div class="k">DATA UPDATED</div>
      <div class="hud-clock">{html.escape(updated)}</div></div><span class="hud-live">LIVE</span></div></div>""",
                unsafe_allow_html=True)


def ticker(items: list[str]):
    """滚动快讯条。items 里可以用 <b>（绿）和 <i>（琥珀）强调。"""
    if not items:
        return
    body = "".join(f'<span class="it">◆ {x}</span>' for x in items)
    st.markdown(f'<div class="ticker"><div class="tag">LIVE FEED</div>'
                f'<div class="win"><div class="track">{body}{body}</div></div></div>', unsafe_allow_html=True)


def hud_cards(cards: list[tuple[str, str, str, str]]):
    """大数字卡。cards: [(标签, 数值, 说明, 颜色)]"""
    h = "".join(f'<div class="hud-card" style="--c:{c}"><div class="k">{html.escape(k)}</div>'
                f'<div class="v">{html.escape(v)}</div><div class="s">{html.escape(s)}</div></div>'
                for k, v, s, c in cards)
    st.markdown(f'<div class="hud-cards">{h}</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------- 人才星球
GLOBE_HTML = r"""
<div id="wrap" style="position:relative;width:100%;height:__H__px;background:transparent;font-family:__MONO__">
  <canvas id="c" style="width:100%;height:100%;display:block"></canvas>
  <div id="tip" style="position:absolute;pointer-events:none;display:none;background:rgba(10,17,28,.96);
       border:1px solid #22D3EE;color:#C8D3E6;font-size:11px;padding:6px 8px;border-radius:3px;
       box-shadow:0 0 12px rgba(34,211,238,.35);white-space:nowrap"></div>
  <div style="position:absolute;left:50%;top:6px;transform:translateX(-50%);color:#6B7C93;font-size:10px;
       letter-spacing:.14em">__TITLE__</div>
</div>
<script>
const D = __DATA__;
const cv = document.getElementById('c'), ctx = cv.getContext('2d'), tip = document.getElementById('tip');
let W, H, R, cx, cy, dpr = window.devicePixelRatio || 1;
function size(){ const r = cv.getBoundingClientRect(); W = r.width; H = r.height;
  cv.width = W*dpr; cv.height = H*dpr; ctx.setTransform(dpr,0,0,dpr,0,0);
  R = Math.min(W*0.30, H*0.42); cx = W/2; cy = H/2 + 6; }
size(); window.addEventListener('resize', size);
let rot = 0, tilt = -0.35, drag = null, auto = true, hover = -1;
const pts = D.points, nodesL = D.left, nodesR = D.right, edges = D.edges;
function proj(p){ const ca=Math.cos(rot), sa=Math.sin(rot), ct=Math.cos(tilt), st=Math.sin(tilt);
  let x = p.x*ca - p.z*sa, z = p.x*sa + p.z*ca, y = p.y;
  let y2 = y*ct - z*st, z2 = y*st + z*ct; const s = 1/(1 + z2*0.35);
  return [cx + x*R*s, cy + y2*R*s, z2, s]; }
function sideNodes(list, left){ const n = list.length, x = left ? 70 : W - 70;
  return list.map((d,i)=>({x, y: 40 + (H-80)*(n>1? i/(n-1) : .5), d})); }
function frame(){
  ctx.clearRect(0,0,W,H);
  // 背景网格圈
  ctx.strokeStyle='rgba(34,211,238,.10)'; ctx.lineWidth=1;
  for (let k=1;k<=3;k++){ ctx.beginPath(); ctx.arc(cx,cy,R*(0.55+0.28*k),0,Math.PI*2); ctx.stroke(); }
  const P = pts.map(proj);
  // 两侧节点与连到星球的曲线
  const L = sideNodes(nodesL, true), Rn = sideNodes(nodesR, false);
  for (const side of [L, Rn]) for (const n of side){
    const tgt = P[n.d.anchor] || [cx,cy];
    ctx.strokeStyle = n.d.color + '55'; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(n.x, n.y);
    const mx = (n.x + tgt[0])/2; ctx.bezierCurveTo(mx, n.y, mx, tgt[1], tgt[0], tgt[1]); ctx.stroke();
  }
  // 合作关系连线
  ctx.lineWidth = .45;
  for (const e of edges){ const a = P[e[0]], b = P[e[1]]; if (!a||!b) continue;
    const al = Math.max(0, .16 - (a[2]+b[2])*.07); ctx.strokeStyle = 'rgba(167,139,250,'+al.toFixed(3)+')';
    ctx.beginPath(); ctx.moveTo(a[0],a[1]); ctx.lineTo(b[0],b[1]); ctx.stroke(); }
  // 粒子（远的先画）
  const order = P.map((p,i)=>i).sort((i,j)=>P[j][2]-P[i][2]);
  ctx.globalCompositeOperation = 'lighter';
  for (const i of order){ const p = P[i], d = pts[i]; const depth = (1 - p[2]) / 2;
    const r = (d.r * p[3]) * (i===hover ? 2.6 : 1); const a = (0.18 + 0.62*depth) * (d.hot ? 1 : 0.8);
    ctx.fillStyle = d.c; ctx.globalAlpha = a; ctx.beginPath(); ctx.arc(p[0], p[1], r, 0, Math.PI*2); ctx.fill();
    if (d.hot){ ctx.globalAlpha = a*.18; ctx.beginPath(); ctx.arc(p[0], p[1], r*2.6, 0, Math.PI*2); ctx.fill(); } }
  ctx.globalAlpha = 1; ctx.globalCompositeOperation = 'source-over';
  // 两侧节点标签
  for (const side of [[L,true],[Rn,false]]) for (const n of side[0]){
    ctx.fillStyle = n.d.color; ctx.shadowColor = n.d.color; ctx.shadowBlur = 12;
    ctx.beginPath(); ctx.arc(n.x, n.y, 5, 0, Math.PI*2); ctx.fill(); ctx.shadowBlur = 0;
    ctx.font = '10px __MONOJS__'; ctx.fillStyle = '#6B7C93'; ctx.textAlign = side[1] ? 'left' : 'right';
    const tx = side[1] ? n.x - 58 : n.x + 58;
    ctx.fillText(n.d.label, tx, n.y - 10); ctx.font = 'bold 12px __MONOJS__'; ctx.fillStyle = n.d.color;
    ctx.fillText(n.d.value, tx, n.y + 18); }
  if (auto && drag === null) rot += 0.0026;
  requestAnimationFrame(frame);
  window.__P = P;
}
cv.addEventListener('mousedown', e => { drag = [e.clientX, e.clientY, rot, tilt]; });
window.addEventListener('mouseup', () => { drag = null; });
cv.addEventListener('mousemove', e => {
  const r = cv.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
  if (drag){ rot = drag[2] + (e.clientX - drag[0])*0.008; tilt = Math.max(-1.3, Math.min(1.3, drag[3] + (e.clientY - drag[1])*0.006)); }
  const P = window.__P || []; let best = -1, bd = 64;
  for (let i=0;i<P.length;i++){ if (P[i][2] > 0.2) continue; const dx = P[i][0]-mx, dy = P[i][1]-my, dd = dx*dx+dy*dy;
    if (dd < bd){ bd = dd; best = i; } }
  hover = best;
  if (best >= 0){ const d = pts[best]; tip.style.display='block';
    tip.innerHTML = '<b style="color:'+d.c+'">'+d.n+'</b><br>'+d.i+'<br><span style="color:#6B7C93">'+d.t+'</span>';
    tip.style.left = Math.min(mx+14, W-260)+'px'; tip.style.top = (my+12)+'px'; }
  else tip.style.display='none';
});
cv.addEventListener('mouseleave', () => { tip.style.display='none'; hover=-1; });
cv.addEventListener('dblclick', () => { auto = !auto; });
requestAnimationFrame(frame);
</script>
"""

QCOLOR = {"①": RED, "②": CYAN, "③": AMBER, "④": "#3B82F6"}


@st.cache_data(max_entries=6, show_spinner=False)
def _globe_payload(rows: tuple, left: tuple, right: tuple, edges_on: bool) -> str:
    """rows: (姓名, 机构, 领域, 象限, 成熟度, 潜力, 合作者) 元组。领域决定经度扇区，成熟度决定纬度，潜力决定亮度和大小。"""
    rnd = random.Random(7)
    doms = sorted({r[2] for r in rows})
    sector = {d: i for i, d in enumerate(doms)}
    pts, by_name = [], {}
    for k, (n, inst, dom, q, ready, poten, co) in enumerate(rows):
        base = 2 * math.pi * sector[dom] / max(1, len(doms))
        lon = base + rnd.uniform(-1, 1) * math.pi / max(1, len(doms)) * 0.92
        lat = math.asin(rnd.uniform(-0.97, 0.97))          # 在球面上均匀分布
        rad = 0.94 + 0.10 * rnd.random() + 0.05 * (poten / 100)
        x, y, z = rad * math.cos(lat) * math.cos(lon), -rad * math.sin(lat), rad * math.cos(lat) * math.sin(lon)
        hot = q.startswith("①")
        c = {"①": RED, "②": CYAN, "③": AMBER}.get(q[:1], "#C9A94E" if poten >= 50 else "#7C6A3A")
        pts.append({"x": round(x, 4), "y": round(y, 4), "z": round(z, 4), "c": c, "hot": hot,
                    "r": round(0.7 + 1.1 * (poten / 100) + (0.9 if hot else 0), 2),
                    "n": html.escape(n), "i": html.escape(inst), "t": f"{html.escape(dom)} · {q} · 成熟度 {ready:.0f} / 潜力 {poten:.0f}"})
        by_name.setdefault(n, k)
    edges = []
    if edges_on:
        for k, r in enumerate(rows):
            if not r[3].startswith("①"):
                continue
            for co in (r[6] or "").split("、")[:2]:
                j = by_name.get(co)
                if j is not None and j != k:
                    edges.append([k, j])
    def anchor(name):
        return next((i for i, r in enumerate(rows) if r[2] == name), 0)
    lft = [{"label": l, "value": v, "color": c, "anchor": anchor(l)} for l, v, c in left]
    rgt = [{"label": l, "value": v, "color": c, "anchor": rnd.randrange(len(rows)) if rows else 0} for l, v, c in right]
    return json.dumps({"points": pts, "left": lft, "right": rgt, "edges": edges[:700]}, ensure_ascii=False)


def globe(df, left: list[tuple[str, str, str]], right: list[tuple[str, str, str]], height: int = 560,
          title: str = "TALENT SPHERE · 拖动旋转 · 双击暂停", max_points: int = 3500):
    """旋转的人才星球。df 需要列：姓名、当前机构、主学科、创业象限、成熟度分、潜力分、稳定合作者。"""
    d = df.dropna(subset=["成熟度分", "潜力分"])
    if d.empty:
        return
    is_hot = d["创业象限"].astype(str).str.startswith("①")
    hot = d[is_hot]
    hot = hot.assign(_s=hot["成熟度分"] + hot["潜力分"]).sort_values("_s", ascending=False).head(260).drop(columns="_s")
    rest = d[~is_hot]
    rest = rest.sample(min(len(rest), max(0, max_points - len(hot))), random_state=1)
    d = hot.head(max_points) if len(hot) >= max_points else pd.concat([hot, rest])
    rows = tuple(zip(d["姓名"].astype(str), d["当前机构"].fillna("—").astype(str), d["主学科"].fillna("未归类").astype(str),
                     d["创业象限"].astype(str), d["成熟度分"].astype(float), d["潜力分"].astype(float),
                     (d["稳定合作者"] if "稳定合作者" in d else pd.Series("", index=d.index)).fillna("").astype(str)))
    payload = _globe_payload(rows, tuple(left), tuple(right), True)
    mono_js = "JetBrains Mono, Menlo, Consolas, monospace"
    page = (GLOBE_HTML.replace("__DATA__", payload).replace("__H__", str(height))
            .replace("__MONOJS__", mono_js).replace("__MONO__", mono_js).replace("__TITLE__", html.escape(title)))
    components.html(page, height=height + 6)


def now_text(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "—"
