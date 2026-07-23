You are working on the `mallangmollangquokka/secure-coding` Flask repo (Tiny Second-hand Shopping Platform).
Phase 1은 이미 완료·배포되어 있음. `spec.md`, `phase1_prompt.md`, 현재 `app.py`, `security.py`, `templates/`, `static/js/chat.js` 상태를 모두 읽은 뒤에 시작할 것.

## 지금 작업 범위 (Phase 2A: 상품 확장 + 사용자 확장 + 보안 강화 인프라)

**이번 작업에서는 아래 항목만 처리한다. 신고 자동조치, 관리자 대시보드, 송금, 1:1 채팅은 이번 범위 아님 — 절대 손대지 말 것 (Phase 2B/2C에서 진행).**

Phase 1이 이미 확립한 것들(bcrypt 해싱, CSRF, session_token, ProxyFix, CSP, 커스텀 에러 핸들러, 관리자 자동시딩 등)의 원칙과 스타일을 그대로 유지하면서, 아래 기능을 추가한다.

---

### A. 공용 검증 모듈 확장 (`security.py`)

1. 기존 `validate_password(password)`와 동일한 시그니처/반환 스타일(에러 리스트 반환, 통과 시 빈 리스트)로 아래 함수들을 추가:
   - `validate_username(username)` — 정규식 `^[A-Za-z0-9_]{4,20}$` 검증. 현재 `app.py`의 `USERNAME_RE`를 이 함수로 대체하고 `app.py`에서 import하도록 리팩터. **동작 로직은 그대로**, 위치만 옮기는 것.
   - `validate_product_title(title)` — 1~100자
   - `validate_product_description(description)` — 1~2000자
   - `validate_product_price(price_raw)` — `str` 입력 받아 int 변환 시도, 실패하거나 음수면 에러 반환. 성공 시 `(errors, parsed_int)` 튜플 반환 (다른 validator와 반환 형식 다르지만, 파싱 결과가 필요하므로 예외 허용)
2. 상수도 여기에 몰아둘 것: `USERNAME_RE`, `TITLE_MAX`, `DESCRIPTION_MAX`, `PASSWORD_MIN` 등
3. `security.py`에 신규 데코레이터 `owner_required(get_owner_id_fn)` 추가:
   - **구현 방식**: 데코레이터 내부에서 `flask.request.view_args`를 통해 라우트 매개변수(예: `product_id`)를 꺼내 `get_owner_id_fn(**request.view_args)` 형태로 소유자 ID를 조회한다. 라우트 함수의 인자 순서/이름에 의존하지 말 것 (`*args`, `**kwargs`를 직접 파싱하는 건 취약함)
   - DB에서 소유자 id를 조회하고, `session['user_id']`와 비교
   - 불일치 시 403 반환
   - 예: `@owner_required(lambda product_id: get_product_seller_id(product_id))`
   - `login_required`와 조합해서 사용 (login_required가 먼저 적용)
   - **주의**: 리소스가 존재하지 않으면 (404) 처리를 이 데코레이터에서 하지 말고 라우트 본체에서 처리 — 데코레이터는 인가 여부만 판단. `get_owner_id_fn`이 None을 반환하면(리소스 없음) 데코레이터는 그대로 통과시켜서 라우트 본체가 404를 처리하게 함

### B. DB 마이그레이션

4. **product 테이블 재생성 (price TEXT → INTEGER)**: spec.md §1의 SQL 그대로 사용. 마이그레이션 실행 전 아래 가드 넣을 것:
   - 현재 `PRAGMA table_info(product)` 결과에서 price 컬럼 타입이 이미 `INTEGER`이면 스킵 (재실행 안전성)
   - 새 컬럼 추가: `status TEXT DEFAULT 'active'`, `report_count INTEGER DEFAULT 0`, `image_filename TEXT`, `created_at TEXT DEFAULT CURRENT_TIMESTAMP`
   - 기존 데이터 이관 시 `CAST(price AS INTEGER)` 사용, CAST 결과가 NULL/음수면 0으로 clamp
