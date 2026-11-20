cmake_minimum_required(VERSION 3.16)
set(IDF_TARGET "esp32c3" CACHE STRING "AI Passport target")
set(EXTRA_COMPONENT_DIRS "../../components")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
idf_build_set_property(MINIMAL_BUILD ON)
project(pa_terminal)
if(NOT "$ENV{IDF_VERSION}" VERSION_EQUAL "5.5.3")
    message(FATAL_ERROR "Use ESP-IDF 5.5.3")
endif()

idf_build_get_property(pp_python PYTHON)
add_custom_target(check_cache_layout ALL
    COMMAND ${pp_python} "${CMAKE_CURRENT_SOURCE_DIR}/../../tools/package_firmware.py"
            --check-terminal-layout "${CMAKE_BINARY_DIR}/partition_table/partition-table.bin"
            "${CMAKE_BINARY_DIR}/pa_terminal.bin"
    DEPENDS gen_project_binary partition_table_bin
    VERBATIM)
