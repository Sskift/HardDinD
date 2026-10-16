"""Turn run summaries into a scorecard against bench/targets.json.

A metric or check that is missing, errored or skipped never counts as passing.
"""
import json
import math
import pathlib
import statistics

from pipeline import config

TARGETS = json.loads((config.REPO / 'bench/targets.json').read_text())
TIER_ORDER = ('key', 'important', 'other')


def paired_ba(metric):
    a, b = metric.get('guest', []), metric.get('guest_b', [])
    if not a or len(a) != len(b):
        raise ValueError('incomplete A/B pairs')
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in a + b):
        raise ValueError('invalid A/B sample')
    ratios = [(bv / av if metric.get('higher_is_better', True) else av / bv) for av, bv in zip(a, b)]
    return round(statistics.median(ratios), 4), [round(min(ratios), 4), round(max(ratios), 4)]


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
    pairs = [(gv / nv if higher else nv / gv) for nv, gv in zip(native, guest) if nv > 0 and gv > 0]
    # Samples are interleaved native/guest pairs, so each pair sees the same host
    # load; the median pair ratio cancels drift that the ratio of medians keeps.
    if len(pairs) == len(native) == len(guest):
        ratio = statistics.median(pairs)
    else:
        ratio = g / n if higher else n / g
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
                native = metric.get('native') or []
                # Native samples drifting by > 30% within the run, or the outer container
                # being CPU-throttled during samples, means the host was contended: the
                # ratio is still scored but is flagged as noisy.
                samples = (metric.get('detail') or {}).get('samples', [])
                throttled = [s['throttled'] for s in samples if s.get('phase') == 'sample'
                             and s.get('side') != 'guest_b' and 'throttled' in s]
                row['throttled'] = round(max(throttled), 3) if throttled else None
                noisy = (len(native) >= 2 and max(native) > 1.3 * min(native)) or bool(throttled and max(throttled) > 0.10)
                row.update(ratio=round(ratio, 4), native=n, guest=g, unit=metric.get('unit'),
                           spread=[round(x, 4) for x in spread] if spread else None,
                           reference='reference_seconds' in spec, noisy=noisy,
                           status='pass' if ratio >= target else 'fail')
        if metric is not None and 'guest_b' in metric:
            b = {k: v for k, v in metric.items() if k != 'guest_b'}
            b['guest'] = metric['guest_b']
            samples = (metric.get('detail') or {}).get('samples', [])
            b['detail'] = {'samples': [{**s, 'side': 'guest' if s.get('side') == 'guest_b' else s.get('side')}
                                      for s in samples if s.get('side') != 'guest']}
            rb = next(r for r in score_bench({'metrics': {name: b}}) if r['metric'] == name)
            row.update({k + '_b': rb.get(k) for k in ('ratio', 'guest', 'spread', 'status', 'noisy', 'throttled')})
            try:
                if metric.get('error'):
                    raise ValueError(metric['error'])
                if 'reference_seconds' not in spec and len(metric.get('native', [])) != len(metric.get('guest', [])):
                    raise ValueError('incomplete native/A/B pairs')
                row['ratio_ba'], row['spread_ba'] = paired_ba(metric)
                row['noisy_ba'] = any(s.get('throttled', 0) > .10 for s in samples
                                      if s.get('phase') == 'sample' and s.get('side') in ('guest', 'guest_b'))
            except ValueError as error:
                row.update(status='fail', status_b='fail', error=str(error))
        rows.append(row)
    return rows


def score_test(test):
    checks = (test or {}).get('checks', [])
    counts = {'pass': 0, 'fail': 0, 'not_tested': 0}
    for check in checks:
        counts[check['status'] if check['status'] in counts else 'fail'] += 1
    return {'checks': len(checks), **counts, 'gate': bool(checks) and counts['pass'] == len(checks)}


