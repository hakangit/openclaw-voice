#!/usr/bin/env python3
"""Tests for the callback-number behaviour of the voice daemon.

The line this daemon answers on is extension 100, which has NO external DID.
So the line's own number can never be a callback number, and the agent must
never offer it as one. It also must never invent a number.

These tests pin the three safe behaviours:
  1. A configured callback number is used verbatim.
  2. With no configured number but a real caller ID, that caller ID is the
     callback number.
  3. With no configured number and no usable caller ID, the agent asks the
     caller for a number and reads it back.
"""
import unittest

import openclaw_voice as ov


class CallbackBlockTests(unittest.TestCase):
    def _voice(self, callback=None):
        v = ov.OpenClawVoice.__new__(ov.OpenClawVoice)
        v.config = {'callback': callback if callback is not None else {'name': 'Alex', 'number': ''}}
        return v

    def test_configured_number_is_used_verbatim(self):
        v = self._voice({'name': 'Alex', 'number': '+12025550142'})
        block = v._callback_block('+12025550123')
        self.assertIn('+12025550142', block)
        self.assertIn('Alex', block)

    def test_caller_id_is_used_when_no_configured_number(self):
        v = self._voice({'name': 'Alex', 'number': ''})
        block = v._callback_block('+12025550123')
        self.assertIn('+12025550123', block)
        self.assertIn('schedule_outbound_call', block)

    def test_unknown_caller_asks_instead_of_inventing(self):
        v = self._voice({'name': 'Alex', 'number': ''})
        for unknown in ('unknown', 'anonymous', '', None, 'None'):
            block = v._callback_block(unknown)
            self.assertIn('ask them for a callback number', block)
            # must never contain a number-like token
            self.assertNotIn('+1423', block)

    def test_never_offers_the_line_as_a_callback_number(self):
        v = self._voice({'name': 'Alex', 'number': ''})
        block = v._callback_block('+12025550123')
        self.assertIn('never offer the line or extension you answered on', block)

    def test_empty_callback_block_still_forbids_offering_the_line(self):
        # With no callback section at all we still must never let the agent
        # offer the line it answered on as a callback number.
        v = self._voice({})
        block = v._callback_block('+12025550123')
        self.assertIn('never offer the line or extension you answered on', block)
        self.assertIn('+12025550123', block)


class ComposeInstructionsTests(unittest.TestCase):
    def _voice(self):
        v = ov.OpenClawVoice.__new__(ov.OpenClawVoice)
        v.config = {
            'xai': {'instructions': 'You are Grok Voice.'},
            'callback': {'name': 'Alex', 'number': ''},
        }
        return v

    def test_persona_is_prepended(self):
        v = self._voice()
        out = v._compose_instructions('BASE')
        self.assertTrue(out.startswith('You are Grok Voice.'))
        self.assertIn('BASE', out)

    def test_caller_number_reaches_the_instructions(self):
        v = self._voice()
        out = v._compose_instructions('BASE', caller_number='+12025550123')
        self.assertIn('+12025550123', out)


if __name__ == '__main__':
    unittest.main(verbosity=2)
