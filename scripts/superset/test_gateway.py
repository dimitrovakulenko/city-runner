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
    async def test_multiple_preview_waiters_reserve_slots_before_async_validation(self):
        gateway.boundary.blocked.clear()
        first, second, started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = 0
        async def upstream(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                started.set()
            await (first if calls <= 2 else second).wait()
            return Response(status_code=200)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url='http://127.0.0.1:8090') as client:
            await client.post('/pilot/login/pilot-a')
            with patch.object(gateway.boundary, 'upstream', upstream):
                requests = [asyncio.create_task(client.post('/api/v1/chart/data')) for _ in range(5)]
                try:
                    await asyncio.wait_for(started.wait(), 3)
                    await asyncio.sleep(0.05)
                    first.set()
                    await asyncio.sleep(0.1)
                    self.assertEqual(calls, 4)
                    self.assertEqual(sum(request.done() for request in requests), 2)
                finally:
                    first.set(); second.set()
                    responses = await asyncio.gather(*requests)
                self.assertTrue(all(response.status_code == 200 for response in responses))

    async def test_saved_dashboard_chart_fanout_waits_for_the_same_sql_slots(self):
        gateway.boundary.blocked.clear()
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def upstream(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                started.set()
            await release.wait()
            return Response(status_code=200)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url='http://127.0.0.1:8090') as client:
            await client.post('/pilot/login/pilot-a')
            with patch.object(gateway.boundary, 'upstream', upstream):
                first = [asyncio.create_task(client.get(f'/api/v1/chart/{number}/data/')) for number in (1, 2)]
                await asyncio.wait_for(started.wait(), 3)
                queued = asyncio.create_task(client.get('/api/v1/chart/3/data/'))
                await asyncio.sleep(0.05)
                self.assertFalse(queued.done())
                self.assertEqual(calls, 2)
                release.set()
                self.assertTrue(all(response.status_code == 200 for response in await asyncio.gather(*first, queued)))

    async def test_metadata_load_does_not_consume_sql_slots(self):
        gateway.boundary.blocked.clear()
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
            with patch.object(gateway.boundary, 'upstream', upstream):
                queries = [asyncio.create_task(client.post('/api/v1/sqllab/execute/')) for _ in range(2)]
                stop = None
                try:
                    await asyncio.wait_for(admitted.wait(), 3)
                    self.assertEqual((await client.post('/api/v1/sqllab/execute/')).status_code, 429)
                    responses = await asyncio.gather(*[
                        client.get('/api/v1/chart/'), client.get('/api/v1/dashboard/'),
                        client.get('/api/v1/chart/1'), client.get('/api/v1/dashboard/_info'),
                        client.get('/api/v1/dashboard/1/datasets'), client.get('/api/v1/dashboard/1/charts'),
                        client.get('/api/v1/dataset/1/drill_info/'),
                    ])
                    self.assertTrue(all(response.status_code == 200 for response in responses))
                    for path in ('/api/v1/chart/', '/api/v1/dashboard/1', '/api/v1/saved_query/1', '/api/v1/explore/form_data/',
                                 '/api/v1/dashboard/1/copy/', '/api/v1/dashboard/1/filter_state'):
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
