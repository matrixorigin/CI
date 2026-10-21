# Apple Silicon release packaging

The caller builds MatrixOne and imports its Developer ID certificate. This action
uses the pinned MatrixOrigin gon fork and the checked-out release's
`optools/sign/gon.json` identity/account settings. `AC_PASSWORD` and `AC_PROVIDER`
remain environment secrets; no unsigned fallback is provided.

The payload includes the executable, third-party dylibs, `cgo/libmo.dylib`, launch
configs, and dictionary/Python service when present. It validates native arm64
dependencies, signs every Mach-O, and notarizes the complete final zip. A fresh
extraction must load without developer-library environment overrides. Only then
is the archive exposed through `asset-path` for upload; the action does not upload
or modify a release itself.

Commands have deadlines and cancellation kills their owned process group. Staging
is per-run and cleaned on error/cancellation; checkout cleanup handles leftovers
from uncatchable runner termination. The caller owns a unique temporary signing
keychain and its post-action cleanup.

Run `python3 -m unittest discover -s actions/package-macos-release -p 'test_*.py' -v`.
Most contracts run offline on Linux/macOS. On Apple Silicon the same suite also
compiles a tiny real Mach-O/dylib fixture and tests extraction/loading. That local
test uses ad-hoc signing; it is not evidence of Apple notarization or a full MO build.

An Apple `403` for a missing/expired agreement requires the account owner to fix
the agreement. This action must fail, not bypass notarization, in that case.
