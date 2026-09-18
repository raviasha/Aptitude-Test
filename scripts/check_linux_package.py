"""Destructive-to-fixtures-only acceptance check on a FRESH disposable WSL build VM.

Refuses existing client configuration. Leaves test state in place for inspection.
Never run on a lab PC. Requires an already installed package and root.
"""
import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from unittest.mock import patch

import httpx


def run(*args):
    subprocess.run(args, check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--package', required=True)
    args = parser.parse_args()
    data = Path('/var/lib/ksat/KSAT Client')
    if os.geteuid() != 0 or 'microsoft' not in os.uname().release.lower():
        raise SystemExit('Only run as root in a disposable WSL build distribution.')
    if (data / 'client-config.json').exists():
        raise SystemExit('Existing configuration: refusing to overwrite any client data.')
    import app
    from ksat.coordinator.releases import prepare_release
    from ksat.protocol import FrozenReviewQuestion
    from scripts import load_distributed_assessment as load

    def review_release(connection, **kwargs):
        kwargs['review_questions'] = [FrozenReviewQuestion(
            question_id=r['question_id'], correct_answer=r['correct_answer'],
            solution_steps=['Compare your choice with the frozen answer.'])
            for r in connection.execute('SELECT question_id,correct_answer FROM questions')]
        return prepare_release(connection, **kwargs)

    client = httpx.Client(base_url='http://127.0.0.1:8010', timeout=30)

    def ready():
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                response = client.get('/')
                response.raise_for_status()
                token = re.search(r'name="ksat-csrf" content="([^"]+)"', response.text)
                assert token, 'CSRF meta tag missing'
                client.headers.update({'X-KSAT-CSRF': token.group(1), 'Origin': str(client.base_url).rstrip('/')})
                return
            except httpx.TransportError:
                time.sleep(.3)
        raise AssertionError('Installed service did not become ready')

    def request(method, route, **kwargs):
        if method in ('POST', 'PUT'):
            kwargs.setdefault('json', {})
        response = client.request(method, route, **kwargs)
        assert response.is_success, (route, response.status_code, response.text)
        return response.json()

    try:
        with tempfile.TemporaryDirectory(prefix='ksat-package-test-') as directory, patch.dict(os.environ, {'KSAT_LOAD_TEST': '1'}), patch.object(load, 'prepare_release', side_effect=review_release), load._Fixture(Path(directory), 1, 3, real_https=True) as fixture:
            run('/opt/ksat-client/KSATClient', '--configure', '--base-url', fixture.base_url,
                '--ca', str(fixture.security.ca_certificate_path), '--metadata', str(fixture.security.public_export_dir / 'coordinator-public.json'))
            ready()
            device = request('POST', '/api/device/enroll')['device_id']
            credentials = {'student_id': fixture.student_ids[0], 'password': fixture.password}
            request('POST', '/api/login', json=credentials)
            request('POST', '/api/content/prefetch', json={'release_id': fixture.release_id})
            attempt = request('POST', f'/api/assessments/{fixture.release_id}/start', json={'confirmed': True})
            aid = attempt['attempt_id']
            qid = attempt['question_order'][0]
            request('PUT', f'/api/attempts/{aid}/responses/{qid}', json={'answer': 'A'})
            run('systemctl', 'restart', 'ksat-client')
            ready()
            recovered = request('GET', '/api/attempts/active')['attempt']
            assert recovered['attempt_id'] == aid and recovered['responses'][str(qid)] == 'A'
            request('POST', '/api/login', json=credentials)
            request('POST', f'/api/attempts/{aid}/submit', json={'confirmed': True})
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                result = request('GET', f'/api/attempts/{aid}/result')
                if result['state'] == 'acknowledged_result':
                    break
                time.sleep(.5)
            assert result['state'] == 'acknowledged_result', result
            with app.db() as connection:
                connection.execute('INSERT INTO admins VALUES (?,?,?)', ('package-admin', 'Test Faculty', app.hash_password('package-test-password')))
            login = fixture.test_client.post('/api/login', json={'identifier': 'package-admin', 'password': 'package-test-password', 'role': 'admin'})
            closed = fixture.test_client.post(f'/api/admin/tests/{fixture.test_id}/close', headers={'X-KSAT-CSRF': login.json()['csrf_token']})
            assert closed.status_code == 200, closed.text
            review = request('GET', f'/api/attempts/{aid}/review')
            assert len(review['questions']) == 3
            # The state anchor is a changing database integrity checkpoint, not identity.
            files = [data / 'client-config.json', Path('/etc/ksat-client/device-wrap.key'), data / 'identity/device-key.bin']
            before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.is_file()}
            run('dpkg', '-i', args.package)
            ready()
            after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.is_file()}
            assert before == after, 'Reinstall changed identity/configuration'
            assert request('POST', '/api/device/enroll')['device_id'] == device
            assert request('GET', f'/api/attempts/{aid}/result') == result
            request('POST', '/api/login', json=credentials)
            assert request('GET', f'/api/attempts/{aid}/review')['questions'] == review['questions']
            print('PASS: installed binary enrollment, login, exam, saved-answer restart, submission, review, reinstall identity/config/result retention', flush=True)
    finally:
        client.close()
        run('systemctl', 'stop', 'ksat-client')


if __name__ == '__main__':
    main()
