#!/bin/sh
# Builds /rootfs: just enough of Python to run DockFeed on a scratch image.
# docker/smoke.py runs inside the result during the build, so anything pruned
# here that turns out to be needed fails the build instead of the container.
set -eu

ver=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
stdlib=/usr/local/lib/python$ver
site=$stdlib/site-packages

# Python packages and the app go straight into site-packages.
rm -rf "$site"/*
cp -r /deps/. "$site"/
cp -r /app/dockfeed "$site"/

cd "$stdlib"
# Nothing a headless HTTP service imports: tests, GUIs, REPL and dev tooling,
# databases, the static libpython used only for embedding.
rm -rf test idlelib tkinter turtledemo turtle.py ensurepip lib2to3 pydoc_data pydoc.py \
    venv sqlite3 curses dbm unittest doctest.py xmlrpc wsgiref __phello__ _pyrepl \
    multiprocessing config-"$ver"-* tomllib zoneinfo pdb.py profile.py cProfile.py \
    pstats.py trace.py tabnanny.py mailbox.py imaplib.py smtplib.py ftplib.py poplib.py \
    nntplib.py telnetlib.py asyncio ctypes _pydecimal.py bz2.py lzma.py compression/zstd
cd lib-dynload
rm -f _tkinter* _sqlite3* _curses* _dbm* _gdbm* _test* xxlimited* _xxtestfuzz* \
    _ctypes_test* readline* _ctypes* _multiprocessing* _posixshmem* _lsprof* \
    _decimal* _zoneinfo* audioop* _crypt* nis* ossaudiodev* spwd* syslog* \
    _interpreters* _interpchannels* _interpqueues* xxsubtype* _asyncio* _bz2* _lzma* _zstd* \
    _uuid*
# Keep the CJK and other codecs: feeds arrive in GB2312, Shift-JIS and friends.
cd "$stdlib"

# Bytecode only: compile next to the source (-b, so it loads without the .py)
# with docstrings and asserts stripped, then drop the sources.
python3 -m compileall -q -b -o 2 "$stdlib"
find "$stdlib" -name '*.py' -delete
find "$stdlib" -name __pycache__ -type d -exec rm -rf {} +

mkdir -p /rootfs/usr/local/bin /rootfs/usr/local/lib /rootfs/etc/ssl/certs
cp -a "$stdlib" /rootfs/usr/local/lib/
cp -L "/usr/local/bin/python$ver" /rootfs/usr/local/bin/python
cp -L /usr/local/lib/libpython"$ver".so.1.0 /rootfs/usr/local/lib/

# Every shared library the interpreter and the remaining extension modules
# link against, plus the musl loader. ldd keeps this right on every arch.
for f in /rootfs/usr/local/bin/python /rootfs/usr/local/lib/libpython*.so* "$stdlib"/lib-dynload/*.so; do
    ldd "$f" 2>/dev/null | grep -o '/[^ ]*' || true
done | sort -u | while read -r lib; do
    case "$lib" in /rootfs/*) continue ;; esac
    mkdir -p "/rootfs$(dirname "$lib")"
    cp -L "$lib" "/rootfs$lib"
done

cp -L /etc/ssl/certs/ca-certificates.crt /rootfs/etc/ssl/certs/
cp -L /etc/ssl/certs/ca-certificates.crt /rootfs/etc/ssl/cert.pem

# Shared objects carry debug symbols the runtime never reads.
find /rootfs -name '*.so*' -type f -exec strip --strip-unneeded {} + 2>/dev/null || true
strip --strip-unneeded /rootfs/usr/local/bin/python 2>/dev/null || true
