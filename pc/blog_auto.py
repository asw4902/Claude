# -*- coding: utf-8 -*-
"""
네이버 블로그 자동 글쓰기 (PC 버전)

하는 일
  1. 매일 정해진 시간에 최신 이슈를 웹에서 검색해 키워드를 고르고 초안을 씁니다. (Claude)
  2. 아이패드·PC 브라우저에서 초안을 검토하고 선정합니다. (같은 검토 앱을 이 PC가 띄워 줌)
  3. 선정한 글을 정해진 시간에 이 PC의 크롬이 네이버 블로그 글쓰기 화면에 입력하고 발행합니다.
     네이버 로그인은 처음 한 번, 열리는 크롬 창에서 직접 합니다.

실행:  python blog_auto.py           (검토 앱 + 자동 작성 + 자동 발행)
       python blog_auto.py --login   (네이버 로그인 창만 열기)
"""
import argparse
import base64
import json
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
IMAGES = DATA / "images"
LOGS = DATA / "logs"
PROFILE = DATA / "naver_profile"      # 네이버 로그인이 저장되는 크롬 프로필
WEB = BASE / "web"
for d in (DATA, IMAGES, LOGS):
    d.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "port": 8765,
    "pin": "",                       # 첫 실행 때 자동으로 만들어짐. 검토 앱 접속 비밀번호
    "writer": "auto",                # auto | claude-cli | api
    "model": "claude-opus-5-5",      # writer=api일 때 쓰는 모델
    "auto_write": False,             # 매일 자동 작성 (기본: 끔. 앱의 버튼으로 원할 때 작성)
    "auto_write_time": "05:47",      # 매일 자동 작성 시각 (이 PC 시간)
    "posts_per_day": 4,
    "auto_publish": True,            # 예약 시간이 된 '발행 대기' 글을 자동 발행
    "headless": False,               # True면 크롬 창을 띄우지 않고 발행
    "publish_mode": "publish",       # publish(발행) | draft(임시저장만, 시험용)
}
CONFIG_PATH = BASE / "config.json"
LOCK = threading.RLock()


