"""Static, embedded rseq selftests from the pinned kernel archive."""
import pathlib
import shutil
import tarfile

from pipeline import build

FLAGS = ['-O2', '-Wall', '-Wextra', '-Werror', '-static', '-pthread']
TESTS = ('basic_percpu_ops_test', 'param_test', 'param_test_compare_twice', 'syscall_errors_test')


def extract_sources(archive, root):
    with tarfile.open(archive, 'r|*') as tar:
        for member in tar:
            parts = pathlib.PurePosixPath(member.name).parts
            name = pathlib.PurePosixPath(*parts[1:])
            if '..' in parts or pathlib.PurePosixPath(member.name).is_absolute():
                raise ValueError('invalid kernel archive path')
            if not member.isfile() or not (str(name).startswith((
                    'tools/testing/selftests/rseq/', 'tools/include/', 'tools/arch/x86/include/')) or
                    str(name).startswith('tools/testing/selftests/kselftest') or
                    str(name) == 'include/uapi/linux/auxvec.h'):
                continue
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(tar.extractfile(member).read())


def program(source):
    key = build.key_of('vdso-rseq/1', build.LOCK['vdso_rseq'], FLAGS, source, pathlib.Path(__file__))
    def compile_(tmp, _final):
        archive = build.fetch('vdso_rseq')
        root = tmp / 'source'
        root.mkdir()
        extract_sources(archive, root)
        tests = root / 'tools/testing/selftests/rseq'
        includes = [f'-I{p}' for p in (tests, tests.parent, root / 'include/uapi',
                                     root / 'tools/include', root / 'tools/arch/x86/include')]
        assembly = ['.section .rodata']
        for name in TESTS:
            compare = name == 'param_test_compare_twice'
            defines = ['-DRSEQ_COMPARE_TWICE'] if compare else []
            source_name = 'param_test' if compare else name
            build.run(['gcc', *FLAGS, '-Wno-unused-parameter', '-Wno-sign-compare', *defines, *includes,
                       tests / (source_name + '.c'), tests / 'rseq.c', '-ldl', '-o', tmp / name],
                      log_file=tmp / 'build.log')
            assembly += [f'.global hd_{name}_start, hd_{name}_end', f'hd_{name}_start:',
                         f'.incbin "{tmp / name}"', f'hd_{name}_end:']
        assembly += ['.section .note.GNU-stack,"",@progbits']
        packed = tmp / 'upstream.S'
        packed.write_text('\n'.join(assembly) + '\n')
        build.run(['gcc', *FLAGS, '-DHD_RSEQ_UPSTREAM', source, packed, '-o', tmp / source.stem],
                  log_file=tmp / 'build.log')
        shutil.rmtree(root)
    return build.artifact('vdso-rseq', key, compile_) / source.stem
