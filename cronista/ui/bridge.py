"""Puente entre el bus de eventos (cualquier hilo) y Qt (hilo principal)."""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from cronista.core.events import Event, EventBus


class QtBridge(QObject):
    event = Signal(object)

    def __init__(self, bus: EventBus, parent: QObject | None = None,
                 topics: tuple[str, ...] | list[str] | None = None) -> None:
        """`topics`: solo esos temas (evita reenviar los eventos frecuentes de audio a diálogos que no los usan).
        Sin `topics`, se reenvía todo."""
        super().__init__(parent)
        if topics:
            self._unsubs = [bus.subscribe(t, self._forward) for t in topics]
        else:
            self._unsubs = [bus.subscribe(EventBus.WILDCARD, self._forward)]

    def _forward(self, ev: Event) -> None:
        # Signal.emit desde otro hilo → conexión en cola hacia el hilo de la UI
        self.event.emit(ev)

    def close(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs = []
