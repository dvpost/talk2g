"""Client transcript integrity and voice-command filtering, independent of Qt."""

from .delivery import Delivery
from .voice_command import VoiceStop


class DictatedText:
    def __init__(self):
        self._delivery = Delivery()
        self._voice_stop = VoiceStop()
        self.text = ""

    def accept(self, event: dict, *, stop_on_phrase: bool) -> tuple[str, bool]:
        delta = self._delivery.accept(event)
        return self._filter(delta, stop_on_phrase=stop_on_phrase)

    def flush(self, *, stop_on_phrase: bool, final: bool = False) -> tuple[str, bool]:
        return self._filter("", stop_on_phrase=stop_on_phrase, final=final)

    def _filter(self, delta: str, *, stop_on_phrase: bool, final: bool = False) -> tuple[str, bool]:
        clean, stop = self._voice_stop.feed(delta, enabled=stop_on_phrase, final=final)
        self.text += clean
        return clean, stop

    def reconcile(self, server_text: str) -> bool:
        """Check the complete result without replacing already confirmed text."""
        return server_text == self._delivery.text
