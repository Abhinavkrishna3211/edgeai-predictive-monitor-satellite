"""gateway/api/notifications.py — alert/drift audit trail, maintenance-log
persistence, and outbound notifications (webhook/email), extracted from
recv_verify.py (Phase 8b2 task 1).

Every function here does a *lazy* `import recv_verify as _rv` inside its own
body (never at module level — recv_verify.py imports from this module at its
own module-load time, so a top-level `import recv_verify` here would try to
import a module that is still mid-initialization; see
gateway/registry/satellite_state.py's module docstring for the original
precedent).

All state touched below (`_ALERT_HISTORY`, `_MAINT_LOG`, `_NOTIFY_WEBHOOK`,
`_storage`, `_bayesian_fusion`, ...) is CLI-configurable and/or reassigned
wholesale by recv_verify.main() via `global`. Module-attribute assignment
(`_rv._MAINT_LOG = ...`) is used instead of this module's own `global`
statement so those reassignments are visible here too — a bare `global
_MAINT_LOG` in this file would silently create a second, disconnected copy.
"""

import datetime
import json
import os
import smtplib
import threading
import time
import urllib.request as _urllib_req
from email.mime.text import MIMEText


def _log_alert_event(sat_name, mac_hex, new_alert, prev_alert,
                     kurtosis, crest, z_score, feat_z=None):
    """Append a state-change event to the in-memory audit trail and SQLite DB."""
    import recv_verify as _rv
    labels = ['OK', 'WARN', 'FAULT']
    from_lbl = labels[min(prev_alert, 2)]
    to_lbl   = labels[min(new_alert, 2)]

    # Build attribution reason: top-3 features by logit contribution
    reason = f'K={kurtosis:.2f} CF={crest:.2f} z={z_score:.1f}'
    if feat_z:
        if _rv._FUSION_AVAILABLE and _rv._bayesian_fusion is not None:
            top = _rv._bayesian_fusion.attribute(feat_z, top_k=3)
        else:
            top = sorted(feat_z.items(), key=lambda x: x[1], reverse=True)[:3]
        if top:
            reason = '; '.join(f'{n}: {v:+.2f}' for n, v in top)

    event = {
        'time':      time.time(),
        'satellite': sat_name,
        'mac':       mac_hex,
        'alert':     to_lbl,
        'prev':      from_lbl,
        'kurtosis':  round(kurtosis, 2),
        'crest':     round(crest, 2),
        'z_score':   round(z_score, 1),
        'reason':    reason,
    }
    with _rv._ALERT_HISTORY_LOCK:
        _rv._ALERT_HISTORY.appendleft(event)
    # Persist to SQLite (non-blocking; autocommit connection)
    if _rv._storage is not None:
        try:
            _rv._storage.log_alert(sat_name, from_lbl, to_lbl, 0.0, reason)
        except Exception as e:
            print(f'[alert] [{sat_name}] DB log_alert failed: {e}')


def _log_drift_event(sat_name, mac_hex, n_samples):
    """Append a BASELINE_REFRESH event to the audit trail and SQLite DB."""
    import recv_verify as _rv
    detail = f'Concept drift — baseline refreshed from {n_samples} OK frames'
    event = {
        'time':       time.time(),
        'satellite':  sat_name,
        'mac':        mac_hex,
        'event_type': 'BASELINE_REFRESH',
        'alert':      'INFO',
        'prev':       'OK',
        'kurtosis':   0.0,
        'crest':      0.0,
        'z_score':    0.0,
        'detail':     detail,
    }
    with _rv._ALERT_HISTORY_LOCK:
        _rv._ALERT_HISTORY.appendleft(event)
    if _rv._storage is not None:
        try:
            _rv._storage.log_alert(sat_name, 'OK', 'INFO', 0.0,
                               f'BASELINE_REFRESH: {detail}')
        except Exception as e:
            print(f'[alert] [{sat_name}] DB log_alert failed: {e}')


# _recompute_z_baseline is imported (by recv_verify.py) from gateway.registry.baselines.


# ─── Maintenance log ──────────────────────────────────────────────────────────

def _load_maint_log(path):
    """Load maintenance records from SQLite (primary) or JSON file (migration fallback)."""
    import recv_verify as _rv
    _rv._MAINT_LOG_PATH = path
    # Primary: SQLite — populated by _storage.get_all_maintenance() at startup
    if _rv._storage is not None:
        try:
            with _rv._MAINT_LOG_LOCK:
                _rv._MAINT_LOG = _rv._storage.get_all_maintenance()
            if _rv._MAINT_LOG:
                print(f'[maint] Loaded {len(_rv._MAINT_LOG)} maintenance record(s) from DB')
                return
        except Exception as e:
            print(f'[maint] WARNING: could not read DB: {e}')
    # Fallback: legacy maintenance_log.json (one-time migration path)
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
            with _rv._MAINT_LOG_LOCK:
                _rv._MAINT_LOG = data
            print(f'[maint] Loaded {len(_rv._MAINT_LOG)} maintenance record(s) from JSON '
                  f'(run migrate_json_to_sqlite.py to migrate permanently)')
        except Exception as e:
            print(f'[maint] WARNING: could not read {path}: {e}')
            _rv._MAINT_LOG = {}


