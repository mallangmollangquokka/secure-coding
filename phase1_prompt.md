You are working on the `ugonfor/secure-coding` Flask repo (Tiny Second-hand Shopping Platform).
The full design spec is in `spec.md` in this repo root — read it fully before starting, especially §0 (현재 코드 원본), §1 (DB 마이그레이션), §3 / §3-1 / §3-2 / §3-3 (보안 보강 작업 목록).

## 지금 작업 범위 (Phase 1: 기존 취약점 수정 + 보안 기반 인프라만)

**이번 작업에서는 아래 항목만 처리한다. 상품 수정/삭제/검색, 1:1채팅, 송금, 관리자 기능, 이미지 업로드는 이번 범위 아님 — 절대 손대지 말 것 (다음 Phase에서 진행).**

기존 기능(회원가입/로그인/로그아웃/대시보드/프로필bio/상품등록/상품조회/신고/전체채팅)의 동작은 그대로 유지하면서, 아래 보안 결함만 고친다.

### A. 환경변수 / 설정 분리
1. `.env.example`과 `.env` 생성. `.env`에 `SECRET_KEY`(랜덤 생성), `FLASK_DEBUG=false`, `ADMIN_USERNAME`, `ADMIN_PASSWORD` 정의
2. `app.py`에서 `app.config['SECRET_KEY'] = 'secret!'` 하드코딩 제거 → `python-dotenv`로 `.env` 로드 후 `os.environ['SECRET_KEY']` 사용
3. `socketio.run(app, debug=True)` → `debug=os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'`
4. `.gitignore`에 `.env` 추가 (이미 `*.db`는 있음, 확인만)
5. `chmod 600 .env`, `chmod 600 market.db` 되도록 README에 안내 문구 추가 (코드로 강제하기보단 문서화). **주의: 이 항목은 로컬/자체 서버 배포에만 해당. Render 같은 PaaS 배포 시에는 `.env` 파일 자체를 올리지 않고 플랫폼의 환경변수 대시보드에 직접 입력하므로 해당 사항 없음**
6. **[배포 필수, 치명적] `init_db()` 호출을 `if __name__ == '__main__':` 블록 안에서 밖으로, 모듈 최상단(앱 객체 생성 직후)으로 이동시킬 것.** 현재처럼 `if __name__ == '__main__':` 블록 안에서만 호출하면, gunicorn이 `app.py`를 모듈로 import해서 실행하는 배포 환경(`__name__`이 `'__main__'`이 아님)에서는 이 블록 자체가 실행되지 않아 `init_db()`가 전혀 호출되지 않고 테이블/마이그레이션/관리자 자동시딩이 다 빠진 채로 앱이 뜸. `if __name__ == '__main__':` 블록에는 `socketio.run(...)` 로컬 실행 부분만 남길 것. **`init_db()` 내부의 `CREATE TABLE IF NOT EXISTS`와 컬럼별 개별 `ALTER TABLE` try/except는 각자 독립적으로 두되(§B-8 참고), `init_db()` 함수 호출 자체를 블랭킷 try/except로 감싸서 에러를 조용히 삼키지 말 것 — 진짜 치명적인 초기화 실패는 로그에 드러나야 원인을 찾을 수 있음**

