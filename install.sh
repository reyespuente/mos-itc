#!/bin/bash
# MOS ITC installer. Run from a checkout, or supply --repo OWNER/REPO --ref TAG_OR_SHA.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C.UTF-8
umask 077

repo=''
ref=''
allow_external=false
upgrade=false
while (($#)); do
    case "$1" in
        --repo) repo="${2:?Missing OWNER/REPO}"; shift 2 ;;
        --ref) ref="${2:?Missing tag or commit}"; shift 2 ;;
        --upgrade) upgrade=true; shift ;;
        --allow-external-repositories) allow_external=true; shift ;;
        --help) echo 'Usage: sudo bash install.sh [--repo OWNER/REPO --ref TAG_OR_SHA] [--allow-external-repositories] [--upgrade]'; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done
if [[ ! -t 0 ]]; then
    echo 'Interactive terminal required. Download install.sh first, then run it; do not pipe curl into bash.' >&2
    exit 1
fi
read -r -p 'Continue with MOS ITC installation? / ¿Continuar? [y/N]: ' answer
[[ "$answer" =~ ^([yY]|[sS])$ ]] || exit 0
printf '\nInstituto Tecnológico de Celaya\n'
if [[ -z "${NO_COLOR+x}" ]]; then printf '\033[38;2;17;117;51m'; fi
cat <<'WORDMARK'
 __  __  ___  ____    ___ _____ ____
|  \/  |/ _ \/ ___|  |_ _|_   _/ ___|
| |\/| | | | \___ \   | |  | || |
| |  | | |_| |___) |  | |  | || |___
|_|  |_|\___/|____/  |___| |_| \____|
WORDMARK
if [[ -z "${NO_COLOR+x}" ]]; then printf '\033[0m'; fi
printf 'Manage your Own Server\n'
printf 'Created by Reyes Guadalupe Puente Balcazar\n'
printf 'Created to make server administration easier from one place.\n'
printf 'Iniciativa creada en el ITC, de libre uso; no implica respaldo institucional oficial.\n\n'

source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
scratch=''
app_stage=''
cleanup() { if [[ -n "$scratch" ]]; then rm -rf -- "$scratch"; fi; if [[ -n "$app_stage" ]]; then rm -rf -- "$app_stage"; fi; }
trap cleanup EXIT
if [[ ! -f "$source_dir/mos_itc/cli.py" ]]; then
    [[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo 'Supply --repo OWNER/REPO for your published GitHub project'; exit 1; }
    [[ "$ref" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,100}$ ]] || { echo 'Supply --ref with a release tag or commit SHA'; exit 1; }
    command -v curl >/dev/null || { echo 'Install curl first, or download and extract the repository manually.'; exit 1; }
    scratch="$(mktemp -d -t mos-install-XXXXXXXX)"
    curl --fail --show-error --silent --location --proto '=https' --proto-redir '=https' \
        "https://codeload.github.com/$repo/tar.gz/$ref" -o "$scratch/source.tar.gz"
    # Extract as an ordinary user first when available. No arbitrary archives accepted.
    if tar -tzf "$scratch/source.tar.gz" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
        echo 'Unsafe archive paths'; exit 1
    fi
    mkdir "$scratch/tree"
    tar --extract --gzip --file "$scratch/source.tar.gz" --directory "$scratch/tree" \
        --strip-components=1 --no-same-owner --no-same-permissions
    source_dir="$scratch/tree"
fi
[[ -f "$source_dir/LICENSE" ]] || { echo 'LICENSE missing'; exit 1; }
cat "$source_dir/LICENSE"
printf '\n'
read -r -p 'Accept the MIT license? Type ACCEPT / Aceptar licencia: ' answer
[[ "$answer" == 'ACCEPT' ]] || { echo 'License not accepted; nothing installed.'; exit 0; }
[[ "$EUID" -eq 0 ]] || { echo 'Run: sudo bash install.sh'; exit 1; }
# Read only distribution-provided identification, not a user-supplied shell config.
. /etc/os-release
case "$ID:$VERSION_ID" in
    debian:11|debian:12|debian:13|ubuntu:22.04|ubuntu:24.04|ubuntu:26.04) ;;
    *) echo "Unsupported host: $PRETTY_NAME. Install inside your Debian/Ubuntu VM, not on Linux Mint."; exit 1 ;;
