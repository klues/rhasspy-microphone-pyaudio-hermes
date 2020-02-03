"""Hermes MQTT server for Rhasspy TTS using external program"""
import audioop
import io
import json
import logging
import threading
import typing
import wave

import attr
import pyaudio
from rhasspyhermes.audioserver import (
    AudioDevice,
    AudioDeviceMode,
    AudioDevices,
    AudioFrame,
    AudioGetDevices,
)
from rhasspyhermes.base import Message

_LOGGER = logging.getLogger(__name__)


class MicrophoneHermesMqtt:
    """Hermes MQTT server for Rhasspy microphone input using external program."""

    def __init__(
        self,
        client,
        sample_rate: int,
        sample_width: int,
        channels: int,
        device_index: typing.Optional[int] = None,
        chunk_size: int = 2048,
        siteId: str = "default",
    ):
        self.client = client
        self.sample_rate = sample_rate
        self.sample_width = sample_width
        self.channels = channels
        self.device_index = device_index
        self.frames_per_buffer = chunk_size // sample_width
        self.siteId = siteId

        self.audioframe_topic: str = AudioFrame.topic(siteId=self.siteId)

    # -------------------------------------------------------------------------

    def record(self):
        """Record audio from PyAudio device."""
        try:
            audio = pyaudio.PyAudio()

            # Open device
            mic = audio.open(
                input_device_index=self.device_index,
                channels=self.channels,
                format=audio.get_format_from_width(self.sample_width),
                rate=self.sample_rate,
                input=True,
            )

            assert mic is not None
            mic.start_stream()
            _LOGGER.debug("Recording audio")

            try:
                # Read frames and publish as MQTT WAV chunks
                while True:
                    chunk = mic.read(self.frames_per_buffer)
                    if chunk:
                        with io.BytesIO() as wav_buffer:
                            wav_file: wave.Wave_write = wave.open(wav_buffer, "wb")
                            with wav_file:
                                wav_file.setframerate(self.sample_rate)
                                wav_file.setsampwidth(self.sample_width)
                                wav_file.setnchannels(self.channels)
                                wav_file.writeframes(chunk)

                            # Publish to audioFrame topic
                            self.client.publish(
                                self.audioframe_topic, wav_buffer.getvalue()
                            )
            finally:
                mic.stop_stream()
                audio.terminate()

        except Exception:
            _LOGGER.exception("record")

    def handle_get_devices(
        self, get_devices: AudioGetDevices
    ) -> typing.Optional[AudioDevices]:
        """Get available microphones and optionally test them."""
        if get_devices.modes and (AudioDeviceMode.INPUT not in get_devices.modes):
            return None

        devices: typing.List[AudioDevice] = []

        try:
            audio = pyaudio.PyAudio()

            default_name = audio.get_default_input_device_info().get("name")
            for device_index in range(audio.get_device_count()):
                device_info = audio.get_device_info_by_index(device_index)
                device_name = device_info.get("name")
                if device_name == default_name:
                    device_name += "*"

                working: typing.Optional[bool] = None
                if get_devices.test:
                    working = self.get_microphone_working(
                        device_name, device_index, audio
                    )

                devices.append(
                    AudioDevice(
                        mode=AudioDeviceMode.INPUT,
                        id=str(device_index),
                        name=device_name,
                        description="",
                        working=working,
                    )
                )
        except Exception:
            _LOGGER.exception("handle_get_devices")
        finally:
            audio.terminate()

        return AudioDevices(
            devices=devices, id=get_devices.id, siteId=get_devices.siteId
        )

    def get_microphone_working(
        self,
        device_name: str,
        device_index: int,
        audio: pyaudio.PyAudio,
        chunk_size: int = 1024,
    ) -> bool:
        """Record some audio from a microphone and check its energy."""
        try:
            # read audio
            pyaudio_stream = audio.open(
                input_device_index=device_index,
                channels=self.channels,
                format=audio.get_format_from_width(self.sample_width),
                rate=self.sample_rate,
                input=True,
            )

            try:
                buffer = pyaudio_stream.read(chunk_size)
                if not pyaudio_stream.is_stopped():
                    pyaudio_stream.stop_stream()
            finally:
                pyaudio_stream.close()

            # compute RMS of debiased audio
            # Thanks to the speech_recognition library!
            # https://github.com/Uberi/speech_recognition/blob/master/speech_recognition/__init__.py
            energy = -audioop.rms(buffer, 2)
            energy_bytes = bytes([energy & 0xFF, (energy >> 8) & 0xFF])
            debiased_energy = audioop.rms(
                audioop.add(buffer, energy_bytes * (len(buffer) // 2), 2), 2
            )

            # probably actually audio
            return debiased_energy > 30
        except Exception:
            _LOGGER.exception("get_microphone_working ({device_name})")
            pass

        return False

    # -------------------------------------------------------------------------

    def on_connect(self, client, userdata, flags, rc):
        """Connected to MQTT broker."""
        try:
            topics = [AudioGetDevices.topic()]

            for topic in topics:
                self.client.subscribe(topic)
                _LOGGER.debug("Subscribed to %s", topic)

            threading.Thread(target=self.record, daemon=True).start()
        except Exception:
            _LOGGER.exception("on_connect")

    def on_message(self, client, userdata, msg):
        """Received message from MQTT broker."""
        try:
            _LOGGER.debug("Received %s byte(s) on %s", len(msg.payload), msg.topic)

            if msg.topic == AudioGetDevices.topic():
                json_payload = json.loads(msg.payload)
                if self._check_siteId(json_payload):
                    result = self.handle_get_devices(
                        AudioGetDevices.from_dict(json_payload)
                    )
                    if result:
                        self.publish(result)
        except Exception:
            _LOGGER.exception("on_message")

    def publish(self, message: Message, **topic_args):
        """Publish a Hermes message to MQTT."""
        try:
            assert self.client
            topic = message.topic(**topic_args)

            _LOGGER.debug("-> %s", message)
            payload: typing.Union[str, bytes] = json.dumps(attr.asdict(message))

            _LOGGER.debug("Publishing %s char(s) to %s", len(payload), topic)
            self.client.publish(topic, payload)
        except Exception:
            _LOGGER.exception("on_message")

    def _check_siteId(self, json_payload: typing.Dict[str, typing.Any]) -> bool:
        return json_payload.get("siteId", "default") == self.siteId