### B. 인증/비밀번호 보안
7. `bcrypt` 설치 및 적용: `register()`에서 비밀번호를 `bcrypt.hashpw()`로 해싱 후 저장, `login()`에서 `bcrypt.checkpw()`로 검증
8. `user` 테이블에 마이그레이션으로 컬럼 추가: `role`, `status`, `failed_login_count`, `locked_until`, `session_token`, `created_at` (spec.md §1 SQL 그대로 사용). **각 `ALTER TABLE ADD COLUMN` 구문은 반드시 개별적으로 독립된 `try: ... except sqlite3.OperationalError: pass` 블록으로 감쌀 것 — 절대로 여러 ALTER 문을 하나의 try 블록에 몰아넣지 말 것.** (하나의 try에 몰아넣으면 먼저 실행된 컬럼이 이미 존재해서 예외가 터지는 순간, 뒤에 있는 나머지 컬럼들은 전혀 추가되지 않는 채로 조용히 넘어가버리는 버그가 생김)
9. 로그인 실패 시 `failed_login_count` 증가, 5회 이상이면 `locked_until`을 현재시각+15분으로 설정. 로그인 시 `locked_until`이 미래면 로그인 거부(잠김 안내). 로그인 성공 시 `failed_login_count`를 0으로 리셋. **날짜/시간은 SQLite에 타입이 없고 TEXT 비교로 처리되므로, `locked_until`/`created_at` 등 모든 시각 값은 반드시 `datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')` 형식으로 통일해서 저장·비교할 것 (포맷이나 타임존이 흔들리면 문자열 비교가 깨져서 영원히 잠기거나 아예 안 잠기는 버그 발생)**
10. 로그인/회원가입 실패 메시지는 "아이디 또는 비밀번호가 올바르지 않습니다" 통합 문구 유지 (아이디 존재 여부 유추 방지, 이미 되어 있음 — 회귀 확인만)
11. 로그인 성공 시 `session_token`을 새 uuid4로 발급해서 DB에 저장 + `session['session_token']`에도 저장

### C. 서버측 입력 검증
12. `register()`: username 4~20자 영숫자(+`_`)만 허용, password 최소 8자. 위반 시 flash로 안내 후 폼 재표시(500 아님)
13. `new_product()`: title 1~100자, description 1~2000자, price는 정수 변환 시도 → 실패하거나 음수면 flash 안내 후 재표시

### D. 세션 / 인가 기반 인프라
14. `login_required` 데코레이터 신설(기존엔 각 라우트에 `if 'user_id' not in session` 반복되던 걸 통합): 세션의 `user_id` 존재 확인 + DB 조회해서 `status == 'active'` 확인 + `session['session_token'] == user['session_token']` 일치 확인. 셋 중 하나라도 실패하면 세션 pop 후 로그인 페이지로 redirect. 기존 라우트(`dashboard`, `profile`, `new_product`, `report`)에 이 데코레이터 적용
15. **리버스 프록시 대응**: `werkzeug.middleware.proxy_fix.ProxyFix`를 적용 — `app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)`. Render 등 PaaS는 리버스 프록시 뒤에서 컨테이너로 평문 HTTP로 요청을 전달하므로, 이게 없으면 Flask가 `request.is_secure`나 클라이언트 IP를 잘못 판단할 수 있음. **주의: `SESSION_COOKIE_SECURE`/`SESSION_COOKIE_SAMESITE` 설정 자체는 그대로 유지(§D-16 참고) — 이 프록시 대응이 필요하다고 해서 쿠키 보안 설정을 완화하지 말 것**
16. `app.config['SESSION_COOKIE_SECURE']`는 `FLASK_DEBUG`가 false일 때만 True (로컬 http 개발 편의성 유지), `SESSION_COOKIE_SAMESITE='Lax'`, `PERMANENT_SESSION_LIFETIME=timedelta(minutes=30)`, `app.permanent_session_lifetime` 적용 + 로그인 시 `session.permanent = True`
17. `/logout`을 `methods=['POST']`로 변경. **`session.clear()`로 클라이언트 세션을 비우는 것에 더해, DB에서 해당 유저의 `session_token`을 즉시 `NULL`로 업데이트할 것** (안 그러면 로그아웃 이후에도 탈취된 옛 쿠키/토큰으로 재사용 공격이 가능함). `base.html`의 `<a href="{{ url_for('logout') }}">로그아웃</a>`을 `<form method="post" action="{{ url_for('logout') }}"><button type="submit">로그아웃</button></form>`로 교체 (CSS는 nav 안에서 버튼이 링크처럼 보이도록 `.nav-form-btn` 클래스 하나만 최소 추가)

### E. CSRF
18. `Flask-WTF`의 `CSRFProtect(app)` 적용
19. `register.html`, `login.html`, `profile.html`, `new_product.html`, `report.html`, 새로 만든 logout form 전부에 `<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">` 추가

### F. 채팅 인증
20. `send_message` 소켓 핸들러 진입 시 `flask.session`(Socket.IO는 Flask 세션 공유 가능)에서 `user_id` 확인, 없으면 메시지 무시(disconnect 또는 그냥 return). 메시지 길이 500자 제한, 빈 문자열/공백만 있으면 무시. **저장/브로드캐스트 전에 이스케이프하지 말 것** — 원본 그대로 전달, 렌더링은 기존처럼 클라이언트의 `textContent` 사용(이미 안전)

