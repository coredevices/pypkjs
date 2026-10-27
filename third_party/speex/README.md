# Speex

[libspeex](https://github.com/xiph/speex) (BSD, see `speex/COPYING`) is a
submodule at `speex/`, pinned to the commit PebbleOS encodes watch microphone
audio with. pypkjs builds its codec into `pypkjs.microphone._speex` to decode
that audio as the phone app does; `setup.py` lists what it leaves out.

`config.h` and `speex_config_types.h` replace what upstream's configure step
generates, as PebbleOS does in its own `third_party/speex`.
