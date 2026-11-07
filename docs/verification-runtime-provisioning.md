# Provisioning the ACP pytest runtime

The Linux builder creates a dedicated virtualenv and publishes it atomically only after a real pytest check and runtime manifest validation. It refuses an existing destination. Its default exact pins match the service environment observed on 2026-11-07: pytest 9.1.1, iniconfig 2.3.0, packaging 26.3, pluggy 1.6.0, Pygments 2.21.0.

Run with the service Python (pip is used only as a build tool):

```sh
PYTHONPATH=src .venv/bin/python scripts/build_verification_runtime.py \
  --root /home/vooxerg/.local/share/opss-verification-runtimes/python-pytest-20261107 \
  --id python-pytest-20261107
```

The default preset fetches pinned universal wheels from public PyPI, verifies their declared SHA256 and sizes, and installs them locally with `--no-index --no-deps` in a cleaned environment. It does not copy the service environment or authentication files. The final runtime has one regular executable, `bin/python`; use `python -m pytest`, not a console-script path. During verification it is mounted read-only at `/opt/opss-verification`, and the operator profile returned by the builder binds the original canonical path and manifest digest. Enabling the returned `verification_runtime` object in the private ACP operator configuration is a separate operator action.

Every runtime file is listed in `manifest.json` with its digest, size and read-only mode. Symlinks, special files and hardlinks are rejected; the `lib64` virtualenv alias and unneeded launchers are removed. Rebuild changed pins into a new versioned destination and validate that profile before selecting it. Never mutate a selected runtime in place.

## Opss project tests

The minimal pytest preset cannot run the repository suite: its autouse fixture imports the executor and its project dependencies. Use the `opss` preset for this repository:

```sh
PYTHONPATH=src .venv/bin/python scripts/build_verification_runtime.py \
  --preset opss \
  --root /home/vooxerg/.local/share/opss-verification-runtimes/python-opss-20261107 \
  --id python-opss-20261107
```

`scripts/verification-runtime-opss-py312-linux.lock` records the 81-package base plus dev dependency closure observed in the service environment on 2026-11-07. Requirement extras and platform markers were included when deriving it; all pins satisfied the declared constraints. The optional ACP SDK was absent from that environment and is not silently added. The installed base closure includes websockets 17.1; the separately configured ACP worker owns its SDK dependencies.

The preset accepts native wheels only when the build Python and target Python report the same CPython 3.12 ABI and platform. Compatible wheels are ranked by `packaging.tags`; incompatible interpreter/ABI wheels and wrong distribution/version names are rejected. Public artifact SHA256, size limits and read-only manifest checks apply to native and universal wheels alike.

The preset prefetches tiktoken's `o200k_base` data into `lib/tiktoken-cache` before freezing the manifest. The pinned tiktoken loader checks the download's expected hash. Repository checks use this explicit, contract-bound command prefix inside the network-isolated verifier:

```sh
TIKTOKEN_CACHE_DIR=/opt/opss-verification/lib/tiktoken-cache PYTHONPATH=src \
  python -B -m pytest -q -p no:cacheprovider tests/test_delegate_packet_completeness.py tests/test_verification_capture.py
```

The asset directory is part of the same immutable profile as its dependencies. No startup hook or inherited host cache is used. Successful installation and a host smoke test establish provisioning only; acceptance still requires running the selected project tests in the actual verification namespace.
