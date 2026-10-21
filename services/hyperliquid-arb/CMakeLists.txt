cmake_minimum_required(VERSION 3.25)
project(hyperliquid_arb VERSION 0.1.0 LANGUAGES CXX)

option(HLA_ENABLE_SANITIZERS "Enable ASan and UBSan" OFF)
option(HLA_BUILD_TESTS "Build tests" ON)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# The macOS system libcurl is built without WebSocket support; prefer Homebrew's.
if(APPLE AND EXISTS /opt/homebrew/opt/curl)
  list(PREPEND CMAKE_PREFIX_PATH /opt/homebrew/opt/curl)
endif()
find_package(CURL 7.86 REQUIRED)
find_package(OpenSSL REQUIRED)
find_package(Threads REQUIRED)

# libsecp256k1 (ECDSA with recovery for Hyperliquid signing), pinned and built from source so the
# Windows box and dev hosts sign with the same library.
include(FetchContent)
set(SECP256K1_ENABLE_MODULE_RECOVERY ON CACHE BOOL "" FORCE)
set(SECP256K1_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(SECP256K1_BUILD_EXHAUSTIVE_TESTS OFF CACHE BOOL "" FORCE)
set(SECP256K1_BUILD_BENCHMARK OFF CACHE BOOL "" FORCE)
set(SECP256K1_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(SECP256K1_BUILD_CTIME_TESTS OFF CACHE BOOL "" FORCE)
set(SECP256K1_DISABLE_SHARED ON CACHE BOOL "" FORCE)
set(SECP256K1_INSTALL OFF CACHE BOOL "" FORCE)
FetchContent_Declare(secp256k1
  URL https://github.com/bitcoin-core/secp256k1/archive/refs/tags/v0.6.0.tar.gz
  DOWNLOAD_EXTRACT_TIMESTAMP TRUE)
FetchContent_MakeAvailable(secp256k1)

# Windows builds use MSYS2 CLANG64 (clang + libc++), which keeps __int128; MSVC does not have it.
if(WIN32 AND NOT CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  message(FATAL_ERROR "Windows builds need clang (MSYS2 CLANG64); MSVC lacks __int128")
endif()

set(HLA_WARNINGS -Wall -Wextra -Wconversion -Wshadow -Werror)

add_library(hla_core
  src/bridge_shm.cpp
  src/hl_feed.cpp
  src/hl_gateway.cpp
  src/hl_info.cpp
  src/hl_instruments.cpp
  src/hl_parser.cpp
  src/hl_sign.cpp
  src/mt5_bridge.cpp
  src/net.cpp
  src/platform.cpp
)
target_include_directories(hla_core PUBLIC include)
target_link_libraries(hla_core PUBLIC CURL::libcurl OpenSSL::SSL OpenSSL::Crypto Threads::Threads secp256k1)
if(WIN32)
  target_link_libraries(hla_core PUBLIC ws2_32 winmm)
endif()
target_compile_options(hla_core PRIVATE ${HLA_WARNINGS})

if(HLA_ENABLE_SANITIZERS)
  target_compile_options(hla_core PUBLIC -fsanitize=address,undefined -fno-omit-frame-pointer)
  target_link_options(hla_core PUBLIC -fsanitize=address,undefined)
endif()

# hla_bridge.dll for MQL5/Libraries: self-contained (static runtime) so terminal64.exe loads it
# without MSYS2 on PATH. Elsewhere it is a static library the tests drive as publisher/executor.
if(WIN32)
  add_library(hla_bridge SHARED mt5/bridge/hla_bridge.cpp src/bridge_shm.cpp)
  target_link_options(hla_bridge PRIVATE -static)
  set_target_properties(hla_bridge PROPERTIES PREFIX "")
else()
  add_library(hla_bridge STATIC mt5/bridge/hla_bridge.cpp src/bridge_shm.cpp)
endif()
target_include_directories(hla_bridge PUBLIC include)
target_compile_options(hla_bridge PRIVATE ${HLA_WARNINGS})

