# ═══════════════════════════════════════════════════════════════════════════════
# OpenDCC  —  Docker multi-stage build
#
# Build context:  ShapeFX/   (parent of OpenDCC/ AND usd-viewer/)
# Build with:     docker-compose -f OpenDCC/docker/docker-compose.yml build
#              or docker build -f OpenDCC/docker/Dockerfile \
#                              -t opendcc:latest \
#                              ../..
#
# Stages
#   builder        – installs all C++ / Python build tools on ASWF ci-base
#   opendcc_build  – compiles OpenDCC with CMake/Ninja
#   runtime        – minimal image: compiled binaries + web server + usd-wasm
# ═══════════════════════════════════════════════════════════════════════════════


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 – dependency builder
#   ASWF CY2022 ci-base ships USD 22.05, Qt 5.15, PySide2, Python 3.9,
#   Boost 1.76, TBB 2020.3, OCIO 2.1, OIIO 2.3, OpenEXR 3.1,
#   OpenSubdiv 3.4  (VFX Reference Platform 2022)
# ─────────────────────────────────────────────────────────────────────────────
FROM aswf/ci-base:2022.1 AS builder

# CentOS 7 reached EOL June 2024 — mirrorlist.centos.org is dead.
# Redirect all base/extras/updates repos to vault.centos.org archive.
RUN sed -i \
        -e 's|^mirrorlist=|#mirrorlist=|g' \
        -e 's|^#baseurl=http://mirror\.centos\.org|baseurl=http://vault.centos.org|g' \
        /etc/yum.repos.d/CentOS-*.repo && \
    yum clean all && yum makecache fast

RUN yum install -y \
        zeromq-devel \
        glew-devel \
        eigen3-devel \
        bullet-devel \
        graphviz-devel \
    && yum clean all

# ── doctest (header-only) ─────────────────────────────────────────────────────
ARG DOCTEST_VERSION=2.4.11
RUN git clone --depth 1 --branch v${DOCTEST_VERSION} \
        https://github.com/doctest/doctest.git /tmp/doctest && \
    cmake -S /tmp/doctest -B /tmp/doctest/build \
          -DDOCTEST_WITH_TESTS=OFF \
          -DCMAKE_INSTALL_PREFIX=/opt/deps && \
    cmake --build /tmp/doctest/build --target install && \
    rm -rf /tmp/doctest

# ── Qt Advanced Docking System ────────────────────────────────────────────────
ARG ADS_VERSION=3.8.2
RUN git clone --depth 1 --branch ${ADS_VERSION} \
        https://github.com/githubuser0xFFFF/Qt-Advanced-Docking-System.git /tmp/ads && \
    cmake -S /tmp/ads -B /tmp/ads/build \
          -DCMAKE_BUILD_TYPE=Release \
          -DCMAKE_PREFIX_PATH=/usr/local \
          -DCMAKE_INSTALL_PREFIX=/opt/deps \
          -DBUILD_EXAMPLES=OFF && \
    cmake --build /tmp/ads/build -j$(nproc) --target install && \
    rm -rf /tmp/ads

# ── Sentry-native ─────────────────────────────────────────────────────────────
ARG SENTRY_VERSION=0.6.6
RUN git clone --depth 1 --branch ${SENTRY_VERSION} \
        https://github.com/getsentry/sentry-native.git /tmp/sentry && \
    cmake -S /tmp/sentry -B /tmp/sentry/build \
          -DCMAKE_BUILD_TYPE=RelWithDebInfo \
          -DSENTRY_BACKEND=breakpad \
          -DCMAKE_INSTALL_PREFIX=/opt/deps && \
    cmake --build /tmp/sentry/build -j$(nproc) --target install && \
    rm -rf /tmp/sentry

# ── OpenMesh ──────────────────────────────────────────────────────────────────
ARG OPENMESH_VERSION=9.0
RUN curl -fsSL https://www.graphics.rwth-aachen.de/media/openmesh_static/Releases/${OPENMESH_VERSION}/OpenMesh-${OPENMESH_VERSION}.tar.bz2 \
        | tar -xj -C /tmp && \
    cmake -S /tmp/OpenMesh-${OPENMESH_VERSION} -B /tmp/openmesh_build \
          -DCMAKE_BUILD_TYPE=Release \
          -DBUILD_APPS=OFF \
          -DCMAKE_INSTALL_PREFIX=/opt/deps && \
    cmake --build /tmp/openmesh_build -j$(nproc) --target install && \
    rm -rf /tmp/OpenMesh-${OPENMESH_VERSION} /tmp/openmesh_build

# ── libigl (header-only) ──────────────────────────────────────────────────────
RUN git clone --depth 1 https://github.com/libigl/libigl.git /opt/deps/include/igl_src && \
    ln -s /opt/deps/include/igl_src/include/igl /opt/deps/include/igl

