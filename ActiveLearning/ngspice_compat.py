"""Narrow PySpice shared-library compatibility helpers.

The bundled ngspice 46 DLL emits successful dynamic-gmin progress as stderr
``Note:`` messages. PySpice 1.5 marks every non-``Warning:`` stderr callback as
an error, which prevents retrieval of an otherwise valid plot. This adapter
only downgrades exact ``Note:`` diagnostics; every other upstream error path is
preserved.
"""

from __future__ import annotations

from PySpice.Spice.NgSpice.Shared import NgSpiceShared, ffi, ffi_string_utf8
from PySpice.Spice.NgSpice.Simulation import NgSpiceSharedCircuitSimulator
from PySpice.Spice.Simulation import CircuitSimulation


_KNOWN_INFORMATIONAL_NOTES = frozenset({
    "Note: Starting dynamic gmin stepping",
    "Note: Dynamic gmin stepping completed",
})


def is_ngspice_informational_note(prefix: str, content: str) -> bool:
    """Return true only for the two observed successful gmin progress records."""
    return prefix == "stderr" and content.strip() in _KNOWN_INFORMATIONAL_NOTES


class NgSpiceSharedNotesCompatible(NgSpiceShared):
    """PySpice shared adapter that does not promote stderr Notes to errors."""

    _instances = {}

    @staticmethod
    def _send_char(message_c, ngspice_id, user_data):
        self = ffi.from_handle(user_data)
        message = ffi_string_utf8(message_c)
        prefix, _, content = message.partition(" ")
        if is_ngspice_informational_note(prefix, content):
            self._stderr.append(content)
            self._logger.info(content)
            return self.send_char(message, ngspice_id)
        # Preserve PySpice's handling for warnings and all possible errors.
        return NgSpiceShared._send_char(message_c, ngspice_id, user_data)


class NgSpiceSharedSingleRenderSimulator(NgSpiceSharedCircuitSimulator):
    """Shared simulator that renders the PySpice deck exactly once per run.

    PySpice 1.5's ``CircuitSimulation.__str__`` removes ``all`` from its live
    saved-node set. The stock shared backend renders once for a debug message
    and once for execution, so ``.save all @device[...]`` loses ``all`` in the
    executed deck. This narrow implementation mirrors the backend while using
    the sole render for execution.
    """

    def _run(self, analysis_method, *args, **kwargs):
        self.reset_analysis()
        if "probes" in kwargs:
            self.save(*kwargs.pop("probes"))
        clean_kwargs = dict(kwargs)
        clean_kwargs.pop("log_desk", None)
        getattr(CircuitSimulation, analysis_method)(self, *args, **clean_kwargs)

        deck = str(self)  # Deliberately exactly once.
        self._ngspice_shared.destroy()
        self._ngspice_shared.load_circuit(deck)
        self.native_run_attempts = getattr(self, "native_run_attempts", 0) + 1
        self._ngspice_shared.run()
        self.native_run_returns = getattr(self, "native_run_returns", 0) + 1
        self.reset_analysis()
        plot_name = self._ngspice_shared.last_plot
        if plot_name == "const":
            raise NameError("Simulation failed")
        return self._ngspice_shared.plot(self, plot_name).to_analysis()
