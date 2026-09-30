#!/bin/bash
# Build "Restic Control.app" and sign it with a stable identity.
#
# macOS ties Full Disk Access and Keychain "Always Allow" to the app's code signature.
# py2app's default ad-hoc signature changes on every build, so those permissions would
# be lost each time.  Signing with the same certificate keeps them.
#
# One-time setup:  ./make-signing-cert.sh   (creates the certificate from Terminal)
# Use another name by setting SIGN_IDENTITY="…".
set -euo pipefail
cd "$(dirname "$0")"
IDENTITY="${SIGN_IDENTITY:-Restic Control Signing}"
APP="dist/Restic Control.app"

rm -rf build dist
python setup.py py2app

# sign by certificate hash, so a duplicate certificate with the same name can't make it ambiguous
HASH="$(security find-identity -p codesigning | awk -v n="\"${IDENTITY}\"" 'index($0, n) {print $2; exit}')"
if [[ -n "${HASH}" ]]; then
    codesign --force --deep --timestamp=none --sign "${HASH}" "${APP}"
    codesign --verify --deep --strict "${APP}"
    echo "✓ Signed with “${IDENTITY}”."
else
    echo "⚠ No code-signing certificate named “${IDENTITY}” found (run ./make-signing-cert.sh);"
    echo "  the app keeps py2app's"
    echo "  ad-hoc signature, so Full Disk Access / Keychain approval reset on every rebuild."
fi

if [[ "${INSTALL:-1}" == "1" ]]; then
    rm -rf "/Applications/Restic Control.app"
    cp -R "${APP}" /Applications/
    echo "✓ Installed to /Applications/Restic Control.app"
fi
