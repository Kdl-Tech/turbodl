#!/bin/sh
# Construit le paquet Debian de TurboDL : sh packaging/construire-deb.sh  ->  dist/turbodl_<version>_all.deb
set -eu
umask 022
cd "$(dirname "$0")/.."
V=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' turbodl.py)
[ -n "$V" ] || { echo "version introuvable" >&2; exit 1; }
R=$(mktemp -d); chmod 755 "$R"; trap 'rm -rf "$R"' EXIT
install -d "$R/DEBIAN" "$R/opt/turbodl" "$R/usr/bin" "$R/usr/share/applications" \
  "$R/usr/share/icons/hicolor/256x256/apps" "$R/usr/share/icons/hicolor/48x48/apps" "$R/usr/share/doc/turbodl"
install -m 644 turbodl.py logo-kdl.png turbodl.png "$R/opt/turbodl/"
cp -r extension "$R/opt/turbodl/extension"; find "$R/opt/turbodl/extension" -type f -exec chmod 644 {} +
install -m 644 turbodl.png "$R/usr/share/icons/hicolor/256x256/apps/turbodl.png"
python3 -c 'import sys; sys.path.insert(0, "."); import mkicon; open(sys.argv[1], "wb").write(mkicon.png(48))' "$R/usr/share/icons/hicolor/48x48/apps/turbodl.png"
install -m 644 LICENSE "$R/usr/share/doc/turbodl/copyright"
cat > "$R/usr/bin/turbodl" <<'L'
#!/bin/sh
exec /usr/bin/python3 /opt/turbodl/turbodl.py "$@"
L
chmod 755 "$R/usr/bin/turbodl"
cat > "$R/usr/share/applications/turbodl.desktop" <<'L'
[Desktop Entry]
Type=Application
Name=TurboDL
GenericName=Gestionnaire de téléchargements
GenericName[en]=Download manager
GenericName[es]=Gestor de descargas
Comment=Liens directs multi-connexions et BitTorrent
Comment[en]=Multi-connection direct links and BitTorrent
Comment[es]=Enlaces directos multiconexión y BitTorrent
Exec=env TURBODL_GUI=1 turbodl %U
Icon=turbodl
Terminal=false
Categories=Network;FileTransfer;P2P;
MimeType=application/x-bittorrent;x-scheme-handler/magnet;
StartupWMClass=Tk
Keywords=download;torrent;magnet;téléchargement;
L
cat > "$R/DEBIAN/control" <<L
Package: turbodl
Version: $V
Architecture: all
Maintainer: KDL TECH <contact@kdl-tech.fr>
Section: net
Priority: optional
Depends: python3 (>= 3.11), python3-tk, python3-libtorrent, xdg-utils
Recommends: python3-certifi, libnotify-bin, xdg-user-dirs
Homepage: https://kdl-tech.fr/logiciels/turbodl/
Description: gestionnaire de téléchargements (liens directs et BitTorrent)
 TurboDL télécharge les liens directs en plusieurs connexions parallèles avec
 reprise, et les torrents (magnet, .torrent) via libtorrent. Extension
 navigateur, file d'attente, limite de débit. Gratuiciel KDL TECH.
L
for s in postinst postrm; do cat > "$R/DEBIAN/$s" <<'L'
#!/bin/sh
set -e
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true
exit 0
L
chmod 755 "$R/DEBIAN/$s"; done
mkdir -p dist
dpkg-deb --root-owner-group -Zxz --build "$R" "dist/turbodl_${V}_all.deb" >/dev/null
sha256sum "dist/turbodl_${V}_all.deb"