def _save_maint_log():
    """Writes to SQLite via _storage; JSON fallback kept for environments without DB."""
    import recv_verify as _rv
    # SQLite writes happen directly in the POST handler via _storage.log_maintenance()
    # This function is retained for the no-storage fallback path only.
    if _rv._storage is not None:
        return
    if _rv._MAINT_LOG_PATH:
        try:
            with open(_rv._MAINT_LOG_PATH, 'w') as f:
                json.dump(_rv._MAINT_LOG, f, indent=2)
        except Exception as e:
            print(f'[maint] WARNING: JSON save failed: {e}')


# ─── Notifications ────────────────────────────────────────────────────────────

def _fire_notification(sat_name, mac_hex, alert_str, kurtosis, crest, z_score):
    """Rate-limited: max 1 notification per satellite per 5 minutes."""
    import recv_verify as _rv
    now = time.time()
    if now - _rv._NOTIFY_COOLDOWN.get(mac_hex, 0) < _rv._NOTIFY_COOLDOWN_S:
        return
    _rv._NOTIFY_COOLDOWN[mac_hex] = now
    ts  = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    msg = (f'EPM ALERT [{alert_str}] — {sat_name}\n'
           f'Kurtosis: {kurtosis:.2f}  Crest Factor: {crest:.2f}  Z-Score: {z_score:.1f}\n'
           f'Time: {ts}\nGateway: {_rv._FACTORY_NAME}')
    if _rv._NOTIFY_WEBHOOK:
        threading.Thread(target=_send_webhook,
                         args=(msg, sat_name, alert_str), daemon=True).start()
    if _rv._NOTIFY_EMAIL_CFG:
        threading.Thread(target=_send_email,
                         args=(msg, sat_name, alert_str), daemon=True).start()


def _send_webhook(msg, sat_name, alert_str):
    import recv_verify as _rv
    url   = _rv._NOTIFY_WEBHOOK
    emoji = '\U0001f6a8' if alert_str == 'FAULT' else '⚠️'
    color = 0xef4444 if alert_str == 'FAULT' else 0xf59e0b
    if 'discord.com/api/webhooks' in url:
        payload = json.dumps({
            'username': 'EPM Monitor',
            'embeds': [{'title': f'{emoji} {alert_str} — {sat_name}',
                        'description': msg, 'color': color,
                        'timestamp': datetime.datetime.utcnow().isoformat()}],
        }).encode()
    elif 'hooks.slack.com' in url or 'slack.com/services' in url:
        payload = json.dumps({
            'text': f'{emoji} *{alert_str}* — {sat_name}',
            'blocks': [{'type': 'section',
                        'text': {'type': 'mrkdwn', 'text': f'```{msg}```'}}],
        }).encode()
    else:
        payload = json.dumps({'alert': alert_str, 'satellite': sat_name,
                               'message': msg, 'timestamp': time.time()}).encode()
    try:
        req = _urllib_req.Request(url, data=payload,
                                   headers={'Content-Type': 'application/json'})
        with _urllib_req.urlopen(req, timeout=10) as resp:
            # VERIFY-FIX: use ASCII arrow to avoid cp1252 UnicodeEncodeError on Windows.
            print(f'[notify] Webhook -> HTTP {resp.status}  ({alert_str} / {sat_name})')
    except Exception as e:
        print(f'[notify] Webhook failed: {e}')


def _send_email(msg, sat_name, alert_str):
    import recv_verify as _rv
    cfg = _rv._NOTIFY_EMAIL_CFG
    m   = MIMEText(msg)
    m['Subject'] = f'EPM {alert_str}: {sat_name} — {_rv._FACTORY_NAME}'
    m['From']    = cfg['from']
    m['To']      = cfg['to']
    try:
        with smtplib.SMTP(cfg['host'], cfg.get('port', 587), timeout=15) as s:
            s.ehlo()
            s.starttls()
            if cfg.get('user'):
                s.login(cfg['user'], cfg['pass'])
            s.send_message(m)
        print(f'[notify] Email sent ({alert_str} / {sat_name})')
    except Exception as e:
        print(f'[notify] Email failed: {e}')
