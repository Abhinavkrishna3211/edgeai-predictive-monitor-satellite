"""gateway/api/dashboard.py — HTTP status dashboard: the single-page HTML/JS
app, its JSON status API, and the maintenance/report/training endpoints,
extracted from recv_verify.py (Phase 8b2 task 1).

_sat_lock/_satellites are imported directly from gateway.registry.satellite_state
at module level (not via a lazy `import recv_verify`) — satellite_state.py has
no import-time dependency on recv_verify.py (its own recv_verify references are
lazy, inside function bodies), so importing it here at module load time is safe
and gives this module its own direct line to the registry rather than routing
through recv_verify.py.

Everything else this module touches (thresholds, CLI-set config, `_MAINT_LOG`,
`_sat_models`, ...) is owned by recv_verify.py and is CLI-mutable via
recv_verify.main()'s `global` statements, so it is read through a *lazy*
`import recv_verify as _rv` inside each function body instead — a module-level
import would deadlock the circular load, and a bare top-level reference would
silently stop tracking main()'s reassignments (see gateway/api/notifications.py's
module docstring for the same reasoning applied there).

_DisplayState/_display (recv_verify.py's most-recently-updated-satellite state
for the matplotlib live plot) deliberately did NOT move here despite this
being the obvious-looking new home: it is read only by run_plot() and written
only by _process_satellite_frame(), never by anything in this module — it is
plot-path state, not HTTP-dashboard state — so it stays in recv_verify.py.
"""

import base64
import glob
import json
import math
import os
import socket
import threading
import time
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

from gateway.registry.satellite_state import _sat_lock, _satellites

_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#07111e">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="EPM Monitor">
<link rel="manifest" href="/manifest.json">
<title>EPM &middot; Industrial Monitor</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/qrcode@1.5.3/build/qrcode.min.js"></script>
<style>
:root{
  --bg:#07111e;--card:#0d1a27;--card2:#122031;--border:#1a2f44;
  --text:#dde6f0;--muted:#5b7a96;--dim:#2d4d66;
  --ok:#22c55e;--warn:#f59e0b;--fault:#ef4444;--blue:#3b82f6;--acc:#8b5cf6;
  --ok-d:rgba(34,197,94,.13);--warn-d:rgba(245,158,11,.13);--fault-d:rgba(239,68,68,.13);
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;min-height:100vh}
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}

