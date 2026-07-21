FROM ubuntu:18.04
# lib32z1 + lib32stdc++6: xr872_sdk/tools/mkimage is a 32-bit Linux ELF; needed
# to run it in this 64-bit image (see docs/screens/bigme_f7/firmware_build.md).
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc-arm-none-eabi \
    binutils-arm-none-eabi \
    libnewlib-arm-none-eabi \
    libstdc++-arm-none-eabi-newlib \
    make \
    findutils \
    lib32z1 \
    lib32stdc++6 \
    && rm -rf /var/lib/apt/lists/*
