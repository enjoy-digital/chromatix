#
# This file is part of ChromatiX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

cmake_minimum_required(VERSION 3.10)
project(SoapyChromatic CXX)

find_package(SoapySDR "0.8" CONFIG REQUIRED)
find_package(Threads REQUIRED)

SOAPY_SDR_MODULE_UTIL(
    TARGET    ChromaticSupport
    SOURCES   SoapyChromatic.cpp
    LIBRARIES Threads::Threads
)
