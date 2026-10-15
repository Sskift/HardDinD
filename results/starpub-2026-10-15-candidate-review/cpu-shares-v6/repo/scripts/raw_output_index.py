"""Type-aware indexing of recorded command outputs (production finalization).

The runner archives every command as <label>.stdout/<label>.stderr and a
commands.log. Finalization must summarize what each file actually contained
WITHOUT assuming the parsed JSON is an object: a pretty-printed instance
state file is one JSON object spread over many lines; wait-a.stdout is a bare
JSON integer; a probe JSONL line that is not an object is an integrity
failure, never a successful probe record.

This module is imported by run-cpu-shares.py and exercised end-to-end by the
regression tests against real archived result directories; it invents no
evidence (a missing file or a missing commands.log is recorded as such).
"""
import json
import re

PROBE_JSONL_LABELS = ('logs-a', 'logs-b')
PROBE_OBJECT_PREFIXES = ('snapshot-a-', 'snapshot-b-', 'preupd-a-', 'preupd-b-',
                         'postupd-a-', 'postupd-b-')
PROBE_OBJECT_EXACT = ('probe-selftest',)
PRETTY_JSON_OBJECT_EXACT = ('instance-start', 'instance-stop')
JSON_SCALAR_PREFIXES = ('wait-',)      # bare exit code JSON
PROBE_LOG_RE = re.compile(r'^(logs-[ab])$')
CMDLINE_RE = re.compile(r'^\[(\S+) exit=(-?\d+) timeout=(True|False)\]\s*$')


def _json_value_type(value):
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, int):
        return 'number'
    if isinstance(value, float):
        return 'number'
    if isinstance(value, str):
        return 'string'
    if isinstance(value, list):
        return 'array'
    if isinstance(value, dict):
        return 'object'
    return type(value).__name__


def _index_probe_jsonl(label, text):
    """Strict: EVERY non-empty line must be a compact JSON object."""
    events, errors = [], []
    lines = text.splitlines()
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as e:
            errors.append({'line': n, 'kind': 'malformed_json', 'error': str(e)})
            continue
        if not isinstance(value, dict):
            # A scalar/array/null on a probe channel is NEVER a valid record.
            errors.append({'line': n, 'kind': 'non_object_record',
                           'json_type': _json_value_type(value)})
            continue
        events.append(value)
    if not events:
        errors.append({'kind': 'empty_probe_stream',
                     'nonempty_lines': sum(1 for l in lines if l.strip())})
    return {'kind': 'probe_jsonl', 'nonempty_lines': sum(
        1 for l in lines if l.strip()), 'events': len(events),
        'parse_errors': errors,
        'first_events': [e.get('event') for e in events[:3]],
        'last_event': events[-1].get('event') if events else None}


def _index_single_json_object(label, text):
    """A compact single-line probe object OR a whole-file pretty object."""
    stripped = text.strip()
    if not stripped:
        return {'kind': 'empty', 'nonempty_lines': 0,
                'errors': [{'kind': 'empty_expected_json_object', 'label': label}]}
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as e:
        return {'kind': 'invalid_json',
                'nonempty_lines': len([l for l in text.splitlines() if l.strip()]),
                'errors': [{'kind': 'malformed_json', 'error': str(e)}]}
    if not isinstance(value, dict):
        return {'kind': 'invalid_json_shape',
                'json_type': _json_value_type(value),
                'errors': [{'kind': 'non_object_record',
                            'json_type': _json_value_type(value)}]}
    return {'kind': 'json_object', 'multiline': '\n' in stripped,
            'top_level_keys': sorted(value.keys())[:12],
            'event': value.get('event'),
            'nonempty_lines': len([l for l in text.splitlines() if l.strip()]),
            'errors': []}


def _index_scalar_or_text(label, text):
    stripped = text.strip()
    if not stripped:
        return {'kind': 'empty', 'nonempty_lines': 0, 'errors': []}
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        # Normal: docker text output (inspect templates, info, names).
        return {'kind': 'text',
                'nonempty_lines': len([l for l in text.splitlines() if l.strip()]),
                'errors': []}
    if isinstance(value, dict):
        # Unexpected for these labels but valid JSON; record as object text.
        return {'kind': 'json_object_text', 'json_type': 'object',
                'multiline': '\n' in stripped,
                'top_level_keys': sorted(value.keys())[:12], 'errors': []}
    return {'kind': 'json_scalar', 'json_type': _json_value_type(value),
            'value': value if isinstance(value, (str, int, float, bool)) or
            value is None else None,
            'nonempty_lines': len([l for l in text.splitlines() if l.strip()]),
            'errors': []}


