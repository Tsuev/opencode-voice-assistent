"""Discover native build tools that are installed outside ``PATH``.

Users regularly install CMake, CUDA or Visual Studio Build Tools and then run
the installer from a shell that was opened earlier.  ``shutil.which`` only
looks at the process ``PATH`` and reports a false "not installed" error.  This
module searches the standard per-platform install locations and, on Windows,
the Visual Studio installation discovered through ``vswhere``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def _program_files() -> list[Path]:
    values = [
        os.environ.get("PROGRAMFILES"),
        os.environ.get("PROGRAMFILES(X86)"),
    ]
    return [Path(value) for value in values if value]


def _first_existing(paths: Iterable[Path]) -> Path | None:
    seen: set[str] = set()
    for path in paths:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if path.exists():
            return path
    return None


def _which(name: str) -> Path | None:
    found = shutil.which(name)
    return Path(found) if found else None


def _version(path: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            [str(path), *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (result.stdout or result.stderr or "").strip()
    return text.splitlines()[0] if text else None


def _vswhere() -> Path | None:
    for base in _program_files():
        candidate = base / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
        if candidate.exists():
            return candidate
    return None


def _vs_installations() -> list[Path]:
    vswhere = _vswhere()
    if vswhere is None:
        return []
    base_cmd = [
        str(vswhere),
        "-products",
        "*",
        "-property",
        "installationPath",
        "-format",
        "value",
    ]
    with_cpp = base_cmd[:1] + [
        "-products",
        "*",
        "-requires",
        "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
        "-property",
        "installationPath",
        "-format",
        "value",
    ]
    for command in (with_cpp, base_cmd):
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        paths = [Path(line.strip()) for line in result.stdout.splitlines() if line.strip()]
        if paths:
            return paths
    return []


def _find_in_versions(root: Path, pattern: str) -> list[Path]:
    if not root.exists():
        return []
    return sorted(root.glob(pattern), reverse=True)


@dataclass(slots=True)
class Toolchain:
    """Paths to the native tools available on this machine."""

    cmake: Path | None = None
    cmake_version: str | None = None
    nvcc: Path | None = None
    cuda_root: Path | None = None
    cl: Path | None = None
    vcvars: Path | None = None
    vs_install: Path | None = None
    ninja: Path | None = None
    gpp: Path | None = None
    gcc: Path | None = None
    make: Path | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def has_cuda(self) -> bool:
        return self.nvcc is not None

    @property
    def has_compiler(self) -> bool:
        return self.cl is not None or self.gpp is not None

    def environment(self, *, include_msvc: bool = False) -> dict[str, str]:
        """Return an environment with discovered tool directories on ``PATH``."""
        env = dict(os.environ)
        directories = [
            path.parent
            for path in (
                self.cmake,
                self.nvcc,
                self.cl,
                self.ninja,
                self.gcc,
                self.gpp,
                self.make,
            )
            if path is not None
        ]
        if directories:
            prefix = os.pathsep.join(str(path) for path in directories)
            current = env.get("PATH", "")
            env["PATH"] = f"{prefix}{os.pathsep}{current}" if current else prefix
        if self.cuda_root is not None:
            env["CUDA_PATH"] = str(self.cuda_root)
            env["CUDAToolkit_ROOT"] = str(self.cuda_root)
        if include_msvc and self.vcvars is not None and sys.platform == "win32":
            env.update(_msvc_environment(self.vcvars, base=env))
        return env

    def summary(self) -> list[str]:
        lines = [
            f"cmake: {self.cmake or 'не найден'}"
            + (f" ({self.cmake_version})" if self.cmake_version else ""),
            f"nvcc: {self.nvcc or 'не найден'}",
            f"cl: {self.cl or 'не найден'}",
            f"ninja: {self.ninja or 'не найден'}",
            f"g++: {self.gpp or 'не найден'}",
        ]
        lines.extend(self.notes)
        return lines


def find_cmake(vs_installations: Iterable[Path] = ()) -> tuple[Path | None, str | None]:
    candidates: list[Path] = []
    found = _which("cmake")
    if found:
        candidates.append(found)
    for base in _program_files():
        candidates.append(base / "CMake" / "bin" / "cmake.exe")
    local_app_data = _env_path("LOCALAPPDATA")
    if local_app_data is not None:
        candidates.append(local_app_data / "Programs" / "CMake" / "bin" / "cmake.exe")
    user_profile = _env_path("USERPROFILE")
    if user_profile is not None:
        candidates.append(user_profile / "scoop" / "shims" / "cmake.exe")
    program_data = _env_path("ProgramData")
    if program_data is not None:
        candidates.append(program_data / "chocolatey" / "bin" / "cmake.exe")
    for install in vs_installations:
        candidates.append(
            install
            / "Common7"
            / "IDE"
            / "CommonExtensions"
            / "Microsoft"
            / "CMake"
            / "CMake"
            / "bin"
            / "cmake.exe"
        )
    # The ``cmake`` PyPI package ships a binary into the active environment.
    candidates.append(Path(sys.executable).parent / "cmake.exe")
    candidates.extend(
        [
            Path("/opt/homebrew/bin/cmake"),
            Path("/usr/local/bin/cmake"),
            Path("/usr/bin/cmake"),
        ]
    )
    cmake = _first_existing(candidates)
    if cmake is None:
        return None, None
    return cmake, _version(cmake, "--version")


def find_nvcc() -> tuple[Path | None, Path | None]:
    candidates: list[Path] = []
    found = _which("nvcc")
    if found:
        candidates.append(found)
    for name in ("CUDA_PATH", "CUDA_HOME", "CUDA_ROOT"):
        root = _env_path(name)
        if root is not None:
            candidates.append(root / "bin" / "nvcc.exe")
            candidates.append(root / "bin" / "nvcc")
    for base in _program_files():
        root = base / "NVIDIA GPU Computing Toolkit" / "CUDA"
        for candidate in _find_in_versions(root, "v*/bin/nvcc.exe"):
            candidates.append(candidate)
    candidates.extend(
        [
            Path("/usr/local/cuda/bin/nvcc"),
            Path("/opt/cuda/bin/nvcc"),
        ]
    )
    nvcc = _first_existing(candidates)
    if nvcc is None:
        return None, None
    cuda_root = nvcc.parent.parent
    return nvcc, cuda_root


def find_msvc(
    vs_installations: Iterable[Path] = (),
) -> tuple[Path | None, Path | None, Path | None]:
    found = _which("cl")
    if found is not None:
        # ``cl`` is already usable, but CUDA builds still benefit from the
        # INCLUDE/LIB variables that vcvars64.bat sets.
        for install in vs_installations:
            vcvars = install / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
            if vcvars.exists():
                return found, vcvars, install
        return found, None, None
    for install in vs_installations:
        vcvars = install / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
        cl_candidates = _find_in_versions(
            install / "VC" / "Tools" / "MSVC", "*/bin/Hostx64/x64/cl.exe"
        )
        if cl_candidates:
            return cl_candidates[0], vcvars if vcvars.exists() else None, install
        if vcvars.exists():
            return None, vcvars, install
    return None, None, None


def find_ninja(vs_installations: Iterable[Path] = ()) -> Path | None:
    candidates: list[Path] = []
    found = _which("ninja")
    if found:
        candidates.append(found)
    for install in vs_installations:
        candidates.append(
            install
            / "Common7"
            / "IDE"
            / "CommonExtensions"
            / "Microsoft"
            / "CMake"
            / "Ninja"
            / "ninja.exe"
        )
    return _first_existing(candidates)


def _msvc_environment(vcvars: Path, *, base: dict[str, str] | None = None) -> dict[str, str]:
    """Read the environment produced by ``vcvars64.bat``.

    ``cmd.exe /c`` mangles nested quotes in an argument list, so the reliable
    way is to write a tiny temporary batch file and execute it.
    """
    if sys.platform != "win32":
        return {}
    fd, name = tempfile.mkstemp(suffix=".bat")
    os.close(fd)
    batch = Path(name)
    try:
        batch.write_text(
            "@echo off\r\n"
            f'call "{vcvars}" >nul\r\n'
            "set\r\n",
            encoding="utf-8",
        )
        try:
            result = subprocess.run(
                [str(batch)],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=30,
                env=base,
            )
        except (OSError, subprocess.SubprocessError):
            return {}
    finally:
        batch.unlink(missing_ok=True)
    if result.returncode != 0:
        return {}
    env: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key and not key.startswith("="):
            env[key] = value
    return env


def detect_toolchain() -> Toolchain:
    """Detect CMake, CUDA, MSVC, MinGW and Ninja without relying on ``PATH``."""
    vs_installations = _vs_installations()
    cmake, cmake_version = find_cmake(vs_installations)
    nvcc, cuda_root = find_nvcc()
    cl, vcvars, vs_install = find_msvc(vs_installations)
    ninja = find_ninja(vs_installations)
    gpp = _which("g++")
    gcc = _which("gcc")
    make = _which("mingw32-make") or _which("make")

    notes: list[str] = []
    if cmake is not None and cmake_version is not None:
        try:
            version = tuple(int(part) for part in cmake_version.split()[2].split(".")[:2])
        except (IndexError, ValueError):
            version = ()
        if version and version < (3, 14):
            notes.append(
                f"CMake {cmake_version} старее 3.14; установите более новую версию."
            )
    return Toolchain(
        cmake=cmake,
        cmake_version=cmake_version,
        nvcc=nvcc,
        cuda_root=cuda_root,
        cl=cl,
        vcvars=vcvars,
        vs_install=vs_install,
        ninja=ninja,
        gpp=gpp,
        gcc=gcc,
        make=make,
        notes=notes,
    )
