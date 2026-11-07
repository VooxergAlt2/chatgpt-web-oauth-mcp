# Provisioning the ACP pytest runtime

The Linux builder creates a dedicated virtualenv and publishes it atomically only after a real pytest check and runtime manifest validation. It refuses an existing destination. Its default exact pins match the service environment observed on 2026-11-07: pytest 9.1.1, iniconfig 2.3.0, packaging 26.3, pluggy 1.6.0, Pygments 2.21.0.

Run with the service Python (pip is used only as a build tool):

```sh
PYTHONPATH=src .venv/bin/python scripts/build_verification_runtime.py \
  --root /home/vooxerg/.local/share/opss-verification-runtimes/python-pytest-20261107 \
  --id python-pytest-20261107
```

The builder fetches only pinned universal wheels from public PyPI, verifies their declared SHA256 and sizes, and installs them locally with `--no-index --no-deps` in a cleaned environment. It does not copy the service environment or authentication files. The final runtime has one regular executable, `bin/python`; use `python -m pytest`, not a console-script path. During verification it is mounted read-only at `/opt/opss-verification`, and the operator profile returned by the builder binds the original canonical path and manifest digest. Enabling the returned `verification_runtime` object in the private ACP operator configuration is a separate operator action.

Every runtime file is listed in `manifest.json` with its digest, size and read-only mode. Symlinks, special files and hardlinks are rejected; the `lib64` virtualenv alias and unneeded launchers are removed. Rebuild changed pins into a new versioned destination and validate that profile before selecting it. Never mutate a selected runtime in place.
