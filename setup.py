#!/usr/bin/env python3

from setuptools import setup
from setuptools import find_packages


with open("README.md", "r") as fp:
    long_description = fp.read()


setup(
    name                          = "chromatix",
    version                       = "2026.09",
    description                   = "LiteX based FPGA design for the ModRetro Chromatic handheld",
    long_description              = long_description,
    long_description_content_type = "text/markdown",
    author                        = "Florent Kermarrec",
    author_email                  = "florent@enjoy-digital.fr",
    url                           = "https://github.com/enjoy-digital/chromatix",
    download_url                  = "https://github.com/enjoy-digital/chromatix",
    license                       = "BSD",
    python_requires               = ">=3.8",
    install_requires              = [
        "litex",
        "litei2c",
        # LUNA USB 2.0 device core (Amaranth, converted to Verilog at build time).
        "amaranth==0.5.8",
        "luna-usb==0.2.3",
        "usb-protocol==0.9.2",
    ],
    packages                      = find_packages(exclude=["test*"]),
    py_modules                    = ["chromatix_platform"],
    include_package_data          = True,
    package_data                  = {"chromatix": ["data/*", "verilog/**/*"]},
    keywords                      = "HDL ASIC FPGA hardware design",
    classifiers                   = [
        "Topic :: Scientific/Engineering :: Electronic Design Automation (EDA)",
        "Environment :: Console",
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: BSD License",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
    ],
)
