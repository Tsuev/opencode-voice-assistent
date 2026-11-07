from __future__ import annotations

from pathlib import Path

from jarvis import toolchain


def test_find_cmake_uses_standard_windows_location(tmp_path: Path, monkeypatch) -> None:
    program_files = tmp_path / "ProgramFiles"
    cmake = program_files / "CMake" / "bin" / "cmake.exe"
    cmake.parent.mkdir(parents=True)
    cmake.write_text("", encoding="utf-8")

    monkeypatch.setattr(toolchain, "_which", lambda _name: None)
    monkeypatch.setattr(toolchain, "_program_files", lambda: [program_files])
    monkeypatch.setattr(toolchain, "_version", lambda *_args: "cmake version 3.31.0")

    found, version = toolchain.find_cmake()

    assert found == cmake
    assert version == "cmake version 3.31.0"


def test_find_nvcc_uses_cuda_install_root(tmp_path: Path, monkeypatch) -> None:
    program_files = tmp_path / "ProgramFiles"
    nvcc = (
        program_files
        / "NVIDIA GPU Computing Toolkit"
        / "CUDA"
        / "v12.8"
        / "bin"
        / "nvcc.exe"
    )
    nvcc.parent.mkdir(parents=True)
    nvcc.write_text("", encoding="utf-8")

    monkeypatch.setattr(toolchain, "_which", lambda _name: None)
    monkeypatch.setattr(toolchain, "_program_files", lambda: [program_files])
    monkeypatch.setattr(toolchain, "_env_path", lambda _name: None)

    found, root = toolchain.find_nvcc()

    assert found == nvcc
    assert root == nvcc.parent.parent


def test_environment_prepends_discovered_directories(tmp_path: Path) -> None:
    cmake = tmp_path / "cmake" / "bin" / "cmake.exe"
    nvcc = tmp_path / "cuda" / "bin" / "nvcc.exe"
    cmake.parent.mkdir(parents=True)
    nvcc.parent.mkdir(parents=True)
    cmake.write_text("", encoding="utf-8")
    nvcc.write_text("", encoding="utf-8")

    discovered = toolchain.Toolchain(
        cmake=cmake,
        nvcc=nvcc,
        cuda_root=nvcc.parent.parent,
    )
    env = discovered.environment()

    assert str(cmake.parent) in env["PATH"]
    assert str(nvcc.parent) in env["PATH"]
    assert env["CUDAToolkit_ROOT"] == str(nvcc.parent.parent)
