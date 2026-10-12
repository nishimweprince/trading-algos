cmake_minimum_required(VERSION 3.22)
project(pump_desk CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

# Release: -O3 -march=native -flto (the binary is built on the box that runs it).
set(CMAKE_CXX_FLAGS_RELEASE "-O3 -march=native -DNDEBUG")
include(CheckIPOSupported)
check_ipo_supported(RESULT PD_LTO OUTPUT PD_LTO_MSG)
add_compile_options(-Wall -Wextra -fno-plt)

find_package(PkgConfig REQUIRED)
find_package(Threads REQUIRED)
find_package(OpenSSL REQUIRED)
find_package(SQLite3 REQUIRED)
pkg_check_modules(SODIUM REQUIRED IMPORTED_TARGET libsodium)
pkg_check_modules(GRPCPP REQUIRED IMPORTED_TARGET grpc++)
find_package(simdjson CONFIG QUIET)
if(NOT simdjson_FOUND)
  pkg_check_modules(SIMDJSON REQUIRED IMPORTED_TARGET simdjson)
  add_library(simdjson::simdjson ALIAS PkgConfig::SIMDJSON)
endif()

# Everything but the gRPC ingest and main: tested on its own.
add_library(pdcore STATIC
  src/core/base58.cpp
  src/core/config.cpp
  src/sol/amm.cpp
  src/sol/constants.cpp
  src/sol/crypto.cpp
  src/sol/swap.cpp
  src/sol/tx.cpp
  src/stream/request.cpp
  src/stream/update.cpp
  src/logic/engine.cpp
  src/logic/exit_fsm.cpp
  src/logic/txparse.cpp
  src/net/conn.cpp
  src/aux/aux.cpp
  src/aux/rpc.cpp
  src/aux/writer.cpp
  src/exec/exec.cpp)
target_include_directories(pdcore PUBLIC src)
target_link_libraries(pdcore PUBLIC PkgConfig::SODIUM simdjson::simdjson OpenSSL::SSL OpenSSL::Crypto SQLite::SQLite3 Threads::Threads)

add_executable(pump-desk src/main.cpp src/stream/ingest.cpp)
target_link_libraries(pump-desk PRIVATE pdcore PkgConfig::GRPCPP)

add_executable(pump-desk-report src/tools/report.cpp)
target_link_libraries(pump-desk-report PRIVATE SQLite::SQLite3)

if(PD_LTO)
  set_property(TARGET pdcore pump-desk pump-desk-report PROPERTY INTERPROCEDURAL_OPTIMIZATION TRUE)
endif()

enable_testing()
file(GLOB PD_TESTS tests/*.cpp)
add_executable(pd_tests ${PD_TESTS})
target_include_directories(pd_tests PRIVATE tests)
target_compile_definitions(pd_tests PRIVATE PD_FIXTURE_DIR="${CMAKE_CURRENT_SOURCE_DIR}/tests/fixtures")
target_link_libraries(pd_tests PRIVATE pdcore)
add_test(NAME pd_tests COMMAND pd_tests)
