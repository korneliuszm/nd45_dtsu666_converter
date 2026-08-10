#!/usr/bin/env bash
# Install / upgrade this bridge on a deployed host, reproducing docs/DEPLOY.md
# exactly (see there for the full "why"). Handles two documented traps
# automatically instead of leaving them to operator memory:
#
#   - overlayroot (DEPLOY.md 0.4): on a host where / is an overlay with a
#     tmpfs writable layer, `systemctl enable`/`disable` and copied unit files
#     work immediately but evaporate on reboot unless also written through
#     `overlayroot-chroot`. This script does both.
#   - the switch-the-second-bridge trap (DEPLOY.md 4.1): a bridge that is
#     disabled in config.json but still enabled in systemd is left retrying
#     forever (StartLimitIntervalSec=0). This script reconciles every bridge
#     name found in config.json against systemd state, not just the enabled
#     ones.
#
# Two phases, split at a confirmation prompt:
#   Phase 1 (safe, repeatable): clone, build venv, copy forward site config,
#     validate it with the app's own pydantic models -- no source connection,
#     nothing touched outside the new version directory.
#   Phase 2 (irreversible, only after typing "yes"): backup, install the
#     systemd unit, swap the symlink, restart services, reconcile systemd
#     state with config.json, persist through overlayroot if present.
#
# Usage:
#   sudo scripts/deploy.sh [--ref <branch|tag|commit>] [--remote-url <url>]
#   sudo scripts/deploy.sh rollback <version-dir-under-APP_DIR>
#
# Environment overrides (defaults match docs/DEPLOY.md 1):
#   APP_DIR             default /persistence/app
#   LIVE_LINK           default /persistence/app/current_modbus_converter
#   SERVICE_USER        default ems_admin
#   BACKUP_APP_DIR      default /persistence/backup/app
#   BACKUP_SYSTEMD_DIR  default /persistence/backup/systemd

set -euo pipefail

APP_DIR="${APP_DIR:-/persistence/app}"
LIVE_LINK="${LIVE_LINK:-/persistence/app/current_modbus_converter}"
SERVICE_USER="${SERVICE_USER:-ems_admin}"
UNIT_NAME="nd45-dtsu666@.service"
BACKUP_APP_DIR="${BACKUP_APP_DIR:-/persistence/backup/app}"
BACKUP_SYSTEMD_DIR="${BACKUP_SYSTEMD_DIR:-/persistence/backup/systemd}"

