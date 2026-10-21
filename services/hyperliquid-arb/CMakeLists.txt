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

# Windows builds use MSYS2 CLANG64 (clang + libc++), which keeps __int128; MSVC does not have it.
if(WIN32 AND NOT CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  message(FATAL_ERROR "Windows builds need clang (MSYS2 CLANG64); MSVC lacks __int128")
endif()

set(HLA_WARNINGS -Wall -Wextra -Wconversion -Wshadow -Werror)

add_library(hla_core
  src/hl_feed.cpp
  src/hl_instruments.cpp
  src/hl_parser.cpp
  src/net.cpp
  src/platform.cpp
)
target_include_directories(hla_core PUBLIC include)
target_link_libraries(hla_core PUBLIC CURL::libcurl OpenSSL::SSL OpenSSL::Crypto Threads::Threads)
if(WIN32)
  target_link_libraries(hla_core PUBLIC ws2_32 winmm)
endif()
target_compile_options(hla_core PRIVATE ${HLA_WARNINGS})

if(HLA_ENABLE_SANITIZERS)
  target_compile_options(hla_core PUBLIC -fsanitize=address,undefined -fno-omit-frame-pointer)
  target_link_options(hla_core PUBLIC -fsanitize=address,undefined)
endif()

