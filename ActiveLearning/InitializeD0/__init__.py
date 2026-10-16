"""Active Learning D0 initialization APIs."""

__all__ = ["build_current_mirror_d0", "build_runtime_d0", "save_d0_dataset"]


def __getattr__(name):
    """Load D0 builders lazily so onboarding stays independent of D0 wiring."""

    if name in __all__:
        from . import d0_builder

        return getattr(d0_builder, name)
    raise AttributeError(name)
