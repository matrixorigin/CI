#!/usr/bin/env python3
"""Select matching latest group evidence and profiles for one Actions run."""
import argparse
import re
from pathlib import Path


def collect(raw_groups, raw_profiles, output, run_id):
    if not str(run_id).isdigit():
        raise ValueError('invalid run ID')
    root = Path(output)
    for group in range(3):
        def candidates(directory, prefix):
            pattern = re.compile(rf'^{prefix}-{group}-(?:generation-)?{run_id}-([1-9][0-9]*)$')
            return {int(match.group(1)): p for p in Path(directory).iterdir()
                    if p.is_dir() and (match := pattern.fullmatch(p.name))}
        evidence = candidates(raw_groups, 'ut-coverage-skips-group')
        profiles = candidates(raw_profiles, 'ut-coverage-group')
        if not evidence or not profiles or max(evidence) != max(profiles):
            raise ValueError(f'group {group}: missing or mismatched latest artifacts')
        attempt = max(evidence)
        group_out = root / 'groups' / str(group)
        profile_out = root / 'profiles' / str(group)
        group_out.mkdir(parents=True)
        profile_out.mkdir(parents=True)
        for filename in ('coverage-group-plan.json', 'ut-coverage-skip-manifest.json'):
            source = evidence[attempt] / filename
            if not source.is_file() or source.is_symlink():
                raise ValueError(f'group {group}: missing evidence')
            (group_out / filename).symlink_to(source.resolve())
        profile = profiles[attempt] / 'ut-coverage.out'
        if not profile.is_file() or profile.is_symlink():
            raise ValueError(f'group {group}: missing profile')
        (profile_out / 'ut-coverage.out').symlink_to(profile.resolve())
        print(f'group {group}: selected attempt {attempt}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--groups', required=True)
    parser.add_argument('--profiles', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    collect(args.groups, args.profiles, args.output, args.run_id)