5. **user 테이블 확장**:
   - `password_history TEXT DEFAULT '[]'` 컬럼 추가 — JSON 배열로 이전 비밀번호 해시들 저장 (최대 5개)
   - `last_login_at TEXT DEFAULT NULL` 컬럼 추가 — 접근 로그 표시용 편의 컬럼
6. **신규 테이블 `audit_log`** 생성 (접근 로그, 강화 항목 (d)):
   ```sql
   CREATE TABLE IF NOT EXISTS audit_log (
       id TEXT PRIMARY KEY,
       actor_id TEXT,                  -- NULL 가능 (예: 비로그인 로그인 시도)
       actor_username TEXT,            -- 조회 편의성 위해 캐시
       action TEXT NOT NULL,           -- 'login_success' | 'login_failure' | 'logout' | 'register' | 'password_change' | 'product_create' | 'product_update' | 'product_delete'
       target_type TEXT,               -- 'user' | 'product' | NULL
       target_id TEXT,
       ip_address TEXT,                -- request.remote_addr (ProxyFix 통해 실제 IP)
       user_agent TEXT,                -- request.user_agent.string (길이 제한: 500자)
       success INTEGER NOT NULL DEFAULT 1,  -- 0/1
       created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
   );
   ```
   - 마이그레이션은 Phase 1과 동일한 패턴 (컬럼별 개별 try/except, `IF NOT EXISTS`)
7. **각 ALTER TABLE 구문은 반드시 개별 try/except로 감쌀 것** (Phase 1 §B-8 원칙 유지)

### C. 접근 로그 (강화 항목 (d))

8. `security.py`에 `log_action(action, target_type=None, target_id=None, success=True, actor_id=None, actor_username=None)` 헬퍼 추가:
   - `actor_id`가 None이면 `session.get('user_id')`에서 자동으로 가져옴 (로그인 시도 실패 케이스 등에서 명시적으로 넘길 수도 있음)
   - `ip_address`는 `request.remote_addr`에서 (ProxyFix가 이미 X-Forwarded-For 처리)
   - `user_agent`는 `request.user_agent.string[:500]`
   - **비밀번호나 password_hash 등 민감정보는 절대 로깅하지 말 것**
9. 다음 시점에 로그 기록:
   - `register()` 성공: `action='register', success=1`
   - `login()` 성공: `action='login_success', success=1`, 그리고 user 테이블의 `last_login_at`도 갱신
   - `login()` 실패 (사용자 없음 / 비밀번호 틀림 / 잠금): `action='login_failure', success=0, actor_username=<시도된 username>`
   - `logout()`: `action='logout'`
   - `new_product()` 성공: `action='product_create', target_type='product', target_id=<product_id>`
   - `edit_product()` 성공: `action='product_update', target_type='product', target_id=<product_id>`
   - `delete_product()` 성공: `action='product_delete', target_type='product', target_id=<product_id>`
   - `change_password()` 성공: `action='password_change'`

### D. Rate Limiting (강화 항목 (f))

10. `Flask-Limiter` 설치 및 초기화. `requirements.txt`에 버전 고정으로 추가.
11. 기본 저장소는 in-memory (배포 무료 티어에 Redis 없음, 단일 워커 `-w 1` 환경이라 in-memory로 충분). Limiter 초기화 시 `storage_uri="memory://"`, `key_func=get_remote_address`
12. 라우트별 제한:
    - `/register` POST: `5 per hour` (봇 대량 가입 방어)
    - `/login` POST: `10 per minute` (브루트포스 완화 — 계정 잠금과 별개 층위)
    - `/report` POST: `5 per minute` (신고 도배 방어)
    - `/profile/password` POST: `3 per hour`
    - `/product/new` POST: `10 per hour` (이미지 업로드 남용 방어)
