#!/usr/bin/env python3
"""Regression tests for the production-readiness fixes.

Three defects are pinned here:

1. **Auth fail-open.** `auth.require_pin: true` combined with a PIN that did
   not resolve (missing env var, unsubstituted `${OPENCLAW_AUTH_PIN}`) used
   to grant the caller the FULL privileged toolset -- process_query (shell
   access), outbound calling, conference bridging. A misconfiguration handed
   an anonymous caller the whole host. The gate must fail CLOSED.

2. **Unbounded call history.** Ended calls were never removed from the
   manager, so a long-running daemon retained every call it ever served,
   and `duration_s` kept growing after the call ended.

3. **PIN brute force.** There was no attempt cap, so a caller could keep
   guessing a short PIN for the lifetime of one call.
"""
import asyncio
import sys
import time
import unittest

sys.path.insert(0, '/root/.openclaw/workspace/openclaw-voice')

import openclaw_voice as ov


# ---------------------------------------------------------------------------
# 1. Auth gate fails closed
# ---------------------------------------------------------------------------

class AuthGateFailsClosedTests(unittest.TestCase):
    """A broken PIN configuration must NOT unlock privileged tools."""

    def _daemon(self, config):
        d = ov.OpenClawVoice.__new__(ov.OpenClawVoice)
        d.config = config
        return d

    def _inbound_session(self, daemon, remote='9001'):
        """Exercise the production auth-gate decision, no network involved."""
        has_auth, pre_authenticated = daemon._auth_gate(remote)
        return has_auth, pre_authenticated

    def test_unsubstituted_placeholder_does_not_unlock(self):
        daemon = self._daemon({
            'auth': {'require_pin': True, 'pin': '${OPENCLAW_AUTH_PIN}', 'whitelist': []}
        })
        has_auth, pre_authenticated = self._inbound_session(daemon)
        self.assertFalse(has_auth)
        self.assertFalse(
            pre_authenticated,
            "An unsubstituted PIN placeholder must not pre-authenticate the caller",
        )

    def test_empty_pin_does_not_unlock(self):
        daemon = self._daemon({
            'auth': {'require_pin': True, 'pin': '', 'whitelist': []}
        })
        has_auth, pre_authenticated = self._inbound_session(daemon)
        self.assertFalse(has_auth)
        self.assertFalse(pre_authenticated)

    def test_working_pin_requires_authentication(self):
        daemon = self._daemon({
            'auth': {'require_pin': True, 'pin': '12345678', 'whitelist': []}
        })
        has_auth, pre_authenticated = self._inbound_session(daemon)
        self.assertTrue(has_auth, "A usable PIN must activate the gate")
        self.assertFalse(pre_authenticated, "A non-whitelisted caller must authenticate")

    def test_whitelisted_caller_is_pre_authenticated(self):
        daemon = self._daemon({
            'auth': {'require_pin': True, 'pin': '12345678', 'whitelist': ['9001']}
        })
        has_auth, pre_authenticated = self._inbound_session(daemon, remote='9001')
        self.assertTrue(has_auth)
        self.assertTrue(pre_authenticated)

    def test_require_pin_false_grants_access(self):
        """The product default (no inbound PIN) must keep working."""
        daemon = self._daemon({
            'auth': {'require_pin': False, 'pin': '${OPENCLAW_AUTH_PIN}', 'whitelist': []}
        })
        has_auth, pre_authenticated = self._inbound_session(daemon)
        self.assertFalse(has_auth)
        self.assertTrue(pre_authenticated)

    def test_authenticate_tool_refuses_when_pin_unresolved(self):
        """The authenticate tool must not auto-pass on a broken config."""
        daemon = self._daemon({
            'auth': {'require_pin': True, 'pin': '${OPENCLAW_AUTH_PIN}'}
        })
        session = ov.CallSession(
            call_id='c1', direction='inbound', remote_number='9001',
            state='connected', authenticated=False,
        )
        result = asyncio.run(daemon._handle_tool(session, 't1', 'authenticate', {'pin': '1234'}))
        self.assertFalse(session.authenticated, "Must not authenticate without a usable PIN")
        self.assertIn('unavailable', result.lower())

    def test_authenticate_tool_accepts_correct_pin(self):
        daemon = self._daemon({
            'auth': {'require_pin': True, 'pin': '12345678'}
        })
        session = ov.CallSession(
            call_id='c1', direction='inbound', remote_number='9001',
            state='connected', authenticated=False,
        )
        result = asyncio.run(daemon._handle_tool(session, 't1', 'authenticate', {'pin': '12345678'}))
        self.assertTrue(session.authenticated)
        self.assertIn('accepted', result.lower())


