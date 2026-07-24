# Secure Coding — Tiny Second-hand Shopping Platform

중고거래 플랫폼을 보안 중심으로 재구현한 프로젝트입니다. 회원가입/로그인부터 상품 거래, 송금, 1:1/전체 채팅, 신고 자동조치, 관리자 기능까지 실제 서비스에 준하는 흐름을 갖추고 있으며, 각 기능에 OWASP Top 10 기준의 방어(인증/인가, CSRF, XSS, 세션 관리, Rate Limiting 등)를 적용하는 데 초점을 맞췄습니다.

## 배포 URL

**https://secure-coding-second-market.onrender.com**

Render 무료 티어로 배포되어 있어 **15분간 요청이 없으면 서버가 슬립 상태로 전환**됩니다. 슬립 이후 첫 접속은 서버가 깨어나는 데 최대 약 60초 정도 걸릴 수 있습니다 (콜드 스타트). 첫 요청이 느리더라도 정상이며, 이후 요청부터는 빠르게 응답합니다.

## 로컬 실행 방법

두 가지 방법 중 하나를 선택하면 됩니다 (둘 다 동일한 앱을 실행하며, 실제로 설치·실행까지 확인했습니다).

### 방법 A. pip (venv + requirements.txt)

```bash
git clone https://github.com/mallangmollangquokka/secure-coding.git
cd secure-coding
python -m venv venv
# Windows: venv\Scripts\activate
# macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# .env 파일을 열어 아래 "env 설정 안내"대로 값을 채운 뒤
python app.py
```

서버가 뜨면 `http://127.0.0.1:5000` 으로 접속합니다.

### 방법 B. conda (enviroments.yaml)

```bash
git clone https://github.com/mallangmollangquokka/secure-coding.git
cd secure-coding
conda env create -f enviroments.yaml
conda activate secure_coding
cp .env.example .env
# .env 파일을 열어 아래 "env 설정 안내"대로 값을 채운 뒤
python app.py
```

`enviroments.yaml`에는 `requirements.txt`와 동일한 패키지·버전이 명시되어 있어 두 방법 모두 같은 의존성으로 실행됩니다.

## .env 설정 안내

민감한 설정값은 코드에 하드코딩하지 않고 `.env` 파일에서 읽어옵니다. 저장소에는 실제 값 대신 `.env.example`만 포함되어 있으므로, 이를 복사해서 `.env`를 만들고 값을 채워야 합니다.

```bash
cp .env.example .env
```

`.env`에 필요한 키는 다음과 같습니다 (실제 값은 이 문서에 적지 않습니다 — 각자 환경에서 직접 채워주세요).

| 키 | 용도 |
|---|---|
| `SECRET_KEY` | Flask 세션 서명, CSRF 토큰 생성에 사용되는 비밀키. 랜덤한 긴 문자열이어야 하며, `python -c "import secrets; print(secrets.token_hex(32))"` 로 생성할 수 있습니다. |
| `FLASK_DEBUG` | 로컬 개발 중에는 `true`, 그 외(배포 등)에는 반드시 `false`. `true`일 때만 세션 쿠키의 `Secure` 플래그가 꺼지고(HTTP 개발 편의), HSTS 헤더가 비활성화됩니다. |
| `ADMIN_USERNAME` | 서버 최초 기동 시 자동 생성되는 관리자 계정의 아이디. |
| `ADMIN_PASSWORD` | 위 관리자 계정의 비밀번호. bcrypt로 해싱되어 저장되며, 이미 해당 username의 계정이 있으면 아무 동작도 하지 않습니다(멱등적). |

`.env` 파일이 없거나 `SECRET_KEY` 값이 비어 있는 상태로 `python app.py`를 실행하면, 스택 트레이스 없이 아래처럼 설정 방법을 안내하는 메시지를 출력하고 종료합니다.

```
[설정 오류] SECRET_KEY가 설정되지 않았습니다.
  1. .env.example을 복사해 .env 파일을 만드세요:
     cp .env.example .env
  2. .env 파일을 열어 SECRET_KEY에 랜덤한 값을 채우세요. 예:
     python -c "import secrets; print(secrets.token_hex(32))"
  3. 서버를 다시 실행하세요:
     python app.py
```

