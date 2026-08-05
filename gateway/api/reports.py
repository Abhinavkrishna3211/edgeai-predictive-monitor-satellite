"""gateway/api/reports.py — print-ready HTML inspection report generator,
extracted from recv_verify.py (Phase 8b2 task 1).

Imports _sat_lock/_satellites directly from gateway.registry.satellite_state
and _sat_health directly from gateway.api.dashboard — both are plain,
dependency-free helpers at module load time (dashboard.py does not import
this module, so there is no import cycle). Everything else this file touches
that is owned by recv_verify.py (`_ALERT_HISTORY`, `_MAINT_LOG`, `K_WARN`,
`_FACTORY_NAME`, ...) is read through a *lazy* `import recv_verify as _rv`
inside the function body instead, for the same reason given in
gateway/api/notifications.py's module docstring: those names are CLI-mutable
via recv_verify.main()'s `global` statements, and only a live module-attribute
read tracks reassignment.
"""

import datetime
import math
import time

from gateway.registry.satellite_state import _sat_lock, _satellites
from gateway.api.dashboard import _sat_health

def _generate_report_html(sat_name=None):
    """Generate a professional, print-ready HTML inspection report from live data."""
    import html as _html
    import recv_verify as _rv

    def esc(s):
        return _html.escape(str(s))

    def fmt_dt(ts):
        if not ts:
            return 'N/A'
        return datetime.datetime.fromtimestamp(ts).strftime('%b %d, %Y  %H:%M')

    def fmt_up(s):
        if s < 60:
            return f'{s}s'
        if s < 3600:
            return f'{s//60}m {s%60:02d}s'
        return f'{s//3600}h {(s%3600)//60:02d}m'

    def fmt_rul(r):
        """Format RUL for HTML report.  r is the sat_rows dict (has rul_result)."""
        rr = r.get('rul_result') if isinstance(r, dict) else None
        if rr is not None and not math.isinf(rr.hours_remaining) and rr.confidence > 0.05:
            h   = round(rr.hours_remaining, 1)
            lo  = round(rr.hours_low,  1) if not math.isinf(rr.hours_low)  else None
            hi  = round(rr.hours_high, 1) if not math.isinf(rr.hours_high) else None
            ci  = f'  CI95: {lo}–{hi} h' if lo is not None else ''
            cst = f'  conf: {round(rr.confidence * 100)}%' if rr.confidence > 0.05 else ''
            return f'{h} h{ci}{cst}'
        # Linear fallback
        d = r.get('rul_days') if isinstance(r, dict) else r
        if d is None:
            return 'Stable — no upward trend'
        if d < 0.05:
            return '⚠ Fault threshold reached'
        if d < 1:
            return '⚠ < 1 day'
        if d < 30:
            return f'~{round(d)} days'
        return f'~{round(d)} days (stable)'

    def status_badge(a):
        cfg = {'OK': ('#166534','#dcfce7'), 'WARN': ('#92400e','#fef3c7'), 'FAULT': ('#991b1b','#fee2e2')}
        fg, bg = cfg.get(a, ('#374151','#f3f4f6'))
        return f'<span style="background:{bg};color:{fg};padding:2px 10px;border-radius:4px;font-weight:700;font-size:.78rem;border:1px solid {fg}30">{a}</span>'

    def health_bar(h):
        c = '#16a34a' if h >= 75 else '#d97706' if h >= 50 else '#dc2626'
        return (f'<span style="display:inline-flex;align-items:center;gap:8px">'
                f'<span style="display:inline-block;width:120px;height:7px;background:#e5e7eb;border-radius:4px;overflow:hidden">'
                f'<span style="display:block;width:{h}%;height:100%;background:{c};border-radius:4px"></span></span>'
                f'<strong style="color:{c}">{h}%</strong></span>')

    now_ts  = time.time()
    now_str = datetime.datetime.now().strftime('%A, %B %d, %Y at %H:%M:%S')

    with _sat_lock:
        all_sats = list(_satellites.values())
    sats = [s for s in all_sats if s.name == sat_name] if sat_name else all_sats

    with _rv._ALERT_HISTORY_LOCK:
        all_alerts = list(_rv._ALERT_HISTORY)
    with _rv._MAINT_LOG_LOCK:
        maint = dict(_rv._MAINT_LOG)

    sat_rows = []
    for s in sats:
        health, maint_str, maint_days, rul_days, rul_result = _sat_health(s)
        ml = maint.get(s.mac_hex, {})
        m  = s.last_frame or {}
        sat_alerts = [ev for ev in all_alerts if ev['satellite'] == s.name]
        sat_rows.append(dict(s=s, health=health, maint_str=maint_str,
                             maint_days=maint_days, rul_days=rul_days,
                             rul_result=rul_result,
                             ml=ml, m=m, alerts=sat_alerts))

    fault_n  = sum(1 for r in sat_rows if r['s'].sent_alert == _rv.EPM_ALERT_FAULT)
    warn_n   = sum(1 for r in sat_rows if r['s'].sent_alert == _rv.EPM_ALERT_WARN)
    ok_n     = len(sat_rows) - fault_n - warn_n
    avg_h    = round(sum(r['health'] for r in sat_rows) / len(sat_rows), 1) if sat_rows else 0
    tot_fault_evts = sum(r['s'].fault_frames for r in sat_rows)
    tot_warn_evts  = sum(r['s'].warn_frames  for r in sat_rows)
    scope_alerts   = [ev for ev in all_alerts if not sat_name or ev['satellite'] == sat_name]

    if fault_n > 0:
        risk, risk_c, risk_bg = 'HIGH', '#dc2626', '#fef2f2'
    elif warn_n > 0:
        risk, risk_c, risk_bg = 'MEDIUM', '#d97706', '#fffbeb'
    elif avg_h >= 75:
        risk, risk_c, risk_bg = 'LOW', '#16a34a', '#f0fdf4'
    else:
        risk, risk_c, risk_bg = 'MEDIUM', '#d97706', '#fffbeb'

    h = []
    W = h.append
    title = f'EPM Report — {esc(sat_name) if sat_name else "All Machines"}'

    W(f'''<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><title>{title}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;color:#1f2937;background:#fff;font-size:13px;line-height:1.65}}
@media print{{.no-print{{display:none!important}}body{{font-size:11px}}.page-break{{page-break-before:always}}h2{{page-break-after:avoid}}}}
.cover{{background:linear-gradient(135deg,#0f172a 0%,#1e3a5f 100%);color:#fff;padding:36px 40px 30px}}
.cover h1{{font-size:1.55rem;font-weight:700;margin-bottom:3px}}
.cover .sub{{font-size:.78rem;opacity:.72;margin-top:5px}}
.cover-meta{{display:flex;flex-wrap:wrap;gap:24px;margin-top:18px;padding-top:18px;border-top:1px solid rgba(255,255,255,.15)}}
.cover-stat{{font-size:.72rem;opacity:.75}}.cover-stat strong{{display:block;font-size:.95rem;color:#fff;opacity:1;margin-bottom:1px}}
.body{{padding:28px 36px}}
.section{{margin-bottom:28px}}
h2{{font-size:.88rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:#374151;border-bottom:2px solid #e5e7eb;padding-bottom:6px;margin-bottom:14px;display:flex;align-items:center;gap:8px}}
.risk-box{{border:2px solid;border-radius:8px;padding:13px 16px;margin-bottom:16px;display:flex;align-items:flex-start;gap:12px}}
.risk-icon{{font-size:1.4rem;flex-shrink:0;margin-top:1px}}
.risk-label{{font-size:1rem;font-weight:800;margin-bottom:3px}}
.risk-note{{font-size:.78rem;color:#374151;line-height:1.55}}
.kpi-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:9px;margin-bottom:16px}}
.kpi{{border:1px solid #e5e7eb;border-radius:8px;padding:11px 12px;text-align:center}}
.kpi-val{{font-size:1.75rem;font-weight:800;line-height:1;margin:3px 0}}
.kpi-lbl{{font-size:.6rem;text-transform:uppercase;letter-spacing:.07em;color:#6b7280}}
table{{width:100%;border-collapse:collapse;font-size:.8rem;margin-bottom:6px}}
th{{background:#f3f4f6;padding:7px 10px;text-align:left;font-size:.65rem;text-transform:uppercase;letter-spacing:.07em;color:#6b7280;border:1px solid #e5e7eb;font-weight:600}}
td{{padding:7px 10px;border:1px solid #e5e7eb;vertical-align:top}}
tr:nth-child(even) td{{background:#f9fafb}}
.sat-card{{border:1px solid #e5e7eb;border-radius:10px;margin-bottom:20px;overflow:hidden}}
.sat-card-head{{background:#f8fafc;padding:11px 15px;border-bottom:1px solid #e5e7eb;display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
.sat-card-head h3{{font-size:.92rem;font-weight:700;flex:1}}
.sat-card-body{{padding:14px 16px}}
.met-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin-bottom:12px}}
.met{{border:1px solid #e5e7eb;border-radius:6px;padding:8px 10px}}
.met-lbl{{font-size:.57rem;text-transform:uppercase;letter-spacing:.05em;color:#9ca3af}}
.met-val{{font-size:.95rem;font-weight:700;margin-top:1px;font-family:monospace}}
.rec{{padding:9px 12px;border-radius:6px;margin-bottom:10px;border-left:4px solid;font-size:.8rem}}
.rec.ok{{background:#f0fdf4;border-color:#22c55e}}.rec.warn{{background:#fffbeb;border-color:#f59e0b}}.rec.fault{{background:#fef2f2;border-color:#ef4444}}
.sub-h{{font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#6b7280;margin:12px 0 6px;border-bottom:1px solid #f3f4f6;padding-bottom:4px}}
.no-data{{color:#9ca3af;font-style:italic;font-size:.78rem;padding:6px 0}}
.tag{{display:inline-block;padding:1px 7px;border-radius:4px;font-size:.68rem;font-weight:700}}
.t-ok{{background:#dcfce7;color:#166534}}.t-warn{{background:#fef3c7;color:#92400e}}.t-fault{{background:#fee2e2;color:#991b1b}}
.analysis-box{{background:#f8fafc;border:1px solid #e5e7eb;border-radius:7px;padding:11px 13px;margin-bottom:10px;font-size:.8rem}}
.analysis-box h4{{font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:#6b7280;margin-bottom:6px}}
.analysis-row{{display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid #f3f4f6}}
.analysis-row:last-child{{border-bottom:none}}
.analysis-key{{color:#6b7280}}.analysis-val{{font-weight:600}}
.recom-list li{{margin-bottom:5px;font-size:.8rem;padding-left:4px}}
.footer{{text-align:center;padding:18px;color:#9ca3af;font-size:.68rem;border-top:1px solid #e5e7eb;margin-top:10px}}
.btn-print{{position:fixed;bottom:20px;right:20px;background:#1e3a5f;color:#fff;border:none;padding:10px 18px;border-radius:7px;cursor:pointer;font-size:.8rem;font-weight:700;box-shadow:0 4px 14px rgba(0,0,0,.25);z-index:999;transition:background .15s}}
.btn-print:hover{{background:#2563eb}}
.conf-note{{font-size:.68rem;color:#9ca3af;text-align:right;margin-bottom:4px}}
</style></head><body>
<button class="btn-print no-print" onclick="window.print()">&#128424; Print / Save as PDF</button>

<div class="cover">
  <div style="font-size:.62rem;opacity:.55;text-transform:uppercase;letter-spacing:.12em;margin-bottom:6px">OFFICIAL INSPECTION REPORT &mdash; CONFIDENTIAL</div>
  <h1>EPM Industrial Monitoring Report</h1>
  <div class="sub">EdgeAI Predictive Maintenance System &mdash; {esc(_rv._FACTORY_NAME)}</div>
  <div class="cover-meta">
    <div class="cover-stat"><strong>{now_str}</strong>Generated</div>
    <div class="cover-stat"><strong>{esc(sat_name) if sat_name else str(len(sats)) + " machine" + ("s" if len(sats)!=1 else "")}</strong>Scope</div>
    <div class="cover-stat"><strong>{fmt_up(int(now_ts - _rv._SERVER_START_T))}</strong>Gateway Uptime</div>
    <div class="cover-stat"><strong>{len(scope_alerts)}</strong>Alert Transitions Logged</div>
    <div class="cover-stat"><strong>{tot_fault_evts}</strong>Total Fault Frames</div>
  </div>
</div>
<div class="body">''')

    # ── EXECUTIVE SUMMARY ─────────────────────────────────────────────────────
    W('<div class="section">')
    W('<h2>&#9881; Executive Summary</h2>')
    W(f'<div class="risk-box" style="background:{risk_bg};border-color:{risk_c}">')
    W(f'<div class="risk-icon">{"🚨" if fault_n>0 else "⚠" if warn_n>0 else "✅"}</div>')
    W(f'<div><div class="risk-label" style="color:{risk_c}">FACTORY RISK LEVEL: {risk}</div>')
    if fault_n > 0:
        names = ', '.join(r['s'].name for r in sat_rows if r['s'].sent_alert==_rv.EPM_ALERT_FAULT)
        W(f'<div class="risk-note"><strong>{fault_n} machine(s) currently in FAULT:</strong> {esc(names)}.'
          f' Immediate inspection required. Do not operate affected machinery until cleared and signed off by a qualified technician.</div>')
    elif warn_n > 0:
        names = ', '.join(r['s'].name for r in sat_rows if r['s'].sent_alert==_rv.EPM_ALERT_WARN)
        W(f'<div class="risk-note"><strong>{warn_n} machine(s) showing elevated vibration (WARN):</strong> {esc(names)}.'
          f' Schedule inspection within 7 days. Monitor closely for escalation to FAULT.</div>')
    else:
        W('<div class="risk-note">All monitored machines are operating within normal vibration parameters. Continue routine monitoring and scheduled maintenance intervals.</div>')
    W('</div></div>')

    W('<div class="kpi-grid">')
    kpis = [
        ('Total Machines', len(sats), '#1e3a5f'),
        ('Healthy (OK)', ok_n, '#16a34a'),
        ('Warning', warn_n, '#d97706'),
        ('Fault', fault_n, '#dc2626'),
        ('Avg Health', f'{avg_h}%', '#16a34a' if avg_h>=75 else '#d97706' if avg_h>=50 else '#dc2626'),
        ('Fault Events', tot_fault_evts, '#dc2626' if tot_fault_evts>0 else '#374151'),
    ]
    for lbl, val, clr in kpis:
        W(f'<div class="kpi"><div class="kpi-lbl">{lbl}</div><div class="kpi-val" style="color:{clr}">{val}</div></div>')
    W('</div>')
    W('</div>')  # /section

    # ── MACHINE STATUS TABLE ──────────────────────────────────────────────────
    W('<div class="section">')
    W('<h2>&#128202; Machine Status at a Glance</h2>')
    W('<table><thead><tr><th>Machine</th><th>Status</th><th>Health</th><th>Kurtosis</th>'
      '<th>RUL Estimate</th><th>Fault Events</th><th>Last Fault</th><th>Last Maintenance</th></tr></thead><tbody>')
    for r in sat_rows:
        s, ml = r['s'], r['ml']
        al = ['OK','WARN','FAULT'][min(int(s.sent_alert),2)]
        k  = (r['m'].get('mic_kurtosis',0))
        kc = '#dc2626' if k>=_rv.K_FAULT else '#d97706' if k>=_rv.K_WARN else '#16a34a'
        fc = '#dc2626' if s.fault_frames>0 else '#374151'
        W(f'<tr><td><strong>{esc(s.name)}</strong><br>'
          f'<span style="font-family:monospace;font-size:.68rem;color:#9ca3af">{esc(s.mac_hex)}</span></td>'
          f'<td>{status_badge(al)}</td>'
          f'<td>{health_bar(r["health"])}</td>'
          f'<td style="font-family:monospace;color:{kc};font-weight:700">{k:.2f}</td>'
          f'<td style="font-size:.78rem">{fmt_rul(r)}</td>'
          f'<td style="text-align:center;font-weight:700;color:{fc}">{s.fault_frames}</td>'
          f'<td style="font-size:.76rem">{fmt_dt(s.last_fault_t)}</td>'
          f'<td style="font-size:.76rem">{esc(ml.get("last_date","— not logged"))}'
          + ('<br><span style="color:#9ca3af;font-size:.7rem">' + esc(ml['technician']) + '</span>' if ml.get('technician') else '')
          + '</td></tr>')
    W('</tbody></table>')
    W('</div>')

    # ── PER-MACHINE DETAIL ────────────────────────────────────────────────────
    W('<div class="section">')
    W('<h2>&#128295; Machine Detail Reports</h2>')
    for r in sat_rows:
        s, ml, m = r['s'], r['ml'], r['m']
        al = ['OK','WARN','FAULT'][min(int(s.sent_alert),2)]
        mc = al.lower()
        mic_k  = m.get('mic_kurtosis', 0)
        mic_cf = m.get('mic_crest', 0)
        mic_rms = m.get('mic_rms', 0)
        hb_pct = m.get('high_band_ratio', 0) * 100
        z = s.last_z
        fault_rate = (s.fault_frames / max(s.frame_count, 1)) * 100
        warn_rate  = (s.warn_frames  / max(s.frame_count, 1)) * 100
        kc = '#dc2626' if mic_k>=_rv.K_FAULT else '#d97706' if mic_k>=_rv.K_WARN else '#16a34a'
        zc = '#dc2626' if z>3 else '#d97706' if z>1.5 else '#16a34a'

        W(f'<div class="sat-card">')
        W(f'<div class="sat-card-head">'
          f'<h3>{esc(s.name)}</h3>{status_badge(al)}'
          f'&nbsp;&nbsp;<span style="font-family:monospace;font-size:.68rem;color:#9ca3af">{esc(s.mac_hex)}</span>'
          f'&nbsp;·&nbsp;<span style="font-size:.7rem;color:#9ca3af">FW {s.fw_str()}</span>'
          f'&nbsp;·&nbsp;<span style="font-size:.7rem;color:#9ca3af">{"✓ Calibrated" if s.calibrated else "⧖ Calibrating"}</span>'
          f'</div>')
        W('<div class="sat-card-body">')

        # Metrics grid
        W('<div class="met-grid">')
        mets = [
            ('Kurtosis', f'{mic_k:.2f}', kc),
            ('Crest Factor', f'{mic_cf:.2f}', None),
            ('High-Band %', f'{hb_pct:.1f}%', None),
            ('Mic RMS', f'{mic_rms:.5f}', None),
            ('Z-Score', f'{z:.2f}', zc),
            ('Frame Rate', f'{s.fps:.1f} fps', None),
        ]
        for lbl, val, clr in mets:
            cs = f'style="color:{clr}"' if clr else ''
            W(f'<div class="met"><div class="met-lbl">{lbl}</div><div class="met-val" {cs}>{val}</div></div>')
        W('</div>')

        # Health
        W(f'<div style="margin-bottom:10px">{health_bar(r["health"])}&nbsp;&nbsp;'
          f'<span style="font-size:.78rem;color:#6b7280">{esc(r["maint_str"])}</span></div>')

        # Condition box
        ft = s.fault_type or 'Normal'
        ft_color = ('#dc2626' if 'Fault' in ft or 'Severe' in ft
                    else '#d97706' if ft != 'Normal'
                    else '#16a34a')
        W(f'<div class="rec {mc}">')
        W(f'<strong>Condition:</strong> {esc(r["maint_str"])}')
        W(f'<br><strong>Fault Analysis:</strong> '
          f'<span style="color:{ft_color};font-weight:700">{esc(ft)}</span>')
        if r['rul_days'] is not None or r.get('rul_result') is not None:
            W(f'<br><strong>Estimated RUL:</strong> {fmt_rul(r)}')
        W('</div>')

        # Analysis box
        W('<div class="analysis-box"><h4>Session Analysis</h4>')
        trend = 'Worsening' if fault_rate > 5 else 'Warning trend' if warn_rate > 15 else 'Stable'
        trend_c = '#dc2626' if 'Worsening' in trend else '#d97706' if 'Warning' in trend else '#16a34a'
        W(f'<div class="analysis-row"><span class="analysis-key">Frames Analyzed</span><span class="analysis-val">{s.frame_count:,}</span></div>')
        W(f'<div class="analysis-row"><span class="analysis-key">Fault Rate</span><span class="analysis-val" style="color:{"#dc2626" if fault_rate>5 else "#374151"}">{fault_rate:.1f}%  ({s.fault_frames} frames)</span></div>')
        W(f'<div class="analysis-row"><span class="analysis-key">Warn Rate</span><span class="analysis-val" style="color:{"#d97706" if warn_rate>10 else "#374151"}">{warn_rate:.1f}%  ({s.warn_frames} frames)</span></div>')
        W(f'<div class="analysis-row"><span class="analysis-key">Vibration Trend</span><span class="analysis-val" style="color:{trend_c}">{trend}</span></div>')
        W(f'<div class="analysis-row"><span class="analysis-key">Alert Transitions Logged</span><span class="analysis-val">{len(r["alerts"])}</span></div>')
        W('</div>')

        # Alert history (mini)
        W(f'<div class="sub-h">Alert History — {len(r["alerts"])} transitions</div>')
        if r['alerts']:
            W('<table><thead><tr><th>Time</th><th>From</th><th>To</th><th>Kurtosis</th><th>Crest</th><th>Z</th><th>Contributing Features</th></tr></thead><tbody>')
            for ev in r['alerts'][:12]:
                dt_s = datetime.datetime.fromtimestamp(ev['time']).strftime('%b %d  %H:%M:%S')
                tc = 'fault' if ev['alert']=='FAULT' else 'warn' if ev['alert']=='WARN' else 'ok'
                reason_s = esc(ev.get('reason', '—'))
                W(f'<tr><td style="font-family:monospace;font-size:.72rem">{dt_s}</td>'
                  f'<td><span class="tag t-ok">{ev["prev"]}</span></td>'
                  f'<td><span class="tag t-{tc}">{ev["alert"]}</span></td>'
                  f'<td style="font-family:monospace">{ev["kurtosis"]:.2f}</td>'
                  f'<td style="font-family:monospace">{ev["crest"]:.2f}</td>'
                  f'<td style="font-family:monospace">{ev["z_score"]:.1f}</td>'
                  f'<td style="font-size:.7rem;color:#d97706;font-family:monospace">{reason_s}</td></tr>')
            if len(r['alerts']) > 12:
                W(f'<tr><td colspan="7" style="text-align:center;color:#9ca3af;font-size:.72rem">… {len(r["alerts"])-12} more events in Full Alert Log below</td></tr>')
            W('</tbody></table>')
        else:
            W('<p class="no-data">No alert transitions recorded — machine has been stable this session.</p>')

        # Attribution — most frequently contributing features over alert history
        _fw_evs = [ev for ev in r['alerts']
                   if ev.get('alert') in ('FAULT', 'WARN') and ev.get('reason', '—') != '—']
        if _fw_evs:
            _feat_counts: dict = {}
            for ev in _fw_evs:
                for part in ev.get('reason', '').split(';'):
                    part = part.strip()
                    if ':' in part:
                        fname = part.split(':')[0].strip()
                        _feat_counts[fname] = _feat_counts.get(fname, 0) + 1
            if _feat_counts:
                _feat_desc = {
                    'mic_kurt':  'Mic kurtosis — impulsive bearing impacts (early/mid fault)',
                    'mic_crest': 'Mic crest factor — peak shock ratio (advanced fault)',
                    'mic_rms':   'Mic RMS — broadband energy (sustained vibration)',
                    'mic_hb':    'High-band energy 2-8 kHz — bearing defect resonance band',
                    'hst':       'HST anomaly score — multivariate spectral deviation',
                }
                W('<div class="sub-h">Attribution — Most Frequent Fault Drivers</div>')
                W('<p style="font-size:.76rem;color:#6b7280;margin:-4px 0 8px">'
                  'Features most frequently contributing to WARN/FAULT decisions this session. '
                  'Guides the technician toward which measurement to investigate first.</p>')
                W('<table><thead><tr><th>Feature</th><th>Alert Count</th>'
                  '<th>% of Alerts</th><th>Diagnostic Significance</th></tr></thead><tbody>')
                for fname, cnt in sorted(_feat_counts.items(),
                                         key=lambda x: x[1], reverse=True)[:5]:
                    pct = 100 * cnt // max(len(_fw_evs), 1)
                    desc = _feat_desc.get(fname, fname)
                    W(f'<tr><td style="font-weight:700;color:#d97706;font-family:monospace">'
                      f'{esc(fname)}</td>'
                      f'<td style="font-family:monospace;text-align:center">{cnt}</td>'
                      f'<td style="font-family:monospace;text-align:center">{pct}%</td>'
                      f'<td style="font-size:.75rem">{esc(desc)}</td></tr>')
                W('</tbody></table>')

        # Maintenance
        W('<div class="sub-h">Maintenance Record</div>')
        if ml and ml.get('last_date'):
            today = datetime.datetime.now().strftime('%Y-%m-%d')
            overdue = ml.get('next_date','') and ml['next_date'] < today
            W('<table><thead><tr><th>Last Service</th><th>Technician</th><th>Service Type</th>'
              '<th>Next Scheduled</th><th>Record Updated</th></tr></thead><tbody>')
            W(f'<tr><td>{esc(ml.get("last_date","—"))}</td>'
              f'<td>{esc(ml.get("technician","—"))}</td>'
              f'<td>{esc(ml.get("maint_type","—"))}</td>'
              f'<td style="{"color:#dc2626;font-weight:700" if overdue else ""}">'
              f'{esc(ml.get("next_date","—"))}{"  ⚠ OVERDUE" if overdue else ""}</td>'
              f'<td style="font-size:.74rem;color:#9ca3af">{fmt_dt(ml.get("updated_at",0))}</td></tr>')
            W('</tbody></table>')
            if ml.get('notes'):
                W(f'<p style="margin-top:6px;font-size:.78rem"><strong>Notes:</strong> {esc(ml["notes"])}</p>')
        else:
            W('<p class="no-data">⚠ No maintenance record found. Log a maintenance entry via the dashboard to satisfy compliance and insurance requirements.</p>')

        # Recommendations
        W('<div class="sub-h">Recommendations</div><ul class="recom-list">')
        if al == 'FAULT':
            W('<li><strong style="color:#dc2626">🚨 IMMEDIATE ACTION:</strong> Machine is in FAULT state. Halt operation, perform bearing inspection, replace if necessary.</li>')
        elif al == 'WARN':
            W('<li><strong style="color:#d97706">⚠ SCHEDULE INSPECTION:</strong> Elevated vibration detected. Perform bearing inspection within 7 days.</li>')
        if fault_rate > 10:
            W(f'<li>High fault event rate ({fault_rate:.0f}%). Recommend vibration analysis, lubrication check, and alignment verification.</li>')
        if not ml.get('last_date'):
            W('<li>No maintenance record on file. Log service history to establish compliance baseline and enable predictive scheduling.</li>')
        _rul_d = (r['rul_result'].hours_remaining / 24.0
                  if r.get('rul_result') and not math.isinf(r['rul_result'].hours_remaining)
                  else r.get('rul_days'))
        if _rul_d is not None and _rul_d < 30:
            W(f'<li>Estimated RUL is {fmt_rul(r)}. Order replacement bearings and schedule planned downtime before failure occurs.</li>')
        if al == 'OK' and not fault_rate:
            W('<li>Machine is operating normally. Continue routine maintenance schedule and log service dates after each inspection.</li>')
        W('</ul>')

        W('</div></div>')  # /sat-card-body, /sat-card

    W('</div>')  # /section

    # ── FULL ALERT AUDIT TRAIL ────────────────────────────────────────────────
    W('<div class="section page-break">')
    W(f'<h2>&#128203; Full Alert Audit Trail ({len(scope_alerts)} events)</h2>')
    if scope_alerts:
        W('<table><thead><tr><th>#</th><th>Timestamp (local)</th><th>Machine</th>'
          '<th>From</th><th>To</th><th>Kurtosis</th><th>Crest</th><th>Z-Score</th>'
          '<th>Contributing Features</th><th>MAC</th></tr></thead><tbody>')
        for i, ev in enumerate(scope_alerts, 1):
            dt_s = datetime.datetime.fromtimestamp(ev['time']).strftime('%Y-%m-%d  %H:%M:%S')
            tc = 'fault' if ev['alert']=='FAULT' else 'warn' if ev['alert']=='WARN' else 'ok'
            W(f'<tr><td style="color:#9ca3af;font-size:.72rem">{i}</td>'
              f'<td style="font-family:monospace;font-size:.72rem">{dt_s}</td>'
              f'<td style="font-weight:700">{esc(ev["satellite"])}</td>'
              f'<td><span class="tag t-ok">{ev["prev"]}</span></td>'
              f'<td><span class="tag t-{tc}">{ev["alert"]}</span></td>'
              f'<td style="font-family:monospace">{ev["kurtosis"]:.2f}</td>'
              f'<td style="font-family:monospace">{ev["crest"]:.2f}</td>'
              f'<td style="font-family:monospace">{ev["z_score"]:.1f}</td>'
              f'<td style="font-size:.68rem;color:#d97706;font-family:monospace">'
              f'{esc(ev.get("reason","—"))}</td>'
              f'<td style="font-family:monospace;font-size:.68rem;color:#9ca3af">'
              f'{esc(ev.get("mac","—"))}</td></tr>')
        W('</tbody></table>')
    else:
        W('<p class="no-data">No alert transitions recorded this session. System has remained stable since gateway startup.</p>')
    W('<p style="font-size:.7rem;color:#9ca3af;margin-top:6px">'
      'This audit trail captures every machine state change with epoch-accurate timestamps keyed to hardware MAC address. '
      'Events are listed newest-first. For permanent archival export this report or use the Alert Log JSON export from the dashboard.</p>')
    W('</div>')

    # ── MAINTENANCE SUMMARY ───────────────────────────────────────────────────
    W('<div class="section">')
    W('<h2>&#128295; Maintenance Log Summary</h2>')
    maint_rows = [(mac, rec) for mac, rec in maint.items()
                  if not sat_name or any(s.mac_hex == mac for s in sats)]
    if maint_rows:
        today = datetime.datetime.now().strftime('%Y-%m-%d')
        W('<table><thead><tr><th>Machine</th><th>MAC</th><th>Last Service</th>'
          '<th>Technician</th><th>Service Type</th><th>Next Scheduled</th><th>Notes</th></tr></thead><tbody>')
        for mac, rec in maint_rows:
            name = next((s.name for s in sats if s.mac_hex == mac), mac)
            overdue = rec.get('next_date','') and rec['next_date'] < today
            W(f'<tr><td><strong>{esc(name)}</strong></td>'
              f'<td style="font-family:monospace;font-size:.7rem;color:#9ca3af">{esc(mac)}</td>'
              f'<td>{esc(rec.get("last_date","—"))}</td>'
              f'<td>{esc(rec.get("technician","—"))}</td>'
              f'<td>{esc(rec.get("maint_type","—"))}</td>'
              f'<td style="{"color:#dc2626;font-weight:700" if overdue else ""}">'
              f'{esc(rec.get("next_date","—"))}{"  ⚠ OVERDUE" if overdue else ""}</td>'
              f'<td style="font-size:.75rem">{esc(rec.get("notes",""))}</td></tr>')
        W('</tbody></table>')
    else:
        W('<p class="no-data">⚠ No maintenance records on file. Log maintenance via the dashboard Maintenance tab to meet compliance requirements.</p>')
    W('</div>')

    # ── COMPLIANCE SUMMARY ────────────────────────────────────────────────────
    W('<div class="section">')
    W('<h2>&#9989; Compliance Summary</h2>')
    today = datetime.datetime.now().strftime('%Y-%m-%d')
    checks = [
        ('All machines connected',           all(s.connected for s in sats) and bool(sats)),
        ('No active FAULT conditions',        fault_n == 0),
        ('No active WARN conditions',         warn_n  == 0),
        ('All sensors calibrated',            all(s.calibrated for s in sats) and bool(sats)),
        ('All machines have maintenance log', all(bool(maint.get(s.mac_hex, {}).get('last_date')) for s in sats) and bool(sats)),
        ('No overdue maintenance scheduled',  all(not (maint.get(s.mac_hex,{}).get('next_date','') and maint.get(s.mac_hex,{})['next_date'] < today) for s in sats)),
        ('Audit trail active',                True),
    ]
    W('<table><thead><tr><th>Requirement</th><th>Status</th></tr></thead><tbody>')
    for label, ok in checks:
        icon, clr = ('✅ PASS', '#16a34a') if ok else ('❌ FAIL', '#dc2626')
        W(f'<tr><td>{label}</td><td style="color:{clr};font-weight:700">{icon}</td></tr>')
    W('</tbody></table>')
    W('</div>')

    # ── FOOTER ────────────────────────────────────────────────────────────────
    W(f'''<div class="footer">
  EPM Industrial Monitoring Report &mdash; {esc(_rv._FACTORY_NAME)}<br>
  Generated: {now_str} &mdash; Confidential &mdash; For authorized personnel only<br>
  Scope: {"Machine: " + esc(sat_name) if sat_name else "All " + str(len(sats)) + " monitored machines"}
  &mdash; EdgeAI Predictive Maintenance System
</div>
</div></body></html>''')

    return '\n'.join(h)
