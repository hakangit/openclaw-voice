#!/usr/bin/env python3
"""Regression tests for SIP registration truthfulness.

One defect is pinned here:

1. **Stale "registered" flag.** `SIPClient.register()` tears the existing
   contact down (Contact: *, Expires: 0) before creating the new one, but it
   never cleared `self.registered` on the failure paths. After a failed
   re-registration the daemon therefore still reported
   `"registered": true` on /health, even though the PBX had dropped the
   extension and callers could not reach it.

   That is the worst possible failure for a watchdog: the daemon looked
   healthy precisely when it was deaf. The health endpoint is the only signal
   the heartbeat automation has, so a lying flag means the outage is invisible
   until a customer reports it by phone.

   The fix clears `registered` up front (the old contact is invalid the moment
   the re-register starts) and keys the re-registration loop on a separate
   `_re_register_enabled` flag so a single failure cannot abandon the
   extension permanently.
"""
import asyncio
import sys
import unittest

sys.path.insert(0, '/root/.openclaw/workspace/openclaw-voice')

import sip_client
from sip_client import SIPClient


def _client(port=9, extension='199'):
    """A client pointed at a closed local port, so REGISTER cannot succeed."""
    return SIPClient({
        'sip_server': '127.0.0.1',
        'sip_port': port,
        'extension': extension,
        'password': 'not-a-real-secret',
        'display_name': 'test',
        'external_ip': '127.0.0.1',
        'rtp_port': 40000,
        'sip_local_port': 0,
    })


class RegistrationFlagTests(unittest.TestCase):
    """A failed registration must not leave a 'registered' flag behind."""

    def test_failed_register_clears_the_registered_flag(self):
        client = _client()
        client.registered = True          # pretend it was registered
        client.registered_at = 12345.0    # ...with an old timestamp

        ok = asyncio.run(client.register())

        self.assertFalse(ok, "register() must report failure when no REGISTER succeeds")
        self.assertFalse(
            client.registered,
            "A failed registration must clear `registered`; otherwise /health "
            "reports the daemon as healthy while the PBX has dropped it",
        )

    def test_failed_register_clears_the_timestamp(self):
        client = _client()
        client.registered = True
        client.registered_at = 12345.0

        asyncio.run(client.register())

        self.assertIsNone(
            client.registered_at,
            "A failed registration must not leave a stale success timestamp",
        )

    def test_registered_flag_survives_while_the_attempt_is_in_flight(self):
        """
        During a re-register the previous registration may still be serving
        callers. The flag must not flip to false until the attempt has
        actually failed, or every re-register would raise a false outage.
        """
        client = _client()
        client.registered = True
        client.registered_at = 12345.0

        seen = {}

        async def fake_unregister_all():
            # Mid-attempt: the old contact is being torn down but the new one
            # has not been accepted yet.
            seen['during'] = client.registered
            return False

        client.unregister_all = fake_unregister_all

        async def fake_sleep(_):
            return None

        orig_sleep = asyncio.sleep
        asyncio.sleep = fake_sleep
        try:
            # No REGISTER response will arrive, so this ends in failure.
            asyncio.run(client.register())
        finally:
            asyncio.sleep = orig_sleep

        self.assertTrue(
            seen.get('during', False),
            "The registered flag must not be cleared before the attempt fails",
        )
        self.assertFalse(
            client.registered,
            "Once the attempt has failed the flag must be cleared",
        )

    def test_failed_register_leaves_health_claiming_registered(self):
        """End-to-end: the health payload must not say 'registered'."""
        import openclaw_voice as ov

        client = _client()
        client.registered = True
        asyncio.run(client.register())

        daemon = ov.OpenClawVoice.__new__(ov.OpenClawVoice)
        daemon.sip_client = client
        daemon.call_manager = ov.CallManager(max_concurrent=3)
        daemon._start_time = 0
        daemon.instance = 'test'
        daemon.config = {'sip': {}}
        daemon.api_port = 8079

        class _Req:
            match_info = {}

        response = asyncio.run(daemon._api_health(_Req()))
        import json
        payload = json.loads(response.text)

        self.assertFalse(
            payload['registered'],
            "The health endpoint must not report registered=true after a failed "
            "registration",
        )
        self.assertNotIn(
            'registered_age_s', payload,
            "No registration age should be reported when registration failed",
        )


class ReRegisterLoopTests(unittest.TestCase):
    """The re-registration loop must survive a failed attempt."""

    def setUp(self):
        # The loop backs off for SIP_RE_REGISTER_RETRY_SECONDS after a failure
        # and for the server-granted interval after a success. Both are patched
        # to zero here so the test can observe several iterations quickly.
        self._orig_retry = sip_client.SIP_RE_REGISTER_RETRY_SECONDS
        sip_client.SIP_RE_REGISTER_RETRY_SECONDS = 0
        self.addCleanup(
            lambda: setattr(sip_client, 'SIP_RE_REGISTER_RETRY_SECONDS', self._orig_retry)
        )

    def test_loop_keeps_running_after_a_failed_registration(self):
        """
        `register()` clears `self.registered` when an attempt fails. If the
        loop were still keyed on that flag it would exit on the next iteration
        and the extension would never re-register again — a permanent outage.
        """
        client = _client()
        client.running = True
        client.registered = True
        client._re_register_enabled = True
        client._normal_re_register_interval = 0

        attempts = []

        async def fake_register():
            attempts.append(1)
            # Emulate the real failure path: flag cleared, no success.
            client.registered = False
            client.registered_at = None
            return False

        client.register = fake_register

        async def drive():
            task = asyncio.ensure_future(client._re_register_loop(0))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(drive())

        self.assertGreaterEqual(
            len(attempts), 2,
            "The re-registration loop must keep retrying after a failed attempt",
        )

    def test_loop_is_keyed_on_its_own_flag_not_registered(self):
        """
        The loop condition must not depend on `registered`, which register()
        clears as part of a normal (even successful) call sequence.
        """
        client = _client()
        client.running = True
        client.registered = False
        client._re_register_enabled = True
        client._normal_re_register_interval = 0

        attempts = []

        async def fake_register():
            attempts.append(1)
            client.registered = True
            return True

        client.register = fake_register

        async def drive():
            task = asyncio.ensure_future(client._re_register_loop(0))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(drive())

        self.assertGreaterEqual(
            len(attempts), 2,
            "The loop must continue while `_re_register_enabled` is set, even "
            "when `registered` is momentarily false",
        )

    def test_loop_stops_when_re_register_is_disabled(self):
        """stop() must end the loop rather than leaving it spinning."""
        client = _client()
        client.running = True
        client.registered = True
        client._re_register_enabled = True
        client._normal_re_register_interval = 0

        attempts = []

        async def fake_register():
            attempts.append(1)
            client._re_register_enabled = False
            return True

        client.register = fake_register

        async def drive():
            task = asyncio.ensure_future(client._re_register_loop(0))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(drive())

        self.assertEqual(
            len(attempts), 1,
            "The loop must exit once `_re_register_enabled` is cleared",
        )


if __name__ == '__main__':
    unittest.main(verbosity=2)