/* HEADER */
header{display:flex;align-items:center;padding:0 22px;height:56px;background:var(--card);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:200;gap:14px;box-shadow:0 2px 20px rgba(0,0,0,.5)}
.logo{display:flex;align-items:center;gap:9px;flex-shrink:0}
.logo-icon{width:32px;height:32px;background:linear-gradient(135deg,#3b82f6,#8b5cf6);border-radius:7px;display:grid;place-items:center;font-size:1rem;flex-shrink:0}
.logo-name{font-size:.88rem;font-weight:700;letter-spacing:.02em;line-height:1.1}
.logo-sub{font-size:.55rem;color:var(--muted);letter-spacing:.06em;text-transform:uppercase}
.hdr-sep{width:1px;height:26px;background:var(--border);flex-shrink:0}
#factory-lbl{font-size:.78rem;font-weight:600;color:var(--blue)}
.hdr-right{display:flex;align-items:center;gap:10px;margin-left:auto}
.chip{display:inline-flex;align-items:center;gap:4px;padding:3px 9px;border-radius:20px;font-size:.63rem;font-weight:600;white-space:nowrap}
.chip-ok{background:var(--ok-d);color:var(--ok)}
.chip-warn{background:var(--warn-d);color:var(--warn)}
.chip-fault{background:var(--fault-d);color:var(--fault)}
.chip-blue{background:rgba(59,130,246,.1);color:var(--blue)}
.chip-muted{background:rgba(91,122,150,.08);color:var(--muted)}
.ldot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.ldot.ok{background:var(--ok);animation:pok 2s infinite}
.ldot.warn{background:var(--warn);animation:pwarn 1s infinite}
.ldot.fault{background:var(--fault);animation:pfault .4s infinite}
@keyframes pok{0%,100%{opacity:1;box-shadow:0 0 0 0 rgba(34,197,94,.4)}70%{box-shadow:0 0 0 5px rgba(34,197,94,0)}}
@keyframes pwarn{0%,100%{opacity:1;box-shadow:0 0 0 0 rgba(245,158,11,.5)}70%{box-shadow:0 0 0 5px rgba(245,158,11,0)}}
@keyframes pfault{0%,20%,40%,60%,80%,100%{opacity:1}10%,30%,50%,70%,90%{opacity:.1}}
.hdr-uptime{font-size:.62rem;color:var(--muted);font-variant-numeric:tabular-nums}
@media(max-width:680px){.hdr-sep,#factory-lbl,.hdr-uptime,.chip-muted{display:none}}
.c-pstatus{text-align:center;font-size:.78rem;font-weight:700;padding:5px 12px 3px;letter-spacing:.03em;border-bottom:1px solid var(--border)}
.c-ml-row{text-align:center;font-size:.66rem;padding:3px 12px;border-bottom:1px solid var(--border);color:var(--muted)}
.btn-green{background:rgba(34,197,94,.14);color:var(--ok);border-color:rgba(34,197,94,.28)}
.btn-green:hover{background:rgba(34,197,94,.24)}
@media(max-width:640px){
  header{padding:0 10px}
  .summary{padding:8px 10px;gap:6px}
  .tile{min-width:72px;padding:8px 10px}
  .tile-val{font-size:1.35rem}
  .tile-lbl{font-size:.52rem}
  .tabs-bar{padding:8px 10px 0;overflow-x:auto}
  .pane{padding:10px 10px 48px}
  .cards-grid{grid-template-columns:1fr}
  .c-metrics{grid-template-columns:repeat(2,1fr)}
  .met.sp3{grid-column:span 2}
  .c-actions{flex-wrap:wrap}
  .btn{font-size:.62rem;padding:4px 8px}
}

/* BANNER */
#banner{display:none;align-items:center;justify-content:center;gap:8px;padding:9px 22px;font-size:.8rem;font-weight:700;letter-spacing:.02em}
#banner.fault{display:flex;background:rgba(239,68,68,.07);color:var(--fault);border-bottom:2px solid rgba(239,68,68,.4);animation:bfl 1.5s infinite}
#banner.warn{display:flex;background:rgba(245,158,11,.06);color:var(--warn);border-bottom:2px solid rgba(245,158,11,.3)}
@keyframes bfl{0%,100%{border-bottom-color:rgba(239,68,68,.4)}50%{border-bottom-color:rgba(239,68,68,.8)}}
.bpulse{display:inline-block;animation:shake .55s infinite}
@keyframes shake{0%,100%{transform:rotate(0)}25%{transform:rotate(-9deg)}75%{transform:rotate(9deg)}}

/* SUMMARY */
.summary{display:flex;flex-wrap:wrap;gap:9px;padding:14px 22px 0}
.tile{flex:1;min-width:100px;background:var(--card);border:1px solid var(--border);border-radius:9px;padding:12px 14px}
.tile-lbl{font-size:.58rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:5px}
.tile-val{font-size:1.85rem;font-weight:800;line-height:1;font-variant-numeric:tabular-nums}
.tile-sub{font-size:.6rem;color:var(--dim);margin-top:3px}

/* TABS */
.tabs-bar{padding:13px 22px 0}
.tabs{display:inline-flex;gap:1px;background:var(--card);border:1px solid var(--border);border-radius:8px;padding:3px}
.tab{padding:6px 15px;border-radius:6px;font-size:.74rem;font-weight:500;color:var(--muted);cursor:pointer;border:none;background:none;transition:all .15s;white-space:nowrap}
.tab:hover{color:var(--text);background:rgba(255,255,255,.04)}
.tab.active{background:var(--card2);color:var(--text);font-weight:700;box-shadow:0 1px 4px rgba(0,0,0,.3)}
.tbadge{display:inline-flex;align-items:center;justify-content:center;min-width:16px;height:16px;border-radius:8px;background:var(--fault);color:#fff;font-size:.55rem;font-weight:700;padding:0 4px;margin-left:4px;vertical-align:middle}
.pane{display:none;padding:13px 22px 48px}
.pane.active{display:block}

/* MACHINE CARDS */
.cards-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(336px,1fr));gap:13px}
.no-sats{text-align:center;padding:80px 0;color:var(--muted)}
.no-sats h2{color:var(--text);font-size:.92rem;margin-bottom:7px}
.no-sats p{font-size:.76rem}
.card{background:var(--card);border:1px solid var(--border);border-radius:11px;overflow:hidden;transition:transform .18s,box-shadow .18s,border-color .2s}
.card:hover{transform:translateY(-2px);box-shadow:0 8px 32px rgba(0,0,0,.4)}
.card.ok{border-color:rgba(34,197,94,.2)}
.card.warn{border-color:rgba(245,158,11,.55);box-shadow:0 0 0 1px rgba(245,158,11,.1)}
.card.fault{border-color:rgba(239,68,68,.7);box-shadow:0 0 0 2px rgba(239,68,68,.12),0 0 26px rgba(239,68,68,.07)}
.card.offline{opacity:.38;filter:grayscale(.5)}
.c-head{display:flex;align-items:flex-start;justify-content:space-between;padding:12px 14px 9px;border-bottom:1px solid var(--border)}
.c-name{font-size:1rem;font-weight:700}
.c-mac{font-size:.6rem;color:var(--muted);font-family:monospace;margin-top:2px}
.c-fw{font-size:.57rem;color:var(--dim);margin-top:1px}
.c-right{display:flex;flex-direction:column;align-items:flex-end;gap:5px}
.sdot{width:9px;height:9px;border-radius:50%}
.sdot.ok{background:var(--ok);animation:pok 2s infinite}
.sdot.warn{background:var(--warn);animation:pwarn 1s infinite}
.sdot.fault{background:var(--fault);animation:pfault .4s infinite}
.badge{padding:2px 8px;border-radius:8px;font-size:.64rem;font-weight:800;letter-spacing:.04em}
.badge.ok{background:var(--ok-d);color:var(--ok)}
.badge.warn{background:var(--warn-d);color:var(--warn)}
.badge.fault{background:var(--fault-d);color:var(--fault)}
.c-metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--border)}
.met{background:var(--card);padding:8px 11px}
.met.sp3{grid-column:span 3}
.ml{font-size:.56rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.mv{font-size:.93rem;font-weight:600;font-family:monospace;margin-top:2px}
.c-health{display:flex;align-items:center;gap:10px;padding:9px 14px}
.c-hbar-wrap{flex:1}
.c-hbar-top{display:flex;justify-content:space-between;font-size:.58rem;color:var(--muted);margin-bottom:4px}
.c-hbar{height:5px;border-radius:3px;background:var(--border);overflow:hidden}
.c-hfill{height:100%;border-radius:3px;transition:width .7s,background .5s}
.c-pfault{padding:.3rem .85rem .45rem;border-bottom:1px solid #0d2035}
.c-pfault-top{display:flex;justify-content:space-between;font-size:.58rem;color:var(--muted);margin-bottom:4px}
.c-pfbar{height:4px;border-radius:3px;background:var(--border);overflow:hidden}
.c-pffill{height:100%;border-radius:3px;transition:width .5s,background .4s}
.c-attr{font-size:.69rem;color:var(--muted);padding:4px 14px 5px;border-top:1px solid #0d2035;background:rgba(251,191,36,.04)}
.attr-feat{color:#fbbf24;font-weight:600;font-family:monospace}
.drift-chip{display:inline-block;font-size:.55rem;font-weight:600;padding:1px 6px;border-radius:8px;margin-left:5px;vertical-align:middle;letter-spacing:.02em}
.drift-stable{background:rgba(74,109,136,.18);color:#4a6d88}
.drift-recent{background:rgba(245,158,11,.18);color:#f59e0b}
.drift-fresh{background:rgba(239,68,68,.18);color:#ef4444}
.ev-refresh{background:rgba(99,102,241,.08);border-left:2px solid #6366f1}
.ev-adapt{background:rgba(16,185,129,.06);border-left:2px solid #10b981}
.adapt-chip{display:inline-block;font-size:.55rem;font-weight:600;padding:1px 6px;border-radius:8px;margin-left:5px;vertical-align:middle;letter-spacing:.02em;background:rgba(16,185,129,.18);color:#10b981;font-family:monospace}
.bl-section{margin-bottom:20px;background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.bl-sat-hdr{padding:10px 14px;font-weight:700;font-size:.8rem;border-bottom:1px solid var(--border);display:flex;gap:10px;align-items:baseline}
.bl-sat-mac{font-size:.66rem;color:var(--muted);font-weight:400;font-family:monospace}
.bl-tbl{width:100%;border-collapse:collapse;font-size:.74rem}
.bl-tbl th{text-align:left;padding:6px 12px;background:rgba(0,0,0,.04);color:var(--muted);font-weight:600;font-size:.64rem;text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid var(--border)}
.bl-tbl td{padding:7px 12px;border-bottom:1px solid rgba(255,255,255,.04)}
.bl-tbl tr:last-child td{border-bottom:none}
.bl-warm{color:var(--muted);font-style:italic}
.c-hpct{font-size:1.15rem;font-weight:700;min-width:46px;text-align:right;font-variant-numeric:tabular-nums}
.c-rec{margin:0 12px 9px;padding:8px 11px;border-radius:7px;font-size:.72rem}
.c-rec.ok{background:rgba(34,197,94,.05);border:1px solid rgba(34,197,94,.17)}
.c-rec.warn{background:rgba(245,158,11,.06);border:1px solid rgba(245,158,11,.22)}
.c-rec.fault{background:rgba(239,68,68,.07);border:1px solid rgba(239,68,68,.3)}
.c-rec-t{font-weight:700;margin-bottom:2px}
.c-rec-s{color:var(--muted);font-size:.64rem;line-height:1.5;margin-top:2px}
.c-chart{padding:2px 12px 8px}
.c-maint-row{display:flex;align-items:center;justify-content:space-between;padding:5px 14px;background:rgba(7,17,30,.45);border-top:1px solid var(--border);font-size:.63rem;color:var(--muted);gap:6px;flex-wrap:wrap}
.c-maint-val{color:var(--text);font-weight:600}
.c-actions{display:flex;gap:6px;padding:8px 12px;background:var(--card2);border-top:1px solid var(--border)}
.btn{padding:5px 11px;border-radius:6px;font-size:.68rem;font-weight:600;cursor:pointer;border:1px solid transparent;transition:all .15s;white-space:nowrap;text-decoration:none;display:inline-flex;align-items:center;gap:4px}
.btn-blue{background:rgba(59,130,246,.14);color:var(--blue);border-color:rgba(59,130,246,.28)}
.btn-blue:hover{background:rgba(59,130,246,.24)}
.btn-ghost{background:transparent;color:var(--muted);border-color:var(--border)}
.btn-ghost:hover{background:rgba(255,255,255,.04);color:var(--text)}
.c-foot{display:flex;justify-content:space-between;padding:6px 14px;background:rgba(7,17,30,.55);border-top:1px solid var(--border);font-size:.6rem;color:var(--muted);flex-wrap:wrap;gap:3px}

/* TABLE (alert log) */
.pane-head{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:12px;gap:10px;flex-wrap:wrap}
.pane-title{font-size:.86rem;font-weight:700}
.pane-note{font-size:.63rem;color:var(--muted);margin-top:3px}
.tbl-wrap{background:var(--card);border:1px solid var(--border);border-radius:9px;overflow:hidden;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.72rem;min-width:580px}
thead tr{background:var(--card2)}
th{padding:8px 12px;text-align:left;font-size:.6rem;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:600;border-bottom:1px solid var(--border)}
td{padding:8px 12px;border-bottom:1px solid rgba(26,47,68,.5);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,.015)}
.trans{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:5px;font-size:.66rem;font-weight:700}
.trans.esc{background:var(--fault-d);color:var(--fault)}
.trans.rec{background:var(--ok-d);color:var(--ok)}
.trans.war{background:var(--warn-d);color:var(--warn)}
.empty-cell{text-align:center;color:var(--muted);padding:32px!important;font-size:.76rem}

/* MAINTENANCE CARDS */
.maint-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:11px}
.mc{background:var(--card);border:1px solid var(--border);border-radius:9px;padding:14px}
.mc-name{font-size:.86rem;font-weight:700;margin-bottom:10px;display:flex;justify-content:space-between;align-items:center;gap:6px}
.mc-row{display:flex;justify-content:space-between;align-items:baseline;padding:5px 0;border-bottom:1px solid rgba(26,47,68,.45);font-size:.72rem;gap:8px}
.mc-row:last-of-type{border-bottom:none}
.mc-lbl{color:var(--muted);font-size:.63rem;flex-shrink:0}
.mc-val{font-weight:600;text-align:right;font-size:.72rem}
.mc-empty{color:var(--muted);font-size:.72rem;text-align:center;padding:16px 0}
.mc-foot{margin-top:11px;padding-top:9px;border-top:1px solid var(--border)}

/* REPORTS */
.rep-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:12px}
.rep-card{background:var(--card);border:1px solid var(--border);border-radius:9px;padding:16px}
.rep-card h3{font-size:.78rem;font-weight:700;margin-bottom:11px;display:flex;align-items:center;gap:6px}
.rep-row{display:flex;justify-content:space-between;padding:5px 0;font-size:.71rem;border-bottom:1px solid rgba(26,47,68,.4)}
.rep-row:last-child{border-bottom:none}
.rep-key{color:var(--muted)}
.rep-val{font-weight:600;font-family:monospace;font-size:.68rem;text-align:right}
.chk-list{list-style:none}
.chk-li{display:flex;align-items:center;gap:7px;padding:5px 0;border-bottom:1px solid rgba(26,47,68,.35);font-size:.71rem}
.chk-li:last-child{border-bottom:none}
.chk-icon{width:15px;text-align:center;font-size:.78rem;flex-shrink:0}
.exp-col{display:flex;flex-direction:column;gap:7px;margin-top:8px}
.exp-btn{display:flex;align-items:center;gap:7px;padding:8px 12px;border-radius:7px;background:var(--card2);border:1px solid var(--border);color:var(--text);font-size:.71rem;cursor:pointer;text-decoration:none;transition:all .15s;font-family:inherit;width:100%;text-align:left}
.exp-btn:hover{border-color:var(--blue);background:rgba(59,130,246,.06)}

/* MODAL */
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:500;backdrop-filter:blur(5px);align-items:center;justify-content:center;padding:20px}
.modal-bg.open{display:flex}
/* QR MODAL */
#qr-modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:600;backdrop-filter:blur(5px);align-items:center;justify-content:center}
#qr-modal.open{display:flex}
.qr-box{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:24px 28px;text-align:center;box-shadow:0 24px 60px rgba(0,0,0,.8)}
.qr-box h3{font-size:.88rem;margin-bottom:6px}
.qr-box p{font-size:.72rem;color:var(--muted);margin-bottom:14px}
.qr-close{margin-top:14px;background:var(--card2);border:1px solid var(--border);color:var(--text);padding:6px 20px;border-radius:6px;cursor:pointer;font-size:.78rem}
.modal{background:var(--card);border:1px solid var(--border);border-radius:12px;width:100%;max-width:450px;max-height:90vh;overflow-y:auto;box-shadow:0 24px 60px rgba(0,0,0,.7)}
.modal-hd{display:flex;align-items:center;justify-content:space-between;padding:15px 18px;border-bottom:1px solid var(--border)}
.modal-title{font-size:.88rem;font-weight:700}
.modal-x{width:25px;height:25px;border-radius:6px;border:none;background:rgba(255,255,255,.06);color:var(--text);cursor:pointer;font-size:.85rem;display:grid;place-items:center;transition:background .15s}
.modal-x:hover{background:rgba(255,255,255,.12)}
.modal-bd{padding:16px 18px}
.modal-info{font-size:.66rem;color:var(--muted);padding:6px 9px;background:rgba(7,17,30,.6);border-radius:6px;margin-bottom:13px;font-family:monospace}
.fg{margin-bottom:12px}
.fg label{display:block;font-size:.64rem;color:var(--muted);margin-bottom:4px;font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.fi{width:100%;background:rgba(7,17,30,.8);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:6px 9px;font-size:.76rem;font-family:inherit;transition:border-color .15s}
.fi:focus{outline:none;border-color:var(--blue)}
.fi::placeholder{color:var(--dim)}
.fg-row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.modal-ft{display:flex;justify-content:flex-end;gap:8px;padding:12px 18px;border-top:1px solid var(--border)}
.btn-sm{padding:6px 14px;border-radius:6px;font-size:.71rem;font-weight:600;cursor:pointer;border:1px solid var(--border);transition:all .15s}
.btn-sm-ok{background:var(--blue);color:#fff;border-color:var(--blue)}
.btn-sm-ok:hover{background:#2563eb}
.btn-sm-cancel{background:transparent;color:var(--muted)}
.btn-sm-cancel:hover{color:var(--text);background:rgba(255,255,255,.04)}

/* TOAST */
#toast{position:fixed;bottom:20px;right:20px;padding:10px 16px;border-radius:8px;font-size:.74rem;font-weight:700;z-index:9999;transform:translateY(50px);opacity:0;transition:all .25s;pointer-events:none;max-width:300px}
#toast.in{transform:translateY(0);opacity:1}
#toast.ok-t{background:#15803d;color:#fff}
#toast.err-t{background:#b91c1c;color:#fff}

footer{text-align:center;padding:11px;color:var(--dim);font-size:.6rem;border-top:1px solid var(--border)}
</style>
</head>
<body>

<header>
  <div class="logo">
    <div class="logo-icon">&#9881;</div>
    <div>
      <div class="logo-name">EPM Monitor</div>
      <div class="logo-sub">EdgeAI Predictive Maintenance</div>
    </div>
  </div>
  <div class="hdr-sep"></div>
  <span id="factory-lbl">Factory</span>
  <div class="hdr-right">
    <span class="chip chip-muted" id="gstatus">&#9679; LOADING</span>
    <span class="chip chip-muted" id="notif-chip">&#128276; Notify OFF</span>
    <span class="chip chip-blue" id="auth-chip" style="display:none">&#128274; Secured</span>
    <div style="display:flex;align-items:center;gap:5px">
      <div class="ldot ok" id="live-dot"></div>
      <span class="hdr-uptime" id="hdr-clock">&#8212;</span>
    </div>
    <span class="hdr-uptime">UP <strong id="hdr-up">&#8212;</strong></span>
  </div>
</header>

<div id="banner"></div>

<div class="summary">
  <div class="tile"><div class="tile-lbl">Connected</div><div class="tile-val" id="s-conn" style="color:var(--blue)">0</div><div class="tile-sub">satellites online</div></div>
  <div class="tile"><div class="tile-lbl">Healthy</div><div class="tile-val" id="s-ok" style="color:var(--ok)">0</div><div class="tile-sub">running OK</div></div>
  <div class="tile"><div class="tile-lbl">Warning</div><div class="tile-val" id="s-warn" style="color:var(--warn)">0</div><div class="tile-sub">elevated vibration</div></div>
  <div class="tile"><div class="tile-lbl">Fault</div><div class="tile-val" id="s-fault" style="color:var(--fault)">0</div><div class="tile-sub">needs attention</div></div>
  <div class="tile"><div class="tile-lbl">Fault Events</div><div class="tile-val" id="s-fevt" style="color:var(--fault)">0</div><div class="tile-sub">this session</div></div>
  <div class="tile"><div class="tile-lbl">Avg Health</div><div class="tile-val" id="s-health">&#8212;</div><div class="tile-sub" id="s-fstate" style="color:var(--muted)">&#8212;</div></div>
</div>

<div class="tabs-bar">
  <div class="tabs">
    <button class="tab active" data-tab="machines">&#9881; Machines</button>
    <button class="tab" data-tab="alerts">&#128203; Alert Log <span class="tbadge" id="alert-badge" style="display:none">0</span></button>
    <button class="tab" data-tab="maintenance">&#128295; Maintenance</button>
    <button class="tab" data-tab="baselines">&#128200; Baselines</button>
    <button class="tab" data-tab="reports">&#128202; Reports</button>
  </div>
</div>

<!-- MACHINES -->
<div class="pane active" id="pane-machines">
  <div class="cards-grid" id="grid">
    <div class="no-sats"><h2>Waiting for satellites&hellip;</h2><p>Power on XIAO ESP32-S3 nodes or run <code>satellite_sim.py</code></p></div>
  </div>
</div>

<!-- ALERT LOG -->
<div class="pane" id="pane-alerts">
  <div class="pane-head">
    <div>
      <div class="pane-title">Alert History &mdash; Audit Trail</div>
      <div class="pane-note">&#128274; Compliance-ready log of all machine state transitions. Every OK&rarr;WARN&rarr;FAULT transition is timestamped and recorded.</div>
    </div>
    <div style="display:flex;gap:7px">
      <button class="btn btn-ghost" onclick="loadAlerts()">&#8635; Refresh</button>
      <button class="btn btn-blue" onclick="exportAlerts()">&#8595; Export JSON</button>
    </div>
  </div>
  <div class="tbl-wrap">
    <table>
      <thead><tr><th>Time</th><th>Machine</th><th>Transition</th><th>Kurtosis</th><th>Crest</th><th>Z-Score</th><th>Contributing Features</th><th>MAC</th></tr></thead>
      <tbody id="alert-tbody"><tr><td class="empty-cell" colspan="8">Switch to this tab to load alert history.</td></tr></tbody>
    </table>
  </div>
</div>

<!-- MAINTENANCE -->
<div class="pane" id="pane-maintenance">
  <div class="pane-head">
    <div>
      <div class="pane-title">Maintenance Records</div>
      <div class="pane-note">Persisted to <code>logs/maintenance_log.json</code> &mdash; survives gateway restarts. Keyed by hardware MAC address.</div>
    </div>
    <button class="btn btn-ghost" onclick="loadMaintenance()">&#8635; Refresh</button>
  </div>
  <div class="maint-grid" id="maint-grid"><p style="color:var(--muted);font-size:.76rem">Loading&hellip;</p></div>
</div>

<!-- BASELINES -->
<div class="pane" id="pane-baselines">
  <div class="pane-head">
    <div>
      <div class="pane-title">Adaptive Machine Baselines</div>
      <div class="pane-note">Per-machine distributions learned during healthy operation (EMA &alpha;=0.0005 &asymp;11 min half-life). Z &ge; 4&sigma; raises WARN; Z &ge; 6&sigma; raises FAULT independently of the absolute thresholds.</div>
    </div>
    <button class="btn btn-ghost" onclick="loadBaselines()">&#8635; Refresh</button>
  </div>
  <div id="baselines-grid"></div>
</div>

<!-- REPORTS -->
<div class="pane" id="pane-reports">
  <div class="pane-head"><div class="pane-title">Reports &amp; System Info</div></div>
  <div class="rep-grid">

    <div class="rep-card">
      <h3><span>&#128187;</span> System Status</h3>
      <div class="rep-row"><span class="rep-key">Factory / Site</span><span class="rep-val" id="r-factory">&#8212;</span></div>
      <div class="rep-row"><span class="rep-key">Gateway Uptime</span><span class="rep-val" id="r-uptime">&#8212;</span></div>
      <div class="rep-row"><span class="rep-key">Satellites</span><span class="rep-val" id="r-sats">&#8212;</span></div>
      <div class="rep-row"><span class="rep-key">K Warn / Fault</span><span class="rep-val" id="r-kth">&#8212;</span></div>
      <div class="rep-row"><span class="rep-key">MIC CF Warn / Fault</span><span class="rep-val" id="r-cfth">&#8212;</span></div>
      <div class="rep-row"><span class="rep-key">IMU CF Warn / Fault</span><span class="rep-val" id="r-imucfth">&#8212;</span></div>
      <div class="rep-row"><span class="rep-key">Notifications</span><span class="rep-val" id="r-notif">&#8212;</span></div>
    </div>

    <div class="rep-card">
      <h3><span>&#9989;</span> Compliance Checklist</h3>
      <ul class="chk-list">
        <li class="chk-li"><span class="chk-icon" id="chk-conn">&#9744;</span>All machines connected</li>
        <li class="chk-li"><span class="chk-icon" id="chk-health">&#9744;</span>No active FAULT conditions</li>
        <li class="chk-li"><span class="chk-icon" id="chk-maint">&#9744;</span>Maintenance records up to date</li>
        <li class="chk-li"><span class="chk-icon" id="chk-auth">&#9744;</span>Dashboard access secured (--auth)</li>
        <li class="chk-li"><span class="chk-icon" id="chk-notif">&#9744;</span>Alert notifications configured</li>
        <li class="chk-li"><span class="chk-icon" id="chk-cal">&#9744;</span>All sensors calibrated</li>
        <li class="chk-li"><span class="chk-icon" id="chk-log">&#10003;</span>Audit trail active (in-memory, 1000 events)</li>
      </ul>
    </div>

    <div class="rep-card">
      <h3><span>&#8595;</span> Export Data</h3>
      <p style="font-size:.7rem;color:var(--muted);margin-bottom:10px;line-height:1.6">Download sensor logs and audit records for compliance reports, insurance claims, or offline ML analysis.</p>
      <div class="exp-col" id="export-sat-list"><span style="font-size:.7rem;color:var(--muted)">Connect a satellite to enable CSV exports.</span></div>
      <div style="margin-top:9px;padding-top:9px;border-top:1px solid var(--border);display:flex;flex-direction:column;gap:7px">
        <a class="exp-btn" href="/api/report" target="_blank">&#128202; Full Factory Report (all machines, printable PDF)</a>
        <button class="exp-btn" onclick="exportAlerts()">&#128203; Alert Log Export (JSON)</button>
      </div>
    </div>

    <div class="rep-card">
      <h3><span>&#128267;</span> Power &amp; Battery</h3>
      <div class="rep-row"><span class="rep-key">Power Source</span><span class="rep-val">USB / External 5V</span></div>
      <div class="rep-row"><span class="rep-key">Battery %</span><span class="rep-val">N/A (USB powered)</span></div>
      <div class="rep-row"><span class="rep-key">WiFi Power Mode</span><span class="rep-val">WIFI_PS_NONE</span></div>
      <p style="margin-top:9px;font-size:.66rem;color:var(--muted);line-height:1.65">
        For LiPo: set <code>esp_wifi_set_ps(WIFI_PS_MIN_MODEM)</code> in wifi_task.c and add
        <code>CONFIG_PM_ENABLE=y</code> to sdkconfig.defaults (~30% power saving).
        Battery % requires ADC on a free GPIO &mdash; not yet wired.
      </p>
    </div>

    <div class="rep-card">
      <h3><span>&#128242;</span> Alert Notifications</h3>
      <p style="font-size:.7rem;color:var(--muted);margin-bottom:9px;line-height:1.6">Sends emergency alerts to phone/Slack/Discord/email on FAULT detection. 5-min rate limit prevents spam.</p>
      <div class="rep-row"><span class="rep-key">Webhook Active</span><span class="rep-val" id="r-wh">Not configured</span></div>
      <div class="rep-row"><span class="rep-key">Email Active</span><span class="rep-val" id="r-email">Not configured</span></div>
      <p style="margin-top:9px;font-size:.65rem;color:var(--dim);line-height:1.65">
        Enable: <code>--notify-webhook URL</code> (Discord/Slack/Teams)<br>
        or: <code>--notify-email from:to:host[:port[:user:pass]]</code>
      </p>
    </div>

    <div class="rep-card">
      <h3><span>&#127963;</span> For Auditors &amp; Inspectors</h3>
      <p style="font-size:.7rem;color:var(--muted);line-height:1.65;margin-bottom:8px">
        Complete digital record of machine health, fault events, and maintenance history.
        All data is timestamped (epoch) and keyed by hardware MAC address &mdash; impossible to spoof without physical device access.
      </p>
      <ul class="chk-list">
        <li class="chk-li"><span class="chk-icon">&#128203;</span>Alert Log tab: every state-change since startup</li>
        <li class="chk-li"><span class="chk-icon">&#128295;</span>Maintenance tab: technician + service records</li>
        <li class="chk-li"><span class="chk-icon">&#128196;</span>CSV files: per-machine daily sensor data</li>
        <li class="chk-li"><span class="chk-icon">&#128274;</span>HTTP Basic Auth: user-level access control</li>
        <li class="chk-li"><span class="chk-icon">&#128268;</span>Hardware MAC: unique ID per sensor node</li>
      </ul>
    </div>

  </div>
</div>

<!-- MAINTENANCE MODAL -->
<div class="modal-bg" id="maint-modal">
  <div class="modal">
    <div class="modal-hd">
      <span class="modal-title">&#128295; Log Maintenance</span>
      <button class="modal-x" onclick="closeModal()">&#x2715;</button>
    </div>
    <div class="modal-bd">
      <div class="modal-info" id="modal-info">&#8212;</div>
      <input type="hidden" id="modal-mac">
      <div class="fg-row">
        <div class="fg"><label>Last Service Date *</label><input class="fi" type="date" id="f-last"></div>
        <div class="fg"><label>Next Scheduled</label><input class="fi" type="date" id="f-next"></div>
      </div>
      <div class="fg"><label>Technician / Team *</label><input class="fi" type="text" id="f-tech" placeholder="e.g. John Smith / Maintenance Team A"></div>
      <div class="fg">
        <label>Service Type</label>
        <select class="fi" id="f-type">
          <option>Routine Inspection</option>
          <option>Bearing Replacement</option>
          <option>Lubrication Service</option>
          <option>Alignment Check</option>
          <option>Vibration Analysis</option>
          <option>Full Overhaul</option>
          <option>Emergency Repair</option>
          <option>Sensor Calibration</option>
          <option>Other</option>
        </select>
      </div>
      <div class="fg"><label>Notes / Observations</label><textarea class="fi" id="f-notes" rows="3" placeholder="Parts replaced, readings taken, observations&hellip;" style="resize:vertical"></textarea></div>
    </div>
    <div class="modal-ft">
      <button class="btn-sm btn-sm-cancel" onclick="closeModal()">Cancel</button>
      <button class="btn-sm btn-sm-ok" onclick="submitMaint()">Save Record</button>
    </div>
  </div>
</div>

<!-- QR CODE MODAL -->
<div id="qr-modal" onclick="if(event.target===this)closeQR()">
  <div class="qr-box">
    <h3 class="qr-name"></h3>
    <p>Scan to open the live inspection report on any device</p>
    <canvas id="qr-canvas"></canvas>
    <button class="qr-close" onclick="closeQR()">Close</button>
  </div>
</div>

<div id="toast"></div>
<footer id="footer">EPM Dashboard &mdash; Auto-refreshes every 2 s</footer>

<script>
const CH={};let TH={k_warn:6,k_fault:12,cf_warn:5,cf_fault:10,imu_cf_warn:9,imu_cf_fault:18};let lastKey='';let STATUS=null;
let alertsLoaded=false;

const $=id=>document.getElementById(id);
function fmtUp(s){if(s<60)return s+'s';if(s<3600)return Math.floor(s/60)+'m '+String(s%60).padStart(2,'0')+'s';return Math.floor(s/3600)+'h '+String(Math.floor((s%3600)/60)).padStart(2,'0')+'m';}
function fmtDt(ts){return ts?new Date(ts*1000).toLocaleString(undefined,{month:'short',day:'numeric',year:'numeric',hour:'2-digit',minute:'2-digit'}):'never';}
function fmtFuture(days){if(!days&&days!==0)return '';const d=new Date(Date.now()+days*864e5);return d.toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'});}
function kCol(k){return k>TH.k_fault?'var(--fault)':k>TH.k_warn?'var(--warn)':'var(--ok)';}
function hCol(h){return h>=75?'var(--ok)':h>=50?'var(--warn)':'var(--fault)';}
function pfCol(p){return p>=0.95?'var(--fault)':p>=0.70?'var(--warn)':p>=0.30?'#ff9500':'var(--ok)';}
function fmtPf(p){if(p===null||p===undefined)return '—';var s=(p*100).toFixed(1)+'%';if(p>=0.95)return '⚠ '+s;return s;}
function driftCls(s){
  if(!s.last_drift_t)return 'drift-stable';
  var age=(Date.now()/1000)-s.last_drift_t;
  return age<3600?'drift-fresh':age<86400?'drift-recent':'drift-stable';
}
function driftChip(s){
  if(!s.drift_count)return '';
  var cls=driftCls(s);
  var label=cls==='drift-stable'?'Drift: '+s.drift_count+'×':cls==='drift-fresh'?'⟳ Drift now':'⟳ Drift today';
  return '<span class="drift-chip '+cls+'" id="DR_'+s.name+'">'+label+'</span>';
}
function adaptChip(s){
  var ov=s.adapt_overlap||0,av=s.adapt_avg_n||4;
  var label='OV:'+ov+'% AVG:'+av;
  return '<span class="adapt-chip" id="ADP_'+s.name+'" title="Gateway-commanded: FFT overlap='+ov+'%  spectral_avg='+av+'">'+label+'</span>';
}
function mCls(d){return d===0?'fault':d<=30?'warn':'ok';}
function rulCol(s){
  // Colour based on hours remaining (from Kalman) or days*24 (linear fallback)
  var h=s.rul_hours!==undefined&&s.rul_hours!==null?s.rul_hours:((s.rul_days||null)!==null?s.rul_days*24:null);
  if(h===null)return 'var(--ok)';
  var d=h/24;
  return d>90?'var(--ok)':d>30?'var(--warn)':d>7?'#ff9500':'var(--fault)';
}
function fmtRul(s){
  // Kalman path: show "47 h  (CI95: 32-68 h)  conf: 73%"
  var h=s.rul_hours!==undefined?s.rul_hours:null;
  if(h!==null&&h!==undefined){
    if(h<=0)return '⚠ Failure threshold reached';
    var lo=s.rul_ci_low_h,hi=s.rul_ci_high_h,cf=s.rul_confidence;
    var pt=Math.round(h)+' h';
    var ci=(lo!==null&&hi!==null)?' (CI95: '+Math.round(lo)+'–'+Math.round(hi)+' h)':'';
    var cstr=(cf!==null&&cf>0.05)?' conf: '+Math.round(cf*100)+'%':'';
    return pt+ci+cstr;
  }
  // Linear fallback: days
  var d=s.rul_days!==undefined?s.rul_days:null;
  if(d===null||d===undefined)return '✓ Stable';
  if(d<0.05)return '⚠ Threshold reached';
  if(d<1)return '⚠ <1 day';
  if(d<7)return '⚠ ~'+Math.round(d)+' day'+(Math.round(d)!==1?'s':'');
  return '~'+Math.round(d)+' days';
}
function transCls(from,to){const r={OK:0,WARN:1,FAULT:2};return (r[to]||0)>(r[from]||0)?'esc':(r[to]||0)<(r[from]||0)?'rec':'war';}
function fmtZ(v){return(v>=0?'+':'')+v.toFixed(1);}

function toast(msg,ok=true){const t=$('toast');t.textContent=msg;t.className='in '+(ok?'ok-t':'err-t');setTimeout(()=>{t.className='';},3200);}

/* --- fault type helpers --- */
function ftCls(ft){
  if(!ft||ft==='Normal')return 'ok';
  if(ft.includes('Fault')||ft.includes('Severe')||ft.includes('Advanced'))return 'fault';
  if(ft.includes('Looseness')||ft.includes('Misalign')||ft.includes('Anomal')||ft.includes('Elevated'))return 'warn';
  return 'info';
}
function showQR(name,mac){
  const m=$('qr-modal');
  m.querySelector('.qr-name').textContent=name+(mac?' · '+mac:'');
  const url=location.origin+'/api/report?name='+encodeURIComponent(name);
  const canvas=m.querySelector('#qr-canvas');
  canvas.width=0;canvas.height=0;
  if(typeof QRCode!=='undefined'){
    QRCode.toCanvas(canvas,url,{width:220,margin:2,color:{dark:'#111111',light:'#ffffff'}},function(err){if(err)console.error(err);});
  }
  m.className='open';
}
function closeQR(){$('qr-modal').className='';}
document.addEventListener('keydown',function(e){if(e.key==='Escape')closeQR();});

/* --- per-satellite ML helpers --- */
function mlStatusText(ml){
  if(!ml)return '\u{1F9E0} AI: no data';
  if(ml.training)return '\u{1F9E0} AI: Training neural net on Uno Q… (~30 s)';
  if(ml.trained&&ml.active){
    const npu=ml.npu_active;
    const hw=npu?'⚡ NPU':'CPU';
    const be=(ml.backend||'').replace('Qualcomm ','').split(' (')[0];
    return '\u{1F9E0} Autoencoder ['+hw+': '+be+'] ✔ — '+(ml.trained_at||'').slice(0,10);
  }
  if(ml.buf_frames>=ml.buf_target)return '\u{1F9E0} AI: Ready to train ('+ml.buf_frames+' OK frames)';
  return '\u{1F9E0} AI: Collecting baseline — '+ml.buf_frames+'/'+ml.buf_target+' OK frames';
}
async function retrainModel(name){
  try{
    const r=await fetch('/api/train?sat='+encodeURIComponent(name));
    const d=await r.json();
    toast(d.status||d.error||'done',!d.error);
  }catch(e){toast('Error: '+e.message,false);}
}

/* --- card HTML --- */
function cardHTML(s){
  const al=s.alert.toLowerCase(),m=s.metrics||{},ml=s.maint_log||{};
  const hc=hCol(s.health_score),mc=mCls(s.maintenance_days);
  const fd=fmtFuture(s.maintenance_days),due=fd?' · Due: <strong>'+fd+'</strong>':'';
  const lm=ml.last_date||'—',tech=ml.technician?'· '+ml.technician:'';
  return '<div class="card '+al+(s.connected?'':' offline')+'" id="C_'+s.name+'">'
    +'<div class="c-head"><div><div class="c-name">'+s.name+'</div><div class="c-mac">'+s.mac+'</div>'
    +'<div class="c-fw">FW '+s.fw+(s.calibrated?' · ✓ Calibrated':' · ⧖ Calibrating')+'</div></div>'
    +'<div class="c-right"><div class="sdot '+al+'"></div><span class="badge '+al+'">'+s.alert+'</span>'
    +'<span class="ft-badge ft-'+ftCls(s.fault_type||'Normal')+'" id="FT_'+s.name+'">'+(s.fault_type||'Normal')+'</span>'
    +driftChip(s)+adaptChip(s)+'</div></div>'
    +'<div class="c-pstatus" id="PS_'+s.name+'" style="color:'+(al==='fault'?'var(--fault)':al==='warn'?'var(--warn)':'var(--ok)')+';">'
    +(al==='fault'?'⚠ FAULT — Inspect machine immediately':al==='warn'?'⚠ Elevated vibration — attention needed':'Machine running normally')+'</div>'
    +'<div class="c-ml-row" id="ML_'+s.name+'">'+mlStatusText(s.ml_status)+'</div>'
    +'<div class="c-metrics">'
    +'<div class="met"><div class="ml">Vibration Level</div><div class="mv" style="color:'+kCol(m.mic_kurtosis||0)+'" id="K_'+s.name+'">'+(m.mic_kurtosis||0).toFixed(2)+'</div></div>'
    +'<div class="met"><div class="ml">Shock Level</div><div class="mv" id="CF_'+s.name+'">'+(m.mic_crest||0).toFixed(2)+'</div></div>'
    +'<div class="met"><div class="ml">High-Freq %</div><div class="mv" id="HB_'+s.name+'">'+(((m.high_band_ratio||0)*100).toFixed(1))+'%</div></div>'
    +'<div class="met"><div class="ml">Sound Level</div><div class="mv" id="RMS_'+s.name+'">'+(m.mic_rms||0).toFixed(5)+'</div></div>'
    +'<div class="met"><div class="ml">Anomaly Score</div><div class="mv" style="color:'+(s.z_score>3?'var(--fault)':s.z_score>1.5?'var(--warn)':'inherit')+'" id="Z_'+s.name+'">'+s.z_score.toFixed(1)+'</div></div>'
    +'<div class="met"><div class="ml">Data Rate</div><div class="mv" id="FPS_'+s.name+'">'+s.fps.toFixed(1)+' fps</div></div>'
    +'<div class="met sp3"><div class="ml">Est. Remaining Useful Life</div>'
    +'<div class="mv" id="RUL_'+s.name+'" style="color:'+rulCol(s)+';font-size:.8rem">'+fmtRul(s)+'</div></div>'
    +'</div>'
    +'<div class="c-health"><div class="c-hbar-wrap">'
    +'<div class="c-hbar-top"><span>Machine Health</span><span id="HS_'+s.name+'" style="color:'+hc+'">'+s.health_score+'%</span></div>'
    +'<div class="c-hbar"><div class="c-hfill" id="HF_'+s.name+'" style="width:'+s.health_score+'%;background:'+hc+'"></div></div>'
    +'</div></div>'
    +'<div class="c-pfault"><div class="c-pfault-top"><span>Fault Probability (Bayesian)</span>'
    +'<span id="PF_'+s.name+'" style="color:'+pfCol(s.p_fault||0)+'">'+fmtPf(s.p_fault||0)+'</span></div>'
    +'<div class="c-pfbar"><div class="c-pffill" id="PFF_'+s.name+'" style="width:'+Math.round((s.p_fault||0)*100)+'%;background:'+pfCol(s.p_fault||0)+'"></div></div></div>'
    +(al!=='ok'&&s.top_contribs&&s.top_contribs.length
      ?'<div class="c-attr">Driven by: '
        +s.top_contribs.map(function(tc){return '<span class="attr-feat">'+tc[0]+'</span> ('+fmtZ(tc[1])+')';}).join(', ')
        +'</div>'
      :'')
    +'<div class="c-rec '+mc+'" id="MNT_'+s.name+'">'
    +'<div class="c-rec-t">🔧 '+s.maintenance+due+'</div>'
    +'<div class="c-rec-s">Warn: '+s.warn_frames+' · Fault: '+s.fault_frames+(s.last_fault_t?' · Last fault: '+fmtDt(s.last_fault_t):' ')+'</div>'
    +'</div>'
    +'<div class="c-chart"><canvas id="CH_'+s.name+'" height="62"></canvas></div>'
    +'<div class="c-maint-row"><span>🔧 Last maint: <span class="c-maint-val" id="LM_'+s.name+'">'+lm+'</span>'+tech+'</span>'
    +(ml.next_date?'<span>Next: <strong>'+ml.next_date+'</strong></span>':'')
    +'</div>'
    +'<div class="c-actions">'
    +'<button class="btn btn-ghost" onclick="showQR(\''+s.name+'\',\''+s.mac+'\')">&#128247; QR</button>'
    +'<button class="btn btn-blue" onclick="openModal(\''+s.mac+'\',\''+s.name+'\')">&#128221; Log Maintenance</button>'
    +'<button class="btn btn-green" onclick="retrainModel(\''+s.name+'\')">&#129504; Retrain AI</button>'
    +'<a class="btn btn-ghost" href="/api/report?name='+encodeURIComponent(s.name)+'" target="_blank">&#128202; Report</a>'
    +'<a class="btn btn-ghost" href="/api/export?name='+encodeURIComponent(s.name)+'" download>&#8595; CSV</a>'
    +'</div>'
    +'<div class="c-foot">'
    +'<span id="CF2_'+s.name+'">Frames: '+s.frame_count.toLocaleString()+'</span>'
    +'<span id="CF3_'+s.name+'">Up '+fmtUp(s.uptime_s)+'</span>'
    +'<span id="CF4_'+s.name+'">'+(s.connected?'🟢 Online':'🔴 Offline')+'</span>'
    +'</div></div>';
}

/* --- build/update sparkline chart --- */
function buildChart(s){
  const el=$('CH_'+s.name);if(!el)return;
  const h=s.history||{kurtosis:[],crest:[]},n=h.kurtosis.length;
  const wl=Array(n).fill(TH.k_warn),fl=Array(n).fill(TH.k_fault);
  if(CH[s.name]){const c=CH[s.name];c.data.datasets[0].data=h.kurtosis;c.data.datasets[1].data=h.crest;c.data.datasets[2].data=wl;c.data.datasets[3].data=fl;c.update('none');return;}
  CH[s.name]=new Chart(el,{type:'line',data:{labels:Array.from({length:n},(_,i)=>i),datasets:[
    {label:'Kurtosis',data:h.kurtosis,borderColor:'rgba(59,130,246,.9)',backgroundColor:'rgba(59,130,246,.07)',borderWidth:1.5,pointRadius:0,tension:.3,fill:true},
    {label:'Crest',data:h.crest,borderColor:'rgba(245,158,11,.8)',backgroundColor:'rgba(245,158,11,.04)',borderWidth:1.5,pointRadius:0,tension:.3},
    {label:'Warn',data:wl,borderColor:'rgba(245,158,11,.4)',borderWidth:1,pointRadius:0,borderDash:[4,4]},
    {label:'Fault',data:fl,borderColor:'rgba(239,68,68,.4)',borderWidth:1,pointRadius:0,borderDash:[4,4]},
  ]},options:{responsive:true,animation:false,plugins:{legend:{display:true,position:'top',labels:{color:'#5b7a96',font:{size:8},boxWidth:9,padding:6}}},scales:{
    x:{display:false},
    y:{min:0,max:Math.max(TH.k_fault+3,14),ticks:{color:'#2d4d66',font:{size:8}},grid:{color:'rgba(255,255,255,.03)'},border:{color:'#1a2f44'}}
  }}});
}

/* --- update card in-place (no full re-render) --- */
function upCard(s){
  const card=$('C_'+s.name);if(!card)return;
  const al=s.alert.toLowerCase(),m=s.metrics||{},ml=s.maint_log||{};
  const hc=hCol(s.health_score),mc=mCls(s.maintenance_days);
  const fd=fmtFuture(s.maintenance_days),due=fd?' · Due: <strong>'+fd+'</strong>':'';
  card.className='card '+al+(s.connected?'':' offline');
  card.querySelector('.sdot').className='sdot '+al;
  const b=card.querySelector('.badge');b.className='badge '+al;b.textContent=s.alert;
  const ps=$('PS_'+s.name);if(ps){
    ps.textContent=al==='fault'?'⚠ FAULT — Inspect machine immediately':al==='warn'?'⚠ Elevated vibration — attention needed':'Machine running normally';
    ps.style.color=al==='fault'?'var(--fault)':al==='warn'?'var(--warn)':'var(--ok)';
  }
  const g=(id,v)=>{const e=$(id);if(e)e.textContent=v;};
  const gs=(id,p,v)=>{const e=$(id);if(e)e.style[p]=v;};
  g('K_'+s.name,(m.mic_kurtosis||0).toFixed(2));gs('K_'+s.name,'color',kCol(m.mic_kurtosis||0));
  g('CF_'+s.name,(m.mic_crest||0).toFixed(2));
  g('HB_'+s.name,((m.high_band_ratio||0)*100).toFixed(1)+'%');
  g('RMS_'+s.name,(m.mic_rms||0).toFixed(5));
  g('Z_'+s.name,s.z_score.toFixed(1));gs('Z_'+s.name,'color',s.z_score>3?'var(--fault)':s.z_score>1.5?'var(--warn)':'');
  g('FPS_'+s.name,s.fps.toFixed(1)+' fps');
  const mlEl=$('ML_'+s.name);if(mlEl)mlEl.textContent=mlStatusText(s.ml_status);
  const rul=$('RUL_'+s.name);if(rul){rul.textContent=fmtRul(s);rul.style.color=rulCol(s);}
  const hf=$('HF_'+s.name);if(hf){hf.style.width=s.health_score+'%';hf.style.background=hc;}
  gs('HS_'+s.name,'color',hc);g('HS_'+s.name,s.health_score+'%');
  const pff=$('PFF_'+s.name);if(pff){const pc=pfCol(s.p_fault||0);pff.style.width=Math.round((s.p_fault||0)*100)+'%';pff.style.background=pc;}
  const pfEl=$('PF_'+s.name);if(pfEl){pfEl.textContent=fmtPf(s.p_fault||0);pfEl.style.color=pfCol(s.p_fault||0);}
  const drEl=$('DR_'+s.name);
  if(drEl){const dc=driftCls(s);drEl.className='drift-chip '+dc;drEl.textContent=dc==='drift-stable'?'Drift: '+s.drift_count+'×':dc==='drift-fresh'?'⟳ Drift now':'⟳ Drift today';}
  else if(s.drift_count){const cr=document.querySelector('#C_'+s.name+' .c-right');if(cr)cr.insertAdjacentHTML('beforeend',driftChip(s));}
  const adpEl=$('ADP_'+s.name);
  if(adpEl){adpEl.textContent='OV:'+(s.adapt_overlap||0)+'% AVG:'+(s.adapt_avg_n||4);adpEl.title='Gateway-commanded: FFT overlap='+(s.adapt_overlap||0)+'%  spectral_avg='+(s.adapt_avg_n||4);}
  const mnt=$('MNT_'+s.name);
  if(mnt){mnt.className='c-rec '+mc;mnt.innerHTML='<div class="c-rec-t">🔧 '+s.maintenance+due+'</div>'
    +'<div class="c-rec-s">Warn: '+s.warn_frames+' · Fault: '+s.fault_frames+(s.last_fault_t?' · Last fault: '+fmtDt(s.last_fault_t):' ')+'</div>';}
  const ftEl=$('FT_'+s.name);if(ftEl){ftEl.textContent=s.fault_type||'Normal';ftEl.className='ft-badge ft-'+ftCls(s.fault_type||'Normal');}
  g('LM_'+s.name,ml.last_date||'—');
  g('CF2_'+s.name,'Frames: '+s.frame_count.toLocaleString());
  g('CF3_'+s.name,'Up '+fmtUp(s.uptime_s));
  g('CF4_'+s.name,s.connected?'🟢 Online':'🔴 Offline');
}

/* --- tab switching --- */
document.querySelectorAll('.tab').forEach(t=>{
  t.addEventListener('click',()=>{
    document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
    document.querySelectorAll('.pane').forEach(x=>x.classList.remove('active'));
    t.classList.add('active');
    $('pane-'+t.dataset.tab).classList.add('active');
    const tab=t.dataset.tab;
    if(tab==='alerts'&&!alertsLoaded){loadAlerts();alertsLoaded=true;}
    if(tab==='maintenance')loadMaintenance();
    if(tab==='baselines')loadBaselines();
    if(tab==='reports'&&STATUS)updateReports(STATUS);
  });
});

/* --- main 2s refresh --- */
async function refresh(){
  try{
    const r=await fetch('/api/status');
    if(!r.ok){const gs=$('gstatus');if(gs){gs.textContent='⚠ Server error '+r.status;gs.className='chip chip-fault';}return;}

    const d=await r.json();STATUS=d;TH=d.thresholds||TH;
    const sats=d.satellites;
    const ok_n=sats.filter(s=>s.alert==='OK').length;
    const wn_n=sats.filter(s=>s.alert==='WARN').length;
    const ft_n=sats.filter(s=>s.alert==='FAULT').length;
    const avg=sats.length?Math.round(sats.reduce((a,s)=>a+s.health_score,0)/sats.length):null;

    // header
    $('factory-lbl').textContent=d.factory_name||'Factory';
    document.title='EPM · '+(d.factory_name||'Monitor');
    $('hdr-up').textContent=fmtUp(d.server_uptime_s);
    $('hdr-clock').textContent=new Date().toLocaleTimeString();
    const nc=$('notif-chip');
    if(d.notify_active){nc.textContent='🔔 Notify ON';nc.className='chip chip-ok';}
    else{nc.textContent='🔔 Notify OFF';nc.className='chip chip-muted';}
    const gs=$('gstatus');
    if(ft_n>0){gs.textContent='● FAULT';gs.className='chip chip-fault';}
    else if(wn_n>0){gs.textContent='● WARNING';gs.className='chip chip-warn';}
    else if(sats.length){gs.textContent='● ALL OK';gs.className='chip chip-ok';}
    else{gs.textContent='● STANDBY';gs.className='chip chip-muted';}
    const ld=$('live-dot');
    ld.className='ldot '+(ft_n>0?'fault':wn_n>0?'warn':'ok');

    // banner
    const bn=$('banner');
    if(ft_n>0){bn.className='fault';bn.innerHTML='<span class="bpulse">🚨</span> FAULT — '+sats.filter(s=>s.alert==='FAULT').map(s=>s.name).join(', ')+' — Immediate inspection required';}
    else if(wn_n>0){bn.className='warn';bn.innerHTML='⚡ WARNING — '+sats.filter(s=>s.alert==='WARN').map(s=>s.name).join(', ')+' — Elevated vibration detected';}
    else bn.className='';

    // summary tiles
    $('s-conn').textContent=d.satellite_count;
    $('s-ok').textContent=ok_n;
    $('s-warn').textContent=wn_n;
    $('s-fault').textContent=ft_n;
    $('s-fevt').textContent=d.total_faults_today;
    const sh=$('s-health');sh.textContent=avg!==null?avg+'%':'—';sh.style.color=avg!==null?hCol(avg):'';
    const sf=$('s-fstate');
    sf.textContent=ft_n>0?'⚠ Factory alert':wn_n>0?'Factory warning':sats.length?'Factory healthy':'No satellites';
    sf.style.color=ft_n>0?'var(--fault)':wn_n>0?'var(--warn)':'var(--ok)';

    // alert badge on tab
    const ab=$('alert-badge');
    if(ft_n>0){ab.style.display='inline-flex';ab.textContent=ft_n;}else{ab.style.display='none';}

    // cards
    const key=sats.map(s=>s.name).sort().join(',');
    const grid=$('grid');
    if(key!==lastKey){
      Object.values(CH).forEach(c=>c.destroy());
      for(const k in CH)delete CH[k];
      grid.innerHTML=sats.length?sats.map(cardHTML).join(''):'<div class="no-sats"><h2>Waiting for satellites…</h2><p>Power on XIAO ESP32-S3 or run satellite_sim.py</p></div>';
      lastKey=key;
    }else{
      sats.forEach(upCard);
    }
    sats.forEach(buildChart);

    // export list + live report tab
    updateExportList(sats);
    if($('pane-reports').classList.contains('active'))updateReports(d);
    if($('pane-maintenance').classList.contains('active'))renderMaintGrid(sats);

    $('footer').textContent='EPM Gateway · '+(d.factory_name||'EPM')+' · Auto-refresh 2 s · K≥'+TH.k_warn+' WARN / K≥'+TH.k_fault+' FAULT · MIC CF≥'+TH.cf_warn+' WARN / CF≥'+TH.cf_fault+' FAULT · IMU CF≥'+TH.imu_cf_warn+' WARN / CF≥'+TH.imu_cf_fault+' FAULT';
  }catch(e){
    console.warn('[refresh]',e);
    const gs=$('gstatus');if(gs){gs.textContent='⚠ API error';gs.className='chip chip-fault';}
  }
}

/* --- reports tab --- */
function updateReports(d){
  const sats=d.satellites||[];
  const ft_n=sats.filter(s=>s.alert==='FAULT').length;
  const wn_n=sats.filter(s=>s.alert==='WARN').length;
  $('r-factory').textContent=d.factory_name||'—';
  $('r-uptime').textContent=fmtUp(d.server_uptime_s);
  $('r-sats').textContent=sats.length;
  $('r-kth').textContent=TH.k_warn+' / '+TH.k_fault;
  $('r-cfth').textContent=TH.cf_warn+' / '+TH.cf_fault;
  $('r-imucfth').textContent=TH.imu_cf_warn+' / '+TH.imu_cf_fault;
  $('r-notif').textContent=d.notify_active?'Active':'Not configured';
  $('r-wh').textContent=d.notify_active?'Configured':'Not configured';
  $('r-email').textContent=d.notify_active?'Check gateway log':'Not configured';
  const ck=(id,ok)=>{$(id).textContent=ok?'✅':'❌';};
  ck('chk-conn',sats.length>0&&sats.every(s=>s.connected));
  ck('chk-health',ft_n===0&&wn_n===0);
  ck('chk-maint',sats.length>0&&sats.every(s=>s.maint_log&&s.maint_log.last_date));
  ck('chk-auth',false); // can't detect from client side; user must use --auth
  ck('chk-notif',d.notify_active);
  ck('chk-cal',sats.length>0&&sats.every(s=>s.calibrated));
}

function updateExportList(sats){
  const el=$('export-sat-list');
  if(!sats||!sats.length){el.innerHTML='<span style="font-size:.7rem;color:var(--muted)">Connect a satellite to enable CSV exports.</span>';return;}
  el.innerHTML=sats.map(s=>
    '<a class="exp-btn" href="/api/report?name='+encodeURIComponent(s.name)+'" target="_blank">&#128202; '+s.name+' &mdash; Full HTML Report (printable PDF)</a>'
    +'<a class="exp-btn" href="/api/export?name='+encodeURIComponent(s.name)+'" download>&#128196; '+s.name+' &mdash; Latest sensor CSV</a>'
  ).join('');
}

/* --- alert log --- */
async function loadAlerts(){
  const tbody=$('alert-tbody');
  tbody.innerHTML='<tr><td class="empty-cell" colspan="8">Loading…</td></tr>';
  try{
    const r=await fetch('/api/alerts?n=500');const data=await r.json();
    if(!data.length){tbody.innerHTML='<tr><td class="empty-cell" colspan="8">No alert transitions recorded yet. State changes appear here in real time.</td></tr>';return;}
    tbody.innerHTML=data.map(ev=>{
      const dt=new Date(ev.time*1000);
      const dts=dt.toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'})+' '+dt.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit',second:'2-digit'});
      if(ev.event_type==='BASELINE_REFRESH'){
        return '<tr class="ev-refresh">'
          +'<td style="font-size:.65rem;color:var(--muted);font-family:monospace;white-space:nowrap">'+dts+'</td>'
          +'<td style="font-weight:700">'+ev.satellite+'</td>'
          +'<td colspan="5" style="font-size:.7rem;color:var(--muted);font-style:italic">'
          +'<span style="margin-right:6px">&#x21BA;</span>'+ev.detail+'</td>'
          +'<td style="font-size:.6rem;color:var(--muted);font-family:monospace">'+(ev.mac||'—')+'</td>'
          +'</tr>';
      }
      if(ev.event_type==='ADAPT'){
        return '<tr class="ev-adapt">'
          +'<td style="font-size:.65rem;color:var(--muted);font-family:monospace;white-space:nowrap">'+dts+'</td>'
          +'<td style="font-weight:700">'+ev.satellite+'</td>'
          +'<td colspan="5" style="font-size:.7rem;color:#10b981;font-family:monospace">'
          +'<span style="margin-right:6px">&#x25B6;</span>'+ev.detail+'</td>'
          +'<td style="font-size:.6rem;color:var(--muted);font-family:monospace">'+(ev.mac||'—')+'</td>'
          +'</tr>';
      }
      const tc=transCls(ev.prev,ev.alert);
      return '<tr>'
        +'<td style="font-size:.65rem;color:var(--muted);font-family:monospace;white-space:nowrap">'+dts+'</td>'
        +'<td style="font-weight:700">'+ev.satellite+'</td>'
        +'<td><span class="trans '+tc+'">'+ev.prev+' → '+ev.alert+'</span></td>'
        +'<td style="font-family:monospace;color:'+(ev.kurtosis>TH.k_fault?'var(--fault)':ev.kurtosis>TH.k_warn?'var(--warn)':'')+'">'+ev.kurtosis.toFixed(2)+'</td>'
        +'<td style="font-family:monospace">'+ev.crest.toFixed(2)+'</td>'
        +'<td style="font-family:monospace;color:'+(ev.z_score>3?'var(--fault)':ev.z_score>1.5?'var(--warn)':'')+'">'+ev.z_score.toFixed(1)+'</td>'
        +'<td style="font-size:.68rem;color:#fbbf24;font-family:monospace">'+(ev.reason||'—')+'</td>'
        +'<td style="font-size:.6rem;color:var(--muted);font-family:monospace">'+(ev.mac||'—')+'</td>'
        +'</tr>';
    }).join('');
    alertsLoaded=true;
  }catch(e){tbody.innerHTML='<tr><td class="empty-cell" colspan="8">Error: '+e.message+'</td></tr>';}
}

async function exportAlerts(){
  try{
    const r=await fetch('/api/alerts?n=1000');const data=await r.json();
    const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'});
    const a=document.createElement('a');a.href=URL.createObjectURL(blob);
    a.download='epm_alert_log_'+new Date().toISOString().slice(0,10)+'.json';a.click();
    toast('Alert log exported ✓');
  }catch(e){toast('Export failed: '+e.message,false);}
}

/* --- baselines tab --- */
function loadBaselines(){
  if(!STATUS||!STATUS.satellites){return;}
  const el=$('baselines-grid');
  const sats=STATUS.satellites;
  if(!sats.length){el.innerHTML='<p style="color:var(--muted);font-size:.76rem;padding:14px">No satellites connected yet.</p>';return;}
  const THL=STATUS.thresholds||{};
  const ws=THL.z_warn_sigma||4,fs=THL.z_fault_sigma||6;
  let html='';
  for(const s of sats){
    const bl=s.baselines||{};
    html+=`<div class="bl-section"><div class="bl-sat-hdr">${s.name}<span class="bl-sat-mac">${s.mac}</span></div>
<table class="bl-tbl"><thead><tr><th>Feature</th><th>Mean</th><th>Std Dev</th><th>Warn (${ws}σ)</th><th>Fault (${fs}σ)</th><th>OK Frames</th></tr></thead><tbody>`;
    const feats=[['Kurtosis','kurtosis'],['Crest Factor','crest'],['RMS','rms'],['High-Band Energy','hb']];
    for(const[fname,fk]of feats){
      const fd=bl[fk];
      if(!fd){
        html+=`<tr><td>${fname}</td><td class="bl-warm" colspan="5">warming up&hellip;</td></tr>`;
      }else{
        html+=`<tr><td>${fname}</td><td>${fd.mean.toFixed(4)}</td><td>${fd.std.toFixed(4)}</td><td>${fd.warn_4s.toFixed(4)}</td><td>${fd.fault_6s.toFixed(4)}</td><td>${fd.n.toLocaleString()}</td></tr>`;
      }
    }
    html+='</tbody></table></div>';
  }
  el.innerHTML=html;
}

/* --- maintenance tab --- */
async function loadMaintenance(){
  try{
    const r=await fetch('/api/maintenance');const data=await r.json();
    if(STATUS)renderMaintGrid(STATUS.satellites,data);
  }catch(e){console.warn('[maint]',e);}
}

function renderMaintGrid(sats,maintData){
  const mg=$('maint-grid');
  if(!sats||!sats.length){mg.innerHTML='<p style="color:var(--muted);font-size:.76rem">No satellites connected.</p>';return;}
  mg.className='maint-grid';
  mg.innerHTML=sats.map(s=>{
    const ml=(maintData&&maintData[s.mac])||s.maint_log||{};
    const has=ml&&ml.last_date;
    return '<div class="mc">'
      +'<div class="mc-name">'+s.name+'<span class="badge '+s.alert.toLowerCase()+'">'+s.alert+'</span></div>'
      +(has
        ?'<div class="mc-row"><span class="mc-lbl">Last Service</span><span class="mc-val">'+ml.last_date+'</span></div>'
         +'<div class="mc-row"><span class="mc-lbl">Technician</span><span class="mc-val">'+(ml.technician||'—')+'</span></div>'
         +'<div class="mc-row"><span class="mc-lbl">Type</span><span class="mc-val">'+(ml.maint_type||'—')+'</span></div>'
         +'<div class="mc-row"><span class="mc-lbl">Next Scheduled</span><span class="mc-val">'+(ml.next_date||'—')+'</span></div>'
         +(ml.notes?'<div class="mc-row" style="flex-direction:column;gap:3px"><span class="mc-lbl">Notes</span><span style="font-size:.68rem;color:var(--muted);margin-top:2px">'+ml.notes+'</span></div>':'')
         +'<div class="mc-row"><span class="mc-lbl">Updated</span><span class="mc-val" style="font-size:.62rem;color:var(--muted)">'+fmtDt(ml.updated_at)+'</span></div>'
        :'<div class="mc-empty">No maintenance record yet.</div>')
      +'<div class="mc-foot"><button class="btn btn-blue" style="width:100%" onclick="openModal(\''+s.mac+'\',\''+s.name+'\')">&#128221; Log Maintenance</button></div>'
      +'</div>';
  }).join('');
}

/* --- modal --- */
function openModal(mac,name){
  $('modal-mac').value=mac;
  $('modal-info').textContent=name+' · '+mac;
  if(STATUS){
    const s=STATUS.satellites.find(x=>x.mac===mac);
    const ml=(s&&s.maint_log)||{};
    $('f-last').value=ml.last_date||new Date().toISOString().slice(0,10);
    $('f-next').value=ml.next_date||'';
    $('f-tech').value=ml.technician||'';
    $('f-type').value=ml.maint_type||'Routine Inspection';
    $('f-notes').value=ml.notes||'';
  }
  $('maint-modal').classList.add('open');
  setTimeout(()=>$('f-tech').focus(),50);
}
function closeModal(){$('maint-modal').classList.remove('open');}
$('maint-modal').addEventListener('click',e=>{if(e.target===$('maint-modal'))closeModal();});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal();});

async function submitMaint(){
  const mac=$('modal-mac').value;
  const lastDate=$('f-last').value,tech=$('f-tech').value.trim();
  if(!mac||!lastDate||!tech){toast('Last date and technician are required.',false);return;}
  try{
    const r=await fetch('/api/maintenance',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({mac,last_date:lastDate,technician:tech,maint_type:$('f-type').value,notes:$('f-notes').value.trim(),next_date:$('f-next').value})});
    const d=await r.json();
    if(d.ok){toast('Maintenance record saved ✓');closeModal();loadMaintenance();refresh();}
    else toast('Save failed: '+(d.error||'unknown'),false);
  }catch(e){toast('Error: '+e.message,false);}
}

refresh();
setInterval(refresh,2000);
setInterval(()=>{if($('pane-alerts').classList.contains('active'))loadAlerts();},12000);
</script>
</body>
</html>
</html>"""


def _sat_health(sat):
    """Compute 0–100 health score, maintenance recommendation, and RUL estimate.

    Returns (health_score, maintenance_str, maintenance_days, rul_days, rul_result).

    rul_days   — days until fault threshold (linear fallback or Kalman/24, for compat)
    rul_result — RULResult from ExponentialRUL Kalman filter (None if not converged)
    """
    import numpy as np
    import recv_verify as _rv
    total = max(sat.frame_count, 1)
    score = 100.0
    score -= (sat.warn_frames  / total) * 25.0   # max −25 for sustained WARN
    score -= (sat.fault_frames / total) * 60.0   # max −60 for sustained FAULT
    score -= min(sat.last_z * 3.0, 15.0)          # max −15 for high z-score
    score  = max(0.0, min(100.0, score))

    if sat.fault_frames > 0 and score < 40:
        maint, days = "CRITICAL — Immediate inspection required", 0
    elif score < 50:
        maint, days = "DEGRADED — Inspect within 7 days", 7
    elif score < 70:
        maint, days = "MONITOR — Schedule inspection within 30 days", 30
    elif score < 85:
        maint, days = "GOOD — Routine inspection within 90 days", 90
    else:
        maint, days = "EXCELLENT — Routine inspection in 180 days", 180

    # ── Kalman exponential RUL (preferred) ───────────────────────────────────
    rul_result = getattr(sat, 'rul_result', None)
    if (rul_result is not None
            and not math.isinf(rul_result.hours_remaining)
            and rul_result.confidence > 0.05):
        rul_days = max(0.0, round(rul_result.hours_remaining / 24.0, 1))
        return round(score, 1), maint, days, rul_days, rul_result

    # ── Linear fallback (history window regression) ───────────────────────────
    rul_days = None
    hist = list(sat.history_kurtosis)
    n    = len(hist)
    if n >= 10 and sat.fps > 0.01:
        xs    = np.arange(n, dtype=np.float64)
        ys    = np.array(hist, dtype=np.float64)
        slope = float(np.polyfit(xs, ys, 1)[0])
        current_k = float(ys[-1])
        if slope > 0.005 and current_k < _rv.K_FAULT:
            frames_to_fault = (_rv.K_FAULT - current_k) / slope
            rul_seconds     = frames_to_fault / sat.fps
            rul_days        = max(0.0, round(rul_seconds / 86400.0, 1))

    return round(score, 1), maint, days, rul_days, rul_result


def _safe_f(v, default=0.0):
    """Return float v if finite; replace NaN/Inf with default so json.dumps never raises."""
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _ab_summary(ab, z_warn: float = 4.0, z_fault: float = 6.0):
    """Return baseline statistics dict for JSON API, or None if not warmed up."""
    import recv_verify as _rv
    if ab is None or ab.n_updates < _rv.AB_WARMUP_FRAMES:
        return None
    return {
        'mean':    round(ab.mean, 6),
        'std':     round(ab.std, 6),
        'warn_4s': round(ab.warn_threshold(z_warn), 6),
        'fault_6s': round(ab.fault_threshold(z_fault), 6),
        'n':       ab.n_updates,
    }


def _top_contribs(sat, k: int = 3) -> list:
    """Return [[name, logit], ...] for top-k fault-driving features (JSON-serialisable)."""
    import recv_verify as _rv
    fz = getattr(sat, 'feat_z', {})
    if not fz:
        return []
    if _rv._FUSION_AVAILABLE and _rv._bayesian_fusion is not None:
        return [[n, round(v, 2)] for n, v in _rv._bayesian_fusion.attribute(fz, top_k=k)]
    return [[n, round(v, 2)]
            for n, v in sorted(fz.items(), key=lambda x: x[1], reverse=True)[:k]]


def _build_status_json():
    import recv_verify as _rv
    now = time.time()
    with _sat_lock:
        sats = list(_satellites.values())

    sat_list = []
    for s in sats:
        health, maint, maint_days, rul_days, rul_result = _sat_health(s)
        m = {}
        if s.last_frame:
            m = {
                'mic_rms':         round(_safe_f(s.last_frame.get('mic_rms',  0)), 6),
                'mic_kurtosis':    round(_safe_f(s.last_frame.get('mic_kurtosis', 0)), 2),
                'mic_crest':       round(_safe_f(s.last_frame.get('mic_crest', 0)), 2),
                'imu_rms':         round(_safe_f(s.last_frame.get('imu_rms',  0)), 5),
                'imu_crest':       round(_safe_f(s.last_frame.get('imu_crest', 0)), 2),
                'high_band_ratio': round(_safe_f(s.last_hb), 3),
            }
        with _rv._MAINT_LOG_LOCK:
            maint_rec = dict(_rv._MAINT_LOG.get(s.mac_hex, {}))
        sat_list.append({
            'name':             s.name,
            'mac':              s.mac_hex,
            'fw':               s.fw_str(),
            'alert':            ['OK', 'WARN', 'FAULT'][min(int(s.sent_alert), 2)],
            'connected':        s.connected,
            'uptime_s':         int(now - s.connect_t),
            'frame_count':      s.frame_count,
            'fps':              round(_safe_f(s.fps), 1),
            'calibrated':       s.calibrated,
            'health_score':     health,
            'maintenance':      maint,
            'maintenance_days': maint_days,
            'rul_days':         rul_days,
            # Kalman RUL fields (None until filter has converged — n_updates >= 30
            # and lambda > 0; inf means no degradation detected)
            'rul_hours':      None if (rul_result is None or math.isinf(rul_result.hours_remaining))
                              else round(rul_result.hours_remaining, 1),
            'rul_ci_low_h':   None if (rul_result is None or math.isinf(rul_result.hours_low))
                              else round(rul_result.hours_low, 1),
            'rul_ci_high_h':  None if (rul_result is None or math.isinf(rul_result.hours_high))
                              else round(rul_result.hours_high, 1),
            'rul_confidence': None if rul_result is None
                              else round(rul_result.confidence, 3),
            'rul_lambda':     None if rul_result is None
                              else round(rul_result.lambda_hat, 6),
            'warn_frames':    s.warn_frames,
            'fault_frames':     s.fault_frames,
            'last_fault_t':     s.last_fault_t,
            'z_score':          round(_safe_f(s.last_z), 2),
            'p_fault':          round(_safe_f(getattr(s, 'p_fault', 0.0)), 4),
            'drift_count':      getattr(s, 'drift_count', 0),
            'last_drift_t':     getattr(s, 'last_drift_t', None),
            'adapt_overlap':    getattr(s, 'adapt_overlap', 0),
            'adapt_avg_n':      getattr(s, 'adapt_avg_n', 4),
            'baselines': {
                'kurtosis': _ab_summary(getattr(s, 'ab_kurtosis', None),
                                        _rv.Z_WARN_SIGMA, _rv.Z_FAULT_SIGMA),
                'crest':    _ab_summary(getattr(s, 'ab_crest', None),
                                        _rv.Z_WARN_SIGMA, _rv.Z_FAULT_SIGMA),
                'rms':      _ab_summary(getattr(s, 'ab_rms', None),
                                        _rv.Z_WARN_SIGMA, _rv.Z_FAULT_SIGMA),
                'hb':       _ab_summary(getattr(s, 'ab_hb', None),
                                        _rv.Z_HB_SIGMA, _rv.Z_HB_SIGMA),
            },
            'feat_z':       {k: round(v, 3)
                             for k, v in getattr(s, 'feat_z', {}).items()},
            'top_contribs': _top_contribs(s),
            'fault_type':       s.fault_type,
            'metrics':          m,
            'maint_log':        maint_rec,
            'ml_status': {
                'trained':    s.ml_trained,
                'training':   s.ml_training,
                'trained_at': s.ml_trained_at,
                'buf_frames': len(s.ml_buf),
                'buf_target': _rv.N_TRAIN_FRAMES,
                'active':     s.mac_hex in _rv._sat_models,
                'backend':    getattr(s, 'ml_backend', 'none'),
                'npu_active': 'NPU' in getattr(s, 'ml_backend', ''),
            },
            'history': {
                'alerts':   list(s.history_alerts),
                'kurtosis': [round(_safe_f(v), 2) for v in s.history_kurtosis],
                'crest':    [round(_safe_f(v), 2) for v in s.history_crest],
            },
        })

    return json.dumps({
        'factory_name':       _rv._FACTORY_NAME,
        'server_uptime_s':    int(now - _rv._SERVER_START_T),
        'timestamp':          now,
        'satellite_count':    sum(1 for s in sat_list if s['connected']),
        'total_faults_today': sum(s['fault_frames'] for s in sat_list),
        'notify_active':      bool(_rv._NOTIFY_WEBHOOK or _rv._NOTIFY_EMAIL_CFG),
        'thresholds': {
            'k_warn':       _rv.K_WARN, 'k_fault': _rv.K_FAULT,
            'cf_warn':      _rv.CREST_WARN, 'cf_fault': _rv.CREST_FAULT,
            'imu_cf_warn':  _rv.IMU_CREST_WARN, 'imu_cf_fault': _rv.IMU_CREST_FAULT,
            'z_warn_sigma': _rv.Z_WARN_SIGMA, 'z_fault_sigma': _rv.Z_FAULT_SIGMA,
        },
        'satellites': sat_list,
    })


class _DashHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    # ── Auth ──────────────────────────────────────────────────────────────────
    def _auth_ok(self):
        import recv_verify as _rv
        if _rv._AUTH_PASS is None:
            return True
        auth = self.headers.get('Authorization', '')
        if not auth.startswith('Basic '):
            return False
        try:
            # b64decode/.decode()/unpacking-split all raise subclasses of
            # ValueError for malformed input; narrowed from Exception so a
            # real bug here surfaces as a traceback instead of just another
            # silently-denied login.
            user, pw = base64.b64decode(auth[6:]).decode().split(':', 1)
            return user == (_rv._AUTH_USER or 'admin') and pw == _rv._AUTH_PASS
        except ValueError:
            return False

    def _require_auth(self):
        if self._auth_ok():
            return True
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="EPM Dashboard"')
        self.send_header('Content-Length', '0')
        self.end_headers()
        return False

    # ── Response helpers ──────────────────────────────────────────────────────
    def _send(self, code, ctype, body):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, ctype, download_name=None):
        with open(path, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Access-Control-Allow-Origin', '*')
        if download_name:
            self.send_header('Content-Disposition',
                             f'attachment; filename="{download_name}"')
        self.end_headers()
        self.wfile.write(data)

    # ── GET ───────────────────────────────────────────────────────────────────
    def do_GET(self):
        import recv_verify as _rv
        if not self._require_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        qs     = urllib.parse.parse_qs(parsed.query)

        if path in ('/', '/index.html'):
            self._send(200, 'text/html; charset=utf-8', _DASHBOARD_HTML)

        elif path == '/api/status':
            try:
                self._send(200, 'application/json', _build_status_json())
            except Exception as exc:
                fallback = json.dumps({
                    'error': str(exc), 'satellites': [],
                    'satellite_count': 0, 'total_faults_today': 0,
                    'factory_name': _rv._FACTORY_NAME, 'server_uptime_s': 0,
                    'notify_active': False,
                    'thresholds': {'k_warn': _rv.K_WARN, 'k_fault': _rv.K_FAULT,
                                   'cf_warn': _rv.CREST_WARN, 'cf_fault': _rv.CREST_FAULT,
                                   'imu_cf_warn': _rv.IMU_CREST_WARN, 'imu_cf_fault': _rv.IMU_CREST_FAULT},
                })
                self._send(200, 'application/json', fallback)
                import traceback
                print(f'[dash] /api/status error: {exc}\n{traceback.format_exc()}')

        elif path == '/api/alerts':
            n = int(qs.get('n', ['200'])[0])
            with _rv._ALERT_HISTORY_LOCK:
                data = list(_rv._ALERT_HISTORY)[:n]
            self._send(200, 'application/json', json.dumps(data))

        elif path == '/api/maintenance':
            with _rv._MAINT_LOG_LOCK:
                self._send(200, 'application/json', json.dumps(_rv._MAINT_LOG))

        elif path == '/api/export':
            name     = qs.get('name', [''])[0]
            log_dir  = os.path.join(_rv._BASE_DIR, 'logs')
            csv_root = os.path.join(log_dir, 'csv')
            pattern  = f'epm_{name}_*.csv' if name else 'epm_*.csv'
            # Search new dated-subdirectory tree first, then legacy flat logs/ dir
            files = sorted(
                glob.glob(os.path.join(csv_root, '**', pattern), recursive=True)
                + glob.glob(os.path.join(log_dir, pattern)),
                reverse=True,
            )
            if files:
                self._send_file(files[0], 'text/csv', os.path.basename(files[0]))
            else:
                self._send(404, 'text/plain', 'No CSV data found for this satellite')

        elif path == '/api/train':
            sat_name = qs.get('sat', [''])[0].strip()
            with _sat_lock:
                target = next((s for s in _satellites.values()
                               if s.name == sat_name or s.mac_hex == sat_name), None)
            if not target:
                self._send(404, 'application/json', '{"error":"satellite not found"}')
                return
            if target.ml_training:
                self._send(200, 'application/json', '{"status":"training in progress"}')
                return
            n_buf      = len(target.ml_buf)
            min_frames = _rv.N_TRAIN_FRAMES // 2
            if n_buf < min_frames:
                self._send(400, 'application/json',
                           json.dumps({'error': f'need {min_frames} OK frames, have {n_buf}'}))
                return
            _rv._trigger_sat_training(target)
            self._send(200, 'application/json',
                       json.dumps({'status': 'training started', 'n_samples': n_buf}))

        elif path == '/api/report':
            sat_name   = qs.get('name', [''])[0] or None
            report_html = _rv._generate_report_html(sat_name)
            self._send(200, 'text/html; charset=utf-8', report_html)

        elif path == '/manifest.json':
            manifest = json.dumps({
                'name': f'EPM Monitor — {_rv._FACTORY_NAME}',
                'short_name': 'EPM',
                'description': 'EdgeAI Predictive Maintenance Dashboard',
                'start_url': '/',
                'display': 'standalone',
                'background_color': '#07111e',
                'theme_color': '#07111e',
                'icons': [
                    {'src': 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">⚙</text></svg>',
                     'sizes': 'any', 'type': 'image/svg+xml', 'purpose': 'any maskable'}
                ],
            })
            self._send(200, 'application/manifest+json', manifest)

        else:
            self._send(404, 'text/plain', 'Not found')

    # ── POST ──────────────────────────────────────────────────────────────────
    def do_POST(self):
        import recv_verify as _rv
        if not self._require_auth():
            return
        path   = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length)

        if path == '/api/maintenance':
            try:
                data = json.loads(body)
                mac  = data.get('mac', '').strip()
                if not mac:
                    self._send(400, 'application/json', '{"error":"mac required"}')
                    return
                record = {
                    'last_date':  data.get('last_date', ''),
                    'technician': data.get('technician', ''),
                    'maint_type': data.get('maint_type', 'Routine Inspection'),
                    'notes':      data.get('notes', ''),
                    'next_date':  data.get('next_date', ''),
                    'updated_at': time.time(),
                }
                with _rv._MAINT_LOG_LOCK:
                    _rv._MAINT_LOG[mac] = record
                # Persist to SQLite (primary) or JSON fallback
                if _rv._storage is not None:
                    try:
                        _rv._storage.log_maintenance(
                            mac,
                            record['technician'],
                            record['maint_type'],
                            json.dumps(record),   # full record as JSON in notes field
                        )
                    except Exception as e:
                        print(f'[maint] DB write failed: {e}')
                        _rv._save_maint_log()
                else:
                    _rv._save_maint_log()
                # Reset baseline so the satellite re-calibrates on known-good
                # post-service data rather than pre-service degraded data.
                with _sat_lock:
                    sat = _satellites.get(mac)
                    if sat:
                        sat.calibrated = False
                        sat._cal_buf   = []
                        sat.bl_mean    = None
                        sat.bl_std     = None
                        sat.fault_type = "Normal"
                print(f'[maint] Record updated: {mac}  by {record["technician"]}')
                self._send(200, 'application/json', '{"ok":true}')
            except Exception as e:
                self._send(400, 'application/json', json.dumps({'error': str(e)}))
        else:
            self._send(404, 'text/plain', 'Not found')


def start_dashboard(port=8080):
    import recv_verify as _rv
    srv = HTTPServer(('0.0.0.0', port), _DashHandler)
    threading.Thread(target=srv.serve_forever, daemon=True, name='dashboard').start()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        lan_ip = s.getsockname()[0]
        s.close()
    except OSError:
        lan_ip = 'localhost'
    auth_note = f'  auth: {_rv._AUTH_USER or "admin"} / [password]' if _rv._AUTH_PASS else '  (no auth — set --auth user:pass)'
    notify_note = f'  notifications: webhook active' if _rv._NOTIFY_WEBHOOK else (
                  f'  notifications: email active' if _rv._NOTIFY_EMAIL_CFG else
                  f'  notifications: OFF (use --notify-webhook or --notify-email)')
    # VERIFY-FIX: replace Unicode arrows with ASCII to avoid cp1252 encode error on Windows.
    print(f"[dashboard] http://localhost:{port}/  <- this machine")
    print(f"[dashboard] http://{lan_ip}:{port}/  <- phone / LAN")
    print(f"[dashboard]{auth_note}")
    print(f"[dashboard]{notify_note}")
    print(f"[dashboard] Firewall (elevated PowerShell, run once):")
    print(f"            New-NetFirewallRule -DisplayName EPM-Dash -Direction Inbound "
          f"-Protocol TCP -LocalPort {port} -Action Allow")
