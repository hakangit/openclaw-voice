"""Unit tests for the in-dialog BYE construction.

These cover the bug where every inbound BYE was rejected by Asterisk with
'481 Call/Transaction Does Not Exist', leaving the call up on the PBX while
the daemon believed it had ended.
"""
import re
import sys
import unittest

sys.path.insert(0, '/root/.openclaw/workspace/openclaw-voice')

from sip_client import SIPClient


def make_client():
    cfg = {
        'sip_server': 'sip.customer1.example',
        'sip_port': 5060,
        'extension': '100',
        'password': 'x',
        'display_name': 'OpenClaw',
        'sip_local_port': 5060,
        'external_ip': '203.0.113.10',
    }
    c = SIPClient.__new__(SIPClient)
    # Minimal state needed by hangup(); avoid opening sockets.
    c.server = cfg['sip_server']
    c.port = 5060
    c.extension = '100'
    c.display_name = 'OpenClaw'
    c.local_ip = '203.0.113.10'
    c.local_port = 5060
    c.external_ip = '203.0.113.10'
    c.contact_port = 5060
    c.cseq = 1
    c.from_tag = None
    c.to_tag = None
    c.active_call = None
    c.rtp_session = None
    c._active_calls = {}
    c._response_queues = {}
    c.sent = []
    c._transport = None
    return c


class TestExtractHelpers(unittest.TestCase):
    def setUp(self):
        self.c = make_client()

    def test_extract_cseq_number(self):
        self.assertEqual(self.c._extract_cseq_number('2008 INVITE'), 2008)
        self.assertEqual(self.c._extract_cseq_number('3 BYE'), 3)
        self.assertEqual(self.c._extract_cseq_number(''), 0)
        self.assertEqual(self.c._extract_cseq_number('garbage'), 0)

    def test_extract_uri(self):
        self.assertEqual(
            self.c._extract_uri('"Anonymous" <sip:anonymous@anonymous.invalid>;tag=abc'),
            'sip:anonymous@anonymous.invalid')
        self.assertEqual(
            self.c._extract_uri('<sip:asterisk@198.51.100.20:5060>'),
            'sip:asterisk@198.51.100.20:5060')
        self.assertEqual(self.c._extract_uri('sip:100@sip.customer1.example'),
                         'sip:100@sip.customer1.example')
        self.assertEqual(self.c._extract_uri(''), '')


class TestInboundBye(unittest.TestCase):
    """The BYE we send for an inbound call must be a valid in-dialog request."""

    def setUp(self):
        self.c = make_client()

    def test_inbound_state_produces_valid_bye(self):
        # Mirrors what _handle_invite now stores.
        self.c._active_calls['cid-1'] = {
            'call_id': 'cid-1',
            'direction': 'inbound',
            'remote_number': '9001',
            'rtp_session': object(),
            'codec': 0,
            'from_tag': 'callertag',
            'to_tag': 'ourtag',
            'from_uri': 'sip:100@sip.customer1.example',
            'to_uri': 'sip:100@sip.customer1.example',
            'remote_target': 'sip:asterisk@198.51.100.20:5060',
            'bye_from_header': '<sip:100@203.0.113.10:5060>;tag=ourtag',
            'bye_to_header': '"Anonymous" <sip:anonymous@anonymous.invalid>;tag=callertag',
            'invite_cseq': 2008,
            'local_cseq': 2009,
        }

        sent = {}

        def fake_send(data, addr):
            sent['req'] = data.decode()
            sent['addr'] = addr

        self.c._send = fake_send
        self.c._recv_filtered = None  # not awaited; replaced below

        import asyncio

        async def fake_recv(cid, timeout):
            return ('SIP/2.0 200 OK\r\nCall-ID: cid-1\r\n\r\n', ('198.51.100.20', 5060))

        self.c._recv_filtered = fake_recv

        closed = []

        class FakeRtp:
            def close(self):
                closed.append(True)

        rtp = FakeRtp()
        self.c._active_calls['cid-1']['rtp_session'] = rtp

        asyncio.run(self.c.hangup(call_id='cid-1'))

        req = sent['req']
        # 1. Request-URI must target the REMOTE party, not us.
        self.assertTrue(
            req.startswith('BYE sip:asterisk@198.51.100.20:5060 SIP/2.0'),
            f'Request-URI points at the wrong party:\n{req}')
        self.assertNotIn('BYE sip:100@sip.customer1.example SIP/2.0', req)

        # 2. From/To must carry the dialog tags, swapped vs the INVITE.
        self.assertRegex(req, r'From: <sip:100@203\.0\.113\.10:5060>;tag=ourtag')
        self.assertRegex(req, r'To: "Anonymous" <sip:anonymous@anonymous\.invalid>;tag=callertag')

        # 3. CSeq must be higher than the INVITE's.
        cseq = int(re.search(r'CSeq: (\d+) BYE', req).group(1))
        self.assertGreater(cseq, 2008)

        # 4. RTP closes only AFTER the BYE is acknowledged.
        self.assertEqual(len(closed), 1)

    def test_rejected_bye_is_reported(self):
        """A 481 must not be reported as success."""
        self.c._active_calls['cid-2'] = {
            'call_id': 'cid-2', 'direction': 'inbound', 'remote_number': '9001',
            'rtp_session': None, 'codec': 0, 'from_tag': 'a', 'to_tag': 'b',
            'from_uri': 'sip:100@sip.customer1.example', 'to_uri': 'sip:100@sip.customer1.example',
            'remote_target': 'sip:asterisk@198.51.100.20:5060',
            'bye_from_header': '<sip:100@203.0.113.10:5060>;tag=b',
            'bye_to_header': '<sip:anon@x>;tag=a',
            'invite_cseq': 10, 'local_cseq': 11,
        }
        self.c._send = lambda data, addr: None

        import asyncio

        async def fake_recv(cid, timeout):
            return ('SIP/2.0 481 Call/Transaction Does Not Exist\r\n\r\n', ('1.2.3.4', 5060))

        self.c._recv_filtered = fake_recv
        ok = asyncio.run(self.c.hangup(call_id='cid-2'))
        self.assertFalse(ok, 'A rejected BYE must not report success')


class TestOutboundBye(unittest.TestCase):
    def setUp(self):
        self.c = make_client()

    def test_outbound_bye_targets_callee(self):
        self.c._active_calls['cid-3'] = {
            'call_id': 'cid-3', 'direction': 'outbound', 'remote_number': '12025550199',
            'rtp_session': None, 'codec': 0, 'from_tag': 'ft', 'to_tag': 'tt',
            'from_uri': 'sip:100@sip.customer1.example', 'to_uri': 'sip:12025550199@sip.customer1.example',
            'remote_target': 'sip:12025550199@sip.customer1.example',
            'bye_from_header': '"OpenClaw" <sip:100@sip.customer1.example>;tag=ft',
            'bye_to_header': '<sip:12025550199@sip.customer1.example>;tag=tt',
            'invite_cseq': 1, 'local_cseq': 2,
        }
        sent = {}
        self.c._send = lambda data, addr: sent.setdefault('req', data.decode())

        import asyncio

        async def fake_recv(cid, timeout):
            return ('SIP/2.0 200 OK\r\n\r\n', ('1.2.3.4', 5060))

        self.c._recv_filtered = fake_recv
        ok = asyncio.run(self.c.hangup(call_id='cid-3'))
        self.assertTrue(ok)
        self.assertTrue(sent['req'].startswith(
            'BYE sip:12025550199@sip.customer1.example SIP/2.0'), sent['req'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
