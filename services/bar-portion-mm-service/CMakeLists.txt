cmake_minimum_required(VERSION 3.25)
project(bar_portion_mm VERSION 0.1.0 LANGUAGES CXX)

option(BPMM_ENABLE_SANITIZERS "Enable ASan and UBSan" OFF)
option(BPMM_BUILD_TESTS "Build tests" ON)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(CURL REQUIRED)
find_package(OpenSSL REQUIRED)
find_package(Threads REQUIRED)

add_library(bpmm_core
  src/book.cpp
  src/strategy.cpp
  src/risk.cpp
  src/shadow.cpp
  src/parser.cpp
  src/journal.cpp
  src/engine.cpp
  src/binance.cpp
  src/status.cpp
  src/dashboard.cpp
  src/allocation.cpp
  src/config.cpp
)
target_include_directories(bpmm_core PUBLIC include)
target_link_libraries(bpmm_core PUBLIC CURL::libcurl OpenSSL::SSL OpenSSL::Crypto Threads::Threads)
target_compile_options(bpmm_core PRIVATE -Wall -Wextra -Wconversion -Wshadow -Werror)

if(BPMM_ENABLE_SANITIZERS)
  target_compile_options(bpmm_core PUBLIC -fsanitize=address,undefined -fno-omit-frame-pointer)
  target_link_options(bpmm_core PUBLIC -fsanitize=address,undefined)
endif()

add_executable(bar-mm src/main.cpp)
target_link_libraries(bar-mm PRIVATE bpmm_core)
target_compile_options(bar-mm PRIVATE -Wall -Wextra -Wconversion -Wshadow -Werror)

if(BPMM_BUILD_TESTS)
  enable_testing()
  add_executable(bpmm-tests tests/test_main.cpp)
  target_link_libraries(bpmm-tests PRIVATE bpmm_core)
  target_compile_options(bpmm-tests PRIVATE -Wall -Wextra -Wconversion -Wshadow -Werror)
  add_test(NAME bpmm-tests COMMAND bpmm-tests)
  add_test(NAME bpmm-drill COMMAND bar-mm drill)
  add_test(NAME bpmm-benchmark COMMAND bar-mm benchmark)
  add_test(NAME bpmm-replay COMMAND bar-mm replay ${CMAKE_CURRENT_SOURCE_DIR}/tests/fixtures/session.jsonl)
  add_test(NAME bpmm-gapcheck COMMAND bar-mm gapcheck ${CMAKE_CURRENT_SOURCE_DIR}/tests/fixtures/session.jsonl)
  add_test(NAME bpmm-calibrate COMMAND bar-mm calibrate ${CMAKE_CURRENT_SOURCE_DIR}/tests/fixtures/calibration.csv --train 3 --evaluate 2)
  add_test(NAME bpmm-live-refusal COMMAND bar-mm shadow --mode live)
  set_tests_properties(bpmm-live-refusal PROPERTIES WILL_FAIL TRUE)
endif()

install(TARGETS bar-mm RUNTIME DESTINATION bin)