def scorecard(summary):
    card = {'run_id': summary['run_id'], 'revision': summary.get('revision', {}), 'quiet': summary.get('quiet'),
            'candidates': summary.get('candidates', []), 'kernel_args': summary.get('kernel_args', [])}
    if summary.get('ab') or summary.get('bench', {}).get('ab'):
        card['ab'] = summary.get('ab', True)
    if 'test' in summary:
        card['test'] = score_test(summary['test'])
    if 'bench' in summary:
        rows = score_bench(summary['bench'])
        card['bench'] = rows
        card['tiers'] = {}
        for tier in TIER_ORDER:
            tier_rows = [r for r in rows if r['tier'] == tier and r['suite'] == 'bench']
            card['tiers'][tier] = {'pass': sum(r['status'] == 'pass' for r in tier_rows), 'total': len(tier_rows)}
        if card.get('ab'):
            card['tiers_b'] = {tier: {'pass': sum(r.get('status_b') == 'pass' for r in rows
                                                  if r['tier'] == tier and r['suite'] == 'bench'),
                                      'total': card['tiers'][tier]['total']} for tier in TIER_ORDER}
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
    quiet = card.get('quiet')
    if quiet and not quiet.get('quiet'):
        lines.append(f"**Host was CPU-throttled** (quota periods throttled: {quiet.get('throttled')}); performance below is not decision-grade.")
    if card['candidates'] or card['kernel_args']:
        lines.append(f"candidates: {', '.join(card['candidates']) or '-'}; kernel args: {' '.join(card['kernel_args']) or '-'}")
    if card.get('ab'):
        lines.append('guest B: ' + json.dumps(card['ab'], sort_keys=True))
    lines.append('')
    if 'test' in card:
        t = card['test']
        lines += [f"**Functional**: {t['pass']}/{t['checks']} pass, {t['fail']} fail, {t['not_tested']} not tested "
                  f"→ {'PASS' if t['gate'] else 'FAIL'}", '']
    if 'bench' in card:
        prev = {r['metric']: r for r in (previous or {}).get('bench', [])}
        summary = ', '.join(f"{tier} {v['pass']}/{v['total']}" for tier, v in card['tiers'].items())
        ab = bool(card.get('ab'))
        if ab:
            summary = 'A: ' + summary + '; B: ' + ', '.join(
                f"{tier} {v['pass']}/{v['total']}" for tier, v in card['tiers_b'].items())
        extra_header = ' B guest | B/native (range) | B/A (range) |' if ab else ''
        lines += [f'**Performance** (guest/native, higher is better): {summary}', '',
                  '`~noisy`: native samples varied by more than 30%, or the outer container was CPU-throttled '
                  'in more than 10% of quota periods during a sample (host contention).', '',
                  '| tier | metric | native | guest | ratio | target | pair range | Δ prev | status |' + extra_header,
                  '|---|---|---:|---:|---:|---:|---|---:|---|' + ('---:|---|---|' if ab else '')]
        for r in card['bench']:
            delta = '-'
            p = prev.get(r['metric'])
            if p and 'ratio' in p and 'ratio' in r:
                delta = f"{(r['ratio'] - p['ratio']) / p['ratio'] * 100:+.0f}%"
            native = ('ref ' if r.get('reference') else '') + _fmt(r.get('native'))
            spread = f"{r['spread'][0]:.2f}–{r['spread'][1]:.2f}" if r.get('spread') else '-'
            status = r['status'] + (f" ({r['error'][:40]})" if r.get('error') else '') + (' ~noisy' if r.get('noisy') else '')
            extra = ''
            if ab:
                def paired(key):
                    value, span = r.get('ratio_' + key), r.get('spread_' + key)
                    return _fmt(value) + (f' [{span[0]:.4g}–{span[1]:.4g}]' if span else '')
                status += '; B ' + (r.get('status_b') or 'not_tested') + (' ~noisy' if r.get('noisy_b') else '')
                extra = f" {_fmt(r.get('guest_b'))} | {paired('b')} | {paired('ba')}" + (' ~noisy' if r.get('noisy_ba') else '') + ' |'
            lines.append(f"| {r['tier']} | {r['metric']} | {native} | {_fmt(r.get('guest'))} {r.get('unit') or ''} | "
                         f"{_fmt(r.get('ratio'))} | {r['target']:.2f} | {spread} | {delta} | {status} |" + extra)
    return '\n'.join(lines) + '\n'


def load(path):
    return json.loads(pathlib.Path(path).read_text())