13. 제한 초과 시 429 응답 → `@app.errorhandler(429)`로 커스텀 에러 페이지 반환 (기존 `error.html` 재사용, 메시지: "요청이 너무 많습니다. 잠시 후 다시 시도해주세요."). **핸들러 함수는 반드시 에러 객체 인자를 받도록 정의할 것**: `def handle_ratelimit(e): return render_template('error.html', code=429, message='...'), 429`. 인자 없이 `def handle_ratelimit():`로 정의하면 Flask가 인자 전달 시 TypeError로 크래시함

### E. 비밀번호 변경 및 재사용 방지 (강화 항목 (e))

14. `POST /profile/password` 라우트 신설, `@login_required`, rate limit 적용:
    - 폼 필드: `current_password`, `new_password`, `new_password_confirm`
    - **재인증**: `current_password`를 bcrypt로 검증 (틀리면 실패, `security.py`의 `log_action`에 `password_change` `success=0`으로 기록)
    - `new_password`가 `new_password_confirm`과 일치하는지 확인
    - `validate_password(new_password)` 통과 확인
    - **재사용 방지**: `user.password_history`(JSON 배열)를 로드해서 새 비번을 그 안 모든 해시 각각에 대해 `bcrypt.checkpw`로 비교. 하나라도 일치하면 거부 ("최근 사용한 비밀번호는 재사용할 수 없습니다.")
    - 현재 비번 해시도 재사용 방지 대상에 포함 (즉 현재 비번과 같으면 거부)
    - 모두 통과하면:
      - 새 비번 bcrypt 해시 생성
      - `password_history`에 **기존 비번 해시**를 push, 최근 5개만 유지(오래된 것 pop)
      - user 테이블 UPDATE: 새 password, 새 password_history JSON, **`session_token`을 새 uuid4로 갱신** (Phase 1 §3-1-B: 비번 변경 시 기존 세션 실시간 무효화)
      - 현재 세션의 `session['session_token']`도 새 값으로 갱신 (본인 세션은 유지, 다른 기기 세션은 다음 요청에서 자동 튕김)
      - `log_action('password_change', success=1)`
      - flash 메시지 후 `/profile`로 redirect
15. `profile.html`에 비밀번호 변경 폼 섹션 추가 (기존 bio 폼 아래에, 별도 form 태그). 각 폼에 `{{ csrf_token() }}` 히든 필드 유지.

### F. 공개 프로필 페이지

16. `GET /user/<user_id>` 라우트 신설:
    - 인증 불필요 (누구나 조회 가능)
    - DB 조회해서 `status == 'active'`인 유저만 표시 (suspended면 404 처럼 처리)
    - 노출 필드: `username`, `bio`, `created_at`
    - **절대 노출 금지**: `password`, `password_history`, `session_token`, `failed_login_count`, `locked_until`, `role`, `status`, `last_login_at`, 이메일 등 어떤 민감정보도 미노출
    - 해당 유저가 등록한 상품 중 `status='active'`인 것만 목록 표시
17. `templates/user_profile.html` 신규 생성. `base.html` 상속, 기존 스타일 컨벤션(§4-1) 준수
18. 상품 상세 페이지(`view_product.html`)의 판매자 username 표시 부분을 이 공개 프로필 링크로 감싸기 (`<a href="{{ url_for('user_profile', user_id=seller.id) }}">{{ seller.username }}</a>`)

### G. 상품 수정/삭제 (소유권 검증)

19. `security.py`의 `owner_required` 사용. `get_product_seller_id(product_id)` 헬퍼는 `security.py`나 별도 `products_common.py`에 두되, `app.py`가 순환 import 안 나게 배치할 것 (단일 파일 유지가 편하면 `security.py`에 두는 것 허용)
20. `GET/POST /product/<product_id>/edit`:
    - `@login_required`, `@owner_required(...)`
    - 리소스 없으면 404 처리 (라우트 본체에서)
    - 수정 가능 필드: `title`, `description`, `price`, `image` (이미지는 선택적 재업로드)
    - 검증은 `security.py`의 validator 재사용
    - 이미지 재업로드 시: 기존 이미지 파일 삭제 후 새 파일 저장 (§I 참고)
    - `log_action('product_update', ...)`
