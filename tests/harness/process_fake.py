"""A fake of the process-control boundary: an operating-system process a patched ``Popen`` returns."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(eq=False)  # hashed by identity, as a ``Popen`` is
class FakeProcess:
    """A ``Popen`` stand-in for a Flow child that never existed: ``wait`` ends it at once.

    It records the signals sent to it. Its PID is above the kernel's limit.
    """

    returncode: int | None = None
    signals: list[int] = field(default_factory=list)
    pid: int = 4_194_305

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, signum: int) -> None:
        self.signals.append(signum)

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode
