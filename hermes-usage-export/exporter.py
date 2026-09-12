"""Explicit trusted-Hermes network boundary; never imported by the shell."""
import importlib.util
import time
from pathlib import Path

_spec = importlib.util.spec_from_file_location('hermes_usage_quota_io', Path(__file__).resolve().with_name('quota_io.py'))
q = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(q)

PLANS = {'free', 'plus', 'pro', 'team', 'business', 'enterprise', 'max', 'starter', 'standard', 'premium'}
LABELS = {'5 hour', '5-hour', 'session', 'weekly', '7 day', '7-day', 'subscription', 'primary', 'secondary', 'all models', 'sonnet', 'opus'}


def setup(parser):
    parser.add_argument('--provider', choices=q.PROVIDERS, required=True)
    parser.add_argument('--allow-network', action='store_true')


def normalize(snapshot, provider, now, info=None):
    result = q.unavailable(provider, now)
    if snapshot is None or getattr(snapshot, 'unavailable_reason', None): return result
    if getattr(snapshot, 'provider', None) != provider: return result
    if provider == 'nous' and (info is None or getattr(info, 'logged_in', None) is not True
                              or getattr(info, 'source', None) != 'account_api'): return result
    stamp = getattr(snapshot, 'fetched_at', None)
    try:
        if stamp.tzinfo is None: return result
        fetched = stamp.timestamp()
    except (AttributeError, ValueError, OverflowError): return result
    if not q.number(fetched) or not fetched <= now < fetched + q.TTL: return result
    result.update(fetchedAt=fetched, expiresAt=fetched + q.TTL)
    # Avoid copying arbitrary provider strings, URLs, IDs, financial prose.
    plan = getattr(snapshot, 'plan', None)
    if type(plan) is str and plan.lower() in PLANS: result['plan'] = plan.lower()
    windows = getattr(snapshot, 'windows', ())
    if type(windows) not in (tuple, list): windows = ()
    for w in windows[:8]:
        p = getattr(w, 'used_percent', None)
        if not q.number(p, high=100): continue
        reset = getattr(w, 'reset_at', None)
        if reset is not None:
            try:
                if reset.tzinfo is None: continue
                reset = reset.timestamp()
            except (AttributeError, ValueError, OverflowError): continue
            if not q.number(reset) or reset <= now: continue
        label = getattr(w, 'label', '')
        label = label.lower() if type(label) is str else ''
        label = label.title() if label in LABELS else 'Account window'
        result['windows'].append(dict(label=label, usedPercent=p, remainingPercent=100-p, resetAt=reset))
    if provider == 'nous':
        access = getattr(info, 'paid_service_access_info', None)
        paid = getattr(info, 'paid_service_access', None)
        result['accessStatus'] = 'allowed' if paid is True else ('denied' if paid is False else 'unknown')
        if getattr(access, 'member_spend_cap_exceeded', None) is True:
            result['accessStatus'] = 'member-cap-exceeded'
        remaining = getattr(access, 'total_usable_credits', None)
        if q.number(remaining): result.update(remainingUsd=remaining, currency='USD')
    result['available'] = bool(result['windows'] or 'remainingUsd' in result)
    result['status'] = 'observed' if result['available'] else 'unavailable'
    return result


def command(args):
    if not args.allow_network:
        print('Refused: --allow-network is required; Hermes may read/refresh its credentials.')
        return 2
    info = None; snapshot = None
    try:
        # Optional APIs are intentionally inside the protected, opted-in path.
        from agent.account_usage import fetch_account_usage, build_nous_credits_snapshot
        if args.provider == 'nous':
            from hermes_cli.nous_account import get_nous_portal_account_info
            info = get_nous_portal_account_info(force_fresh=True)
            snapshot = build_nous_credits_snapshot(info)
        else:
            snapshot = fetch_account_usage(args.provider)
    except Exception:
        pass  # Never export exception strings, raw responses, or credentials.
    from hermes_constants import get_hermes_home
    record = normalize(snapshot, args.provider, time.time(), info)
    try:
        q.write_snapshot(Path(get_hermes_home()) / 'usage-export', args.provider, record)
    except (OSError, ValueError):
        print('Export refused: unsafe path or invalid snapshot.')
        return 1
    print('Exported ' + args.provider + ': ' + record['status'])
    return 0 if record['available'] else 1