log()  { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

require_root() {
    [ "$(id -u)" -eq 0 ] || die "must run as root (sudo $0 ...)"
}

# --- shared config-reading helper -------------------------------------------
# One Python one-liner, reused by both phases and by rollback, so "how do I
# read bridges[] out of config.json" exists in exactly one place. Emits
# "<name> <enabled-as-0-or-1>" one bridge per line -- deliberately every bridge
# NAMED in the file, not just AppConfig.bridge_specs (which drops disabled
# ones entirely). The systemd-reconciliation step needs the disabled names
# too, or it can never notice one is still live in systemd (DEPLOY.md 4.1).
bridge_list() {
    local venv_python="$1" config_dir="$2"
    (
        cd "$config_dir"
        "$venv_python" - <<'PY'
from nd45_dtsu666.config import load_config
config = load_config("config/config.json")
legacy = config.legacy_bridge
if legacy is not None:
    print(legacy.name, "1")  # the legacy single-bridge shape has no enabled flag
for spec in config.bridges:
    print(spec.name, "1" if spec.enabled else "0")
PY
    )
}

validate_config() {
    local venv_python="$1" config_dir="$2"
    (
        cd "$config_dir"
        "$venv_python" - <<'PY'
from nd45_dtsu666.config import load_config, load_registers, load_mqtt_config
load_config("config/config.json")
load_registers("config/registers.json")
load_mqtt_config("config/mqtt.json")  # None (missing file) is fine; malformed still raises
print("config OK")
PY
    )
}

# --- rollback ----------------------------------------------------------------
cmd_rollback() {
    require_root
    local target="${1:-}"
    if [ -z "$target" ] || [ ! -d "$target" ]; then
        echo "Usage: $0 rollback <version-dir>" >&2
        echo "Available version directories under $APP_DIR:" >&2
        ls -1 "$APP_DIR" >&2 2>/dev/null || true
        exit 1
    fi
    [ -x "$target/.venv/bin/python" ] || die "$target has no .venv/bin/python -- not a built checkout"

    log "Rolling back: $LIVE_LINK -> $target"
    ln -sfn "$target" "$LIVE_LINK"

    while read -r name enabled; do
        [ "$enabled" = "1" ] || continue
        log "restarting nd45-dtsu666@$name"
        systemctl restart "nd45-dtsu666@$name"
    done < <(bridge_list "$target/.venv/bin/python" "$target")

    log "Rollback complete. Check: systemctl status nd45-dtsu666@<bridge>"
}

# --- deploy (install or upgrade, auto-detected) ------------------------------
cmd_deploy() {
    require_root

    local ref="" remote_url=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --ref) ref="$2"; shift 2 ;;
            --remote-url) remote_url="$2"; shift 2 ;;
            *) die "unknown argument: $1" ;;
        esac
    done

    mkdir -p "$APP_DIR"

    # Where this script itself lives is the checkout to read the remote from,
    # so no URL is ever hardcoded -- matches docs/DEPLOY.md, which never
    # hardcodes one either.
    local self_dir repo_root
    self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    repo_root="$(cd "$self_dir/.." && pwd)"
    if [ -z "$remote_url" ]; then
        remote_url="$(git -C "$repo_root" remote get-url origin)" \
            || die "could not determine remote URL; pass --remote-url"
    fi

    local is_first_install=1 old_target=""
    if [ -L "$LIVE_LINK" ] || [ -e "$LIVE_LINK" ]; then
        is_first_install=0
        old_target="$(readlink -f "$LIVE_LINK")"
        [ -d "$old_target" ] || die "$LIVE_LINK resolves to $old_target, which does not exist"
    fi

    local repo_base timestamp new_dir
    repo_base="$(basename "$remote_url" .git)"
    timestamp="$(date -u '+%Y%m%d-%H%M%S')"
    new_dir="$APP_DIR/${repo_base}_${timestamp}"

    log "Cloning $remote_url -> $new_dir"
    git clone "$remote_url" "$new_dir"
    if [ -n "$ref" ]; then
        git -C "$new_dir" checkout "$ref"
    fi
    chown -R "$SERVICE_USER:$SERVICE_USER" "$new_dir"

    local pyver
    pyver="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
        || die "python3 is $pyver; this app needs >=3.10 (see pyproject.toml)"

    log "Building venv as $SERVICE_USER (python $pyver)"
    sudo -u "$SERVICE_USER" bash -c "
        set -e
        cd '$new_dir'
        python3 -m venv .venv
        .venv/bin/pip install -e '.[dev]'
    "

    if [ "$is_first_install" -eq 0 ]; then
        log "Copying site config forward from $old_target"
        cp "$old_target/config/config.json" "$new_dir/config/"
        cp "$old_target/config/registers.json" "$new_dir/config/"
        cp "$old_target/config/mqtt.json" "$new_dir/config/" 2>/dev/null || true
    else
        log "First install: no previous config to copy forward."
        log "  $new_dir/config/config.json is the example shipped in the repo."
        log "  Edit it for this site (source host/port, RS-485 port, dtsu.slave_id,"
        log "  dtsu.identity.ir_at) before continuing -- see docs/DEPLOY.md section 3.1."
    fi

    log "Validating config (no source connection, no hardware touched)"
    validate_config "$new_dir/.venv/bin/python" "$new_dir" \
        || die "config validation failed -- fix config/*.json in $new_dir and re-run"

    local new_commit
    # -c safe.directory: $new_dir is now owned by $SERVICE_USER (chowned above for
    # the venv build), but this git call runs as root -- without this, git's
    # dubious-ownership check refuses to read it.
    new_commit="$(git -c safe.directory="$new_dir" -C "$new_dir" rev-parse --short HEAD)"

    local unit_changed=0
    if [ -f "/etc/systemd/system/$UNIT_NAME" ]; then
        cmp -s "$new_dir/systemd/$UNIT_NAME" "/etc/systemd/system/$UNIT_NAME" || unit_changed=1
    else
        unit_changed=1
    fi

    echo
    echo "================ deploy summary ================"
    if [ "$is_first_install" -eq 1 ]; then
        echo "  mode:          first install"
    else
        echo "  mode:          upgrade"
        echo "  old target:    $old_target"
    fi
    echo "  new target:    $new_dir"
    echo "  commit:        $new_commit"
    echo "  systemd unit:  $([ "$unit_changed" -eq 1 ] && echo "will be (re)installed" || echo "unchanged")"
    echo "  bridges:"
    while read -r name enabled; do
        if [ "$enabled" = "1" ]; then
            echo "    - $name (enabled -> will be enabled + restarted)"
        else
            echo "    - $name (disabled -> will be disabled if currently live in systemd)"
        fi
    done < <(bridge_list "$new_dir/.venv/bin/python" "$new_dir")
    echo
    echo "Before continuing, verify each enabled source in another shell (docs/DEPLOY.md 3.2):"
    while read -r name enabled; do
        [ "$enabled" = "1" ] || continue
        echo "    $new_dir/.venv/bin/python -m nd45_dtsu666 --config $new_dir/config/config.json \\"
        echo "        --registers $new_dir/config/registers.json --bridge $name diag"
    done < <(bridge_list "$new_dir/.venv/bin/python" "$new_dir")
    if [ "$is_first_install" -eq 0 ]; then
        echo
        echo "  backup will be written to: $BACKUP_APP_DIR/$(basename "$old_target")-${timestamp}.tar.gz"
    fi
    echo "=================================================="
    echo

    read -r -p "Type 'yes' to install systemd units, switch the symlink, and restart services: " confirm
    if [ "$confirm" != "yes" ]; then
        log "Aborted. Nothing installed, symlink untouched, systemd untouched."
        log "The new checkout is left at $new_dir for inspection or re-run."
        exit 1
    fi

    # --- phase 2: irreversible from here -----------------------------------

    if [ "$is_first_install" -eq 0 ]; then
        mkdir -p "$BACKUP_APP_DIR"
        local backup_tar="$BACKUP_APP_DIR/$(basename "$old_target")-${timestamp}.tar.gz"
        log "Backing up $old_target -> $backup_tar"
        tar --exclude=.venv --exclude=.git -czf "$backup_tar" \
            -C "$APP_DIR" "$(basename "$old_target")"
    fi

    if [ "$unit_changed" -eq 1 ]; then
        if [ -f "/etc/systemd/system/$UNIT_NAME" ]; then
            mkdir -p "$BACKUP_SYSTEMD_DIR"
            cp "/etc/systemd/system/$UNIT_NAME" \
               "$BACKUP_SYSTEMD_DIR/${UNIT_NAME}.${timestamp}"
        fi
        log "Installing $UNIT_NAME"
        cp "$new_dir/systemd/$UNIT_NAME" "/etc/systemd/system/$UNIT_NAME"
        chmod 644 "/etc/systemd/system/$UNIT_NAME"
        systemctl daemon-reload
    fi

    log "Switching symlink: $LIVE_LINK -> $new_dir"
    ln -sfn "$new_dir" "$LIVE_LINK"

    local enabled_bridges=() disabled_bridges=()
    while read -r name enabled; do
        if [ "$enabled" = "1" ]; then
            enabled_bridges+=("$name")
            log "enabling + restarting nd45-dtsu666@$name"
            systemctl enable "nd45-dtsu666@$name"
            systemctl restart "nd45-dtsu666@$name"
        else
            disabled_bridges+=("$name")
            if systemctl is-enabled --quiet "nd45-dtsu666@$name" 2>/dev/null; then
                log "disabling nd45-dtsu666@$name (disabled in config.json, still live in systemd)"
                systemctl disable --now "nd45-dtsu666@$name"
            fi
        fi
    done < <(bridge_list "$new_dir/.venv/bin/python" "$new_dir")

    # Everything above already took effect on the running system. On an
    # overlayroot host it also needs writing to the real disk, or it reverts
    # on next reboot (docs/DEPLOY.md 0.4) -- /persistence is not mounted
    # inside overlayroot-chroot, so the unit content goes in via heredoc.
    if mount | grep -q overlayroot; then
        log "overlayroot detected: persisting systemd state to disk via overlayroot-chroot"
        local chroot_script
        chroot_script="$(mktemp)"
        {
            echo "set -e"
            echo "cat > /etc/systemd/system/$UNIT_NAME <<'DEPLOY_UNIT_EOF'"
            cat "$new_dir/systemd/$UNIT_NAME"
            echo "DEPLOY_UNIT_EOF"
            echo "chmod 644 /etc/systemd/system/$UNIT_NAME"
            echo "W=/etc/systemd/system/multi-user.target.wants"
            for name in "${enabled_bridges[@]}"; do
                echo "ln -sfn /etc/systemd/system/$UNIT_NAME \$W/nd45-dtsu666@${name}.service"
            done
            for name in "${disabled_bridges[@]}"; do
                echo "rm -f \$W/nd45-dtsu666@${name}.service"
            done
        } > "$chroot_script"
        overlayroot-chroot sh -s < "$chroot_script" \
            || log "WARNING: overlayroot-chroot persistence failed -- services are running now" \
                    "but will NOT survive a reboot. Re-run the chroot step manually (see" \
                    "docs/DEPLOY.md 0.4) before rebooting this host."
        rm -f "$chroot_script"

        local live_md5 disk_md5
        live_md5="$(md5sum "/etc/systemd/system/$UNIT_NAME" 2>/dev/null | awk '{print $1}')"
        disk_md5="$(md5sum "/media/root-ro/etc/systemd/system/$UNIT_NAME" 2>/dev/null | awk '{print $1}')"
        if [ -n "$disk_md5" ] && [ "$live_md5" = "$disk_md5" ]; then
            log "verified: unit file matches on disk and live"
        else
            log "WARNING: unit file on disk does not match live copy -- will not survive reboot as expected."
            log "  live:  $live_md5  ($([ -f "/etc/systemd/system/$UNIT_NAME" ] && echo present || echo missing))"
            log "  disk:  $disk_md5  ($([ -f "/media/root-ro/etc/systemd/system/$UNIT_NAME" ] && echo present || echo missing))"
        fi
    fi

    echo
    echo "================ verification ================"
    for name in "${enabled_bridges[@]}"; do
        systemctl status "nd45-dtsu666@$name" --no-pager --lines=3 || true
        local limit
        limit="$(systemctl show -p StartLimitIntervalUSec "nd45-dtsu666@$name" 2>/dev/null | cut -d= -f2)"
        if [ "$limit" != "0" ]; then
            log "WARNING: nd45-dtsu666@$name StartLimitIntervalUSec=$limit (expected 0) -- see docs/DEPLOY.md 0.3"
        fi
    done
    ps aux | grep -F nd45_dtsu666 | grep -v grep || true
    echo
    echo "Once Sigenergy has had a chance to poll, confirm activity per bridge:"
    while read -r name enabled; do
        [ "$enabled" = "1" ] || continue
        local port
        port="$("$new_dir/.venv/bin/python" - "$new_dir" "$name" <<'PY'
import sys
from nd45_dtsu666.config import load_config
config = load_config(f"{sys.argv[1]}/config/config.json")
for spec in config.bridge_specs:
    if spec.name == sys.argv[2]:
        print(config.metrics_port_for(spec.name))
        break
PY
)"
        echo "    curl -s localhost:${port}/metrics | grep -E 'dtsu_requests_total|dtsu_bus_frames'"
    done < <(bridge_list "$new_dir/.venv/bin/python" "$new_dir")
    echo
    if [ "$is_first_install" -eq 0 ]; then
        echo "Rollback if needed:"
        echo "    sudo $0 rollback $old_target"
        echo "Backup of the previous version: $BACKUP_APP_DIR/$(basename "$old_target")-${timestamp}.tar.gz"
    fi
    echo "=================================================="
}

case "${1:-}" in
    rollback) shift; cmd_rollback "$@" ;;
    *) cmd_deploy "$@" ;;
esac
