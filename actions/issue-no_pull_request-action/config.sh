#!/usr/bin/env bash

set -e;

MODULE_PREFIX='github.com'
BEFORE_OWNER="guguducken";
BEFORE_REPOSITORY="action-go-template";

OWNER="$1";
REPOSITORY="$2";

BEFORE="${MODULE_PREFIX}/${BEFORE_OWNER}/${BEFORE_REPOSITORY}"
NOW="${MODULE_PREFIX}/${OWNER}/${REPOSITORY}"

FILES=($(grep -r ${BEFORE} | grep -Ev 'idea|./.git|config.sh|Makefile' | cut -d ":" -f 1));

for file in "${FILES[@]}"; do
  sed -i "" "s|${BEFORE}|${NOW}|g" "$file";
done
