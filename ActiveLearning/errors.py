"""Cross-layer Active Learning exceptions with no runtime dependencies."""


class ReferenceStimulusUnavailableError(RuntimeError):
    """Reference measurement exists but no safe external excitation is resolved."""
