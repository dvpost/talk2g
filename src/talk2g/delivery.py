"""Validate and assemble ordered, exactly-once transcript commits."""


class Delivery:
    """De-duplicate server events without pretending desktop insertion is transactional."""

    def __init__(self):
        self.sequence = 0
        self.text = ""

    def accept(self, event: dict) -> str:
        sequence = event["seq"]
        if type(sequence) is not int or sequence < 1:
            raise ValueError("Неверный номер фрагмента")
        if sequence <= self.sequence:
            return ""
        if sequence != self.sequence + 1:
            raise ValueError("Пропущен фрагмент. Диктовка остановлена, полученный текст сохранён")
        delta = event["delta"]
        if not isinstance(delta, str):
            raise ValueError("Неверный текст фрагмента")
        self.sequence = sequence
        self.text += delta
        return delta
