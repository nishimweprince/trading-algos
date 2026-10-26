cmake_minimum_required(VERSION 3.25)
project(pk_maker_cpp VERSION 0.1.0 LANGUAGES CXX)

option(PKM_ENABLE_SANITIZERS "Enable ASan and UBSan" OFF)
option(PKM_BUILD_TESTS "Build tests" ON)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(OpenSSL 3.0 REQUIRED)
find_package(Threads REQUIRED)

# libsecp256k1 with the recovery module: the system package when there is one
# (Ubuntu's libsecp256k1-dev), else pinned and built from source as hyperliquid-arb does.
find_path(SECP256K1_INCLUDE_DIR secp256k1_recovery.h)
find_library(SECP256K1_LIBRARY secp256k1)
if(SECP256K1_INCLUDE_DIR AND SECP256K1_LIBRARY)
  add_library(secp256k1 UNKNOWN IMPORTED)
  set_target_properties(secp256k1 PROPERTIES
    IMPORTED_LOCATION ${SECP256K1_LIBRARY}
    INTERFACE_INCLUDE_DIRECTORIES ${SECP256K1_INCLUDE_DIR})
else()
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
endif()

set(PKM_WARNINGS -Wall -Wextra -Wconversion -Wshadow -Werror)

add_library(pkm_core
  src/book.cpp
  src/clob.cpp
  src/config.cpp
  src/crypto.cpp
  src/eip712.cpp
  src/engine.cpp
  src/feeds.cpp
  src/http.cpp
  src/json.cpp
  src/net.cpp
  src/platform.cpp
  src/presign.cpp
  src/runtime.cpp
  src/venue.cpp
  src/ws.cpp
)
target_include_directories(pkm_core PUBLIC include)
target_link_libraries(pkm_core PUBLIC OpenSSL::SSL OpenSSL::Crypto Threads::Threads secp256k1)
target_compile_options(pkm_core PRIVATE ${PKM_WARNINGS})

if(PKM_ENABLE_SANITIZERS)
  target_compile_options(pkm_core PUBLIC -fsanitize=address,undefined -fno-omit-frame-pointer)
  target_link_options(pkm_core PUBLIC -fsanitize=address,undefined)
endif()

add_executable(pkm src/main.cpp)
target_link_libraries(pkm PRIVATE pkm_core)
target_compile_options(pkm PRIVATE ${PKM_WARNINGS})

if(PKM_BUILD_TESTS)
  enable_testing()
  add_executable(pkm-tests
    tests/test_main.cpp
    tests/test_crypto.cpp
    tests/test_engine.cpp
    tests/test_feeds.cpp
    tests/test_json.cpp
    tests/test_net.cpp
  )
  target_link_libraries(pkm-tests PRIVATE pkm_core)
  target_compile_options(pkm-tests PRIVATE ${PKM_WARNINGS})
  add_test(NAME pkm-tests COMMAND pkm-tests ${CMAKE_CURRENT_SOURCE_DIR}/tests/fixtures)
  add_executable(pkm-e2e tests/e2e_main.cpp)
  target_link_libraries(pkm-e2e PRIVATE pkm_core)
  target_compile_options(pkm-e2e PRIVATE ${PKM_WARNINGS})
  add_test(NAME pkm-e2e COMMAND pkm-e2e)
  add_test(NAME pkm-usage COMMAND pkm)
  set_tests_properties(pkm-usage PROPERTIES WILL_FAIL TRUE)
  # Live is refused without PKM_LIVE_CONFIRM=yes (and without keys).
  add_test(NAME pkm-run-refused COMMAND pkm --env ${CMAKE_CURRENT_SOURCE_DIR}/config.example.env run)
  set_tests_properties(pkm-run-refused PROPERTIES WILL_FAIL TRUE)
endif()

install(TARGETS pkm RUNTIME DESTINATION bin)
