#!/usr/bin/env bash
# Debian 12+ / Ubuntu 24.04+, systemd. All orders are simulated.
set -euo pipefail
umask 077
# BEGIN CI RELEASE HELPERS -- keep embedded copies identical to deploy/release-common.sh
# Only discovery uses latest; the archive is always fetched from its immutable commit tag.
ci_release_resolve() {
  local repository=$1 workspace=$2 manifest values
  [[ "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || return 1
  CI_RELEASE_REPOSITORY=$repository
  CI_RELEASE_WORK=$workspace
  mkdir -p -- "$workspace" || return 1
  manifest="$workspace/release-manifest.json"
  if ! curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    --retry 3 --connect-timeout 15 --max-time 90 --max-filesize 1048576 \
    "https://github.com/$repository/releases/latest/download/release-manifest.json" -o "$manifest"; then
    printf '[CI] %s 暂无可用部署清单或下载失败；现有服务保持原样。\n' "$repository" >&2
    return 1
  fi
  values=$(python3 - "$manifest" "$repository" "$(uname -m)" <<'CI_MANIFEST_PY'
import json, re, sys
from pathlib import Path
def require(condition, message):
    if not condition:
        raise ValueError(message)
try:
    data = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    architecture = {'x86_64': 'linux-x64', 'aarch64': 'linux-arm64', 'arm64': 'linux-arm64'}[sys.argv[3]]
    require(type(data['schema']) is int and data['schema'] == 1 and data['repository'] == sys.argv[2], 'repository/schema mismatch')
    commit = data['commit']
    require(re.fullmatch(r'[a-f0-9]{40}', commit), 'invalid commit')
    require(data['tag'] == 'deploy-' + commit, 'tag/commit mismatch')
    item = data['artifacts'][architecture]
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*\.tar\.gz', item['file']), 'invalid archive name')
    for key in ('sha256', 'application_key'):
        require(re.fullmatch(r'[a-f0-9]{64}', item[key]), 'invalid ' + key)
    node = data.get('node_version', '')
    require(not node or re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', node), 'invalid Node version')
    print('\n'.join([commit, data['tag'], item['file'], item['sha256'], item['application_key'], node]))
except (OSError, ValueError, KeyError, TypeError, AssertionError) as error:
    print('[CI] Invalid deployment manifest: ' + str(error), file=sys.stderr)
    sys.exit(1)
CI_MANIFEST_PY
  ) || return 1
  local -a fields
  mapfile -t fields <<< "$values"
  CI_RELEASE_COMMIT=${fields[0]}
  CI_RELEASE_TAG=${fields[1]}
  CI_RELEASE_FILE=${fields[2]}
  CI_RELEASE_SHA256=${fields[3]}
  CI_RELEASE_APPLICATION_KEY=${fields[4]}
  CI_RELEASE_NODE_VERSION=${fields[5]:-}
  printf '[CI] %s 最新可用部署包：%s。\n' "$repository" "${CI_RELEASE_COMMIT:0:12}"
}

ci_release_extract() {
  local cache=$1 destination=$2 archive temporary actual
  [[ ! -L "$cache" && ( ! -e "$cache" || -d "$cache" ) ]] || { printf '[CI] Invalid archive cache.\n' >&2; return 1; }
  mkdir -p -- "$cache" || return 1
  [[ $(stat -c %u "$cache") == "$EUID" ]] || { printf '[CI] Archive cache has an unexpected owner.\n' >&2; return 1; }
  archive="$cache/$CI_RELEASE_SHA256.tar.gz"
  [[ ! -L "$archive" && ( ! -e "$archive" || -f "$archive" ) ]] || return 1
  actual=''
  if [[ -f "$archive" ]]; then actual=$(sha256sum "$archive" | cut -d ' ' -f1) || return 1; fi
  if [[ "$actual" != "$CI_RELEASE_SHA256" ]]; then
    temporary=$(mktemp "$cache/.download.XXXXXXXX") || return 1
    if ! curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
      --retry 3 --connect-timeout 15 --max-time 600 --max-filesize 2147483648 \
      "https://github.com/$CI_RELEASE_REPOSITORY/releases/download/$CI_RELEASE_TAG/$CI_RELEASE_FILE" -o "$temporary"; then
      rm -f -- "$temporary"
      return 1
    fi
    actual=$(sha256sum "$temporary" | cut -d ' ' -f1) || { rm -f -- "$temporary"; return 1; }
    if [[ "$actual" != "$CI_RELEASE_SHA256" ]]; then
      printf '[CI] 部署包校验失败；现有服务保持原样。\n' >&2
      rm -f -- "$temporary"
      return 1
    fi
    chmod 0644 "$temporary" || { rm -f -- "$temporary"; return 1; }
    mv -f -- "$temporary" "$archive" || return 1
  else
    printf '[CI] 部署包已缓存且校验通过，跳过下载。\n'
  fi
  python3 - "$archive" "$destination" "$CI_RELEASE_COMMIT" "$CI_RELEASE_APPLICATION_KEY" <<'CI_EXTRACT_PY'
import os, shutil, sys, tarfile
from pathlib import Path, PurePosixPath
def require(condition, message):
    if not condition:
        raise ValueError(message)
try:
    destination = Path(sys.argv[2])
    require(not destination.is_symlink(), 'destination cannot be a symlink')
    destination.mkdir(parents=True, exist_ok=True)
    require(not any(destination.iterdir()), 'destination must be empty')
    with tarfile.open(sys.argv[1], 'r:gz') as archive:
        members = []
        names, total = set(), 0
        for member in archive:
            require(len(members) < 200000, 'too many archive entries')
            path = PurePosixPath(member.name)
            require(not path.is_absolute() and '..' not in path.parts and '\\' not in member.name and ':' not in member.name, 'unsafe archive path')
            require(member.isdir() or member.isfile(), 'archive links and special files are forbidden')
            name = str(path)
            require(name not in names, 'duplicate archive entry')
            names.add(name)
            total += member.size
            require(0 <= member.size and total <= 2147483648, 'archive too large')
            members.append(member)
        for member in members:
            path = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
                path.chmod(0o755)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, path.open('xb') as target:
                    shutil.copyfileobj(source, target)
                path.chmod(0o755 if member.mode & 0o111 else 0o644)
    destination.chmod(0o755)
    for directory in destination.rglob('*'):
        if directory.is_dir():
            directory.chmod(0o755)
    require((destination / '.release-commit').read_text().strip() == sys.argv[3], 'archive commit mismatch')
    require((destination / '.release-application-key').read_text().strip() == sys.argv[4], 'archive application key mismatch')
except (OSError, ValueError, EOFError, tarfile.TarError, AssertionError) as error:
    print('[CI] Invalid deployment archive: ' + str(error), file=sys.stderr)
    sys.exit(1)
CI_EXTRACT_PY
}
# END CI RELEASE HELPERS

deploy_mode=${PROJECT_DEPLOY_MODE:-source}
case "$deploy_mode" in source|ci) ;; *) printf 'PROJECT_DEPLOY_MODE must be source or ci.\n' >&2; exit 1 ;; esac
requested_mode=
requested_cl_bz=0
cleanup_only=0
usage() {
  cat <<'HELP'
Usage: install.sh [--compare|--inventory|--qqq-hedge|--single] [--with-cl-bz]
       install.sh [--cleanup|--help]
Debian 12+ / Ubuntu 24.04+, with systemd and Python 3.11+.
New installs run Lighter QQQ / Variational US100 paper scalping by default.
Three independent take-profit settings: 0.05% / 0.1% / 0.2%.
Includes a localhost dashboard on port 9876, accessed over SSH.
Repeating the command upgrades code and preserves mode, settings and data.
Historical modes share the main strategy service. QQQ can also run a separate CL scalper with a BZ short hedge.
Deployments use a cached offline preflight; full regression tests run in CI.
Unchanged dependencies, validated code and running services are reused.
  --compare  Explicitly select the historical CL/BZ three-grid comparison.
  --inventory  Explicitly select the historical CL/BZ inventory comparison.
  --qqq-hedge  Start the three-scenario Lighter QQQ / Variational US100 paper comparison.
  --with-cl-bz  Opt in to one CL paper scalper with an equal-barrel BZ short hedge alongside QQQ.
                Saved across upgrades; paused in historical modes, with settings and data retained.
  --single   Explicitly select the historical CL/BZ single grid using config.json.
  --cleanup  Reclaim obsolete deployments without downloading or restarting.
  --help     Show this help without installing anything.
Deployment never asks for vr-token; saved sessions are preserved.
QQQ users can add or refresh it from the dashboard's Update Var token button.
Authenticated paper pricing waits for a valid session; Lighter data stays public.
No wallet key is needed.
HELP
}
for argument in "$@"; do
  case $argument in
    --compare|--inventory|--qqq-hedge|--single)
      [[ -z $requested_mode ]] || { usage >&2; exit 1; }
      requested_mode=${argument#--}
      [[ $requested_mode != single ]] || requested_mode=run ;;
    --with-cl-bz) requested_cl_bz=1 ;;
    --cleanup) [[ $# == 1 ]] || { usage >&2; exit 1; }; cleanup_only=1 ;;
    --help|-h) [[ $# == 1 ]] || { usage >&2; exit 1; }; usage; exit 0 ;;
    *) usage >&2; exit 1 ;;
  esac
done

# Kept inside the downloaded installer so cleanup runs before fetching any code.
# This helper uses only Python's standard library and never imports application code.
storage() {
  python3 - "$app" "$conf" "$state" "$installer_source" "$@" <<'STORAGE_PY'
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib


class DeploymentStorage:
    def __init__(self, app, conf, state, installer_source=''):
        self.app, self.conf, self.state = map(Path, (app, conf, state))
        self.releases = self.app / 'releases'
        self.source = self.app / 'source'
        self.current = (self.app / 'current').resolve()
        self.roots = []
        self.running = []
        for path in (self.conf, self.state, self.source):
            self.protect(path)
        if installer_source:
            self.protect(installer_source)
        # Preserve any configured path, including symlinks to a release. Config is
        # parsed as data; neither credentials nor configuration values are printed.
        for name in ('config.json', 'inventory-base.json', 'experiments.json', 'inventory.json', 'qqq-hedge.json', 'cl-bz-scalper.json'):
            path = self.conf / name
            self.protect(path)
            if not path.exists():
                continue
            spec = json.loads(path.read_text())
            if not isinstance(spec, dict):
                raise ValueError('Expected configuration object')
            for key in ('session_file', 'state_file', 'output_dir', 'previous_output_dir', 'base_config'):
                value = spec.get(key)
                if isinstance(value, str) and value:
                    configured = Path(value)
                    if not configured.is_absolute():
                        # Application paths are relative to the configuration file.
                        # Also retain the historical current-relative protection.
                        self.protect(path.parent / configured)
                        configured = self.current / configured
                    self.protect(configured)

    def protect(self, path):
        path = Path(path).absolute()
        self.roots.extend((path, path.resolve()))

    def inspect_services(self):
        for service in ('variational-grid.service', 'variational-grid-web.service', 'variational-grid-cl-bz.service'):
            result = subprocess.run(['systemctl', 'show', '--property=MainPID',
                                     '--property=LoadState', service], text=True, capture_output=True)
            props = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
            if props.get('LoadState') == 'not-found':
                continue
            if result.returncode or props.get('LoadState') != 'loaded' or not props.get('MainPID', '').isdigit():
                raise RuntimeError('Cannot inspect service process; preserving all deployments.')
            pid = int(props['MainPID'])
            if pid:
                try:
                    self.running.append((Path('/proc') / str(pid) / 'cwd').resolve(strict=True))
                except OSError as error:
                    raise RuntimeError('Cannot inspect running directory; preserving all deployments.') from error

    @staticmethod
    def overlaps(left, right):
        return left == right or left in right.parents or right in left.parents

    def protected(self, path):
        path = Path(path)
        return (any(self.overlaps(path, root) for root in self.roots)
                or any(path == item or path in item.parents for item in
                       (self.current, Path.cwd().resolve(), *self.running)))

    def managed(self, path):
        path = Path(path)
        if path.is_symlink() or not path.is_dir() or path.resolve() != path.absolute():
            return False
        if path.parent == self.app and re.fullmatch(r'\.deploy\.[A-Za-z0-9]{6}', path.name):
            return self.marker(path, '.install-owned')
        if path.parent != self.releases or self.releases.is_symlink():
            return False
        if re.fullmatch(r'\.staging\.[A-Za-z0-9]{6}', path.name):
            return self.marker(path, '.install-owned')
        if not re.fullmatch(r'[0-9a-f]{40}(?:-ci)?', path.name):
            return False
        if self.marker(path, '.install-owned'):
            return True
        # Compatibility with releases made by the original installer. Unknown
        # directories, even those named like hashes, must not be removed.
        try:
            return (not (path / 'pyproject.toml').is_symlink()
                    and tomllib.loads((path / 'pyproject.toml').read_text())['project']['name'] in {'variational-cl-bz-grid', 'variational-grid'}
                    and (path / 'install.sh').is_file()
                    and (path / 'variational_grid/__init__.py').is_file())
        except (OSError, ValueError, KeyError):
            return False

    @staticmethod
    def marker(path, name):
        marker = path / name
        return marker.is_file() and not marker.is_symlink()

    @staticmethod
    def contains_mount(path):
        # Do not descend into a mounted volume, including a same-device bind
        # mount. os.path.ismount alone does not identify all Linux bind mounts.
        if sys.platform == 'linux':
            mounts = Path('/proc/self/mountinfo').read_text().splitlines()
            for row in mounts:
                mount = Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), row.split()[4]))
                if mount == path or path in mount.parents:
                    return True
        for root, directories, _ in os.walk(path, followlinks=False):
            if os.path.ismount(root) or any(os.path.ismount(Path(root) / item) for item in directories):
                return True
        return os.path.ismount(path)

    def remove(self, path):
        if not self.managed(path) or self.protected(path) or self.contains_mount(path):
            return False
        shutil.rmtree(path)
        return True

    def validation_key(self, release):
        marker = release / '.install-validation'
        if self.marker(release, '.install-validation'):
            key = marker.read_text().strip()
            if re.fullmatch(r'[0-9a-f]{64}', key):
                return key
        # Old installers stored only global stamps. Reconstruct the key for each
        # retained legacy release so a no-op upgrade still reuses validation.
        result = subprocess.run(['git', '-C', str(self.source), 'ls-tree', '-r', release.name, '--',
                                 'variational_grid', 'tests', 'install.sh', 'config.example.json',
                                 'experiments.example.json', 'inventory.example.json', 'qqq-hedge.example.json', 'cl-bz-scalper.example.json',
                                 'pyproject.toml'], capture_output=True)
        if result.returncode:
            return None
        version = subprocess.check_output([sys.executable, '--version'])
        return hashlib.sha256(version + result.stdout).hexdigest()

    def prune_validation(self):
        cache = self.app / 'validated'
        if (cache.is_symlink() or not cache.is_dir() or cache.resolve() != cache.absolute()
                or self.protected(cache) or self.contains_mount(cache)):
            return
        keys = set()
        for release in self.releases.iterdir():
            if self.managed(release) and re.fullmatch(r'[0-9a-f]{40}(?:-ci)?', release.name):
                key = self.validation_key(release)
                if key is None:
                    return  # Cannot establish references: retain the small stamps.
                keys.add(key)
        for stamp in cache.iterdir():
            if (stamp.is_file() and not stamp.is_symlink()
                    and re.fullmatch(r'[0-9a-f]{64}', stamp.name) and stamp.name not in keys
                    and not self.protected(stamp) and not os.path.ismount(stamp)):
                stamp.unlink()

    def prune(self, preferred=''):
        if not self.releases.is_dir() or self.releases.is_symlink():
            return
        candidates = [path for path in [*self.releases.iterdir(), *self.app.glob('.deploy.*')]
                      if self.managed(path)]
        ready = [path for path in candidates if path != self.current and
                 (self.marker(path, '.install-ready') or
                  (re.fullmatch(r'[0-9a-f]{40}', path.name) and not self.marker(path, '.install-owned')))]
        # Legacy releases predate success markers; conservatively retain the
        # newest validated archive as well as every actual running directory.
        backup = Path(preferred) if preferred and Path(preferred) in ready else None
        if backup is None and ready:
            backup = max(ready, key=lambda p: (p / '.install-ready').stat().st_mtime_ns
                         if self.marker(p, '.install-ready') else p.stat().st_mtime_ns)
        removed = sum(self.remove(path) for path in candidates if path != backup)
        self.prune_validation()
        print(f'Storage: reclaimed {removed} obsolete deployment(s); current, rollback and protected paths retained.')