def classify_label(label):
    if PROBE_LOG_RE.match(label):
        return 'probe_jsonl'
    if label in PROBE_OBJECT_EXACT:
        return 'json_object'
    if label.startswith(PROBE_OBJECT_PREFIXES):
        return 'json_object'
    if label in PRETTY_JSON_OBJECT_EXACT:
        return 'json_object'
    if label.startswith(JSON_SCALAR_PREFIXES):
        return 'scalar_or_text'
    return 'text'


def index_output(label, text):
    """Type-aware index of one recorded stdout. Never raises: every failure
    shape is returned as a diagnostic error plus an integrity flag."""
    kind = classify_label(label)
    try:
        if kind == 'probe_jsonl':
            rec = _index_probe_jsonl(label, text)
        elif kind == 'json_object':
            rec = _index_single_json_object(label, text)
        else:
            rec = _index_scalar_or_text(label, text)
    except Exception as error:  # defensive: an indexer bug must not crash finalize
        rec = {'kind': 'indexer_exception',
               'errors': [{'kind': 'indexer_exception',
                           'error': '%s: %s' % (type(error).__name__, error)}]}
    rec['label'] = label
    rec['expected_kind'] = kind
    rec['integrity_ok'] = not (rec.get('errors') or rec.get('parse_errors'))
    return rec


def read_command_log(path):
    """Parse commands.log into metadata actually present on disk. Returns
    ({label: {...}}, errors); it does NOT fabricate missing fields."""
    meta, errors = {}, []
    if not path.exists():
        return meta, ['commands.log missing: command metadata unavailable']
    seen = set()
    for n, line in enumerate(path.read_text(errors='replace').splitlines(), 1):
        m = CMDLINE_RE.match(line)
        if m:
            label, code, timeout = m.group(1), int(m.group(2)), m.group(3) == 'True'
            if label in seen:
                errors.append('duplicate commands.log label %s at line %d' % (label, n))
            seen.add(label)
            meta[label] = {'label': label, 'exit_code': code,
                           'timed_out': timeout, 'commands_log_line': n}
    return meta, errors


def index_results_dir(results_dir):
    """Production finalization indexing. Reads commands.log and every archived
    .stdout/.stderr. Returns JSON-native dict with a separate diagnostic
    `errors` list; integrity_ok is False when an EXPECTED probe record is
    malformed/scalar or required files are missing. Nothing is invented."""
    p = results_dir
    meta, meta_errors = read_command_log(p / 'commands.log')
    labels = set(meta)
    for f in p.glob('*.stdout'):
        labels.add(f.name[:-len('.stdout')])
    for f in p.glob('*.stderr'):
        labels.add(f.name[:-len('.stderr')])
    index, errors, present, missing_stdout, missing_stderr = [], [], [], [], []
    integrity_ok = not meta_errors
    if meta_errors:
        errors.extend({'kind': 'command_log', 'error': e} for e in meta_errors)
    for label in sorted(labels):
        sf, ef = p / (label + '.stdout'), p / (label + '.stderr')
        rec = None
        if sf.exists():
            present.append(label)
            rec = index_output(label, sf.read_text(errors='replace'))
            rec['stdout_bytes'] = sf.stat().st_size
            if not rec.pop('integrity_ok'):
                integrity_ok = False
        else:
            missing_stdout.append(label)
            rec = {'label': label, 'expected_kind': classify_label(label),
                   'kind': 'missing_stdout',
                   'errors': [{'kind': 'missing_stdout', 'label': label}]}
            # A missing stdout for a command listed in commands.log is an
            # integrity hole (the file was never archived or lost).
            if label in meta:
                integrity_ok = False
        if ef.exists():
            rec['stderr_bytes'] = ef.stat().st_size
            errtxt = ef.read_text(errors='replace')
            rec['stderr_nonempty_lines'] = len(
                [l for l in errtxt.splitlines() if l.strip()])
        elif label in meta:
            missing_stderr.append(label)
        rec['in_commands_log'] = label in meta
        rec_errors = rec.get('errors') or rec.get('parse_errors') or []
        for e in rec_errors:
            errors.append({'label': label, **e})
        index.append(rec)
    if missing_stdout:
        errors.append({'kind': 'missing_stdout_set', 'labels': sorted(missing_stdout)})
    return {'results_dir': str(p), 'labels_indexed': len(labels),
            'labels_in_commands_log': len(meta),
            'commands_meta': sorted(meta.values(), key=lambda r: r['commands_log_line']),
            'index': index, 'errors': errors,
            'missing_stdout': sorted(missing_stdout),
            'missing_stderr': sorted(missing_stderr),
            'integrity_ok': integrity_ok}