21. `POST /product/<product_id>/delete`:
    - `@login_required`, `@owner_required(...)`
    - **Soft delete**: `UPDATE product SET status = 'deleted'` (실제 row 삭제 안 함, 감사 목적)
    - 연결된 이미지 파일은 실제로 파일시스템에서 제거 (orphan 방지)
    - `log_action('product_delete', ...)`
    - 삭제 후 `/dashboard`로 redirect
22. 상품 상세 페이지(`view_product.html`)에 현재 유저가 판매자일 때만 "수정"/"삭제" 버튼 노출. 삭제는 반드시 form + POST + CSRF 토큰. **주의**: 이 UI 숨김은 편의성일 뿐 실제 보안은 서버측 `owner_required`가 담당함
23. `view_product` 라우트도 `status != 'active'`인 상품은 404 처리하도록 수정 (Phase 1까진 그냥 조회 가능이었음)
24. `dashboard.html`의 상품 목록에서도 `status='active'`인 것만 표시 (SQL WHERE 절 추가)

### H. 상품 검색

25. `GET /product/search?q=<keyword>` 라우트 신설:
    - 인증 불필요
    - `q` 파라미터 길이 1~50자 검증 (없거나 너무 길면 400 or 빈 결과)
    - **SQL**: `SELECT * FROM product WHERE title LIKE ? AND status = 'active' ORDER BY created_at DESC LIMIT 100` — 파라미터는 `f'%{q}%'` **가 아니라** `('%' + q + '%',)` 튜플로 파라미터 바인딩. `q` 안의 `%`, `_`는 SQLite LIKE 특수문자이므로 이스케이프 필요 → `q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')` 후 `LIKE ? ESCAPE '\\'` 사용
    - 결과를 `search_results.html`에 렌더링
26. `dashboard.html` 상단에 검색 폼 추가 (`GET` 요청, 검색어 input + 버튼). CSRF는 GET이라 불필요
27. `templates/search_results.html` 신규 생성

### I. 이미지 업로드 + Pillow 재인코딩

28. `Pillow` 설치, `requirements.txt` 버전 고정 추가
29. `app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024` (5MB) 설정
30. `static/uploads/products/` 디렉토리 생성 (부팅 시 `os.makedirs(..., exist_ok=True)`), `.gitignore`에 `static/uploads/products/*` 추가하되 `.gitkeep`은 예외로 커밋
31. `security.py`에 `save_product_image(file_storage)` 함수 추가:
    - `file_storage`가 None이거나 빈 파일이면 `None` 반환 (이미지 없이 상품 등록 허용)
    - 확장자 화이트리스트 검증: `.jpg .jpeg .png .gif .webp` (SVG 금지). `secure_filename(file.filename)` 사용 후 확장자만 추출
    - **1차 검증**: `Image.open(file).verify()` — 예외 나면 거부. verify 후에는 파일 포인터를 `file.seek(0)`로 되돌려야 함(verify가 파일을 소진함)
    - **2차 재인코딩** (§3-2): `Image.open(file)`로 다시 열어서 최대 해상도(예: 1920x1920) 안으로 `thumbnail()`, 그다음 새 uuid4 파일명 + 검증된 확장자로 저장. 이때 EXIF는 자동으로 사라짐. PNG는 PNG로, JPEG는 JPEG로 그대로 저장(포맷 변경 안 함).
    - **모드 호환성 처리**: Pillow 저장 전에 이미지 모드를 확인해서 각 포맷에 안전한 모드로 변환할 것. JPEG로 저장할 때는 `RGB` 모드가 아니면 `im.convert('RGB')` (RGBA/P/CMYK 등이 그대로 저장되면 `ValueError` 발생). PNG로 저장할 때는 RGBA/RGB/P 모두 OK. GIF는 원본이 애니메이션일 수 있으므로 첫 프레임만 저장 (`im.seek(0)` 후 저장). 포맷별 안전 모드 매핑을 코드에 명시적으로 둘 것 — try/except로 뭉개지 말고
    - **파일명 재확인**: 저장 파일명은 반드시 서버가 생성한 `uuid4().hex + '.' + 검증된_확장자`만 사용할 것. `secure_filename(원본파일명)`은 확장자 추출용으로만 쓰고, 저장 경로 어디에도 사용자 제공 파일명이 들어가면 안 됨 (path traversal 완전 차단)
    - 저장 성공 시 파일명 반환, 실패(어느 단계든) 시 예외를 던지지 말고 `None`과 에러 메시지 반환 → 라우트에서 flash로 안내
