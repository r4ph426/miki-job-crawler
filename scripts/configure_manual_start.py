"""Store a repo-scoped Actions token securely for connecting the private Site."""
import getpass
import subprocess

if __name__ == '__main__':
    print('Create a fine-grained GitHub token for r4ph426/miki-job-crawler only, with Actions: read/write.')
    print('The next secure password prompt stores it in macOS Keychain. Do not paste it into chat.')
    subprocess.run(['/usr/bin/security', 'add-generic-password', '-U', '-a', getpass.getuser(),
                    '-s', 'miki-jobsearch/GITHUB_ACTIONS_TOKEN', '-w'], check=True)
    print('Token stored. Tell Codex the secure entry is ready; Codex can transfer it to the private Site runtime secret without displaying it.')
