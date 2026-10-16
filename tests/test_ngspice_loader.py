"""Offline platform-selection tests; no ngspice binary is loaded."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from PySpice.Spice.NgSpice.Shared import NgSpiceShared

from run.single_stage_amplifier import run_ideal_dataset_simulations as spice_runtime


class NgSpiceLoaderTests(unittest.TestCase):
    def test_repo_root_is_derived_from_module_location(self) -> None:
        self.assertEqual(spice_runtime.AG_ROOT, Path(spice_runtime.__file__).resolve().parents[2])

    def test_windows_uses_existing_bundled_dll_and_openmp_preload(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(spice_runtime.platform, "system", return_value="Windows"),
            patch.object(spice_runtime.Path, "is_file", return_value=True),
            patch.object(spice_runtime.os, "add_dll_directory", return_value="handle", create=True) as add_dir,
            patch.object(spice_runtime.ctypes, "WinDLL", return_value="openmp", create=True) as preload,
            patch.object(spice_runtime, "_DLL_HANDLE", None),
            patch.object(spice_runtime, "_PRELOADED_DLLS", []),
            patch.object(NgSpiceShared, "LIBRARY_PATH", None),
        ):
            first = spice_runtime.configure_ngspice()
            second = spice_runtime.configure_ngspice()
            self.assertEqual(NgSpiceShared.LIBRARY_PATH, first)
        self.assertEqual(first, second)
        self.assertEqual(Path(first).name, "ngspice.dll")
        add_dir.assert_called_once_with(str(Path(first).parent))
        preload.assert_called_once_with(str(Path(first).parent / "libomp140.x86_64.dll"))

    def test_linux_uses_discovered_shared_library(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(spice_runtime.platform, "system", return_value="Linux"),
            patch.object(spice_runtime.ctypes.util, "find_library", return_value="libngspice.so.0") as find,
            patch.object(spice_runtime.ctypes, "CDLL") as load,
            patch.object(NgSpiceShared, "LIBRARY_PATH", None),
        ):
            self.assertEqual(spice_runtime.configure_ngspice(), "libngspice.so.0")
            self.assertEqual(NgSpiceShared.LIBRARY_PATH, "libngspice.so.0")
        find.assert_called_once_with("ngspice")
        load.assert_called_once_with("libngspice.so.0")

    def test_explicit_absolute_path_takes_priority(self) -> None:
        library = str(Path(spice_runtime.__file__).resolve())
        with (
            patch.dict(os.environ, {"NGSPICE_LIBRARY_PATH": library}),
            patch.object(spice_runtime.platform, "system", return_value="Linux"),
            patch.object(spice_runtime.ctypes.util, "find_library") as find,
            patch.object(spice_runtime.ctypes, "CDLL") as load,
            patch.object(NgSpiceShared, "LIBRARY_PATH", None),
        ):
            self.assertEqual(spice_runtime.configure_ngspice(), library)
            self.assertEqual(NgSpiceShared.LIBRARY_PATH, library)
        find.assert_not_called()
        load.assert_called_once_with(library)

    def test_missing_library_has_clear_error(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(spice_runtime.platform, "system", return_value="Linux"),
            patch.object(spice_runtime.ctypes.util, "find_library", return_value=None),
        ):
            with self.assertRaisesRegex(FileNotFoundError, "libngspice0"):
                spice_runtime.configure_ngspice()

    def test_unloadable_library_has_clear_error(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(spice_runtime.platform, "system", return_value="Linux"),
            patch.object(spice_runtime.ctypes.util, "find_library", return_value="libngspice.so.0"),
            patch.object(spice_runtime.ctypes, "CDLL", side_effect=OSError("missing dependency")),
        ):
            with self.assertRaisesRegex(FileNotFoundError, "could not be loaded"):
                spice_runtime.configure_ngspice()

    def test_invalid_explicit_paths_fail_without_fallback(self) -> None:
        with (
            patch.dict(os.environ, {"NGSPICE_LIBRARY_PATH": "relative/libngspice.so"}),
            patch.object(spice_runtime.platform, "system", return_value="Linux"),
            patch.object(spice_runtime.ctypes.util, "find_library") as find,
        ):
            with self.assertRaisesRegex(ValueError, "absolute path"):
                spice_runtime.configure_ngspice()
        find.assert_not_called()


if __name__ == "__main__":
    unittest.main()
