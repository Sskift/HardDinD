"""Clock and rseq semantics in ordinary and privileged containers."""
import json

from pipeline import probe


def run(case, name):
    from pipeline.functional import NotTested, require
    rseq = name == 'rseq'
    binary = probe.program('src/rseq_check.c' if rseq else 'src/clock_vdso_check.c')
    image = case.object('image', 'checker')
    archive = case.root / 'checker.tar'
    archive.write_bytes(probe.image_tar(binary))
    case.d('import', archive, image)
    args = ['--upstream'] if rseq else ['--expect-boot',
             '--mutate-guest' if name == 'clock_privileged' else '--path-only']
    if rseq:
        container = case.object('container', 'checker')
        output = case.d('run', '--rm', '--name', container, '--label', case.label,
                        image, '/probe', *args, timeout=270)
        case.forget_removed(('rm', container))
    else:
        output = case.output('/probe', *args, image=image,
                             opts=('--privileged',) if name == 'clock_privileged' else ())
    row = json.loads(output.splitlines()[-1])
    require(row.get('check') == 'summary' and row.get('pass', 0) > 0 and row.get('fail') == 0,
            'checker did not complete successfully: ' + str(row))
    if row.get('not_tested', 0):
        raise NotTested('checker has untested cases: ' + str(row))
    require(row.get('status') == 'pass', 'checker did not pass: ' + str(row))
