"""Regression checks for concurrent sessions and browser metadata admission."""

import asyncio
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from starlette.responses import Response

import gateway


class SessionTests(unittest.TestCase):
    def test_handoff_rejects_invalid_revoked_and_cross_origin_sessions(self):
        with TestClient(gateway.app) as client:
            client.post('/pilot/login/pilot-a', follow_redirects=False)
            token = client.cookies[gateway.COOKIE]
            headers = {'Authorization': f'Bearer {token}'}
            self.assertEqual(client.post('/pilot/handoff', headers=headers, follow_redirects=False).status_code, 303)
            self.assertEqual(client.post('/pilot/handoff', headers={'Authorization': 'Bearer invalid'}).status_code, 401)
            self.assertEqual(gateway.owner(token), 'pilot-a')
            self.assertEqual(client.post('/pilot/handoff', headers={**headers, 'Origin': 'https://other.example'}).status_code, 403)
            client.post('/pilot/logout', follow_redirects=False)
            self.assertEqual(client.post('/pilot/handoff', headers=headers).status_code, 401)

    def test_read_during_logout_cannot_rollback_revocation(self):
        with TestClient(gateway.app) as client:
            client.post('/pilot/login/pilot-a', follow_redirects=False)
            token = client.cookies[gateway.COOKIE]
            revoked = threading.Event()
            release = threading.Event()
            reader_started = threading.Event()
            revoke = gateway.revoke_token

            def pause_after_update(db, value):
                result = revoke(db, value)
                revoked.set()
                self.assertTrue(release.wait(3))
                return result

            def read_owner():
                reader_started.set()
                return gateway.owner(token)

            with patch.object(gateway, 'revoke_token', pause_after_update), ThreadPoolExecutor(2) as pool:
                logout = pool.submit(client.post, '/pilot/logout', follow_redirects=False)
                self.assertTrue(revoked.wait(3))
                read = pool.submit(read_owner)
                self.assertTrue(reader_started.wait(3))
                try:
                    # The pending read must not touch the writer's connection.
                    self.assertFalse(read.done())
                finally:
                    release.set()
                self.assertEqual(logout.result().status_code, 303)
                self.assertIsNone(read.result())
            self.assertIsNone(gateway.owner(token))


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_load_does_not_consume_sql_slots(self):
        gateway.BLOCKED.clear()
        admitted = asyncio.Event()
        release = asyncio.Event()
        control_admitted = asyncio.Event()
        release_control = asyncio.Event()
        calls = 0

        async def upstream(path, *args):
            nonlocal calls
            if path == 'api/v1/sqllab/execute/':
                calls += 1
                if calls == 2:
                    admitted.set()
                await release.wait()
            if path == 'api/v1/query/stop':
                control_admitted.set()
                await release_control.wait()
            return Response(status_code=200)

        transport = httpx.ASGITransport(app=gateway.app)
        async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:8090') as client:
            await client.post('/pilot/login/pilot-a')
            with patch.object(gateway, 'upstream', upstream):
                queries = [asyncio.create_task(client.post('/api/v1/sqllab/execute/')) for _ in range(2)]
                stop = None
                try:
                    await asyncio.wait_for(admitted.wait(), 3)
                    self.assertEqual((await client.post('/api/v1/sqllab/execute/')).status_code, 429)
                    responses = await asyncio.gather(*[
                        client.get('/api/v1/chart/'), client.get('/api/v1/dashboard/'),
                        client.get('/api/v1/chart/1'), client.get('/api/v1/dashboard/_info'),
                        client.get('/api/v1/dashboard/1/datasets'), client.get('/api/v1/dashboard/1/charts'),
                    ])
                    self.assertTrue(all(response.status_code == 200 for response in responses))
                    for path in ('/api/v1/chart/', '/api/v1/dashboard/1', '/api/v1/saved_query/1', '/api/v1/explore/form_data/'):
                        self.assertEqual((await client.post(path)).status_code, 200)
                    stop = asyncio.create_task(client.post('/api/v1/query/stop'))
                    await asyncio.wait_for(control_admitted.wait(), 3)
                    self.assertEqual((await client.post('/api/v1/query/stop')).status_code, 429)
                    # Data, exports, samples and unknown paths cannot evade SQL admission.
                    for path in ('/api/v1/chart/data', '/api/v1/chart/1/data/',
                                 '/api/v1/sqllab/export/abc/', '/api/v1/dataset/1/samples'):
                        self.assertEqual((await client.get(path)).status_code, 429)
                finally:
                    release.set()
                    release_control.set()
                    await asyncio.gather(*queries)
                    if stop:
                        self.assertEqual((await stop).status_code, 200)
