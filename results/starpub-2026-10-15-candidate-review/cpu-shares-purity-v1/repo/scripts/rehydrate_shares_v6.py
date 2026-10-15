#!/usr/bin/env python3
"""Actual-v6 evidence rehydration + report-purity verification (read-only).

This is a RECONSTRUCTION, not a new experiment and not a functional rerun:
it rebuilds the evaluator input from the original raw captures that were
already recorded on disk by the v6 run (logs-a/b contend JSONL,
preupd/postupd/snapshot/*.stdout JSON snapshots, inspect templates,
ps-leftovers, daemon-info, and the frozen report's commands/hashes/params/
final_state). Nothing is measured, inferred or invented beyond parsing those
files; the immutable results directory and the normalized original report are
never modified.

It documents and enforces three things:

1. HISTORICAL DEFECT: the frozen v6 report's serialized `evidence` was mutated
   by the v5-era evaluator (update pre/post normalized in place, and an added
   stage_snapshots key), so directly re-running evaluate() on that stored
   evidence FAILS ("pre bracket is not a snapshot event") instead of
   reproducing its own verdict.
2. CORRECT REPLAY: evidence rehydrated from the RAW captures, evaluated by
   the now-pure evaluate(), reproduces the exact original 17 check rows.
3. PURITY: evaluate() called twice returns identical rows and leaves its
   input byte-identical before and after.

The stall section is a bounded read-only diagnostic of what the raw counters
can distinguish (sampler/observation stall vs actual workload progress); it
never produces a pass.
"""
import argparse
import importlib.util
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + '.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


checks = _load('cpu_share_checks')
reanalyze = _load('reanalyze_shares_run')

REQUIRED = checks.REQUIRED_CHECK_NAMES


def _rows(result):
    return {name: result[name]['status'] for name in REQUIRED}


def rehydrate(results_dir, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    results_dir = results_dir.resolve()
    report, rehydrated_ev, raw_replay = reanalyze.replay(results_dir)
    frozen_rows = {k: v['status'] for k, v in report['checks'].items()}
    frozen_verdict = report.get('verdict')

    # 1. Historical defect: replay the STORED (mutated) evidence directly.
    stored = report.get('evidence') or {}
    defect = None
    if stored:
        before_stored = json.dumps(stored, sort_keys=True)
        stored_rows = _rows(checks.evaluate(stored))
        after_stored = json.dumps(stored, sort_keys=True)
        defect = {
            'direct_replay_statuses': stored_rows,
            'probe_streams_direct_replay': stored_rows.get('probe_streams'),
            'reproduces_original_17': stored_rows == frozen_rows,
            'stored_evidence_still_mutated_after_eval': after_stored != before_stored,
            'note': ('frozen report serialized evaluator-mutated evidence '
                     '(normalized pre/post + stage_snapshots); direct replay '
                     'cannot reproduce its own verdict')}
    else:
        defect = {'direct_replay_statuses': None,
                  'note': 'frozen report has no serialized evidence to replay'}

    # 2. Correct replay from raw captures (purity fix in effect).
    raw_before = json.dumps(rehydrated_ev, sort_keys=True)
    corr_full_1 = checks.evaluate(rehydrated_ev)
    corr_rows_1 = _rows(corr_full_1)
    raw_mid = json.dumps(rehydrated_ev, sort_keys=True)
    corr_rows_2 = _rows(checks.evaluate(rehydrated_ev))
    raw_after = json.dumps(rehydrated_ev, sort_keys=True)
    rehydrated_verdict = checks.overall_verdict(
        corr_full_1, report.get('errors') or [])

    corrected = {
        'rehydrated_rows_match_original_17': corr_rows_1 == frozen_rows,
        'evaluate_twice_identical': corr_rows_1 == corr_rows_2,
        'input_byte_identical_before_after': raw_before == raw_mid == raw_after,
        'rehydrated_verdict': rehydrated_verdict,
        'frozen_verdict': frozen_verdict,
        'frozen_17': frozen_rows,
        'rehydrated_17': corr_rows_1}

    out = {
        'kind': 'shares_v6_rehydration_read_only',
        'source_results_dir': str(results_dir),
        'is_reconstruction_not_rerun': True,
        'historical_defect': defect,
        'corrected': corrected,
        'raw_capture_records': raw_replay,
        'functional_verdict_note': (
            'read-only reconstruction of an INDETERMINATE run; no functional '
            'criterion changed; no pass is produced')}
    (out_dir / 'v6-rehydration.json').write_text(json.dumps(out, indent=2) + '\n')

    ok = (defect.get('reproduces_original_17') is False
          and corrected['rehydrated_rows_match_original_17']
          and corrected['evaluate_twice_identical']
          and corrected['input_byte_identical_before_after'])
    return out, ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('results_dir')
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    out, ok = rehydrate(pathlib.Path(a.results_dir), pathlib.Path(a.out))
    print(json.dumps({
        'frozen_verdict': out['corrected']['frozen_verdict'],
        'frozen_defect_probe_streams': out['historical_defect'].get('probe_streams_direct_replay'),
        'rehydrated_matches_original_17': out['corrected']['rehydrated_rows_match_original_17'],
        'evaluate_twice_identical': out['corrected']['evaluate_twice_identical'],
        'input_byte_identical': out['corrected']['input_byte_identical_before_after']},
        indent=2))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
