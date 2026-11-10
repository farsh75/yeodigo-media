"""토큰 점검 — 게시 없이 두 계정에 접속되는지만 본다. 토큰 값은 출력하지 않는다."""
from __future__ import annotations

import os
import sys

from publish import IG_HOST, TH_HOST, PublishError, _req

ok = True
try:
    ver = os.environ.get("IG_GRAPH_VERSION", "v23.0")
    r = _req("GET", f"{IG_HOST}/{ver}/{os.environ['IG_USER_ID']}",
             {"fields": "id,username", "access_token": os.environ["IG_ACCESS_TOKEN"]})
    print(f"인스타 OK: @{r.get('username')} (id {r.get('id')})")
except (PublishError, KeyError) as e:
    ok = False
    print(f"인스타 실패: {e}")
try:
    ver = os.environ.get("THREADS_GRAPH_VERSION", "v1.0")
    uid = os.environ.get("THREADS_USER_ID", "me")
    r = _req("GET", f"{TH_HOST}/{ver}/{uid}", {"fields": "id,username", "access_token": os.environ["THREADS_ACCESS_TOKEN"]})
    print(f"스레드 OK: @{r.get('username')} (id {r.get('id')})")
    if uid == "me":
        print("참고: THREADS_USER_ID를 위 숫자 id로 바꿔도 되고, me 그대로 둬도 됩니다.")
except (PublishError, KeyError) as e:
    ok = False
    print(f"스레드 실패: {e}")
sys.exit(0 if ok else 1)
