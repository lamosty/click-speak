#!/usr/bin/env bash
set -euo pipefail

APP_NAME="ClickSpeak"
APP_BUNDLE="/Applications/${APP_NAME}.app"
LEGACY_APP_BUNDLE="/Applications/clickspeak.app"
APP_BUNDLE_ID="com.lamosty.clickspeak"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_DIR="${HOME}/Library/Application Support/ClickSpeak/runtime"
RUNTIME_PYTHON="${RUNTIME_DIR}/bin/python3"
RUNTIME_MARKER_FILE=".clickspeak-runtime"
REQUIRED_PYTHON_MAJOR=3
REQUIRED_PYTHON_MINOR=12

python_version() {
    local python_path="$1"
    "${python_path}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
}

python_is_supported() {
    local python_path="$1"
    local version

    if [ ! -x "${python_path}" ]; then
        return 1
    fi

    if ! version="$(python_version "${python_path}" 2>/dev/null)"; then
        return 1
    fi

    [ "${version}" = "${REQUIRED_PYTHON_MAJOR}.${REQUIRED_PYTHON_MINOR}" ]
}

resolve_host_python() {
    local candidate
    local candidates

    candidates=()
    if [ -n "${CLICKSPEAK_HOST_PYTHON:-}" ]; then
        candidates+=("${CLICKSPEAK_HOST_PYTHON}")
    fi

    for candidate in \
        "$(command -v python3.12 || true)" \
        "$(command -v python3 || true)"; do
        [ -n "${candidate}" ] && candidates+=("${candidate}")
    done

    for candidate in "${candidates[@]}"; do
        if python_is_supported "${candidate}"; then
            echo "${candidate}"
            return 0
        fi
    done

    return 1
}

runtime_is_ready() {
    local runtime_version
    if [ ! -x "${RUNTIME_PYTHON}" ]; then
        return 1
    fi

    if [ ! -f "${RUNTIME_DIR}/${RUNTIME_MARKER_FILE}" ]; then
        return 1
    fi

    runtime_version="$(cat "${RUNTIME_DIR}/${RUNTIME_MARKER_FILE}" 2>/dev/null || true)"
    if [ "${runtime_version}" != "${REQUIRED_PYTHON_MAJOR}.${REQUIRED_PYTHON_MINOR}" ]; then
        return 1
    fi

    python_is_supported "${RUNTIME_PYTHON}"
}

ensure_runtime() {
    local bootstrap_python
    local python_version_line

    if runtime_is_ready; then
        return 0
    fi

    bootstrap_python="$(resolve_host_python || true)"
    if [ -z "${bootstrap_python}" ]; then
        echo "[clickspeak] Could not locate a supported Python ${REQUIRED_PYTHON_MAJOR}.${REQUIRED_PYTHON_MINOR} runtime."
        echo "[clickspeak] Install Python ${REQUIRED_PYTHON_MAJOR}.${REQUIRED_PYTHON_MINOR} and re-run."
        return 1
    fi

    rm -rf "${RUNTIME_DIR}"
    mkdir -p "${RUNTIME_DIR}"
    echo "[clickspeak] Bootstrapping app runtime at ${RUNTIME_DIR} using ${bootstrap_python}"
    "${bootstrap_python}" -m venv "${RUNTIME_DIR}"
    "${RUNTIME_PYTHON}" -m pip install --upgrade pip setuptools wheel >/dev/null
    "${RUNTIME_PYTHON}" -m pip install -e "${PROJECT_DIR}"
    python_version_line="${REQUIRED_PYTHON_MAJOR}.${REQUIRED_PYTHON_MINOR}"
    printf "%s\n" "${python_version_line}" > "${RUNTIME_DIR}/${RUNTIME_MARKER_FILE}"
}

