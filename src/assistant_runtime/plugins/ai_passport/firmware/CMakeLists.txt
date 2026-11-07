cmake_minimum_required(VERSION 3.16)

set(IDF_TARGET "esp32c3" CACHE STRING "FoloToy AI Passport target")
if(NOT IDF_TARGET STREQUAL "esp32c3")
    message(FATAL_ERROR "AI Passport firmware requires ESP32-C3")
endif()

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
idf_build_set_property(MINIMAL_BUILD ON)
project(ppdemo)

if(NOT "$ENV{IDF_VERSION}" VERSION_EQUAL "5.5.3")
    message(FATAL_ERROR "Use the pinned ESP-IDF 5.5.3 toolchain")
endif()