## 관리자 계정 안내

`.env`의 `ADMIN_USERNAME` / `ADMIN_PASSWORD`로 일반 로그인 화면에서 로그인하면 관리자 계정으로 접속됩니다. 로그인 후 상단 네비게이션에 "관리자" 링크가 노출되며, `/admin`에서 신고 내역·자동조치 내역·회원/상품 목록 조회 및 정지·차단·잔액지급·복구 등의 관리 기능을 사용할 수 있습니다.

## 채점자를 위한 기능 확인 가이드

일부 기능은 계정 1개만으로는 확인이 어렵습니다. 아래 순서대로 확인하는 것을 권장합니다.

1. **계정 2개 준비**: 1:1 채팅은 상대방이 있어야 확인 가능합니다. 상대방의 상품 상세 페이지 또는 공개 프로필(`/user/<id>`)에 있는 "1:1 문의하기" / "1:1 채팅하기" 버튼으로 대화를 시작할 수 있습니다.
2. **송금 테스트는 잔액이 필요합니다**: 신규 가입 계정의 초기 잔액은 0원입니다. 관리자 계정으로 로그인 → `/admin` → 잔액을 지급할 사용자에게 금액 지급 후, 그 계정으로 `/transfer`에서 송금을 테스트하세요.
3. **신고 자동 차단은 서로 다른 계정 5개가 필요합니다**: 같은 대상(상품 또는 사용자)을 서로 다른 계정 5개로 신고해야 5번째 신고 시점에 자동 차단/정지가 발동합니다. 같은 계정으로 반복 신고하면 중복 신고로 거부됩니다.

## 구현 범위 요약

- **회원가입/로그인**: 서버측 입력 검증, bcrypt 비밀번호 해싱, 로그인 5회 실패 시 계정 잠금, 세션 만료(30분), 로그아웃 시 세션 무효화
- **프로필/비밀번호**: bio 수정, 비밀번호 변경(현재 비밀번호 재인증 + 최근 5개 재사용 방지), 공개 프로필 페이지
- **상품**: 등록/수정/삭제(soft delete)/검색/이미지 업로드(Pillow로 재인코딩), 소유자 검증
- **송금**: 비밀번호 재인증 + `BEGIN IMMEDIATE` 트랜잭션 기반 원자적 잔액 이동, 동시 요청에 대한 레이스 컨디션 방지
- **1:1 채팅 / 전체 채팅**: Socket.IO 기반 실시간 메시지, 소켓 이벤트마다 계정 상태(정지 여부) 재검증, 서버측 payload 재구성(클라이언트가 보낸 사용자명 무시), 5초/5회 rate limiting, 메시지 DB 영구 저장, `textContent` 렌더링으로 클라이언트측 XSS 방지
- **신고**: 자기 자신/본인 상품 신고 차단, 중복 신고 차단, 신고 5회 누적 시 대상 자동 차단(상품)/정지(사용자) 및 세션 무효화
- **관리자**: 매 요청 DB에서 role 재조회하는 인가, 사용자 정지/해제/잔액지급, 상품 차단/복구, 모든 조치 감사 로그 기록
- **공통 보안 조치**: CSRF 보호(모든 상태변경 요청), 보안 헤더 6종(CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy, HSTS), Rate Limiting(회원가입/로그인/송금/신고 등 라우트별), 접근/조작 감사 로그(`audit_log`), 민감정보(비밀번호/해시/세션 토큰) 미노출

## 문서 목록

저장소에 포함된 설계/작업 문서입니다.

- `spec.md` — 상세 설계 스펙 (DB 스키마, 라우트별 요구사항 등)
- `secure_coding_checklist.csv` — 보안 점검 체크리스트
- `phase1_prompt.md` / `Phase2a_prompt.md` / `Phhase2bc prompt.md` — 단계별(Phase 1 / 2A / 2B+2C) 구현 지시서. 어떤 순서로, 어떤 요구사항에 맞춰 기능이 추가되었는지 확인할 때 참고할 수 있습니다.
