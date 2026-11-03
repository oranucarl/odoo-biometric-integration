#!/bin/sh
# Build libfpmatch.dylib (NBIS minutiae extraction + bozorth3 matching).
# Needs Homebrew's glib:  brew install glib
set -e
cd "$(dirname "$0")"

GLIB="$(brew --prefix glib)"

cc -O2 -dynamiclib -o libfpmatch.dylib \
  -Iinclude -Ilibfprint-include \
  -I"$GLIB/include/glib-2.0" -I"$GLIB/lib/glib-2.0/include" \
  -Wno-everything \
  fpmatch.c bozorth3/*.c mindtct/*.c \
  -L"$GLIB/lib" -lglib-2.0

echo "Built $(pwd)/libfpmatch.dylib"