### G. 에러 처리 / 보안 헤더
21. `@app.errorhandler(400)`, `403`, `404`, `500` 각각 등록해서 간단한 커스텀 템플릿(또는 flash+redirect) 반환, 스택트레이스 노출 금지
22. `after_request` 훅으로 응답 헤더 추가: `Content-Security-Policy` (socket.io CDN 스크립트 허용하도록 `script-src`에 `https://cdnjs.cloudflare.com` 포함, **그리고 웹소켓 연결이 차단되지 않도록 `connect-src 'self' ws: wss:;`를 반드시 함께 포함** — `connect-src`를 안 열어주면 CSP가 Socket.IO 연결 자체를 막아서 채팅 기능이 통째로 먹통이 됨), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`

### H. 의존성
23. `requirements.txt` 생성(버전 고정): `flask`, `flask-socketio`, `bcrypt`, `flask-wtf`, `python-dotenv`, **`gunicorn`, `eventlet`**(운영 배포용 WSGI 서버 및 Socket.IO 워커) 등 현재 설치된 버전으로 `==` 고정
24. `enviroments.yaml`은 그대로 두되 `requirements.txt`를 pip 설치 기준으로 병행 제공 (README에 두 가지 설치 방법 모두 안내)

### I. Render 배포 대응
25. README.md에 Render 배포 섹션 추가: Build Command `pip install -r requirements.txt`, Start Command `gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:$PORT app:app` (포트 바인딩 누락 시 서비스가 응답 없음 상태가 되므로 `--bind 0.0.0.0:$PORT` 반드시 포함), 환경변수는 Render 대시보드에 직접 입력(`.env` 파일 업로드 아님)
26. README.md에 다음 캐비어트 명시: Render 무료 티어는 디스크가 비영속적(ephemeral)이라 재배포/재시작 시 `market.db`와 `static/uploads/products/`의 업로드 이미지가 초기화될 수 있음 — 이번 프로젝트는 과제 데모 목적이라 이 리스크를 감수하는 것으로 결정함(spec.md §6)

## 중요 지시사항 (코드 생략 금지)
코드를 작성/수정할 때 절대로 `# ... 기존 코드와 동일 ...`, `// TODO`, `(생략)` 같은 생략 표기법을 사용하지 말 것. 변경이 필요한 함수나 파일은 생략 없이 전체를 온전한 코드로 출력할 것 (긴 파일이라도 마찬가지).

## 완료 조건 (스스로 검증할 것)
- 기존 기능(회원가입~채팅) 전부 회귀 없이 동작하는지 수동으로 각 라우트 호출해서 확인
- 로그인 5회 실패 시 잠기는지, 15분 뒤(또는 테스트를 위해 잠깐 시간 조작) 풀리는지 확인
- CSRF 토큰 없이 curl로 POST 요청 보내면 403 뜨는지 확인
- `.env` 없이 실행하면 명확한 에러로 죽는지(조용히 기본값으로 넘어가지 않는지) 확인
- 변경 파일 목록과 각 변경이 spec.md의 어느 항목에 대응하는지 커밋 메시지 또는 요약으로 정리해서 보고
- (Render 배포 후) 실제 배포 URL에서 로그인 → 다른 페이지 이동 → 새로고침까지 해봤을 때 세션이 유지되는지 확인 (ProxyFix/쿠키 설정이 실제 프록시 환경에서 문제없이 동작하는지 최종 검증)

## 하지 말 것
- 상품 수정/삭제/검색, 1:1 채팅, 송금, 관리자 기능, 이미지 업로드 — 이번 스코프 아님
- `report` 테이블에 `target_type` 컬럼 추가하는 것도 이번 스코프 아님 (다음 Phase에서 신고 기능 확장할 때 진행)
- 채팅 메시지를 저장 전에 이스케이프하는 것 (이중 인코딩 버그 유발, spec.md §3-14 참고)
- CSS 프레임워크(Bootstrap/Tailwind) 도입 — 기존 인라인 스타일만 최소 확장