def log(*a):
    msg = datetime.now().strftime("%m-%d %H:%M:%S ") + " ".join(str(x) for x in a)
    print(msg, flush=True)
    try:
        with open(LOGS / "blog_auto.log", "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(read_json(CONFIG_PATH, {}))
    if not cfg.get("pin"):
        cfg["pin"] = f"{secrets.randbelow(1000000):06d}"
        write_json(CONFIG_PATH, cfg)
    return cfg


CFG = load_config()

# ---------------------------------------------------------------- 저장소
POSTS_PATH = DATA / "posts.json"
SETTINGS_PATH = DATA / "settings.json"
AUTORUN_PATH = DATA / "autorun.json"


def posts_all():
    with LOCK:
        return read_json(POSTS_PATH, {})


def post_get(pid):
    return posts_all().get(pid)


def post_put(p):
    with LOCK:
        allp = read_json(POSTS_PATH, {})
        old = allp.get(p["id"], {})
        # 발행 결과는 서버가 관리하므로 앱이 덮어쓰지 않게 유지
        for k in ("pub",):
            if k in old and k not in p:
                p[k] = old[k]
        p["updatedAt"] = int(time.time() * 1000)
        allp[p["id"]] = p
        write_json(POSTS_PATH, allp)
        return p


def post_patch(pid, **kw):
    with LOCK:
        allp = read_json(POSTS_PATH, {})
        if pid not in allp:
            return None
        allp[pid].update(kw)
        allp[pid]["updatedAt"] = int(time.time() * 1000)
        write_json(POSTS_PATH, allp)
        return allp[pid]


def post_delete(pid):
    with LOCK:
        allp = read_json(POSTS_PATH, {})
        allp.pop(pid, None)
        write_json(POSTS_PATH, allp)
    shutil.rmtree(IMAGES / pid, ignore_errors=True)


def settings():
    s = {
        "cats": ["정부지원·복지", "주거·청약", "생활 꿀팁", "일상 이야기"],
        "persona": "가족과 평범하게 사는 50대 가장. 지원 제도나 생활 정보를 직접 찾아보고 겪어 본 걸 쉬운 말로 정리한다. 꾸밈없고 솔직한 편.",
        "nick": "팜로그랩", "blogId": "panlog-lab", "tone": "친근한 해요체",
        "minLen": 3000, "imgCount": "5~7", "emoji": True,
    }
    s.update(read_json(SETTINGS_PATH, {}))
    return s


def autorun_set(**kw):
    with LOCK:
        a = read_json(AUTORUN_PATH, {"kind": "autorun"})
        a.update(kw)
        write_json(AUTORUN_PATH, a)
        return a


# ---------------------------------------------------------------- Claude 호출
class WriterError(Exception):
    pass


def parse_json_loose(text):
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except ValueError:
            pass
    a = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    b = max(text.rfind("}"), text.rfind("]"))
    if a >= 0 and b > a:
        return json.loads(text[a:b + 1])
    raise WriterError("결과를 JSON으로 읽지 못했습니다.")


def find_claude():
    """claude 명령 위치. PATH에 없어도 기본 설치 위치를 직접 찾아본다."""
    exe = shutil.which("claude")
    if exe:
        return exe
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    for c in (home / ".local" / "bin" / "claude.exe", home / ".local" / "bin" / "claude",
              Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "claude.exe",
              Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd"):
        if c.is_file():
            return str(c)
    return None


def writer_kind():
    w = CFG.get("writer", "auto")
    if w == "auto":
        if os.environ.get("BLOG_AUTO_FAKE"):
            return "fake"
        if find_claude():
            return "claude-cli"
        return "api"
    return w


def ask_claude(prompt, web=False, timeout=1200):
    """프롬프트를 보내고 JSON 결과를 돌려준다. web=True면 웹 검색 허용."""
    kind = writer_kind()
    if kind == "fake":
        return fake_answer(prompt)
    if kind == "claude-cli":
        return ask_cli(prompt, web, timeout)
    return ask_api(prompt, web)


def ask_cli(prompt, web, timeout):
    # 이 PC에 설치된 Claude Code(claude 명령)를 내 Claude 계정으로 사용
    exe = find_claude()
    if not exe:
        raise WriterError("claude 명령을 찾지 못했습니다. Claude Code를 설치하거나 config.json의 writer를 api로 바꾸세요.")
    cmd = [exe, "-p", "--output-format", "json"]
    if web:
        cmd += ["--allowedTools", "WebSearch", "WebFetch"]
    # API 키가 환경 변수에 있으면 Claude Code가 그 키(유료)를 쓸 수 있으므로, 구독 로그인만 쓰도록 빼고 실행
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    # 오래 걸리므로 30초마다 진행 상황을 창에 표시
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", cwd=str(DATA), env=env)
    result = {}

    def talk():
        try:
            result["out"], result["err"] = proc.communicate(prompt)
        except Exception as e:   # noqa
            result["exc"] = e
    th = threading.Thread(target=talk, daemon=True)
    th.start()
    start = time.time()
    while th.is_alive():
        th.join(30)
        if th.is_alive():
            waited = int(time.time() - start)
            if waited >= timeout:
                proc.kill()
                raise WriterError(f"Claude 응답이 {timeout // 60}분 넘게 없어 중단했습니다.")
            log(f"    Claude 작업 중… {waited // 60}분 {waited % 60}초 ({'웹 검색 포함, 5~10분 걸릴 수 있음' if web else '1~2분'})")
    if "exc" in result:
        raise WriterError(f"Claude Code 실행 실패: {result['exc']}")
    out_s, err_s = result.get("out") or "", result.get("err") or ""
    if proc.returncode != 0:
        raise WriterError("Claude Code 실행 실패: " + (err_s or out_s).strip()[-300:])
    try:
        out = json.loads(out_s)
        text = out.get("result", "") if isinstance(out, dict) else str(out)
        if isinstance(out, dict) and out.get("is_error"):
            raise WriterError("Claude Code 오류: " + str(text)[-300:])
    except ValueError:
        text = out_s
    return parse_json_loose(text)


def ask_api(prompt, web):
    # Anthropic API (ANTHROPIC_API_KEY 환경 변수 또는 ant auth login 프로필)
    try:
        import anthropic
    except ImportError:
        raise WriterError("anthropic 패키지가 없습니다. 설치.bat을 다시 실행하세요.")
    client = anthropic.Anthropic()
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 12}] if web else []
    messages = [{"role": "user", "content": prompt}]
    texts = []
    for _ in range(6):   # 웹 검색이 길어지면 pause_turn으로 끊겨서 이어서 요청
        kw = dict(model=CFG.get("model", "claude-opus-5-5"), max_tokens=64000, messages=messages,
                  output_config={"effort": "medium"},
                  betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        if tools:
            kw["tools"] = tools
        try:
            with client.beta.messages.stream(**kw) as stream:
                msg = stream.get_final_message()
        except anthropic.AuthenticationError:
            raise WriterError("API 키가 없거나 잘못됐습니다. ANTHROPIC_API_KEY를 설정하세요.")
        except anthropic.RateLimitError:
            raise WriterError("API 사용 한도에 걸렸습니다. 잠시 뒤 다시 시도하세요.")
        except anthropic.APIStatusError as e:
            raise WriterError(f"API 오류 {e.status_code}: {str(e)[:200]}")
        except anthropic.APIConnectionError:
            raise WriterError("API 서버에 연결하지 못했습니다. 인터넷 연결을 확인하세요.")
        if msg.stop_reason == "refusal":
            raise WriterError("Claude가 이 요청을 처리하지 않았습니다. 주제를 바꿔 보세요.")
        if msg.stop_reason == "pause_turn":
            messages = [{"role": "user", "content": prompt}, {"role": "assistant", "content": msg.content}]
            continue
        texts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
        break
    if not texts:
        raise WriterError("Claude 응답에 글이 없습니다.")
    return parse_json_loose(texts[-1] if texts[-1].strip().startswith(("{", "[", "`")) else "\n".join(texts))


def fake_answer(prompt):
    # 테스트용 (BLOG_AUTO_FAKE=1)
    time.sleep(0.3)
    if "키워드" in prompt and "keywords" in prompt:
        return {"keywords": [{"keyword": f"테스트 키워드 {i}", "cat": "생활 꿀팁", "angle": f"테스트 주제 {i}",
                              "why": "테스트", "sources": [{"title": "정책브리핑", "url": "https://www.korea.kr"}]} for i in range(1, 3)]}
    body = "■ 첫 소제목\n" + ("솔직히 처음엔 몰랐어요. 그런데 해 보니 쉽더라고요. " * 70) + "\n\n[사진: 신청 화면 캡처]\n\n■ 둘째 소제목\n마무리예요.\n\n확인일: 2026년 11월 15일 · 출처: 정책브리핑"
    return {"titles": ["테스트 제목입니다"], "body": body, "tags": ["테스트"], "thumb": "테스트", "summary": ["요약 하나"],
            "images": [{"type": "steps", "title": "신청 순서", "items": ["로그인", "신청", "확인"]}], "images_only": True}


# ---------------------------------------------------------------- 자동 작성
BANNED = ("알아보겠습니다, 알아볼게요, 살펴보겠습니다, 결론적으로, 요약하자면, 정리하자면, 결론부터 말씀, 이번 포스팅에서는, "
          "오늘 포스팅에서는, 도움이 되셨길, 도움이 되셨기를, 첫째·둘째·셋째, 답부터 적겠습니다, 자료에 잘려, 숫자를 지어낼, "
          "확정하지 못했, 참고용으로만, (…수집), AI·인공지능·챗GPT 언급, 충격, 무조건, 모르면 손해, 총정리")

IMAGE_RULE = (
    'images: 본문의 [사진: …] 자리와 같은 개수, 같은 순서로 정리 카드 내용. 형식 {"type":"steps|checklist|keynum|compare|qa|tip",'
    '"title":"16자 이내","items":[...],"big":"keynum일 때 핵심 값","cols":["compare 왼쪽 열","오른쪽 열"],"note":"tip·qa 설명 60자 이내"}. '
    "steps=순서 3~5개, checklist=확인할 것 3~6개, keynum=big에 날짜·금액 하나+items 1~3줄, compare=items [[항목,왼쪽,오른쪽],…] 2~4줄, "
    "qa=title 질문+note 답, tip=title+note. 본문에 있는 사실만, [공고 확인] 값은 넣지 말 것. 종류를 섞을 것."
)


def keyword_prompt(s, n, used):
    today = datetime.now().strftime("%Y년 %m월 %d일")
    return f"""오늘은 {today}(한국). 네이버 블로그 '{s['nick']}'에 올릴 글 키워드 {n}개를 웹 검색으로 골라줘.
- 블로그 카테고리 (이 중에서만): {' / '.join(s['cats'])}
- 글쓴이: {s['persona']}
- 한국어로 8~12번 검색: 이번 주 정부 지원금 신청 시작, 정책브리핑 이번 주 생활 정책, 이번 달 신청 마감 지원금, 청약 일정 이번 주, 공공임대 모집 공고, 다음 달부터 달라지는 생활 요금, 계절 생활 이슈, 오늘 인기 검색어(참고만).
- 최근 2주 안에 발표됐거나 화제이거나 지금 신청 기간인 것, 카테고리에 맞는 것, 글쓴이가 자기 이야기처럼 쓸 수 있는 것. 카테고리를 골고루.
- 제외: 건강·의료 조언, 연예인, 주식·코인 종목, 정치 논쟁, 사건사고, 상품 홍보, 최근에 쓴 키워드({', '.join(used) or '없음'}).
반드시 JSON 하나만 답해: {{"keywords": [{{"keyword": "검색 키워드", "cat": "카테고리 그대로", "angle": "글 주제 한 줄", "why": "요즘 찾는 이유 한 줄", "sources": [{{"title": "...", "url": "https://..."}}]}}]}}"""


def post_prompt(s, k):
    today = datetime.now().strftime("%Y년 %#m월 %#d일" if os.name == "nt" else "%Y년 %-m월 %-d일")
    emoji = "글 전체에 3~5개" if s.get("emoji", True) else "쓰지 않기"
    src = "\n".join(f"- {x.get('title','')}: {x.get('url','')}" for x in k.get("sources", []))
    return f"""너는 네이버 블로그 '{s['nick']}'를 직접 운영하는 사람이다. 글쓴이: {s['persona']}
아래 키워드로 블로그 글 한 편을 써라. 먼저 웹 검색 2~4번으로 대상·금액·기간·신청 방법 같은 사실을 확인하고, 확인한 사실만 써라. 확인 못 한 중요한 값은 한 줄로 '[공고 확인: 무엇]'. 숫자·날짜를 지어내지 말 것.

- 카테고리: {k['cat']} / 키워드: {k['keyword']} / 주제: {k['angle']} / 요즘 찾는 이유: {k.get('why','')}
- 참고할 자료:
{src or '- (직접 검색)'}

[쓰는 법]
- 말투 {s['tone']}, 블로그 이웃에게 말하듯. 1인칭. 너무 매끄러운 AI 문장 말고 사람이 쓴 흐름.
- 본문 공백 제외 {s['minLen']}자 이상(10% 넉넉히). 소제목 5~7개, 소제목 줄은 '■ '로 시작, 소제목마다 450자 이상.
- 시작은 작은 장면·가족이 물어본 말·처음에 잘못 알았던 것·계절 이야기 중 하나로. '안녕하세요' 금지.
- 짧은 문장과 긴 문장을 섞고 가끔 솔직한 혼잣말. 소제목마다 같은 구조 반복 금지.
- 글쓴이 경험은 지어내지 말고, 경험이 들어갈 곳 2~3군데에 한 줄로 '[직접 쓰기: 어떤 경험을 쓰면 좋을지]'.
- 금지 표현: {BANNED}. '다양한·중요합니다·또한·뿐만 아니라·~것이 좋습니다'는 각각 1번 이하.
- 이모지 {emoji}, 'ㅎㅎ'는 2번 이하. 마크다운(#, **, -, 표) 금지. 한 문단 1~4문장, 문단 사이 빈 줄.
- 키워드는 첫 문단 1번, 본문 4~6번 자연스럽게.
- 이미지 자리 {s['imgCount']}곳: 한 줄로 '[사진: 무엇을 어떻게 찍은 사진]' (직접 찍거나 캡처할 수 있는 장면), 고르게.
- 마무리는 짧게. 맨 끝 빈 줄 뒤 '확인일: {today} · 출처: 기관/언론 이름', 제도 글이면 다음 줄 '제도는 자주 바뀌니 신청 전에 해당 기관 공지를 한 번 더 확인해 주세요.'
- 제목 3개(25~35자, 키워드 앞쪽, 담백하게), 태그 8~12개(# 없이), thumb(대표 이미지 문구 14자 안팎), summary(4~5줄, 각 22자 이내).
- {IMAGE_RULE}

반드시 JSON 하나만 답해: {{"titles": [...], "body": "본문(줄바꿈은 \\n)", "tags": [...], "thumb": "...", "summary": [...], "images": [...], "sources": [{{"title": "...", "url": "..."}}]}}"""


AUTORUN_BUSY = threading.Event()


def autorun(n=None):
    if AUTORUN_BUSY.is_set():
        return False
    AUTORUN_BUSY.set()
    threading.Thread(target=_autorun, args=(n,), daemon=True).start()
    return True


def _autorun(n):
    try:
        s = settings()
        n = int(n or CFG.get("posts_per_day", 10))
        now_ms = int(time.time() * 1000)
        autorun_set(status="running", startedAt=now_ms, finishedAt=None, count=0, keywords=[], message="최신 이슈 검색 중")
        cutoff = now_ms - 21 * 86400000
        used = [p.get("keyword", "") for p in posts_all().values() if p.get("createdAt", 0) > cutoff and p.get("keyword")]
        log(f"자동 작성 시작: {n}편 · 1단계 최신 키워드 검색 (글쓰기 방식: {writer_kind()})")
        r = ask_claude(keyword_prompt(s, n, used), web=True)
        kws = (r.get("keywords") if isinstance(r, dict) else r) or []
        kws = [k for k in kws if isinstance(k, dict) and k.get("keyword")][:n]
        if not kws:
            raise WriterError("키워드를 고르지 못했습니다.")
        autorun_set(keywords=[k["keyword"] for k in kws], message="글 쓰는 중")
        log("  고른 키워드: " + ", ".join(k["keyword"] for k in kws))
        day = datetime.now().strftime("%Y%m%d")
        done = 0
        for i, k in enumerate(kws, 1):
            if k.get("cat") not in s["cats"]:
                k["cat"] = s["cats"][0]
            try:
                log(f"  2단계 {i}/{len(kws)} 글쓰기: {k['keyword']}")
                r = ask_claude(post_prompt(s, k), web=True)
                if not isinstance(r, dict) or not isinstance(r.get("body"), str):
                    raise WriterError("본문이 없습니다.")
                pid = f"a{day}-{i:02d}"
                while post_get(pid):
                    pid += "b"
                ms = int(time.time() * 1000)
                post_put({
                    "kind": "post", "id": pid, "createdAt": ms, "status": "review",
                    "topic": k.get("angle") or k["keyword"], "keyword": k["keyword"], "cat": k["cat"], "why": k.get("why", ""),
                    "minLen": s["minLen"], "nick": s["nick"], "titles": [str(t) for t in (r.get("titles") or [k["keyword"]])][:3],
                    "titleIdx": 0, "body": r["body"].replace("\r", "").strip(), "tags": [str(t).lstrip("#") for t in (r.get("tags") or [])][:12],
                    "thumb": str(r.get("thumb") or k["keyword"])[:40], "summary": [str(x) for x in (r.get("summary") or [])][:5],
                    "images": r.get("images") or [], "when": "", "pickedAt": 0, "error": "", "batch": "auto-" + day, "auto": True,
                    "sources": (r.get("sources") or k.get("sources") or [])[:8],
                })
                done += 1
                log(f"  {i}/{len(kws)} 저장: {k['keyword']}")
            except Exception as e:   # 한 편 실패해도 나머지는 계속
                log(f"  {i}/{len(kws)} 실패: {k['keyword']} - {e}")
            autorun_set(count=done, message=f"{done}/{len(kws)}편 완료")
        autorun_set(status="done", finishedAt=int(time.time() * 1000), count=done, message="검토·발행 탭에서 확인하세요")
        log(f"자동 작성 끝: {done}편")
    except Exception as e:
        log("자동 작성 실패:", e)
        autorun_set(status="failed", finishedAt=int(time.time() * 1000), message=str(e)[:120])
    finally:
        AUTORUN_BUSY.clear()


# ---------------------------------------------------------------- 네이버 발행 (Playwright)
JOBS = queue.Queue()


def browser_context(p, headless=None):
    PROFILE.mkdir(parents=True, exist_ok=True)
    hl = CFG.get("headless", False) if headless is None else headless
    if os.environ.get("BLOG_AUTO_HEADLESS"):
        hl = True
    args = dict(user_data_dir=str(PROFILE), headless=hl, locale="ko-KR", viewport={"width": 1400, "height": 950},
                args=["--disable-blink-features=AutomationControlled"])
    if os.environ.get("BLOG_AUTO_CHROME"):   # 테스트용
        return p.chromium.launch_persistent_context(executable_path=os.environ["BLOG_AUTO_CHROME"], **args)
    try:   # 설치된 크롬이 있으면 그걸 사용 (네이버가 덜 의심함)
        return p.chromium.launch_persistent_context(channel="chrome", **args)
    except Exception:
        try:
            return p.chromium.launch_persistent_context(**args)
        except Exception as e:
            if "Executable doesn't exist" in str(e):
                raise RuntimeError("자동화용 크롬이 설치되지 않았습니다. 설치.bat을 다시 실행하세요.")
            raise


def is_logged_in(ctx):
    return any(c.get("name") == "NID_AUT" for c in ctx.cookies("https://naver.com"))


def do_login():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = browser_context(p, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://nid.naver.com/nidlogin.login")
        log("열린 크롬 창에서 네이버에 직접 로그인하세요. (로그인 상태 유지 체크 권장) 최대 10분 기다립니다.")
        for _ in range(600):
            if is_logged_in(ctx):
                log("네이버 로그인 확인됨. 이 PC에 저장했습니다.")
                time.sleep(2)
                ctx.close()
                return True
            if not ctx.pages:
                break
            time.sleep(1)
        ok = is_logged_in(ctx)
        try:
            ctx.close()
        except Exception:
            pass
        return ok


def editor_frame(page, timeout=40):
    """SmartEditor가 들어 있는 frame을 찾는다 (mainFrame 안이거나 페이지 자체)."""
    end = time.time() + timeout
    while time.time() < end:
        for fr in page.frames:
            try:
                if fr.locator(".se-content").count() > 0:
                    return fr
            except Exception:
                pass
        time.sleep(0.5)
    raise RuntimeError("글쓰기 편집기를 찾지 못했습니다. (로그인이 풀렸거나 네이버 화면이 바뀜)")


def click_first(fr, selectors, timeout=5000):
    for sel in selectors:
        loc = fr.locator(sel).first
        try:
            loc.wait_for(state="visible", timeout=timeout)
            loc.click()
            return sel
        except Exception:
            continue
    return None


def type_lines(page, text):
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if ln:
            page.keyboard.type(ln, delay=2)
        if i < len(lines) - 1:
            page.keyboard.press("Enter")


def move_to_end(page, fr):
    paras = fr.locator(".se-component.se-text .se-text-paragraph")
    n = paras.count()
    if n:
        paras.nth(n - 1).click()
        page.keyboard.press("Control+End" if os.name == "nt" else "End")


def insert_image(page, fr, path):
    before = fr.locator(".se-component.se-image").count()
    with page.expect_file_chooser(timeout=15000) as fc:
        if not click_first(fr, ["button.se-image-toolbar-button", "button[data-name='image']", ".se-toolbar-item-image button"]):
            raise RuntimeError("사진 버튼을 찾지 못했습니다.")
    fc.value.set_files(str(path))
    end = time.time() + 60
    while time.time() < end and fr.locator(".se-component.se-image").count() <= before:
        time.sleep(0.5)
    time.sleep(1.0)
    move_to_end(page, fr)


def publish_post(job):
    """job = {id, title, tags, category, blocks:[{type:text|image, text|path}], mode}"""
    from playwright.sync_api import sync_playwright
    s = settings()
    blog_id = (s.get("blogId") or "").strip()
    if not blog_id:
        raise RuntimeError("블로그 아이디가 비어 있습니다. 앱의 블로그 설정에서 넣어 주세요.")
    mode = job.get("mode") or CFG.get("publish_mode", "publish")
    with sync_playwright() as p:
        ctx = browser_context(p)
        try:
            if not is_logged_in(ctx) and not os.environ.get("BLOG_AUTO_WRITE_URL"):
                raise RuntimeError("네이버 로그인이 필요합니다. '네이버 로그인.bat'을 실행해 직접 로그인하세요.")
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(os.environ.get("BLOG_AUTO_WRITE_URL") or f"https://blog.naver.com/{blog_id}?Redirect=Write&", wait_until="domcontentloaded")
            fr = editor_frame(page)
            time.sleep(2)
            # 이전에 쓰던 글 불러오기 팝업 → 취소, 도움말 → 닫기
            click_first(fr, [".se-popup-button-cancel"], timeout=2500)
            click_first(fr, [".se-help-panel-close-button"], timeout=1500)
            # 제목
            if not click_first(fr, [".se-documentTitle .se-text-paragraph", ".se-title-text", ".se-section-documentTitle"]):
                raise RuntimeError("제목 칸을 찾지 못했습니다.")
            page.keyboard.type(job["title"], delay=5)
            # 본문
            if not click_first(fr, [".se-component.se-text .se-text-paragraph", ".se-section-text .se-text-paragraph"]):
                raise RuntimeError("본문 칸을 찾지 못했습니다.")
            for b in job["blocks"]:
                if b["type"] == "text":
                    type_lines(page, b["text"])
                    page.keyboard.press("Enter")
                elif b["type"] == "image" and Path(b["path"]).exists():
                    insert_image(page, fr, b["path"])
            time.sleep(1)
            if mode == "draft":
                if not click_first(fr, ["button[class*='save_btn']", "button:has-text('저장')"]):
                    raise RuntimeError("저장 버튼을 찾지 못했습니다.")
                time.sleep(3)
                return {"ok": True, "url": "", "message": "임시저장 완료"}
            # 발행 패널
            if not click_first(fr, ["button[class*='publish_btn']", "button:has-text('발행')"]):
                raise RuntimeError("발행 버튼을 찾지 못했습니다.")
            time.sleep(1.5)
            cat = job.get("category")
            if cat:
                if click_first(fr, ["button[class*='selectbox_button']", "[class*='option_category'] button"], timeout=3000):
                    time.sleep(0.8)
                    if not click_first(fr, [f"label:has-text('{cat}')", f"span:has-text('{cat}')"], timeout=3000):
                        log(f"  카테고리 '{cat}'를 찾지 못해 기본 카테고리로 발행합니다.")
                        page.keyboard.press("Escape")
            tag_box = None
            for sel in ["#tag-input", "input[class*='tag_input']", "input[placeholder*='태그']"]:
                if fr.locator(sel).count():
                    tag_box = fr.locator(sel).first
                    break
            if tag_box:
                for t in job.get("tags", [])[:30]:
                    tag_box.click()
                    page.keyboard.type(t, delay=5)
                    page.keyboard.press("Enter")
                    time.sleep(0.15)
            if not click_first(fr, ["button[class*='confirm_btn']", "[class*='layer_btn_area'] button:has-text('발행')"]):
                raise RuntimeError("발행 확인 버튼을 찾지 못했습니다.")
            for _ in range(40):
                time.sleep(0.5)
                if re.search(r"logNo=\d+|/\d{9,}", page.url):
                    break
            return {"ok": True, "url": page.url, "message": "발행 완료"}
        except Exception as e:
            shot = LOGS / f"fail_{job['id']}_{datetime.now():%H%M%S}.png"
            try:
                ctx.pages[0].screenshot(path=str(shot), full_page=False)
            except Exception:
                pass
            raise RuntimeError(f"{e} (화면: {shot.name})")
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def publish_worker():
    while True:
        kind, payload = JOBS.get()
        try:
            if kind == "login":
                do_login()
            elif kind == "publish":
                pid = payload["id"]
                post_patch(pid, pub={**(post_get(pid) or {}).get("pub", {}), "state": "running", "at": int(time.time() * 1000)})
                log(f"발행 시작: {payload['title']}")
                try:
                    res = publish_post(payload)
                    old = (post_get(pid) or {}).get("pub", {})
                    post_patch(pid, status="published" if payload.get("mode") != "draft" else (post_get(pid) or {}).get("status"),
                               publishedAt=int(time.time() * 1000),
                               pub={**old, "state": "done", "url": res.get("url", ""), "message": res["message"], "at": int(time.time() * 1000)})
                    log(f"  {res['message']} {res.get('url','')}")
                except Exception as e:
                    first = str(e).strip().splitlines()[0] if str(e).strip() else "알 수 없는 오류"
                    log("  발행 실패:", first)
                    old = (post_get(pid) or {}).get("pub", {})
                    post_patch(pid, pub={**old, "state": "failed", "message": first[:300], "at": int(time.time() * 1000)})
        except Exception:
            log(traceback.format_exc())


def build_job(pid, mode):
    p = post_get(pid)
    if not p:
        return None
    pub = p.get("pub") or {}
    files = pub.get("images") or {}
    blocks, buf, n = [], [], 0

    def flush():
        if buf:
            blocks.append({"type": "text", "text": "\n".join(buf).strip("\n")})
            buf.clear()

    if files.get("thumb"):
        blocks.append({"type": "image", "path": files["thumb"]})
    summary_done = False
    for ln in p.get("body", "").split("\n"):
        if re.match(r"^\s*\[(사진|캡처)\s*\d*\s*:[^\]]*\]\s*$", ln):
            n += 1
            f = files.get(str(n))
            if f:
                flush()
                blocks.append({"type": "image", "path": f})
                continue
        if not summary_done and ln.startswith("확인일") and files.get("summary"):
            flush()
            blocks.append({"type": "image", "path": files["summary"]})
            summary_done = True
        buf.append(ln)
    flush()
    if not summary_done and files.get("summary"):
        blocks.append({"type": "image", "path": files["summary"]})
    titles = p.get("titles") or [p.get("topic", "")]
    return {"id": pid, "title": titles[min(p.get("titleIdx", 0), len(titles) - 1)], "tags": p.get("tags", []),
            "category": p.get("cat", ""), "blocks": blocks, "mode": mode}


def scheduler():
    last_auto = None
    while True:
        try:
            now = datetime.now()
            # 매일 자동 작성
            if CFG.get("auto_write") and now.strftime("%H:%M") == CFG.get("auto_write_time", "05:47") and last_auto != now.date():
                last_auto = now.date()
                autorun()
            # 예약 시간이 된 글 발행
            if CFG.get("auto_publish"):
                for p in posts_all().values():
                    pub = p.get("pub") or {}
                    if pub.get("state") != "waiting" or not p.get("when"):
                        continue
                    try:
                        due = datetime.strptime(p["when"][:16], "%Y-%m-%dT%H:%M")
                    except ValueError:
                        continue
                    if due <= now:
                        job = build_job(p["id"], pub.get("mode", "publish"))
                        if job:
                            post_patch(p["id"], pub={**pub, "state": "queued"})
                            JOBS.put(("publish", job))
        except Exception:
            log(traceback.format_exc())
        time.sleep(20)


# ---------------------------------------------------------------- 웹 서버 (검토 앱 + API)
def save_images(pid, images):
    """앱에서 그린 이미지(dataURL)를 PNG 파일로 저장."""
    d = IMAGES / pid
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    out = {}
    for key, url in (images or {}).items():
        m = re.match(r"data:image/(png|jpeg);base64,(.+)", url or "")
        if not m or not re.match(r"^(thumb|summary|\d{1,2})$", str(key)):
            continue
        f = d / f"{key}.{'png' if m.group(1) == 'png' else 'jpg'}"
        f.write_bytes(base64.b64decode(m.group(2)))
        out[str(key)] = str(f)
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "BlogAuto/1.0"

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 60 * 1024 * 1024:
            raise ValueError("too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def authed(self):
        if self.client_address[0] in ("127.0.0.1", "::1"):
            return True
        return secrets.compare_digest(self.headers.get("X-Pin", ""), CFG["pin"])

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self.send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        if not path.startswith("/api/"):
            return self.send(404, {"error": "not found"})
        if path == "/api/ping":
            return self.send(200, {"ok": True, "pc": True, "auth": self.authed()})
        if not self.authed():
            return self.send(401, {"error": "pin"})
        if path == "/api/posts":
            return self.send(200, list(posts_all().values()))
        if path == "/api/autorun":
            return self.send(200, read_json(AUTORUN_PATH, {}))
        if path == "/api/status":
            return self.send(200, {"writer": writer_kind(), "autoWrite": CFG.get("auto_write"), "autoWriteTime": CFG.get("auto_write_time"),
                                   "postsPerDay": CFG.get("posts_per_day", 4), "autoPublish": CFG.get("auto_publish"), "publishMode": CFG.get("publish_mode"),
                                   "loggedIn": (PROFILE / "Default").exists(), "busy": AUTORUN_BUSY.is_set()})
        return self.send(404, {"error": "not found"})

    def do_PUT(self):
        path = urlparse(self.path).path
        if not self.authed():
            return self.send(401, {"error": "pin"})
        try:
            data = self.body()
        except ValueError:
            return self.send(400, {"error": "bad json"})
        m = re.match(r"^/api/posts/([A-Za-z0-9_\-]+)$", path)
        if m and isinstance(data, dict):
            data["id"] = m.group(1)
            data.pop("pub", None)   # 발행 상태는 서버만 바꿈
            return self.send(200, post_put(data))
        if path == "/api/config" and isinstance(data, dict):
            with LOCK:
                if "auto_write" in data:
                    CFG["auto_write"] = bool(data["auto_write"])
                if "posts_per_day" in data:
                    CFG["posts_per_day"] = max(1, min(10, int(data["posts_per_day"] or 4)))
                if re.match(r"^\d{2}:\d{2}$", str(data.get("auto_write_time", ""))):
                    CFG["auto_write_time"] = data["auto_write_time"]
                write_json(CONFIG_PATH, CFG)
            return self.send(200, {"ok": True, "autoWrite": CFG["auto_write"], "postsPerDay": CFG["posts_per_day"], "autoWriteTime": CFG["auto_write_time"]})
        if path == "/api/settings" and isinstance(data, dict):
            keep = {k: data[k] for k in ("cats", "persona", "nick", "blogId", "tone", "minLen", "imgCount", "emoji") if k in data}
            write_json(SETTINGS_PATH, {**read_json(SETTINGS_PATH, {}), **keep})
            return self.send(200, {"ok": True})
        return self.send(404, {"error": "not found"})

    def do_DELETE(self):
        if not self.authed():
            return self.send(401, {"error": "pin"})
        m = re.match(r"^/api/posts/([A-Za-z0-9_\-]+)$", urlparse(self.path).path)
        if m:
            post_delete(m.group(1))
            return self.send(200, {"ok": True})
        return self.send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self.authed():
            return self.send(401, {"error": "pin"})
        try:
            data = self.body()
        except ValueError:
            return self.send(400, {"error": "bad json"})
        if path == "/api/ask":
            try:
                return self.send(200, {"ok": True, "data": ask_claude(str(data.get("prompt", "")), web=bool(data.get("web")))})
            except Exception as e:
                return self.send(200, {"ok": False, "error": str(e)[:300]})
        if path == "/api/autorun":
            started = autorun(data.get("count"))
            return self.send(200, {"ok": started, "error": "" if started else "이미 쓰는 중입니다."})
        if path == "/api/login":
            JOBS.put(("login", {}))
            return self.send(200, {"ok": True})
        if path == "/api/publish":
            pid = str(data.get("id", ""))
            p = post_get(pid)
            if not p:
                return self.send(404, {"error": "글이 없습니다."})
            mode = data.get("mode", "schedule")   # schedule | now | draft | cancel
            if mode == "cancel":
                post_patch(pid, pub={})
                return self.send(200, {"ok": True})
            files = save_images(pid, data.get("images"))
            pmode = "draft" if mode == "draft" else CFG.get("publish_mode", "publish")
            if mode == "schedule":
                if not p.get("when"):
                    return self.send(200, {"ok": False, "error": "먼저 발행 시간을 정하세요."})
                post_patch(pid, pub={"state": "waiting", "mode": pmode, "images": files, "at": int(time.time() * 1000)})
            else:
                post_patch(pid, pub={"state": "queued", "mode": pmode, "images": files, "at": int(time.time() * 1000)})
                JOBS.put(("publish", build_job(pid, pmode)))
            return self.send(200, {"ok": True})
        return self.send(404, {"error": "not found"})


def local_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


def diagnose():
    print("== 진단 시작 ==")
    print("파이썬:", sys.version.split()[0])
    print("글쓰기 방식:", writer_kind())
    exe = find_claude()
    print("claude 위치:", exe or "못 찾음")
    if exe:
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        print("Claude Code에 짧은 질문을 보내는 중… (최대 3분)")
        t0 = time.time()
        try:
            r = subprocess.run([exe, "-p", "--output-format", "json"], input='"확인"이라는 한 단어만 답해.', capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=180, cwd=str(DATA), env=env)
            print(f"  끝난 시간: {time.time() - t0:.0f}초 / 종료 코드: {r.returncode}")
            print("  응답:", (r.stdout or "").strip()[:400])
            if r.stderr.strip():
                print("  오류 출력:", r.stderr.strip()[:600])
        except subprocess.TimeoutExpired:
            print("  3분 동안 응답이 없습니다. 명령 프롬프트에서 claude 를 한 번 실행해 로그인·첫 설정 화면이 남아 있는지 확인하세요.")
    try:
        from playwright.sync_api import sync_playwright  # noqa
        print("playwright: 설치됨")
    except ImportError:
        print("playwright: 없음 → 설치.bat을 다시 실행하세요")
    print("네이버 로그인 프로필:", "있음" if (PROFILE / "Default").exists() else "없음 → 네이버 로그인.bat 실행")
    print("최근 기록 (data/logs/blog_auto.log):")
    try:
        print("".join((LOGS / "blog_auto.log").read_text(encoding="utf-8").splitlines(True)[-15:]))
    except OSError:
        print("  (기록 없음)")
    print("== 진단 끝 == 이 창을 캡처해서 보내 주세요.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="네이버 로그인 창만 열기")
    ap.add_argument("--write-now", type=int, metavar="N", help="지금 N편 자동 작성 후 종료")
    ap.add_argument("--diag", action="store_true", help="문제 진단")
    a = ap.parse_args()
    if a.diag:
        diagnose()
        return
    if a.login:
        sys.exit(0 if do_login() else 1)
    if a.write_now:
        autorun(a.write_now)
        while AUTORUN_BUSY.is_set():
            time.sleep(1)
        return
    if not (WEB / "index.html").exists():
        log("web/index.html이 없습니다. 파일을 모두 받았는지 확인하세요.")
        sys.exit(1)
    threading.Thread(target=publish_worker, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
    port = int(CFG.get("port", 8765))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("=" * 60)
    print(" 네이버 블로그 자동 글쓰기 (PC) 실행 중  -  이 창을 닫으면 멈춥니다")
    print(f"  이 PC에서:      http://localhost:{port}")
    for ip in local_ips():
        print(f"  아이패드에서:   http://{ip}:{port}   (같은 와이파이 또는 Tailscale)")
    print(f"  접속 비밀번호:  {CFG['pin']}")
    print(f"  글쓰기 방식:    {writer_kind()}   /  매일 자동 작성 {CFG.get('auto_write_time') if CFG.get('auto_write') else '꺼짐 (앱의 버튼이나 지금 글쓰기.bat으로 작성)'}")
    print("=" * 60, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