def main():
    manager = DeploymentStorage(*sys.argv[1:5])
    manager.inspect_services()
    action, *args = sys.argv[5:]
    if action == 'prune':
        manager.prune(*args)
    elif action == 'abandon':
        for value in args:
            if value:
                manager.remove(Path(value))
        if manager.releases.is_dir():
            manager.prune_validation()


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        # Do not expose preserved configuration values in parser error messages.
        print(f'Deployment storage inspection failed ({type(error).__name__}); no unsafe cleanup attempted.', file=sys.stderr)
        sys.exit(1)
STORAGE_PY
}

require_space() {
  local path=$1 required_kb=$2 required_inodes=$3 available inodes
  while [[ ! -e $path && $path != / ]]; do path=${path%/*}; [[ -n $path ]] || path=/; done
  available=$(df -Pk -- "$path" | awk 'END {print $4}')
  inodes=$(df -Pi -- "$path" | awk 'END {print $4}')
  [[ $available =~ ^[0-9]+$ && ( $inodes =~ ^[0-9]+$ || $inodes == - ) ]] || { echo 'Cannot determine available storage.' >&2; exit 1; }
  (( available >= required_kb )) || { printf 'Insufficient disk space: %s has %s MiB; this stage needs %s MiB. Services have not been switched.\n' "$path" "$((available / 1024))" "$((required_kb / 1024))" >&2; exit 1; }
  [[ $inodes == - ]] || (( inodes >= required_inodes )) || { printf 'Insufficient inodes: %s has %s; this stage needs %s. Services have not been switched.\n' "$path" "$inodes" "$required_inodes" >&2; exit 1; }
}

if [[ ${EUID} -ne 0 ]]; then
  echo 'Run with sudo bash (root is needed to install the service).' >&2
  exit 1
fi
if [[ $(uname -s) != Linux ]] || ! command -v systemctl >/dev/null; then
  echo 'This installer requires Linux with systemd.' >&2
  exit 1
fi
if ! command -v apt-get >/dev/null; then
  echo 'Supported: Debian 12+ / Ubuntu 24.04+ with apt.' >&2
  exit 1
fi

app=/opt/variational-grid
conf=/etc/variational-grid
state=/var/lib/variational-grid
repository=https://github.com/hxx344/variational-grid.git
legacy_repository=https://github.com/hxx344/variational-cl-bz-grid.git
account=variational-grid
installer_source=''
if [[ -n ${BASH_SOURCE[0]:-} && -f ${BASH_SOURCE[0]} ]]; then
  installer_source=$(readlink -f -- "${BASH_SOURCE[0]}")
fi
staging= deployment= new_release= old_current= ci_work=
activation_started=0
ci_changed_services=()
if (( cleanup_only )) && [[ ! -e $app ]]; then
  echo 'No deployment to clean.'; exit 0
fi
[[ ! -L $app && ! -L $app/releases && ! -L $app/validated && ! -L $app/install.lock ]] || { echo 'Deployment directories must not be symlinks.' >&2; exit 1; }
[[ ! -e $app/current || -L $app/current ]] || { echo 'current must be an installer-managed symlink.' >&2; exit 1; }
install -d -m 755 "$app"
exec 9>"$app/install.lock"
flock -n 9 || { echo 'Another deployment is running.' >&2; exit 1; }
old_current=$(readlink -f "$app/current" 2>/dev/null || true)
cleanup() {
  local status=$? candidate=''
  trap - EXIT INT TERM
  if [[ $status != 0 && $activation_started == 1 ]]; then rollback_ci || true; fi
  [[ $status == 0 ]] || candidate=$new_release
  if [[ -n $staging || -n $deployment || -n $candidate ]]; then
    # Refresh all process directories before deleting a failed candidate.
    # Failed inspection retains it; configuration and active code take priority.
    storage abandon "$staging" "$deployment" "$candidate" || true
  fi
  if [[ -n $ci_work && $ci_work == "$app/.ci-release."* && -d $ci_work && ! -L $ci_work ]]; then rm -rf -- "$ci_work"; fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if command -v python3 >/dev/null; then
  storage prune
elif (( cleanup_only )); then
  echo 'Cleanup requires the Python 3.11+ used by the existing installation.' >&2; exit 1
fi
if (( cleanup_only )); then
  storage_paths=("$app")
  for path in "$conf" "$state"; do [[ ! -e $path ]] || storage_paths+=("$path"); done
  df -h "${storage_paths[@]}"
  df -i "${storage_paths[@]}"
  echo 'Cleanup complete; configuration, data and services unchanged.'
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive
export PYTHONDONTWRITEBYTECODE=1
missing_packages=()
system_packages=(python3 ca-certificates)
if [[ $deploy_mode == source ]]; then system_packages+=(git); else system_packages+=(curl); fi
for package in "${system_packages[@]}"; do
  if [[ $(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true) != installed ]]; then
    missing_packages+=("$package")
  fi
done
if (( ${#missing_packages[@]} )); then
  require_space /var 524288 10000
  apt-get update -qq
  apt-get install -y -qq "${missing_packages[@]}"
else
  echo 'Dependencies present; skipping apt update/install.'
fi
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required (Debian 12+ / Ubuntu 24.04+)"'

mode=${requested_mode:-$(cat "$conf/mode" 2>/dev/null || echo qqq-hedge)}
[[ $mode == run || $mode == compare || $mode == inventory || $mode == qqq-hedge ]] || { echo 'Invalid saved service mode.' >&2; exit 1; }
cl_bz_enabled=$(cat "$conf/cl-bz-enabled" 2>/dev/null || echo 0)
[[ $cl_bz_enabled == 0 || $cl_bz_enabled == 1 ]] || { echo 'Invalid saved CL/BZ preference.' >&2; exit 1; }
if (( requested_cl_bz )); then cl_bz_enabled=1; fi
cl_bz_active=0
if [[ $mode == qqq-hedge && $cl_bz_enabled == 1 ]]; then cl_bz_active=1; fi
experiment_name=experiments.json
config_name=config.json
if [[ $mode == inventory ]]; then
  experiment_name=inventory.json
  config_name=inventory-base.json
elif [[ $mode == qqq-hedge ]]; then
  experiment_name=qqq-hedge.json
fi
case "$mode" in
  qqq-hedge) service_description='Lighter QQQ / Variational US100 paper scalper' ;;
  inventory) service_description='Variational CL/BZ paper inventory comparison' ;;
  compare) service_description='Variational CL/BZ paper grid comparison' ;;
  run) service_description='Variational CL/BZ paper single grid' ;;
esac

id "$account" >/dev/null 2>&1 || useradd --system --home-dir "$state" --shell /usr/sbin/nologin "$account"
install -d -m 755 "$app" "$app/releases" "$conf"
install -d -m 700 -o "$account" -g "$account" "$state"
require_space "$app" 8192 128
if [[ $deploy_mode == ci ]]; then
  ci_work=$(mktemp -d "$app/.ci-release.XXXXXXXX")
  ci_release_resolve hxx344/variational-grid "$ci_work"
  revision=$CI_RELEASE_COMMIT
elif [[ ! -d "$app/source/.git" ]]; then
  require_space "$app" 196608 8192
  git clone --depth 1 --branch main "$repository" "$app/source"
  revision=$(git -C "$app/source" rev-parse 'origin/main^{commit}')
else
  source_remote=$(git -C "$app/source" remote get-url origin)
  if [[ $source_remote == "$legacy_repository" ]]; then
    git -C "$app/source" remote set-url origin "$repository"
    echo 'Updated the renamed repository remote; existing releases and data are preserved.'
    source_remote=$repository
  fi
  [[ $source_remote == "$repository" ]] || { echo 'Unexpected existing source remote.' >&2; exit 1; }
  revision=$(git -C "$app/source" ls-remote --exit-code origin refs/heads/main | cut -f1)
  [[ $revision =~ ^[a-f0-9]{40}$ ]] || { echo 'Cannot determine the remote main revision.' >&2; exit 1; }
  if ! git -C "$app/source" cat-file -e "$revision^{commit}" 2>/dev/null; then
    require_space "$app" 131072 4096
    git -C "$app/source" fetch --depth 1 origin "$revision"
  else
    echo 'Requested Git objects cached; skipping fetch.'
  fi
fi
[[ $revision =~ ^[a-f0-9]{40}$ ]] || exit 1
release="$app/releases/$revision"
if [[ $deploy_mode == ci ]]; then
  release="$release-ci"
  if [[ -n $old_current && -f $old_current/.install-ready && -f $old_current/.release-inputs.json &&
        $(cat "$old_current/.release-application-key" 2>/dev/null || true) == "$CI_RELEASE_APPLICATION_KEY" ]]; then
    release=$old_current
    echo 'CI runtime inputs unchanged; reusing the checked release without downloading its archive.'
  fi
fi
# Version the quick check separately from the legacy full-test cache.
# Docs/tests-only revisions reuse it; failed checks never write a success stamp.
validation_key=$({
  printf '%s\n' 'quick-preflight-v1'
  python3 -c 'import sys, sqlite3; print(sys.version, sys.implementation.name, sys.implementation.cache_tag, sys.executable, sqlite3.sqlite_version)'
  if [[ $deploy_mode == ci ]]; then printf '%s\n' "ci-preflight-v1:$CI_RELEASE_APPLICATION_KEY"
  else git -C "$app/source" ls-tree -r "$revision" -- variational_grid deploy_check.py install.sh config.example.json experiments.example.json inventory.example.json qqq-hedge.example.json cl-bz-scalper.example.json pyproject.toml; fi
} | sha256sum | cut -d ' ' -f1)
install -d -m 755 "$app/validated"
validate_release() {
  if [[ $(cat "$app/validated/$validation_key" 2>/dev/null || true) == "$validation_key" ]]; then
    echo 'Matching release/runtime already checked; skipping quick preflight. Full tests run in CI.'
  else
    (cd "$1" && python3 -B deploy_check.py)
    printf '%s\n' "$validation_key" >"$app/validated/$validation_key"
  fi
  printf '%s\n' "$validation_key" >"$1/.install-validation"
}
if [[ ! -d "$release" ]]; then
  require_space "$app" 131072 4096
  staging=$(mktemp -d "$app/releases/.staging.XXXXXX")
  if [[ $deploy_mode == ci ]]; then
    if ! ci_release_extract "$app/ci-archives" "$staging"; then
      printf 'variational-grid\n' >"$staging/.install-owned"
      exit 1
    fi
  else git -C "$app/source" archive "$revision" | tar -x -C "$staging"; fi
  printf 'variational-grid\n' >"$staging/.install-owned"
  validate_release "$staging"
  chmod -R u=rwX,go=rX "$staging"
  mv -- "$staging" "$release"
  new_release=$release
  staging=
else
  if [[ $deploy_mode == ci ]]; then
    [[ -f $release/.release-inputs.json && $(cat "$release/.release-application-key" 2>/dev/null || true) == "$CI_RELEASE_APPLICATION_KEY" ]] || { echo 'Existing CI release is incomplete.' >&2; exit 1; }
  fi
  validate_release "$release"
fi
release_tree() {
  local kind=$1
  if [[ $deploy_mode == ci ]]; then
    python3 - "$release/.release-inputs.json" "$kind" <<'CI_TREE_PY'
import json, sys
from pathlib import Path
value = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))[sys.argv[2] + '_tree']
if not isinstance(value, str) or not value:
    raise ValueError('Missing runtime input tree')
sys.stdout.write(value)
CI_TREE_PY
  elif [[ $kind == engine ]]; then
    git -C "$app/source" ls-tree -r "$revision" -- variational_grid | sed '\|[[:space:]]variational_grid/web/|d; \|[[:space:]]variational_grid/dashboard.py$|d; \|[[:space:]]variational_grid/hub.py$|d'
  else git -C "$app/source" ls-tree -r "$revision" -- variational_grid; fi
}
require_space "$conf" 16384 512
require_space "$state" 16384 512
if [[ ! -f "$conf/config.json" ]]; then
  python3 - "$release/config.example.json" "$conf/config.json" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
data['session_file'] = '/var/lib/variational-grid/session.json'
data['state_file'] = '/var/lib/variational-grid/paper-unbounded-grid-100x.sqlite3'
Path(sys.argv[2]).write_text(json.dumps(data, indent=2) + '\n')
PY
  chmod 644 "$conf/config.json"
fi
if [[ $mode == compare && ! -f "$conf/experiments.json" ]]; then
  python3 - "$release/experiments.example.json" "$conf/experiments.json" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
data['base_config'] = '/etc/variational-grid/config.json'
data['output_dir'] = '/var/lib/variational-grid/comparison-pct-05-1-2-center3d-unbounded-grid-100x'
Path(sys.argv[2]).write_text(json.dumps(data, indent=2) + '\n')
PY
  chmod 644 "$conf/experiments.json"
fi
if [[ $mode == inventory && ! -f "$conf/inventory-base.json" ]]; then
  # Freeze the starting economics independently of later legacy-mode migrations.
  install -m 644 "$conf/config.json" "$conf/inventory-base.json"
fi
if [[ $mode == inventory && ! -f "$conf/inventory.json" ]]; then
  python3 - "$release/inventory.example.json" "$conf/inventory.json" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
data['base_config'] = '/etc/variational-grid/inventory-base.json'
data['output_dir'] = '/var/lib/variational-grid/inventory-pct-0-5-10-20-v1'
Path(sys.argv[2]).write_text(json.dumps(data, indent=2) + '\n')
PY
  chmod 644 "$conf/inventory.json"
fi
if [[ $mode == qqq-hedge && ! -f "$conf/qqq-hedge.json" ]]; then
  python3 - "$release/qqq-hedge.example.json" "$conf/qqq-hedge.json" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
data['base_config'] = '/etc/variational-grid/config.json'
data['output_dir'] = '/var/lib/variational-grid/qqq-hedge-scalper-v3'
Path(sys.argv[2]).write_text(json.dumps(data, indent=2) + '\n')
PY
  chmod 644 "$conf/qqq-hedge.json"
fi
if (( cl_bz_active )) && [[ ! -f "$conf/cl-bz-scalper.json" ]]; then
  python3 - "$release/cl-bz-scalper.example.json" "$conf/cl-bz-scalper.json" <<'PY'
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
data['base_config'] = '/etc/variational-grid/config.json'
data['output_dir'] = '/var/lib/variational-grid/cl-bz-scalper-v1'
Path(sys.argv[2]).write_text(json.dumps(data, indent=2) + '\n')
PY
  chmod 644 "$conf/cl-bz-scalper.json"
fi
# Validate preserved config before switching the running version.
validate_settings() {
  (cd "$release" && python3 - "$mode" "$cl_bz_active" <<'PY'
import json, sys
from dataclasses import replace
from pathlib import Path
from variational_grid.cli import configuration
from variational_grid.comparison import Experiment
base_path = Path('/etc/variational-grid') / ('inventory-base.json' if sys.argv[1] == 'inventory' else 'config.json')
config = configuration(base_path)
if sys.argv[1] in ('compare', 'inventory', 'qqq-hedge'):
    name = {'compare': 'experiments.json', 'inventory': 'inventory.json', 'qqq-hedge': 'qqq-hedge.json'}[sys.argv[1]]
    path = Path('/etc/variational-grid') / name
    spec = json.loads(path.read_text())
    if sys.argv[1] == 'inventory' and spec.get('kind') != 'inventory':
        raise SystemExit('Inventory service requires kind=inventory')
    if sys.argv[1] == 'qqq-hedge' and spec.get('kind') != 'qqq_hedge':
        raise SystemExit('QQQ hedge service requires kind=qqq_hedge')
    experiment = Experiment.load(path)
    expected = replace(config, center_hours=72, max_levels=None, max_margin_fraction=None) if sys.argv[1] == 'inventory' else config
    if sys.argv[1] == 'qqq-hedge':
        # QQQ uses the base session path, but not legacy economics or ledger.
        valid_base = (path.parent / spec['base_config']).resolve() == base_path.resolve()
    else:
        valid_base = experiment.base == expected
    if not valid_base:
        raise SystemExit(f'Service experiments must use {base_path}')
    output = experiment.output
    if output == Path('/var/lib/variational-grid') or not output.is_relative_to('/var/lib/variational-grid'):
        raise SystemExit('Service experiment output must stay inside /var/lib/variational-grid')
root = Path('/var/lib/variational-grid').resolve()
for value in ((config.session_file,) if sys.argv[1] == 'qqq-hedge' else (config.session_file, config.state_file)):
    path = Path(value).resolve()
    if path == root or not path.is_relative_to(root):
        raise SystemExit('Service session_file and state_file must stay inside /var/lib/variational-grid')
if sys.argv[2] == '1':
    path = Path('/etc/variational-grid/cl-bz-scalper.json')
    spec = json.loads(path.read_text())
    if spec.get('kind') != 'cl_bz_scalper':
        raise SystemExit('CL/BZ companion requires kind=cl_bz_scalper')
    if (path.parent / spec['base_config']).resolve() != base_path.resolve():
        raise SystemExit(f'CL/BZ companion must use {base_path}')
    companion = Experiment.load(path)
    output = companion.output.resolve()
    if output == root or not output.is_relative_to(root):
        raise SystemExit('CL/BZ companion output must stay inside /var/lib/variational-grid')
    # Never share any active or saved simulation tree, including historical ledgers.
    protected = [Path(config.session_file).resolve(), Path(config.state_file).resolve()]
    for name in ('qqq-hedge.json', 'experiments.json', 'inventory.json', 'inventory-base.json'):
        other_path = path.parent / name
        if other_path.exists():
            other = json.loads(other_path.read_text())
            for key in ('output_dir', 'previous_output_dir', 'session_file', 'state_file'):
                if other.get(key):
                    protected.append((other_path.parent / other[key]).resolve())
    if any(output == other or output in other.parents or other in output.parents for other in protected):
        raise SystemExit('CL/BZ companion output must be separate from all saved simulation, session and ledger paths')
PY
  )
}
validate_settings
if [[ $mode == qqq-hedge ]]; then
  (cd "$release" && python3 - <<'PY'
from variational_grid.qqq_migration import upgrade_qqq_defaults
backup = upgrade_qqq_defaults('/etc/variational-grid/qqq-hedge.json')
if backup:
    print(f'Updated to QQQ scalper v3: GTT take-profit exits, no entry distance gate, three TP settings, dynamic 450s base wait, 3000 USDC hedge threshold; starting a new simulation with zero positions and statistics; old configuration: {backup}; old positions and ledgers preserved in the previous output directory.')
else:
    print('QQQ settings unchanged; skipping migration.')
PY
  )
  # A legacy QQQ migration can select a new output directory; validate the final pair.
  if (( cl_bz_active )); then validate_settings; fi
  echo 'QQQ public feed / US100 vr-token authenticated paper pricing selected; existing simulation ledgers are preserved.'
fi
# Credential availability must not block service installation or dashboard access.
if [[ $mode == compare ]]; then
  (cd "$release" && python3 - "$conf/experiments.json" <<'PY'
import sys
from variational_grid.migration import upgrade_experiment
backup = upgrade_experiment(sys.argv[1])
if backup:
    print(f'Updated grid steps to 0.5% / 1% / 2%, each direction 30% (60/30/15 levels); old settings: {backup}; old ledgers preserved.')
else:
    print('Experiment settings unchanged; skipping migration.')
PY
  )
fi
if [[ $mode == run || $mode == compare ]]; then
  (cd "$release" && python3 - "$mode" "$conf" <<'PY'
import sys
from pathlib import Path
from variational_grid.migration import upgrade_center, upgrade_margin_limit, upgrade_unbounded_grid
comparison = sys.argv[1] == 'compare'
path = Path(sys.argv[2]) / ('experiments.json' if comparison else 'config.json')
backup = upgrade_center(path, comparison=comparison)
if backup:
    print(f'Updated center to 3 days (72 closed hours); a new simulation will start. Old settings: {backup}; old ledgers preserved.')
else:
    print('Three-day center already configured; skipping center migration.')
backup = upgrade_margin_limit(path, comparison=comparison)
if backup:
    print(f'Removed paper position/margin budget; grid levels and drawdown rules preserved. A new simulation will start. Old settings: {backup}; old ledgers preserved.')
else:
    print('Position budget settings unchanged; skipping margin migration.')
backup = upgrade_unbounded_grid(path, comparison=comparison)
if backup:
    print(f'Updated to unlimited grid levels and 100x paper leverage; position/margin budgets disabled. Old settings: {backup}; old ledgers preserved in a separate run.')
else:
    print('Grid/leverage settings unchanged; skipping unbounded-grid migration.')
PY
  )
else
  printf '%s strategy selected; skipping legacy grid migrations.\n' "$mode"
fi
settings_key=$({
  printf '%s\n' "$mode"
  if [[ $mode != qqq-hedge ]]; then
    sha256sum "$conf/$config_name"
  else
    # Session content is reloaded; changing its configured path needs a restart.
    python3 - "$conf/$config_name" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
print((path.parent / json.loads(path.read_text())['session_file']).resolve())
PY
  fi
  if [[ $mode != run ]]; then sha256sum "$conf/$experiment_name"; fi
} | sha256sum | cut -d ' ' -f1)
engine_key=$({
  printf '%s\n' "$settings_key"
  python3 --version
  release_tree engine
} | sha256sum | cut -d ' ' -f1)
cl_bz_settings_key=
cl_bz_engine_key=
if (( cl_bz_active )); then
  cl_bz_settings_key=$(sha256sum "$conf/config.json" "$conf/cl-bz-scalper.json" | sha256sum | cut -d ' ' -f1)
  cl_bz_engine_key=$({
    printf '%s\n' "$cl_bz_settings_key"
    python3 --version
    release_tree engine
  } | sha256sum | cut -d ' ' -f1)
fi
web_key=$({
  printf '%s\n' "$settings_key"
  if (( cl_bz_active )); then printf '%s\n' "$cl_bz_settings_key"; fi
  python3 --version
  release_tree web
} | sha256sum | cut -d ' ' -f1)
deployment=$(mktemp -d "$app/.deploy.XXXXXX")
printf 'variational-grid\n' >"$deployment/.install-owned"
prepare_ci_rollback() {
  [[ $deploy_mode == ci ]] || return 0
  local service file
  mkdir "$deployment/previous"
  for service in variational-grid.service variational-grid-web.service variational-grid-cl-bz.service; do
    if [[ -f /etc/systemd/system/$service ]]; then cp -p -- "/etc/systemd/system/$service" "$deployment/previous/$service"; fi
    if systemctl is-active --quiet "$service"; then touch "$deployment/previous/$service.active"; fi
    if systemctl is-enabled --quiet "$service"; then touch "$deployment/previous/$service.enabled"; fi
  done
  for file in applied-engine applied-web applied-cl-bz applied-units; do
    [[ ! -f $app/$file ]] || cp -p -- "$app/$file" "$deployment/previous/$file"
  done
  for file in mode cl-bz-enabled; do
    [[ ! -f $conf/$file ]] || cp -p -- "$conf/$file" "$deployment/previous/config-$file"
  done
  activation_started=1
}
rollback_ci() {
  local service file changed
  echo 'CI deployment failed; restoring the previous release and service configuration.' >&2
  if [[ -n $old_current ]]; then
    ln -sfn "$old_current" "$app/current.rollback"
    mv -Tf "$app/current.rollback" "$app/current"
  else rm -f -- "$app/current"; fi
  for service in variational-grid.service variational-grid-web.service variational-grid-cl-bz.service; do
    if [[ -f $deployment/previous/$service ]]; then cp -p -- "$deployment/previous/$service" "/etc/systemd/system/$service"
    else rm -f -- "/etc/systemd/system/$service"; fi
  done
  for file in applied-engine applied-web applied-cl-bz applied-units; do
    if [[ -f $deployment/previous/$file ]]; then cp -p -- "$deployment/previous/$file" "$app/$file"
    else rm -f -- "$app/$file"; fi
  done
  for file in mode cl-bz-enabled; do
    if [[ -f $deployment/previous/config-$file ]]; then cp -p -- "$deployment/previous/config-$file" "$conf/$file"
    else rm -f -- "$conf/$file"; fi
  done
  systemctl daemon-reload || true
  for service in "${ci_changed_services[@]}"; do
    if [[ -f $deployment/previous/$service.enabled ]]; then systemctl enable "$service" || true
    else systemctl disable "$service" || true; fi
    if [[ -f $deployment/previous/$service.active ]]; then systemctl restart "$service" || true
    else systemctl stop "$service" || true; fi
  done
  activation_started=0
}
prepare_ci_rollback
cat >"$deployment/variational-grid.service" <<'UNIT'
[Unit]
Description=Variational paper strategy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=variational-grid
Group=variational-grid
WorkingDirectory=/opt/variational-grid/current
ExecStart=/usr/bin/python3 -m variational_grid run --config /etc/variational-grid/config.json
Restart=on-failure
RestartSec=30
UMask=0077
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/variational-grid
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

[Install]
WantedBy=multi-user.target
UNIT
sed -i "s|^Description=.*|Description=$service_description|" "$deployment/variational-grid.service"
if [[ $mode != run ]]; then
  sed -i "s|^ExecStart=.*|ExecStart=/usr/bin/python3 -m variational_grid compare --experiments $conf/$experiment_name|" "$deployment/variational-grid.service"
  convergence_arguments=
  if (( cl_bz_active )); then
    convergence_arguments=" --convergence-experiments $conf/cl-bz-scalper.json"
    cp "$deployment/variational-grid.service" "$deployment/variational-grid-cl-bz.service"
    sed -i "s|^Description=.*|Description=Variational CL paper scalper / BZ short hedge|; s|^ExecStart=.*|ExecStart=/usr/bin/python3 -m variational_grid compare --experiments $conf/cl-bz-scalper.json|" "$deployment/variational-grid-cl-bz.service"
  fi
  cat >"$deployment/variational-grid-web.service" <<UNIT
[Unit]
Description=$service_description dashboard (localhost)
After=variational-grid.service

[Service]
Type=simple
User=variational-grid
Group=variational-grid
WorkingDirectory=/opt/variational-grid/current
ExecStart=/usr/bin/python3 -m variational_grid dashboard --experiments $conf/$experiment_name$convergence_arguments --port 9876
Restart=on-failure
RestartSec=10
UMask=0077
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
# SQLite read-only connections may need to maintain shared-memory sidecars.
ReadWritePaths=/var/lib/variational-grid
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

[Install]
WantedBy=multi-user.target
UNIT
elif [[ -f /etc/systemd/system/variational-grid-web.service ]]; then
  if systemctl is-active --quiet variational-grid-web.service || systemctl is-enabled --quiet variational-grid-web.service; then
    [[ $deploy_mode != ci ]] || ci_changed_services+=(variational-grid-web.service)
    systemctl disable --now variational-grid-web.service
  fi
fi
if (( ! cl_bz_active )) && [[ -f /etc/systemd/system/variational-grid-cl-bz.service ]]; then
  if systemctl is-active --quiet variational-grid-cl-bz.service || systemctl is-enabled --quiet variational-grid-cl-bz.service; then
    [[ $deploy_mode != ci ]] || ci_changed_services+=(variational-grid-cl-bz.service)
    systemctl disable --now variational-grid-cl-bz.service
  fi
fi
if [[ $(readlink -f "$app/current" 2>/dev/null || true) != "$release" ]]; then
  ln -sfn "$release" "$app/current"
fi
if [[ $(cat "$conf/mode" 2>/dev/null || true) != "$mode" ]]; then
  printf '%s\n' "$mode" >"$conf/mode"
  chmod 644 "$conf/mode"
fi
if [[ $cl_bz_enabled == 1 && $(cat "$conf/cl-bz-enabled" 2>/dev/null || true) != 1 ]]; then
  printf '1\n' >"$conf/cl-bz-enabled"
  chmod 644 "$conf/cl-bz-enabled"
fi
units_changed=false
engine_unit_changed=false
web_unit_changed=false
cl_bz_unit_changed=false
for service in variational-grid.service variational-grid-web.service variational-grid-cl-bz.service; do
  if [[ -f "$deployment/$service" ]] && ! cmp -s "$deployment/$service" "/etc/systemd/system/$service"; then
    # Invalidate before writing so an interrupted reload/restart is retried.
    : >"$app/applied-units"
    case $service in
      variational-grid.service) : >"$app/applied-engine"; engine_unit_changed=true ;;
      variational-grid-web.service) : >"$app/applied-web"; web_unit_changed=true ;;
      variational-grid-cl-bz.service) : >"$app/applied-cl-bz"; cl_bz_unit_changed=true ;;
    esac
    install -m 644 "$deployment/$service" "/etc/systemd/system/$service"
    units_changed=true
  fi
done
units_key=$({
  sha256sum /etc/systemd/system/variational-grid.service
  if [[ -f /etc/systemd/system/variational-grid-web.service ]]; then sha256sum /etc/systemd/system/variational-grid-web.service; fi
  if [[ -f /etc/systemd/system/variational-grid-cl-bz.service ]]; then sha256sum /etc/systemd/system/variational-grid-cl-bz.service; fi
} | sha256sum | cut -d ' ' -f1)
if $units_changed || [[ $(cat "$app/applied-units" 2>/dev/null || true) != "$units_key" ]]; then
  systemctl daemon-reload
  printf '%s\n' "$units_key" >"$app/applied-units"
fi
apply_service() {
  local service=$1 key=$2 unit_changed=$3 stamp=$4
  if ! systemctl is-enabled --quiet "$service"; then
    [[ $deploy_mode != ci ]] || ci_changed_services+=("$service")
    systemctl enable "$service"
  fi
  if [[ $(cat "$stamp" 2>/dev/null || true) != "$key" ]] || $unit_changed || ! systemctl is-active --quiet "$service"; then
    [[ $deploy_mode != ci ]] || ci_changed_services+=("$service")
    systemctl restart "$service"
    systemctl --no-pager --full status "$service"
    printf '%s\n' "$key" >"$stamp"
  else
    printf '%s unchanged and running; skipping restart.\n' "$service"
  fi
}
apply_service variational-grid.service "$engine_key" "$engine_unit_changed" "$app/applied-engine"
if (( cl_bz_active )); then
  apply_service variational-grid-cl-bz.service "$cl_bz_engine_key" "$cl_bz_unit_changed" "$app/applied-cl-bz"
fi
if [[ $mode != run ]]; then
  apply_service variational-grid-web.service "$web_key" "$web_unit_changed" "$app/applied-web"
fi
touch "$release/.install-ready"
activation_started=0
storage prune "$old_current"

echo 'Deployment ready. Settings, saved session and ledger are preserved on repeat installation.'
if [[ $mode == qqq-hedge ]]; then
  echo 'vr-token is optional during deployment. Add or refresh it using the dashboard Update Var token button (更新 Var token).'
  echo 'Missing or invalid sessions pause authenticated paper actions; a verified dashboard update is loaded automatically without restart.'
else
  echo 'Authenticated paper pricing requires a valid saved session. Use init-session separately if it needs to be added or refreshed.'
fi
printf 'Service mode: %s\n' "$mode"
printf 'Strategy: %s\n' "$service_description"
if (( cl_bz_active )); then
  printf 'CL/BZ companion: enabled; separate settings %s/cl-bz-scalper.json and preserved ledger.\n' "$conf"
  echo 'CL/BZ logs: journalctl -u variational-grid-cl-bz -f'
elif [[ $cl_bz_enabled == 1 ]]; then
  echo 'CL/BZ companion: paused in this mode; saved preference, settings and data preserved.'
else
  echo 'CL/BZ companion: not enabled; add --with-cl-bz alongside QQQ to opt in.'
fi
printf 'Settings: %s/%s\n' "$conf" "$config_name"
if [[ $mode != run ]]; then
  printf 'Experiments: %s/%s\n' "$conf" "$experiment_name"
  printf 'Report: <output_dir from %s>/public/index.html\n' "$experiment_name"
  echo 'Dashboard: run this on your own computer (keep the terminal open):'
  echo '  ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=6 -L 127.0.0.1:18765:127.0.0.1:9876 USER@SERVER_IP'
  echo 'Then open http://127.0.0.1:18765/ in your browser. No public web port is required.'
  echo 'Keep using an existing tunnel after upgrade; do not open another on the same local port.'
  echo 'Windows reconnect helper: https://github.com/hxx344/variational-grid#ssh-tunnel-recovery'
  echo 'A dashboard restart may briefly interrupt HTTP; an SSH Connection reset requires reconnecting the SSH transport.'
  echo 'Dashboard logs: journalctl -u variational-grid-web -f'
fi
echo 'Logs: journalctl -u variational-grid -f'
echo 'Stop: sudo systemctl stop variational-grid'
echo 'Restart: sudo systemctl restart variational-grid'
