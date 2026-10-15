#!/usr/bin/env python3
"""Compile an actual GPU source snapshot into a bounded userspace fixture.

This checks forwarding interleavings and error/refcount paths. It does not
emulate kernel scheduling, GPU hardware, or all VFS/user-access semantics.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent


def extract(source):
    # Keep byte positions, ignoring comments and literals while finding braces.
    clean = re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
                   lambda m: ' ' * len(m[0]), source, flags=re.S)
    chunks = []
    for name in ['gpu_nested_ptr', 'gpu_fd_field', 'gpu_fd_hold']:
        match = list(re.finditer(r'\bstruct\s+' + name + r'\s*\{', clean))
        if len(match) != 1:
            raise ValueError('expected one struct definition: ' + name)
        end = clean.index('};', match[0].end()) + 2
        chunks.append(source[match[0].start():end])
    for name in ['GPU_KIND_UVM', 'GPU_MAX_POKES', 'GPU_MAX_FIELDS']:
        matches = re.findall(r'^#define\s+' + name + r'\s+\d+\b[^\n]*', source, re.M)
        if len(matches) != 1:
            raise ValueError('expected one constant: ' + name)
        chunks.append(matches[0])
    for name in ['gpu_hold_fd', 'gpu_put_holds', 'gpu_check_nested',
                 'gpu_shadow_snapshot', 'gpu_forward_block']:
        matches = list(re.finditer(r'\bstatic\s+(?:long|void)\s+' + name + r'\s*\([^;{}]*\)\s*\{', clean))
        if len(matches) != 1:
            raise ValueError('expected one function definition: ' + name)
        start = matches[0].start(); pos = matches[0].end(); depth = 1
        while depth and pos < len(clean):
            depth += (clean[pos] == '{') - (clean[pos] == '}')
            pos += 1
        if depth:
            raise ValueError('unterminated function: ' + name)
        chunks.append(source[start:pos])
    return '\n\n'.join(chunks) + '\n'


def command(argv, timeout):
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate(timeout=10)
    # Compilers can leave descendants even after their leader exits.
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        empty = True
    else:
        os.killpg(process.pid, signal.SIGKILL)
        empty = False
    return {'argv': argv, 'exit_code': process.returncode, 'timeout': timed_out,
            'group_empty_at_exit': empty, 'stdout': stdout, 'stderr': stderr}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='frozen arch/um/kernel/gpu.c')
    parser.add_argument('--cc', default=os.environ.get('CC', 'cc'))
    parser.add_argument('--output', required=True, type=Path, help='new JSON result file')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('refusing to replace existing results')
    source = args.source.read_bytes()
    includes = args.source.parent.parent / 'include/shared'
    constants = []
    headers = {}
    for path, name in [(includes / 'as-layout.h', 'STUB_GPU_SHADOW_PAGES'),
                       (includes / 'skas/stub-data.h', 'GPU_OP_IOCTL_ADDR')]:
        data = path.read_bytes()
        matches = re.findall(r'^#define\s+' + name + r'\s+(\d+)\b', data.decode(), re.M)
        if len(matches) != 1:
            raise ValueError('expected one header constant: ' + name)
        constants.append('#define ' + name + ' ' + matches[0])
        headers[str(path)] = hashlib.sha256(data).hexdigest()
    fixture = ROOT / 'tests/gpu_usercopy/fixture.c'
    generated = extract(source.decode())
    report = {'scope': 'Source-bound native fixture; not actual GPU or guest acceptance.',
              'platform': platform.platform(),
              'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'source_sha256': hashlib.sha256(source).hexdigest(),
              'fixture_sha256': hashlib.sha256(fixture.read_bytes()).hexdigest(),
              'extracted_sha256': hashlib.sha256(generated.encode()).hexdigest(),
              'header_sha256': headers,
              'full_acceptance': False, 'performance_acceptance': False, 'commands': []}
    compiler = command([args.cc, '--version'], 10)
    report['commands'].append(compiler)
    compiler_ok = compiler['exit_code'] == 0 and not compiler['timeout'] and compiler['group_empty_at_exit']
    with tempfile.TemporaryDirectory(prefix='harddind-gpu-usercopy-') as directory:
        directory = Path(directory)
        (directory / 'extracted.inc').write_text(generated)
        (directory / 'model_constants.inc').write_text('\n'.join(constants) + '\n')
        shutil.copy2(fixture, directory / 'fixture.c')
        binary = directory / 'fixture'
        compiled = command([args.cc, '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                            '-pthread', str(directory / 'fixture.c'),
                            '-o', str(binary)], 60)
        report['commands'].append(compiled)
        ok = compiler_ok and compiled['exit_code'] == 0 and not compiled['timeout'] and compiled['group_empty_at_exit']
        if ok:
            for options, expected in [([], 0), (['--alias-negative'], 1)]:
                run = command([str(binary), *options], 20)
                report['commands'].append(run)
                passed = run['exit_code'] == expected and not run['timeout'] and run['group_empty_at_exit']
                if passed:
                    data = json.loads(run['stdout'])
                    passed = data['alias_negative'] == bool(options) and data['failures'] == expected
                    if options:
                        passed = passed and "A received another request's output" in run['stderr']
                ok = ok and passed
        report['passed'] = ok
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as output:
        json.dump(report, output, indent=2); output.write('\n')
    print(json.dumps({'passed': ok, 'output': str(args.output), 'source_sha256': report['source_sha256']}))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
