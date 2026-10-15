"""Turn run summaries into a scorecard against bench/targets.json.

A metric or check that is missing, errored or skipped never counts as passing.
"""
import json
import pathlib
import statistics

from pipeline import config

TARGETS = json.loads((config.REPO / 'bench/targets.json').read_text())
TIER_ORDER = ('key', 'important', 'other')


def ratio_of(metric, spec):
    """Return (ratio, native_median, guest_median, pair_spread) or raise ValueError."""
    guest = metric.get('guest') or []
    if not guest:
        raise ValueError('no guest samples')
    g = statistics.median(guest)
    higher = metric.get('higher_is_better', True)
    if 'reference_seconds' in spec:
        if higher:
            raise ValueError('reference metrics must be durations')
        return spec['reference_seconds'] / g, spec['reference_seconds'], g, None
    native = metric.get('native') or []
    if not native:
        raise ValueError('no native samples')
    n = statistics.median(native)
    if g <= 0 or n <= 0:
        raise ValueError('non-positive sample median')
    ratio = g / n if higher else n / g
    pairs = [(gv / nv if higher else nv / gv) for nv, gv in zip(native, guest) if nv > 0 and gv > 0]
    spread = (min(pairs), max(pairs)) if len(pairs) >= 2 else None
    return ratio, n, g, spread


def score_bench(bench):
    rows = []
    metrics = (bench or {}).get('metrics', {})
    for name, spec in TARGETS['metrics'].items():
        tier = spec['tier']
        target = TARGETS['tiers'][tier]['min_ratio']
        row = {'metric': name, 'tier': tier, 'target': target, 'suite': spec.get('suite', 'bench')}
        metric = metrics.get(name)
        if metric is None:
            row['status'] = 'not_tested'
        elif metric.get('error'):
            row.update(status='fail', error=metric['error'])
        else:
            try:
                ratio, n, g, spread = ratio_of(metric, spec)
            except ValueError as error:
                row.update(status='fail', error=str(error))
            else:
                row.update(ratio=round(ratio, 4), native=n, guest=g, unit=metric.get('unit'),
                           spread=[round(x, 4) for x in spread] if spread else None,
                           reference='reference_seconds' in spec,
                           status='pass' if ratio >= target else 'fail')
        rows.append(row)
    return rows


def score_test(test):
    checks = (test or {}).get('checks', [])
    counts = {'pass': 0, 'fail': 0, 'not_tested': 0}
    for check in checks:
        counts[check['status'] if check['status'] in counts else 'fail'] += 1
    return {'checks': len(checks), **counts, 'gate': bool(checks) and counts['pass'] == len(checks)}


def scorecard(summary):
    card = {'run_id': summary['run_id'], 'revision': summary.get('revision', {}),
            'candidates': summary.get('candidates', []), 'kernel_args': summary.get('kernel_args', [])}
    if 'test' in summary:
        card['test'] = score_test(summary['test'])
    if 'bench' in summary:
        rows = score_bench(summary['bench'])
        card['bench'] = rows
        card['tiers'] = {}
        for tier in TIER_ORDER:
            tier_rows = [r for r in rows if r['tier'] == tier and r['suite'] == 'bench']
            card['tiers'][tier] = {'pass': sum(r['status'] == 'pass' for r in tier_rows), 'total': len(tier_rows)}
    return card


def _fmt(value):
    if value is None:
        return '-'
    if abs(value) >= 1000:
        return f'{value:,.0f}'
    if abs(value) >= 10:
        return f'{value:.1f}'
    return f'{value:.3g}'


def markdown(card, previous=None):
    rev = card['revision']
    lines = [f"# Scorecard `{card['run_id']}`", '',
             f"revision `{rev.get('git', '?')[:12]}`{' (dirty)' if rev.get('dirty') else ''} {rev.get('subject', '')}"]
    if card['candidates'] or card['kernel_args']:
        lines.append(f"candidates: {', '.join(card['candidates']) or '-'}; kernel args: {' '.join(card['kernel_args']) or '-'}")
    lines.append('')
    if 'test' in card:
        t = card['test']
        lines += [f"**Functional**: {t['pass']}/{t['checks']} pass, {t['fail']} fail, {t['not_tested']} not tested "
                  f"→ {'PASS' if t['gate'] else 'FAIL'}", '']
    if 'bench' in card:
        prev = {r['metric']: r for r in (previous or {}).get('bench', [])}
        summary = ', '.join(f"{tier} {v['pass']}/{v['total']}" for tier, v in card['tiers'].items())
        lines += [f'**Performance** (guest/native, higher is better): {summary}', '',
                  '| tier | metric | native | guest | ratio | target | pair range | Δ prev | status |',
                  '|---|---|---:|---:|---:|---:|---|---:|---|']
        for r in card['bench']:
            delta = '-'
            p = prev.get(r['metric'])
            if p and 'ratio' in p and 'ratio' in r:
                delta = f"{(r['ratio'] - p['ratio']) / p['ratio'] * 100:+.0f}%"
            native = ('ref ' if r.get('reference') else '') + _fmt(r.get('native'))
            spread = f"{r['spread'][0]:.2f}–{r['spread'][1]:.2f}" if r.get('spread') else '-'
            status = r['status'] + (f" ({r['error'][:40]})" if r.get('error') else '')
            lines.append(f"| {r['tier']} | {r['metric']} | {native} | {_fmt(r.get('guest'))} {r.get('unit') or ''} | "
                         f"{_fmt(r.get('ratio'))} | {r['target']:.2f} | {spread} | {delta} | {status} |")
    return '\n'.join(lines) + '\n'


def load(path):
    return json.loads(pathlib.Path(path).read_text())
