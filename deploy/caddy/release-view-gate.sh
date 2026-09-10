#!/bin/sh
set -eu

view=/srv/eurith/releases/android/sha256
regular="$view/.eurith-release-view-gate-regular"
external="$view/.eurith-release-view-gate-external"

fail() {
    printf '%s\n' 'release_view_gate=failed' >&2
    exit 1
}

mount_options="$({
    awk -v target="$view" '
        $5 == target { print $6; found = 1; exit }
        END { if (!found) exit 1 }
    ' /proc/self/mountinfo
} 2>/dev/null)" || fail

case ",$mount_options," in
    *,ro,*) ;;
    *) fail ;;
esac
case ",$mount_options," in
    *,nosymfollow,*) ;;
    *) fail ;;
esac

[ -f "$regular" ] || fail
head -c 1 "$regular" >/dev/null 2>&1 || fail
[ -L "$external" ] || fail
[ "$(readlink "$external" 2>/dev/null)" = "/etc/passwd" ] || fail
if head -c 1 "$external" >/dev/null 2>&1; then
    fail
fi

printf '%s\n' 'release_view_gate=passed'