32. `new_product()` 라우트가 `request.form` 뿐 아니라 `request.files.get('image')`도 처리하도록 수정. `enctype="multipart/form-data"` 필수 (`new_product.html` 폼에 추가)
33. 이미지 저장 성공 시 반환된 파일명을 `product.image_filename`에 저장
34. `edit_product`, `delete_product`에서 이미지 파일 정리(교체 시 옛 파일 unlink, 삭제 시 unlink) — `os.remove(path)`를 try/except로 감싸서 파일 없어도 크래시 안 나게
35. `view_product.html`, `dashboard.html`, `search_results.html`에서 `product.image_filename`이 있으면 `<img>`로 표시(`url_for('static', filename='uploads/products/' + product.image_filename)`), 없으면 §4-1의 회색 placeholder

### J. 보안 헤더 추가 (강화 항목 (g))

36. `set_security_headers` 훅에 헤더 추가:
    - `Strict-Transport-Security`: 값 `max-age=31536000; includeSubDomains` — 단, **`FLASK_DEBUG`가 false일 때만 추가** (로컬 http 개발 시 HSTS를 걸면 브라우저가 강제로 https 리다이렉트 시도해서 개발 불편)
    - `Permissions-Policy`: 값 `camera=(), microphone=(), geolocation=(), payment=(), usb=()` — 앱이 이런 기능 자체를 안 쓰므로 전부 차단. XSS로 뚫려도 카메라/마이크/위치 접근 자체가 API 레벨에서 막힘

### K. CSS 확장

37. `base.html` 스타일 블록에 §4-1의 신규 클래스 추가: `.product-card`, `.product-thumb`, `.search-bar`, `.btn-danger`. 색상은 기존 팔레트 유지, `.btn-danger`는 `#D32F2F`

---

## 중요 지시사항

### 코드 생략 금지
Phase 1과 동일: `# ... 기존 코드 유지 ...`, `(생략)` 등 절대 사용 금지. 변경되는 함수/파일은 전체를 온전히 출력할 것.

### 인라인 스크립트 금지 (Phase 1에서 이미 교훈)
CSP `script-src`가 인라인을 허용하지 않으므로, 새로 만드는 JS는 반드시 `static/js/*.js`로 분리하고 `<script src="...">`로 로드할 것. 절대 `<script>`, `onclick="..."`, `onsubmit="..."` 등 인라인 사용 금지.

### 이미 결정된 원칙 (재확인)
- 저장은 원본, 이스케이프는 렌더링 시점(Jinja2 autoescape). `|safe` 절대 사용 금지
- 날짜/시간은 `datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')` 포맷 통일
- password/password_hash 등 민감정보는 로깅하지 말 것 (audit_log에도 절대 넣지 말 것)
- SESSION_COOKIE_SECURE/SAMESITE 설정은 그대로 유지, 완화 금지

