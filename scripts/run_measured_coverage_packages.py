"""Opt-in wrapper: keep the existing package scheduler and original Go arguments."""
import argparse
from pathlib import Path
import run_coverage_packages as original
from go_rebuild_metrics import run_measured


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--profile', required=True,type=Path)
    p.add_argument('--coverpkg',required=True)
    p.add_argument('--parallel',required=True,type=int)
    p.add_argument('packages',nargs='+')
    a=p.parse_args()
    if a.parallel<1:p.error('parallelism must be positive')
    summary=a.profile.parent/'go-rebuild-counts.json'
    def execute(cmd,*,stderr):
        return run_measured(cmd,diagnostics=stderr,output=summary)
    return original.run(a.packages,a.profile,a.coverpkg,a.parallel,execute=execute)


if __name__=='__main__':raise SystemExit(main())
