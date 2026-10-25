#!/bin/sh
# Pack the macOS app into a disk image with the usual "drag to Applications" layout.
#   packaging/macos/make-dmg.sh dist/vinyl-check.app dist/vinyl-check-1.0.1-macos-arm64.dmg
set -eu
app="$1"
dmg="$2"
[ -d "$app" ] || { echo "no app bundle at $app" >&2; exit 1; }
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
ditto "$app" "$staging/$(basename "$app")"         # ditto keeps symlinks, permissions and the code signature
ln -s /Applications "$staging/Applications"
# the license texts also lie next to the app, readable without opening it
for f in LICENSE.txt THIRD-PARTY-NOTICES.txt; do
  if [ -f "$app/Contents/Resources/$f" ]; then cp "$app/Contents/Resources/$f" "$staging/$f"; fi
done
rm -f "$dmg"
hdiutil create -volname "vinyl-check" -srcfolder "$staging" -fs HFS+ -format UDZO -imagekey zlib-level=9 "$dmg"
echo "$dmg: $(du -h "$dmg" | cut -f1)"
