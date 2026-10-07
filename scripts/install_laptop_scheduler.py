"""Install the authorized per-user launchd schedule; no keys in the plist."""
import argparse
import plistlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABEL = 'de.miki.jobsearch'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--install', action='store_true')
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        raise SystemExit('Use Python 3.11+ to install the scheduler.')
    logs = ROOT / 'out/laptop-scheduler'
    logs.mkdir(parents=True, exist_ok=True)
    plist = {'Label': LABEL, 'ProgramArguments': [sys.executable, '-m', 'scripts.laptop_scheduler'],
             'WorkingDirectory': str(ROOT), 'StartInterval': 60, 'RunAtLoad': True,
             'ProcessType': 'Background', 'StandardOutPath': str(logs / 'launchd.log'),
             'StandardErrorPath': str(logs / 'launchd-error.log'),
             'EnvironmentVariables': {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'GIT_TERMINAL_PROMPT': '0'}}
    preview = logs / f'{LABEL}.plist'
    preview.write_bytes(plistlib.dumps(plist))
    print(f'Prepared {preview}')
    if args.install:
        import os
        target = Path.home() / 'Library/LaunchAgents' / f'{LABEL}.plist'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(plistlib.dumps(plist))
        domain = f'gui/{os.getuid()}'
        subprocess.run(['/bin/launchctl', 'bootout', domain + '/' + LABEL], capture_output=True)
        subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(target)], check=True)
        print(f'Installed {target}')


if __name__ == '__main__':
    main()
