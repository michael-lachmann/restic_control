#!/bin/bash
# Create a self-signed code-signing certificate in your login keychain, once.
# build.sh signs every build with it, so macOS keeps Full Disk Access and the
# Keychain "Always Allow" approval across rebuilds.  No Keychain Access app needed.
set -euo pipefail
NAME="${SIGN_IDENTITY:-Restic Control Signing}"
OPENSSL=/usr/bin/openssl     # macOS's LibreSSL: its .p12 files import cleanly with `security`

if security find-identity -p codesigning | grep -qF "\"${NAME}\""; then
    echo "✓ Certificate “${NAME}” already exists."
    exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
cat > "${TMP}/cert.cnf" <<CNF
[req]
distinguished_name = dn
x509_extensions    = ext
prompt             = no
[dn]
CN = ${NAME}
[ext]
basicConstraints = critical,CA:false
keyUsage         = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
CNF

"${OPENSSL}" req -x509 -newkey rsa:2048 -nodes -days 3650 -config "${TMP}/cert.cnf" \
    -keyout "${TMP}/key.pem" -out "${TMP}/cert.pem" 2>/dev/null
PASS="$(uuidgen)"
"${OPENSSL}" pkcs12 -export -inkey "${TMP}/key.pem" -in "${TMP}/cert.pem" -name "${NAME}" \
    -out "${TMP}/${NAME}.p12" -passout "pass:${PASS}"
# The key gets its label from the .p12 file name, hence "${NAME}.p12".
# -T: let codesign use the private key without asking every time
security import "${TMP}/${NAME}.p12" -k "${HOME}/Library/Keychains/login.keychain-db" \
    -P "${PASS}" -T /usr/bin/codesign

echo "✓ Created code-signing certificate “${NAME}” (valid 10 years)."

# Let Apple's signing tools use the key without a dialog for every signed file.
echo "Allowing codesign to use the key — enter your login (keychain) password:"
security set-key-partition-list -S apple-tool:,apple: -s -l "${NAME}" \
    "${HOME}/Library/Keychains/login.keychain-db" >/dev/null \
    && echo "✓ codesign may use the key without asking." \
    || echo "⚠ Skipped; if build.sh keeps asking, click Always Allow in the dialog."
echo "  Now run ./build.sh."