# ---------------------------------------------------------------------------
# 2. Call history is bounded, and ended calls report a frozen duration
# ---------------------------------------------------------------------------

class CallHistoryTests(unittest.TestCase):
    def _session(self, call_id):
        return ov.CallSession(
            call_id=call_id, direction='inbound', remote_number='9001',
            state='connected', started_at=time.time() - 10,
        )

    def test_ended_call_is_removed_beyond_the_cap(self):
        mgr = ov.CallManager(max_concurrent=3, max_history=5)
        for i in range(20):
            s = self._session(f'c{i}')
            mgr.add(s)
            mgr.remove(s.call_id)
        self.assertLessEqual(
            len(mgr.calls), 5,
            "Finished calls must be evicted once the history cap is exceeded",
        )

    def test_active_calls_are_never_evicted(self):
        mgr = ov.CallManager(max_concurrent=3, max_history=2)
        actives = [self._session(f'a{i}') for i in range(3)]
        for s in actives:
            mgr.add(s)
        for i in range(10):
            s = self._session(f'old{i}')
            mgr.add(s)
            mgr.remove(s.call_id)
        for s in actives:
            self.assertIsNotNone(mgr.get(s.call_id), "Active calls must never be evicted")
        self.assertEqual(len(mgr.active()), 3)

    def test_duration_stops_growing_after_the_call_ends(self):
        mgr = ov.CallManager(max_concurrent=3, max_history=50)
        s = self._session('c1')
        mgr.add(s)
        mgr.remove('c1')
        first = mgr.to_list()[0]['duration_s']
        time.sleep(1.1)
        second = mgr.to_list()[0]['duration_s']
        self.assertEqual(
            first, second,
            "A finished call must report a frozen duration, not one that keeps growing",
        )
        self.assertGreaterEqual(first, 9)

    def test_default_history_cap_is_bounded(self):
        mgr = ov.CallManager()
        self.assertGreater(mgr.max_history, 0)


# ---------------------------------------------------------------------------
# 3. PIN attempts are capped
# ---------------------------------------------------------------------------

class PinAttemptCapTests(unittest.TestCase):
    def _daemon(self):
        d = ov.OpenClawVoice.__new__(ov.OpenClawVoice)
        d.config = {'auth': {'require_pin': True, 'pin': '12345678'}}
        return d

    def test_wrong_pin_is_rejected(self):
        daemon = self._daemon()
        session = ov.CallSession(
            call_id='c1', direction='inbound', remote_number='9001',
            state='connected', authenticated=False,
        )
        result = asyncio.run(daemon._handle_tool(session, 't1', 'authenticate', {'pin': '00000000'}))
        self.assertFalse(session.authenticated)
        self.assertIn('wrong pin', result.lower())
        self.assertEqual(session.pin_attempts, 1)

    def test_attempts_are_capped(self):
        daemon = self._daemon()
        session = ov.CallSession(
            call_id='c1', direction='inbound', remote_number='9001',
            state='connected', authenticated=False,
        )
        for _ in range(ov.MAX_PIN_ATTEMPTS):
            asyncio.run(daemon._handle_tool(session, 't1', 'authenticate', {'pin': '00000000'}))
        self.assertEqual(session.pin_attempts, ov.MAX_PIN_ATTEMPTS)

    def test_correct_pin_after_the_cap_is_refused(self):
        """A caller must not brute-force their way in once the cap is hit."""
        daemon = self._daemon()
        session = ov.CallSession(
            call_id='c1', direction='inbound', remote_number='9001',
            state='connected', authenticated=False,
        )
        for _ in range(ov.MAX_PIN_ATTEMPTS):
            asyncio.run(daemon._handle_tool(session, 't1', 'authenticate', {'pin': '00000000'}))
        result = asyncio.run(daemon._handle_tool(session, 't1', 'authenticate', {'pin': '12345678'}))
        self.assertFalse(
            session.authenticated,
            "The correct PIN must still be refused once the attempt cap is reached",
        )
        self.assertIn('too many', result.lower())


if __name__ == '__main__':
    unittest.main(verbosity=2)
