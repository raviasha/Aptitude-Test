import base64
import hashlib
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from ksat.client.updates import ClientUpdateManager
from ksat.coordinator.client_updates import ClientUpdateStore, UpdateStateError
from ksat.coordinator.schema import migrate_distributed_schema
from ksat.crypto import generate_ed25519_keypair
from ksat.sqlite import connect_sqlite
from ksat.update_protocol import parse_client_update
from scripts.build_client_update import build_client_update


class Transport:
    def __init__(self, policy, bundle): self.policy=policy; self.bundle=bundle
    def update_policy(self, _version): return self.policy
    def download_update_range(self, _release, offset, destination):
        with Path(destination).open('ab' if offset else 'wb') as stream: stream.write(self.bundle[offset:])
        return len(self.bundle),len(self.bundle)
    def report_update_status(self, *_args, **_kwargs): return {}


class ClientUpdateEndToEndTests(unittest.TestCase):
    def test_pilot_publish_offline_reconnect_and_assessment_deferral(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); db=root/'coordinator.sqlite3'
            with closing(connect_sqlite(db)) as connection:
                connection.executescript('CREATE TABLE tests(test_id INTEGER PRIMARY KEY); CREATE TABLE attempts(attempt_id TEXT PRIMARY KEY, student_id TEXT);')
                migrate_distributed_schema(connection)
                now=datetime.now(timezone.utc).isoformat()
                for device in ('a','b'):
                    connection.execute('INSERT INTO devices VALUES (?,?,?,?,?,NULL)',(str(uuid.uuid4()),device,base64.b64encode(b'x'*32).decode(),'active',now))
                connection.commit()
                ids=[row['device_id'] for row in connection.execute('SELECT device_id FROM devices ORDER BY label')]
            installer=root/'setup.exe'; installer.write_bytes(b'signed installer')
            private,public=generate_ed25519_keypair(); key=root/'key'; key.write_bytes(base64.b64decode(private))
            release_id=str(uuid.uuid4()); bundle_path=root/'release.ksat-client-update'
            build_client_update(installer=installer,version='2.2.0',minimum_source_version='2.1.0',publisher='CN=KSIT',private_key_file=key,output=bundle_path,release_notes='Acceptance',release_id=release_id,published_at=datetime(2026,9,24,tzinfo=timezone.utc),authenticode_verifier=lambda _p,p:{'publisher':p})
            verified=parse_client_update(bundle_path,public,lambda _p,p:{'publisher':p})
            with closing(connect_sqlite(db)) as connection:
                store=ClientUpdateStore(connection,root/'Client Updates')
                store.upload(verified); store.select_pilot(release_id,ids[0])
                with self.assertRaises(UpdateStateError): store.publish(release_id)
                attempt=str(uuid.uuid4())
                store.record_status(release_id,ids[0],'healthy','2.2.0',None,attempt_id=attempt)
                store.publish(release_id)
                policy=store.policy_for_device(ids[1],'2.1.0')
            bundle=bundle_path.read_bytes(); transport=Transport(policy,bundle)
            client_root=root/'client-b'; client_root.mkdir()
            identity=client_root/'identity'; config=client_root/'config'; state=client_root/'state'
            identity.write_bytes(b'identity'); config.write_bytes(b'config'); state.write_bytes(b'state')
            before=[hashlib.sha256(path.read_bytes()).hexdigest() for path in (identity,config,state)]
            manager=ClientUpdateManager(client_root/'updates',transport,'2.1.0',public,lambda _p,p:{'publisher':p})
            self.assertEqual('deferred_active_attempt',manager.check(active_attempt=True).stage)
            self.assertEqual('deferred_pending_submission',manager.check(pending_submission=True).stage)
            self.assertEqual('required',manager.check().stage)
            self.assertEqual('ready_to_install',manager.download().stage)
            self.assertEqual(before,[hashlib.sha256(path.read_bytes()).hexdigest() for path in (identity,config,state)])

            def read_policy(_index):
                with closing(connect_sqlite(db)) as connection:
                    return ClientUpdateStore(connection,root/'Client Updates').policy_for_device(ids[1],'2.1.0')['release_id']
            with ThreadPoolExecutor(max_workers=12) as pool:
                self.assertEqual({release_id},set(pool.map(read_policy,range(100))))


if __name__ == '__main__': unittest.main()
