"""Interactive macOS secure setup. Password entry belongs to security, not files."""
import getpass
import subprocess
from scripts.laptop_scheduler import SECRET_NAMES, SERVICE_PREFIX


def main():
    print('Keys are stored in macOS Keychain. Do not paste them into chat.')
    for name in SECRET_NAMES:
        if name == 'MAIL_CC' and input('Add a CC recipient? [y/N] ').lower() != 'y':
            continue
        print(f'Configure {name} (the next password prompt is the value).')
        subprocess.run(['/usr/bin/security', 'add-generic-password', '-U', '-a', getpass.getuser(),
                        '-s', SERVICE_PREFIX + name, '-w'], check=True)
    print('Setup complete. Run python -m scripts.laptop_scheduler --check-credentials to verify presence only.')


if __name__ == '__main__':
    main()
