"""여디고 게시기 — 공개 저장소(yeodigo-media)의 GitHub Actions에서 돈다.

queue/<id>.json 중 발행 시각이 지난 글을 스레드·인스타에 올리고 results/<id>.json에 결과를 남긴다.
표준 라이브러리만 쓴다(Actions에서 설치 없이 실행).

환경 변수 (GitHub Secrets / Variables)
  IG_ACCESS_TOKEN, IG_USER_ID            인스타 (시스템 사용자 토큰 + 인스타 비즈니스 계정 ID)
  THREADS_ACCESS_TOKEN, THREADS_USER_ID  스레드
  PUBLISH_ENABLED                        true/false. 비어 있으면 publisher/config.json의 publish_enabled를 따름. 꺼져 있으면 드라이런
  MEDIA_BASE_URL                         이미지 공개 주소 앞부분 (예: https://raw.githubusercontent.com/farsh75/yeodigo-media/main)
  IG_GRAPH_VERSION (기본 v23.0), THREADS_GRAPH_VERSION (기본 v1.0)
  MAX_PER_RUN (기본 3)                   한 번에 올리는 최대 개수 (실수로 몰아 올리기 방지)
  ONLY_ID                                이 id 하나만 (시험 게시용)

queue/<id>.json 형식 (yeodigo_content export-queue가 만든다)
  {id, channel: threads|instagram, slot: ISO8601, approved_at, text, replies: [..], caption, images: ["images/<id>/01.jpg", ...]}
results/<id>.json
  {id, status: published|failed, at, media_id, permalink, error, reply_ids}
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("MEDIA_REPO", "."))
QUEUE, RESULTS = ROOT / "queue", ROOT / "results"
# 인스타: 새 앱은 시스템 사용자 토큰(만료 없음) → graph.facebook.com. 인스타 로그인 토큰이면 IG_API_HOST=https://graph.instagram.com
IG_HOST = os.environ.get("IG_API_HOST", "https://graph.facebook.com")
TH_HOST = "https://graph.threads.net"


class PublishError(Exception):
    pass


def _req(method: str, url: str, params: dict | None = None, opener=urllib.request.urlopen) -> dict:
    data = None
    if params:
        enc = urllib.parse.urlencode(params)
        if method == "GET":
            url = url + ("&" if "?" in url else "?") + enc
        else:
            data = enc.encode()
    r = urllib.request.Request(url, data=data, method=method)
    try:
        with opener(r, timeout=60) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        raise PublishError(f"HTTP {e.code} {url.split('?')[0]}: {body}") from None


class Api:
    """HTTP 호출을 한곳에 모아 테스트에서 바꿔 끼울 수 있게."""

    def __init__(self, call=_req, sleep=time.sleep):
        self.call, self.sleep = call, sleep


def media_url(path: str) -> str:
    base = os.environ.get("MEDIA_BASE_URL", "").rstrip("/")
    if not base:
        raise PublishError("MEDIA_BASE_URL이 없음")
    return f"{base}/{path.lstrip('/')}"


# ---------------- 스레드 ----------------
def _th_wait(api: Api, ver: str, cid: str, token: str, tries: int = 20) -> None:
    for _ in range(tries):
        st = api.call("GET", f"{TH_HOST}/{ver}/{cid}", {"fields": "status,error_message", "access_token": token})
        s = st.get("status")
        if s == "FINISHED":
            return
        if s in ("ERROR", "EXPIRED"):
            raise PublishError(f"스레드 컨테이너 {s}: {st.get('error_message', '')}")
        api.sleep(3)
    raise PublishError("스레드 컨테이너 준비 시간 초과")


def publish_threads(post: dict, api: Api) -> dict:
    token, uid = os.environ["THREADS_ACCESS_TOKEN"], os.environ["THREADS_USER_ID"]
    ver = os.environ.get("THREADS_GRAPH_VERSION", "v1.0")
    p = {"text": post["text"], "access_token": token}
    imgs = post.get("images") or []
    if imgs:
        p.update(media_type="IMAGE", image_url=media_url(imgs[0]))
    else:
        p.update(media_type="TEXT")
    cid = api.call("POST", f"{TH_HOST}/{ver}/{uid}/threads", p)["id"]
    _th_wait(api, ver, cid, token)
    mid = api.call("POST", f"{TH_HOST}/{ver}/{uid}/threads_publish", {"creation_id": cid, "access_token": token})["id"]
    reply_ids, parent = [], mid
    for txt in post.get("replies") or []:
        api.sleep(2)
        rc = api.call("POST", f"{TH_HOST}/{ver}/{uid}/threads",
                      {"media_type": "TEXT", "text": txt, "reply_to_id": parent, "access_token": token})["id"]
        _th_wait(api, ver, rc, token)
        rid = api.call("POST", f"{TH_HOST}/{ver}/{uid}/threads_publish", {"creation_id": rc, "access_token": token})["id"]
        reply_ids.append(rid)
        parent = rid  # 이어쓰기는 앞 글에 이어 단다
    link = api.call("GET", f"{TH_HOST}/{ver}/{mid}", {"fields": "permalink", "access_token": token}).get("permalink", "")
    return {"media_id": mid, "permalink": link, "reply_ids": reply_ids}


# ---------------- 인스타 ----------------
def _ig_wait(api: Api, ver: str, cid: str, token: str, tries: int = 30) -> None:
    for _ in range(tries):
        st = api.call("GET", f"{IG_HOST}/{ver}/{cid}", {"fields": "status_code,status", "access_token": token})
        s = st.get("status_code")
        if s == "FINISHED":
            return
        if s in ("ERROR", "EXPIRED"):
            raise PublishError(f"인스타 컨테이너 {s}: {st.get('status', '')}")
        api.sleep(4)
    raise PublishError("인스타 컨테이너 준비 시간 초과")


def publish_instagram(post: dict, api: Api) -> dict:
    token, uid = os.environ["IG_ACCESS_TOKEN"], os.environ["IG_USER_ID"]
    ver = os.environ.get("IG_GRAPH_VERSION", "v23.0")
    imgs = post.get("images") or []
    if not imgs:
        raise PublishError("인스타 글에 이미지가 없음")
    if len(imgs) > 10:
        raise PublishError("인스타 캐러셀은 10장까지")
    caption = post.get("caption", "")
    if len(imgs) == 1:
        cid = api.call("POST", f"{IG_HOST}/{ver}/{uid}/media",
                       {"image_url": media_url(imgs[0]), "caption": caption, "access_token": token})["id"]
    else:
        kids = []
        for im in imgs:
            k = api.call("POST", f"{IG_HOST}/{ver}/{uid}/media",
                         {"image_url": media_url(im), "is_carousel_item": "true", "access_token": token})["id"]
            _ig_wait(api, ver, k, token)
            kids.append(k)
        cid = api.call("POST", f"{IG_HOST}/{ver}/{uid}/media",
                       {"media_type": "CAROUSEL", "children": ",".join(kids), "caption": caption, "access_token": token})["id"]
    _ig_wait(api, ver, cid, token)
    mid = api.call("POST", f"{IG_HOST}/{ver}/{uid}/media_publish", {"creation_id": cid, "access_token": token})["id"]
    link = api.call("GET", f"{IG_HOST}/{ver}/{mid}", {"fields": "permalink", "access_token": token}).get("permalink", "")
    return {"media_id": mid, "permalink": link}


# ---------------- 실행 ----------------
def due_posts(now: datetime) -> list[dict]:
    out = []
    only = os.environ.get("ONLY_ID", "").strip()
    for f in sorted(QUEUE.glob("*.json")):
        post = json.loads(f.read_text(encoding="utf-8"))
        if only and post.get("id") != only:
            continue
        if (RESULTS / f.name).exists():
            continue  # 이미 처리함(성공·실패 모두). 다시 올리려면 결과 파일을 지운다
        if not post.get("approved_at"):
            continue  # 승인 안 된 글은 절대 올리지 않음
        if not only and datetime.fromisoformat(post["slot"]) > now:
            continue
        out.append(post)
    return sorted(out, key=lambda p: p["slot"])


def is_live() -> bool:
    """자동 게시 스위치. 환경 변수 PUBLISH_ENABLED(true/false)가 있으면 그걸, 없으면 publisher/config.json의 publish_enabled."""
    env = os.environ.get("PUBLISH_ENABLED", "").strip().lower()
    if env in ("true", "false"):
        return env == "true"
    cfg = ROOT / "publisher" / "config.json"
    try:
        return bool(json.loads(cfg.read_text(encoding="utf-8")).get("publish_enabled"))
    except (OSError, ValueError):
        return False


def run(now: datetime | None = None, api: Api | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    api = api or Api()
    live = is_live()
    limit = int(os.environ.get("MAX_PER_RUN", "3"))
    RESULTS.mkdir(parents=True, exist_ok=True)
    done = []
    for post in due_posts(now)[:limit]:
        if not live:
            print(f"[드라이런] {post['id']} ({post['channel']}, {post['slot']}) 이미지 {len(post.get('images') or [])}장")
            continue
        res = {"id": post["id"], "channel": post["channel"], "at": now.isoformat()}
        try:
            fn = publish_threads if post["channel"] == "threads" else publish_instagram
            res.update(status="published", **fn(post, api))
            print(f"[게시] {post['id']} → {res.get('permalink')}")
        except (PublishError, KeyError) as e:
            res.update(status="failed", error=str(e))
            print(f"[실패] {post['id']}: {e}", file=sys.stderr)
        (RESULTS / f"{post['id']}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        done.append(res)
    return done


def _git(*args: str) -> None:
    import subprocess
    subprocess.run(["git", *args], cwd=ROOT, check=False)


def _save_results() -> None:
    _git("add", "results")
    import subprocess
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode != 0:
        _git("commit", "-m", "publish results")
        _git("pull", "--rebase", "-q")
        _git("push", "-q")


def pending_slots() -> list[datetime]:
    out = []
    for f in QUEUE.glob("*.json"):
        if (RESULTS / f.name).exists():
            continue
        try:
            post = json.loads(f.read_text(encoding="utf-8"))
            if post.get("approved_at"):
                out.append(datetime.fromisoformat(post["slot"]))
        except (ValueError, KeyError):
            continue
    return sorted(out)


def watch(minutes: int, api: Api | None = None) -> list[dict]:
    """GitHub 예약 실행(cron)은 몇 시간씩 늦거나 빠지는 일이 있어서, 동기화 작업이 내보낸 직후 이 모드로 한 번 띄운다.
    정해진 시간(분) 동안 깨어 있으면서 시각이 된 글을 바로 올리고, 15분마다 저장소를 다시 받아 새로 들어온 글도 챙긴다.
    남은 시간 안에 올릴 글이 없으면 일찍 끝낸다."""
    import time
    from datetime import timedelta
    end = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    allres: list[dict] = []
    while True:
        _git("pull", "--rebase", "-q")
        res = run(api=api)
        if res:
            allres += res
            _save_results()
        now = datetime.now(timezone.utc)
        upcoming = [t for t in pending_slots() if t <= end]
        if now >= end or not upcoming:
            print(f"[대기 끝] 남은 글 {len(pending_slots())}개, 이번 창 안에 올릴 글 없음" if not upcoming else "[대기 끝] 시간 다 됨")
            return allres
        nxt = max(upcoming[0], now)
        wait = min((nxt - now).total_seconds() + 20, 15 * 60, (end - now).total_seconds())
        print(f"[대기] 다음 글 {upcoming[0].isoformat()} · {int(wait // 60)}분 뒤 다시 확인")
        time.sleep(max(wait, 30))


if __name__ == "__main__":
    w = os.environ.get("WATCH_MINUTES", "").strip()
    results = watch(int(w)) if w.isdigit() and int(w) > 0 else run()
    sys.exit(1 if any(r["status"] == "failed" for r in results) else 0)
