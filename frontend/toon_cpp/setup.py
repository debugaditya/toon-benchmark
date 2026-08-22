"""
Builds toon_cpp as an installable Python extension via pybind11.

Install (compiles the C++ during pip install, standard pybind11 pattern):
    pip install ./toon_cpp

This is what requirements.txt references (as a local path dependency) so
Render's build step compiles it automatically alongside the other
requirements. If the build fails on a given platform (e.g. missing a C++
compiler), main.py catches the ImportError and falls back gracefully --
codec=cpp becomes unavailable (reported via /health) rather than crashing
the whole service.

C++17 is required (not 14) -- the encoder uses std::to_chars from
<charconv> for fast integer/float formatting.
"""
from pybind11.setup_helpers import Pybind11Extension, build_ext
from setuptools import setup

ext_modules = [
    Pybind11Extension(
        "toon_cpp",
        ["toon_cpp.cpp"],
        cxx_std=17,
    ),
]

setup(
    name="toon_cpp",
    version="0.2.0",
    description="C++ implementation of TOON encode/decode (flat + nested), "
                "verified byte-identical to the official toon-format package's output.",
    ext_modules=ext_modules,
    cmdclass={"build_ext": build_ext},
)
