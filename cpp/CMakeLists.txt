cmake_minimum_required(VERSION 3.16)
project(caravan CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# v0: links directly against an already-built llama.cpp checkout rather
# than building it as part of this project. LLAMA_CPP_DIR is where the
# proper submodule vendoring (see docs/ARCHITECTURE.md) will eventually
# live; for now it defaults to the existing standalone clone this whole
# project has been running from all along.
set(LLAMA_CPP_DIR "$ENV{HOME}/dev/llama.cpp" CACHE PATH "Path to a built llama.cpp checkout")
set(LLAMA_CPP_BUILD_DIR "${LLAMA_CPP_DIR}/build" CACHE PATH "Path to that checkout's build directory")

add_executable(caravan
    main.cpp
    subprocess.cpp
    discovery_mdns.cpp
)

target_include_directories(caravan PRIVATE
    ${LLAMA_CPP_DIR}/ggml/include
    ${LLAMA_CPP_DIR}/vendor/nlohmann
)

target_link_directories(caravan PRIVATE
    ${LLAMA_CPP_BUILD_DIR}/bin
)

target_link_libraries(caravan PRIVATE
    llama-server-impl
    ggml-rpc
    ggml
    ggml-base
)

# Runtime search path: these are all still real dylibs in llama.cpp's own
# build/bin, not vendored into caravan's own build product -- so
# `caravan` needs to be able to find them at load time via rpath.
set_target_properties(caravan PROPERTIES
    BUILD_RPATH "${LLAMA_CPP_BUILD_DIR}/bin"
    INSTALL_RPATH "${LLAMA_CPP_BUILD_DIR}/bin"
)
