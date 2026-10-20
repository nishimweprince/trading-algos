cmake_minimum_required(VERSION 3.25)
project(crossex_arbitrage VERSION 0.1.0 LANGUAGES CXX)

option(CXA_ENABLE_SANITIZERS "Enable ASan and UBSan" OFF)
option(CXA_BUILD_TESTS "Build tests" ON)

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

set(CXA_WARNINGS -Wall -Wextra -Wconversion -Wshadow -Werror)

add_library(cxa_core
  src/book.cpp
  src/config.cpp
  src/engine.cpp
  src/feeds.cpp
  src/instruments.cpp
  src/journal.cpp
  src/parser.cpp
  src/rest.cpp
  src/scenario.cpp
  src/status.cpp
)
target_include_directories(cxa_core PUBLIC include)
target_link_libraries(cxa_core PUBLIC CURL::libcurl OpenSSL::SSL OpenSSL::Crypto Threads::Threads)
target_compile_options(cxa_core PRIVATE ${CXA_WARNINGS})

if(CXA_ENABLE_SANITIZERS)
  target_compile_options(cxa_core PUBLIC -fsanitize=address,undefined -fno-omit-frame-pointer)
  target_link_options(cxa_core PUBLIC -fsanitize=address,undefined)
endif()

# Allocation counting replaces global operator new; it is linked into the binary only
# so the benchmark can prove the decision path does not allocate.
add_executable(crossex src/main.cpp src/allocation.cpp)
target_link_libraries(crossex PRIVATE cxa_core)
target_compile_options(crossex PRIVATE ${CXA_WARNINGS})

if(CXA_BUILD_TESTS)
  enable_testing()
  add_executable(cxa-tests tests/test_main.cpp)
  target_link_libraries(cxa-tests PRIVATE cxa_core)
  target_compile_options(cxa-tests PRIVATE ${CXA_WARNINGS})
  add_test(NAME cxa-tests COMMAND cxa-tests ${CMAKE_CURRENT_SOURCE_DIR}/tests/fixtures)
  add_test(NAME cxa-drill COMMAND crossex drill)
  if(NOT CXA_ENABLE_SANITIZERS)
    add_test(NAME cxa-benchmark COMMAND crossex benchmark)
  endif()
  add_test(NAME cxa-replay COMMAND crossex replay ${CMAKE_CURRENT_SOURCE_DIR}/tests/fixtures/session.jsonl)
  add_test(NAME cxa-gapcheck COMMAND crossex gapcheck ${CMAKE_CURRENT_SOURCE_DIR}/tests/fixtures/session.jsonl)
  add_test(NAME cxa-daily-report COMMAND crossex daily-report ${CMAKE_CURRENT_SOURCE_DIR}/tests/fixtures/session.jsonl)
  add_test(NAME cxa-census-report COMMAND crossex census-report ${CMAKE_CURRENT_SOURCE_DIR}/tests/fixtures/session.jsonl)
  add_test(NAME cxa-live-refusal COMMAND crossex shadow --mode live)
  set_tests_properties(cxa-live-refusal PROPERTIES WILL_FAIL TRUE)
  # The fixture was recorded with these settings; replay must use the same.
  set_tests_properties(cxa-replay PROPERTIES ENVIRONMENT
    "CXA_BASIS_WARMUP_MS=0;CXA_COOLDOWN_MS=0;CXA_LATENCY_MODE=fixed")
endif()

install(TARGETS crossex RUNTIME DESTINATION bin)