### 마이그레이션 재실행 안전성
`init_db()`가 여러 번 실행돼도 데이터 파괴 없어야 함. product 재생성 시 특히 주의 — 이미 INTEGER인지 체크하는 가드 필수.

---

## 완료 조건 (스스로 검증할 것)

로컬(`python app.py`)에서 아래 시나리오 전부 통과 확인:

**기존 기능 회귀 없음**
- 회원가입, 로그인, 로그아웃, bio 수정, 상품 등록, 상품 상세, 신고, 전체 채팅 전부 그대로 동작

**신규 기능**
- 이미지 없이 상품 등록 → 성공, 카드에 placeholder
- 이미지 첨부 상품 등록 → 성공, static 폴더에 파일 생성됨, 재인코딩되어 EXIF 제거됐는지 `exiftool` 또는 파이썬으로 확인
- SVG 파일을 `.png`로 확장자 위장해서 업로드 시도 → `Image.open().verify()`에서 거부
- 6MB 이미지 업로드 시도 → 413 처리
- A 유저 로그인 상태에서 B 유저 상품의 `/edit` URL 직접 접근 → 403
- 본인 상품 수정 → 성공, 이미지 교체 시 옛 파일 삭제 확인
- 본인 상품 삭제 → status='deleted'로 변경, 목록/상세에서 안 보이는지 확인, 이미지 파일도 삭제됐는지
- 검색어 `'; DROP TABLE product; --` 입력 → 정상 처리(파라미터 바인딩), 검색 결과 없음 표시. 검색 후 상품 테이블 여전히 존재하는지 sqlite3로 확인
- 검색어 `%` 하나만 입력 → LIKE 특수문자 이스케이프되어 리터럴 `%` 검색으로 처리됨 (모든 상품 나오는 게 아니라)
- `/user/<본인id>` 접근 → 자기 프로필 페이지 정상 표시. HTML 소스에 password/session_token 등 민감 필드가 어디에도 안 노출되는지 grep

**보안 강화**
- 비밀번호 변경: 현재 비번 틀리게 넣으면 실패, 새 비번이 현재/이전 비번 중 하나면 거부, 통과 시 다른 브라우저에서 열어둔 세션이 다음 요청에 튕겨나가는지 확인
- Rate limit: 로그인 폼에 11번 연속 시도(10 per minute 초과) → 429 응답
- Rate limit: 회원가입 6번 연속 → 429
- HSTS: `curl -I https://secure-coding-second-market.onrender.com` 응답 헤더에 `Strict-Transport-Security` 있는지 확인 (배포 후)
- Permissions-Policy 헤더 존재 확인
- audit_log 테이블에 로그인 성공/실패, 상품 CRUD, 비번 변경 기록이 각각 들어가는지 sqlite3로 SELECT 확인. password/hash 등 민감값 절대 없어야 함

**변경 파일 목록과 각 변경이 spec.md/phase2a_prompt.md의 어느 항목에 대응하는지 커밋 메시지 또는 요약으로 정리해서 보고할 것.**

---

## 하지 말 것

- 신고 자동조치 로직, admin_action_log 테이블, `/admin` 라우트, `/admin/user/<id>/suspend` 등 관리자 기능 — Phase 2B 스코프
- `/transfer` 송금, `/message/<user_id>` 1:1 채팅, `direct_message`/`transaction` 테이블 — Phase 2C 스코프
- `report` 테이블에 `target_type` 컬럼 추가 — Phase 2B에서 신고 확장할 때 진행
- CSS 프레임워크 도입, 인라인 스크립트/이벤트 핸들러, `|safe` 필터, 저장 전 이스케이프 — 절대 금지
- password_history에 평문 저장 (반드시 bcrypt 해시들의 리스트여야 함)
- audit_log에 password/hash/session_token 등 민감정보 로깅
- SQLite에 Redis 붙이거나 Postgres로 이주 — 이번 Phase 스코프 아님