# ── Python web dependencies ───────────────────────────────────────────────────
# NOTE: path is relative to the build context (ShapeFX/)
COPY OpenDCC/web/requirements_web.txt /tmp/requirements_web.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements_web.txt

RUN pip3 install --no-cache-dir pybind11


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 – OpenDCC build
# ─────────────────────────────────────────────────────────────────────────────
FROM builder AS opendcc_build

WORKDIR /src
# Copy the OpenDCC source tree (context = ShapeFX/, so prefix is OpenDCC/)
COPY OpenDCC/ /src/OpenDCC

RUN cmake -S /src/OpenDCC -B /src/build \
    -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=/opt/opendcc \
    -DUSD_ROOT=/usr/local \
    -DCMAKE_PREFIX_PATH="/usr/local;/opt/deps" \
    -DPython3_ROOT_DIR=/usr \
    -DDCC_PYSIDE_CMAKE_FIND=OFF \
    -DSHIBOKEN_CLANG_INSTALL_DIR=/usr/lib/llvm-13 \
    -Dqtadvanceddocking_DIR=/opt/deps/lib/cmake/qtadvanceddocking \
    -Dsentry_DIR=/opt/deps/lib/cmake/sentry \
    -Ddoctest_DIR=/opt/deps/lib/cmake/doctest \
    -DOpenMesh_DIR=/opt/deps/share/OpenMesh/cmake \
    -DDCC_BUILD_ARNOLD_SUPPORT=OFF \
    -DDCC_USD_FALLBACK_PROXY_BUILD_ARNOLD_USD=OFF \
    -DDCC_USD_FALLBACK_PROXY_BUILD_CYCLES=OFF \
    -DDCC_BUILD_RENDERMAN_SUPPORT=OFF \
    -DDCC_EMBEDDED_PYTHON_HOME=ON \
    -DDCC_BUILD_RENDER_VIEW=ON \
    -DDCC_BUILD_TESTS=OFF && \
    ninja -j$(nproc) install


# ─────────────────────────────────────────────────────────────────────────────
# Stage 3 – runtime image
# ─────────────────────────────────────────────────────────────────────────────
FROM aswf/ci-base:2022.1 AS runtime

# Same CentOS 7 EOL vault fix needed in the runtime stage
RUN sed -i \
        -e 's|^mirrorlist=|#mirrorlist=|g' \
        -e 's|^#baseurl=http://mirror\.centos\.org|baseurl=http://vault.centos.org|g' \
        /etc/yum.repos.d/CentOS-*.repo && \
    yum clean all && yum makecache fast

# ── OpenDCC compiled output ───────────────────────────────────────────────────
COPY --from=opendcc_build /opt/opendcc /opt/opendcc
COPY --from=opendcc_build /opt/deps    /opt/deps

# ── Web server sources ────────────────────────────────────────────────────────
COPY OpenDCC/web/ /opt/opendcc/web/

# ── needle-tools usd-wasm (WASM binaries + ThreeJsRenderDelegate) ─────────────
#   emHdBindings.wasm      ~16 MB  (OpenUSD compiled to WebAssembly)
#   emHdBindings.js        ~352 KB (WASM glue, sets globalThis["NEEDLE:USD:GET"])
#   emHdBindings.worker.js ~4 KB   (SharedArrayBuffer worker)
#   emHdBindings.data      ~829 KB (USD schema data bundle)
#   ThreeJsRenderDelegate.js       (Hydra → Three.js bridge)
COPY usd-viewer/usd-wasm/src     /opt/usd-viewer/usd-wasm/src

# ── es-module-shims polyfill (import-map support for older browsers) ──────────
COPY usd-viewer/public/modules   /opt/usd-viewer/public/modules

# ── Runtime Python packages ───────────────────────────────────────────────────
COPY OpenDCC/web/requirements_web.txt /tmp/requirements_web.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements_web.txt && \
    yum install -y zeromq glew && \
    yum clean all

# ── Environment ──────────────────────────────────────────────────────────────
ENV PYTHONPATH=/opt/opendcc/lib/python3.9/site-packages:/usr/local/lib/python3.9/site-packages
ENV LD_LIBRARY_PATH=/opt/opendcc/lib:/opt/deps/lib:/usr/local/lib
ENV PXR_PLUGINPATH_NAME=/opt/opendcc/plugin/usd:/opt/opendcc/plugin/opendcc
ENV OPENDCC_ROOT=/opt/opendcc

# Path where server.py looks for the usd-wasm src and es-module-shims
ENV USD_VIEWER_PATH=/opt/usd-viewer

# Headless Qt (no display required)
ENV QT_QPA_PLATFORM=offscreen
ENV DISPLAY=

WORKDIR /opt/opendcc
EXPOSE 8080

# Launch the FastAPI web server (starts OpenDCC headlessly, serves UI on :8080)
CMD ["python3", "/opt/opendcc/web/server.py", "--host", "0.0.0.0", "--port", "8080"]
