#!/usr/bin/env python3
"""
Audio Utilities Module
======================

Provides VAD (Voice Activity Detection) and echo suppression for speakerphone use.

NOTE: Audio conversion (resampling) is NO LONGER NEEDED because xAI Grok Voice API
natively supports G.711 u-law at 8kHz - the exact format SIP phones use!

USAGE:
    from audio import VoiceActivityDetector, EchoSuppressor

    vad = VoiceActivityDetector()
    echo = EchoSuppressor()

    # Check if audio contains speech
    if vad.is_speech(ulaw_bytes):
        # Process voice

    # Echo suppression for speakerphone
    echo.set_playing(True)   # AI is speaking
    if echo.should_send():   # Check if we should send mic audio
        send_to_xai(audio)

DEPENDENCIES:
    pip install webrtcvad
"""

import audioop
import time

import webrtcvad

# Audio constants
SIP_SAMPLE_RATE = 8000   # G.711 u-law uses 8kHz
RTP_FRAME_BYTES = 160    # 160 bytes u-law = 20ms @ 8kHz
RTP_FRAME_MS = 20        # 20ms per RTP packet


def split_into_rtp_frames(ulaw_bytes: bytes) -> list[bytes]:
    """
    Split u-law audio into RTP-sized frames (160 bytes each).

    Args:
        ulaw_bytes: u-law audio data

    Returns:
        List of 160-byte frames (drops incomplete final frame)
    """
    frames = []
    for i in range(0, len(ulaw_bytes) - RTP_FRAME_BYTES + 1, RTP_FRAME_BYTES):
        frames.append(ulaw_bytes[i:i + RTP_FRAME_BYTES])
    return frames


class VoiceActivityDetector:
    """
    Detects speech in audio using WebRTC VAD.

    WebRTC VAD is lightweight and works well for real-time applications.
    It requires specific frame sizes (10, 20, or 30ms) at supported sample rates.

    For 8kHz audio:
    - 10ms = 80 samples = 160 bytes (PCM) or 80 bytes (u-law)
    - 20ms = 160 samples = 320 bytes (PCM) or 160 bytes (u-law)  <-- RTP frame size
    - 30ms = 240 samples = 480 bytes (PCM) or 240 bytes (u-law)
    """

    def __init__(self, aggressiveness: int = 2):
        """
        Initialize VAD.

        Args:
            aggressiveness: How aggressive to filter non-speech (0-3).
                0 = least aggressive (more false positives, fewer missed speech)
                3 = most aggressive (fewer false positives, may miss quiet speech)
                2 = good balance for speakerphone
        """
        self.vad = webrtcvad.Vad(aggressiveness)
        self.sample_rate = SIP_SAMPLE_RATE

    def is_speech(self, ulaw_bytes: bytes) -> bool:
        """
        Check if u-law audio frame contains speech.

        Args:
            ulaw_bytes: G.711 u-law audio (must be 10, 20, or 30ms at 8kHz)
                       160 bytes = 20ms (standard RTP frame)

        Returns:
            True if speech detected, False otherwise
        """
        # Convert u-law to PCM for VAD
        pcm = audioop.ulaw2lin(ulaw_bytes, 2)

        try:
            return self.vad.is_speech(pcm, self.sample_rate)
        except Exception:
            return False

    def is_speech_pcm(self, pcm_bytes: bytes, sample_rate: int = 8000) -> bool:
        """
        Check if PCM audio frame contains speech.

        Args:
            pcm_bytes: 16-bit PCM audio (must be 10, 20, or 30ms)
            sample_rate: Sample rate (8000, 16000, 32000, or 48000)

        Returns:
            True if speech detected
        """
        try:
            return self.vad.is_speech(pcm_bytes, sample_rate)
        except Exception:
            return False


class EchoSuppressor:
    """
    Simple echo suppression for speakerphone use.

    When the AI is speaking (playing audio), we suppress sending microphone
    audio to prevent the AI from hearing itself. This uses a "hold time"
    to account for acoustic echo that continues briefly after playback stops.
    """

    def __init__(self, hold_time_ms: int = 300):
        """
        Initialize echo suppressor.

        Args:
            hold_time_ms: How long to suppress after playback stops (ms).
                         300ms works well for most rooms.
        """
        self.hold_time = hold_time_ms / 1000.0
        self.is_playing = False
        self.last_play_time = 0.0

    def set_playing(self, playing: bool):
        """
        Update playback state.

        Call this when AI audio starts/stops playing.
        """
        self.is_playing = playing
        if playing:
            self.last_play_time = time.monotonic()

    def output_audio(self):
        """Call this each time you send audio to the speaker."""
        self.last_play_time = time.monotonic()
        self.is_playing = True

    def should_send(self) -> bool:
        """
        Check if we should send microphone audio.

        Returns:
            True if safe to send (no echo expected), False if suppressed
        """
        if self.is_playing:
            return False

        elapsed = time.monotonic() - self.last_play_time
        return elapsed > self.hold_time

    def get_gain(self) -> float:
        """
        Get suggested gain for microphone audio (0.0 to 1.0).

        Can be used for soft suppression instead of hard muting.
        """
        if self.is_playing:
            return 0.0

        elapsed = time.monotonic() - self.last_play_time
        if elapsed > self.hold_time:
            return 1.0

        return min(1.0, elapsed / self.hold_time)