write_info_plist() {
    local bundle_path="$1"
    cat > "${bundle_path}/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>ClickSpeak</string>
    <key>CFBundleDisplayName</key>
    <string>ClickSpeak</string>
    <key>CFBundleIdentifier</key>
    <string>com.lamosty.clickspeak</string>
    <key>CFBundleVersion</key>
    <string>0.1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>0.1.0</string>
    <key>CFBundleExecutable</key>
    <string>ClickSpeak</string>
    <key>CFBundleIconFile</key>
    <string>ClickSpeak.icns</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSUIElement</key>
    <true/>
    <key>LSApplicationCategoryType</key>
    <string>public.app-category.productivity</string>
    <key>NSMicrophoneUsageDescription</key>
    <string>ClickSpeak needs microphone access for voice-to-text transcription.</string>
    <key>NSAppleEventsUsageDescription</key>
    <string>ClickSpeak needs accessibility access to paste transcribed text into other apps.</string>
    <key>NSInputMonitoringUsageDescription</key>
    <string>ClickSpeak needs input monitoring permission to use the global hotkey (Option+Space).</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST
}

compile_launcher() {
    local bundle_path="$1"
    local python_path="$2"
    local source="${PROJECT_DIR}/src/launcher.c"
    local binary="${bundle_path}/Contents/MacOS/ClickSpeak"

    mkdir -p "${bundle_path}/Contents/MacOS"
    echo "[clickspeak] Compiling native launcher..."

    local include_dir
    include_dir="$("${python_path}" -c "import sysconfig; print(sysconfig.get_path('include'))")"
    if [ ! -d "${include_dir}" ]; then
        echo "[clickspeak] ERROR: Python include directory not found: ${include_dir}"
        echo "[clickspeak] Install Python 3.12 development headers."
        exit 1
    fi

    local framework_prefix
    framework_prefix="$("${python_path}" -c "import sysconfig; print(sysconfig.get_config_var('PYTHONFRAMEWORKPREFIX') or '')")"

    if [ -n "${framework_prefix}" ]; then
        echo "[clickspeak] Using framework Python at ${framework_prefix}"
        cc -o "${binary}" \
            "${source}" \
            -I"${include_dir}" \
            -F"${framework_prefix}" \
            -framework Python \
            -Wl,-rpath,"${framework_prefix}" \
            -O2
    else
        local libdir ldversion
        libdir="$("${python_path}" -c "import sysconfig; print(sysconfig.get_config_var('LIBDIR'))")"
        ldversion="$("${python_path}" -c "import sysconfig; print(sysconfig.get_config_var('LDVERSION') or sysconfig.get_config_var('VERSION'))")"
        echo "[clickspeak] Using non-framework Python: libdir=${libdir}"
        cc -o "${binary}" \
            "${source}" \
            -I"${include_dir}" \
            -L"${libdir}" \
            -lpython"${ldversion}" \
            -Wl,-rpath,"${libdir}" \
            -O2
    fi

    echo "[clickspeak] Native launcher compiled: ${binary}"
    echo "[clickspeak] Binary type: $(file "${binary}")"
}

write_bundle() {
    local bundle_path="$1"
    mkdir -p "${bundle_path}/Contents/MacOS"
    mkdir -p "${bundle_path}/Contents/Resources"
    write_info_plist "${bundle_path}"
    compile_launcher "${bundle_path}" "${RUNTIME_PYTHON}"
    if [ -f "${PROJECT_DIR}/ClickSpeak.icns" ]; then
        cp -n "${PROJECT_DIR}/ClickSpeak.icns" "${bundle_path}/Contents/Resources/" 2>/dev/null || true
    fi
    echo "[clickspeak] Wrote bundle ${bundle_path}"
}

echo "[clickspeak] Building launch bundles..."
ensure_runtime
if [ -d "${LEGACY_APP_BUNDLE}" ]; then
    rm -rf "${LEGACY_APP_BUNDLE}"
fi
write_bundle "${APP_BUNDLE}"

# Ad-hoc code sign so macOS recognizes the bundle identity
codesign --force --deep --sign - "${APP_BUNDLE}"

echo "[clickspeak] Launch bundles ready"
echo "[clickspeak] Quick permissions check:"
"${APP_BUNDLE}/Contents/MacOS/ClickSpeak" --check-permissions || true
