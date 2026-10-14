#!/bin/sh
# git이 .githooks/의 pre-push 리뷰 훅을 쓰게 한다. 클론 후 한 번 실행한다.
set -e
cd "$(git rev-parse --show-toplevel)"
chmod +x .githooks/*
git config core.hooksPath .githooks
echo "git 훅 설치 완료: core.hooksPath=.githooks (pre-push)"