esac
[[ "$(uname -m)" == x86_64 ]] || { echo 'This beta supports x86_64 only'; exit 1; }
command -v apt-get >/dev/null
[[ -d /run/systemd/system ]] || { echo 'A running systemd installation is required'; exit 1; }
[[ ! -L /opt/mos-itc && ! -L /etc/mos-itc && ! -L /var/lib/mos-itc ]] || { echo 'Unexpected symbolic link in installation paths'; exit 1; }
if [[ -e /opt/mos-itc || -e /usr/local/bin/mos || -e /usr/local/bin/mos-itc ]]; then
    if [[ "$upgrade" != true || ! -f /opt/mos-itc/mos_itc/cli.py ]]; then
        echo 'MOS already exists. Close the application, then use --upgrade to preserve sites and settings.'
        exit 1
    fi
    read -r -p 'Upgrade MOS code while keeping websites and settings? Type UPGRADE: ' answer
    [[ "$answer" == UPGRADE ]] || exit 0
fi
# Conservative source allowlist, including Debian822 and disabled-source handling.
shopt -s nullglob
sources=(/etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources)
[[ ! -f /etc/apt/sources.list ]] || sources+=(/etc/apt/sources.list)
source_uris=()
if ((${#sources[@]})); then
    mapfile -t source_uris < <(awk '
    BEGIN { RS=""; FS="\n" }
    FILENAME ~ /\.sources$/ {
        if ($0 ~ /(^|\n)Enabled:[ \t]*no([ \t]*\n|$)/) next
        continuation=0
        for(i=1;i<=NF;i++) {
            if($i ~ /^URIs:/) { sub(/^URIs:[ \t]*/,"",$i); continuation=1; print $i }
            else if(continuation && $i ~ /^[ \t]/) print $i
            else continuation=0
        }
        next
    }
    { for(i=1;i<=NF;i++) if($i ~ /^[ \t]*deb(-src)?[ \t]/) {
        line=$i; gsub(/\[[^]]*\]/,"",line); sub(/^[ \t]*/,"",line)
        split(line,parts,/[ \t]+/); print parts[2]
    }}' "${sources[@]}" | tr ' \t' '\n' | sed '/^$/d')
fi
for uri in "${source_uris[@]}"; do
    case "$uri" in
        http://deb.debian.org/*|https://deb.debian.org/*|http://security.debian.org/*|https://security.debian.org/*|http://archive.ubuntu.com/*|https://archive.ubuntu.com/*|http://*.archive.ubuntu.com/*|https://*.archive.ubuntu.com/*|http://security.ubuntu.com/*|https://security.ubuntu.com/*|http://ports.ubuntu.com/*|https://ports.ubuntu.com/*) ;;
        *)
            if [[ "$allow_external" != true ]]; then
                echo "External/custom source detected: $uri"
                echo 'Disable it or explicitly use --allow-external-repositories at your own responsibility.'
                exit 1
            fi ;;
    esac
done
if [[ "$allow_external" == true ]]; then
    read -r -p 'Third-party package sources are your responsibility. Type ALLOW: ' answer
    [[ "$answer" == ALLOW ]] || exit 0
fi
# Capture configuration and package inventory BEFORE package/code changes.
install -d -m 0700 /var/backups/mos-itc/restore-points
point="/var/backups/mos-itc/restore-points/$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0700 "$point"
tar --create --gzip --file "$point/etc.tar.gz" --directory / etc
cp --preserve=mode /var/lib/dpkg/status "$point/dpkg-status"
dpkg-query -W -f='${binary:Package}\t${Version}\t${Status}\n' > "$point/packages.tsv"
apt-mark showmanual > "$point/manual-packages.txt"
systemctl list-unit-files --type=service --no-pager > "$point/services.txt"
if [[ -f /var/lib/mos-itc/state.json ]]; then
    cp /var/lib/mos-itc/state.json "$point/mos-state.json"
fi
if [[ -d /opt/mos-itc ]]; then
    tar --create --gzip --file "$point/previous-app.tar.gz" --directory / opt/mos-itc
fi
printf 'Configuration restore point: %s\n' "$point"
# Only bootstrap the CLI here. The menu separately plans and confirms server packages.
if [[ "$upgrade" == true && -x /usr/bin/python3 && -x /usr/bin/nano ]]; then
    echo "Python runtime present; upgrading application files."
else
    apt-get update
    apt-get install -y --no-install-recommends python3 ca-certificates nano
fi
install -d -m 0755 /opt /usr/local/bin
install -d -m 0700 /etc/mos-itc /var/lib/mos-itc
exec 9>/var/lib/mos-itc/operation.lock
flock -n 9 || { echo 'A MOS operation is running. Close MOS and try again.'; exit 1; }
app_stage="$(mktemp -d /opt/.mos-itc-stage-XXXXXXXX)"
chmod 0755 "$app_stage"
install -d -m 0755 "$app_stage/mos_itc" "$app_stage/languages" "$app_stage/assets"
for file in __init__.py __main__.py core.py web.py ops.py ui.py cli.py i18n.py terminal.py recovery.py users.py; do
    [[ -f "$source_dir/mos_itc/$file" && ! -L "$source_dir/mos_itc/$file" ]] || { echo "Missing or unsafe file: $file"; exit 1; }
    install -m 0644 "$source_dir/mos_itc/$file" "$app_stage/mos_itc/$file"
done
for language in en es; do
    [[ -f "$source_dir/languages/$language.json" && ! -L "$source_dir/languages/$language.json" ]] || { echo 'Missing language catalog'; exit 1; }
    /usr/bin/python3 -m json.tool "$source_dir/languages/$language.json" >/dev/null
    install -m 0644 "$source_dir/languages/$language.json" "$app_stage/languages/$language.json"
done
/usr/bin/python3 -m json.tool "$source_dir/assets/itc-logo.json" >/dev/null
install -m 0644 "$source_dir/assets/itc-logo.json" "$app_stage/assets/itc-logo.json"
install -m 0644 "$source_dir/LICENSE" "$app_stage/LICENSE"
/usr/bin/python3 -m compileall -q "$app_stage/mos_itc"
chown -R root:root "$app_stage"
if [[ -d /opt/mos-itc ]]; then
    previous="/opt/mos-itc.previous-$(date +%Y%m%d%H%M%S)"
    mv /opt/mos-itc "$previous"
fi
mv "$app_stage" /opt/mos-itc
app_stage=''
cat > /usr/local/bin/mos-itc <<'LAUNCHER'
#!/bin/sh
unset PYTHONPATH PYTHONHOME
exec /usr/bin/python3 -I -c 'import sys; sys.path.insert(0,"/opt/mos-itc"); from mos_itc.cli import main; raise SystemExit(main())' "$@"
LAUNCHER
chmod 0755 /usr/local/bin/mos-itc
ln -sfn mos-itc /usr/local/bin/mos
chown -R root:root /opt/mos-itc /etc/mos-itc /var/lib/mos-itc /usr/local/bin/mos-itc
# Brand the login welcome while leaving update/reboot notices in place.
if [[ -d /etc/update-motd.d ]]; then
    if [[ -f /etc/update-motd.d/00-header ]] && ! dpkg-divert --list /etc/update-motd.d/00-header | grep -q .; then
        dpkg-divert --local --rename --add /etc/update-motd.d/00-header
        printf '["/etc/update-motd.d/00-header"]\n' > /var/lib/mos-itc/motd-diversions.json
    fi
    cat > /etc/update-motd.d/00-mos-itc <<'MOTD'
#!/bin/sh
MOS_MOTD=1 /usr/local/bin/mos-itc banner
MOTD
    chmod 0755 /etc/update-motd.d/00-mos-itc
fi
printf '\nMOS ITC installed. Run: sudo mos-itc\n' 
printf 'No passwordless sudo rule was created. Existing services were not configured.\n'
