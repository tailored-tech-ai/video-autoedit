#!/bin/bash
# Собирает TightClick.app в ~/Applications и подписывает локальной подписью.
# После пересборки подпись меняется: в System Settings, Privacy & Security, Accessibility
# убери старую запись TightClick минусом и выдай доступ заново (TightClick setup).
set -euo pipefail
cd "$(dirname "$0")"
APP="$HOME/Applications/TightClick.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp Info.plist "$APP/Contents/Info.plist"
swiftc -O TightClick.swift -o "$APP/Contents/MacOS/TightClick"
codesign --force --sign - --identifier org.tailoredtech.tightclick "$APP"
echo "$APP"
