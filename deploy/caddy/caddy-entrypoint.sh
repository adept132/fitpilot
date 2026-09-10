#!/bin/sh
set -eu

fail() {
    printf '%s\n' 'release_view_gate=failed' >&2
    exit 1
}

if [ "$#" -eq 0 ]; then
    mountinfo=/proc/self/mountinfo
    final_mount=/srv/eurith/releases/android/sha256
    probe_mount=/run/eurith-release-view-probe
    final_mount_key=$final_mount
    probe_mount_key=$probe_mount
    head_command=head
    readlink_command=readlink
    caddy_command=caddy
elif [ "$#" -eq 2 ] && [ "$1" = "--test-root" ]; then
    test_root=$2
    mountinfo="$test_root/proc/self/mountinfo"
    final_mount="$test_root/srv/eurith/releases/android/sha256"
    probe_mount="$test_root/run/eurith-release-view-probe"
    final_mount_key=FINAL_MOUNT
    probe_mount_key=PROBE_MOUNT
    head_command="$test_root/test-bin/head"
    readlink_command="$test_root/test-bin/readlink"
    caddy_command="$test_root/test-bin/caddy"
else
    fail
fi

mount_options() {
    /usr/bin/awk -v target="$1" '
        $5 == target { print $6; found = 1; exit }
        END { if (!found) exit 1 }
    ' "$mountinfo" 2>/dev/null
}

require_protected_mount() {
    options="$(mount_options "$1")" || fail
    case ",$options," in
        *,ro,*) ;;
        *) fail ;;
    esac
    case ",$options," in
        *,nosymfollow,*) ;;
        *) fail ;;
    esac
}

require_protected_mount "$final_mount_key"
require_protected_mount "$probe_mount_key"

regular="$probe_mount/regular"
external="$probe_mount/external"
"$head_command" -c 1 "$regular" >/dev/null 2>&1 || fail
[ "$("$readlink_command" "$external" 2>/dev/null)" = "/etc/passwd" ] || fail
if "$head_command" -c 1 "$external" >/dev/null 2>&1; then
    fail
fi

printf '%s\n' 'release_view_gate=passed'
exec "$caddy_command" run --config /etc/caddy/Caddyfile --adapter caddyfile
