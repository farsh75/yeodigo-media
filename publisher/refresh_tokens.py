"""메타 장기 토큰 연장 — GitHub Actions에서 2주마다 돈다.

인스타·스레드 장기 토큰은 60일이면 만료된다. 만든 지 24시간이 지난 유효한 토큰은 연장할 수 있다.
연장 결과 토큰 값이 바뀌면 GH_PAT(이 저장소 Secrets 쓰기 권한이 있는 토큰)가 있을 때 gh로 Secret을 바꾼다.
GH_PAT이 없으면 만료일만 출력하고, 값이 바뀌었으면 실패로 끝내 알림이 가게 한다(토큰 값은 절대 출력하지 않음).
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from publish import IG_HOST, TH_HOST, PublishError, _req

# 인스타 시스템 사용자 토큰은 만료가 없어 연장하지 않는다(IG_REFRESH=true일 때만, 인스타 로그인 토큰용).
TARGETS = [("THREADS_ACCESS_TOKEN", f"{TH_HOST}/refresh_access_token", "th_refresh_token")]
if os.environ.get("IG_REFRESH", "").lower() == "true":
    TARGETS.insert(0, ("IG_ACCESS_TOKEN", "https://graph.instagram.com/refresh_access_token", "ig_refresh_token"))


def refresh(call=_req) -> int:
    bad = 0
    for name, url, grant in TARGETS:
        tok = os.environ.get(name, "")
        if not tok:
            print(f"{name}: 없음 (건너뜀)")
            continue
        try:
            r = call("GET", url, {"grant_type": grant, "access_token": tok})
        except PublishError as e:
            print(f"{name}: 연장 실패 — {e}")
            bad += 1
            continue
        new, secs = r.get("access_token", ""), int(r.get("expires_in", 0))
        until = (datetime.now(timezone.utc) + timedelta(seconds=secs)).date()
        print(f"{name}: 연장됨, 만료 예정 {until}")
        if new and new != tok:
            pat = os.environ.get("GH_PAT", "")
            if not pat:
                print(f"{name}: 새 토큰 값이 나왔지만 GH_PAT이 없어 Secret을 못 바꿈 → 맥퀸님 확인 필요")
                bad += 1
                continue
            env = dict(os.environ, GH_TOKEN=pat)
            subprocess.run(["gh", "secret", "set", name, "--body", new], check=True, env=env,
                           stdout=subprocess.DEVNULL)
            print(f"{name}: Secret 갱신함")
    return bad


if __name__ == "__main__":
    sys.exit(1 if refresh() else 0)
