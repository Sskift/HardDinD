"""Functional Docker suite (placeholder until the real suite lands)."""


def run(ctx):
    result = ctx.docker('version', '--format', '{{.Server.Version}}', timeout=60)
    status = 'pass' if result.returncode == 0 and result.stdout.strip() else 'fail'
    return {'schema': 'hd.test/1', 'checks': [{'id': 'daemon_version', 'status': status, 'covers': ['version'],
                                               'detail': (result.stdout + result.stderr)[-300:]}]}
